package com.example.usbbridge

import org.json.JSONObject
import java.io.IOException
import java.io.InputStream
import java.util.concurrent.atomic.AtomicInteger

/**
 * Protocol mirror implementation; field order must match PC-side protocol.py exactly:
 *   magic(2) | type(1) | headerLen(2) | header(JSON,UTF-8) | payloadLen(4) | payload
 * Total length = 9 + headerLen + payloadLen
 */
object FrameIO {
    const val HELLO = 0x00; const val TEXT = 0x01
    const val FILE_META = 0x02; const val FILE_CHUNK = 0x03; const val FILE_END = 0x04
    const val REMOTE_OPEN = 0x10; const val REMOTE_DATA = 0x11
    const val REMOTE_OUTPUT = 0x12; const val REMOTE_CLOSE = 0x13
    const val ACK = 0x20; const val ERROR = 0x21; const val PING = 0x30; const val PONG = 0x31

    /** Phone-side id space starts at 0x40000000 to avoid colliding with the PC side (starting at 1) */
    private val idSeq = AtomicInteger(0x40000000)
    fun nextId(): Int = idSeq.getAndIncrement()

    data class Frame(val type: Int, val header: JSONObject, val payload: ByteArray)

    fun encode(type: Int, header: JSONObject, payload: ByteArray = ByteArray(0)): ByteArray {
        val h = header.toString().toByteArray(Charsets.UTF_8)
        return ByteArray(9 + h.size + payload.size).apply {
            var i = 0
            fun u1(v: Int) { this[i++] = v.toByte() }
            fun u2(v: Int) { u1(v shr 8); u1(v) }
            fun u4(v: Int) { u2(v shr 16); u2(v) }
            u1(0xAB); u1(0xCD); u1(type); u2(h.size)
            h.copyInto(this, i); i += h.size          // Must write at cursor i, not a hardcoded offset
            u4(payload.size); payload.copyInto(this, i)
        }
    }

    fun readFrame(input: InputStream): Frame? {
        val magic = ByteArray(2)
        if (!fill(input, magic)) return null                       // Peer closed cleanly
        if (magic[0] != 0xAB.toByte() || magic[1] != 0xCD.toByte()) throw IOException("bad magic")
        val type = readByte(input) ?: return null
        val hLen = readShort(input)
        val header = if (hLen > 0) JSONObject(String(readN(input, hLen), Charsets.UTF_8)) else JSONObject()
        val pLen = readInt(input)
        return Frame(type, header, if (pLen > 0) readN(input, pLen) else ByteArray(0))
    }

    private fun fill(input: InputStream, buf: ByteArray): Boolean {
        var off = 0
        while (off < buf.size) {
            val n = input.read(buf, off, buf.size - off)
            if (n < 0) return false
            off += n
        }
        return true
    }

    private fun readByte(s: InputStream): Int? {
        val b = ByteArray(1)
        return if (fill(s, b)) b[0].toInt() and 0xFF else null
    }

    private fun readShort(s: InputStream): Int {
        val b = ByteArray(2); require(fill(s, b))
        return ((b[0].toInt() and 0xFF) shl 8) or (b[1].toInt() and 0xFF)
    }

    private fun readInt(s: InputStream): Int {
        val b = ByteArray(4); require(fill(s, b))
        return ((b[0].toInt() and 0xFF) shl 24) or ((b[1].toInt() and 0xFF) shl 16) or
                ((b[2].toInt() and 0xFF) shl 8) or (b[3].toInt() and 0xFF)
    }

    private fun readN(s: InputStream, n: Int): ByteArray {
        val b = ByteArray(n); require(fill(s, b)); return b
    }
}
