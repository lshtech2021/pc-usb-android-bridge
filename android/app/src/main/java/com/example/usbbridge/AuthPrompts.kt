package com.example.usbbridge

import android.app.Activity
import android.app.AlertDialog
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.os.Handler
import android.os.Looper
import android.text.method.ScrollingMovementMethod
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

    fun copyText(ctx: Context, label: String, text: String) {
        val cm = ctx.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        cm.setPrimaryClip(ClipData.newPlainText(label, text))
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

    /** First-use host key trust with full selectable fingerprint + Copy. */
    fun promptTrustHostKey(host: String, fingerprint: String): Boolean {
        val act = activityRef?.get() ?: return false
        val result = AtomicReference(false)
        val latch = CountDownLatch(1)
        main.post {
            try {
                val box = fingerprintDialogBody(
                    act,
                    "First connection to $host\n\nFull fingerprint (long-press to select):\n",
                    fingerprint
                )
                val dialog = AlertDialog.Builder(act)
                    .setTitle("Trust host key?")
                    .setView(box)
                    .setCancelable(false)
                    .setPositiveButton("Trust") { _, _ ->
                        result.set(true); latch.countDown()
                    }
                    .setNegativeButton("Reject") { _, _ ->
                        result.set(false); latch.countDown()
                    }
                    .setNeutralButton("Copy", null)
                    .create()
                dialog.setOnShowListener {
                    dialog.getButton(AlertDialog.BUTTON_NEUTRAL).setOnClickListener {
                        copyText(act, "host-key", fingerprint)
                        toast(act, "Fingerprint copied")
                    }
                }
                dialog.show()
            } catch (_: Exception) {
                result.set(false)
                latch.countDown()
            }
        }
        latch.await(5, TimeUnit.MINUTES)
        return result.get()
    }

    /** Choice when a previously trusted host presents a new fingerprint. */
    enum class HostKeyChangedChoice { FORGET_AND_RETRUST, CANCEL }

    fun promptHostKeyChanged(host: String, oldFp: String?, newFp: String): HostKeyChangedChoice {
        val act = activityRef?.get() ?: return HostKeyChangedChoice.CANCEL
        val result = AtomicReference(HostKeyChangedChoice.CANCEL)
        val latch = CountDownLatch(1)
        main.post {
            try {
                val intro = buildString {
                    append("Host key for $host changed!\n\n")
                    append("This can mean the server was reinstalled — or a MITM attack.\n")
                    append("Long-press fingerprints to select, or use Copy.\n\n")
                    if (!oldFp.isNullOrBlank()) append("Previously trusted:\n")
                }
                val box = LinearLayout(act).apply {
                    orientation = LinearLayout.VERTICAL
                    setPadding(48, 24, 48, 8)
                    addView(TextView(act).apply { text = intro })
                    if (!oldFp.isNullOrBlank()) {
                        addView(selectableFingerprint(act, oldFp))
                        addView(TextView(act).apply {
                            text = "\nNew fingerprint:\n"
                            setPadding(0, 16, 0, 0)
                        })
                    } else {
                        addView(TextView(act).apply { text = "New fingerprint:\n" })
                    }
                    addView(selectableFingerprint(act, newFp))
                    addView(TextView(act).apply {
                        text = "\nOnly continue if you verified the new key with the server admin."
                        setPadding(0, 16, 0, 0)
                    })
                }
                val dialog = AlertDialog.Builder(act)
                    .setTitle("Host key changed")
                    .setView(box)
                    .setCancelable(false)
                    .setPositiveButton("Forget & re-trust") { _, _ ->
                        result.set(HostKeyChangedChoice.FORGET_AND_RETRUST)
                        latch.countDown()
                    }
                    .setNegativeButton("Cancel") { _, _ ->
                        result.set(HostKeyChangedChoice.CANCEL)
                        latch.countDown()
                    }
                    .setNeutralButton("Copy new", null)
                    .create()
                dialog.setOnShowListener {
                    dialog.getButton(AlertDialog.BUTTON_NEUTRAL).setOnClickListener {
                        copyText(act, "host-key-new", newFp)
                        toast(act, "New fingerprint copied")
                    }
                }
                dialog.show()
            } catch (_: Exception) {
                result.set(HostKeyChangedChoice.CANCEL)
                latch.countDown()
            }
        }
        latch.await(5, TimeUnit.MINUTES)
        return result.get()
    }

    private fun fingerprintDialogBody(act: Activity, header: String, fingerprint: String): LinearLayout {
        return LinearLayout(act).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 24, 48, 8)
            addView(TextView(act).apply { text = header })
            addView(selectableFingerprint(act, fingerprint))
        }
    }

    private fun selectableFingerprint(act: Activity, fingerprint: String): TextView {
        return TextView(act).apply {
            text = fingerprint
            textSize = 13f
            setTextIsSelectable(true)
            movementMethod = ScrollingMovementMethod.getInstance()
            setPadding(0, 8, 0, 8)
            setOnLongClickListener {
                copyText(act, "host-key", fingerprint)
                toast(act, "Fingerprint copied")
                true
            }
        }
    }

    fun toast(ctx: Context, msg: String) {
        main.post {
            android.widget.Toast.makeText(ctx.applicationContext, msg, android.widget.Toast.LENGTH_LONG).show()
        }
    }
}
