package com.example.usbbridge

import android.Manifest
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.DocumentsContract
import android.provider.OpenableColumns
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.widget.TextViewCompat
import androidx.documentfile.provider.DocumentFile
import java.io.File

/** Start the service + show the Token + send text/files from the phone + display messages sent by the PC */
class MainActivity : AppCompatActivity() {
    private lateinit var info: TextView
    private lateinit var saveFolderLabel: TextView
    private lateinit var msgLog: TextView
    private lateinit var input: EditText
    private lateinit var serviceButton: Button

    /** Resolve a dimens token to pixels (the UI is built in Kotlin, so paddings are raw px). */
    private fun dp(resId: Int): Int = resources.getDimensionPixelSize(resId)

    private val onPcText: (String) -> Unit = { text ->
        runOnUiThread { appendMsg("[PC] $text") }
    }

    private val notifPerm = registerForActivityResult(ActivityResultContracts.RequestPermission()) {}
    private val pickSaveDir = registerForActivityResult(ActivityResultContracts.OpenDocumentTree()) { uri ->
        if (uri == null) return@registerForActivityResult
        ReceiveDirPrefs.takePersistable(this, uri)
        refreshSaveFolderLabel()
        appendMsg("Save folder updated")
    }
    private val pickFile = registerForActivityResult(ActivityResultContracts.GetContent()) { uri ->
        if (uri == null) return@registerForActivityResult
        appendMsg("Preparing to send: ${displayName(uri)}")
        Thread {
            runCatching { copyToCache(uri) }
                .onSuccess {
                    val ok = BridgeService.broadcastFile(it, cleanup = true)
                    runOnUiThread {
                        appendMsg(
                            if (ok) "Send started: ${it.name}"
                            else "Send failed: PC not connected")
                    }
                }
                .onFailure { e -> runOnUiThread { appendMsg("Send failed: ${e.message}") } }
        }.start()
    }

    override fun onCreate(s: Bundle?) {
        super.onCreate(s)
        if (Build.VERSION.SDK_INT >= 33) {
            notifPerm.launch(Manifest.permission.POST_NOTIFICATIONS)   // Otherwise the "PC message arrived" alert would not be received
        }

        info = TextView(this).apply {
            TextViewCompat.setTextAppearance(this, R.style.TextAppearance_UsbBridge_Body)
            setTextIsSelectable(true)
        }
        saveFolderLabel = TextView(this).apply {
            TextViewCompat.setTextAppearance(this, R.style.TextAppearance_UsbBridge_Label)
            setPadding(0, dp(R.dimen.space_l), 0, dp(R.dimen.space_xs))
            setTextIsSelectable(true)
        }
        msgLog = TextView(this).apply {
            TextViewCompat.setTextAppearance(this, R.style.TextAppearance_UsbBridge_BodyDense)
            setPadding(0, dp(R.dimen.space_s), 0, dp(R.dimen.space_s))
            text = "(no messages yet)"
            setTextIsSelectable(true)
        }
        val msgTitle = TextView(this).apply {
            text = "Received messages (long-press to copy)"
            TextViewCompat.setTextAppearance(this, R.style.TextAppearance_UsbBridge_Body)
            setPadding(0, dp(R.dimen.space_xl), 0, dp(R.dimen.space_xs))
        }
        serviceButton = Button(this).apply {
            setOnClickListener {
                if (BridgeService.running) {
                    BridgeService.stop(this@MainActivity)
                } else {
                    ContextCompat.startForegroundService(
                        this@MainActivity, Intent(this@MainActivity, BridgeService::class.java))
                }
                postDelayed({ refreshServiceStatus() }, 300)
            }
        }
        val connections = Button(this).apply {
            text = "SSH Connections"
            setOnClickListener {
                startActivity(Intent(this@MainActivity, ConnectionsActivity::class.java))
            }
        }
        val trustedPcs = Button(this).apply {
            text = "Trusted PCs"
            setOnClickListener { showTrustedPcs() }
        }
        val chooseFolder = Button(this).apply { text = "Choose save folder" }
        val resetFolder = Button(this).apply { text = "Use app default folder" }
        val sendText = Button(this).apply { text = "Send text to PC" }
        val sendFile = Button(this).apply { text = "Send file to PC" }
        input = EditText(this).apply { hint = "Enter text to send to the PC" }
        val msgScroll = ScrollView(this).apply {
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f)
            isFillViewport = true
            addView(msgLog, LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT))
        }

        chooseFolder.setOnClickListener {
            val initial = ReceiveDirPrefs.getTreeUri(this)
            pickSaveDir.launch(initial)
        }
        resetFolder.setOnClickListener {
            ReceiveDirPrefs.clear(this)
            refreshSaveFolderLabel()
            appendMsg("Save folder reset to app default")
        }
        sendText.setOnClickListener {
            val t = input.text.toString().trim()
            if (t.isNotEmpty()) {
                if (BridgeService.broadcastText(t)) {
                    input.text.clear()
                    appendMsg("[Me] $t")
                } else {
                    appendMsg("Send failed: PC not connected")
                }
            }
        }
        sendFile.setOnClickListener { pickFile.launch("*/*") }

        setContentView(LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            val pad = dp(R.dimen.screen_padding)
            setPadding(pad, pad, pad, pad)
            addView(info)
            addView(serviceButton)
            addView(connections)
            addView(trustedPcs)
            addView(saveFolderLabel)
            addView(chooseFolder)
            addView(resetFolder)
            addView(input)
            addView(sendText)
            addView(sendFile)
            addView(msgTitle)
            addView(msgScroll)
        })
        refreshSaveFolderLabel()
        refreshServiceStatus()
    }

    override fun onStart() {
        super.onStart()
        AuthPrompts.bind(this)
        val recent = BridgeService.addTextListener(onPcText)
        if (recent.isNotEmpty()) {
            msgLog.text = recent.joinToString("\n") { "[PC] $it" }
        }
    }

    override fun onResume() {
        super.onResume()
        // Activity may be recreated after backgrounding; restore listening / token / SSH status
        refreshServiceStatus()
    }

    override fun onStop() {
        AuthPrompts.unbind(this)
        BridgeService.removeTextListener(onPcText)
        super.onStop()
    }

    private fun refreshServiceStatus() {
        if (!::info.isInitialized) return
        val ssh = ConnectionHub.runningIds()
        serviceButton.text = getString(
            if (BridgeService.running) R.string.action_stop_service
            else R.string.action_start_service)
        info.text = when {
            BridgeService.running && BridgeService.token.isNotEmpty() -> buildString {
                append("Listening on 127.0.0.1:${BridgeService.PORT}\n")
                append("Token: ${BridgeService.token}\n")
                if (ssh.isNotEmpty()) append("SSH running: ${ssh.joinToString(", ")}")
                else append("SSH: none (Start from SSH Connections)")
            }
            BridgeService.token.isNotEmpty() -> buildString {
                append("Bridge stopped\n")
                append("Token: ${BridgeService.token}\n")
                append("Starting again reuses this token, so the PC reconnects without re-pairing")
            }
            else -> "Service not running. Tap Start USB Bridge service."
        }
    }

    private fun showTrustedPcs() {
        val entries = PcTrustStore.list(this)
        if (entries.isEmpty()) {
            AlertDialog.Builder(this)
                .setTitle(R.string.title_trusted_pcs)
                .setMessage("No trusted PCs yet. Approve a PC when it connects.")
                .setPositiveButton("OK", null)
                .show()
            return
        }
        val labels = entries.map {
            "${it.pcName}\n${LinkCrypto.shortId(it.pcId)}…"
        }.toTypedArray()
        AlertDialog.Builder(this)
            .setTitle("Trusted PCs — tap to forget")
            .setItems(labels) { _, which ->
                val e = entries[which]
                AlertDialog.Builder(this)
                    .setTitle("Forget PC?")
                    .setMessage("Forget \"${e.pcName}\" (${LinkCrypto.shortId(e.pcId)}…)?\nNext connect will ask again.")
                    .setPositiveButton("Forget") { _, _ ->
                        PcTrustStore.forget(this, e.pcId)
                        appendMsg("Forgot PC ${e.pcName}")
                    }
                    .setNegativeButton("Cancel", null)
                    .show()
            }
            .setNegativeButton("Close", null)
            .show()
    }

    private fun refreshSaveFolderLabel() {
        val tree = ReceiveDirPrefs.getTreeUri(this)
        saveFolderLabel.text = if (tree != null) {
            "Save folder: ${friendlyTreeLabel(tree)}"
        } else {
            "Save folder: App default\n${ReceiveDirPrefs.defaultDirLabel(this)}"
        }
    }

    private fun friendlyTreeLabel(uri: Uri): String {
        val doc = DocumentFile.fromTreeUri(this, uri)
        val name = doc?.name
        if (!name.isNullOrBlank()) return name
        // Fallback: last path segment of the document id
        val docId = runCatching { DocumentsContract.getTreeDocumentId(uri) }.getOrNull()
        return docId ?: uri.toString()
    }

    private fun appendMsg(line: String) {
        val cur = msgLog.text?.toString().orEmpty()
        msgLog.text = if (cur == "(no messages yet)" || cur.isEmpty()) line else "$cur\n$line"
    }

    private fun displayName(uri: Uri): String =
        contentResolver.query(uri, null, null, null, null)?.use { c ->
            val i = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
            if (i >= 0 && c.moveToFirst()) c.getString(i) else null
        } ?: "file_${System.currentTimeMillis()}"

    private fun copyToCache(uri: Uri): File {
        val f = File(cacheDir, displayName(uri))
        contentResolver.openInputStream(uri)!!.use { ins -> f.outputStream().use { ins.copyTo(it) } }
        return f
    }
}
