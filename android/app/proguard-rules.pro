# JSch 使用反射读取私钥算法信息，保留其类与成员避免被裁剪
-keep class com.jcraft.jsch.** { *; }
-dontwarn com.jcraft.jsch.**
