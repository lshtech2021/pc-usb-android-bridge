package com.example.usbbridge

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import org.json.JSONArray
import org.json.JSONObject

/** Encrypted SSH connection profiles (phone-only; never sent to PC). */
object ConnectionStore {
    private const val PREFS = "usb_bridge_conn_enc"
    private const val KEY = "profiles"
    private const val FALLBACK = "usb_bridge_conn_fallback"

    data class Profile(
        val id: String,
        val name: String,
        val host: String,
        val port: Int,
        val user: String,
        val password: String?,
        val privateKey: String?,
        val passphrase: String?,
        val mfa: Boolean
    ) {
        fun toJson(): JSONObject = JSONObject()
            .put("id", id).put("name", name).put("host", host).put("port", port)
            .put("user", user)
            .put("password", password ?: JSONObject.NULL)
            .put("private_key", privateKey ?: JSONObject.NULL)
            .put("passphrase", passphrase ?: JSONObject.NULL)
            .put("mfa", mfa)

        companion object {
            fun fromJson(o: JSONObject): Profile = Profile(
                id = o.getString("id"),
                name = o.optString("name", o.getString("id")),
                host = o.getString("host"),
                port = o.optInt("port", 22),
                user = o.getString("user"),
                password = o.optNullable("password"),
                privateKey = o.optNullable("private_key"),
                passphrase = o.optNullable("passphrase"),
                mfa = o.optBoolean("mfa", false)
            )

            private fun JSONObject.optNullable(key: String): String? =
                if (!has(key) || isNull(key)) null else optString(key).ifEmpty { null }
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

    fun list(ctx: Context): List<Profile> {
        val raw = prefs(ctx).getString(KEY, "[]") ?: "[]"
        val arr = JSONArray(raw)
        return (0 until arr.length()).map { Profile.fromJson(arr.getJSONObject(it)) }
    }

    fun get(ctx: Context, id: String): Profile? = list(ctx).find { it.id == id }

    fun save(ctx: Context, profile: Profile) {
        val all = list(ctx).toMutableList()
        val i = all.indexOfFirst { it.id == profile.id }
        if (i >= 0) all[i] = profile else all.add(profile)
        writeAll(ctx, all)
    }

    fun delete(ctx: Context, id: String) {
        writeAll(ctx, list(ctx).filter { it.id != id })
    }

    fun nextId(ctx: Context): String {
        val used = list(ctx).map { it.id }.toSet()
        var n = 1
        while (true) {
            val id = "ID_%02d".format(n)
            if (id !in used) return id
            n++
        }
    }

    private fun writeAll(ctx: Context, profiles: List<Profile>) {
        val arr = JSONArray()
        profiles.forEach { arr.put(it.toJson()) }
        prefs(ctx).edit().putString(KEY, arr.toString()).apply()
    }
}
