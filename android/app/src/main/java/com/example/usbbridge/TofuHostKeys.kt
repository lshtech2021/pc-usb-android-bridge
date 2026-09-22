package com.example.usbbridge

import android.util.Base64
import com.jcraft.jsch.HostKey
import com.jcraft.jsch.HostKeyRepository
import com.jcraft.jsch.UserInfo
import java.io.File
import java.security.MessageDigest

/**
 * TOFU (Trust On First Use) known-hosts store.
 *
 * Storage format: one "host<TAB>SHA256:xxx" per line; fingerprints match OpenSSH (cross-check with `ssh-keygen -lf`).
 *
 * @param approvedFingerprint fingerprint manually confirmed on the PC side; only written and allowed through when it equals the actual fingerprint.
 */
class TofuHostKeys(
    private val store: File,
    private val approvedFingerprint: String? = null
) : HostKeyRepository {

    private val known = HashMap<String, String>()

    @Volatile var lastHost: String? = null; private set
    @Volatile var lastFingerprint: String? = null; private set
    @Volatile var lastResult: Int = HostKeyRepository.OK; private set

    init {
        if (store.exists()) {
            store.readLines().forEach { line ->
                val parts = line.split('\t')
                if (parts.size == 2 && parts[1].isNotBlank()) known[parts[0]] = parts[1]
            }
        }
    }

    override fun check(host: String, key: ByteArray): Int {
        val fp = fingerprint(key)
        lastHost = host; lastFingerprint = fp
        val recorded = known[host]
        lastResult = when {
            recorded == null && approvedFingerprint == fp -> { trust(host, fp); HostKeyRepository.OK }
            recorded == null -> HostKeyRepository.NOT_INCLUDED                    // Unknown host: leave to PC side for confirmation
            recorded == fp -> HostKeyRepository.OK
            else -> HostKeyRepository.CHANGED                                     // Fingerprint changed: reject
        }
        return lastResult
    }

    fun trust(host: String, fingerprint: String) {
        known[host] = fingerprint
        persist()
    }

    /** HostKey's key field is protected and unreadable from outside, so reuse the fingerprint cached during check(). */
    override fun add(hostkey: HostKey, ui: UserInfo?) {
        val h = lastHost ?: return
        val fp = lastFingerprint ?: return
        trust(h, fp)
    }

    override fun remove(host: String, type: String?) {
        known.remove(host); persist()
    }

    override fun remove(host: String, type: String?, key: ByteArray?) {
        known.remove(host); persist()
    }

    override fun getKnownHostsRepositoryID(): String = store.absolutePath
    override fun getHostKey(): Array<HostKey> = emptyArray()
    override fun getHostKey(host: String?, type: String?): Array<HostKey> = emptyArray()

    private fun persist() {
        runCatching {
            store.writeText(known.entries.joinToString("\n") { "${it.key}\t${it.value}" })
        }
    }

    companion object {
        fun fingerprint(key: ByteArray): String = "SHA256:" + Base64.encodeToString(
            MessageDigest.getInstance("SHA-256").digest(key),
            Base64.NO_WRAP or Base64.NO_PADDING)
    }
}
