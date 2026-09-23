# JSch reads private-key algorithm info via reflection; keep its classes and members to avoid stripping
-keep class com.jcraft.jsch.** { *; }
-dontwarn com.jcraft.jsch.**

# Bouncy Castle (ed25519 / OpenSSH crypto on Android)
-keep class org.bouncycastle.** { *; }
-dontwarn org.bouncycastle.**
