package com.example.usbbridge

import android.Manifest
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.OpenableColumns
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import java.io.File

/** Start the service + show the Token + send text/files from the phone + display messages sent by the PC */
class MainActivity : AppCompatActivity() {
    private lateinit var info: TextView
    private lateinit var msgLog: TextView
    private lateinit var input: EditText

    private val onPcText: (String) -> Unit = { text ->
        runOnUiThread { appendMsg("[PC] $text") }
    }

    private val notifPerm = registerForActivityResult(ActivityResultContracts.RequestPermission()) {}
    private val pickFile = registerForActivityResult(ActivityResultContracts.GetContent()) { uri ->
        if (uri == null) return@registerForActivityResult
        info.text = "准备发送: ${displayName(uri)}"
        Thread {
            runCatching { copyToCache(uri) }
                .onSuccess {
                    BridgeService.broadcastFile(it, cleanup = true)
                    runOnUiThread { info.text = "已发起发送: ${it.name}" }
                }
                .onFailure { e -> runOnUiThread { info.text = "发送失败: ${e.message}" } }
        }.start()
    }

    override fun onCreate(s: Bundle?) {
        super.onCreate(s)
        if (Build.VERSION.SDK_INT >= 33) {
            notifPerm.launch(Manifest.permission.POST_NOTIFICATIONS)   // Otherwise the "PC message arrived" alert would not be received
        }

        info = TextView(this).apply { textSize = 16f }
        msgLog = TextView(this).apply {
            textSize = 15f
            setPadding(0, 8, 0, 8)
            text = "（尚无消息）"
        }
        val start = Button(this).apply { text = "启动 USB Bridge 服务" }
        val sendText = Button(this).apply { text = "发送文本到 PC" }
        val sendFile = Button(this).apply { text = "发送文件到 PC" }
        input = EditText(this).apply { hint = "输入要发给 PC 的文本" }
        val msgTitle = TextView(this).apply {
            text = "收到的消息"
            textSize = 16f
            setPadding(0, 24, 0, 4)
        }
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
                info.text = "服务已启动，监听 127.0.0.1:${BridgeService.PORT}\nToken: ${BridgeService.token}"
            }, 300)
        }
        sendText.setOnClickListener {
            val t = input.text.toString().trim()
            if (t.isNotEmpty()) {
                BridgeService.broadcastText(t)
                input.text.clear()
                info.text = "已发送: $t"
                appendMsg("[我] $t")
            }
        }
        sendFile.setOnClickListener { pickFile.launch("*/*") }

        setContentView(LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(40, 40, 40, 40)
            addView(info)
            addView(start)
            addView(input)
            addView(sendText)
            addView(sendFile)
            addView(msgTitle)
            addView(msgScroll)
        })
    }

    override fun onStart() {
        super.onStart()
        val recent = BridgeService.addTextListener(onPcText)
        if (recent.isNotEmpty()) {
            msgLog.text = recent.joinToString("\n") { "[PC] $it" }
        }
    }

    override fun onStop() {
        BridgeService.removeTextListener(onPcText)
        super.onStop()
    }

    private fun appendMsg(line: String) {
        val cur = msgLog.text?.toString().orEmpty()
        msgLog.text = if (cur == "（尚无消息）" || cur.isEmpty()) line else "$cur\n$line"
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
