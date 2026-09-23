package com.example.usbbridge

import android.app.AlertDialog
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
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.documentfile.provider.DocumentFile
import java.io.File

/** Start the service + show the Token + send text/files from the phone + display messages sent by the PC */
class MainActivity : AppCompatActivity() {
    private lateinit var info: TextView
    private lateinit var saveFolderLabel: TextView
    private lateinit var msgLog: TextView
    private lateinit var input: EditText

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
            textSize = 16f
            setTextIsSelectable(true)
        }
        saveFolderLabel = TextView(this).apply {
            textSize = 14f
            setPadding(0, 16, 0, 4)
            setTextIsSelectable(true)
        }
        msgLog = TextView(this).apply {
            textSize = 15f
            setPadding(0, 8, 0, 8)
            text = "(no messages yet)"
            setTextIsSelectable(true)
        }
        val msgTitle = TextView(this).apply {
            text = "Received messages (long-press to copy)"
            textSize = 16f
            setPadding(0, 24, 0, 4)
        }
        val start = Button(this).apply { text = "Start USB Bridge service" }
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

        start.setOnClickListener {
            ContextCompat.startForegroundService(this, Intent(this, BridgeService::class.java))
            info.postDelayed({
                info.text = "Service started, listening on 127.0.0.1:${BridgeService.PORT}\nToken: ${BridgeService.token}"
            }, 300)
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
            setPadding(40, 40, 40, 40)
            addView(info)
            addView(start)
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
    }

    override fun onStart() {
        super.onStart()
        AuthPrompts.bind(this)
        val recent = BridgeService.addTextListener(onPcText)
        if (recent.isNotEmpty()) {
            msgLog.text = recent.joinToString("\n") { "[PC] $it" }
        }
    }

    override fun onStop() {
        AuthPrompts.unbind(this)
        BridgeService.removeTextListener(onPcText)
        super.onStop()
    }

    private fun showTrustedPcs() {
        val entries = PcTrustStore.list(this)
        if (entries.isEmpty()) {
            AlertDialog.Builder(this)
                .setTitle("Trusted PCs")
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
