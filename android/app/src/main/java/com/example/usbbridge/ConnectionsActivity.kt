package com.example.usbbridge

import android.os.Bundle
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity

/** CRUD + Start/Stop for phone-managed SSH connection profiles. */
class ConnectionsActivity : AppCompatActivity() {
    private lateinit var listBox: LinearLayout
    private val refreshListener: () -> Unit = { runOnUiThread { renderList() } }

    override fun onCreate(s: Bundle?) {
        super.onCreate(s)
        AuthPrompts.bind(this)
        ConnectionHub.ensureSlots(this)

        listBox = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        val add = Button(this).apply {
            text = "Add connection"
            setOnClickListener { editDialog(null) }
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
            val row = LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                setPadding(0, 16, 0, 8)
            }
            row.addView(TextView(this).apply {
                text = "${p.id}  ${p.name}\n${p.user}@${p.host}:${p.port}\nState: $state" +
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

    private fun editDialog(existing: ConnectionStore.Profile?) {
        val id = existing?.id ?: ConnectionStore.nextId(this)
        val name = EditText(this).apply {
            hint = "Display name"; setText(existing?.name ?: id)
        }
        val host = EditText(this).apply {
            hint = "Host"; setText(existing?.host ?: "")
        }
        val port = EditText(this).apply {
            hint = "Port"; setText((existing?.port ?: 22).toString()); inputType =
                android.text.InputType.TYPE_CLASS_NUMBER
        }
        val user = EditText(this).apply {
            hint = "Username"; setText(existing?.user ?: "")
        }
        val password = EditText(this).apply {
            hint = if (existing?.password != null) "Password (leave blank to keep)" else "Password"
            inputType = android.text.InputType.TYPE_CLASS_TEXT or
                android.text.InputType.TYPE_TEXT_VARIATION_PASSWORD
        }
        val key = EditText(this).apply {
            hint = if (existing?.privateKey != null) "Private key PEM (blank = keep)" else "Private key PEM (optional)"
            minLines = 3
        }
        val phrase = EditText(this).apply {
            hint = "Key passphrase (optional)"
            inputType = android.text.InputType.TYPE_CLASS_TEXT or
                android.text.InputType.TYPE_TEXT_VARIATION_PASSWORD
        }
        val mfa = CheckBox(this).apply {
            text = "MFA (Google Authenticator / keyboard-interactive)"
            isChecked = existing?.mfa == true
        }
        val form = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(40, 16, 40, 0)
            listOf(name, host, port, user, password, key, phrase, mfa).forEach { addView(it) }
        }
        AlertDialog.Builder(this)
            .setTitle(if (existing == null) "Add $id" else "Edit $id")
            .setView(ScrollView(this).apply { addView(form) })
            .setPositiveButton("Save") { _, _ ->
                val h = host.text.toString().trim()
                val u = user.text.toString().trim()
                if (h.isEmpty() || u.isEmpty()) {
                    Toast.makeText(this, "Host and user required", Toast.LENGTH_SHORT).show()
                    return@setPositiveButton
                }
                val pwdIn = password.text.toString()
                val keyIn = key.text.toString()
                val phraseIn = phrase.text.toString()
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
            .show()
    }
}
