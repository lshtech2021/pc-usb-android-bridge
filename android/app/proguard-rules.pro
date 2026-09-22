# JSch reads private-key algorithm info via reflection; keep its classes and members to avoid stripping
-keep class com.jcraft.jsch.** { *; }
-dontwarn com.jcraft.jsch.**
