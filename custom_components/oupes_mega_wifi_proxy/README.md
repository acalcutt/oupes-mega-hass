# OUPES Mega WiFi — Home Assistant Custom Integration

A Home Assistant custom integration that acts as a local replacement for the OUPES/Cleanergy cloud backend. It intercepts both the **TCP broker** connection from devices and the **REST API** calls from the mobile app, redirecting them to your local HA instance via firewall NAT rules.

This is the **infrastructure/proxy** integration. For HA entities (sensors, switches, etc.), install the companion **[`oupes_mega_wifi_client`](../oupes_mega_wifi_client/README.md)** integration which connects to this proxy as a TCP client.

---

## Architecture

```
┌──────────────┐       TCP 8896        ┌────────────────────────────────┐
│  OUPES Device│ ──────────────────►   │  HA: OUPESWiFiProxyServer      │
│  (firmware   │   (NAT: 47.252.10.9   │  server.py — topic routing,    │
│   hardcoded) │    → 192.168.1.5)     │  immediate device activation,  │
│              │                       │  telemetry forwarding           │
└──────────────┘                       └─────────────┬──────────────────┘
                                                     │ topic routing
┌──────────────┐       TCP 8896        ┌─────────────▼──────────────────┐
│  WiFi Client │ ──────────────────►   │  oupes_mega_wifi_client         │
│  integration │   (localhost or LAN)  │  coordinator.py — subscribes,   │
│              │                       │  sends heartbeats, pushes data  │
│              │                       │  to HA entities                 │
└──────────────┘                       └────────────────────────────────┘

┌──────────────┐       HTTP 8897       ┌────────────────────────────────┐
│  OUPES App   │ ──────────────────►   │  HA: OUPESHttpInterceptServer  │
│  (Android /  │   (NAT: 47.251.27.175 │  http_server.py — full API     │
│   iOS)       │    :80 → :8897)       │  emulator, rewrites tcp_host   │
└──────────────┘                       └────────────────────────────────┘

┌──────────────┐   HTTPS 443 (SiBo)    ┌────────────────┐   HTTP 8898   ┌────────────────────────────────┐
│  OUPES App   │ ──────────────────►   │ SSL-inspecting │ ──────────► │  HA: SiBoClouServerStub        │
│  (SiBo API   │   (NAT: 8.135.109.78  │  proxy (e.g.   │              │  sibo_server.py — stub resp.   │
│   calls)     │    :443 → proxy)      │  Squid + CA)   │              │  prevents "token error" loop   │
└──────────────┘                       └────────────────┘               └────────────────────────────────┘
```

### Protocol summary (from PCAP reverse engineering)

| Channel | Address | Protocol | Purpose |
|---------|---------|----------|---------|
| TCP broker (primary) | `47.252.10.9:8896` / `wp-us.doiting.com:8896` | Text `key=value\r\n` | Device telemetry & control |
| TCP broker v2 | `47.251.14.8:9504` | Same protocol | Newer endpoint returned in login response |
| REST API | `http://api.upspowerstation.top` (`47.251.27.175:80`) | HTTP/JSON | App ↔ OUPES cloud |
| SiBo API | `https://wp-cn.doiting.com` (`8.135.109.78:443`) | HTTPS/JSON | App ↔ SiBo secondary cloud (device group sync, causes "token error" loop if unreachable) |
| UDP discovery | `255.255.255.255:6095` | JSON broadcast `{"cmd":0,"pv":0,"sn":"..."}` | LAN device discovery by app (every 10 s) |
| UDP cloud | `47.251.14.8:9200` | Unknown | Returned in login response, not yet observed in captures |

The device **firmware hardcodes** the TCP broker IP (`47.252.10.9`); DNS interception alone is not enough. NAT rules are required on your firewall/router for both the device TCP connection and the app REST calls.

---

## NAT Rules

Add these **port-forward / destination NAT** rules on your firewall or router's LAN interface (repeat for each VLAN that has OUPES devices or phones). The examples below use pfSense terminology, but any firewall that supports destination NAT will work:

| # | Protocol | Source | Destination | Dst Port | Redirect IP | Redirect Port | Purpose |
|---|----------|--------|-------------|----------|-------------|---------------|---------|
| 1 | TCP | LAN net | `47.252.10.9` | 8896 | `192.168.1.5` | 8896 | Device TCP broker (primary) |
| 2 | TCP | LAN net | `47.251.14.8` | 9504 | `192.168.1.5` | 8896 | Device TCP broker v2 |
| 3 | TCP | LAN net | `8.135.109.78` | 80 | `192.168.1.5` | 8897 | Device firmware bind/unbind (SiBo HTTP) |
| 4 | TCP | LAN net | `8.135.109.78` | 443 | `192.168.1.5` | 8898 | App SiBo HTTPS stub (prevents "token error" loop) |
| 5 | TCP | LAN net | `47.251.27.175` | 80 | `192.168.1.5` | 8897 | App REST API |

Replace `192.168.1.5` with your actual HA IP address.

> **Notes:**
> - Rules 1 and 2 both redirect to port 8896 (the TCP proxy). Rule 2 ensures devices using the newer `tcp_host_v2` endpoint are also intercepted.
> - Rule 3 intercepts the device firmware's boot-time `POST /api/device/bind` and `POST /api/device/unbind` calls to `wp-cn.doiting.com` on plain HTTP. Without this, the device has no broker address and never connects. The HTTP intercept server handles these alongside the app REST API.
> - Rule 4 intercepts the app's SiBo HTTPS calls. See [SiBo Cloud Interception](#sibo-cloud-interception-fixes-token-error-login-loop) below for details.
> - If you have multiple VLANs (e.g., LAN + IoT/SMARTHOME), duplicate all 5 rules for each interface.

---

## SiBo Cloud Interception (fixes "token error" login loop)

The OUPES app makes a **second set of HTTPS calls** to a SiBo cloud backend (`wp-cn.doiting.com` = `8.135.109.78:443`). These calls are completely separate from the main OUPES API and use a different HTTP client with no certificate pinning. If these calls fail (e.g. because the SiBo token has expired), the app shows a **"token error"** toast and forces a re-login — creating an infinite loop.

This integration includes a built-in SiBo stub server (port 8898) that intercepts those calls and returns valid stub responses, preventing the loop entirely.

### How the SiBo loop happens (root cause)

1. App logs into our OUPES mock (✅ succeeds)  
2. App calls `POST https://wp-cn.doiting.com/api/app/temp_user/login` with a stale/null SiBo token  
3. App calls `GET https://wp-cn.doiting.com/api/v2/app/device_with_group/list` — SiBo returns `ret:"9"` (expired token)  
4. `ResponseParse.onParse()` fires `skipToLogin(true)` + toast **"token error"**  
5. `Repository.clearAllMMKV()` clears session state **but not saved credentials**, so the login screen pre-fills email/password from storage and the user re-logs in — repeating from step 1

### Option A — NAT redirect only (recommended, simplest)

**No Squid or certificate installation required.** Rule 4 from the [NAT table above](#nat-rules) is all you need.

#### How it works

The HA stub server on port 8898 presents a self-signed TLS certificate. Android (API 24+) rejects any certificate that is not in the system trust store. OkHttp fires `onError(SSLHandshakeException)` — the app's SiBo error handler swallows it silently. Because no JSON response is received, `ret:"9"` is never seen, `skipToLogin()` is never called, and the login loop never triggers.

**PCAP-verified**: the TLS 1.3 handshake reaches HA, HA sends its cert, Android sends an encrypted Alert (certificate_unknown) and closes cleanly — 12 packets total, no retry, no loop.

---

### Option B — Squid SSL inspection (advanced, full stub responses)

Use this if you want the stub to return real JSON responses rather than failing at TLS. In practice Option A produces identical app behavior since OkHttp's SiBo error handler is silent.

#### Step 1 — Install Squid on pfSense

1. Go to **System → Package Manager → Available Packages**
2. Install `pfSense-pkg-squid`

#### Step 2 — Configure Squid with SSL inspection

1. Go to **Services → Squid Proxy Server → General**
2. Enable **Transparent HTTP Proxy** on your LAN interface
3. Enable **HTTPS/SSL Interception**
4. Under **SSL/MITM Mode**, select **Splice All** or **Bump All**
5. Select your **pfSense CA** (or create one under System → Certificate Manager → CAs) as the **SSL Intercept CA**
6. Save and apply

#### Step 3 — Add a Squid ACL + cache_peer for SiBo

In the **Custom Options** box, add:

```
cache_peer 192.168.1.5 parent 8898 0 no-query no-digest name=sibo_stub
acl sibo_cloud dst 8.135.109.78
cache_peer_access sibo_stub allow sibo_cloud
cache_peer_access sibo_stub deny all
never_direct allow sibo_cloud
```

Replace `192.168.1.5` with your HA IP.

#### Step 4 — NAT rule

Rule 4 from the [NAT table above](#nat-rules) is still required (same as Option A).

#### Step 5 — Install pfSense CA on Android

For Squid SSL inspection to work, the Android device must trust the pfSense CA certificate.

**Android 7+ (API 24+) restriction**: Apps that do not include `<trust-anchors>` in their `network_security_config.xml` (including the OUPES app) **do not trust user-installed CAs**. You need a **system-level** CA install.

**Option B1 — Rooted device with Magisk**:
1. Export the pfSense CA cert (System → Certificate Manager → CAs → Export → Certificate only)
2. Copy the `.crt` file to the Android device
3. Install **Magisk TrustUserCerts** module (from Magisk module repository)
4. Install the pfSense CA as a user CA (Settings → Security → Encryption & credentials → Install a certificate → CA certificate)
5. The Magisk module will automatically move user CAs to the system store on next boot

**Option B2 — Custom ROM / LineageOS**:
Push the CA cert to `/system/etc/security/cacerts/` using `adb` with root access.

**Option B3 — Android Enterprise / MDM (work profile)**:
Enroll the device under an MDM that can push system-level CA certificates.

---

### SiBo Stub Server (HA side)

The integration starts a SiBo stub server on port **8898** (configurable in the integration options). It provides stub responses for all known SiBo endpoints:

| Method | Path | Stub Response |
|--------|------|--------------|
| POST | `/api/app/temp_user/login` | `{"ret":"1","info":{"token":"oupes_ha_stub_token"},"desc":"success"}` |
| GET | `/api/v2/app/device_with_group/list` | `{"ret":"1","info":{"bindDevices":[],"shareDevices":[]},"desc":"success"}` |
| GET | `/api/v2/app/device/info` | `{"ret":"1","info":{},"desc":"success"}` |
| POST | `/api/app/device/bind` | `{"ret":"1","info":{},"desc":"success"}` |
| POST | `/api/app/device/unbind` | `{"ret":"1","info":{},"desc":"success"}` |
| POST | `/api/app/user/logout` | `{"ret":"1","info":{},"desc":"success"}` |
| (any other path) | — | `{"ret":"1","info":{},"desc":"ok"}` |

> **SiBo `ret` field is a string**: SiBo uses `"ret":"1"` (string), not `"ret":1` (integer) like the OUPES API. The stub server returns the correct string format.

> **Port 8898 and TLS**: The stub server listens on plain HTTP by default (Squid handles TLS termination). The server generates a self-signed TLS certificate at startup which is used only for direct HTTPS connections (e.g. testing without Squid).

---

## Installation

1. Copy the `oupes_mega_wifi_proxy` folder to `<config>/custom_components/`.
2. Restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration** and search for **OUPES Mega WiFi**.

---

## Configuration

### Main entry

| Option | Default | Description |
|--------|---------|-------------|
| TCP Port | `8896` | Port the TCP broker proxy listens on. Must match your NAT redirect port. |
| HTTP Port | `8897` | Port the HTTP intercept server listens on. Must match your NAT redirect port. |
| SiBo HTTPS Port | `8898` | Port the SiBo stub server listens on. Squid (or equivalent) forwards `wp-cn.doiting.com` traffic here. |
| Validation Mode | `accept_all` | How to handle devices with unknown keys (see below). |
| Debug: Raw Lines | Off | Log every raw `key=value` TCP line to the debug file. |
| Debug: Telemetry | Off | Log parsed `cmd=10` telemetry JSON objects. |
| Debug: HTTP | Off | Log every intercepted HTTP request/response body. |

#### Validation modes

| Mode | Behaviour |
|------|-----------|
| `accept_all` | Accept any connecting device regardless of credentials. |
| `log_only` | Validate key against registry; log warnings but keep the connection open. |
| `accept_registered` | Only accept devices whose `device_id` + `device_key` match a registered sub-entry. |

### Sub-entries (users / devices)

Each sub-entry represents one app user account. Add via **Add Entry** after the main entry is created.

| Field | Description |
|-------|-------------|
| Email | App account email address. |
| Password | Plaintext password — stored internally as a SHA-256 hex digest. |
| Device ID | `device_id` of the device to associate with this user (e.g. `756112968cd0b2a7ecad`). |
| Device Key | `device_key` for the device (e.g. `bd236b1695`). Used by the TCP broker to validate the connection. |

---

## How it replaces the cloud

1. **App login** (`POST /api/app/user/login`) — The HTTP server authenticates the user against the sub-entry registry. On success it returns the standard response but **rewrites `tcp_host` and `tcp_host_v2`** to point to the HA IP (`192.168.1.5:8896`). The app then tells the device to connect to that address.

2. **Device sync** (`POST /api/app/device/sync`) — The app pushes the full device JSON here. The HTTP server caches it so subsequent `device/list` and `device/info` calls return accurate data enriched with the locally-registered `device_key`.

3. **Device connects** via TCP to `192.168.1.5:8896` — The TCP proxy (`server.py`) receives the handshake, validates the `device_id`/`device_key` pair, and begins logging telemetry. In `accept_all` mode it accepts any device unconditionally.

---

## TCP Broker Protocol

The proxy emulates the real OUPES cloud broker (`47.252.10.9:8896`). It accepts connections from both devices (`from=device`) and clients (`from=control`) and routes messages between them using a topic-based pub/sub system.

### Topic Routing

Each connection subscribes to a topic. The broker maintains a `topic → [sessions]` map and forwards published messages to all subscribers of the target topic (excluding the sender):

| Session type | Subscribes to | Publishes to | Purpose |
|---|---|---|---|
| Device (`from=device`) | `control_<device_id>` | `device_<device_id>` | Receives commands, sends telemetry |
| Client (`from=control`) | `device_<device_id>` | `control_<device_id>` | Receives telemetry, sends commands |

When a device publishes `cmd=10` telemetry to `device_<id>`, the broker forwards it to all client sessions subscribed to `device_<id>`. When a client publishes a command to `control_<id>`, it's forwarded to the device session on that topic.

> **Forwarded messages omit `device_key`:** Matching the real broker's behavior, the proxy strips the `device_key` field from forwarded messages. Only the originating session includes `device_key`.

### Immediate Device Activation

When a device subscribes (`from=device`) and client sessions are already waiting on `device_<id>`, the proxy **immediately** sends a three-part activation sequence to the device:

1. `cmd=is_online&device_id=<id>` — notifies device a client is watching
2. `cmd=3` with `{"attr":[84],"data":{"84":1}}` — streaming trigger (attr 84 keepalive)
3. `cmd=2` with `{"attr":[1]}` — initial attribute query

This matches the real cloud broker's behavior and causes the device to begin streaming `cmd=10` telemetry. The same sequence is sent when a new client subscribes and the device is already connected.

### Command Dispatch

| `cmd` value | Direction | Behavior |
|---|---|---|
| `auth` | Client→Proxy | Logs the token. No response sent (matches real broker). |
| `subscribe` | Both→Proxy | Registers session in topic map. Replies `cmd=subscribe&topic=…&res=1`. |
| `ping` | Device→Proxy | Replies `cmd=pong&res=1`. |
| `keep` | Both→Proxy | Replies `cmd=keep&timestamp=<unix_s>&res=1`. |
| `is_online` | Client→Proxy | Checks device registry; forwards `cmd=is_online` to device. Replies `cmd=is_online&res=1&online=0\|1`. |
| `publish` | Both→Proxy | ACKs with `cmd=publish&res=1&num=1`. Routes message to all topic subscribers (excluding sender). |

### Poll Loop (Legacy)

For device-side sessions, the proxy also runs a background poll loop that sends `cmd=2` read requests across 4 attribute groups every 30 seconds. This is a fallback — the streaming protocol (above) is the primary data source.

---

## REST API Endpoints (intercepted)

All endpoints are under the `DOHOME_DOMAIN` (`http://api.upspowerstation.top`) base URL with the path prefix `/api/app/`.

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/app/user/login` | Login — returns `tcp_host` (rewritten to HA IP) |
| POST | `/api/app/user/register` | Register new account |
| POST | `/api/app/user/register/code` | Send verification code |
| POST | `/api/app/user/logout` | Logout / invalidate session |
| POST | `/api/app/user/logoff` | Delete account |
| POST | `/api/app/user/repasswd` | Change password |
| GET | `/api/app/user/profile` | User profile (returns broker redirect in `mark`) |
| GET | `/api/app/device/list` | List user's bound devices |
| GET | `/api/app/device/info` | Device info by `device_id` |
| POST | `/api/app/device/sync` | App pushes full device JSON (cached locally) |
| GET | `/api/app/device/model` | Device model metadata |
| GET | `/api/app/config/weburl` | Web URLs (privacy policy, etc.) |
| GET | `/api/app/config/app_version` | App update check |
| GET | `/api/app/shop/list` | Shop listing |
| GET | `/api/app/refresh/token` | Refresh auth token |

---

## Product IDs (from decompiled app)

These `device_product_id` values identify device models returned in `device/list`:

| Constant | Product ID | Model |
|----------|-----------|-------|
| `PID_MEGA1` | `O44A5o` | MEGA 1 |
| `PID_MEGA2` | `YRWj81` | MEGA 2 |
| `PID_MEGA3` | `EFDayi` | MEGA 3 |
| `PID_MEGA5` | `JTEnK3` | MEGA 5 |
| `PID_S012` | `Hr9Uhd` | S012 |
| `PID_S015` | `pba1j6` | S015 |
| `PID_S024` | `IDaSL8` | S024 |
| `PID_DC800` | `5cY3Mf` | DC800 |
| `PID_D5` | `oB6OKs` | D5 |
| `PID_D2` | `LtQmdj` | D2 |
| `PID_L350` | `zcWgyE` | L350 |
| `PID_L700` | `fckIgv` | L700 |
| `PID_UPS_1200` | `uAsyax` | UPS 1200 |
| `PID_UPS_1800` | `QWlryl` | UPS 1800 |

---

## Debug Logging

When any debug option is enabled, events are appended in JSONL format to:

```
<config>/oupes_mega_wifi_proxy_debug.jsonl
```

Each line is a JSON object with a `type` field:

| `type` | Trigger | Fields |
|--------|---------|--------|
| `raw_rx` | Every received TCP line | `ts`, `device_id`, `line` |
| `raw_tx` | Every sent TCP line | `ts`, `device_id`, `line` |
| `telemetry` | `cmd=10` data parsed | `ts`, `device_id`, `data` (dict) |
| `http_request` | Intercepted HTTP request | `ts`, `method`, `path`, `body` |
| `http_response` | Intercepted HTTP response | `ts`, `path`, `status`, `body` |

---

## WiFi Provisioning

WiFi credentials are provisioned to a new device **entirely via BLE** — there is no HTTP endpoint for this. The pairing sequence uses BLE CLAIM packets (packets 6, 7, 8 of the BLE pairing flow) which contain the SSID and password encoded in the device's CLAIM protocol.

The HTTP server does **not** need to handle WiFi provisioning.

---

## Known Limitations / Future Work

- The `udp_host` (`47.251.14.8:9200`) endpoint returned in login responses has not been observed in captures; its protocol is unknown.
- The `user_system` and `statistic` API families (energy monitoring, run schedules, weather, backup) return empty stubs — full implementation would require understanding the energy monitoring data model.
- OTA update interception (`/api/app/ota/new_version`) always returns "no update available".
- Home Assistant sensor/switch entities are provided by the companion integration **`oupes_mega_wifi_client`** — see its [README](../oupes_mega_wifi_client/README.md) for details. This integration (`oupes_mega_wifi_proxy`) handles infrastructure only (proxy/broker/stub servers).

---

## Source Reference

Protocol details were reverse-engineered from:
- PCAP captures of real device ↔ cloud traffic (13 capture files)
- Decompiled APK (`com.cleanergy.app`) — key files:
  - `lib/http/HttpConfig.java` — all API endpoint paths and base URLs (including `DOHOME_SIBO_DOMAIN = "https://wp-cn.doiting.com"`)
  - `lib/http/pojo/LoginPo.java` — login response schema
  - `lib/http/bean/UserDevicesBean.java` — device list schema
  - `AppParams.java` — product IDs and app-wide constants
  - `weight/ResponseParse.java` — `onParse()` method: SiBo `ret:"9"` → `skipToLogin(true)` → "token error" loop
  - `ui/user/UserLoginFragment.java` — `toLoginTemp()`: SiBo login flow; `onLazyLoad()`: credential pre-fill
  - `lib/http/model/HttpModel.java` — `uploadSiboToken()`: POSTs SiBo token to our `profile/upload` endpoint
  - `lib/mmkv/HttpExpKt.java` — `generalSettingMMKV` (credentials survive `clearAllMMKV()`), static `loginInfo` cache
