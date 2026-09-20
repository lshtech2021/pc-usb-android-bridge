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
 * JSch SSH 代理（功能3核心）：手机作为跳板去连远程服务器，输出经 REMOTE_OUTPUT 回传 PC。
 *
 * @param hostKeys TOFU 主机指纹库（决定是否放行本次连接）
 */
class RemoteSession(
    private val hostKeys: TofuHostKeys,
    private val onOutput: (stream: String, data: ByteArray) -> Unit,
    private val onClose: (code: Int, reason: String) -> Unit
) {
    private var session: Session? = null
    private var channel: Channel? = null
    private val stdin = PipedOutputStream()
    private val stdinPipe = PipedInputStream(stdin, 1 shl 20)   // 1MB，降低被背压阻塞的概率

    data class Auth(val password: String?, val privateKey: String?, val passphrase: String?)

    /** 交互式 shell：PC 的 REMOTE_DATA 写入 stdin，远端输出经 onOutput 回传 */
    fun openSsh(host: String, port: Int, user: String, auth: Auth) = connect(host, port, user, auth) {
        val ch = session!!.openChannel("shell") as ChannelShell
        ch.setPty(true)
        ch.setPtySize(120, 30, 0, 0)                          // 与 PC 终端宽度匹配，避免全屏程序换行错乱
        runCatching { ch.setEnv("TERM", "xterm-256color") }   // 远端未开 AcceptEnv 时会被忽略
        ch.setInputStream(stdinPipe)
        ch.setOutputStream(fwdStream("out"))
        ch.connect(10_000)
        channel = ch
        watchClose(ch) { onClose(0, "shell 已退出") }
    }

    /** 一次性命令执行（kind=exec）：支持 stdin，结束回传退出码 */
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
        if (!auth.privateKey.isNullOrBlank()) {               // 私钥内容只存在内存中
            jsch.addIdentity("bridge", auth.privateKey.toByteArray(Charsets.UTF_8), null,
                auth.passphrase?.takeIf { it.isNotEmpty() }?.toByteArray(Charsets.UTF_8))
        }
        val s = jsch.getSession(user, host, port)
        auth.password?.takeIf { it.isNotEmpty() }?.let { s.setPassword(it) }
        s.setConfig("StrictHostKeyChecking", "yes")           // 是否放行由 TofuHostKeys 决定
        s.setHostKeyRepository(hostKeys)
        s.connect(10_000)
        session = s
        block()
    }

    /** 在独立线程等待通道结束，避免占用调用线程（remoteOps 单线程队列） */
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
