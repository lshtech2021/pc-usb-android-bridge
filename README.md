# USB Bridge — Deployment and Usage Guide

The PC client (Python + PyQt5) connects to the phone app (Android foreground service) over a USB cable and does three things:

1. **Bidirectional file transfer** (PC ↔ phone, 256KB chunks + SHA256 verification)
2. **Bidirectional text messaging**
3. **Remote SSH via phone-managed connections**: create/start SSH profiles on the phone (secrets stay on the phone); the PC attaches by connection ID (`ID_01`…) to an interactive terminal

See [prd.md](./prd.md) for design details and the protocol definition.

---

## 1. Directory Structure

```
app-demo-6/
├── prd.md                    Design document (protocol, architecture, acceptance criteria)
├── pc/                       PC-side client
│   ├── ui.py                 Entry point: PyQt5 UI (start it from this directory)
│   ├── client.py             Business logic: text/files/conn attach/heartbeat
│   ├── terminal.py           Interactive pyte terminal widget
│   ├── identity.py           Persistent PC keypair (fingerprint; private key never shown)
│   ├── link_crypto.py        P-256 ECDH + HKDF + AES-GCM frame sealing
│   ├── protocol.py           Frame encode/decode (byte order matches the Android side)
│   ├── transport.py          TCP send/receive (+ post-HELLO seal)
│   ├── adb_manager.py        adb device listing and port forwarding
│   ├── requirements.txt      Dependencies (PyQt5, pyte, cryptography)
│   └── downloads/            Files sent from the phone land here (created on first run)
└── android/                  Android project (open this directory in Android Studio)
    └── app/src/main/java/com/example/usbbridge/
        ├── MainActivity.kt        UI: service / Token / Trusted PCs / files / text
        ├── ConnectionsActivity.kt SSH profile CRUD + Start/Stop + MFA prompts
        ├── ConnectionStore.kt     Encrypted profile storage (Keystore)
        ├── ConnectionHub.kt       Long-lived SSH sessions (survive USB disconnect)
        ├── PcTrustStore.kt        Trusted PC fingerprints (TOFU)
        ├── LinkCrypto.kt          ECDH + AES-GCM (mirrors pc/link_crypto.py)
        ├── BridgeService.kt       Foreground service + USB session + file writing
        ├── FrameIO.kt             Protocol mirror implementation
        ├── RemoteSession.kt       JSch SSH (+ keyboard-interactive MFA)
        └── TofuHostKeys.kt        SSH host fingerprint store (TOFU)
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
pip install -r requirements.txt      # PyQt5 + pyte + cryptography
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

4. Run `py ui.py` on the PC → tap "**Refresh Devices**" and select the phone → enter the **Token** (masked field) shown on the phone → tap "**Connect**".
5. On **first** connect from this PC, the phone shows an **Approve PC** dialog with the PC name and fingerprint — tap Trust. Later connects with the same fingerprint are automatic.
6. After HELLO/ACK, the link is **AES-GCM sealed**. Status becomes `Connected <serial> (encrypted)`.

> On connect, the PC runs `adb -s <serial> forward tcp:12580 tcp:9999` (after `--remove-all`), mapping PC `127.0.0.1:12580` → phone `127.0.0.1:9999`. HELLO is cleartext (token + PC pubkey); all later frames are encrypted. Only **one** live session per PC fingerprint (`PC_ALREADY_CONNECTED` if a second client retries).

**Threat limits:** Encryption + PC TOFU harden against other **local** processes on the PC that can open the forwarded port. They do **not** protect against a fully administered/compromised Windows host/VM, malware with admin rights, or OS-level screen recording. Company LAN users cannot MITM this USB/loopback path as designed.

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

### Remote Terminal (Tab 3) — attach by phone connection ID

SSH secrets never leave the phone. Workflow:

1. On the phone: **SSH Connections** → Add a profile (`ID_01`…) with host/user/password or private key. Check **MFA** if the server uses Google Authenticator / keyboard-interactive.
2. Tap **Start** on that profile. First-time host keys: confirm the fingerprint dialog on the phone. MFA codes are entered on the phone when prompted.
3. On the PC (USB connected): Remote Terminal → **Refresh list** → select a **running** ID → **Attach**.
4. Use the interactive terminal (raw keys, ANSI via pyte — suitable for shell scripts, `vim`/`nano`, etc.). **Detach** leaves the phone SSH session running; **Stop** on the phone ends it.

USB disconnect does **not** stop phone SSH sessions. PC never receives passwords or private keys.

### Disconnecting and Exiting

- Tapping "**Disconnect**" on the PC closes the USB bridge and removes the `adb forward`; half-written files the PC was **receiving** under `./downloads/` are deleted. Phone SSH sessions keep running until you **Stop** them in SSH Connections.
- Swiping the app away or stopping the bridge service on the phone releases the listening port (and stops hub sessions when the process dies).
- There is no automatic reconnect; tap "Connect" again when needed.

---

## 5. Data and File Locations

| Content | Location |
|---|---|
| Files the PC received from the phone | `<directory where ui.py was started>/downloads/` |
| Files the PC sends to the phone | User-chosen folder via **Choose save folder** (persisted SAF access), or app default `Android/data/com.example.usbbridge/files/` |
| Text received by the phone | Notification bar + in-app UI notice |
| Phone known_hosts (host fingerprints) | `/data/data/com.example.usbbridge/files/known_hosts` (not visible without root; clearing app data resets it) |
| SSH connection profiles (encrypted) | Android EncryptedSharedPreferences (Keystore); passwords/keys never sent to the PC |
| Trusted PC fingerprints | Phone EncryptedSharedPreferences (`Trusted PCs` to list/forget) |
| PC identity (keypair) | `~/.usbbridge/identity.json` (or `%USERPROFILE%\.usbbridge\`); private key never shown in UI |
| SSH passwords / private-key contents | Stored encrypted on the phone only; entered/used on phone Start (incl. MFA) |

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
| `BAD_TOKEN` | Wrong Token, or the phone service was restarted (Token regenerates) |
| `PC_REJECTED` | User denied the PC on the phone Approve dialog |
| `PC_ALREADY_CONNECTED` | Another session with the same PC fingerprint is already connected |
| `PC_KEY_CHANGED` | Saved PC fingerprint’s pubkey changed — Forget that PC on the phone and re-approve |
| `CONN_NOT_RUNNING` / `CONN_BUSY` | Start the connection on the phone first; only one PC attach per ID |
| `AUTH_FAILED` | Wrong username/password/private key or MFA code on the phone. **JSch 0.1.55 does not support the newer OpenSSH key format** (`-----BEGIN OPENSSH PRIVATE KEY-----`) or ed25519 keys; convert to PEM/PKCS#8: `ssh-keygen -p -m PEM -f id_rsa` |
| Host trust dialog on phone | First connection to that host; verify the fingerprint, then Trust |
| `HOST_KEY_CHANGED` | Host key changed or possible MITM; clear phone known_hosts after investigating |
| `Algorithm negotiation fail` | JSch 0.1.55 supports only `ssh-rsa(SHA-1)`, `ssh-dss`, and `ecdsa-sha2-*` host key algorithms. If the target server offers only ed25519 or has ssh-rsa disabled per the OpenSSH 8.8+ default, negotiation fails. Two ways out: ① in `app/build.gradle` switch to the community fork `implementation 'com.github.mwiede:jsch:0.2.17'` (API-compatible, supports rsa-sha2/ed25519); ② add `HostKeyAlgorithms +ssh-rsa` and `PubkeyAcceptedAlgorithms +ssh-rsa` to the target server's `sshd_config` and restart sshd |
| Text messages lag during large file transfers | Chunked file writes within a single connection are synchronous, a known limitation (nothing is lost; see prd §8); avoid chatting while sending large files |

---

## 7. Known Limitations

- USB/`adb forward` only; not designed as a Wi‑Fi/LAN transport.
- Link encryption starts after HELLO/ACK; HELLO (including token) is cleartext on loopback.
- Does not defend against a compromised PC OS or covert screen recording.
- Interactive terminal uses pyte (120×30 PTY); advanced truecolor / every terminal quirk is not guaranteed.
- One PC attach per running connection ID; profiles are not synced to the PC.
- No resume-after-interruption, no parallel multi-device support, no directory browsing; the PC connects to only one device at a time.
- Within a connection, file transfer and text share one TCP stream, so text lags while a large file is in flight.
- An adb environment is required (an adb-free approach would need root or a custom ROM).

### Smoke checklist (link crypto + SSH hub)

1. PC token field is masked; first Connect shows **Approve PC** on phone → Trust → status `Connected … (encrypted)`.
2. Disconnect and Connect again (same PC) → no Approve dialog; still encrypted.
3. Second client with same fingerprint while first is connected → `PC_ALREADY_CONNECTED`.
4. Phone: add `ID_01`, Start, complete host trust / MFA → state `running`.
5. PC: Remote Terminal → Refresh → Attach `ID_01` → `uname -a`, arrows / Ctrl-C.
6. Detach → session still running → Attach again (backlog may replay).
7. Unplug USB → phone SSH still running → reconnect → Attach again.
8. Phone: Stop `ID_01` → cannot attach until Start again.

See [prd.md](./prd.md) §7 and §8 for more extensibility points and security boundaries.
