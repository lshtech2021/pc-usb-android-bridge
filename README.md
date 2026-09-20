# USB Bridge —— 部署与使用说明

PC 端（Python + PyQt5）通过 USB 线连接手机端 App（Android 前台服务），实现三件事：

1. **双向传文件**（PC ↔ 手机，256KB 分块 + SHA256 校验）
2. **双向发文本**
3. **PC 远程操作**：手机作 SSH 代理，去连一台远程服务器，PC 借手机操作该服务器

设计细节与协议定义见 [prd.md](./prd.md)。

---

## 一、目录结构

```
app-demo-6/
├── prd.md                    设计文档（协议、架构、验收标准）
├── pc/                       PC 端客户端
│   ├── ui.py                 入口：PyQt5 界面（在此目录启动）
│   ├── client.py             业务逻辑：文本/文件/远程通道/心跳
│   ├── protocol.py           帧编解码（与 Android 端字节序一致）
│   ├── transport.py          TCP 收发
│   ├── adb_manager.py        adb 设备列表与端口转发
│   ├── requirements.txt      依赖
│   └── downloads/            手机发来的文件落在这里（首次运行自动创建）
└── android/                  Android 工程（Android Studio 打开此目录）
    └── app/src/main/java/com/example/usbbridge/
        ├── MainActivity.kt   界面：启动服务 / 显示 Token / 发文本 / 发文件
        ├── BridgeService.kt  前台服务 + 会话处理 + 文件落盘
        ├── FrameIO.kt        协议镜像实现
        ├── RemoteSession.kt  JSch SSH 代理
        └── TofuHostKeys.kt   主机指纹库（TOFU）
```

---

## 二、环境要求

| 端 | 要求 |
|---|---|
| PC | Windows / Linux / macOS；Python 3.8+；PyQt5；`adb`（Android platform-tools）需在 PATH 中 |
| 手机 | Android 7.0（API 24）及以上，推荐 Android 8.0+；已开启「开发者选项 → USB 调试」 |
| 构建 Android | Android Studio（自带 JDK 17）；AGP 8.1.4 / Gradle 8.2 |

---

## 三、部署步骤

### 1. 构建并安装手机 App

用 Android Studio **打开 `android/` 目录**（不是仓库根目录），等待 Gradle Sync 完成后 Run 到手机。

命令行方式（需先 Sync 一次让 Android Studio 生成 gradle wrapper，或用本机已安装的 `gradle`）：

```bash
cd android
gradlew assembleDebug                # 产物：app/build/outputs/apk/debug/app-debug.apk
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

授予通知权限（首次启动会弹窗）。拒绝也能用，只是「PC 发来文本」不再有通知栏提醒。

### 2. 准备 PC 端

```bash
cd pc
pip install -r requirements.txt      # 只装 PyQt5
```

若命令行没有 `python`（Windows 只装了 py 启动器），用：

```powershell
py ui.py
```

`adb` 不在 PATH 时，有两种办法：把 platform-tools 目录加进 PATH，或改 [ui.py](./pc/ui.py) 里构造 `Adb` 的那行，指定绝对路径：

```python
self.adb, self.client, self.ch = Adb(path=r"D:\platform-tools\adb.exe"), PhoneClient(), None
```

### 3. 启动手机服务并连接

1. 手机打开 App → 点「**启动 USB Bridge 服务**」→ 界面显示 `服务已启动，监听 127.0.0.1:9999` 与 **Token**（8 位十六进制），通知栏同样显示。
2. USB 连接手机与 PC，手机上确认「允许 USB 调试」。
3. PC 上确认设备可见：

```bash
adb devices          # 看到 "<serial>   device" 才算可连（unauthorized 表示还没在手机上点允许）
```

4. PC 运行 `py ui.py` → 点「**刷新设备**」选中手机 → 填入手机上的 **Token** → 点「**连接**」。顶部状态变为 `已连接 <serial>`，消息面板出现 `[已连接手机: <型号>]`。

> 连接时 PC 会自动执行 `adb -s <serial> forward tcp:12580 tcp:9999`（先 `--remove-all` 清理残留），把 PC 的 `127.0.0.1:12580` 映射到手机服务的 `127.0.0.1:9999`。链路加密由 USB + adb 提供，协议本身不额外加密。

---

## 四、使用说明

### 消息（Tab 1）

输入框打字回车发送，`[PC] xxx` 是本机发出，`[手机] xxx` 是手机发来；手机收到 PC 文本会出通知栏提醒。
手机端发文本：App 里输入内容 → 「发送文本到 PC」（**需要 PC 先连接成功**，否则没有接收方）。

### 文件（Tab 2）

- **PC → 手机**：点「选择文件发送到手机…」，表格按文件分行显示进度，可同时发多个。
- **手机 → PC**：App 里点「发送文件到 PC」选文件；PC 收到后落在 `pc/downloads/`，表格显示 `✓ 完成 → 完整路径`。
- 重名不覆盖：两端都按 `name(1).ext`、`name(2).ext` 递增。
- 两端都做 SHA256 校验，不一致则删除落盘文件并标记失败。

### 远程终端（Tab 3）

1. 填目标服务器 `IP / 端口 / 用户名 / 密码`；用密钥则勾「使用私钥」并选私钥文件（可填私钥口令）。
2. 「**经手机 SSH 连接(shell)**」：建立交互式 shell，底部输入框回车把整行命令送到远端，输出实时回流。
3. 「**exec 执行一次**」：右侧输入框填一次性命令（如 `uname -a`），执行完回传 stdout/stderr 与退出码，通道自动关闭。
4. 「**断开**」：关闭当前远程通道（只断 SSH，不断 USB 连接）。

首次连某台目标服务器时，会弹窗展示该主机的 `SHA256:...` 指纹，请你与服务器管理员核对后选择「信任并继续」；确认后写入手机的 known_hosts，下次不再询问。
若指纹与记录**不一致**，直接红色告警并拒绝连接（疑似中间人攻击），不提供「忽略」入口。

可用 `ssh-keygen -lf /etc/ssh/ssh_host_ecdsa_key.pub` 在目标服务器上核对指纹。

### 断开与退出

- PC 点「**断开**」：关闭连接并移除 `adb forward`；PC 端未传完的文件会被删除。
- 手机上划掉 App 或停止服务：释放监听端口。
- 不自动重连，需要时重新点「连接」。

---

## 五、数据与文件位置

| 内容 | 位置 |
|---|---|
| PC 收到手机的文件 | `<启动 ui.py 时的目录>/downloads/` |
| PC 发往手机的文件 | `Android/data/com.example.usbbridge/files/`（App 外部私有目录，免存储权限） |
| 手机收到的文本 | 通知栏 + App 界面提示 |
| 手机 known_hosts（主机指纹） | `/data/data/com.example.usbbridge/files/known_hosts`（非 root 不可见，清除应用数据即重置） |
| SSH 密码 / 私钥内容 | 仅内存传递，不写文件 |

> 若需「重新确认某主机的指纹」，在手机「设置 → 应用 → USB Bridge → 存储 → 清除数据」，或卸载重装。

---

## 六、常见问题

| 现象 | 原因与处理 |
|---|---|
| `刷新设备` 列表为空 | 未开 USB 调试 / 未在手机上点「允许」/ 数据线只充电不传数据；先在命令行跑 `adb devices` 看状态 |
| 提示 `[adb 不可用]` | `adb` 不在 PATH；按“准备 PC 端”一节指定绝对路径 |
| 连接后立刻断开，消息面板出现 `BAD_TOKEN` | Token 填错，或手机服务重启过（Token 会重新生成）；以手机界面当前显示为准 |
| 提示端口占用 / forward 失败 | 12580 被占用，或上一次 forward 残留；断开后重连即可（连接前会自动 `--remove-all`） |
| 手机点了「启动服务」没反应 | 通知权限/前台服务被系统限制；到设置里允许通知，或关掉电池优化后重试 |
| PC 发文本到手机，手机没提醒 | 手机拒绝了通知权限；内容仍会记在 App 界面 |
| `HOST_UNREACHABLE` | 目标服务器地址/端口不对，或手机当前网络到不了那台机器（手机需能上网且能访问该主机） |
| `AUTH_FAILED` | 用户名/密码/私钥不对。**JSch 0.1.55 不支持新版 OpenSSH 私钥格式**（`-----BEGIN OPENSSH PRIVATE KEY-----`）与 ed25519 私钥，需要转成 PEM/PKCS#8：`ssh-keygen -p -m PEM -f id_rsa` |
| `UNKNOWN_HOST` 弹窗 | 首次连接该主机的正常流程，核对指纹后选择信任 |
| `HOST_KEY_CHANGED` | 目标服务器重装/换过主机密钥，或真有中间人；**先查清原因**再清除手机 known_hosts 重连 |
| `Algorithm negotiation fail` | JSch 0.1.55 仅支持 `ssh-rsa(SHA-1)`、`ssh-dss`、`ecdsa-sha2-*` 主机密钥算法。若目标服务器只提供 ed25519 或已按 OpenSSH 8.8+ 默认禁用 ssh-rsa，就会协商失败。两条出路：① `app/build.gradle` 换成社区分支 `implementation 'com.github.mwiede:jsch:0.2.17'`（接口兼容，支持 rsa-sha2/ed25519）；② 目标服务器 `sshd_config` 加 `HostKeyAlgorithms +ssh-rsa`、`PubkeyAcceptedAlgorithms +ssh-rsa` 后重启 sshd |
| 大文件传输时文本消息延迟 | 单连接内文件分块写入是同步的，属已知限制（不会丢，见 prd §8）；发大文件时避免同时聊天 |

---

## 七、已知限制

- 远程终端是**行模式**（输入整行 + 回车），适合执行命令；`vim`/`htop` 这类全屏交互程序尚不可用（缺按键直传与 ANSI 终端仿真）。
- 不支持断点续传、多设备并行、目录浏览；PC 一次只连一台设备。
- 连接内文件传输与文本共用一条 TCP，大文件期间文本有延迟。
- 需 adb 环境（免 adb 方案需 root 或定制 ROM）。

更多可扩展点与安全边界见 [prd.md](./prd.md) §7、§8。
