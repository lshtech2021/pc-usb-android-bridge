package com.example.usbbridge

import android.util.Base64
import com.jcraft.jsch.HostKey
import com.jcraft.jsch.HostKeyRepository
import com.jcraft.jsch.UserInfo
import java.io.File
import java.security.MessageDigest

/**
 * TOFU（Trust On First Use）已知主机库。
 *
 * 存储格式：每行 "host<TAB>SHA256:xxx"，指纹与 OpenSSH 一致（`ssh-keygen -lf` 可交叉核对）。
 *
 * @param approvedFingerprint PC 端已人工确认的指纹；仅当它等于实际指纹时才写入并放行。
 */
class TofuHostKeys(
    private val store: File,
    private val approvedFingerprint: String? = null
) : HostKeyRepository {

    private val known = HashMap<String, String>()

    @Volatile var lastHost: String? = null; private set
    @Volatile var lastFingerprint: String? = null; private set
    @Volatile var lastResult: Int = OK; private set

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
            recorded == null && approvedFingerprint == fp -> { trust(host, fp); OK }
            recorded == null -> NOT_INCLUDED                    // 未知主机：交 PC 端确认
            recorded == fp -> OK
            else -> CHANGED                                     // 指纹变更：拒绝
        }
        return lastResult
    }

    fun trust(host: String, fingerprint: String) {
        known[host] = fingerprint
        persist()
    }

    /** HostKey 的 key 字段是 protected，无法从外部读取，故复用 check() 时缓存的指纹。 */
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
