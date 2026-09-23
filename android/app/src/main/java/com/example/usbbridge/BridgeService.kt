package com.example.usbbridge

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.webkit.MimeTypeMap
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.documentfile.provider.DocumentFile
import org.json.JSONObject
import java.io.Closeable
import java.io.File
import java.io.OutputStream
import java.net.InetAddress
import java.net.ServerSocket
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.ArrayDeque
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicInteger

/**
 * Foreground service: listens on 127.0.0.1:PORT (the PC side reconnects via adb forward), one SessionHandler per connection.
 */
class BridgeService : Service() {
    override fun onBind(intent: Intent?) = null

    override fun onCreate() {
        super.onCreate()
        token = randomToken()                  // §7: regenerate on every service start
        ensureChannels(this)
        val n = buildNotification(this, "USB Bridge running (127.0.0.1:$PORT)", "Token: $token",
            ongoing = true, channelId = CHANNEL_SERVICE)
        if (Build.VERSION.SDK_INT >= 29) {
            ServiceCompat.startForeground(this, NOTIF_SERVICE, n,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        } else {
            startForeground(NOTIF_SERVICE, n)
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (running) return START_STICKY          // Re-entry guard: repeated button taps / system restart won't start a second listener
        running = true
        Thread {
            try {
                // Bind loopback only: device-side adb forward reconnects via 127.0.0.1, and other apps cannot connect directly
                val srv = ServerSocket(PORT, 8, InetAddress.getByName("127.0.0.1"))
                server = srv
                while (running) {
                    val sock = srv.accept()
                    val handler = SessionHandler(applicationContext, sock)
                    addSession(handler)
                    pool.execute(handler)
                }
            } catch (e: Exception) {
                if (running) e.printStackTrace()
            } finally {
                running = false
                runCatching { server?.close() }
                server = null
            }
        }.start()
        return START_STICKY
    }

    override fun onDestroy() {
        running = false
        runCatching { server?.close() }
        sessions.forEach { runCatching { it.close() } }
        sessions.clear()
        super.onDestroy()
    }

    companion object {
        const val PORT = 9999
        const val CHANNEL_SERVICE = "usb_bridge"
        const val CHANNEL_MESSAGE = "usb_bridge_msg"
        const val NOTIF_SERVICE = 1
        const val MAX_FILE_SIZE = 2L * 1024 * 1024 * 1024   // 2GB NFR
        private const val NOTIF_MSG_BASE = 1000
        private const val MAX_RECENT = 50
        const val PING_INTERVAL_MS = 15_000L

        @Volatile var token: String = ""
            private set

        @Volatile var running: Boolean = false
            private set

        private var server: ServerSocket? = null
        private val pool = Executors.newCachedThreadPool()
        private val sessions = CopyOnWriteArrayList<SessionHandler>()
        private val mainHandler = Handler(Looper.getMainLooper())
        private val textListeners = CopyOnWriteArrayList<(String) -> Unit>()
        private val recentTexts = ArrayDeque<String>()
        private val msgNotifSeq = AtomicInteger(0)

        fun randomToken(): String =
            ByteArray(4).also { SecureRandom().nextBytes(it) }
                .joinToString("") { "%02x".format(it) }

        /** Shared thread pool for SessionHandler (time-consuming phone-side operations like sending files must not occupy the service listener thread) */
        fun poolExecute(task: Runnable) {
            pool.execute(task)
        }

        fun addSession(s: SessionHandler) { sessions.add(s) }
        fun removeSession(s: SessionHandler) { sessions.remove(s) }

        /** Phone-side text/file send: reuse the already-established connection. Returns false if no session. */
        fun broadcastText(text: String): Boolean {
            if (sessions.isEmpty()) return false
            sessions.forEach { it.sendText(text) }
            return true
        }

        fun broadcastFile(file: File, cleanup: Boolean = false): Boolean {
            val target = sessions.firstOrNull()
            if (target == null) {
                if (cleanup) runCatching { file.delete() }
                return false
            }
            target.sendFile(file, cleanup)
            return true
        }

        /** UI subscribes to PC -> phone text; the callback always runs on the main thread. Returns the currently cached recent messages. */
        fun addTextListener(listener: (String) -> Unit): List<String> {
            textListeners.add(listener)
            synchronized(recentTexts) { return recentTexts.toList() }
        }

        fun removeTextListener(listener: (String) -> Unit) {
            textListeners.remove(listener)
        }

        fun dispatchIncomingText(text: String) {
            synchronized(recentTexts) {
                recentTexts.addLast(text)
                while (recentTexts.size > MAX_RECENT) recentTexts.removeFirst()
            }
            mainHandler.post {
                textListeners.forEach { runCatching { it(text) } }
            }
        }

        fun ensureChannels(ctx: Context) {
            if (Build.VERSION.SDK_INT < 26) return
            val nm = ctx.getSystemService(NotificationManager::class.java) ?: return
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL_SERVICE, "USB Bridge", NotificationManager.IMPORTANCE_LOW))
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL_MESSAGE, "PC messages", NotificationManager.IMPORTANCE_DEFAULT))
        }

        fun buildNotification(
            ctx: Context,
            title: String,
            text: String,
            ongoing: Boolean = false,
            channelId: String = CHANNEL_SERVICE
        ): Notification {
            val open = PendingIntent.getActivity(
                ctx, 0, Intent(ctx, MainActivity::class.java),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
            return NotificationCompat.Builder(ctx, channelId)
                .setContentTitle(title)
                .setContentText(text)
                .setStyle(NotificationCompat.BigTextStyle().bigText(text))
                .setSmallIcon(R.drawable.ic_notification)
                .setContentIntent(open)
                .setAutoCancel(!ongoing)
                .setOngoing(ongoing)
                .setPriority(
                    if (ongoing) NotificationCompat.PRIORITY_LOW
                    else NotificationCompat.PRIORITY_DEFAULT)
                .build()
        }

        fun nextMessageNotificationId(): Int =
            NOTIF_MSG_BASE + (msgNotifSeq.getAndIncrement() and 0x0FFF)
    }
}

/** A single PC connection: parse frames, write files to disk, run remote sessions. */
class SessionHandler(private val ctx: Context, private val sock: java.net.Socket) : Runnable, Closeable {
    private val out: OutputStream = sock.getOutputStream()
    private val outLock = Any()
    private val remotes = ConcurrentHashMap<Int, RemoteSession>()
    private val sinks = ConcurrentHashMap<Int, FileSink>()

    /** Remote channel operations run serially: guarantees OPEN -> DATA ordering without blocking the main read loop (TEXT/FILE/PING are unaffected by the SSH handshake) */
    private val remoteOps = Executors.newSingleThreadExecutor()

    @Volatile private var closed = false
    @Volatile private var authed = false
    private var pingThread: Thread? = null

    override fun run() {
        try {
            val input = sock.getInputStream()
            while (!closed) handle(FrameIO.readFrame(input) ?: break)
        } catch (e: Exception) {
            if (!closed) e.printStackTrace()
        } finally {
            close()
        }
    }

    fun send(type: Int, header: JSONObject, payload: ByteArray = ByteArray(0)) {
        if (closed) return
        val data = FrameIO.encode(type, header, payload)
        synchronized(outLock) {
            out.write(data)
            out.flush()
        }
    }

    private fun startPingLoop() {
        pingThread = Thread({
            while (!closed) {
                try {
                    Thread.sleep(BridgeService.PING_INTERVAL_MS)
                } catch (_: InterruptedException) {
                    return@Thread
                }
                if (closed) break
                runCatching { send(FrameIO.PING, JSONObject()) }
            }
        }, "bridge-ping").apply { isDaemon = true; start() }
    }

    private fun JSONObject.optNullableString(key: String): String? =
        if (has(key) && !isNull(key)) optString(key) else null

    private fun handle(f: FrameIO.Frame) {
        with(f.header) {
            // The first frame must be a HELLO with the correct token, otherwise reject
            if (!authed) {
                if (f.type == FrameIO.HELLO && optString("token") == BridgeService.token) {
                    authed = true
                    send(FrameIO.ACK, JSONObject().put("ok", true).put("device", Build.MODEL))
                    startPingLoop()
                } else {
                    send(FrameIO.ERROR, JSONObject().put("code", "BAD_TOKEN").put("message", "token verification failed"))
                    close()
                }
                return
            }
            when (f.type) {
                FrameIO.PING -> send(FrameIO.PONG, JSONObject())
                FrameIO.PONG -> {}                             // peer keepalive reply

                FrameIO.TEXT -> {                                      // PC -> phone text
                    notifyText(ctx, optString("text"))
                    send(FrameIO.ACK, JSONObject().put("id", optInt("id")))
                }

                FrameIO.FILE_META -> {
                    val id = optInt("id")
                    val size = optLong("size")
                    if (size > BridgeService.MAX_FILE_SIZE) {
                        send(FrameIO.ERROR, JSONObject().put("code", "FILE_WRITE_FAILED")
                            .put("id", id).put("message", "file exceeds 2GB limit"))
                    } else {
                        try {
                            sinks[id] = FileSink.create(ctx, f.header)
                        } catch (e: Exception) {                           // Disk full / no permission, etc.
                            send(FrameIO.ERROR, JSONObject().put("code", "FILE_WRITE_FAILED")
                                .put("id", id).put("message", e.message ?: ""))
                        }
                    }
                }

                FrameIO.FILE_CHUNK -> {
                    val id = optInt("id")
                    try {
                        sinks[id]?.append(f.payload)
                    } catch (e: Exception) {
                        sinks.remove(id)?.abort()
                        send(FrameIO.ERROR, JSONObject().put("code", "FILE_WRITE_FAILED")
                            .put("id", id).put("message", e.message ?: ""))
                    }
                }

                FrameIO.FILE_END -> {
                    val id = optInt("id")
                    val sha = optNullableString("sha256")
                    val path = sinks.remove(id)?.finish(optBoolean("ok"), sha)
                    send(FrameIO.ACK, JSONObject()
                        .put("file_id", id).put("ok", path != null).put("path", path ?: ""))
                }

                FrameIO.REMOTE_OPEN -> {
                    // Legacy PC credential open removed from product path; reject politely
                    send(FrameIO.ERROR, JSONObject().put("code", "CHANNEL_OPEN_FAILED")
                        .put("message", "use CONN_ATTACH with a phone connection id"))
                }

                FrameIO.REMOTE_DATA -> {
                    val d = f.payload; val c = optInt("channel")
                    post {
                        if (!ConnectionHub.writeStdin(c, d)) {
                            runCatching { remotes[c]?.writeStdin(d) }
                        }
                    }
                }

                FrameIO.REMOTE_CLOSE -> {
                    val c = optInt("channel")
                    post {
                        ConnectionHub.detach(c)
                        runCatching { remotes.remove(c)?.close() }
                    }
                }

                FrameIO.CONN_LIST -> {
                    send(FrameIO.CONN_LIST_RESULT,
                        JSONObject().put("connections", ConnectionHub.snapshot(ctx)))
                }

                FrameIO.CONN_ATTACH -> {
                    val ch = optInt("channel")
                    val cid = optString("connection_id")
                    try {
                        val backlog = ConnectionHub.attach(this@SessionHandler, ch, cid)
                        send(FrameIO.ACK, JSONObject()
                            .put("channel", ch).put("connection_id", cid).put("ok", true),
                            backlog)
                    } catch (e: ConnectionHub.HubException) {
                        send(FrameIO.ERROR, JSONObject()
                            .put("channel", ch).put("code", e.code).put("message", e.message ?: ""))
                    } catch (e: Exception) {
                        send(FrameIO.ERROR, JSONObject()
                            .put("channel", ch).put("code", "CHANNEL_OPEN_FAILED")
                            .put("message", e.message ?: ""))
                    }
                }

                FrameIO.CONN_DETACH -> {
                    ConnectionHub.detach(optInt("channel"))
                    send(FrameIO.ACK, JSONObject().put("ok", true).put("channel", optInt("channel")))
                }
                else -> {}
            }
        }
    }

    private fun post(task: () -> Unit) {
        runCatching { remoteOps.execute(task) }        // The queue is shut down after close(), so new tasks are ignored
    }

    // ---------- Feature 1/2: phone -> PC text and files ----------
    fun sendText(text: String) {
        // Must not run on the UI thread (StrictMode NetworkOnMainThreadException)
        BridgeService.poolExecute {
            send(FrameIO.TEXT, JSONObject().put("id", FrameIO.nextId()).put("text", text))
        }
    }

    fun sendFile(file: File, cleanup: Boolean = false) {
        BridgeService.poolExecute {
            val id = FrameIO.nextId()
            runCatching {
                if (file.length() > BridgeService.MAX_FILE_SIZE) {
                    error("file exceeds 2GB limit")
                }
                val md = MessageDigest.getInstance("SHA-256")
                send(FrameIO.FILE_META, JSONObject()
                    .put("id", id).put("name", file.name).put("size", file.length()))
                file.inputStream().use { ins ->
                    val buf = ByteArray(256 * 1024)
                    while (true) {
                        val n = ins.read(buf)
                        if (n <= 0) break
                        md.update(buf, 0, n)
                        send(FrameIO.FILE_CHUNK, JSONObject().put("id", id), buf.copyOf(n))
                    }
                }
                val hex = md.digest().joinToString("") { "%02x".format(it) }
                send(FrameIO.FILE_END, JSONObject().put("id", id).put("ok", true).put("sha256", hex))
            }.onFailure {
                send(FrameIO.FILE_END, JSONObject().put("id", id).put("ok", false)
                    .put("error", it.message ?: ""))
            }
            if (cleanup) runCatching { file.delete() }          // What is sent is a cached copy, cleaned up after sending
        }
    }

    // ---------- Feature 3: remote session (managed by ConnectionHub) ----------

    private fun notifyText(ctx: Context, text: String) {
        // Always write to the in-app log (does not depend on notification permission)
        BridgeService.dispatchIncomingText(text)
        if (Build.VERSION.SDK_INT >= 33 &&
            ctx.checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED) return
        // A notification failure must not bring down the session thread / main thread (a bad icon, etc. kills the process with RemoteServiceException)
        runCatching {
            BridgeService.ensureChannels(ctx)
            val nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            nm.notify(
                BridgeService.nextMessageNotificationId(),
                BridgeService.buildNotification(
                    ctx, "Message from PC", text,
                    ongoing = false, channelId = BridgeService.CHANNEL_MESSAGE))
        }.onFailure { it.printStackTrace() }
    }

    override fun close() {
        if (closed) return
        closed = true
        pingThread?.interrupt()
        pingThread = null
        ConnectionHub.detachHandler(this)
        remoteOps.shutdownNow()
        remotes.values.forEach { it.close() }
        sinks.values.forEach { it.abort() }
        BridgeService.removeSession(this)
        runCatching { sock.close() }
    }
}

/** Write received files to the user-chosen folder (SAF) or the app private dir; SHA256 + name(1).ext. */
class FileSink private constructor(
    private val fos: OutputStream,
    private val displayPath: String,
    private val deleteOnFail: () -> Unit
) {
    private val md = MessageDigest.getInstance("SHA-256")

    fun append(chunk: ByteArray) = synchronized(fos) {
        fos.write(chunk)
        md.update(chunk)
    }

    fun finish(ok: Boolean, sha256: String?): String? = synchronized(fos) {
        fos.close()
        val actual = md.digest().joinToString("") { "%02x".format(it) }
        val good = ok && (sha256.isNullOrEmpty() || sha256 == actual)
        if (!good) {
            deleteOnFail(); null
        } else displayPath
    }

    fun abort() {
        runCatching { fos.close() }
        runCatching { deleteOnFail() }
    }

    companion object {
        fun createDir(ctx: Context): File = ctx.getExternalFilesDir(null) ?: ctx.filesDir

        /** Consistent with the PC side: a.txt -> a(1).txt */
        fun uniqueName(dir: File, name: String): File {
            val safe = File(name).name
            var f = File(dir, safe)
            if (!f.exists()) return f
            val dot = safe.lastIndexOf('.')
            val stem = if (dot > 0) safe.substring(0, dot) else safe
            val ext = if (dot > 0) safe.substring(dot) else ""
            var i = 1
            while (f.exists()) {
                f = File(dir, "$stem($i)$ext")
                i++
            }
            return f
        }

        private fun uniqueDocument(tree: DocumentFile, name: String): DocumentFile {
            val safe = File(name).name
            val mime = mimeFor(safe)
            if (tree.findFile(safe) == null) {
                return tree.createFile(mime, safe)
                    ?: error("cannot create file in chosen folder")
            }
            val dot = safe.lastIndexOf('.')
            val stem = if (dot > 0) safe.substring(0, dot) else safe
            val ext = if (dot > 0) safe.substring(dot) else ""
            var i = 1
            while (true) {
                val candidate = "$stem($i)$ext"
                if (tree.findFile(candidate) == null) {
                    return tree.createFile(mime, candidate)
                        ?: error("cannot create file in chosen folder")
                }
                i++
            }
        }

        private fun mimeFor(name: String): String {
            val ext = name.substringAfterLast('.', "").lowercase()
            if (ext.isEmpty()) return "application/octet-stream"
            return MimeTypeMap.getSingleton().getMimeTypeFromExtension(ext)
                ?: "application/octet-stream"
        }

        fun create(ctx: Context, h: JSONObject): FileSink {
            val name = h.optString("name", "file_${h.optInt("id")}")
            val treeUri = ReceiveDirPrefs.getTreeUri(ctx)
            if (treeUri != null) {
                val tree = DocumentFile.fromTreeUri(ctx, treeUri)
                    ?: error("chosen save folder is no longer available")
                if (!tree.canWrite()) error("chosen save folder is not writable")
                val doc = uniqueDocument(tree, name)
                val os = ctx.contentResolver.openOutputStream(doc.uri)
                    ?: error("cannot open output stream for ${doc.name}")
                return FileSink(os, doc.uri.toString()) { runCatching { doc.delete() } }
            }
            val file = uniqueName(createDir(ctx), name)
            return FileSink(file.outputStream(), file.absolutePath) { runCatching { file.delete() } }
        }
    }
}
