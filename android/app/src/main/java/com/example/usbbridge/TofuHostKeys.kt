package com.example.usbbridge

import android.content.Context
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
 * @param approvedFingerprint fingerprint manually confirmed; only written and allowed through when it equals the actual fingerprint.
 */
class TofuHostKeys(
    private val store: File,
    private val approvedFingerprint: String? = null
) : HostKeyRepository {

    private val known = HashMap<String, String>()

    @Volatile var lastHost: String? = null; private set
    @Volatile var lastFingerprint: String? = null; private set
    /** Previous stored fingerprint when [lastResult] is CHANGED. */
    @Volatile var lastRecordedFingerprint: String? = null; private set
    @Volatile var lastResult: Int = HostKeyRepository.OK; private set

    init {
        known.putAll(loadMap(store))
    }

    override fun check(host: String, key: ByteArray): Int {
        val fp = fingerprint(key)
        lastHost = host
        lastFingerprint = fp
        val recorded = known[host]
        lastRecordedFingerprint = recorded
        lastResult = when {
            recorded == null && approvedFingerprint == fp -> {
                trust(host, fp); HostKeyRepository.OK
            }
            recorded == null -> HostKeyRepository.NOT_INCLUDED
            recorded == fp -> HostKeyRepository.OK
            else -> HostKeyRepository.CHANGED
        }
        return lastResult
    }

    fun trust(host: String, fingerprint: String) {
        known[host] = fingerprint
        persist()
    }

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

    private fun persist() = saveMap(store, known)

    companion object {
        fun storeFile(ctx: Context): File = File(ctx.filesDir, "known_hosts")

        fun loadMap(store: File): MutableMap<String, String> {
            val map = LinkedHashMap<String, String>()
            if (!store.exists()) return map
            store.readLines().forEach { line ->
                val parts = line.split('\t')
                if (parts.size == 2 && parts[1].isNotBlank()) map[parts[0]] = parts[1]
            }
            return map
        }

        fun saveMap(store: File, map: Map<String, String>) {
            runCatching {
                if (map.isEmpty()) {
                    if (store.exists()) store.delete()
                } else {
                    store.writeText(map.entries.joinToString("\n") { "${it.key}\t${it.value}" })
                }
            }
        }

        /** Host name forms JSch / OpenSSH may use in known_hosts. */
        fun hostAliases(host: String, port: Int): Set<String> {
            val h = host.trim().removePrefix("[").substringBefore(']').substringBefore(':').trim()
            if (h.isEmpty()) return emptySet()
            return setOf(
                h,
                "$h:$port",
                "[$h]:$port",
                host.trim()
            )
        }

        /** Remove all known_hosts entries for this host (any common alias). Returns count removed. */
        fun forgetHost(store: File, host: String, port: Int = 22): Int {
            val map = loadMap(store)
            val aliases = hostAliases(host, port)
            val bare = host.trim().removePrefix("[").substringBefore(']').substringBefore(':').trim()
            val toRemove = map.keys.filter { k ->
                k in aliases ||
                    k.equals(bare, ignoreCase = true) ||
                    k.equals(host.trim(), ignoreCase = true) ||
                    k.startsWith("$bare:", ignoreCase = true) ||
                    k.startsWith("[$bare]", ignoreCase = true)
            }
            toRemove.forEach { map.remove(it) }
            saveMap(store, map)
            return toRemove.size
        }

        fun forgetExact(store: File, hostKey: String): Boolean {
            val map = loadMap(store)
            val removed = map.remove(hostKey) != null
            if (removed) saveMap(store, map)
            return removed
        }

        fun fingerprintOf(store: File, host: String, port: Int = 22): String? {
            val map = loadMap(store)
            hostAliases(host, port).forEach { a -> map[a]?.let { return it } }
            val bare = host.trim().removePrefix("[").substringBefore(']').substringBefore(':').trim()
            return map.entries.firstOrNull { (k, _) ->
                k.equals(bare, ignoreCase = true) ||
                    k.startsWith("$bare:", ignoreCase = true) ||
                    k.startsWith("[$bare]", ignoreCase = true)
            }?.value
        }

        fun clearAll(store: File) {
            runCatching {
                if (store.exists()) store.delete()
            }
        }

        fun entryCount(store: File): Int = loadMap(store).size

        fun fingerprint(key: ByteArray): String = "SHA256:" + Base64.encodeToString(
            MessageDigest.getInstance("SHA-256").digest(key),
            Base64.NO_WRAP or Base64.NO_PADDING)
    }
}
