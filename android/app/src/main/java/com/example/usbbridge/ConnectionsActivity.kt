package com.example.usbbridge

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.net.Uri
import android.os.Bundle
import android.text.InputType
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity

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
                    toast("File is empty")
                    return@onSuccess
                }
                field.setText(pem)
                updateKeyStatus(pem)
                when {
                    !looksLikePem(pem) ->
                        toast("Loaded file — does not look like a private key")
                    pem.contains("BEGIN OPENSSH PRIVATE KEY") ->
                        toast("OpenSSH private key loaded")
                    else ->
                        toast("PEM private key loaded")
                }
            }
            .onFailure { e -> toast("Failed to read file: ${e.message}") }
    }

    override fun onCreate(s: Bundle?) {
        super.onCreate(s)
        AuthPrompts.bind(this)
        ConnectionHub.ensureSlots(this)

        listBox = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        val add = Button(this).apply {
            text = "Add connection"
            setOnClickListener { editDialog(null) }
        }
        val clearHosts = Button(this).apply {
            text = "Clear all host keys"
            setOnClickListener { confirmClearAllHostKeys() }
        }
        val scroll = ScrollView(this).apply {
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f)
            addView(listBox)
        }
        setContentView(LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(32, 32, 32, 32)
            addView(TextView(this@ConnectionsActivity).apply {
                text = "SSH Connections"
                textSize = 20f
                setPadding(0, 0, 0, 16)
            })
            addView(add)
            addView(clearHosts)
            addView(scroll)
        })
        renderList()
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

    private fun renderList() {
        ConnectionHub.ensureSlots(this)
        listBox.removeAllViews()
        val profiles = ConnectionStore.list(this)
        if (profiles.isEmpty()) {
            listBox.addView(TextView(this).apply {
                text = "No connections yet. Tap Add connection."
                setPadding(0, 24, 0, 0)
            })
            return
        }
        profiles.forEach { p ->
            val state = ConnectionHub.stateOf(p.id)
            val err = ConnectionHub.lastError(p.id)
            val authHint = when {
                !p.privateKey.isNullOrBlank() && !p.password.isNullOrBlank() -> "key+password"
                !p.privateKey.isNullOrBlank() -> "private key"
                !p.password.isNullOrBlank() -> "password"
                else -> "no auth secret"
            }
            val row = LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                setPadding(0, 16, 0, 8)
            }
            row.addView(TextView(this).apply {
                text = "${p.id}  ${p.name}\n${p.user}@${p.host}:${p.port}\n" +
                    "Auth: $authHint · State: $state" +
                    hostKeyHint(p) +
                    if (!err.isNullOrBlank()) "\nError: $err" else ""
                textSize = 15f
            })
            val actions = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
            fun btn(label: String, enabled: Boolean = true, click: () -> Unit) =
                Button(this).apply {
                    text = label
                    isEnabled = enabled
                    setOnClickListener { click() }
                }.also { actions.addView(it) }
            btn("Start", state != ConnectionHub.STATE_RUNNING && state != ConnectionHub.STATE_STARTING) {
                ConnectionHub.start(this, p.id)
            }
            btn("Stop", state == ConnectionHub.STATE_RUNNING || state == ConnectionHub.STATE_STARTING) {
                ConnectionHub.stop(p.id)
            }
            btn("Edit") { editDialog(p) }
            btn("Forget host key") { confirmForgetHostKey(p) }
            btn("Delete") {
                AlertDialog.Builder(this)
                    .setMessage("Delete ${p.id}?")
                    .setPositiveButton("Delete") { _, _ ->
                        ConnectionHub.stop(p.id)
                        ConnectionStore.delete(this, p.id)
                        renderList()
                    }
                    .setNegativeButton("Cancel", null)
                    .show()
            }
            row.addView(actions)
            listBox.addView(row)
        }
    }

    private fun hostKeyHint(p: ConnectionStore.Profile): String {
        val fp = TofuHostKeys.fingerprintOf(TofuHostKeys.storeFile(this), p.host, p.port)
        return if (fp != null) "\nHost key: ${fp.take(20)}…" else "\nHost key: (none trusted yet)"
    }

    private fun confirmForgetHostKey(p: ConnectionStore.Profile) {
        val store = TofuHostKeys.storeFile(this)
        val fp = TofuHostKeys.fingerprintOf(store, p.host, p.port)
        if (fp == null) {
            toast("No trusted host key for ${p.host}")
            return
        }
        AlertDialog.Builder(this)
            .setTitle("Forget host key?")
            .setMessage(
                "Remove trusted key for ${p.host}:${p.port}?\n\n$fp\n\n" +
                    "Next Start will ask you to Trust the fingerprint again.")
            .setPositiveButton("Forget") { _, _ ->
                val n = TofuHostKeys.forgetHost(store, p.host, p.port)
                toast(if (n > 0) "Forgot $n host key entr${if (n == 1) "y" else "ies"}" else "Nothing to forget")
                renderList()
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun confirmClearAllHostKeys() {
        val store = TofuHostKeys.storeFile(this)
        val n = TofuHostKeys.entryCount(store)
        if (n == 0) {
            toast("No host keys stored")
            return
        }
        AlertDialog.Builder(this)
            .setTitle("Clear all host keys?")
            .setMessage(
                "Remove all $n trusted SSH host fingerprint(s)?\n\n" +
                    "Connection profiles are kept. Next Start for each host will ask to Trust again.")
            .setPositiveButton("Clear all") { _, _ ->
                TofuHostKeys.clearAll(store)
                toast("All host keys cleared")
                renderList()
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun editDialog(existing: ConnectionStore.Profile?) {
        val id = existing?.id ?: ConnectionStore.nextId(this)
        val name = EditText(this).apply {
            hint = "Display name"; setText(existing?.name ?: id)
        }
        val host = EditText(this).apply {
            hint = "Host"; setText(existing?.host ?: "")
        }
        val port = EditText(this).apply {
            hint = "Port"; setText((existing?.port ?: 22).toString())
            inputType = InputType.TYPE_CLASS_NUMBER
        }
        val user = EditText(this).apply {
            hint = "Username"; setText(existing?.user ?: "")
        }
        val password = EditText(this).apply {
            hint = if (existing?.password != null) "Password (leave blank to keep)" else "Password"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
        }
        val showPassword = CheckBox(this).apply {
            text = "Show password"
            setOnCheckedChangeListener { _, checked ->
                setPasswordVisible(password, checked)
            }
        }
        val key = EditText(this).apply {
            hint = if (existing?.privateKey != null) {
                "Private key (blank = keep existing)"
            } else {
                "Private key: PEM or OpenSSH (paste or pick file)"
            }
            minLines = 4
            inputType = InputType.TYPE_CLASS_TEXT or
                InputType.TYPE_TEXT_FLAG_MULTI_LINE or
                InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
        }
        val keyStatus = TextView(this).apply {
            textSize = 13f
            setPadding(0, 4, 0, 8)
            text = when {
                existing?.privateKey != null ->
                    "Saved key on file (${existing.privateKey.length} chars). Paste/pick to replace."
                else -> "No key loaded. Pick a key file or paste PEM / OpenSSH text."
            }
        }
        pendingKeyField = key
        pendingKeyStatus = keyStatus

        val keyActions = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        keyActions.addView(Button(this).apply {
            text = "Pick key file"
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
            text = "Paste key"
            setOnClickListener {
                val clip = readClipboardText()
                if (clip.isNullOrBlank()) {
                    toast("Clipboard is empty")
                    return@setOnClickListener
                }
                key.setText(clip)
                updateKeyStatus(clip)
                when {
                    clip.contains("BEGIN OPENSSH PRIVATE KEY") ->
                        toast("OpenSSH private key pasted")
                    looksLikePem(clip) ->
                        toast("PEM private key pasted")
                    else ->
                        toast("Pasted text does not look like a private key")
                }
            }
        })
        keyActions.addView(Button(this).apply {
            text = "Clear"
            setOnClickListener {
                key.text.clear()
                keyStatus.text = if (existing?.privateKey != null) {
                    "Cleared editor — Save still keeps existing key unless you paste a new one"
                } else {
                    "No key loaded"
                }
            }
        })

        val phrase = EditText(this).apply {
            hint = "Key passphrase (optional)"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
        }
        val showPhrase = CheckBox(this).apply {
            text = "Show passphrase"
            setOnCheckedChangeListener { _, checked ->
                setPasswordVisible(phrase, checked)
            }
        }
        val mfa = CheckBox(this).apply {
            text = "MFA (Google Authenticator / keyboard-interactive)"
            isChecked = existing?.mfa == true
        }
        val form = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(40, 16, 40, 0)
            addView(name)
            addView(host)
            addView(port)
            addView(user)
            addView(password)
            addView(showPassword)
            addView(TextView(this@ConnectionsActivity).apply {
                text = "Private key (PEM or OpenSSH)"
                setPadding(0, 16, 0, 4)
            })
            addView(keyActions)
            addView(keyStatus)
            addView(key)
            addView(phrase)
            addView(showPhrase)
            addView(mfa)
        }
        AlertDialog.Builder(this)
            .setTitle(if (existing == null) "Add $id" else "Edit $id")
            .setView(ScrollView(this).apply { addView(form) })
            .setPositiveButton("Save") { _, _ ->
                val h = host.text.toString().trim()
                val u = user.text.toString().trim()
                if (h.isEmpty() || u.isEmpty()) {
                    toast("Host and user required")
                    return@setPositiveButton
                }
                val pwdIn = password.text.toString()
                val keyIn = key.text.toString().trim()
                val phraseIn = phrase.text.toString()
                if (keyIn.isNotEmpty() && !looksLikePem(keyIn)) {
                    toast("Warning: key does not look like PEM/OpenSSH — saved anyway")
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
            .setNegativeButton("Cancel", null)
            .setOnDismissListener {
                pendingKeyField = null
                pendingKeyStatus = null
            }
            .show()
    }

    private fun updateKeyStatus(pem: String) {
        pendingKeyStatus?.text = when {
            pem.contains("BEGIN OPENSSH PRIVATE KEY") ->
                "Loaded OpenSSH key (${pem.length} chars)"
            looksLikePem(pem) ->
                "Loaded PEM key (${pem.length} chars)"
            else ->
                "Loaded text (${pem.length} chars) — may not be a private key"
        }
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

    private fun toast(msg: String) =
        Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()
}
