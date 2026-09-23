package com.example.usbbridge

import android.app.Activity
import android.app.AlertDialog
import android.content.Context
import android.os.Handler
import android.os.Looper
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import java.lang.ref.WeakReference
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

/**
 * Blocks a worker thread until the user answers a dialog on the main thread.
 * Used for MFA OTP and host-key trust during ConnectionHub.start().
 */
object AuthPrompts {
    @Volatile private var activityRef: WeakReference<Activity>? = null
    private val main = Handler(Looper.getMainLooper())

    fun bind(activity: Activity) {
        activityRef = WeakReference(activity)
    }

    fun unbind(activity: Activity) {
        if (activityRef?.get() === activity) activityRef = null
    }

    fun promptText(title: String, message: String, hint: String = ""): String? {
        val act = activityRef?.get() ?: return null
        val result = AtomicReference<String?>(null)
        val latch = CountDownLatch(1)
        main.post {
            try {
                val input = EditText(act).apply {
                    this.hint = hint
                    setSingleLine()
                }
                val box = LinearLayout(act).apply {
                    orientation = LinearLayout.VERTICAL
                    setPadding(48, 24, 48, 0)
                    addView(TextView(act).apply { text = message })
                    addView(input)
                }
                AlertDialog.Builder(act)
                    .setTitle(title)
                    .setView(box)
                    .setCancelable(false)
                    .setPositiveButton("OK") { _, _ ->
                        result.set(input.text?.toString())
                        latch.countDown()
                    }
                    .setNegativeButton("Cancel") { _, _ ->
                        result.set(null)
                        latch.countDown()
                    }
                    .show()
            } catch (_: Exception) {
                result.set(null)
                latch.countDown()
            }
        }
        latch.await(5, TimeUnit.MINUTES)
        return result.get()
    }

    fun promptYesNo(title: String, message: String): Boolean {
        val act = activityRef?.get() ?: return false
        val result = AtomicReference(false)
        val latch = CountDownLatch(1)
        main.post {
            try {
                AlertDialog.Builder(act)
                    .setTitle(title)
                    .setMessage(message)
                    .setCancelable(false)
                    .setPositiveButton("Trust") { _, _ ->
                        result.set(true); latch.countDown()
                    }
                    .setNegativeButton("Reject") { _, _ ->
                        result.set(false); latch.countDown()
                    }
                    .show()
            } catch (_: Exception) {
                result.set(false)
                latch.countDown()
            }
        }
        latch.await(5, TimeUnit.MINUTES)
        return result.get()
    }

    fun toast(ctx: Context, msg: String) {
        main.post {
            android.widget.Toast.makeText(ctx.applicationContext, msg, android.widget.Toast.LENGTH_LONG).show()
        }
    }
}
