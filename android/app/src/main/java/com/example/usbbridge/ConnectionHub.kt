package com.example.usbbridge

import android.content.Context
import com.jcraft.jsch.HostKeyRepository
import com.jcraft.jsch.JSchException
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.Executors

/**
 * Owns long-lived SSH sessions keyed by connection profile id (ID_01…).
 * Survives USB SessionHandler teardown; PC attaches/detaches independently.
 */
object ConnectionHub {
    const val STATE_STOPPED = "stopped"
    const val STATE_STARTING = "starting"
    const val STATE_RUNNING = "running"
    const val STATE_ERROR = "error"

    private const val RING_MAX = 64 * 1024

    data class Attach(
        val channel: Int,
        val handler: SessionHandler
    )

    data class Live(
        val id: String,
        @Volatile var state: String = STATE_STOPPED,
        @Volatile var session: RemoteSession? = null,
        @Volatile var lastError: String? = null,
        val ring: ByteArrayOutputStream = ByteArrayOutputStream(),
        @Volatile var attach: Attach? = null
    )

    private val lives = ConcurrentHashMap<String, Live>()
    private val listeners = CopyOnWriteArrayList<() -> Unit>()
    private val startPool = Executors.newCachedThreadPool()
    private val lock = Any()

    fun addListener(l: () -> Unit) { listeners.add(l) }
    fun removeListener(l: () -> Unit) { listeners.remove(l) }
    private fun notifyListeners() { listeners.forEach { runCatching { it() } } }

    fun ensureSlots(ctx: Context) {
        ConnectionStore.list(ctx).forEach { p ->
            lives.putIfAbsent(p.id, Live(p.id))
        }
        // Drop lives for deleted profiles that are stopped
        val ids = ConnectionStore.list(ctx).map { it.id }.toSet()
        lives.keys.toList().forEach { id ->
            if (id !in ids && lives[id]?.state == STATE_STOPPED) lives.remove(id)
        }
    }

    fun snapshot(ctx: Context): JSONArray {
        ensureSlots(ctx)
        val arr = JSONArray()
        ConnectionStore.list(ctx).forEach { p ->
            val live = lives[p.id]
            arr.put(JSONObject()
                .put("id", p.id)
                .put("name", p.name)
                .put("host", p.host)
                .put("port", p.port)
                .put("user", p.user)
                .put("state", live?.state ?: STATE_STOPPED)
                .put("error", live?.lastError ?: ""))
        }
        return arr
    }

    fun stateOf(id: String): String = lives[id]?.state ?: STATE_STOPPED

    fun lastError(id: String): String? = lives[id]?.lastError

    fun start(ctx: Context, id: String) {
        ensureSlots(ctx)
        val profile = ConnectionStore.get(ctx, id) ?: run {
            AuthPrompts.toast(ctx, "Unknown connection $id")
            return
        }
        val live = lives.getOrPut(id) { Live(id) }
        synchronized(lock) {
            if (live.state == STATE_RUNNING || live.state == STATE_STARTING) {
                AuthPrompts.toast(ctx, "$id already ${live.state}")
                return
            }
            live.state = STATE_STARTING
            live.lastError = null
        }
        notifyListeners()
        startPool.execute {
            try {
                openWithTofu(ctx, profile, live)
                synchronized(lock) {
                    live.state = STATE_RUNNING
                    live.lastError = null
                }
                AuthPrompts.toast(ctx, "$id connected")
            } catch (e: Exception) {
                live.session?.close()
                live.session = null
                synchronized(lock) {
                    live.state = STATE_ERROR
                    live.lastError = e.message ?: e.javaClass.simpleName
                }
                AuthPrompts.toast(ctx, "$id failed: ${live.lastError}")
            }
            notifyListeners()
        }
    }

    fun stop(id: String) {
        val live = lives[id] ?: return
        val att = synchronized(lock) {
            val a = live.attach
            live.attach = null
            live.session?.close()
            live.session = null
            live.state = STATE_STOPPED
            live.lastError = null
            a
        }
        att?.let { a ->
            runCatching {
                a.handler.send(FrameIO.REMOTE_CLOSE,
                    JSONObject().put("channel", a.channel).put("code", 0).put("reason", "stopped on phone"))
            }
        }
        notifyListeners()
    }

    /** Attach PC channel to a running connection; returns backlog bytes. */
    fun attach(handler: SessionHandler, channel: Int, connectionId: String): ByteArray {
        val live = lives[connectionId] ?: throw HubException("CONN_NOT_FOUND", "unknown id")
        synchronized(lock) {
            if (live.state != STATE_RUNNING || live.session == null)
                throw HubException("CONN_NOT_RUNNING", "connection is not running")
            if (live.attach != null)
                throw HubException("CONN_BUSY", "already attached")
            live.attach = Attach(channel, handler)
            return live.ring.toByteArray()
        }
    }

    fun detach(channel: Int) {
        lives.values.forEach { live ->
            synchronized(lock) {
                if (live.attach?.channel == channel) live.attach = null
            }
        }
    }

    fun detachHandler(handler: SessionHandler) {
        lives.values.forEach { live ->
            synchronized(lock) {
                if (live.attach?.handler === handler) live.attach = null
            }
        }
    }

    fun writeStdin(channel: Int, data: ByteArray): Boolean {
        lives.values.forEach { live ->
            val att = live.attach
            if (att != null && att.channel == channel) {
                live.session?.writeStdin(data)
                return true
            }
        }
        return false
    }

    private fun openWithTofu(ctx: Context, profile: ConnectionStore.Profile, live: Live) {
        val store = TofuHostKeys.storeFile(ctx)
        fun attempt(approved: String?): RemoteSession {
            val keys = TofuHostKeys(store, approved)
            val bridge = object : RemoteSession.AuthBridge {
                override fun askPassword(message: String): String? =
                    AuthPrompts.promptText("SSH password", message, "password")
                override fun askKeyboardInteractive(
                    destination: String, name: String, instruction: String,
                    prompt: Array<String>, echo: BooleanArray
                ): Array<String>? {
                    val out = Array(prompt.size) { "" }
                    for (i in prompt.indices) {
                        val ans = AuthPrompts.promptText(
                            if (profile.mfa) "MFA / Authenticator" else "SSH challenge",
                            listOf(destination, name, instruction, prompt[i])
                                .filter { it.isNotBlank() }.joinToString("\n"),
                            if (echo.getOrElse(i) { true }) "response" else "code")
                            ?: return null
                        out[i] = ans
                    }
                    return out
                }
            }
            val rs = RemoteSession(keys,
                { stream, data -> onOutput(live, stream, data) },
                { code, reason -> onSessionClosed(live, code, reason) },
                bridge)
            try {
                rs.openSsh(profile.host, profile.port, profile.user,
                    RemoteSession.Auth(profile.password, profile.privateKey, profile.passphrase, profile.mfa))
                return rs
            } catch (e: Exception) {
                rs.close()
                if (keys.lastResult == HostKeyRepository.NOT_INCLUDED) {
                    val fp = keys.lastFingerprint ?: throw e
                    val host = keys.lastHost ?: profile.host
                    val ok = AuthPrompts.promptYesNo(
                        "Trust host key?",
                        "First connection to $host\n\nFingerprint:\n$fp\n\nTrust and continue?")
                    if (ok) return attempt(fp)
                }
                if (keys.lastResult == HostKeyRepository.CHANGED) {
                    val newFp = keys.lastFingerprint ?: throw e
                    val hostLabel = keys.lastHost ?: profile.host
                    val oldFp = keys.lastRecordedFingerprint
                        ?: TofuHostKeys.fingerprintOf(store, profile.host, profile.port)
                    when (AuthPrompts.promptHostKeyChanged(hostLabel, oldFp, newFp)) {
                        AuthPrompts.HostKeyChangedChoice.FORGET_AND_RETRUST -> {
                            TofuHostKeys.forgetHost(store, profile.host, profile.port)
                            keys.lastHost?.let { TofuHostKeys.forgetExact(store, it) }
                            return attempt(newFp)
                        }
                        AuthPrompts.HostKeyChangedChoice.CANCEL ->
                            throw JSchException("HOST_KEY_CHANGED: $newFp")
                    }
                }
                throw e
            }
        }
        live.session = attempt(null)
    }

    private fun onOutput(live: Live, stream: String, data: ByteArray) {
        synchronized(live.ring) {
            live.ring.write(data)
            val bytes = live.ring.toByteArray()
            if (bytes.size > RING_MAX) {
                live.ring.reset()
                live.ring.write(bytes, bytes.size - RING_MAX, RING_MAX)
            }
        }
        val att = live.attach ?: return
        runCatching {
            att.handler.send(FrameIO.REMOTE_OUTPUT,
                JSONObject().put("channel", att.channel).put("stream", stream), data)
        }
    }

    private fun onSessionClosed(live: Live, code: Int, reason: String) {
        val att = synchronized(lock) {
            val a = live.attach
            live.attach = null
            live.session = null
            if (live.state == STATE_RUNNING) live.state = STATE_STOPPED
            a
        }
        att?.let { a ->
            runCatching {
                a.handler.send(FrameIO.REMOTE_CLOSE,
                    JSONObject().put("channel", a.channel).put("code", code).put("reason", reason))
            }
        }
        notifyListeners()
    }

    class HubException(val code: String, override val message: String) : Exception(message)
}
