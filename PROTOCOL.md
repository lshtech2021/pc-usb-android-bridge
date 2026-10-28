# USB Bridge — PC ↔ Android Protocol Specification

Normative description of the application-layer protocol between the PC client
(`pc/`, Python + PyQt5) and the Android app (`android/`, Kotlin). This document
reflects the current implementation (HELLO `version: 2`). The reference
implementations are `pc/protocol.py` + `pc/transport.py` and
`android/.../FrameIO.kt` + `BridgeService.kt`; both must mirror each other
byte-for-byte.

---

## 1. Overview

### 1.1 Architecture and transport

The phone app runs an Android foreground service that listens on the **loopback
only** — `127.0.0.1:9999`. The PC reaches it through an adb port forward:

```
┌─────────────┐  adb forward tcp:12580 tcp:9999   ┌──────────────────┐
│  PC client  │ ─────────────────────────────────►│  Phone service   │
│ 127.0.0.1:  │      USB cable (ADB transport)    │  127.0.0.1:9999  │
│    12580    │◄──────────────────────────────────│  (loopback only) │
└─────────────┘                                   └──────────────────┘
```

- On connect the PC runs `adb -s <serial> forward --remove-all` then
  `adb -s <serial> forward tcp:12580 tcp:9999`, and opens a TCP connection to
  `127.0.0.1:12580` (connect timeout 5 s). The TCP connection is then a plain
  full-duplex byte stream carrying frames; both directions share the same
  socket — no `adb reverse` is needed.
- One adb forward → one phone session. Each accepted connection on the phone is
  handled by a dedicated `SessionHandler` on its own thread.
- The phone port is bound to loopback so only adb-forwarded traffic (and local
  processes on the phone) can reach it; the token + PC-trust checks (§2.3) and
  link encryption (§3.2–§3.3) protect against other local processes on the PC that
  could open the forwarded port.

### 1.2 Roles and feature set

| # | Feature | Direction | Messages |
|---|---------|-----------|----------|
| F1 | File transfer | both | `FILE_META` / `FILE_CHUNK` / `FILE_END` (+ `ACK`) |
| F2 | Text messages | both | `TEXT` (+ `ACK` from phone) |
| F3 | Remote terminal over phone-managed SSH | PC drives, phone owns SSH | `CONN_LIST` / `CONN_ATTACH` / `CONN_DETACH` / `REMOTE_DATA` / `REMOTE_OUTPUT` / `REMOTE_CLOSE` |
| F4 | Reverse tethering (SOCKS5) | PC-initiated streams, phone dials out | `PROXY_OPEN` / `PROXY_OPENED` / `PROXY_DATA` / `PROXY_CLOSE` |
| — | Handshake / keepalive / errors | both | `HELLO` / `ACK` / `PING` / `PONG` / `ERROR` |

The PC never receives SSH credentials: SSH profiles (host, port, user, auth,
MFA, host-key trust) live on the phone; the PC attaches to a running session by
connection id and only exchanges stdin/stdout bytes.

### 1.3 Protocol versioning

`HELLO` carries `version: 2` (the post-handshake AES-GCM sealing version). The
phone does not currently negotiate on `version`; it assumes a v2 client that
always performs the ECDH handshake (§2.2). A HELLO without `eph_pub` is
rejected (`BAD_FRAME`).

---

## 2. Connection establishment and authentication

### 2.1 The handshake

```
PC                                                      Phone
 │  TCP connect 127.0.0.1:12580 (via adb forward)          │
 │                                                         │
 │  HELLO {client, version:2, token, pc_id, pc_name,       │
 │         pc_pubkey, eph_pub}        ──────────────────►  │ validate token,
 │                                                         │ pc_id claim, trust store
 │                                                         │ ECDH(eph) → session key
 │  ◄──────────────────  ACK {ok:true, device, eph_pub}    │   (cleartext, last
 │                                                         │    unsealed frame)
 │  ECDH(eph) → session key                                │
 │  enable seal ◄────────────────────── enable seal        │
 │                                                         │
 │  PING (every 15 s)        ◄──────────►   PING (every 15 s)
 │  (all frames now sealed with AES-GCM)                   │
```

Rules:

- The **first frame must be HELLO**. Anything else causes the phone to send
  `ERROR {code:BAD_TOKEN, message:"expected HELLO"}` and close the socket.
- HELLO and ACK are **cleartext**; every frame after ACK is sealed (§3.2).
  A cleartext non-HELLO/ACK frame after the seal is enabled is a protocol
  violation: the AEAD decrypt fails and the session is torn down.
- Both sides enable the seal exactly once, immediately after sending/receiving
  the handshake ACK. The sender seals starting with its *next* frame; the
  receiver unseals starting with the frame after ACK.

### 2.2 HELLO field semantics

| Field | Type | Required | Meaning |
|-------|------|----------|---------|
| `client` | string | yes | `"pc-client"` |
| `version` | int | yes | `2` |
| `token` | string | yes | Pairing token shown in the phone UI/notification: 8 hex chars (4 random bytes), generated when the service starts and kept while the app process lives |
| `pc_id` | string | yes | PC fingerprint: SHA-256 hex (64 chars) of the PC's **long-term** P-256 public key in DER/SPKI form |
| `pc_name` | string | yes | Display name for the Approve-PC prompt |
| `pc_pubkey` | string | yes | Base64 of the PC long-term P-256 public key, DER/SPKI |
| `eph_pub` | string | yes | Base64 of a fresh **ephemeral** P-256 public key, DER/SPKI, generated per connection |

The PC identity (`pc_id`/`pc_pubkey`) is a persistent P-256 keypair stored in
`~/.usbbridge/identity.json` (mode 0600); the ephemeral key exists only for one
handshake and provides forward secrecy for the session key.

### 2.3 Phone-side validation order

The phone processes HELLO in this order and sends `ERROR` + close on the first
failure:

1. `token` mismatch → `BAD_TOKEN` ("token verification failed").
2. Missing `pc_id` / `pc_pubkey` / `eph_pub` → `BAD_FRAME`.
3. Another live session already holds this `pc_id` → `PC_ALREADY_CONNECTED`
   (only **one session per PC fingerprint** at a time).
4. Trust store (TOFU, per `pc_id`):
   - Unknown `pc_id` → "Approve PC" dialog showing `pc_name` and the first
     16 hex chars of `pc_id`. Deny → `PC_REJECTED`; approve → stored
     (`pc_id`, `pc_name`, `pc_pubkey`).
   - Known `pc_id` but different `pc_pubkey` → `PC_KEY_CHANGED` (must forget
     the PC on the phone and re-approve).
   - Known and key matches but `pc_name` changed → name updated silently.
5. Key agreement (§3.2–§3.3) fails → `BAD_FRAME` with the crypto error message.

After success the phone sends the handshake ACK (cleartext) **before** enabling
the seal, marks the session authenticated, claims the `pc_id`, and starts its
ping loop.

### 2.4 Session lifecycle rules

- No automatic reconnect: on disconnect both sides clean up and the user
  reconnects manually.
- Phone-side SSH sessions (`ConnectionHub`) **survive** USB disconnects; the
  USB `SessionHandler` teardown only clears hub attachments.
- Disconnect cleanup (both sides): abort and delete half-written files, close
  open proxy streams, release channel/sid/id tables, release the `pc_id` claim.

---

## 3. Frame grammar (wire format)

All multi-byte integers are **big-endian**, unsigned. All header values are
strings/integers/booleans in a single JSON object encoded as UTF-8
(`ensure_ascii` off on the PC side). Payload is an opaque byte string.

### 3.1 Cleartext frame

```
+--------+------+-----------+-------------------+-----------+------------+
| magic  | type | headerLen | header            | payloadLen| payload    |
| 2 bytes| 1 B  | 2 B (BE)  | N bytes (JSON)    | 4 B (BE)  | M bytes    |
+--------+------+-----------+-------------------+-----------+------------+
```

- `magic` = `0xAB 0xCD` (constant `MAGIC` on both ends; reader closes the
  connection on mismatch).
- `type` = message type byte (§4).
- `headerLen` = byte length of the JSON header (may be 0 → empty header `{}`).
- `payloadLen` = byte length of the payload (may be 0).
- Total length = `9 + headerLen + payloadLen`.
- Reader requires exact byte counts; EOF mid-frame is a disconnect.

### 3.2 Sealed frame (post-handshake)

Outer frame — same shape, but `headerLen` is always 0 and the payload is the
AEAD ciphertext of an inner plaintext:

```
+--------+------+-----------+-----------+----------------------------------+
| magic  | type | headerLen | payloadLen| ciphertext = AES-256-GCM(inner)  |
| 2 B    | 1 B  | 0x0000    | 4 B (BE)  |  (inner len + 16 B tag)          |
+--------+------+-----------+-----------+----------------------------------+
```

Inner plaintext (what gets encrypted):

```
+-----------+-------------------+-----------+------------+
| headerLen | header            | payloadLen| payload    |
| 2 B (BE)  | N bytes (JSON)    | 4 B (BE)  | M bytes    |
+-----------+-------------------+-----------+------------+
```

- **Nonce**: 12-byte big-endian counter. Two independent counters per
  direction — `send_ctr` for frames this side encrypts, `recv_ctr` for frames
  it decrypts — both starting at 0 and incremented once per frame.
- **AAD** (associated data, authenticates the outer header):
  `magic(2) ‖ type(1)`.
- **Tag**: GCM 128-bit (16 B), appended to the ciphertext.
- `type` stays in the clear so the receiver can look up the right nonce
  counter before decrypting; the counter ordering is what makes the nonces
  unique, so **frames must be sealed strictly in wire order** (§9).

### 3.3 Session key derivation

```
shared   = ECDH(priv_eph_self, pub_eph_peer) on P-256     (left-pad/trim to 32 B)
ikm      = shared ‖ token (token as UTF-8)
salt     = pc_id decoded from hex (32 B)   [if pc_id is 64 hex chars;
                                             otherwise pc_id as UTF-8]
key      = HKDF-SHA256(salt, info="usb-bridge-link-v1", ikm, L=32)   (RFC 5869)
```

- Both sides must derive the same key: the PC and the phone each run ECDH with
  their own ephemeral private key and the peer's `eph_pub`; `pc_id` and `token`
  are known to both.
- The ECDH output is normalized to exactly 32 bytes by left-padding or
  trimming, because Python `cryptography` and Android `KeyAgreement` can
  produce different leading-zero representations.
- The link seal is AES-256-GCM with the 32-byte key (GCM tag 128 bits).

### 3.4 Header encoding rules

- JSON object, UTF-8. Keys are exactly as listed per message.
- **Never send JSON `null`.** On Android, `JSONObject.optString("x")` converts
  a JSON `null` into the *string* `"null"`, which corrupts optional fields.
  The PC filters out `None` values when encoding; the phone omits absent keys.
- Numbers: `id`/`channel`/`sid`/`code`/`size`/`port`/`version` are integers;
  `size` and `ok` on the phone side are read with `optLong`/`optBoolean`.
- Base64 fields (`pc_pubkey`, `eph_pub`) are standard Base64 (PC: with
  padding, no line breaks).
- `sha256` fields are lowercase hex (64 chars).

---

## 4. Message reference (API)

### 4.1 Message type table

| Name | Value | Direction | Header | Payload |
|------|-------|-----------|--------|---------|
| `HELLO` | `0x00` | PC → phone | identity + token (§2.2) | — |
| `TEXT` | `0x01` | both | `{id, text}` | — |
| `FILE_META` | `0x02` | both | `{id, name, size}` | — |
| `FILE_CHUNK` | `0x03` | both | `{id}` | chunk bytes |
| `FILE_END` | `0x04` | both | `{id, ok, sha256? / error?}` | — |
| `REMOTE_OPEN` | `0x10` | PC → phone (legacy) | `{channel, kind, …}` | rejected |
| `REMOTE_DATA` | `0x11` | PC → phone | `{channel}` | stdin bytes |
| `REMOTE_OUTPUT` | `0x12` | phone → PC | `{channel, stream}` | stdout/stderr bytes |
| `REMOTE_CLOSE` | `0x13` | phone → PC (also accepted from PC) | `{channel, code, reason}` | — |
| `CONN_LIST` | `0x14` | PC → phone | `{}` | — |
| `CONN_LIST_RESULT` | `0x15` | phone → PC | `{connections: […]}` | — |
| `CONN_ATTACH` | `0x16` | PC → phone | `{channel, connection_id}` | — |
| `CONN_DETACH` | `0x17` | PC → phone | `{channel}` | — |
| `ACK` | `0x20` | phone → PC | varies by context (§4.3) | attach backlog |
| `ERROR` | `0x21` | both | `{code, message, …ctx}` | — |
| `PING` | `0x30` | both | `{}` | — |
| `PONG` | `0x31` | both | `{}` | — |
| `PROXY_OPEN` | `0x40` | PC → phone | `{sid, host, port}` | — |
| `PROXY_OPENED` | `0x41` | phone → PC | `{sid, ok, code?, message?}` | — |
| `PROXY_DATA` | `0x42` | both | `{sid}` | stream bytes |
| `PROXY_CLOSE` | `0x43` | both | `{sid, dir, code, reason}` | — |

### 4.2 Identifiers (id / channel / sid)

A single int namespace per direction, so concurrent operations never collide:

- **PC allocates**: 1, 2, 3, … (up to `0x3FFFFFFF`) — `PhoneClient._new_id()`
- **Phone allocates**: from `0x40000000` upward — `FrameIO.nextId()`

Used for TEXT `id`, FILE `id`, remote `channel` and proxy `sid`. The receiver
keys its tables on the sender's id, so both sides can upload/download,
attach, and open proxy streams simultaneously without clashing.

### 4.3 ACK — the polymorphic reply

`ACK` (0x20) is sent by the phone in four contexts; the PC dispatches on which
keys are present:

| Context | Header | Payload | Notes |
|---------|--------|---------|-------|
| Handshake | `{ok:true, device, eph_pub}` | — | `device` = `Build.MODEL`. Cleartext; the *last* unsealed frame |
| Text received | `{id}` | — | echoes the TEXT id |
| Upload finished | `{file_id, ok, path}` | — | `path` = saved location on phone (URI string or absolute path), `""` when rejected/mismatched; PC surfaces `"phone rejected / SHA mismatch"` when `ok:false` and path is empty |
| Attach finished | `{channel, connection_id, ok:true}` | backlog bytes | backlog = buffered output captured while the connection ran unattached (§6.5) |
| Detach finished | `{ok:true, channel}` | — | reply to `CONN_DETACH` |

Dispatch rule (PC): `"device" in header` → handshake; else `"file_id" in header`
→ upload result; else `connection_id` + `channel` present → attach result.

### 4.4 TEXT (0x01) — bidirectional

- Header: `{id:int, text:string}`.
- PC → phone: the phone raises a notification + appends to the message log and
  replies `ACK {id}`.
- Phone → PC: delivered to the PC message panel; **no ACK is sent** (the phone
  does not wait for one).

### 4.5 FILE_META / FILE_CHUNK / FILE_END — bidirectional transfer

**FILE_META (0x02)**
- `{id:int, name:string, size:int}` — `size` in bytes, `name` is the base name
  (paths are stripped on the receiver; both sides dedupe as `name(1).ext`).

**FILE_CHUNK (0x03)**
- `{id:int}` + payload = up to 256 KiB (262 144 B) of raw bytes. Chunks are
  streamed; only the final chunk may be shorter. Never buffers the whole file
  in memory.

**FILE_END (0x04)**
- Success: `{id, ok:true, sha256:hex}`.
- Cancel / failure from the sender: `{id, ok:false}` (PC cancel) or
  `{id, ok:false, error:message}` (phone failure while reading).
- Receiver checks `ok` and, when present, `sha256` against the running digest
  of the received bytes; on mismatch or `ok:false` the partial file is
  **deleted** and (for uploads) the ACK reports failure.

Limits and errors:

- Max single file **2 GiB** on both ends (PRD NFR). Oversized `FILE_META`:
  phone replies `ERROR {code:FILE_WRITE_FAILED, id, message:"file exceeds 2GB
  limit"}`; the PC refuses locally (status line) before the phone can send.
- Phone-side write problems (no permission, disk full, SAF folder gone):
  `ERROR {code:FILE_WRITE_FAILED, id, message}`; the PC marks the upload failed.
- Duplicate names never overwrite: receiver picks `name(1).ext`, `name(2).ext`, …
- Save locations: phone writes to the user-chosen SAF folder or the app's
  external files dir; the PC writes to `./downloads/`.

### 4.6 REMOTE_OPEN (0x10) — legacy, rejected

The credential-bearing PC-side open (`{channel, kind, host, …}`) was removed
from the product path. The phone answers every `REMOTE_OPEN` with
`ERROR {code:CHANNEL_OPEN_FAILED, message:"use CONN_ATTACH with a phone
connection id"}`. Product path: §4.7.

### 4.7 Remote SSH over phone-managed connections

**CONN_LIST (0x14)** — request the profile inventory.
- `{}` → reply `CONN_LIST_RESULT`:

```
{connections: [
  {id, name, host, port, user, state, error},
  …
]}
```

- `id` = phone-generated profile id (`ID_01`, …). `state` ∈ `"stopped"` |
  `"starting"` | `"running"` | `"error"`; `error` = last failure message or
  `""`. `host`/`port`/`user` are informational (the PC never needs them to
  attach).

**CONN_ATTACH (0x16)** — bind a PC channel to a *running* hub session.
- `{channel:int, connection_id:string}`
- Success: `ACK {channel, connection_id, ok:true}` whose **payload** is the
  backlog — output bytes captured in the phone-side ring buffer (max 64 KiB,
  keeps the most recent bytes) while the session ran unattached. The PC feeds
  the backlog into the terminal first.
- Failure: `ERROR {channel, code, message}` with
  - `CONN_NOT_FOUND` — unknown profile id,
  - `CONN_NOT_RUNNING` — known but not currently running,
  - `CONN_BUSY` — another PC channel is already attached (one attach at a
    time),
  - `CHANNEL_OPEN_FAILED` — any other failure.
- While attached, output flows as `REMOTE_OUTPUT` frames; the ring buffer keeps
  accumulating so a later re-attach still gets recent history.

**REMOTE_DATA (0x11)** — PC → phone stdin.
- `{channel:int}` + payload = raw bytes (terminal key sequences / pasted text,
  UTF-8). Written to the SSH channel's stdin. The phone processes remote ops on
  a serial executor so OPEN/DATA ordering is preserved.

**REMOTE_OUTPUT (0x12)** — phone → PC stream output.
- `{channel:int, stream:string}` + payload = raw bytes.
- `stream` ∈ `"out"` (stdout, shell + exec) | `"err"` (stderr, exec only).

**REMOTE_CLOSE (0x13)** — the hub session ended (phone → PC).
- `{channel:int, code:int, reason:string}`.
- `code` = SSH exit status (0 for a shell), `reason` ∈ `"shell exited"` |
  `"exec finished"` | `"stopped on phone"`. The PC pops the channel and closes
  the terminal tab view. No ACK.

**CONN_DETACH (0x17)** — PC releases the channel but leaves the SSH session
**running** on the phone (it survives USB disconnect).
- `{channel:int}` → `ACK {ok:true, channel}`. Output keeps accumulating in the
  ring buffer for the next attach.

The phone also accepts `REMOTE_CLOSE` from the PC (legacy close request): it
detaches the hub attachment and closes any legacy session, without ACK. The
product UI uses `CONN_DETACH`.

> SSH auth, MFA (keyboard-interactive), and host-key TOFU all happen on the
> phone's UI. Their results are not carried on this protocol; the PC sees only
> `state`/`error` in `CONN_LIST_RESULT` and `REMOTE_OUTPUT` once attached.

### 4.8 PROXY_* — reverse tethering (SOCKS5 over the bridge)

The PC runs a local SOCKS5 server (default `127.0.0.1:1080`, port
configurable). Each SOCKS client connection becomes one proxy stream; the
**phone opens the outbound TCP socket** on its own network (Wi-Fi / cellular),
so all DNS resolution happens on the phone.

**PROXY_OPEN (0x40)** — PC → phone, one per SOCKS CONNECT.
- `{sid:int, host:string, port:int}` — `host` is the domain name verbatim
  (ATYP 0x03) or the textual IPv4/IPv6 address; the phone resolves it.
- The PC refuses to open streams until the link is **sealed** (a SOCKS
  CONNECT arriving mid-handshake gets `REP 0x01` immediately).

**PROXY_OPENED (0x41)** — phone → PC, exactly one per sid, either
- `{sid, ok:true}` — TCP connected; both directions may now flow, or
- `{sid, ok:false, code, message}` — connect failed; no `PROXY_CLOSE` follows.
  `code` ∈ `PROXY_REFUSED` | `PROXY_HOST_UNREACHABLE` | `PROXY_TIMEOUT` |
  `PROXY_BAD_REQUEST` | `PROXY_LIMIT` (§5.2).

The phone rejects invalid opens synchronously with `ok:false` +
`PROXY_BAD_REQUEST` (empty host, port outside 1–65535, duplicate `sid`) or
`PROXY_LIMIT` (≥ 128 live streams).

**PROXY_DATA (0x42)** — both directions.
- `{sid:int}` + payload ≤ 64 KiB per frame.

**PROXY_CLOSE (0x43)** — both directions, half-close aware.
- `{sid:int, dir:string, code:int, reason:string}`, `dir` ∈:
  - `"c2s"` — PC's client→remote direction finished (SOCKS client stopped
    sending). The phone flushes queued bytes to the remote socket and calls
    `shutdownOutput()`. The phone may keep sending `s2c` data.
  - `"s2c"` — remote→client direction finished (remote EOF). The PC flushes
    its client-bound queue and shuts down the client socket's write side.
    The client may keep sending `c2s` data.
  - `"both"` — full teardown of the stream (error, buffer overflow, link
    loss, or both EOFs reached).

Stream teardown conditions:

- Phone reader hits remote EOF → `PROXY_CLOSE {dir:"s2c", code:0, reason:"remote EOF"}`.
- Phone socket error after connect → `PROXY_CLOSE {dir:"both", code:0, reason:"socket error"}`.
- Phone-side enqueue overflow (per-stream queue > 1 MiB) → phone closes the
  stream and sends `PROXY_CLOSE {dir:"both", code:0, reason:"buffer overflow"}`.
- PC-side buffer overflow (never block the bridge recv thread) → PC tears the
  stream down and sends `{dir:"both", reason:"buffer overflow"}`.
- Both half-closes received → both sides finish; the PC tears down after both
  EOFs. A half-closed but silent stream is reaped after 600 s idle.
- Bridge disconnect → every open stream is closed locally on both sides.

Sending `PROXY_DATA`/`PROXY_CLOSE` for an unknown `sid` is silently ignored on
both sides (best-effort). A generic `ERROR` frame carrying `sid` is treated by
the PC as `PROXY_CLOSE {dir:"both"}` (defensive path; the phone currently
reports proxy failures only via `PROXY_OPENED`).

### 4.9 PING / PONG (0x30 / 0x31)

- Empty header `{}`, no payload.
- Both sides send `PING` every 15 s and answer `PING` with `PONG` immediately.
- The PC enforces liveness: if no `PONG` is seen for 45 s it closes the
  connection with the status "heartbeat timeout". The phone has no PONG
  timeout; socket death surfaces through its own read loop.

### 4.10 ERROR (0x21) — generic failure

- `{code:string, message:string, …context}`. Context keys by domain:
  `id` (file), `channel` (remote attach/IO), `sid` (proxy).
- PC dispatch: `FILE_WRITE_FAILED` + `id` → fail that upload; `sid` present →
  close that proxy stream; `channel` present → fire `on_remote_error` for that
  channel; otherwise print `[Error] code message` to the status area.

---

## 5. Error codes

### 5.1 Full list

| Code | Sent by | Trigger |
|------|---------|---------|
| `BAD_TOKEN` | phone | token mismatch; or the first frame was not HELLO |
| `BAD_FRAME` | phone | HELLO missing `pc_id`/`pc_pubkey`/`eph_pub`; handshake crypto failure. (Wire-level magic/length failures close the socket without an ERROR) |
| `PC_REJECTED` | phone | user denied the Approve-PC dialog |
| `PC_ALREADY_CONNECTED` | phone | a live session already holds this `pc_id` |
| `PC_KEY_CHANGED` | phone | trusted `pc_id` presented a different long-term pubkey |
| `CONN_NOT_FOUND` | phone | `CONN_ATTACH` for an unknown profile id |
| `CONN_NOT_RUNNING` | phone | `CONN_ATTACH` for a profile that is not `running` |
| `CONN_BUSY` | phone | `CONN_ATTACH` while another PC channel is attached |
| `CHANNEL_OPEN_FAILED` | phone | legacy `REMOTE_OPEN` rejection; any other attach failure |
| `FILE_WRITE_FAILED` | phone | oversized FILE_META; cannot create/write the sink |
| `PROXY_BAD_REQUEST` | phone | `PROXY_OPENED.code`: invalid/empty host, port ∉ 1–65535, duplicate `sid` |
| `PROXY_LIMIT` | phone | `PROXY_OPENED.code`: ≥ 128 live streams |
| `PROXY_REFUSED` | phone | `PROXY_OPENED.code`: `ConnectException` / other connect failure |
| `PROXY_HOST_UNREACHABLE` | phone | `PROXY_OPENED.code`: `UnknownHostException` / `NoRouteToHostException` |
| `PROXY_TIMEOUT` | phone | `PROXY_OPENED.code`: connect timeout (5 s) |
| `UNKNOWN_HOST` | — (reserved) | legacy PC-credential open path; not sent by the current product path |
| `HOST_KEY_CHANGED` | — (reserved) | host-key MITM alarm; now handled by the phone-side TOFU UI, never on this wire |
| `AUTH_FAILED` | — (reserved) | SSH auth failure; now surfaces via `CONN_LIST_RESULT.state="error"` / `error` field |
| `HOST_UNREACHABLE` | — (reserved) | legacy; remote host failures now surface the same way |

### 5.2 SOCKS5 reply mapping (PC side)

`PROXY_OPENED.code` → SOCKS5 `REP` byte sent to the local SOCKS client:

| Bridge code | SOCKS5 REP |
|-------------|------------|
| (success) | `0x00` |
| `PROXY_REFUSED` | `0x05` (connection refused) |
| `PROXY_HOST_UNREACHABLE`, `PROXY_TIMEOUT` | `0x04` (host unreachable) |
| anything else (`PROXY_BAD_REQUEST`, `PROXY_LIMIT`, …) | `0x01` (general failure) |

---

## 6. Feature flows (sequence summaries)

### 6.1 Handshake and sealing

```
PC                        Phone
 │ HELLO (cleartext) ──────►│  token → claim → trust → ECDH
 │◄────── ACK (cleartext) ──│  (last unsealed frame)
 │ ECDH → key               │
 │══════ AES-GCM sealed ════│
 │ PING ───────────────────►│
 │◄─────────────────── PONG │
 │◄─────────────────── PING │
 │ PONG ───────────────────►│
```

### 6.2 Text

```
PC → phone:  TEXT{id:1,text:"hi"}      phone → PC:  ACK{id:1}
phone → PC:  TEXT{id:0x40000000,text}  (no reply)
```

### 6.3 File upload (PC → phone), including cancel

```
PC                            Phone
 │ FILE_META{id, name, size} ──►│ create FileSink (unique name)
 │ FILE_CHUNK{id} ×N ──────────►│ append + digest
 │ FILE_END{id, ok:true,        │
 │          sha256} ───────────►│ verify → keep file
 │◄── ACK{file_id, ok, path} ───│
```

Cancel / failures:

```
PC FILE_END{id, ok:false} ──► phone deletes partial → ACK{file_id, ok:false, path:""}
phone ERROR{code:FILE_WRITE_FAILED, id, message} ──► PC marks upload failed
```

### 6.4 File download (phone → PC)

```
Phone                        PC
 │ FILE_META{id, name, size} ──►│ open downloads/name(1).ext
 │ FILE_CHUNK{id} ×N ──────────►│ write + digest
 │ FILE_END{id, ok, sha256} ───►│ verify → keep or delete
                                 │ (no reply frame)
```

### 6.5 Remote terminal over a phone-managed SSH connection

```
PC                                     Phone (ConnectionHub)
 │ CONN_LIST {} ───────────────────────►│
 │◄── CONN_LIST_RESULT {connections:    │
 │       [{id:"ID_01", state:"running", │
 │         host, port, user, error}]}   │
 │ CONN_ATTACH {channel:5,              │
 │   connection_id:"ID_01"} ──────────►│  (must be running & unattached)
 │◄── ACK {channel:5, connection_id:    │
 │      "ID_01", ok:true} + backlog ────│  attach {channel→handler}
 │ REMOTE_DATA {channel:5} + "ls\r" ──►│  → SSH stdin
 │◄── REMOTE_OUTPUT {channel:5,         │  ← SSH stdout
 │      stream:"out"} + bytes           │
 │ CONN_DETACH {channel:5} ───────────►│  detach; SSH keeps running
 │◄── ACK {ok:true, channel:5}          │
 │           … USB may drop …           │  session survives; output → ring buffer
 │ CONN_ATTACH {channel:8,              │
 │   connection_id:"ID_01"} ──────────►│
 │◄── ACK {channel:8, ok:true} + backlog│
 │◄── REMOTE_CLOSE {channel:8,          │  SSH exited (or stopped on phone)
 │      code, reason}                   │
```

### 6.6 Reverse tethering (SOCKS5)

```
App/PC SOCKS client        PC bridge              Phone bridge          Remote server
 │ SOCKS5 handshake          │ (requires sealed link)│                       │
 │ CONNECT host:port ───────►│ PROXY_OPEN{sid,host,  │                       │
 │                           │   port} ─────────────►│  TCP connect (5 s)    │
 │                           │◄── PROXY_OPENED{sid,   │◄─────────────────────►│
 │                           │      ok:true}          │                       │
 │◄── REP 0x00               │                       │                       │
 │ data ────────────────────►│ PROXY_DATA{sid} ─────►│ → socket              │
 │◄───────────────────────── │◄── PROXY_DATA{sid} ───│ ← socket              │
 │ FIN (c2s) ───────────────►│ PROXY_CLOSE{c2s} ────►│ shutdownOutput()      │
 │                           │◄── PROXY_CLOSE{s2c} ──│ remote EOF            │
 │◄── EOF (s2c)              │                       │                       │
```

Local SOCKS5 server behavior (PC, `pc/proxy.py`):

- Listens on `127.0.0.1:1080` by default (configurable); refuses everything
  while the bridge is down or unsealed.
- No authentication (`0x00` only); only `CONNECT` (0x01); address types
  IPv4 / domain / IPv6 all accepted and forwarded verbatim.
- Per-stream: handshake timeout 15 s; wait for `PROXY_OPENED` 12 s; read/write
  chunks 64 KiB; downstream buffer 1 MiB; idle timeout 600 s (only while
  half-closed); max 128 live streams — excess clients get `REP 0x01`.
- Phone per-stream: connect timeout 5 s; queue limit 1 MiB; TCP_NODELAY on.

---

## 7. Keepalive and disconnect

| Rule | PC | Phone |
|------|----|-------|
| PING every | 15 s (starts after seal) | 15 s (starts after HELLO accepted) |
| PONG reply | immediate | immediate |
| Liveness check | close if no PONG within 45 s | none (relies on read loop) |
| Disconnect cleanup | delete partial downloads, close proxy streams + sockets, drop channels, fail pending uploads | delete partial files (FileSink.abort), close proxy sockets, detach hub attachments; **SSH sessions stay running** |
| Reconnect | manual; new HELLO (fresh eph key) | same token reused while process lives |

---

## 8. Constants and limits

| Constant | Value | Where |
|----------|-------|-------|
| Frame magic | `0xAB 0xCD` | both |
| Phone listen port | `127.0.0.1:9999` | `BridgeService.PORT` |
| PC adb-forward port | `tcp:12580` (local) | `pc/ui.py` |
| PC SOCKS5 default | `127.0.0.1:1080` | `pc/proxy.py` (configurable) |
| Token | 8 hex chars (4 random bytes) | phone service |
| HELLO version | `2` | PC |
| Link crypto | P-256 ECDH + HKDF-SHA256 + AES-256-GCM | both |
| HKDF info | `"usb-bridge-link-v1"` | both |
| GCM tag | 128 bit (16 B) | both |
| GCM nonce | 12 B per-direction counter (big-endian, from 0) | both |
| File chunk | 256 KiB | both |
| Max file size | 2 GiB | both |
| Heartbeat | PING 15 s / PC PONG timeout 45 s | both |
| Proxy chunk | 64 KiB | both |
| Proxy stream queue/buffer | 1 MiB | both |
| Max proxy streams | 128 | both |
| Phone connect timeout (proxy) | 5 s | phone |
| PC open wait / handshake / idle (proxy) | 12 s / 15 s / 600 s | PC |
| SSH backlog ring buffer | 64 KiB | phone `ConnectionHub.RING_MAX` |
| Terminal geometry | 120 × 30, `TERM=xterm-256color` | `RemoteSession` / `pc/terminal.py` |
| One session per pc_id | enforced | phone `tryClaimPc` |

---

## 9. Implementation notes and pitfalls

These are the subtle points that both reference implementations encode; a
third-party implementation must reproduce them exactly.

1. **Field order is fixed.** Frame layout is
   `magic(2) | type(1) | headerLen(2) | header | payloadLen(4) | payload`,
   total `9 + hlen + plen`. `pc/protocol.py` and `FrameIO.kt` must stay in
   lockstep.
2. **Never emit JSON `null`** for optional fields — Android `optString`
   returns the string `"null"`. Omit absent keys instead.
3. **Seal under the write lock.** The AEAD nonce is a counter, so the order
   frames are *encrypted* must equal the order they reach the wire. Two
   concurrent senders sealing outside the lock swap nonces and desync the
   peer's counter. Both `Transport.send` (PC) and `SessionHandler.send`
   (phone) therefore encode/encrypt inside their send lock.
4. **Per-direction nonce counters** start at 0 and count frames, not bytes.
   The `type` byte stays in the clear and is bound into the AAD, so a frame
   can only be replayed as the same message type at the same counter position.
5. **ECDH shared-secret normalization.** P-256 ECDH output is left-padded or
   trimmed to exactly 32 bytes so `cryptography` and Android `KeyAgreement`
   agree; HKDF input is `shared ‖ token` with salt = hex-decoded `pc_id`.
6. **HELLO/ACK are cleartext; everything after is sealed.** Receivers must
   switch to the unsealing decoder exactly after the handshake ACK — no
   cleartext frame may follow it, and a cleartext frame before HELLO is a
   violation.
7. **ID spaces are disjoint** (PC from 1, phone from `0x40000000`) so
   concurrent bidirectional transfers/attaches/proxies never collide in
   receiver tables.
8. **`ACK` is polymorphic** — dispatch on `device` → handshake, `file_id` →
   upload result, `channel`+`connection_id` → attach. Adding a new ACK
   context requires a new discriminator key.
9. **Name dedup rule** is `name(1).ext` on both ends; never overwrite.
10. **SHA-256 end-to-end.** The sender computes the digest while streaming,
    the receiver verifies at `FILE_END` and deletes the partial file on
    mismatch. Upload completion is only known from the phone's ACK.
11. **Backlog ring.** Phone-side SSH output is always appended to a 64 KiB
    ring; `CONN_ATTACH` returns the current contents as the ACK payload so a
    fresh terminal shows recent history.
12. **Disconnect = abort partials.** Close file handles, delete half-written
    files, wake blocked proxy streams on both sides; phone SSH sessions and
    their ring buffers intentionally outlive the USB link.
13. **Proxy data must never block the bridge read loop.** Bounded queues with
    overflow → teardown of that stream only (`buffer overflow`), never the
    shared connection.
14. **SOCKS is refused until the link is sealed**, so a browser CONNECT racing
    the handshake cannot leak the destination hostname in cleartext.

---

## Appendix A — code-side API mapping

**PC (`pc/client.py` → messages)**

| `PhoneClient` method | Wire action |
|----------------------|-------------|
| `connect(host, port, token)` | opens TCP; sends `HELLO`; arms handshake |
| `send_text(text)` | `TEXT {id, text}` |
| `send_file(path)` | worker thread: `FILE_META` → `FILE_CHUNK`×N → `FILE_END{ok, sha256}` |
| `cancel_upload(fid)` | `FILE_END {id, ok:false}` |
| `list_connections()` | `CONN_LIST {}` |
| `attach(connection_id)` | allocates channel; `CONN_ATTACH {channel, connection_id}` |
| `detach(ch)` / `remote_close(ch)` | `CONN_DETACH {channel}` |
| `remote_input(ch, bytes)` | `REMOTE_DATA {channel}` + payload |
| `alloc_proxy(cbs)` + `proxy_send_open` / `proxy_data` / `proxy_close` | `PROXY_OPEN` / `PROXY_DATA` / `PROXY_CLOSE` |
| callbacks `on_text/on_file_done/on_remote_output/…` | fed from `_on_frame` dispatch |

**Android (`BridgeService.kt` / `ConnectionHub.kt` / `ProxySession.kt`)**

| Entry point | Role |
|-------------|------|
| `SessionHandler.run` | frame read loop: unseal → `handle()` dispatch |
| `SessionHandler.send` | sealed encode under `outLock` |
| `SessionHandler.handleHello` | token/trust/claim checks → ECDH → ACK → seal → ping loop |
| `FileSink.create/append/finish/abort` | receive-side file store (SAF or app dir) |
| `SessionHandler.sendText` / `sendFile` | phone-initiated TEXT/FILE |
| `ConnectionHub.snapshot/attach/detach/writeStdin` | `CONN_LIST_RESULT` data, attach bookkeeping, stdin routing |
| `RemoteSession.openSsh/execSsh` | JSch SSH shell/exec; PTY 120×30; output → ring + `REMOTE_OUTPUT` |
| `ProxySession.readLoop/writeLoop/enqueue` | dial-out socket, per-stream relay, half-close handling |
