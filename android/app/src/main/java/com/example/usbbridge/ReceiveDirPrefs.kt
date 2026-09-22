package com.example.usbbridge

import android.content.Context
import android.content.Intent
import android.net.Uri

/** Persists the user-chosen folder (SAF tree URI) for PC → phone file receives. */
object ReceiveDirPrefs {
    private const val PREFS = "usb_bridge_prefs"
    private const val KEY_TREE_URI = "receive_tree_uri"

    fun getTreeUri(ctx: Context): Uri? {
        val s = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getString(KEY_TREE_URI, null) ?: return null
        return runCatching { Uri.parse(s) }.getOrNull()
    }

    fun setTreeUri(ctx: Context, uri: Uri?) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            .apply {
                if (uri == null) remove(KEY_TREE_URI) else putString(KEY_TREE_URI, uri.toString())
            }
            .apply()
    }

    fun takePersistable(ctx: Context, uri: Uri) {
        val flags = Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION
        runCatching {
            ctx.contentResolver.takePersistableUriPermission(uri, flags)
        }
        setTreeUri(ctx, uri)
    }

    fun clear(ctx: Context) {
        getTreeUri(ctx)?.let { uri ->
            val flags = Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION
            runCatching { ctx.contentResolver.releasePersistableUriPermission(uri, flags) }
        }
        setTreeUri(ctx, null)
    }

    fun defaultDirLabel(ctx: Context): String {
        val dir = ctx.getExternalFilesDir(null) ?: ctx.filesDir
        return dir.absolutePath
    }
}
