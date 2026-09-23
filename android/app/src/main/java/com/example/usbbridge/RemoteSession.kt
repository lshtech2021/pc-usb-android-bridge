package com.example.usbbridge

import com.jcraft.jsch.Channel
import com.jcraft.jsch.ChannelExec
import com.jcraft.jsch.ChannelShell
import com.jcraft.jsch.JSch
import com.jcraft.jsch.Session
import com.jcraft.jsch.UIKeyboardInteractive
import com.jcraft.jsch.UserInfo
import org.bouncycastle.jce.provider.BouncyCastleProvider
import java.io.OutputStream
import java.io.PipedInputStream
import java.io.PipedOutputStream
import java.security.Security

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
        ensureCryptoProviders()
        val jsch = JSch()
        if (!auth.privateKey.isNullOrBlank()) {
            jsch.addIdentity("bridge", auth.privateKey.toByteArray(Charsets.UTF_8), null,
                auth.passphrase?.takeIf { it.isNotEmpty() }?.toByteArray(Charsets.UTF_8))
        }
        val s = jsch.getSession(user, host, port)
        auth.password?.takeIf { it.isNotEmpty() }?.let { s.setPassword(it) }
        s.setConfig("StrictHostKeyChecking", "yes")
        // Match modern OpenSSH preference: ed25519 host key first (same SHA256 as `ssh -vvv`)
        s.setConfig("server_host_key", OPENSSH_HOST_KEY_ORDER)
        s.setConfig("PubkeyAcceptedAlgorithms", OPENSSH_PUBKEY_ORDER)
        s.setHostKeyRepository(hostKeys)

        var effectivePassphrase = auth.passphrase
        if (auth.mfa || prompt != null || !auth.privateKey.isNullOrBlank()) {
            s.setUserInfo(object : UserInfo, UIKeyboardInteractive {
                override fun getPassword(): String? =
                    auth.password ?: prompt?.askPassword("SSH password for $user@$host")
                override fun promptYesNo(message: String?) = false
                override fun getPassphrase(): String? = effectivePassphrase
                override fun promptPassphrase(message: String?): Boolean {
                    if (!effectivePassphrase.isNullOrEmpty()) return true
                    val p = prompt?.askPassword(message ?: "Private key passphrase") ?: return false
                    effectivePassphrase = p
                    return true
                }
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

    fun writeStdin(data: ByteArray) {
        stdin.write(data)
        stdin.flush()
    }

    fun close() {
        runCatching { stdin.close() }
        runCatching { channel?.disconnect() }
        runCatching { session?.disconnect() }
    }

    companion object {
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
