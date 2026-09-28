package com.example.usbbridge

import org.json.JSONObject
import java.io.IOException
import java.net.ConnectException
import java.net.InetSocketAddress
import java.net.NoRouteToHostException
import java.net.Socket
import java.net.SocketTimeoutException
import java.net.UnknownHostException
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.atomic.AtomicBoolean

/**
 * One reverse-tether stream (SOCKS5): the phone opens the outbound socket on the PC's
 * behalf from this app, and relays bytes over the sealed bridge.
 *
 * Two daemon threads per stream keep the read loop free: a reader (socket -> bridge) and
 * a writer (bridge -> socket). [enqueue] only appends to a size-bounded queue, so it is O(1)
 * and safe to call from the bridge read loop. Every send funnels through [send], which
 * serialises on the session output lock (LinkCrypto.Seal counters are not thread-safe).
 */
class ProxySession(
    private val sid: Int,
    private val send: (type: Int, header: JSONObject, payload: ByteArray) -> Unit,
    private val onFinished: (Int) -> Unit
) {
    companion object {
        const val CONNECT_TIMEOUT_MS = 5000
        const val CHUNK = 64 * 1024
        const val QUEUE_LIMIT = 1024 * 1024
        private val EOF = Any()          // remote write side finished -> shutdownOutput()
        private val CLOSED = Any()       // tear the writer down
    }

    private val queue = LinkedBlockingQueue<Any>()
    private val finished = AtomicBoolean(false)
    @Volatile private var socket: Socket? = null
    private var reader: Thread? = null
    private var writer: Thread? = null
    private var queuedBytes = 0

    fun start(host: String, port: Int) {
        reader = Thread({ readLoop(host, port) }, "proxy-read-$sid")
            .apply { isDaemon = true; start() }
    }

    private fun readLoop(host: String, port: Int) {
        val sock = Socket()
        try {
            sock.tcpNoDelay = true
            sock.connect(InetSocketAddress(host, port), CONNECT_TIMEOUT_MS)
            socket = sock
            if (finished.get()) {                    // closed while the connect was in flight
                runCatching { sock.close() }
                return
            }
            send(FrameIO.PROXY_OPENED, JSONObject().put("sid", sid).put("ok", true),
                ByteArray(0))
            writer = Thread({ writeLoop(sock) }, "proxy-write-$sid")
                .apply { isDaemon = true; start() }

            val buf = ByteArray(CHUNK)
            val ins = sock.getInputStream()
            while (true) {
                val n = ins.read(buf)
                if (n <= 0) break
                send(FrameIO.PROXY_DATA, JSONObject().put("sid", sid), buf.copyOf(n))
            }
            send(FrameIO.PROXY_CLOSE, JSONObject().put("sid", sid).put("dir", "s2c")
                .put("code", 0).put("reason", "remote EOF"), ByteArray(0))
        } catch (e: Exception) {
            if (socket == null) {
                // Connect never completed: report the failure on the open, not as a close.
                send(FrameIO.PROXY_OPENED, JSONObject().put("sid", sid).put("ok", false)
                    .put("code", errorCode(e)).put("message", e.message ?: errorCode(e)),
                    ByteArray(0))
            } else if (!finished.get()) {
                send(FrameIO.PROXY_CLOSE, JSONObject().put("sid", sid).put("dir", "both")
                    .put("code", 0).put("reason", e.message ?: "socket error"), ByteArray(0))
            }
        } finally {
            finish()
        }
    }

    private fun writeLoop(sock: Socket) {
        val out = sock.getOutputStream()
        try {
            while (true) {
                val item = queue.take()
                if (item === CLOSED) break
                if (item === EOF) {
                    runCatching { out.flush() }
                    runCatching { sock.shutdownOutput() }
                    continue
                }
                val data = item as ByteArray
                synchronized(this) { queuedBytes -= data.size }
                out.write(data)
                out.flush()
            }
        } catch (e: InterruptedException) {
            Thread.currentThread().interrupt()
        } catch (e: IOException) {
            // Peer closed the socket; the reader will observe it too.
        } finally {
            runCatching { sock.close() }
        }
    }

    /** Bridge read loop -> socket. O(1), never blocks; false means the queue is full. */
    fun enqueue(data: ByteArray): Boolean {
        if (finished.get()) return false
        synchronized(this) {
            if (queuedBytes + data.size > QUEUE_LIMIT) return false
            queuedBytes += data.size
        }
        return queue.offer(data)
    }

    /** PC finished the client->server direction: flush queued bytes, then half-close. */
    fun halfCloseRemoteWrite() {
        queue.offer(EOF)
    }

    /** Full teardown: drop the socket, stop both threads, and unregister. */
    fun close() {
        runCatching { socket?.close() }
        reader?.interrupt()
        writer?.interrupt()
        queue.offer(CLOSED)
        finish()
    }

    private fun finish() {
        if (finished.compareAndSet(false, true)) onFinished(sid)
    }

    private fun errorCode(e: Exception): String = when (e) {
        is ConnectException -> "PROXY_REFUSED"
        is UnknownHostException, is NoRouteToHostException -> "PROXY_HOST_UNREACHABLE"
        is SocketTimeoutException -> "PROXY_TIMEOUT"
        else -> "PROXY_REFUSED"
    }
}
