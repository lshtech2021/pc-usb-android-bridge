package com.example.usbbridge

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.res.ColorStateList
import android.net.Uri
import android.os.Bundle
import android.text.InputType
import android.view.Gravity
import android.view.Menu
import android.view.MenuItem
import android.view.View
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.view.ContextThemeWrapper
import androidx.core.content.ContextCompat
import androidx.core.graphics.ColorUtils
import androidx.core.widget.ImageViewCompat
import androidx.core.widget.TextViewCompat
import com.google.android.material.appbar.MaterialToolbar
import com.google.android.material.card.MaterialCardView
import com.google.android.material.floatingactionbutton.FloatingActionButton
import java.util.Locale

/** CRUD + Start/Stop for phone-managed SSH connection profiles. */
class ConnectionsActivity : AppCompatActivity() {
    private lateinit var listBox: LinearLayout
    private val refreshListener: () -> Unit = { runOnUiThread { renderList() } }

    /** Filled by the open edit dialog; used when a PEM file is picked. */
    private var pendingKeyField: EditText? = null
    private var pendingKeyStatus: TextView? = null

    private val pickPem = registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        val field = pendingKeyField ?: return@registerForActivityResult
        if (uri == null) return@registerForActivityResult
        runCatching { readPemFromUri(uri) }
            .onSuccess { pem ->
                if (pem.isBlank()) {
                    toast(getString(R.string.pem_file_empty))
                    return@onSuccess
                }
                field.setText(pem)
                updateKeyStatus(pem)
                when {
                    !looksLikePem(pem) ->
                        toast(getString(R.string.pem_not_a_key))
                    pem.contains("BEGIN OPENSSH PRIVATE KEY") ->
                        toast(getString(R.string.pem_openssh_loaded))
                    else ->
                        toast(getString(R.string.pem_loaded))
                }
            }
            .onFailure { e -> toast(getString(R.string.pem_read_failed, e.message.orEmpty())) }
    }

    override fun onCreate(s: Bundle?) {
        super.onCreate(s)
        AuthPrompts.bind(this)
        ConnectionHub.ensureSlots(this)

        listBox = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        val toolbar = MaterialToolbar(this)
        val scroll = ScrollView(this).apply {
            isFillViewport = true
            // Let the list scroll clear of the floating action button.
            clipToPadding = false
            val pad = dp(R.dimen.screen_padding)
            setPadding(pad, pad, pad, pad + dp(R.dimen.fab_clearance))
            addView(listBox, matchWidth())
        }
        val fab = FloatingActionButton(this).apply {
            setImageResource(R.drawable.ic_add)
            ImageViewCompat.setImageTintList(
                this, ColorStateList.valueOf(color(R.color.brand_on_primary_container)))
            contentDescription = getString(R.string.action_add_connection)
            setOnClickListener { editDialog(null) }
        }

        setContentView(FrameLayout(this).apply {
            addView(LinearLayout(this@ConnectionsActivity).apply {
                orientation = LinearLayout.VERTICAL
                addView(toolbar, LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT))
                addView(scroll, LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f))
            }, FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT))
            addView(fab, FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.WRAP_CONTENT,
                FrameLayout.LayoutParams.WRAP_CONTENT).apply {
                gravity = Gravity.END or Gravity.BOTTOM
                marginEnd = dp(R.dimen.space_l)
                bottomMargin = dp(R.dimen.space_l)
            })
        })

        setSupportActionBar(toolbar)
        setTitle(R.string.title_ssh_connections)
        supportActionBar?.setDisplayHomeAsUpEnabled(true)
        renderList()
    }

    // The manifest already declares MainActivity as the parent, so up == back.
    override fun onSupportNavigateUp(): Boolean {
        onBackPressedDispatcher.onBackPressed()
        return true
    }

    // "Clear all host keys" is destructive and rare, so it lives in the overflow rather than
    // competing with Add for the top of the screen.
    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menu.add(Menu.NONE, MENU_CLEAR_HOST_KEYS, Menu.NONE, R.string.action_clear_host_keys)
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean =
        when (item.itemId) {
            MENU_CLEAR_HOST_KEYS -> {
                confirmClearAllHostKeys()
                true
            }
            else -> super.onOptionsItemSelected(item)
        }

    override fun onStart() {
        super.onStart()
        AuthPrompts.bind(this)
        ConnectionHub.addListener(refreshListener)
        renderList()
    }

    override fun onStop() {
        ConnectionHub.removeListener(refreshListener)
        AuthPrompts.unbind(this)
        super.onStop()
    }

    // ---------- List ----------

    private fun renderList() {
        ConnectionHub.ensureSlots(this)
        listBox.removeAllViews()
        val profiles = ConnectionStore.list(this)
        if (profiles.isEmpty()) {
            listBox.addView(TextView(this).apply {
                setText(R.string.empty_no_connections)
                TextViewCompat.setTextAppearance(this, R.style.TextAppearance_UsbBridge_BodyDense)
                setPadding(0, dp(R.dimen.space_xl), 0, 0)
            })
            return
        }
        profiles.forEach { listBox.addView(profileCard(it)) }
    }

    private fun profileCard(p: ConnectionStore.Profile): View {
        val state = ConnectionHub.stateOf(p.id)
        val err = ConnectionHub.lastError(p.id)
        val fp = TofuHostKeys.fingerprintOf(TofuHostKeys.storeFile(this), p.host, p.port)

        val column = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }

        // Title is the user's name for the connection; the ID is shown once, below.
        column.addView(LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            addView(TextView(this@ConnectionsActivity).apply {
                text = p.name
                TextViewCompat.setTextAppearance(this, R.style.TextAppearance_UsbBridge_Body)
                layoutParams = LinearLayout.LayoutParams(
                    0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
            })
            addView(stateChip(state))
        })

        column.addView(caption(getString(R.string.profile_target, p.id, p.user, p.host, p.port)))
        column.addView(caption(getString(R.string.profile_auth, authLabel(p))))
        if (!err.isNullOrBlank()) {
            column.addView(caption(getString(R.string.profile_error, err), R.color.brand_error))
        }

        // The whole host-key story - view the full value, copy it, forget it - is behind this
        // one line, instead of spilling past the screen edge and needing three separate buttons.
        column.addView(if (fp == null) {
            caption(getString(R.string.host_key_none))
        } else {
            caption(getString(R.string.host_key_line, shortenFingerprint(fp))).apply {
                isClickable = true
                isFocusable = true
                setOnClickListener { showHostKeyDialog(p, fp) }
            }
        })

        column.addView(actionRow(p, state))

        return MaterialCardView(this).apply {
            radius = dp(R.dimen.card_corner_radius).toFloat()
            cardElevation = 0f
            strokeWidth = 0
            setCardBackgroundColor(color(R.color.brand_surface_variant))
            layoutParams = matchWidth(top = dp(R.dimen.space_s))
            setPadding(dp(R.dimen.card_padding), dp(R.dimen.card_padding),
                dp(R.dimen.card_padding), dp(R.dimen.card_padding))
            addView(column, FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.WRAP_CONTENT))
        }
    }

    private fun caption(text: CharSequence, @androidx.annotation.ColorRes tint: Int? = null) =
        TextView(this).apply {
            this.text = text
            TextViewCompat.setTextAppearance(this, R.style.TextAppearance_UsbBridge_Caption)
            if (tint != null) setTextColor(color(tint))
        }

    /** Running / Starting / Error / Stopped, colour-coded, instead of buried mid-sentence. */
    private fun stateChip(state: String): TextView {
        val labelRes = when (state) {
            ConnectionHub.STATE_RUNNING -> R.string.state_running
            ConnectionHub.STATE_STARTING -> R.string.state_starting
            ConnectionHub.STATE_ERROR -> R.string.state_error
            else -> R.string.state_stopped
        }
        val tint = color(
            when (state) {
                ConnectionHub.STATE_RUNNING -> R.color.status_running
                ConnectionHub.STATE_STARTING -> R.color.brand_tertiary
                ConnectionHub.STATE_ERROR -> R.color.brand_error
                else -> R.color.status_stopped
            })
        return TextView(this).apply {
            setText(labelRes)
            TextViewCompat.setTextAppearance(this, R.style.TextAppearance_UsbBridge_Caption)
            setTextColor(tint)
            background = ContextCompat.getDrawable(this@ConnectionsActivity, R.drawable.bg_chip)
            backgroundTintList = ColorStateList.valueOf(ColorUtils.setAlphaComponent(tint, CHIP_TINT_ALPHA))
            setPadding(dp(R.dimen.chip_padding_horizontal), dp(R.dimen.chip_padding_vertical),
                dp(R.dimen.chip_padding_horizontal), dp(R.dimen.chip_padding_vertical))
        }
    }

    private fun actionRow(p: ConnectionStore.Profile, state: String): View {
        val active = state == ConnectionHub.STATE_RUNNING || state == ConnectionHub.STATE_STARTING
        val destructiveCtx = ContextThemeWrapper(this, R.style.ThemeOverlay_UsbBridge_DestructiveButton)
        // One state-dependent button instead of showing Start and Stop at the same time.
        val primary = Button(this).apply {
            setText(if (active) R.string.action_stop else R.string.action_start)
            setOnClickListener {
                if (active) ConnectionHub.stop(p.id)
                else ConnectionHub.start(this@ConnectionsActivity, p.id)
            }
            layoutParams = actionButtonParams()
        }
        val edit = Button(this).apply {
            setText(R.string.action_edit)
            setOnClickListener { editDialog(p) }
            layoutParams = actionButtonParams()
        }
        val delete = Button(destructiveCtx).apply {
            setText(R.string.action_delete)
            setOnClickListener { confirmDelete(p) }
        }
        return LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(0, dp(R.dimen.space_s), 0, 0)
            addView(primary)
            addView(edit)
            addView(delete)
        }
    }

    private fun actionButtonParams() = LinearLayout.LayoutParams(
        LinearLayout.LayoutParams.WRAP_CONTENT,
        LinearLayout.LayoutParams.WRAP_CONTENT).apply {
        marginEnd = dp(R.dimen.space_s)
    }

    private fun authLabel(p: ConnectionStore.Profile): String = when {
        !p.privateKey.isNullOrBlank() && !p.password.isNullOrBlank() ->
            getString(R.string.auth_key_and_password)
        !p.privateKey.isNullOrBlank() -> getString(R.string.auth_private_key)
        !p.password.isNullOrBlank() -> getString(R.string.auth_password)
        else -> getString(R.string.auth_none)
    }

    private fun shortenFingerprint(fp: String) =
        if (fp.length <= FINGERPRINT_MAX_CHARS) fp else fp.take(FINGERPRINT_MAX_CHARS - 1) + "…"

    // ---------- Host key ----------

    /** View the full fingerprint, copy it, or forget it - all in one place. */
    private fun showHostKeyDialog(p: ConnectionStore.Profile, fp: String) {
        val body = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(R.dimen.dialog_padding_horizontal), dp(R.dimen.space_xl),
                dp(R.dimen.dialog_padding_horizontal), dp(R.dimen.space_s))
            addView(TextView(this@ConnectionsActivity).apply {
                text = getString(R.string.host_key_message, p.host, p.port)
            })
            addView(TextView(this@ConnectionsActivity).apply {
                text = fp
                TextViewCompat.setTextAppearance(this, R.style.TextAppearance_UsbBridge_Caption)
                setTextIsSelectable(true)
                setPadding(0, dp(R.dimen.space_s), 0, dp(R.dimen.space_s))
            })
        }
        val dialog = AlertDialog.Builder(this)
            .setTitle(R.string.title_host_key)
            .setView(body)
            .setPositiveButton(R.string.action_forget) { _, _ -> forgetHostKey(p) }
            .setNeutralButton(R.string.action_copy, null)
            .setNegativeButton(R.string.action_close, null)
            .create()
        dialog.setOnShowListener {
            dialog.getButton(AlertDialog.BUTTON_NEUTRAL).setOnClickListener {
                AuthPrompts.copyText(this, "host-key", fp)
                toast(getString(R.string.fingerprint_copied))
            }
        }
        dialog.show()
    }

    private fun forgetHostKey(p: ConnectionStore.Profile) {
        val n = TofuHostKeys.forgetHost(TofuHostKeys.storeFile(this), p.host, p.port)
        toast(getString(if (n > 0) R.string.host_key_forgotten else R.string.nothing_to_forget))
        renderList()
    }

    private fun confirmClearAllHostKeys() {
        val store = TofuHostKeys.storeFile(this)
        val n = TofuHostKeys.entryCount(store)
        if (n == 0) {
            toast(getString(R.string.clear_keys_none))
            return
        }
        AlertDialog.Builder(this)
            .setTitle(R.string.clear_keys_title)
            .setMessage(resources.getQuantityString(R.plurals.clear_keys_message, n, n))
            .setPositiveButton(R.string.clear_keys_action) { _, _ ->
                TofuHostKeys.clearAll(store)
                toast(getString(R.string.clear_keys_done))
                renderList()
            }
            .setNegativeButton(R.string.action_cancel, null)
            .show()
    }

    private fun confirmDelete(p: ConnectionStore.Profile) {
        AlertDialog.Builder(this)
            .setTitle(getString(R.string.delete_profile_title, p.id))
            .setMessage(R.string.delete_profile_message)
            .setPositiveButton(R.string.action_delete) { _, _ ->
                ConnectionHub.stop(p.id)
                ConnectionStore.delete(this, p.id)
                toast(getString(R.string.profile_deleted, p.id))
                renderList()
            }
            .setNegativeButton(R.string.action_cancel, null)
            .show()
    }

    // ---------- Add / edit ----------

    private fun editDialog(existing: ConnectionStore.Profile?) {
        val id = existing?.id ?: ConnectionStore.nextId(this)
        val name = EditText(this).apply {
            hint = getString(R.string.field_name); setText(existing?.name ?: id)
        }
        val host = EditText(this).apply {
            hint = getString(R.string.field_host); setText(existing?.host ?: "")
        }
        val port = EditText(this).apply {
            hint = getString(R.string.field_port)
            // Locale.ROOT: a port is ASCII digits, never localised digits or grouping.
            setText(String.format(Locale.ROOT, "%d", existing?.port ?: 22))
            inputType = InputType.TYPE_CLASS_NUMBER
        }
        val user = EditText(this).apply {
            hint = getString(R.string.field_user); setText(existing?.user ?: "")
        }
        val password = EditText(this).apply {
            hint = if (existing?.password != null) getString(R.string.field_password_keep)
            else getString(R.string.field_password)
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
        }
        val showPassword = CheckBox(this).apply {
            setText(R.string.field_show_password)
            setOnCheckedChangeListener { _, checked -> setPasswordVisible(password, checked) }
        }
        val key = EditText(this).apply {
            hint = if (existing?.privateKey != null) getString(R.string.field_key_keep)
            else getString(R.string.field_key)
            minLines = 4
            inputType = InputType.TYPE_CLASS_TEXT or
                InputType.TYPE_TEXT_FLAG_MULTI_LINE or
                InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
        }
        val keyStatus = TextView(this).apply {
            TextViewCompat.setTextAppearance(this, R.style.TextAppearance_UsbBridge_Caption)
            setPadding(0, dp(R.dimen.space_xs), 0, dp(R.dimen.space_s))
            text = if (existing?.privateKey != null) {
                getString(R.string.key_status_saved)
            } else {
                getString(R.string.key_status_none)
            }
        }
        pendingKeyField = key
        pendingKeyStatus = keyStatus

        val keyActions = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        keyActions.addView(Button(this).apply {
            setText(R.string.action_pick_key)
            setOnClickListener {
                pickPem.launch(arrayOf(
                    "application/x-pem-file",
                    "application/x-x509-ca-cert",
                    "application/pkcs8",
                    "text/plain",
                    "*/*"
                ))
            }
        })
        keyActions.addView(Button(this).apply {
            setText(R.string.action_paste_key)
            setOnClickListener {
                val clip = readClipboardText()
                if (clip.isNullOrBlank()) {
                    toast(getString(R.string.clipboard_empty))
                    return@setOnClickListener
                }
                key.setText(clip)
                updateKeyStatus(clip)
                when {
                    clip.contains("BEGIN OPENSSH PRIVATE KEY") ->
                        toast(getString(R.string.key_pasted_openssh))
                    looksLikePem(clip) ->
                        toast(getString(R.string.key_pasted_pem))
                    else ->
                        toast(getString(R.string.key_pasted_unlike))
                }
            }
        })
        keyActions.addView(Button(this).apply {
            setText(R.string.action_clear)
            setOnClickListener {
                key.text.clear()
                keyStatus.text = if (existing?.privateKey != null) {
                    getString(R.string.key_cleared_keeps_existing)
                } else {
                    getString(R.string.key_cleared_none)
                }
            }
        })

        val phrase = EditText(this).apply {
            hint = getString(R.string.field_passphrase)
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
        }
        val showPhrase = CheckBox(this).apply {
            setText(R.string.field_show_passphrase)
            setOnCheckedChangeListener { _, checked -> setPasswordVisible(phrase, checked) }
        }
        val mfa = CheckBox(this).apply {
            setText(R.string.field_mfa)
            isChecked = existing?.mfa == true
        }
        val form = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(R.dimen.dialog_padding_horizontal), dp(R.dimen.space_l),
                dp(R.dimen.dialog_padding_horizontal), 0)
            addView(name)
            addView(host)
            addView(port)
            addView(user)
            addView(password)
            addView(showPassword)
            addView(TextView(this@ConnectionsActivity).apply {
                setText(R.string.field_key_section)
                setPadding(0, dp(R.dimen.space_l), 0, dp(R.dimen.space_xs))
            })
            addView(keyActions)
            addView(keyStatus)
            addView(key)
            addView(phrase)
            addView(showPhrase)
            addView(mfa)
        }
        AlertDialog.Builder(this)
            .setTitle(if (existing == null) getString(R.string.title_add_profile, id)
            else getString(R.string.title_edit_profile, id))
            .setView(ScrollView(this).apply { addView(form) })
            .setPositiveButton(R.string.action_save) { _, _ ->
                val h = host.text.toString().trim()
                val u = user.text.toString().trim()
                if (h.isEmpty() || u.isEmpty()) {
                    toast(getString(R.string.need_host_and_user))
                    return@setPositiveButton
                }
                val pwdIn = password.text.toString()
                val keyIn = key.text.toString().trim()
                val phraseIn = phrase.text.toString()
                if (keyIn.isNotEmpty() && !looksLikePem(keyIn)) {
                    toast(getString(R.string.key_suspect_saved))
                }
                val saved = ConnectionStore.Profile(
                    id = id,
                    name = name.text.toString().trim().ifEmpty { id },
                    host = h,
                    port = port.text.toString().toIntOrNull() ?: 22,
                    user = u,
                    password = when {
                        pwdIn.isNotEmpty() -> pwdIn
                        else -> existing?.password
                    },
                    privateKey = when {
                        keyIn.isNotEmpty() -> keyIn
                        else -> existing?.privateKey
                    },
                    passphrase = when {
                        phraseIn.isNotEmpty() -> phraseIn
                        keyIn.isNotEmpty() -> null
                        else -> existing?.passphrase
                    },
                    mfa = mfa.isChecked
                )
                ConnectionStore.save(this, saved)
                ConnectionHub.ensureSlots(this)
                renderList()
            }
            .setNegativeButton(R.string.action_cancel, null)
            .setOnDismissListener {
                pendingKeyField = null
                pendingKeyStatus = null
            }
            .show()
    }

    private fun updateKeyStatus(pem: String) {
        pendingKeyStatus?.setText(
            when {
                pem.contains("BEGIN OPENSSH PRIVATE KEY") -> R.string.key_loaded_openssh
                looksLikePem(pem) -> R.string.key_loaded_pem
                else -> R.string.key_loaded_unlike
            })
    }

    private fun readPemFromUri(uri: Uri): String {
        contentResolver.openInputStream(uri)?.use { ins ->
            return ins.bufferedReader(Charsets.UTF_8).readText()
        } ?: error("cannot open file")
    }

    private fun readClipboardText(): String? {
        val cm = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        val clip: ClipData = cm.primaryClip ?: return null
        if (clip.itemCount < 1) return null
        return clip.getItemAt(0).coerceToText(this)?.toString()
    }

    private fun setPasswordVisible(field: EditText, visible: Boolean) {
        val start = field.selectionStart
        val end = field.selectionEnd
        field.inputType = if (visible) {
            InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD
        } else {
            InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
        }
        field.setSelection(start.coerceAtLeast(0), end.coerceAtLeast(0))
    }

    private fun looksLikePem(text: String): Boolean {
        val t = text.uppercase()
        return t.contains("BEGIN") && t.contains("PRIVATE KEY") && t.contains("END")
    }

    private fun toast(msg: String) = Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()

    private companion object {
        const val MENU_CLEAR_HOST_KEYS = 1
        const val CHIP_TINT_ALPHA = 48
        const val FINGERPRINT_MAX_CHARS = 34
    }
}
