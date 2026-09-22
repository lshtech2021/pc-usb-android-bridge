package com.example.usbbridge

import com.jcraft.jsch.Channel
import com.jcraft.jsch.ChannelExec
import com.jcraft.jsch.ChannelShell
import com.jcraft.jsch.JSch
import com.jcraft.jsch.Session
import java.io.OutputStream
import java.io.PipedInputStream
import java.io.PipedOutputStream

/**
 * JSch SSH proxy (core of feature 3): the phone acts as a jump host to reach a remote server, and output is sent back to the PC via REMOTE_OUTPUT.
 *
 * @param hostKeys TOFU host fingerprint store (decides whether to allow this connection)
 */
class RemoteSession(
    private val hostKeys: TofuHostKeys,
    private val onOutput: (stream: String, data: ByteArray) -> Unit,
    private val onClose: (code: Int, reason: String) -> Unit
) {
    private var session: Session? = null
    private var channel: Channel? = null
    private val stdin = PipedOutputStream()
    private val stdinPipe = PipedInputStream(stdin, 1 shl 20)   // 1MB, reduces the chance of blocking on backpressure

    data class Auth(val password: String?, val privateKey: String?, val passphrase: String?)

    /** Interactive shell: PC's REMOTE_DATA is written to stdin, remote output is sent back via onOutput */
    fun openSsh(host: String, port: Int, user: String, auth: Auth) = connect(host, port, user, auth) {
        val ch = session!!.openChannel("shell") as ChannelShell
        ch.setPty(true)
        ch.setPtySize(120, 30, 0, 0)                          // Match the PC terminal width to avoid garbled line wrapping in full-screen programs
        runCatching { ch.setEnv("TERM", "xterm-256color") }   // Ignored when AcceptEnv is not enabled on the remote
        ch.setInputStream(stdinPipe)
        ch.setOutputStream(fwdStream("out"))
        ch.connect(10_000)
        channel = ch
        watchClose(ch) { onClose(0, "shell 已退出") }
    }

    /** One-shot command execution (kind=exec): supports stdin, returns the exit code on completion */
    fun execSsh(host: String, port: Int, user: String, auth: Auth, command: String) =
        connect(host, port, user, auth) {
            val ch = session!!.openChannel("exec") as ChannelExec
            ch.setCommand(command)
            ch.setInputStream(stdinPipe)
            ch.setOutputStream(fwdStream("out"))
            ch.setErrStream(fwdStream("err"))
            ch.connect(10_000)
            channel = ch
            watchClose(ch) { onClose(ch.exitStatus, "exec 完成") }
        }

    private fun connect(host: String, port: Int, user: String, auth: Auth, block: () -> Unit) {
        val jsch = JSch()
        if (!auth.privateKey.isNullOrBlank()) {               // Private-key contents exist only in memory
            jsch.addIdentity("bridge", auth.privateKey.toByteArray(Charsets.UTF_8), null,
                auth.passphrase?.takeIf { it.isNotEmpty() }?.toByteArray(Charsets.UTF_8))
        }
        val s = jsch.getSession(user, host, port)
        auth.password?.takeIf { it.isNotEmpty() }?.let { s.setPassword(it) }
        s.setConfig("StrictHostKeyChecking", "yes")           // Whether to allow is decided by TofuHostKeys
        s.setHostKeyRepository(hostKeys)
        s.connect(10_000)
        session = s
        block()
    }

    /** Wait for the channel to end on a dedicated thread to avoid occupying the calling thread (remoteOps single-threaded queue) */
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
