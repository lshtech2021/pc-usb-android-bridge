package com.example.usbbridge

import android.util.Log
import com.jcraft.jsch.Channel
import com.jcraft.jsch.ChannelExec
import com.jcraft.jsch.ChannelSftp
import com.jcraft.jsch.ChannelShell
import com.jcraft.jsch.JSch
import com.jcraft.jsch.Session
import com.jcraft.jsch.UIKeyboardInteractive
import com.jcraft.jsch.UserInfo
import org.bouncycastle.jce.provider.BouncyCastleProvider
import java.io.OutputStream
import java.security.Security
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.ThreadPoolExecutor
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * SSH session via mwiede JSch + Bouncy Castle (OpenSSH/PEM, ed25519 host keys & client keys).
 * Auth prompts (password / MFA / key passphrase) go through [prompt] on the phone.
 */
class RemoteSession(
    private val hostKeys: TofuHostKeys,
    private val onOutput: (stream: String, data: ByteArray) -> Unit,
    private val onClose: (code: Int, reason: String) -> Unit,
    private val prompt: AuthBridge? = null
) {
    private var session: Session? = null
    @Volatile private var channel: Channel? = null
    @Volatile private var stdinOut: OutputStream? = null
    private val stdinLock = Any()
    private val dead = AtomicBoolean(false)

    /** Serial, bounded stdin writer. SSH flow control can block a write for a long time
     *  (Ctrl+S, a stopped shell); that must never block the bridge read loop or the
     *  remoteOps queue that also carries attach/SFTP work. Single thread keeps PC input
     *  ordering; a full queue means the session is unusable, so it is closed. */
    private val stdinExec = ThreadPoolExecutor(
        1, 1, 0L, TimeUnit.MILLISECONDS,
        ArrayBlockingQueue(STDIN_QUEUE_CAPACITY),
        { r -> Thread(r, "ssh-stdin").apply { isDaemon = true } }
    )

    data class Auth(
        val password: String?,
        val privateKey: String?,
        val passphrase: String?,
        val mfa: Boolean = false
    )

    /** Callbacks for interactive auth on the phone UI thread (via AuthPrompts). */
    interface AuthBridge {
        fun askPassword(message: String): String?
        fun askPassphrase(message: String): String?
        fun askKeyboardInteractive(destination: String, name: String, instruction: String,
                                   prompt: Array<String>, echo: BooleanArray): Array<String>?
    }

    fun openSsh(host: String, port: Int, user: String, auth: Auth) = connect(host, port, user, auth) {
        val ch = session!!.openChannel("shell") as ChannelShell
        ch.setPty(true)
        ch.setPtySize(120, 30, 0, 0)
        runCatching { ch.setEnv("TERM", "xterm-256color") }
        // Write stdin straight to the channel. A PipedInputStream pump thread dies
        // silently when its last writer thread is torn down (USB SessionHandler
        // close -> remoteOps.shutdownNow), while the SSH channel keeps reporting
        // connected; the next attach then fails with "Read end dead" on the first
        // key press. A channel OutputStream reports a closed channel as an ordinary
        // IOException on the writer instead.
        val out = ch.getOutputStream()
        ch.setOutputStream(fwdStream("out"))
        ch.connect(10_000)
        channel = ch
        stdinOut = out
        watchClose(ch) { markDead(0, "shell exited") }
    }

    fun execSsh(host: String, port: Int, user: String, auth: Auth, command: String) =
        connect(host, port, user, auth) {
            val ch = session!!.openChannel("exec") as ChannelExec
            ch.setCommand(command)
            val out = ch.getOutputStream()
            ch.setOutputStream(fwdStream("out"))
            ch.setErrStream(fwdStream("err"))
            ch.connect(10_000)
            channel = ch
            stdinOut = out
            watchClose(ch) { markDead(ch.exitStatus, "exec finished") }
        }

    /** Open an SFTP channel on the connected session (file explorer feature).
     *  The caller tears it down with ChannelSftp.disconnect() only — never [close],
     *  which would kill the shell channel and the whole session. */
    fun openSftp(): ChannelSftp? {
        val s = session ?: return null
        val ch = s.openChannel("sftp") as ChannelSftp          // Android's default charset is UTF-8
        ch.connect(10_000)             // throws JSchException when the server has no sftp subsystem
        return ch
    }

    private fun connect(host: String, port: Int, user: String, auth: Auth, block: () -> Unit) {
        ensureCryptoProviders()
        installJschLogger()
        val jsch = JSch()
        if (!auth.privateKey.isNullOrBlank()) {
            jsch.addIdentity("bridge", auth.privateKey.toByteArray(Charsets.UTF_8), null,
                auth.passphrase?.takeIf { it.isNotEmpty() }?.toByteArray(Charsets.UTF_8))
        }
        val s = jsch.getSession(user, host, port)
        // A phone network that silently drops the idle TCP flow (NAT/firewall timeout)
        // otherwise leaves `isConnected` true forever: no data flows, but the hub and
        // UI still advertise the session as running. Keepalives detect that and let
        // watchClose clear the state (PC receives REMOTE_CLOSE).
        s.setServerAliveInterval(15_000)
        s.setServerAliveCountMax(3)
        auth.password?.takeIf { it.isNotEmpty() }?.let { s.setPassword(it) }
        s.setConfig("StrictHostKeyChecking", "yes")
        // Match modern OpenSSH preference: ed25519 host key first (same SHA256 as `ssh -vvv`)
        s.setConfig("server_host_key", OPENSSH_HOST_KEY_ORDER)
        s.setConfig("PubkeyAcceptedAlgorithms", OPENSSH_PUBKEY_ORDER)
        s.setHostKeyRepository(hostKeys)

        Log.i(TAG, "auth $user@$host:$port passwordChars=${auth.password?.length ?: 0} " +
            "key=${!auth.privateKey.isNullOrBlank()} passphrase=${!auth.passphrase.isNullOrBlank()} " +
            "mfa=${auth.mfa}")
        var effectivePassword = auth.password
        var effectivePassphrase = auth.passphrase
        var passwordPrompts = 0
        if (auth.mfa || prompt != null || !auth.privateKey.isNullOrBlank()) {
            s.setUserInfo(object : UserInfo, UIKeyboardInteractive {
                override fun getPassword(): String? {
                    // Reuse an answer typed for an earlier prompt: JSch calls
                    // promptPassword() then getPassword(), and both must agree.
                    effectivePassword?.takeIf { it.isNotEmpty() }?.let { return it }
                    val p = prompt?.askPassword("SSH password for $user@$host") ?: return null
                    effectivePassword = p
                    s.setPassword(p)
                    return p
                }
                override fun promptYesNo(message: String?) = false
                override fun getPassphrase(): String? = effectivePassphrase
                override fun promptPassphrase(message: String?): Boolean {
                    if (!effectivePassphrase.isNullOrEmpty()) return true
                    val p = prompt?.askPassphrase(message ?: "Private key passphrase") ?: return false
                    effectivePassphrase = p
                    return true
                }
                override fun promptPassword(message: String?): Boolean {
                    if (!effectivePassword.isNullOrEmpty()) return true
                    val p = prompt?.askPassword(message ?: "Password") ?: return false
                    effectivePassword = p
                    s.setPassword(p)
                    return true
                }
                override fun showMessage(message: String?) {}
                override fun promptKeyboardInteractive(
                    destination: String?, name: String?, instruction: String?,
                    promptArr: Array<String>?, echo: BooleanArray?
                ): Array<String>? {
                    if (promptArr == null || echo == null) return null
                    Log.d(TAG, "keyboard-interactive: ${promptArr.joinToString(" | ")} " +
                        "echo=${echo.contentToString()}")
                    // JSch only auto-answers a single English "Password:" prompt.
                    // Servers whose PAM asks for the password again (after a rejected
                    // OTP) or localises the prompt ("密码：") would otherwise reach the
                    // MFA dialog, and the user would type the OTP into the password
                    // field. Answer password prompts here, before the UI bridge.
                    val answers = Array(promptArr.size) { "" }
                    val uiPrompts = ArrayList<Int>(promptArr.size)
                    for (i in promptArr.indices) {
                        if (!echo[i] && AuthPromptText.isPassword(promptArr[i])) {
                            // JSch answers the first "Password:" prompt itself when a
                            // saved password exists. If one reaches this callback, the
                            // saved password was already rejected (or the server sent
                            // password + code together): ask the user instead of
                            // retrying the same stale password until the server drops
                            // the connection.
                            val combined = promptArr.size > 1 ||
                                promptArr.any { AuthPromptText.isCode(it) }
                            val retrying = (passwordPrompts > 0) ||
                                (!auth.password.isNullOrEmpty() && !combined)
                            val note = if (retrying) {
                                "\n\nThe server did not accept the previous password. " +
                                    "Enter it again (check the IME is in half-width/English mode)."
                            } else ""
                            Log.i(TAG, "asking for SSH password (savedChars=" +
                                "${auth.password?.length ?: 0} retry=$retrying)")
                            val p = prompt?.askPassword("SSH password for $user@$host$note")
                                ?: return null
                            Log.i(TAG, "password entered, chars=${p.length}")
                            passwordPrompts++
                            effectivePassword = p
                            s.setPassword(p)
                            answers[i] = p
                        } else {
                            uiPrompts.add(i)
                        }
                    }
                    if (uiPrompts.isNotEmpty()) {
                        val rest = Array(uiPrompts.size) { promptArr[uiPrompts[it]] }
                        val restEcho = BooleanArray(uiPrompts.size) { echo[uiPrompts[it]] }
                        val typed = prompt?.askKeyboardInteractive(
                            destination ?: "", name ?: "", instruction ?: "", rest, restEcho)
                            ?: return null
                        uiPrompts.forEachIndexed { k, i ->
                            answers[i] = typed.getOrElse(k) { "" }
                        }
                    }
                    return answers
                }
            })
            when {
                auth.mfa ->
                    s.setConfig("PreferredAuthentications", "keyboard-interactive,password,publickey")
                !auth.privateKey.isNullOrBlank() ->
                    s.setConfig("PreferredAuthentications", "publickey,keyboard-interactive,password")
            }
        }
        s.connect(30_000)
        session = s
        block()
    }

    private fun watchClose(ch: Channel, done: () -> Unit) = Thread {
        while (ch.isConnected) {
            try { Thread.sleep(200) } catch (_: InterruptedException) { return@Thread }
        }
        done()
    }.apply { isDaemon = true }.start()

    private fun fwdStream(stream: String) = object : OutputStream() {
        override fun write(b: Int) = write(byteArrayOf(b.toByte()))
        override fun write(b: ByteArray, off: Int, len: Int) =
            onOutput(stream, b.copyOfRange(off, off + len))
    }

    /**
     * Queue PC input for the SSH channel.
     *
     * Returns false when the session is gone or its stdin backlog is saturated, so the
     * caller can detach and tell the PC. Writes themselves run on [stdinExec]: an SSH
     * write can block on flow control, and it must not stall the remoteOps queue or the
     * USB read loop. Write failures mark the session dead, which clears the hub state
     * and sends REMOTE_CLOSE. This never throws: an exception escaping here used to be
     * thrown on a ThreadPoolExecutor worker, whose uncaught handler kills the process.
     */
    fun writeStdin(data: ByteArray): Boolean {
        if (dead.get()) return false
        return try {
            stdinExec.execute { writeNow(data) }
            true
        } catch (_: RejectedExecutionException) {
            // Queue full (or shutdown in progress): the session can no longer accept
            // input in bounded time. Close it instead of dropping keystrokes silently.
            Log.w(TAG, "stdin queue full, closing session")
            markDead(0, "stdin backlog")
            false
        }
    }

    private fun writeNow(data: ByteArray) {
        synchronized(stdinLock) {
            val ch = channel
            val out = stdinOut
            if (dead.get() || ch == null || out == null || !ch.isConnected || ch.isClosed) return
            try {
                out.write(data)
                out.flush()
            } catch (e: Exception) {
                Log.w(TAG, "stdin write failed: ${e.message}")
                markDead(0, "stdin write failed: ${e.message}")
            }
        }
    }

    fun isAlive(): Boolean = !dead.get() && channel?.isConnected == true

    fun close() {
        // ConnectionHub already owns the state transition here (stop / failed start).
        shutdown(notifyHub = false)
    }

    private fun markDead(code: Int, reason: String) {
        shutdown(notifyHub = true, code = code, reason = reason)
    }

    /** Single-shot teardown; [notifyHub] is false when the caller updates hub state itself. */
    private fun shutdown(notifyHub: Boolean, code: Int = 0, reason: String = "") {
        if (!dead.compareAndSet(false, true)) return
        runCatching { stdinExec.shutdownNow() }
        runCatching { stdinOut?.close() }
        runCatching { channel?.disconnect() }
        runCatching { session?.disconnect() }
        stdinOut = null
        if (notifyHub) onClose(code, reason)
    }

    companion object {
        private const val TAG = "RemoteSession"
        private const val STDIN_QUEUE_CAPACITY = 128

        @Volatile private var loggerReady = false

        /** Surface JSch's INFO lines (auth methods, host key negotiation) in logcat. */
        private fun installJschLogger() {
            if (loggerReady) return
            synchronized(this) {
                if (loggerReady) return
                runCatching {
                    JSch.setLogger(object : com.jcraft.jsch.Logger {
                        override fun isEnabled(level: Int) =
                            level >= com.jcraft.jsch.Logger.INFO

                        override fun log(level: Int, message: String) {
                            Log.i("JSch", message)
                        }
                    })
                }
                loggerReady = true
            }
        }

        /** OpenSSH-like HostKeyAlgorithms order (certs omitted for simplicity). */
        private const val OPENSSH_HOST_KEY_ORDER =
            "ssh-ed25519,ecdsa-sha2-nistp256,ecdsa-sha2-nistp384,ecdsa-sha2-nistp521,rsa-sha2-512,rsa-sha2-256"

        private const val OPENSSH_PUBKEY_ORDER =
            "ssh-ed25519,ecdsa-sha2-nistp256,ecdsa-sha2-nistp384,ecdsa-sha2-nistp521,rsa-sha2-512,rsa-sha2-256"

        @Volatile private var cryptoReady = false

        fun ensureCryptoProviders() {
            if (cryptoReady) return
            synchronized(this) {
                if (cryptoReady) return
                // Android ART is not Java 15+; without BC, JSch strips ssh-ed25519
                if (Security.getProvider(BouncyCastleProvider.PROVIDER_NAME) == null) {
                    Security.addProvider(BouncyCastleProvider())
                }
                cryptoReady = true
            }
        }
    }
}
