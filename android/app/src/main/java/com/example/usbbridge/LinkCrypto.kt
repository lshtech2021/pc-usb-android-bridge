package com.example.usbbridge

import java.security.KeyFactory
import java.security.KeyPair
import java.security.KeyPairGenerator
import java.security.MessageDigest
import java.security.PrivateKey
import java.security.PublicKey
import java.security.spec.ECGenParameterSpec
import java.security.spec.PKCS8EncodedKeySpec
import java.security.spec.X509EncodedKeySpec
import javax.crypto.Cipher
import javax.crypto.KeyAgreement
import javax.crypto.Mac
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

/** P-256 ECDH + HKDF-SHA256 + AES-GCM; mirrors pc/link_crypto.py. */
object LinkCrypto {
    private val MAGIC = byteArrayOf(0xAB.toByte(), 0xCD.toByte())
    private val INFO = "usb-bridge-link-v1".toByteArray(Charsets.UTF_8)

    fun generateKeyPair(): KeyPair {
        val kpg = KeyPairGenerator.getInstance("EC")
        kpg.initialize(ECGenParameterSpec("secp256r1"))
        return kpg.generateKeyPair()
    }

    fun publicDer(pub: PublicKey): ByteArray = pub.encoded

    fun loadPublic(der: ByteArray): PublicKey =
        KeyFactory.getInstance("EC").generatePublic(X509EncodedKeySpec(der))

    fun loadPrivate(der: ByteArray): PrivateKey =
        KeyFactory.getInstance("EC").generatePrivate(PKCS8EncodedKeySpec(der))

    fun pcIdOf(pubkeyDer: ByteArray): String =
        MessageDigest.getInstance("SHA-256").digest(pubkeyDer)
            .joinToString("") { "%02x".format(it) }

    fun shortId(pcId: String): String =
        if (pcId.length >= 16) pcId.substring(0, 16) else pcId

    fun ecdh(priv: PrivateKey, peerPubDer: ByteArray): ByteArray {
        val ka = KeyAgreement.getInstance("ECDH")
        ka.init(priv)
        ka.doPhase(loadPublic(peerPubDer), true)
        val shared = ka.generateSecret()
        // Left-pad / trim to 32 bytes so Python cryptography and Android match
        return when {
            shared.size >= 32 -> shared.copyOfRange(shared.size - 32, shared.size)
            else -> ByteArray(32 - shared.size) + shared
        }
    }

    fun deriveSessionKey(shared: ByteArray, token: String, pcId: String): ByteArray {
        val salt = if (pcId.length == 64) {
            pcId.chunked(2).map { it.toInt(16).toByte() }.toByteArray()
        } else {
            pcId.toByteArray(Charsets.UTF_8)
        }
        val ikm = shared + token.toByteArray(Charsets.UTF_8)
        return hkdfSha256(ikm, salt, INFO, 32)
    }

    /** HKDF-Extract + Expand (RFC 5869) with SHA-256. */
    private fun hkdfSha256(ikm: ByteArray, salt: ByteArray, info: ByteArray, length: Int): ByteArray {
        val mac = Mac.getInstance("HmacSHA256")
        val actualSalt = if (salt.isEmpty()) ByteArray(32) else salt
        mac.init(SecretKeySpec(actualSalt, "HmacSHA256"))
        val prk = mac.doFinal(ikm)
        val result = ByteArray(length)
        var t = ByteArray(0)
        var offset = 0
        var counter = 1
        while (offset < length) {
            mac.init(SecretKeySpec(prk, "HmacSHA256"))
            mac.update(t)
            mac.update(info)
            mac.update(counter.toByte())
            t = mac.doFinal()
            val copy = minOf(t.size, length - offset)
            System.arraycopy(t, 0, result, offset, copy)
            offset += copy
            counter++
        }
        return result
    }

    class Seal(key: ByteArray) {
        private val keySpec = SecretKeySpec(key, "AES")
        private var sendCtr = 0L
        private var recvCtr = 0L

        fun sealPayload(msgType: Int, headerBytes: ByteArray, payload: ByteArray): ByteArray {
            val inner = ByteArray(2 + headerBytes.size + 4 + payload.size)
            inner[0] = ((headerBytes.size shr 8) and 0xFF).toByte()
            inner[1] = (headerBytes.size and 0xFF).toByte()
            headerBytes.copyInto(inner, 2)
            val o = 2 + headerBytes.size
            inner[o] = ((payload.size shr 24) and 0xFF).toByte()
            inner[o + 1] = ((payload.size shr 16) and 0xFF).toByte()
            inner[o + 2] = ((payload.size shr 8) and 0xFF).toByte()
            inner[o + 3] = (payload.size and 0xFF).toByte()
            payload.copyInto(inner, o + 4)
            val nonce = counterNonce(sendCtr++)
            val aad = byteArrayOf(MAGIC[0], MAGIC[1], (msgType and 0xFF).toByte())
            val c = Cipher.getInstance("AES/GCM/NoPadding")
            c.init(Cipher.ENCRYPT_MODE, keySpec, GCMParameterSpec(128, nonce))
            c.updateAAD(aad)
            return c.doFinal(inner)
        }

        fun unsealPayload(msgType: Int, ciphertext: ByteArray): Pair<ByteArray, ByteArray> {
            val nonce = counterNonce(recvCtr++)
            val aad = byteArrayOf(MAGIC[0], MAGIC[1], (msgType and 0xFF).toByte())
            val c = Cipher.getInstance("AES/GCM/NoPadding")
            c.init(Cipher.DECRYPT_MODE, keySpec, GCMParameterSpec(128, nonce))
            c.updateAAD(aad)
            val inner = c.doFinal(ciphertext)
            require(inner.size >= 6) { "sealed frame too short" }
            val hlen = ((inner[0].toInt() and 0xFF) shl 8) or (inner[1].toInt() and 0xFF)
            require(inner.size >= 2 + hlen + 4) { "sealed frame truncated" }
            val headerBytes = inner.copyOfRange(2, 2 + hlen)
            val o = 2 + hlen
            val plen = ((inner[o].toInt() and 0xFF) shl 24) or
                    ((inner[o + 1].toInt() and 0xFF) shl 16) or
                    ((inner[o + 2].toInt() and 0xFF) shl 8) or
                    (inner[o + 3].toInt() and 0xFF)
            require(inner.size >= o + 4 + plen) { "sealed frame truncated payload" }
            val payload = inner.copyOfRange(o + 4, o + 4 + plen)
            return headerBytes to payload
        }

        private fun counterNonce(ctr: Long): ByteArray {
            val n = ByteArray(12)
            for (i in 0 until 8) {
                n[11 - i] = ((ctr shr (8 * i)) and 0xFF).toByte()
            }
            return n
        }
    }
}
