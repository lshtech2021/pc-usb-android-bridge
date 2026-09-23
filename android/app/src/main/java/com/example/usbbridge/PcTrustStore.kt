package com.example.usbbridge

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import org.json.JSONArray
import org.json.JSONObject

/** Trusted PC fingerprints (TOFU). Private keys never stored — only name + pc_id + pubkey. */
object PcTrustStore {
    private const val PREFS = "usb_bridge_pc_trust_enc"
    private const val KEY = "pcs"
    private const val FALLBACK = "usb_bridge_pc_trust_fallback"

    data class Entry(val pcId: String, val pcName: String, val pcPubkeyB64: String) {
        fun toJson(): JSONObject = JSONObject()
            .put("pc_id", pcId)
            .put("pc_name", pcName)
            .put("pc_pubkey", pcPubkeyB64)

        companion object {
            fun fromJson(o: JSONObject) = Entry(
                pcId = o.getString("pc_id"),
                pcName = o.optString("pc_name", o.getString("pc_id")),
                pcPubkeyB64 = o.getString("pc_pubkey")
            )
        }
    }

    private fun prefs(ctx: Context): SharedPreferences {
        return try {
            val master = MasterKey.Builder(ctx)
                .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
                .build()
            EncryptedSharedPreferences.create(
                ctx, PREFS, master,
                EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
                EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM)
        } catch (_: Exception) {
            ctx.getSharedPreferences(FALLBACK, Context.MODE_PRIVATE)
        }
    }

    fun list(ctx: Context): List<Entry> {
        val raw = prefs(ctx).getString(KEY, "[]") ?: "[]"
        val arr = JSONArray(raw)
        return (0 until arr.length()).map { Entry.fromJson(arr.getJSONObject(it)) }
    }

    fun get(ctx: Context, pcId: String): Entry? = list(ctx).find { it.pcId == pcId }

    fun trust(ctx: Context, pcId: String, pcName: String, pcPubkeyB64: String) {
        val all = list(ctx).toMutableList()
        val i = all.indexOfFirst { it.pcId == pcId }
        val e = Entry(pcId, pcName, pcPubkeyB64)
        if (i >= 0) all[i] = e else all.add(e)
        writeAll(ctx, all)
    }

    fun forget(ctx: Context, pcId: String) {
        writeAll(ctx, list(ctx).filter { it.pcId != pcId })
    }

    private fun writeAll(ctx: Context, all: List<Entry>) {
        val arr = JSONArray()
        all.forEach { arr.put(it.toJson()) }
        prefs(ctx).edit().putString(KEY, arr.toString()).apply()
    }
}
