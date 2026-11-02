package com.example.usbbridge

import java.util.Locale

/** Classifies keyboard-interactive prompts by their text.
 *
 *  PAM localises its prompts ("密码：" instead of "Password: "), and some servers
 *  combine the password and the one-time code in one prompt. Getting this wrong
 *  sends the authenticator code where the password is expected and the server
 *  restarts the whole challenge — the prompt then looks like it "reappears".
 */
internal object AuthPromptText {
    private val PASSWORD_WORDS = listOf(
        "password", "passphrase", "passwort", "mot de passe", "contraseña", "пароль",
        "密码", "密碼"
    )

    private val CODE_WORDS = listOf(
        "verification", "one-time", "one time", "otp", "authenticator", "passcode",
        "token", "enter code", "2fa", "two-factor", "two factor",
        "code:", "code：", "验证码", "驗證碼", "动态码", "動態碼",
        "动态密码", "動態密碼", "一次性密码", "动态口令", "動態口令", "令牌"
    )

    fun isCode(text: String): Boolean {
        val t = text.lowercase(Locale.ROOT)
        return CODE_WORDS.any { t.contains(it) }
    }

    fun isPassword(text: String): Boolean {
        val t = text.lowercase(Locale.ROOT)
        // A combined "password + verification code" prompt needs the code the
        // user is reading, not the stored password.
        if (isCode(t)) return false
        // Password-change prompts (JSch's own "New Password: ") must not be
        // answered with the current password.
        if (t.contains("new password") || t.contains("new passphrase") ||
            t.contains("新密码") || t.contains("新密碼")) return false
        return PASSWORD_WORDS.any { t.contains(it) }
    }
}
