package com.example.usbbridge

import android.Manifest
import android.content.Intent
import android.content.res.ColorStateList
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.DocumentsContract
import android.provider.OpenableColumns
import android.text.format.DateFormat
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.ImageButton
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.view.ContextThemeWrapper
import androidx.core.content.ContextCompat
import androidx.core.widget.ImageViewCompat
import androidx.core.widget.TextViewCompat
import androidx.documentfile.provider.DocumentFile
import com.google.android.material.appbar.MaterialToolbar
import com.google.android.material.card.MaterialCardView
import java.io.File
import java.text.DateFormat as JavaDateFormat
import java.util.Date

/** Start the service + show the Token + send text/files from the phone + display messages sent by the PC */
class MainActivity : AppCompatActivity() {
    private lateinit var statusDot: View
    private lateinit var statusState: TextView
    private lateinit var statusDetail: TextView
    private lateinit var sshLine: TextView
    private lateinit var tokenValue: TextView
    private lateinit var tokenHint: TextView
    private lateinit var copyTokenButton: ImageButton
    private lateinit var folderValue: TextView
    private lateinit var folderDetail: TextView
    private lateinit var resetFolderButton: Button
    private lateinit var serviceButton: Button
    private lateinit var input: EditText
    private lateinit var messagesBox: LinearLayout
    private lateinit var messagesEmpty: TextView
    private lateinit var timeFormat: JavaDateFormat

    /** Assigned once the view tree exists; the log is rebuilt in onStart, which runs after. */
    private var messagesScroll: ScrollView? = null

    /** Resolve a dimens token to pixels (the UI is built in Kotlin, so paddings are raw px). */
    private fun dp(resId: Int): Int = resources.getDimensionPixelSize(resId)

    private fun color(resId: Int): Int = ContextCompat.getColor(this, resId)

    private val onPcText: (String) -> Unit = { text ->
        runOnUiThread { addBubble(text, fromMe = false, at = System.currentTimeMillis()) }
    }

    private val notifPerm = registerForActivityResult(ActivityResultContracts.RequestPermission()) {}
    private val pickSaveDir = registerForActivityResult(ActivityResultContracts.OpenDocumentTree()) { uri ->
        if (uri == null) return@registerForActivityResult
        ReceiveDirPrefs.takePersistable(this, uri)
        refreshSaveFolderLabel()
        addNotice(getString(R.string.notice_save_folder_updated))
    }
    private val pickFile = registerForActivityResult(ActivityResultContracts.GetContent()) { uri ->
        if (uri == null) return@registerForActivityResult
        addNotice(getString(R.string.notice_preparing_send, displayName(uri)))
        Thread {
            runCatching { copyToCache(uri) }
                .onSuccess { f ->
                    val ok = BridgeService.broadcastFile(f, cleanup = true)
                    runOnUiThread {
                        addNotice(
                            if (ok) getString(R.string.notice_send_started, f.name)
                            else getString(R.string.notice_send_failed_no_pc))
                    }
                }
                .onFailure { e ->
                    runOnUiThread { addNotice(getString(R.string.notice_send_failed, e.message.orEmpty())) }
                }
        }.start()
    }

    override fun onCreate(s: Bundle?) {
        super.onCreate(s)
        if (Build.VERSION.SDK_INT >= 33) {
            notifPerm.launch(Manifest.permission.POST_NOTIFICATIONS)   // Otherwise the "PC message arrived" alert would not be received
        }
        timeFormat = DateFormat.getTimeFormat(this)

        val scroll = ScrollView(this).apply {
            isFillViewport = true
            addView(buildContent(), LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT))
        }
        messagesScroll = scroll
        setContentView(LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            addView(MaterialToolbar(this@MainActivity).apply { setTitle(R.string.app_name) },
                LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT))
            // Everything above the composer scrolls together, so the log stays reachable on
            // short screens instead of being squeezed to nothing.
            addView(scroll, LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f))
            // The composer stays pinned, chat-style.
            addView(buildComposer())
        })

        refreshSaveFolderLabel()
        refreshServiceStatus()
    }

    // ---------- Layout building ----------

    private companion object {
        const val BUBBLE_MAX_WIDTH_FRACTION = 0.75f
    }

    private fun buildContent() = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        val pad = dp(R.dimen.screen_padding)
        setPadding(pad, pad, pad, pad)
        serviceButton = buildServiceButton()
        addView(buildStatusCard())
        addView(serviceButton, wrap(top = dp(R.dimen.space_l)))
        addView(sectionHeader(R.string.section_connections))
        addView(buildConnectionsCard())
        addView(sectionHeader(R.string.section_save_folder))
        addView(buildSaveFolderCard())
        addView(sectionHeader(R.string.section_messages))
        addView(TextView(this@MainActivity).apply {
            setText(R.string.hint_message_copy)
            TextViewCompat.setTextAppearance(this, R.style.TextAppearance_UsbBridge_Caption)
            setPadding(0, 0, 0, dp(R.dimen.space_s))
            layoutParams = wrap()
        })
        addView(buildMessagesArea())
    }

    private fun wrap(top: Int = 0, bottom: Int = 0) =
        LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT,
            LinearLayout.LayoutParams.WRAP_CONTENT).apply {
            topMargin = top
            bottomMargin = bottom
        }

    private fun sectionHeader(textRes: Int) = TextView(this).apply {
        setText(textRes)
        TextViewCompat.setTextAppearance(this, R.style.TextAppearance_UsbBridge_SectionHeader)
        setPadding(0, dp(R.dimen.space_xl), 0, dp(R.dimen.space_s))
    }

    private fun iconView(iconRes: Int, tintRes: Int) = ImageView(this).apply {
        setImageResource(iconRes)
        ImageViewCompat.setImageTintList(this, ColorStateList.valueOf(color(tintRes)))
        layoutParams = LinearLayout.LayoutParams(dp(R.dimen.icon_size), dp(R.dimen.icon_size))
        importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
    }

    private fun iconButton(iconRes: Int, cdRes: Int, tintRes: Int, onClick: () -> Unit) =
        ImageButton(this).apply {
            setImageResource(iconRes)
            ImageViewCompat.setImageTintList(this, ColorStateList.valueOf(color(tintRes)))
            background = ContextCompat.getDrawable(this@MainActivity, R.drawable.bg_icon_button)
            contentDescription = getString(cdRes)
            layoutParams = LinearLayout.LayoutParams(
                dp(R.dimen.icon_button_size), dp(R.dimen.icon_button_size))
            setOnClickListener { onClick() }
        }

    /**
     * A filled card grouping related rows. MaterialCardView is a FrameLayout, so rows go into
     * an inner vertical column rather than the card itself - adding them directly would stack
     * every row on top of the previous one.
     */
    private fun card(build: (LinearLayout) -> Unit): MaterialCardView {
        val column = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        build(column)
        return MaterialCardView(this).apply {
            radius = dp(R.dimen.card_corner_radius).toFloat()
            cardElevation = 0f
            strokeWidth = 0
            setCardBackgroundColor(color(R.color.brand_surface_variant))
            layoutParams = wrap(top = dp(R.dimen.space_xs))
            addView(column, FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.WRAP_CONTENT))
        }
    }

    private fun divider() = View(this).apply {
        background = ContextCompat.getDrawable(this@MainActivity, R.drawable.bg_divider)
        alpha = 0.35f
        layoutParams = LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, dp(R.dimen.divider_height)).apply {
            marginStart = dp(R.dimen.space_l)
        }
    }

    /** One row: leading icon, a title, and optional trailing views. The caller wires up clicks. */
    private fun listRow(
        iconRes: Int,
        titleRes: Int,
        trailing: List<View> = emptyList()
    ): LinearLayout {
        val label = TextView(this).apply {
            setText(titleRes)
            TextViewCompat.setTextAppearance(this, R.style.TextAppearance_UsbBridge_Body)
            layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
            setPadding(dp(R.dimen.space_l), 0, dp(R.dimen.space_s), 0)
        }
        return LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            minimumHeight = dp(R.dimen.row_min_height)
            background = ContextCompat.getDrawable(this@MainActivity, R.drawable.bg_row_ripple)
            val pad = dp(R.dimen.card_padding)
            setPadding(pad, dp(R.dimen.space_s), pad, dp(R.dimen.space_s))
            layoutParams = wrap()
            addView(iconView(iconRes, R.color.brand_on_surface_variant))
            addView(label)
            trailing.forEach { addView(it) }
        }
    }

    private fun clickableRow(row: LinearLayout, onClick: () -> Unit) = row.apply {
        isClickable = true
        isFocusable = true
        setOnClickListener { onClick() }
    }

    private fun buildServiceButton() = Button(
        ContextThemeWrapper(this, R.style.ThemeOverlay_UsbBridge_PrimaryButton)
    ).apply {
        setOnClickListener {
            if (BridgeService.running) BridgeService.stop(this@MainActivity)
            else ContextCompat.startForegroundService(
                this@MainActivity, Intent(this@MainActivity, BridgeService::class.java))
            postDelayed({ refreshServiceStatus() }, 300)
        }
    }

    private fun buildStatusCard(): View {
        statusDot = View(this).apply {
            background = ContextCompat.getDrawable(this@MainActivity, R.drawable.bg_status_dot)
            layoutParams = LinearLayout.LayoutParams(
                dp(R.dimen.status_dot_size), dp(R.dimen.status_dot_size)).apply {
                marginEnd = dp(R.dimen.space_s)
            }
        }
        statusState = TextView(this).apply {
            TextViewCompat.setTextAppearance(this, R.style.TextAppearance_UsbBridge_Body)
        }
        statusDetail = TextView(this).apply {
            TextViewCompat.setTextAppearance(this, R.style.TextAppearance_UsbBridge_Caption)
        }
        sshLine = TextView(this).apply {
            TextViewCompat.setTextAppearance(this, R.style.TextAppearance_UsbBridge_Caption)
            visibility = View.GONE
        }
        tokenValue = TextView(this).apply {
            TextViewCompat.setTextAppearance(this, R.style.TextAppearance_UsbBridge_Body)
            setTextIsSelectable(true)
            layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
        }
        tokenHint = TextView(this).apply {
            setText(R.string.token_none)
            TextViewCompat.setTextAppearance(this, R.style.TextAppearance_UsbBridge_Caption)
            visibility = View.GONE
        }
        copyTokenButton = iconButton(
            R.drawable.ic_copy, R.string.cd_copy_token, R.color.brand_primary) { copyToken() }

        return card { column ->
            column.setPadding(dp(R.dimen.card_padding), dp(R.dimen.card_padding),
                dp(R.dimen.card_padding), dp(R.dimen.card_padding))
            column.addView(LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                addView(statusDot)
                addView(statusState)
            })
            column.addView(statusDetail)
            column.addView(sshLine)
            column.addView(LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                setPadding(0, dp(R.dimen.space_s), 0, 0)
                addView(TextView(this@MainActivity).apply {
                    setText(R.string.label_token)
                    TextViewCompat.setTextAppearance(this, R.style.TextAppearance_UsbBridge_Caption)
                    setPadding(0, 0, dp(R.dimen.space_s), 0)
                })
                addView(tokenValue)
                addView(copyTokenButton)
            })
            column.addView(tokenHint)
        }
    }

    private fun buildConnectionsCard() = card { column ->
        column.addView(clickableRow(
            listRow(R.drawable.ic_terminal, R.string.title_ssh_connections,
                trailing = listOf(iconView(R.drawable.ic_chevron_right, R.color.brand_on_surface_variant)))
        ) {
            startActivity(Intent(this, ConnectionsActivity::class.java))
        })
        column.addView(divider())
        column.addView(clickableRow(
            listRow(R.drawable.ic_shield_check, R.string.title_trusted_pcs,
                trailing = listOf(iconView(R.drawable.ic_chevron_right, R.color.brand_on_surface_variant)))
        ) { showTrustedPcs() })
    }

    private fun buildSaveFolderCard(): View {
        folderValue = TextView(this).apply {
            TextViewCompat.setTextAppearance(this, R.style.TextAppearance_UsbBridge_Body)
        }
        folderDetail = TextView(this).apply {
            TextViewCompat.setTextAppearance(this, R.style.TextAppearance_UsbBridge_Caption)
        }
        // Text-button variant, applied through a theme overlay (see styles.xml).
        val textButtonCtx = ContextThemeWrapper(this, R.style.ThemeOverlay_UsbBridge_TextButton)
        val change = Button(textButtonCtx).apply {
            setText(R.string.action_change)
            setOnClickListener { pickSaveDir.launch(ReceiveDirPrefs.getTreeUri(this@MainActivity)) }
        }
        resetFolderButton = Button(textButtonCtx).apply {
            setText(R.string.action_reset_default)
            setOnClickListener {
                ReceiveDirPrefs.clear(this@MainActivity)
                refreshSaveFolderLabel()
                addNotice(getString(R.string.notice_save_folder_reset))
            }
        }

        return card { column ->
            column.addView(LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                minimumHeight = dp(R.dimen.row_min_height)
                val pad = dp(R.dimen.card_padding)
                setPadding(pad, dp(R.dimen.space_s), pad, dp(R.dimen.space_s))
                addView(iconView(R.drawable.ic_folder, R.color.brand_on_surface_variant))
                addView(LinearLayout(this@MainActivity).apply {
                    orientation = LinearLayout.VERTICAL
                    layoutParams = LinearLayout.LayoutParams(
                        0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
                    setPadding(dp(R.dimen.space_l), 0, dp(R.dimen.space_s), 0)
                    addView(folderValue)
                    addView(folderDetail)
                })
                addView(change)
                addView(resetFolderButton)
            })
        }
    }

    private fun buildMessagesArea(): View {
        messagesEmpty = TextView(this).apply {
            setText(R.string.empty_no_messages)
            TextViewCompat.setTextAppearance(this, R.style.TextAppearance_UsbBridge_Caption)
            gravity = Gravity.CENTER
            setPadding(0, dp(R.dimen.space_l), 0, dp(R.dimen.space_l))
        }
        messagesBox = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        return LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            addView(messagesEmpty)
            addView(messagesBox)
        }
    }

    private fun buildComposer(): View {
        input = EditText(this).apply {
            setHint(R.string.hint_send_text)
            // The bar itself is the field's container, so drop the default underline.
            background = null
            layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
        }
        return LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setBackgroundColor(color(R.color.brand_surface_variant))
            val pad = dp(R.dimen.screen_padding)
            setPadding(pad, dp(R.dimen.space_s), pad, dp(R.dimen.space_s))
            addView(input)
            addView(iconButton(R.drawable.ic_send, R.string.cd_send_text, R.color.brand_primary) {
                sendInputText()
            })
            addView(iconButton(R.drawable.ic_upload, R.string.cd_send_file, R.color.brand_on_surface_variant) {
                pickFile.launch("*/*")
            })
        }
    }

    // ---------- Message log ----------

    private fun addBubble(text: String, fromMe: Boolean, at: Long, scroll: Boolean = true) {
        if (!::messagesBox.isInitialized) return
        messagesEmpty.visibility = View.GONE

        val bodyColor = color(if (fromMe) R.color.brand_on_primary_container else R.color.brand_on_surface)
        val bubble = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            background = ContextCompat.getDrawable(this@MainActivity, R.drawable.bg_bubble)
            backgroundTintList = ColorStateList.valueOf(
                color(if (fromMe) R.color.brand_primary_container else R.color.brand_surface_variant))
            val h = dp(R.dimen.bubble_padding_horizontal)
            val v = dp(R.dimen.bubble_padding_vertical)
            setPadding(h, v, h, v)
            addView(TextView(this@MainActivity).apply {
                this.text = text
                TextViewCompat.setTextAppearance(this, R.style.TextAppearance_UsbBridge_BodyDense)
                setTextColor(bodyColor)
                maxWidth = (resources.displayMetrics.widthPixels * BUBBLE_MAX_WIDTH_FRACTION).toInt()
            })
            addView(TextView(this@MainActivity).apply {
                this.text = timeFormat.format(Date(at))
                TextViewCompat.setTextAppearance(this, R.style.TextAppearance_UsbBridge_BubbleTime)
                setTextColor(bodyColor)
                alpha = 0.7f
                gravity = Gravity.END
            })
            // Copies this one message; the old selectable block copied the whole log.
            setOnLongClickListener {
                AuthPrompts.copyText(this@MainActivity, "message", text)
                toast(getString(R.string.message_copied))
                true
            }
        }
        messagesBox.addView(LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            // MATCH_PARENT so gravity actually places the bubble left or right.
            gravity = if (fromMe) Gravity.END else Gravity.START
            setPadding(0, dp(R.dimen.bubble_gap), 0, 0)
            layoutParams = wrap()
            addView(bubble)
        })
        if (scroll) scrollToLatest()
    }

    /** System notices (folder changed, send failed) are not chat messages: centred and muted. */
    private fun addNotice(text: String) {
        if (!::messagesBox.isInitialized) return
        messagesEmpty.visibility = View.GONE
        messagesBox.addView(TextView(this).apply {
            this.text = text
            TextViewCompat.setTextAppearance(this, R.style.TextAppearance_UsbBridge_Notice)
            gravity = Gravity.CENTER
            setPadding(0, dp(R.dimen.space_s), 0, 0)
            layoutParams = wrap()
        })
        scrollToLatest()
    }

    private fun scrollToLatest() {
        messagesScroll?.post { messagesScroll?.fullScroll(View.FOCUS_DOWN) }
    }

    private fun sendInputText() {
        val t = input.text.toString().trim()
        if (t.isEmpty()) return
        if (BridgeService.broadcastText(t)) {
            input.text.clear()
            addBubble(t, fromMe = true, at = System.currentTimeMillis())
        } else {
            addNotice(getString(R.string.notice_send_failed_no_pc))
        }
    }

    private fun copyToken() {
        val token = BridgeService.token
        if (token.isEmpty()) return
        AuthPrompts.copyText(this, "token", token)
        toast(getString(R.string.token_copied))
    }

    // ---------- Lifecycle ----------

    override fun onStart() {
        super.onStart()
        AuthPrompts.bind(this)
        // Rebuild the log from the service cache so it survives backgrounding. Only PC
        // messages are cached; text you sent is not persisted.
        val recent = BridgeService.addTextListener(onPcText)
        messagesBox.removeAllViews()
        if (recent.isEmpty()) {
            messagesEmpty.visibility = View.VISIBLE
        } else {
            messagesEmpty.visibility = View.GONE
            recent.forEach { addBubble(it.text, fromMe = false, at = it.at, scroll = false) }
            scrollToLatest()
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
        if (!::statusState.isInitialized) return
        val running = BridgeService.running
        val ssh = ConnectionHub.runningIds()

        serviceButton.setText(if (running) R.string.action_stop_service else R.string.action_start_service)

        statusState.setText(if (running) R.string.state_running else R.string.state_stopped)
        statusDot.backgroundTintList = ColorStateList.valueOf(
            color(if (running) R.color.status_running else R.color.status_stopped))
        statusDetail.text = if (running) {
            getString(R.string.status_listening, BridgeService.PORT)
        } else {
            getString(R.string.status_not_listening)
        }

        if (ssh.isEmpty()) {
            sshLine.visibility = View.GONE
        } else {
            sshLine.visibility = View.VISIBLE
            sshLine.text = getString(R.string.status_ssh_running, ssh.joinToString(", "))
        }

        val token = BridgeService.token
        val hasToken = token.isNotEmpty()
        if (hasToken) tokenValue.text = token
        tokenValue.visibility = if (hasToken) View.VISIBLE else View.GONE
        copyTokenButton.visibility = if (hasToken) View.VISIBLE else View.GONE
        tokenHint.visibility = if (hasToken) View.GONE else View.VISIBLE
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
                        addNotice(getString(R.string.notice_forgot_pc, e.pcName))
                    }
                    .setNegativeButton("Cancel", null)
                    .show()
            }
            .setNegativeButton("Close", null)
            .show()
    }

    private fun refreshSaveFolderLabel() {
        val tree = ReceiveDirPrefs.getTreeUri(this)
        if (tree != null) {
            folderValue.text = friendlyTreeLabel(tree)
            folderDetail.visibility = View.GONE
            resetFolderButton.visibility = View.VISIBLE
        } else {
            folderValue.setText(R.string.folder_app_default)
            folderDetail.text = ReceiveDirPrefs.defaultDirLabel(this)
            folderDetail.visibility = View.VISIBLE
            // Nothing to reset while the default is already in use.
            resetFolderButton.visibility = View.GONE
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

    private fun toast(msg: String) = Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()
}
