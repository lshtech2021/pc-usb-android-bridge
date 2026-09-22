# USB Bridge — Deployment and Usage Guide

The PC client (Python + PyQt5) connects to the phone app (Android foreground service) over a USB cable and does three things:

1. **Bidirectional file transfer** (PC ↔ phone, 256KB chunks + SHA256 verification)
2. **Bidirectional text messaging**
3. **PC remote operation**: the phone acts as an SSH proxy to reach a remote server, letting the PC operate that server through the phone

See [prd.md](./prd.md) for design details and the protocol definition.

---

## 1. Directory Structure

```
app-demo-6/
├── prd.md                    Design document (protocol, architecture, acceptance criteria)
├── pc/                       PC-side client
│   ├── ui.py                 Entry point: PyQt5 UI (start it from this directory)
│   ├── client.py             Business logic: text/files/remote channels/heartbeat
│   ├── protocol.py           Frame encode/decode (byte order matches the Android side)
│   ├── transport.py          TCP send/receive
│   ├── adb_manager.py        adb device listing and port forwarding
│   ├── requirements.txt      Dependencies
│   └── downloads/            Files sent from the phone land here (created on first run)
└── android/                  Android project (open this directory in Android Studio)
    └── app/src/main/java/com/example/usbbridge/
        ├── MainActivity.kt   UI: start service / show Token / send text / send file
        ├── BridgeService.kt  Foreground service + session handling + file writing
        ├── FrameIO.kt        Protocol mirror implementation
        ├── RemoteSession.kt  JSch SSH proxy
        └── TofuHostKeys.kt   Host fingerprint store (TOFU)
```

---

## 2. Requirements

| Side | Requirement |
|---|---|
| PC | Windows / Linux / macOS; Python 3.8+; PyQt5; `adb` (Android platform-tools) must be on PATH |
| Phone | Android 7.0 (API 24) or later, Android 8.0+ recommended; "Developer options → USB debugging" enabled |
| Android build | Android Studio (bundles JDK 17); AGP 8.1.4 / Gradle 8.2 |

---

## 3. Deployment Steps

### 1. Build and install the phone app

**Open the `android/` directory** in Android Studio (not the repository root), wait for Gradle Sync to finish, then Run to the phone.

Command-line alternative (first run a Sync in Android Studio so the gradle wrapper is generated, or use a locally installed `gradle`):

```bash
cd android
gradlew assembleDebug                # Output: app/build/outputs/apk/debug/app-debug.apk
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

Grant notification permission (a dialog appears on first launch). Denying it still works; you just no longer get notification-bar alerts for "text from PC".

### 2. Prepare the PC side

```bash
cd pc
pip install -r requirements.txt      # Only installs PyQt5
```

If `python` is not available on the command line (Windows with only the py launcher), use:

```powershell
py ui.py
```

If `adb` is not on PATH, there are two options: add the platform-tools directory to PATH, or edit the line in [ui.py](./pc/ui.py) that constructs `Adb` to specify an absolute path:

```python
self.adb, self.client, self.ch = Adb(path=r"D:\platform-tools\adb.exe"), PhoneClient(), None
```

### 3. Start the phone service and connect

1. Open the app on the phone → tap "**Start USB Bridge service**" → the UI shows `Service started, listening on 127.0.0.1:9999` plus the **Token** (8 hex digits), which also appears in the notification bar.
2. Connect the phone to the PC over USB and confirm "Allow USB debugging" on the phone.
3. On the PC, confirm the device is visible:

```bash
adb devices          # Only "<serial>   device" means it is connectable (unauthorized means you have not tapped Allow on the phone yet)
```

4. Run `py ui.py` on the PC → tap "**Refresh Devices**" and select the phone → enter the **Token** shown on the phone → tap "**Connect**". The top status changes to `Connected <serial>` and the message panel shows `[Phone connected: <model>]`.

> On connect, the PC automatically runs `adb -s <serial> forward tcp:12580 tcp:9999` (after `--remove-all` to clear leftovers), mapping the PC's `127.0.0.1:12580` to the phone service's `127.0.0.1:9999`. Link encryption is provided by USB + adb; the protocol itself adds no extra encryption.

---

## 4. Usage

### Messages (Tab 1)

Type in the input box and press Enter to send; `[PC] xxx` is sent from this machine and `[Phone] xxx` came from the phone; when the phone receives PC text it raises a notification-bar alert.
To send text from the phone: type the content in the app → "Send text to PC" (**the PC must be connected first**, otherwise there is no receiver).

### Files (Tab 2)

- **PC → phone**: tap "Choose a file to send to phone..."; the table shows progress in one row per file, and multiple files can be sent at once.
- **Phone → PC**: tap "Send file to PC" in the app and choose a file; the PC saves it under `pc/downloads/` and the table shows `✓ Done -> full path`.
- Duplicate names are never overwritten: both sides increment through `name(1).ext`, `name(2).ext`.
- Both sides verify SHA256; on mismatch the written file is deleted and marked as failed.

### Remote Terminal (Tab 3)

1. Fill in the target server `IP / port / username / password`; to use a key, check "Use private key" and choose the key file (a key passphrase may also be entered).
2. "**SSH via phone (shell)**": opens an interactive shell; pressing Enter in the bottom input box sends the whole line to the remote end, and output streams back in real time.
3. "**Run exec once**": enter a one-shot command in the box on the right (e.g. `uname -a`); when it finishes, stdout/stderr and the exit code come back and the channel closes automatically.
4. "**Disconnect**": closes the current remote channel (closes only SSH, not the USB connection).

The first time you connect to a given target server, a dialog shows that host's `SHA256:...` fingerprint; verify it with the server administrator and then choose "Trust and continue". Once confirmed it is written to the phone's known_hosts and you are not asked again.
If the fingerprint **does not match** the record, the connection is refused immediately with a red warning (possible man-in-the-middle attack) and there is no "ignore" option.

You can cross-check the fingerprint on the target server with `ssh-keygen -lf /etc/ssh/ssh_host_ecdsa_key.pub`.

### Disconnecting and Exiting

- Tapping "**Disconnect**" on the PC closes the connection and removes the `adb forward`; files the PC had not finished sending are deleted.
- Swiping the app away or stopping the service on the phone releases the listening port.
- There is no automatic reconnect; tap "Connect" again when needed.

---

## 5. Data and File Locations

| Content | Location |
|---|---|
| Files the PC received from the phone | `<directory where ui.py was started>/downloads/` |
| Files the PC sends to the phone | `Android/data/com.example.usbbridge/files/` (app external private dir, no storage permission needed) |
| Text received by the phone | Notification bar + in-app UI notice |
| Phone known_hosts (host fingerprints) | `/data/data/com.example.usbbridge/files/known_hosts` (not visible without root; clearing app data resets it) |
| SSH passwords / private-key contents | Passed through memory only, never written to disk |

> To "re-confirm a host's fingerprint", go to the phone's "Settings → Apps → USB Bridge → Storage → Clear data", or uninstall and reinstall.

---

## 6. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Refresh Devices` list is empty | USB debugging off / "Allow" not tapped on the phone / cable only charges and does not transfer data; run `adb devices` on the command line to check the state |
| Shows `[adb unavailable]` | `adb` is not on PATH; specify an absolute path as described in "Prepare the PC side" |
| The connection drops immediately and `BAD_TOKEN` appears in the message panel | Wrong Token, or the phone service was restarted (the Token is regenerated); use whatever the phone UI currently shows |
| Port-in-use / forward failure | Port 12580 is taken, or a previous forward is lingering; disconnect and reconnect (a `--remove-all` runs before connecting) |
| Tapping "Start service" on the phone does nothing | Notification permission / foreground service is restricted by the system; allow notifications in settings, or disable battery optimization and retry |
| PC text to the phone gives no notification | The phone denied notification permission; the content is still recorded in the app UI |
| `HOST_UNREACHABLE` | Wrong target server address/port, or the phone's current network cannot reach that machine (the phone needs internet access and must be able to reach the host) |
| `AUTH_FAILED` | Wrong username/password/private key. **JSch 0.1.55 does not support the newer OpenSSH key format** (`-----BEGIN OPENSSH PRIVATE KEY-----`) or ed25519 keys; convert to PEM/PKCS#8: `ssh-keygen -p -m PEM -f id_rsa` |
| `UNKNOWN_HOST` dialog | The normal flow for a first connection to that host; verify the fingerprint and then choose to trust |
| `HOST_KEY_CHANGED` | The target server was reinstalled or its host key changed, or there really is a man-in-the-middle; **find out the cause first**, then clear the phone's known_hosts and reconnect |
| `Algorithm negotiation fail` | JSch 0.1.55 supports only `ssh-rsa(SHA-1)`, `ssh-dss`, and `ecdsa-sha2-*` host key algorithms. If the target server offers only ed25519 or has ssh-rsa disabled per the OpenSSH 8.8+ default, negotiation fails. Two ways out: ① in `app/build.gradle` switch to the community fork `implementation 'com.github.mwiede:jsch:0.2.17'` (API-compatible, supports rsa-sha2/ed25519); ② add `HostKeyAlgorithms +ssh-rsa` and `PubkeyAcceptedAlgorithms +ssh-rsa` to the target server's `sshd_config` and restart sshd |
| Text messages lag during large file transfers | Chunked file writes within a single connection are synchronous, a known limitation (nothing is lost; see prd §8); avoid chatting while sending large files |

---

## 7. Known Limitations

- The remote terminal is **line mode** (type a whole line + Enter), suitable for running commands; full-screen interactive programs such as `vim`/`htop` are not yet usable (no raw keystroke passthrough or ANSI terminal emulation).
- No resume-after-interruption, no parallel multi-device support, no directory browsing; the PC connects to only one device at a time.
- Within a connection, file transfer and text share one TCP stream, so text lags while a large file is in flight.
- An adb environment is required (an adb-free approach would need root or a custom ROM).

See [prd.md](./prd.md) §7 and §8 for more extensibility points and security boundaries.
