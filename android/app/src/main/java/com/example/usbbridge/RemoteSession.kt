package com.example.usbbridge

import com.jcraft.jsch.Channel
import com.jcraft.jsch.ChannelExec
import com.jcraft.jsch.ChannelShell
import com.jcraft.jsch.JSch
import com.jcraft.jsch.Session
import com.jcraft.jsch.UIKeyboardInteractive
import com.jcraft.jsch.UserInfo
import java.io.OutputStream
import java.io.PipedInputStream
import java.io.PipedOutputStream

/**
 * SSH session via mwiede JSch (OpenSSH + PEM keys, ed25519, rsa-sha2).
 * Auth prompts (password / MFA keyboard-interactive) go through [prompt] on the phone.
 */
class RemoteSession(
    private val hostKeys: TofuHostKeys,
    private val onOutput: (stream: String, data: ByteArray) -> Unit,
    private val onClose: (code: Int, reason: String) -> Unit,
    private val prompt: AuthBridge? = null
) {
    private var session: Session? = null
    private var channel: Channel? = null
    private val stdin = PipedOutputStream()
    private val stdinPipe = PipedInputStream(stdin, 1 shl 20)

    data class Auth(
        val password: String?,
        val privateKey: String?,
        val passphrase: String?,
        val mfa: Boolean = false
    )

    /** Callbacks for interactive auth on the phone UI thread (via AuthPrompts). */
    interface AuthBridge {
        fun askPassword(message: String): String?
        fun askKeyboardInteractive(destination: String, name: String, instruction: String,
                                   prompt: Array<String>, echo: BooleanArray): Array<String>?
    }

    fun openSsh(host: String, port: Int, user: String, auth: Auth) = connect(host, port, user, auth) {
        val ch = session!!.openChannel("shell") as ChannelShell
        ch.setPty(true)
        ch.setPtySize(120, 30, 0, 0)
        runCatching { ch.setEnv("TERM", "xterm-256color") }
        ch.setInputStream(stdinPipe)
        ch.setOutputStream(fwdStream("out"))
        ch.connect(10_000)
        channel = ch
        watchClose(ch) { onClose(0, "shell exited") }
    }

    fun execSsh(host: String, port: Int, user: String, auth: Auth, command: String) =
        connect(host, port, user, auth) {
            val ch = session!!.openChannel("exec") as ChannelExec
            ch.setCommand(command)
            ch.setInputStream(stdinPipe)
            ch.setOutputStream(fwdStream("out"))
            ch.setErrStream(fwdStream("err"))
            ch.connect(10_000)
            channel = ch
            watchClose(ch) { onClose(ch.exitStatus, "exec finished") }
        }

    private fun connect(host: String, port: Int, user: String, auth: Auth, block: () -> Unit) {
        val jsch = JSch()
        if (!auth.privateKey.isNullOrBlank()) {
            jsch.addIdentity("bridge", auth.privateKey.toByteArray(Charsets.UTF_8), null,
                auth.passphrase?.takeIf { it.isNotEmpty() }?.toByteArray(Charsets.UTF_8))
        }
        val s = jsch.getSession(user, host, port)
        auth.password?.takeIf { it.isNotEmpty() }?.let { s.setPassword(it) }
        s.setConfig("StrictHostKeyChecking", "yes")
        s.setHostKeyRepository(hostKeys)
        if (auth.mfa || prompt != null) {
            s.setUserInfo(object : UserInfo, UIKeyboardInteractive {
                override fun getPassword(): String? =
                    auth.password ?: prompt?.askPassword("SSH password for $user@$host")
                override fun promptYesNo(message: String?) = false
                override fun getPassphrase(): String? = auth.passphrase
                override fun promptPassphrase(message: String?) = !auth.passphrase.isNullOrEmpty()
                override fun promptPassword(message: String?): Boolean {
                    if (!auth.password.isNullOrEmpty()) return true
                    val p = prompt?.askPassword(message ?: "Password") ?: return false
                    s.setPassword(p)
                    return true
                }
                override fun showMessage(message: String?) {}
                override fun promptKeyboardInteractive(
                    destination: String?, name: String?, instruction: String?,
                    promptArr: Array<String>?, echo: BooleanArray?
                ): Array<String>? {
                    if (promptArr == null || echo == null) return null
                    return prompt?.askKeyboardInteractive(
                        destination ?: "", name ?: "", instruction ?: "", promptArr, echo)
                }
            })
            // Prefer keyboard-interactive when MFA is expected
            if (auth.mfa) {
                s.setConfig("PreferredAuthentications", "keyboard-interactive,password,publickey")
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

    fun writeStdin(data: ByteArray) {
        stdin.write(data)
        stdin.flush()
    }

    fun close() {
        runCatching { stdin.close() }
        runCatching { channel?.disconnect() }
        runCatching { session?.disconnect() }
    }
}
