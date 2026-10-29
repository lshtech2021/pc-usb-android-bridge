package com.example.usbbridge

import com.jcraft.jsch.ChannelSftp
import org.json.JSONObject
import java.io.InputStream
import java.io.OutputStream
import java.security.MessageDigest
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Remote-file transfers over a phone-opened SFTP channel (the file explorer feature).
 *
 * Same discipline as [ProxySession]: the bridge read loop only ever enqueues (O(1), never
 * blocks), while dedicated executors perform the blocking SFTP I/O. Every send funnels
 * through [send], which serialises on the session output lock.
 */

/**
 * One PC -> server upload: the read loop enqueues chunks into a size-bounded queue; a
 * serial-executor task drains the queue to the [ChannelSftp.put] stream. The FS_END frame
 * becomes an in-queue marker so finalization can never race ahead of queued chunk writes.
 * The drain task only runs while there is work (it never blocks waiting for the next chunk),
 * so a slow upload cannot stall other FS operations on the same executor.
 */
class FsUpload(
    private val id: Int,
    private val sftp: ChannelSftp,
    private val path: String,                 // remote path, for rm on failure
    private val send: (type: Int, header: JSONObject, payload: ByteArray) -> Unit,
    private val post: (() -> Unit) -> Unit,   // SessionHandler remoteOps (serial)
    private val onFinished: (Int) -> Unit
) {
    companion object {
        const val QUEUE_LIMIT = 4 * 1024 * 1024        // 16 x 256 KiB chunks
    }

    private class End(val ok: Boolean, val sha256: String?)
    private object Closed

    private val queue = LinkedBlockingQueue<Any>()
    private var queuedBytes = 0
    private val ended = AtomicBoolean(false)           // no more chunks will be accepted
    private val finalized = AtomicBoolean(false)       // cleanup + ACK ran exactly once
    private val draining = AtomicBoolean(false)
    private val md = MessageDigest.getInstance("SHA-256")
    private var out: OutputStream? = sftp.put(path)    // OVERWRITE; the PC UI confirms collisions

    /** Read loop, O(1), never blocks. false = overflow; the caller tears the upload down. */
    fun offer(chunk: ByteArray): Boolean {
        if (ended.get()) return false
        synchronized(this) {
            if (queuedBytes + chunk.size > QUEUE_LIMIT) return false
            queuedBytes += chunk.size
        }
        queue.offer(chunk)
        startDraining()
        return true
    }

    /** Read loop. FS_END from the PC becomes an in-queue marker processed after all
     *  previously enqueued chunks; the drain owns finalization. */
    fun end(ok: Boolean, sha256: String?) {
        if (!ended.getAndSet(true)) {
            queue.offer(End(ok, sha256))
            startDraining()
        }
    }

    /** Any thread. Called directly from SessionHandler.close() (remoteOps is already shut
     *  down there) and via post from the overflow path. Idempotent. */
    fun teardown(reason: String) {
        if (!ended.getAndSet(true)) queue.offer(Closed)
        finalizeFail(reason)
    }

    /** Kick the drain task; also called from SessionHandler after registration. */
    fun startDraining() {
        if (draining.compareAndSet(false, true)) post { drain() }
    }

    private fun drain() {
        draining.set(false)                  // a concurrent offer() may re-post a follow-up
        try {
            while (true) {
                when (val item = queue.poll()) {
                    null -> return           // queue drained; the executor is free again
                    is ByteArray -> {
                        synchronized(this) { queuedBytes -= item.size }
                        out?.write(item)
                        md.update(item)
                    }
                    is End -> { finish(item.ok, item.sha256); return }
                    is Closed -> return     // teardown() already cleaned up
                }
            }
        } catch (_: InterruptedException) {
            Thread.currentThread().interrupt()   // shutdownNow during USB teardown
        } catch (e: Exception) {
            finalizeFail(e.message ?: "upload failed")
        }
    }

    private fun finish(ok: Boolean, sha256: String?) {
        runCatching { out?.close() }
        val actual = md.digest().joinToString("") { "%02x".format(it) }
        val good = ok && (sha256.isNullOrEmpty() || sha256 == actual)
        if (good) {
            if (finalized.compareAndSet(false, true)) {
                send(FrameIO.ACK, JSONObject().put("fs_id", id).put("ok", true).put("path", path),
                    ByteArray(0))
                onFinished(id)
            }
        } else {
            runCatching { sftp.rm(path) }
            finalizeFail(if (ok) "SHA mismatch" else "cancelled")
        }
    }

    private fun finalizeFail(reason: String) {
        if (!finalized.compareAndSet(false, true)) return
        runCatching { out?.close() }
        runCatching { sftp.rm(path) }
        send(FrameIO.ACK, JSONObject().put("fs_id", id).put("ok", false).put("error", reason),
            ByteArray(0))
        onFinished(id)
    }
}

/**
 * One server -> PC download: a dedicated pool thread (like sendFile) streams 256 KiB chunks
 * to the bridge and computes the SHA-256 the PC verifies. [abort] closes the SFTP stream,
 * which unblocks the reader.
 */
class FsDownload(
    private val id: Int,
    private val channel: Int,
    private val ins: InputStream,             // sftp.get(path) stream
    private val send: (type: Int, header: JSONObject, payload: ByteArray) -> Unit,
    private val onFinished: (Int) -> Unit
) {
    companion object {
        const val CHUNK = 256 * 1024
    }

    private val aborted = AtomicBoolean(false)
    private val md = MessageDigest.getInstance("SHA-256")

    /** Read loop, O(1). */
    fun abort() {
        if (aborted.compareAndSet(false, true)) runCatching { ins.close() }
    }

    fun run() {
        try {
            val buf = ByteArray(CHUNK)
            while (!aborted.get()) {
                val n = ins.read(buf)
                if (n <= 0) break
                md.update(buf, 0, n)
                send(FrameIO.FS_CHUNK, JSONObject().put("channel", channel).put("id", id),
                    buf.copyOf(n))
            }
            if (aborted.get()) {
                sendEnd(false, "aborted")
            } else {
                val hex = md.digest().joinToString("") { "%02x".format(it) }
                sendEnd(true, hex)
            }
        } catch (e: Exception) {
            // Includes a dead bridge (send throws): teardown aborts us anyway.
            runCatching { sendEnd(false, if (aborted.get()) "aborted" else (e.message ?: "download failed")) }
        } finally {
            runCatching { ins.close() }
            onFinished(id)
        }
    }

    private fun sendEnd(ok: Boolean, detail: String) {
        val h = JSONObject().put("channel", channel).put("id", id).put("ok", ok)
        if (ok) h.put("sha256", detail) else h.put("error", detail)
        send(FrameIO.FS_END, h, ByteArray(0))
    }
}
