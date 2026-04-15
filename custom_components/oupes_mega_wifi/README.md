# OUPES Mega WiFi — Home Assistant Custom Integration

A Home Assistant custom integration that replaces the OUPES/Cleanergy cloud backend entirely. It acts as a local TCP broker for the device, intercepts REST API calls from the mobile app, and exposes full device telemetry as HA entities in real time.

**This is a combined integration** — the proxy server, HTTP interceptor, SiBo stub, and HA entity clients are all contained in a single custom component. No companion integration required.

> **Device settings over WiFi**: Silent Mode, Breath Light, Fast Charge, Screen Timeout, and standby timeouts are writable via this integration, but the device firmware **never echoes setting values back** over WiFi. Saved values are persisted in the HA state database and restored on restart. For full bidirectional setting state visibility, use the companion [`oupes_mega_ble`](../oupes_mega_ble/README.md) integration.

---

## Architecture

```
┌──────────────┐  TCP 8896 (NAT)   ┌─────────────────────────────────────────┐
│  OUPES Device│ ────────────────► │  HA: OUPESWiFiServer  (server.py)       │
│  (firmware)  │  wp-cn.doiting   │  TCP broker — topic routing, keepalive, │
│              │  .com → <HA_IP>  │  telemetry forwarding, device activation │
└──────────────┘                   └───────────────┬─────────────────────────┘
                                                   │ telemetry push
                                   ┌───────────────▼─────────────────────────┐
                                   │  OUPESWiFiCoordinator  (coordinator.py) │
                                   │  TCP client — subscribes, sends beats,  │
                                   │  pushes coordinator data to entities     │
                                   │                                         │
                                   │  Sensors · Binary Sensors               │
                                   │  Switches · Number (settings)           │
                                   └─────────────────────────────────────────┘

┌──────────────┐  HTTP 8897 (NAT)  ┌─────────────────────────────────────────┐
│  OUPES App   │ ────────────────► │  HA: OUPESHttpInterceptServer           │
│  (Android /  │  api.upspowers   │  http_server.py — API emulator,         │
│   iOS)       │  tation.top →    │  device bind/unbind, device sync,       │
│              │  <HA_IP>         │  rewrites tcp_host → local HA IP        │
└──────────────┘                   └─────────────────────────────────────────┘

┌──────────────┐  HTTPS 443 (NAT)  ┌────────────────┐  HTTP 8898  ┌──────────────────────────────────┐
│  OUPES App   │ ────────────────► │ SSL proxy       │ ──────────► │  HA: SiBoServerStub              │
│  (SiBo API)  │  wp-cn.doiting   │ (opt. Squid)    │             │  sibo_server.py — stub responses, │
│  + Device    │  .com → <HA_IP>  │                 │             │  prevents "token error" login loop│
└──────────────┘                   └────────────────┘             └──────────────────────────────────┘
```

### Protocol overview

| Channel | Remote Address | Protocol | Purpose |
|---------|---------------|----------|---------|
| TCP broker | `wp-cn.doiting.com:8896` / `wp-us.doiting.com:8896` | Text `key=value\r\n` | Device telemetry & control (app and device both use this) |
| TCP broker (legacy port) | `wp-cn.doiting.com:9504` / `wp-us.doiting.com:9504` | Same protocol | Alternate port seen in some firmware versions |
| REST API | `http://api.upspowerstation.top` (`47.251.27.175:80`) | HTTP/JSON | App ↔ OUPES cloud |
| SiBo API | `https://wp-cn.doiting.com` (HTTPS/443) | HTTPS/JSON | Device bind/unbind + secondary app cloud — real backend sends an unsubscribe that triggers a "token error" loop; intercepted with a self-signed cert that Android rejects silently |
| UDP discovery | `255.255.255.255:6095` | JSON broadcast | LAN device discovery by app (every 10 s) |

The TCP broker hostname (`wp-cn.doiting.com`) is returned in the login response `mark.tcpHost` field and resolved via DNS by both the device firmware and the app. The resolved IP varies by Alibaba Cloud region (e.g. `47.252.10.9`). DNS interception alone is not sufficient — firewall NAT rules matching the resolved IPs are required. Use **FQDN aliases** (type Host(s)) in your firewall so the alias IPs are kept up to date automatically.

---

## NAT Rules

Add these **destination NAT / port-forward** rules on your firewall. Examples use OPNsense terminology; pfSense is identical. Repeat for each VLAN that contains OUPES devices or phones.

The table below uses the alias names from [Firewall Aliases](#firewall-aliases-recommended) below. You can substitute raw IPs if preferred, but aliases are strongly recommended so rules survive cloud IP changes.

| # | Protocol | Destination (alias) | Dst Port | Redirect to | Redirect Port | Purpose |
|---|----------|---------------------|----------|-------------|---------------|---------|
| 1 | TCP | `OUPES_broker_host` | 8896 | `<HA_IP>` | 8896 | Device + app TCP broker |
| 2 | TCP | `OUPES_broker_host` | 9504 | `<HA_IP>` | 8896 | Legacy broker port (some firmware versions) |
| 3 | TCP | `OUPES_broker_host` | 80 | `<HA_IP>` | 8897 | Device firmware bind/unbind (SiBo HTTP) |
| 4 | TCP | `OUPES_broker_host` | 443 | `<HA_IP>` | 8898 | App SiBo HTTPS stub (token-error prevention) |
| 5 | TCP | `OUPES_App_REST_API` | 80 | `<HA_IP>` | 8897 | App REST API |

Replace `<HA_IP>` with your actual HA IP address.

> **Notes:**
> - All broker and SiBo traffic shares the `OUPES_broker_host` alias (`wp-cn.doiting.com`, `wp-us.doiting.com`). The device uses this hostname for both its boot-time bind (HTTP 80) and its ongoing TCP telemetry connection (port 8896). The app also uses it for TCP broker and SiBo HTTPS calls.
> - Rule 3 intercepts the device's boot-time `POST /api/device/bind` call. Without this, the device never receives a local broker address and will not connect. The HTTP intercept server (`http_server.py`) handles device bind/unbind alongside the app REST API on the same port 8897.
> - Rule 4 intercepts SiBo HTTPS calls from the app. See [SiBo Cloud Interception](#sibo-cloud-interception) below.
> - If you have multiple VLANs (e.g. LAN + SMARTHOME/IoT), duplicate all 5 rules for each interface.

### Firewall Aliases (recommended)

Create these two Host aliases under **Firewall → Aliases → +Add → Type: Host(s)**. Using aliases instead of hardcoded IPs means your NAT rules continue to work if the cloud host IPs change.

| Alias name | Type | Hostnames | Purpose |
|------------|------|-----------|---------|
| `OUPES_broker_host` | Host(s) | `wp-cn.doiting.com`, `wp-us.doiting.com` | TCP broker, device bind/unbind (HTTP 80), and SiBo HTTPS stub (443). One alias covers all traffic to the doiting.com cloud backend. |
| `OUPES_App_REST_API` | Host(s) | `api.upspowerstation.top`, `static.upspowerstation.top` | OUPES app REST API and static assets. |

> **Use FQDN (hostname) entries, not hardcoded IPs.** The doiting.com hostnames resolve to rotating Alibaba Cloud IPs that can change without notice. OPNsense re-resolves hostname aliases on a schedule and updates the firewall table automatically. After saving, force a refresh via **Firewall → Diagnostics → Aliases** to immediately populate the current IPs.

---

## SiBo Cloud Interception

The OUPES app makes a second set of HTTPS calls to a SiBo cloud backend (`wp-cn.doiting.com`). If these calls fail or return a stale-token error (`ret:"9"`), the app shows a **"token error"** toast and forces a re-login — creating an infinite loop.

This integration includes a built-in SiBo stub server (port 8898) that returns valid stub responses, completely preventing the loop.

### Option A — NAT redirect only (recommended)

No Squid or CA certificate installation required. Rule 4 from the NAT table above is all you need.

The HA stub server listens on port 8898 and presents a self-signed TLS certificate. Android (API 24+) rejects this certificate since it is not in the system trust store. OkHttp fires `onError(SSLHandshakeException)` — the app's SiBo error handler silently swallows it. Because no JSON response is received, `ret:"9"` is never seen and the login loop never triggers.

PCAP-verified: the TLS 1.3 handshake reaches HA, HA sends its cert, Android sends an encrypted Alert (certificate_unknown) and closes — 12 packets, no retry, no loop.

### Option B — Squid SSL inspection (advanced, untested)

Use this if you want the stub to return real JSON responses. In practice, Option A produces identical app behavior.

> **Untested.** Option A worked and Squid was never configured. These steps are provided as a reference only.

1. Install the Squid plugin (**System → Firmware → Plugins → os-squid** on OPNsense, or `pfSense-pkg-squid` on pfSense)
2. Enable **HTTPS/SSL Interception** with **Bump All** mode
3. Select your firewall's CA as the SSL Intercept CA
4. Add a `cache_peer` pointing to HA port 8898 with an ACL matching the `OUPES_broker_host` alias
5. Install the CA on Android — requires a rooted device (Magisk TrustUserCerts), LineageOS, or MDM push, as Android API 24+ ignores user-installed CAs for apps without explicit `network_security_config.xml` trust anchors

### SiBo Stub Endpoints

| Method | Path | Stub Response |
|--------|------|--------------|
| POST | `/api/app/temp_user/login` | `{"ret":"1","info":{"token":"oupes_ha_stub_token"},"desc":"success"}` |
| GET | `/api/v2/app/device_with_group/list` | `{"ret":"1","info":{"bindDevices":[],"shareDevices":[]},"desc":"success"}` |
| GET/POST | any other path | `{"ret":"1","info":{},"desc":"ok"}` |

> **Note:** SiBo uses string `"ret":"1"`, not integer `"ret":1`. The stub returns the correct string format.

---

## Installation

1. Copy the `oupes_mega_wifi` folder to `<config>/custom_components/`.
2. Restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration** and search for **OUPES Mega WiFi**.

---

## Configuration

### Account Entry

The first step creates an account entry that starts the TCP, HTTP, and SiBo servers. Only one account entry is needed per HA instance regardless of the number of devices.

| Field | Default | Description |
|-------|---------|-------------|
| Email | — | Any email address you choose — used as a local login identifier only |
| Password | — | Any password you choose — stored locally as a SHA-256 hash |
| TCP Port | `8896` | Port the TCP broker listens on — must match your NAT redirect |
| HTTP Port | `8897` | Port the HTTP intercept server listens on |
| SiBo HTTPS Port | `8898` | Port the SiBo stub server listens on |

> **These credentials are completely independent of the Cleanergy / OUPES cloud app.** This integration runs entirely locally and never contacts the OUPES cloud. The email and password you enter here are stored on your HA instance only — use whatever values you like; they do not need to match your real Cleanergy account.

Additional account entries can be added via the **Add Entry** button on the integration card if you want to support multiple local app logins — ports are inherited from the first entry.

#### Options (post-setup)

| Option | Default | Description |
|--------|---------|-------------|
| TCP Port | `8896` | Change the TCP broker port |
| HTTP Port | `8897` | Change the HTTP intercept port |
| SiBo HTTPS Port | `8898` | Change the SiBo stub port |
| Validation Mode | `accept_all` | How the broker handles unknown devices (see below) |
| Debug: Raw Lines | Off | Log every raw `key=value` TCP line |
| Debug: Telemetry | Off | Log parsed `cmd=10` telemetry objects |
| Debug: HTTP | Off | Log every intercepted HTTP request/response body |

##### Validation modes

| Mode | Behaviour |
|------|-----------|
| `accept_all` | Accept any connecting device regardless of credentials |
| `log_only` | Validate key against registry; log warnings but keep the connection open |
| `accept_registered` | Only accept devices whose `device_id` + `device_key` match a registered subentry |

### Device Subentries

Each device is a **subentry** under the account entry. Add devices by clicking the **+** button on the account entry. The **Add Entry** button on the integration card adds a new account, not a device.

#### Adding a device

The subentry flow offers three paths depending on whether you have a paired BLE entry:

| Option | When to use |
|--------|------------|
| **Generate new device key** | New device — uses BLE to provision WiFi credentials and a fresh device key. |
| **Import BLE Device** | An existing `oupes_mega_ble` subentry is detected — auto-fills device_id and device_key. |
| **Enter existing device key / Enter manually** | You already know the device_id and device_key. The device must already have WiFi configured pointing to HA, or you can send new WiFi credentials later via the **Reconfigure** option (gear icon on the subentry). |

For **Generate** mode, the flow:
1. Optionally discovers nearby BLE devices advertising as `TT` and pre-fills the MAC address
2. Asks for credentials (device name, device ID, device key, MAC address, WiFi SSID, WiFi password — SSID and password are **required** since the device needs them to reach the HA broker)
3. Shows factory-reset instructions, then runs BLE provisioning in a background progress task
4. On success, creates the subentry; on failure, returns to the credentials step with an error message

| Field | Description |
|-------|-------------|
| Device Name | Friendly name shown in HA |
| Device ID | 20-character hex ID (e.g. `756112968cd0b2a7ecad`) |
| Device Key | 10-character hex key (e.g. `bd236b1695`). Auto-derived from UID hash in generate mode. |
| MAC Address | BLE MAC address (required for generate/pairing; optional for manual) |
| WiFi SSID | Your home WiFi network name (generate mode only) |
| WiFi Password | Your home WiFi password (generate mode only) |

#### Reconfiguring a device

The **Reconfigure** option in the three-dot menu for an existing subentry lets you update WiFi credentials (sent over BLE to the device) or change any stored credential field.

---

## How It Works

### Device Boot Sequence

1. Device powers on → calls `POST /api/device/bind` to `wp-cn.doiting.com` (HTTP 80) — intercepted by Rule 3 → HA's HTTP server returns the local HA IP as `tcp_host`
2. Device connects TCP to `<HA_IP>:8896` → sends `cmd=subscribe&from=device&topic=control_<id>&device_id=<id>&device_key=<key>`
3. HA proxy server validates the connection and starts sending keepalives
4. HA coordinator (TCP client) subscribes to the device's telemetry topic → device begins streaming `cmd=10` telemetry

### Telemetry Streaming

The device only streams telemetry while it believes an active client is watching. The proxy server sends three periodic signals **directly from the device-side TCP session** to sustain the stream:

| Signal | Interval | Purpose |
|--------|----------|---------|
| `cmd=is_online` | Every 5 s | Notifies device a client is watching. Device silences itself within ~30 s without this. |
| Attr 84 keepalive (`cmd=3` with `{"84":1}`) | Every 10 s | WiFi equivalent of BLE `KEEPALIVE_PKT`. Required to keep stream alive. |
| Attribute group poll (`cmd=2`) | Every 30 s | Queries all telemetry attribute groups to force fresh data. |

These are sent server-side so the telemetry stream continues even if the HA coordinator briefly disconnects.

### App Login Flow

When the app logs in via HTTP:

1. `POST /api/app/user/login` — HA authenticates against the subentry registry and responds with a standard JSON login response
2. The response's `tcp_host` and `tcp_host_v2` fields are **rewritten** to point to `<HA_IP>:8896`
3. The app tells the device to connect to that address (or the device reconnects via its own boot bind)
4. `POST /api/app/device/sync` — HA caches full device metadata for subsequent `device/list` and `device/info` calls

### Model Detection

When a device calls `POST /api/device/bind`, the HTTP server extracts `device_product_id` from the POST body. This `product_id` is:
- Cached in the HTTP server's live device cache
- Persisted to the subentry data so it survives HA restarts
- Used to drive model-specific entity names (see [Model-Specific Labels](#model-specific-entity-labels) below)

---

## Entities

### Sensors (Main Unit)

| Sensor | Attribute | Unit | Notes |
|--------|-----------|------|-------|
| Battery Charge | 3 | % | |
| Total Output Power | 4 | W | |
| AC Output Power | 5 | W | |
| DC 12V Output Power | 6 | W | Label varies by model (see below) |
| USB-C Output Power | 7 | W | |
| USB-A Output Power | 8 | W | |
| Total Input Power | 21 | W | |
| Grid Input Power | 22 | W | |
| Solar Input Power | 23 | W | |
| Remaining Runtime | 30 | min | Clamped to filter firmware noise |
| Main Unit Temperature | 32 | °F | Raw value ÷ 10 |
| Expansion Battery Count | 51 | — | Number of connected expansion batteries |

### Sensors (Expansion Batteries — per slot)

Created dynamically when expansion batteries are detected (attr 101):

| Sensor | Attribute | Unit |
|--------|-----------|------|
| Expansion Battery Charge | 79 | % |
| Expansion Battery Runtime | 78 | min |
| Expansion Battery Temperature | 80 | °F (÷10) |
| Expansion Battery Output Power | 54 | W |
| Expansion Battery Input Power | 53 | W |

### Binary Sensors

Read-only status indicators derived from the attr-1 output bitmask:

| Binary Sensor | Bit | Notes |
|--------------|-----|-------|
| AC Output | 0x01 | |
| Car Port / Car & 12V Output | 0x02 | Label varies by model |
| USB Output / Anderson & USB Output | 0x04 | Label varies by model |

### Switches

Writable output port control via attr-1 bitmask (`cmd=3`):

| Switch | Bit | Notes |
|--------|-----|-------|
| AC Output | 0x01 | |
| Car Port / Car & 12V Output | 0x02 | Label varies by model |
| USB Output / Anderson & USB Output / XT90 Output | 0x04 | Label varies by model |

Switch state is applied **optimistically** — the local coordinator state is updated immediately on toggle so the UI responds instantly, without waiting for the next telemetry packet.

### Number Entities (Device Settings)

Writable device settings sent as `cmd=3` setting commands. These DPIDs are **never echoed back** by the device over WiFi, so values are stored optimistically and persisted to the HA state database across restarts.

| Entity | DPID | Range | Step | Notes |
|--------|------|-------|------|-------|
| Screen Timeout | 41 | 0–3600 s | 30 s | All series |
| Machine Standby Timeout | 45 | 0–43200 s | 600 s | Mega, Guardian, LP, Portable, UPS |
| WiFi Standby Timeout | 46 | 0–86400 s | 3600 s | Mega, Guardian |
| USB/Car Port Standby Timeout | 47 | 0–21600 s | 600 s | Mega, Guardian, LP, Portable, UPS |
| XT90 Standby Timeout | 48 | 0–21600 s | 600 s | Guardian only |
| AC Output Standby Timeout | 49 | 0–21600 s | 600 s | Mega, Guardian, LP, Portable, UPS |
| AC ECO Threshold | 111 | 0–100 W | 5 W | Exodus series only |
| DC ECO Threshold | 113 | 0–100 W | 5 W | Exodus series only |

Entities not applicable to a device's detected series are not created.

### Availability

All entities are marked **unavailable** if no successful telemetry update has been received within 5 minutes. Expansion battery sensors additionally require their slot to be present in the latest data.

---

## Model-Specific Entity Labels

Entity names and icons vary based on the device's product ID, which is detected automatically when the device performs its bind call. Known models:

| Product ID | Model Name | Series |
|------------|-----------|--------|
| `O44A5o` | Mega 1 | `mega_1` |
| `YRWj81` | Mega 2 | `mega` |
| `EFDayi` | Mega 3 | `mega` |
| `JTEnK3` | Mega 5 | `mega` |
| `Hr9Uhd` | Exodus 1200 | `exodus` |
| `pba1j6` | Exodus 1500 | `exodus` |
| `IDaSL8` | Exodus 2400 | `exodus` |
| `oB6OKs` | Guardian 6000 | `guardian` |
| `LtQmdj` | HP2500 | `guardian` |
| `95haDY` | D5 V2 | `guardian` |
| `zcWgyE` | LP350 | `lp` |
| `fckIgv` | LP700 | `lp` |
| `uAsyax` | UPS 1200 | `ups` |
| `QWlryl` | UPS 1800 | `ups` |

The series key drives entity name variants. For example, bit-2 of attr 1:

| Series | Switch/Sensor label |
|--------|-------------------|
| `mega_1` | USB Output |
| `mega` | Anderson & USB Output |
| `guardian` | XT90 Output |

DC output (bit 1 of attr 1):

| Series | Label |
|--------|-------|
| `mega_1` | Car Port |
| `mega` / `guardian` | Car & 12V Output |

The `product_id` is persisted in the subentry so model-specific labels are correct immediately after HA restart, without needing to wait for a new device bind.

---

## TCP Broker Protocol

The integration emulates the real OUPES cloud broker at `wp-cn.doiting.com:8896`.

### Topic Routing

Connections subscribe to topics. Messages are forwarded to all subscribers of the target topic (sender excluded):

| Connection type | Subscribes to | Publishes to |
|----------------|--------------|-------------|
| Device (`from=device`) | `control_<device_id>` | `device_<device_id>` |
| Client (`from=control`) | `device_<device_id>` | `control_<device_id>` |

When a device subscribes and a coordinator client is already waiting, the proxy sends the three-part activation sequence immediately (and vice versa):
1. `cmd=is_online&device_id=<id>` — notifies device a client is watching
2. `cmd=3` with `{"attr":[84],"data":{"84":1}}` — attr-84 streaming trigger
3. `cmd=2` with `{"attr":[1]}` — initial attribute query

> **Forwarded messages omit `device_key`:** Only the originating session includes `device_key`. The proxy strips it from forwarded messages, matching the real broker's behavior.

### Command Reference

| `cmd` | Direction | Behavior |
|-------|-----------|----------|
| `auth` | Client→Proxy | Logs the token. No response (matches real broker). |
| `subscribe` | Both→Proxy | Registers session in topic map. Replies `cmd=subscribe&topic=…&res=1`. |
| `ping` | Device→Proxy | Replies `cmd=pong&res=1`. |
| `keep` | Both→Proxy | Replies `cmd=keep&timestamp=<unix_s>&res=1`. |
| `is_online` | Client→Proxy | Checks device registry; forwards to device. Replies `cmd=is_online&res=1&online=0\|1`. |
| `publish` | Both→Proxy | ACKs `cmd=publish&res=1&num=1`. Routes to topic subscribers. |

---

## Coordinator (HA Client) Details

The coordinator connects to the proxy broker as a TCP client (`from=control`), exactly as the real Cleanergy Android app does.

### Connection Sequence

1. Sends `cmd=auth&token=<token>`
2. Sends `cmd=subscribe&topic=device_<id>&from=control&device_id=<id>&device_key=<key>`
3. Sends attr-84 keepalive to trigger streaming
4. Enters main read/heartbeat loop

### Heartbeats

| Heartbeat | Interval | Purpose |
|-----------|----------|---------|
| `cmd=is_online` | Every 5 s | Sustains telemetry stream |
| Attr 84 keepalive | Every 10 s | Secondary stream keepalive |
| `cmd=keep` | Every 60 s | TCP session liveness ping |

### Telemetry Processing

- Data arrives as `cmd=10` publish messages with JSON payload containing `msg.data` (integer attr→value pairs)
- `cmd=2` and `cmd=3` responses use the same `msg.data` format
- Attributes are accumulated in an internal `_attrs` dict; a snapshot is pushed to all HA entities after each update via `async_set_updated_data()`
- **Attr 101** indicates an expansion battery slot — subsequent ext-battery attributes are stored per-slot
- Runtime attrs (30, 78) are clamped to filter firmware noise emitted during charging

### Push-Based Updates

The coordinator uses `update_interval=None` — it does not poll. All data is push-driven from the persistent TCP connection. HA entities update in real time.

### Reconnection

If the TCP connection drops, the coordinator waits 10 seconds and reconnects automatically. The activation sequence is re-sent on each reconnect.

---

## Protocol Reference

For detailed reverse-engineered protocol documentation including full attribute maps, BLE protocol, OTA architecture, and raw PCAP findings, see [`debug_info/README.md`](../../debug_info/README.md).
