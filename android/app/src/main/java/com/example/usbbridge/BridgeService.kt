package com.example.usbbridge

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.os.Build
import androidx.core.app.ServiceCompat
import com.jcraft.jsch.HostKeyRepository
import com.jcraft.jsch.JSchException
import org.json.JSONObject
import java.io.Closeable
import java.io.File
import java.io.OutputStream
import java.net.InetAddress
import java.net.ServerSocket
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.Executors

/**
 * 前台服务：在 127.0.0.1:PORT 上监听（由 PC 侧 adb forward 回连），每连接一个 SessionHandler。
 */
class BridgeService : Service() {
    override fun onBind(intent: Intent?) = null

    override fun onCreate() {
        super.onCreate()
        if (token.isEmpty()) token = randomToken()
        if (Build.VERSION.SDK_INT >= 26) {
            getSystemService(NotificationManager::class.java).createNotificationChannel(
                NotificationChannel(CHANNEL_ID, "USB Bridge", NotificationManager.IMPORTANCE_LOW))
        }
        val n = buildNotification(this, "USB Bridge 运行中 (127.0.0.1:$PORT)", "Token: $token")
        if (Build.VERSION.SDK_INT >= 29) {
            ServiceCompat.startForeground(this, 1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        } else {
            startForeground(1, n)
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (running) return START_STICKY          // 防重入：重复点按钮/系统重启不再起第二个监听
        running = true
        Thread {
            try {
                // 只绑回环：设备侧 adb forward 通过 127.0.0.1 回连；同时避免其他 App 直接接入
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
        const val CHANNEL_ID = "usb_bridge"

        @Volatile var token: String = ""
            private set

        @Volatile var running: Boolean = false
            private set

        private var server: ServerSocket? = null
        private val pool = Executors.newCachedThreadPool()
        private val sessions = CopyOnWriteArrayList<SessionHandler>()

        fun randomToken(): String =
            ByteArray(4).also { SecureRandom().nextBytes(it) }
                .joinToString("") { "%02x".format(it) }

        /** 供 SessionHandler 复用线程池（手机端发送文件等耗时操作不能占用服务监听线程） */
        fun poolExecute(task: Runnable) {
            pool.execute(task)
        }

        fun addSession(s: SessionHandler) { sessions.add(s) }
        fun removeSession(s: SessionHandler) { sessions.remove(s) }

        /** 手机端发起文本/文件：复用当前已建立的连接（PC 侧已具备接收逻辑） */
        fun broadcastText(text: String) = sessions.forEach { it.sendText(text) }

        fun broadcastFile(file: File, cleanup: Boolean = false) =
            sessions.forEach { it.sendFile(file, cleanup) }

        fun buildNotification(ctx: Context, title: String, text: String): Notification =
            if (Build.VERSION.SDK_INT >= 26)
                Notification.Builder(ctx, CHANNEL_ID)
                    .setContentTitle(title).setContentText(text)
                    .setSmallIcon(android.R.drawable.ic_menu_info_details).build()
            else @Suppress("DEPRECATION")
                Notification.Builder(ctx)
                    .setContentTitle(title).setContentText(text)
                    .setSmallIcon(android.R.drawable.ic_menu_info_details).build()
    }
}

/** 单条 PC 连接：解析帧、落盘文件、执行远程会话。 */
class SessionHandler(private val ctx: Context, private val sock: java.net.Socket) : Runnable, Closeable {
    private val out: OutputStream = sock.getOutputStream()
    private val outLock = Any()
    private val remotes = ConcurrentHashMap<Int, RemoteSession>()
    private val sinks = ConcurrentHashMap<Int, FileSink>()

    /** 远程通道操作串行执行：保证 OPEN→DATA 顺序，同时不阻塞主读循环（TEXT/FILE/PING 不受 SSH 握手影响） */
    private val remoteOps = Executors.newSingleThreadExecutor()

    @Volatile private var closed = false
    @Volatile private var authed = false

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

    private fun handle(f: FrameIO.Frame) {
        with(f.header) {
            // 首个帧必须是带正确 token 的 HELLO，否则拒绝
            if (!authed) {
                if (f.type == FrameIO.HELLO && optString("token") == BridgeService.token) {
                    authed = true
                    send(FrameIO.ACK, JSONObject().put("ok", true).put("device", Build.MODEL))
                } else {
                    send(FrameIO.ERROR, JSONObject().put("code", "BAD_TOKEN").put("message", "token 校验失败"))
                    close()
                }
                return
            }
            when (f.type) {
                FrameIO.PING -> send(FrameIO.PONG, JSONObject())

                FrameIO.TEXT -> {                                      // PC → 手机 文本
                    notifyText(ctx, optString("text"))
                    send(FrameIO.ACK, JSONObject().put("id", optInt("id")))
                }

                FrameIO.FILE_META -> {
                    val id = optInt("id")
                    try {
                        sinks[id] = FileSink.create(ctx, f.header)
                    } catch (e: Exception) {                           // 磁盘满 / 无权限等
                        send(FrameIO.ERROR, JSONObject().put("code", "FILE_WRITE_FAILED")
                            .put("id", id).put("message", e.message ?: ""))
                    }
                }

                FrameIO.FILE_CHUNK -> sinks[optInt("id")]?.append(f.payload)

                FrameIO.FILE_END -> {
                    val id = optInt("id")
                    val path = sinks.remove(id)?.finish(optBoolean("ok"), optString("sha256"))
                    send(FrameIO.ACK, JSONObject()
                        .put("file_id", id).put("ok", path != null).put("path", path ?: ""))
                }

                FrameIO.REMOTE_OPEN -> {
                    val fr = f; post { openRemote(fr) }
                }

                FrameIO.REMOTE_DATA -> {
                    val d = f.payload; val c = optInt("channel")
                    post { runCatching { remotes[c]?.writeStdin(d) } }
                }

                FrameIO.REMOTE_CLOSE -> {
                    val c = optInt("channel")
                    post { runCatching { remotes.remove(c)?.close() } }
                }
                else -> {}
            }
        }
    }

    private fun post(task: () -> Unit) {
        runCatching { remoteOps.execute(task) }        // close() 后队列已关闭，忽略新任务
    }

    // ---------- 功能1/2：手机 → PC 文本与文件 ----------
    fun sendText(text: String) =
        send(FrameIO.TEXT, JSONObject().put("id", FrameIO.nextId()).put("text", text))

    fun sendFile(file: File, cleanup: Boolean = false) {
        BridgeService.poolExecute {
            val id = FrameIO.nextId()
            runCatching {
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
            if (cleanup) runCatching { file.delete() }          // 发送的是缓存副本，发完清理
        }
    }

    // ---------- 功能3：远程会话 ----------
    private fun openRemote(f: FrameIO.Frame) = with(f.header) {
        val ch = optInt("channel")
        val host = optString("host")
        val trust = if (has("trust_fingerprint") && !isNull("trust_fingerprint"))
            optString("trust_fingerprint") else null
        val auth = RemoteSession.Auth(
            password = if (has("password")) optString("password") else null,
            privateKey = if (has("private_key")) optString("private_key") else null,
            passphrase = if (has("passphrase")) optString("passphrase") else null)
        val hostKeys = TofuHostKeys(File(ctx.filesDir, "known_hosts"), trust)
        val rs = RemoteSession(hostKeys,
            { stream, data ->
                send(FrameIO.REMOTE_OUTPUT,
                    JSONObject().put("channel", ch).put("stream", stream), data)
            },
            { code, reason ->
                send(FrameIO.REMOTE_CLOSE, JSONObject().put("channel", ch)
                    .put("code", code).put("reason", reason))
                remotes.remove(ch)
            })
        try {
            if (optString("kind") == "exec")
                rs.execSsh(host, optInt("port", 22), optString("user"), auth, optString("command"))
            else
                rs.openSsh(host, optInt("port", 22), optString("user"), auth)
            remotes[ch] = rs
        } catch (e: Exception) {
            rs.close()
            remotes.remove(ch)
            val code = when {
                hostKeys.lastResult == HostKeyRepository.CHANGED -> "HOST_KEY_CHANGED"
                hostKeys.lastResult == HostKeyRepository.NOT_INCLUDED -> "UNKNOWN_HOST"
                e is JSchException && e.message.orEmpty().contains("Auth fail", true) -> "AUTH_FAILED"
                e.message.orEmpty().let { m ->
                    m.contains("Connection refused", true) || m.contains("UnknownHost", true) ||
                            m.contains("timeout", true) || m.contains("Network is unreachable", true)
                } -> "HOST_UNREACHABLE"
                else -> "CHANNEL_OPEN_FAILED"
            }
            send(FrameIO.ERROR, JSONObject().put("channel", ch).put("code", code)
                .put("message", e.message ?: "").put("host", host)
                .put("fingerprint", hostKeys.lastFingerprint ?: ""))
        }
    }

    private fun notifyText(ctx: Context, text: String) {
        if (Build.VERSION.SDK_INT >= 33 &&
            ctx.checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED) return       // 未授权则静默跳过，界面日志仍有记录
        val nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        nm.notify((System.currentTimeMillis() and 0xFFFF).toInt(),
            BridgeService.buildNotification(ctx, "来自 PC 的消息", text))
    }

    override fun close() {
        if (closed) return
        closed = true
        remoteOps.shutdownNow()
        remotes.values.forEach { it.close() }
        sinks.values.forEach { it.abort() }
        BridgeService.removeSession(this)
        runCatching { sock.close() }
    }
}

/** 文件落盘（App 外部私有目录，免存储权限）；带 SHA256 校验与 name(1).ext 重名规则 */
class FileSink private constructor(private val file: File) {
    private val fos = file.outputStream()
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
            file.delete(); null
        } else file.absolutePath
    }

    fun abort() {
        runCatching { fos.close() }
        runCatching { file.delete() }
    }

    companion object {
        fun createDir(ctx: Context): File = ctx.getExternalFilesDir(null) ?: ctx.filesDir

        /** 与 PC 端一致：a.txt → a(1).txt */
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

        fun create(ctx: Context, h: JSONObject): FileSink =
            FileSink(uniqueName(createDir(ctx), h.optString("name", "file_${h.optInt("id")}")))
    }
}
