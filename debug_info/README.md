# OUPES Mega 1 — Reverse Engineered Telemetry API

> **Status:** Complete — 3 working Home Assistant integrations (`oupes_mega_ble`, `oupes_mega_wifi_proxy`, `oupes_mega_wifi_client`)  
> **Device:** OUPES Mega 1 Power Station (WiFi+BLE model)  
> **App:** Cleanergy (`com.cleanergy.app`)  
> **Firmware:** 1.2.0  

This document summarizes findings from reverse engineering the OUPES Mega 1's communication protocols, captured via PCAPdroid (Android), Wireshark, and pfSense firewall captures. These findings were used to build three Home Assistant integrations:

| Integration | Transport | Capabilities |
|---|---|---|
| **`oupes_mega_ble`** | Bluetooth LE (local) | Full read/write: sensors, binary sensors, output switches, all device settings (silent mode, breath light, fast charge, screen timeout, standby timeouts, ECO mode) |
| **`oupes_mega_wifi_proxy`** | TCP broker (LAN) | Infrastructure — intercepts the device's cloud connection via pfSense NAT and re-serves it locally. Provides the TCP broker that `wifi_client` connects to |
| **`oupes_mega_wifi_client`** | TCP via proxy broker | Read + output control only: sensors, binary sensors, AC/DC/USB output switches. Device settings are **not** supported over WiFi (firmware does not echo setting DPIDs back) |

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [WiFi / Cloud API](#wifi--cloud-api)
  - [HTTP REST API](#http-rest-api)
  - [Cloud Login API](#cloud-login-api)
  - [TCP Telemetry Channel (Port 8896)](#tcp-telemetry-channel-port-8896)
  - [Required Credentials](#required-credentials)
  - [Device Firmware Boot Sequence (SiBo)](#device-firmware-boot-sequence-sibo--wp-cndoitingcom)
  - [Device-Side Broker Protocol](#device-side-broker-protocol)
  - [Connection Sequence (App-Side)](#connection-sequence-app-side)
  - [WiFi Streaming Protocol](#wifi-streaming-protocol)
  - [Requesting Telemetry](#requesting-telemetry)
  - [Working Python Example](#working-python-example)
- [Bluetooth LE (BLE)](#bluetooth-le-ble)
  - [GATT Profile](#gatt-profile)
  - [BLE Advertising & Device Discovery](#ble-advertising--device-discovery)
  - [Obtaining the `device_key`](#obtaining-the-device_key)
  - [Connection Sequence](#connection-sequence-1)
  - [Init Sequence](#init-sequence)
  - [BLE Pairing / Re-keying Protocol](#ble-pairing--re-keying-protocol)
  - [Packet Format](#packet-format)
  - [BLE Output Control](#ble-output-control)
  - [BLE Python Example](#ble-python-example)
- [Telemetry Attribute Map](#telemetry-attribute-map)
- [WiFi Local Port Investigation](#wifi-local-port-investigation)
- [Firmware Update (OTA)](#firmware-update-ota)
  - [OTA Architecture](#ota-architecture)
  - [OTA Version Check API](#ota-version-check-api)
  - [OTA Command Protocol (Cmd5)](#ota-command-protocol-cmd5)
  - [Board Targets](#board-targets)
  - [Firmware Binary Format](#firmware-binary-format)
  - [DNS Redirect Attack Surface](#dns-redirect-attack-surface)
- [Notes & Unknowns](#notes--unknowns)

---

## Architecture Overview

The OUPES Mega 1 communicates via two parallel channels:

```
┌─────────────────────────────────────────────────────┐
│                  OUPES Mega 1 (ESP32)               │
│                                                     │
│  ┌──────────────┐        ┌───────────────────────┐  │
│  │  BLE (GATT)  │        │  WiFi TCP port 8896   │  │
│  │  ADV: "TT"   │        │  → wp-cn.doiting.com  │  │
│  └──────┬───────┘        └──────────┬────────────┘  │
│         │                           │               │
└─────────┼───────────────────────────┼───────────────┘
          │                           │
    Phone (BLE)               Cloud relay server
    local only                (Alibaba Cloud)
                                      │
                               App subscribes
                               and polls via
                               TCP 8896
```

**Key findings:**
- **The device firmware independently maintains its cloud connection.** Contrary to earlier observations, the device connects to the TCP broker on its own during the boot sequence — it does NOT require an active BLE connection from the app. The earlier `num=0` observations were due to the device not being bound (the SiBo bind step was missing). Confirmed via pfSense PCAP (2026-04-13): device boots → unbind → bind → TCP broker connect → telemetry streaming, all without any app interaction.
- **The device streams telemetry continuously once properly triggered.** The real cloud broker sends an immediate three-part notification sequence when a client subscribes, which starts the telemetry stream. The client must then sustain it with periodic `is_online` (every 5s) and `attr 84` keepalive (every 10s) messages. See [WiFi Streaming Protocol](#wifi-streaming-protocol) below.
- While cloud-connected, the device sends a `cmd=ping` / `cmd=pong` heartbeat approximately every 90 seconds.
- The BLE and WiFi channels use **identical attribute numbers** and the same attr 84 keepalive concept — BLE sends a binary `KEEPALIVE_PKT` every 10s, WiFi sends `cmd=3` with `{"84":1}` every 10s.

---

## WiFi / Cloud API

### HTTP REST API

**Base URL:** `http://api.upspowerstation.top` (port 80, unencrypted HTTP)

The HTTP API handles account and device management only. It does **not** provide live telemetry. Useful endpoints:

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/app/device/info` | Device metadata, online status |
| GET | `/api/app/device/list` | All devices bound to account |
| POST | `/api/app/device/sync` | Sync device registration |
| GET | `/api/app/config/weburl` | App configuration URLs |
| GET | `/api/app/device/model` | Device model list |

All requests require these query parameters:

```
token=<user_auth_token>
platform=android
lang=en
systemVersion=36
```

Example — get device info:
```
GET http://api.upspowerstation.top/api/app/device/info?device_id=<device_id>&token=<token>&platform=android&lang=en&systemVersion=36
```

> **Note:** The `online` field in the device info response is not reliable. The device may show `"online": 0` while actively maintaining its cloud TCP connection.

---

### Cloud Login API

The cloud API requires authentication. The login endpoint uses **unencrypted HTTP** (matching the official Cleanergy app's own behaviour). Confirmed via PCAPdroid packet capture of the official app's login flow.

#### Login

```
POST http://api.upspowerstation.top/api/app/user/login
Content-Type: application/json

{
  "mail": "<your_cleanergy_email>",
  "passwd": "<your_cleanergy_password>",
  "lang": "en",
  "platform": "android",
  "systemVersion": 36
}
```

**Headers** (observed from app):
```
versionname: 1.4.1
lang: en
package: com.cleanergy.app
```

**Response** (on success, `ret=1`):
```json
{
  "ret": 1,
  "desc": "ok",
  "info": {
    "token": "<session_token>",
    "uid": 12345,
    "mail": "...",
    "mark": "{\"client_id\":\"<mqtt_client_id>\",\"password\":\"<mqtt_password>\",\"userName\":\"<mqtt_user>\"}"
  }
}
```

The `token` field is used for all subsequent API calls. The `mark` field contains MQTT broker credentials (used for the TCP 8896 cloud channel — not needed for BLE).

#### Fetch device list

```
GET http://api.upspowerstation.top/api/app/device/list?token=<token>&platform=android&lang=en&systemVersion=36
```

**Response:**
```json
{
  "ret": 1,
  "desc": "ok",
  "info": {
    "bind": [
      {
        "device_id": "<device_id>",
        "device_key": "<device_key>",
        "device_product_id": "O44A5o",
        "mac_address": "<mac_address>",
        "name": "MEGA1",
        "firmware_version": "1.2.0",
        "online": 0
      }
    ]
  }
}
```

Each entry in the `bind` array contains the `device_key` needed for BLE init. The HA integration matches devices by `device_id` (extracted from BLE advertising) or `mac_address`.

> **Security note:** The API uses plaintext HTTP. Credentials and tokens are transmitted unencrypted. This mirrors the official app's behaviour — OUPES does not provide an HTTPS endpoint. The HA integration uses credentials only once during setup and does not store them.

---

### TCP Telemetry Channel (Port 8896)

**Server:** `wp-cn.doiting.com:8896` (Alibaba Cloud — resolves to a regional IP such as `47.252.10.9`; use FQDN firewall aliases, not hardcoded IPs)

This is a custom text-based pub/sub protocol over raw TCP — not MQTT, but conceptually similar. All messages are `key=value&key=value` pairs terminated with `\r\n`.

> **Note:** The device maintains its own persistent connection to this broker (see [Device Firmware Boot Sequence](#device-firmware-boot-sequence-sibo--wp-cndoitingcom)). A client must subscribe to the device's topic and send an activation sequence to start the telemetry stream — see [WiFi Streaming Protocol](#wifi-streaming-protocol).

---

### Required Credentials

Three pieces of information are needed to access telemetry:

| Credential | Description | How to Obtain |
|------------|-------------|---------------|
| `device_id` | Unique device identifier | From HTTP API (`/api/app/device/list`) or PCAPdroid capture |
| `device_key` | Device authentication key | From HTTP API or PCAPdroid capture |
| `auth_token` | Session/user token for the broker | Captured from PCAPdroid on the TCP 8896 connection (`cmd=auth&token=...`) |

> **Token longevity:** Both the broker token and HTTP token were observed to be **identical across multiple capture sessions** over several hours, suggesting they are long-lived account tokens rather than short-lived session tokens. The broker token comes from the `wp-cn.doiting.com` TLS connection the app makes at startup — this connection is encrypted and its contents cannot be read without TLS interception.

Example values (replace with your own — obtain via PCAPdroid as described above):
```
device_id  = <20-char hex string, e.g. from /api/app/device/list>
device_key = <10-char hex string, paired with device_id>
auth_token = <broker token from cmd=auth in TCP 8896 capture>
http_token = <user account token from HTTP API requests>
```

---

### Device Firmware Boot Sequence (SiBo / wp-cn.doiting.com)

> **Confirmed via pfSense PCAP 2026-04-13** — with NAT rules disabled, observing the device talking to the real OUPES cloud servers.

The device firmware (DoHome ESP32, `User-Agent: DoHome-HTTP-Client/2.1`) performs an HTTP boot sequence on **plain HTTP port 80** to `wp-cn.doiting.com` (resolves to `8.135.109.78`) before connecting to the TCP broker. This is separate from the app's HTTPS API on port 443.

**Boot sequence:**

```
1. Device powers on, obtains DHCP lease, resolves wp-cn.doiting.com
2. POST /api/device/unbind  (plain HTTP to 8.135.109.78:80)
3. POST /api/device/bind    (plain HTTP to 8.135.109.78:80)
4. Parse bind response → extract tcp_ip and tcp_port
5. TCP connect to tcp_ip:tcp_port (broker)
6. cmd=subscribe&from=device&topic=control_<device_id>&...
7. cmd=keep&device_id=...&device_key=...
8. Begin publishing telemetry (cmd=10)
```

**Bind request** (`POST /api/device/bind`):
```json
{
  "device_id": "756173148cd0b2a8e142",
  "device_key": "bd236b1695",
  "device_product_id": "O44A5o",
  "user_token": "siimPDqjxJCRBelB2cf10ILo43Lyvk",
  "lat": "0",
  "lng": "0",
  "device_firmware_version": "1.2.0",
  "additional_detail": {
    "net_mac": "8cd0b2a8e142",
    "bt_mac": "8cd0b2a8e144",
    "ap": "MyWiFi_2.4GHz"
  }
}
```

**Bind response** (real cloud):
```json
{
  "ret": "1",
  "desc": "Success",
  "info": {
    "uid": "60859",
    "tcp_ip": "47.252.10.9",
    "tcp_port": 8896,
    "timestamp": "1776055648",
    "timezone_offset": 0
  }
}
```

> **Critical:** The `info.tcp_ip` and `info.tcp_port` fields tell the device where to connect for the TCP broker. Without these fields, the device has no broker address and never connects. The `ret` code is `"1"` (string), matching the SiBo app API convention.

**Timing observed (PCAP):**
| Time | Event |
|------|-------|
| 04:46:57 | POST /api/device/unbind |
| 04:47:26 | POST /api/device/bind (289-byte JSON body) |
| 04:47:29 | Bind response received (ret:"1" with tcp_ip/tcp_port) |
| 04:47:29.383 | TCP SYN to wp-cn.doiting.com:8896 (resolved to 47.252.10.9 at time of capture) |
| 04:47:29.462 | cmd=subscribe sent to broker |
| 04:47:29.486 | cmd=keep sent |
| 04:47:29.563 | First cmd=publish (telemetry attr [0] init) |

The device connects to the broker within **300ms** of receiving the bind response.

---

### Device-Side Broker Protocol

The device firmware uses a slightly different protocol from the app when talking to the TCP broker:

**Device subscribe** (note `from=device` and `control_` topic prefix):
```
cmd=subscribe&from=device&topic=control_<device_id>&device_id=<device_id>&device_key=<device_key>\r\n
```

**Device keep** (includes credentials):
```
cmd=keep&device_id=<device_id>&device_key=<device_key>\r\n
```

**Device publish** (telemetry, uses `device_` topic prefix):
```
cmd=publish&topic=device_<device_id>&device_id=<device_id>&device_key=<device_key>&message=<json>\r\n
```

**Server responses** (identical for device and app):
```
cmd=subscribe&topic=control_<device_id>&res=1\r\n
cmd=keep&timestamp=<unix_seconds>&res=1\r\n
```

> **Note on `device_key`:** The `device_key` is **account-scoped, not device-scoped**. It is derived from the user's cloud account UID via `MD5(uid)[:10]` — confirmed from `KeyUtils.java` in the Cleanergy APK (`createDeviceKey(userId) = MD5Util.encodeMD5(userId).substring(0, 10)`). All devices bound to the same Cleanergy account share the same `device_key`. Example: uid=`60859` → `MD5("60859")[:10]` = `bd236b1695`. A different value seen in earlier captures (`e98ff526ad`) was from a different user account, not a firmware-vs-API discrepancy.

---

### Connection Sequence (App-Side)

The full sequence to establish a working telemetry session on TCP 8896:

```
1. TCP connect → wp-cn.doiting.com:8896  (IP resolved via DNS at connect time)

2. TX: cmd=auth&token=<auth_token>\r\n
   RX: (acknowledged silently — no explicit response)

3. TX: cmd=subscribe&topic=device_<device_id>&from=control&device_id=<device_id>&device_key=<device_key>\r\n
   RX: cmd=subscribe&topic=device_<device_id>&res=1\r\n

4. TX: cmd=is_online&device_id=<device_id>\r\n
   RX: cmd=is_online&res=1&online=1\r\n
   (The real broker also forwards this as cmd=is_online to the device,
    which triggers it to begin streaming telemetry.)

5. Periodic heartbeats (must be sent continuously to sustain the stream):
   - Every 5s:  TX: cmd=is_online&device_id=<device_id>\r\n
   - Every 10s: TX: cmd=publish&...&message={"cmd":3,"msg":{"attr":[84],"data":{"84":1}},...}\r\n
   - Every 60s: TX: cmd=keep\r\n
                RX: cmd=keep&timestamp=<unix_s>&res=1\r\n

6. Device streams telemetry autonomously:
   RX: cmd=publish&device_id=...&topic=device_<device_id>&message={...cmd=10 telemetry...}\r\n
   (continuous — new data arrives every few seconds while heartbeats are active)
```

The device sends its own independent heartbeat to the broker while cloud-connected:
```
TX (device): cmd=ping\r\n
RX (server): cmd=pong&res=1\r\n
```
approximately every 90 seconds.

---

### WiFi Streaming Protocol

> **Confirmed working:** This protocol was reverse-engineered by comparing PCAP captures of the real Cleanergy app ↔ cloud broker (PCAPdroid) against local proxy captures. The HA integration (`oupes_mega_wifi_proxy` + `oupes_mega_wifi_client`) implements this protocol and produces live telemetry.

The device does not push telemetry just because a client subscribes — the broker must send a specific activation sequence to the device, and the client must send periodic heartbeats to sustain the stream. This mirrors the BLE protocol, where a keepalive packet (`attr 84 = 1`) must be sent every 10 seconds.

#### Activation: Three-Part Immediate Notification

When a client subscribes to `device_<id>` and the device is already connected (subscribed to `control_<id>`), the real cloud broker **immediately** sends three messages to the device:

```
1. cmd=is_online&device_id=<device_id>\r\n
   → Tells the device a client is watching. The device echoes this back.

2. cmd=publish&topic=control_<device_id>&message={"cmd":3,"pv":0,"sn":"<ts>","msg":{"attr":[84],"data":{"84":1}}}\r\n
   → Attr 84 streaming trigger (identical semantics to BLE KEEPALIVE_PKT).
     The device ACKs with cmd=10 containing {"84":1}.

3. cmd=publish&topic=control_<device_id>&message={"cmd":2,"pv":0,"sn":"<ts>","msg":{"attr":[1]}}\r\n
   → Initial attribute query. The device responds with a cmd=10 burst
     containing the current values of the requested attributes.
```

After this sequence, the device begins **continuous telemetry streaming** — it pushes `cmd=10` data every few seconds without further prompting.

> **Key discovery:** Without this immediate notification sequence, the device never starts streaming, even if a client sends poll requests. The three-part trigger at subscribe time is what starts the data flow.

#### Sustaining the Stream: Three Heartbeats

Once streaming is active, the client must send three periodic heartbeats to keep data flowing:

| Heartbeat | Interval | Command | Purpose |
|-----------|----------|---------|---------|
| `is_online` | Every 5s | `cmd=is_online&device_id=<id>` | Tells broker/device a client is still watching. Without this, the device stops streaming after ~30s. |
| Attr 84 keepalive | Every 10s | `cmd=publish` with `{"cmd":3,"msg":{"attr":[84],"data":{"84":1}}}` | WiFi equivalent of BLE `KEEPALIVE_PKT`. Sustains the telemetry stream. |
| Session keep | Every 60s | `cmd=keep` | TCP session liveness ping. Broker replies with `cmd=keep&timestamp=<unix_s>&res=1`. |

If any heartbeat stops for more than ~30 seconds, the device stops streaming and must be re-triggered with the activation sequence.

> **Mega 2 observation (2026-04-14):** In a full Mega 2 session captured via pfSense pcap, the device began streaming after `subscribe` + `cmd=is_online` alone, with attr 84 not sent until ~40 seconds later. This suggests the `is_online` notification is sufficient to trigger streaming; attr 84 at subscribe time is **recommended** (it matches the real cloud broker's activation sequence and is sent by our proxy) but not strictly required to start data flow. The attr 84 keepalive every 10 s is still required to **sustain** the stream.

#### Forwarded Message Format

When the real broker forwards a `cmd=publish` from one session to another (e.g. device telemetry to app client), it **strips the `device_key`** field. This was confirmed by comparing device-side and app-side PCAPs:

```
Device sends:   cmd=publish&device_id=X&device_key=Y&topic=device_X&message={...}
Client receives: cmd=publish&device_id=X&topic=device_X&message={...}
                 (no device_key)
```

The `device_key` field is only present in messages originating from the sender's own session. Forwarded copies omit it. The local proxy must match this behavior.

#### JSON Format

The device firmware parses JSON strictly. Payloads must use **compact JSON** with no spaces after separators:

```
✅ {"cmd":3,"pv":0,"sn":"1234567890","msg":{"attr":[84],"data":{"84":1}}}
❌ {"cmd": 3, "pv": 0, "sn": "1234567890", "msg": {"attr": [84], "data": {"84": 1}}}
```

Use `json.dumps(..., separators=(",", ":"))` in Python.

---

### Requesting Telemetry

After subscribing, the client must send `cmd=publish` messages with `cmd=2` (read request) in the JSON body to trigger the device to respond with `cmd=10` (telemetry data). The device responds to each group independently.

**Request format:**
```
cmd=publish&device_id=<device_id>&topic=control_<device_id>&device_key=<device_key>&message=<json>\r\n
```

Where `<json>` is:
```json
{
  "msg": {"attr": [1, 2, 3, 4, 5]},
  "pv": 0,
  "cmd": 2,
  "sn": "<timestamp_ms_string>"
}
```

**Recommended attribute groups to poll:**

```python
ATTR_GROUPS = [
    [1, 2, 3, 4, 5, 6, 7, 8, 9],      # outputs, inputs, battery %
    [21, 22, 23, 30, 32],              # AC voltage/freq, Wh, battery V
    [51],                              # charge mode
    [101, 53, 54, 78, 79, 80],         # charging source, temp, battery %
]
```

**Device response format (`cmd=10`):**
```
cmd=publish&device_id=<device_id>&topic=device_<device_id>&message=<json>\r\n
```

Where `<json>` is:
```json
{
  "cmd": 10,
  "pv": 0,
  "sn": "<timestamp_ms_string>",
  "msg": {
    "attr": [1, 2, 3, 4],
    "data": {
      "1": 1,
      "2": 0,
      "3": 100,
      "4": 488
    }
  }
}
```

**Control command format (`cmd=3`, write):**
```json
{
  "msg": {"attr": [84], "data": {"84": 1}},
  "pv": 0,
  "cmd": 3,
  "sn": "<timestamp_ms_string>"
}
```

---

### Working Python Example

```python
import socket, time, json

HOST       = 'wp-cn.doiting.com'  # or your local proxy IP
PORT       = 8896
DEVICE_ID  = 'YOUR_DEVICE_ID'
DEVICE_KEY = 'YOUR_DEVICE_KEY'
TOKEN      = 'YOUR_BROKER_AUTH_TOKEN'

def send(s, msg):
    s.send((msg + '\r\n').encode())

def make_json(cmd, attrs, data=None):
    payload = {
        "msg": {"attr": attrs},
        "pv": 0,
        "cmd": cmd,
        "sn": str(int(time.time() * 1000))
    }
    if data:
        payload["msg"]["data"] = data
    return json.dumps(payload, separators=(",", ":"))

def connect(s):
    send(s, f"cmd=auth&token={TOKEN}")
    time.sleep(0.2)
    send(s, f"cmd=subscribe&topic=device_{DEVICE_ID}&from=control"
           f"&device_id={DEVICE_ID}&device_key={DEVICE_KEY}")
    time.sleep(0.3)
    # Trigger streaming with is_online + attr 84 keepalive
    send(s, f"cmd=is_online&device_id={DEVICE_ID}")
    time.sleep(0.1)
    msg = make_json(3, [84], {"84": 1})
    send(s, f"cmd=publish&device_id={DEVICE_ID}"
           f"&topic=control_{DEVICE_ID}&device_key={DEVICE_KEY}"
           f"&message={msg}")

def listen_and_heartbeat(s):
    buf = ""
    last_online = last_attr84 = last_keep = time.time()
    s.settimeout(2)  # short timeout for interleaved read + heartbeat
    while True:
        now = time.time()
        # Send periodic heartbeats
        if now - last_online >= 5:
            send(s, f"cmd=is_online&device_id={DEVICE_ID}")
            last_online = now
        if now - last_attr84 >= 10:
            msg = make_json(3, [84], {"84": 1})
            send(s, f"cmd=publish&device_id={DEVICE_ID}"
                   f"&topic=control_{DEVICE_ID}&device_key={DEVICE_KEY}"
                   f"&message={msg}")
            last_attr84 = now
        if now - last_keep >= 60:
            send(s, "cmd=keep")
            last_keep = now
        # Read incoming data
        try:
            buf += s.recv(4096).decode(errors='replace')
            while '\r\n' in buf:
                line, buf = buf.split('\r\n', 1)
                if f'topic=device_{DEVICE_ID}' in line and 'message=' in line:
                    try:
                        payload = json.loads(line[line.index('message=') + 8:])
                        if payload.get('cmd') == 10:
                            data = payload['msg']['data']
                            print(f"Battery: {data.get('3','?')}% | "
                                  f"Output: {data.get('4','?')}W | "
                                  f"Grid: {data.get('22','?')}W | "
                                  f"Solar: {data.get('23','?')}W | "
                                  f"Temp: {int(data.get('32', 0)) / 10:.1f}°F")
                    except Exception:
                        pass
        except socket.timeout:
            pass

# Main loop with auto-reconnect
while True:
    try:
        print("Connecting...")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((HOST, PORT))
        connect(sock)
        print("Connected. Streaming telemetry...")
        listen_and_heartbeat(sock)
    except (ConnectionResetError, BrokenPipeError, OSError) as e:
        print(f"Disconnected ({e}), reconnecting in 10s...")
        time.sleep(10)
    except KeyboardInterrupt:
        print("Stopped.")
        break
```
        break
```

---

## Bluetooth LE (BLE)

The device advertises as **"TT"** over Bluetooth LE. The BLE channel is fully local — it does not require cloud connectivity, an auth token, or an active WiFi connection. This is the **recommended path** for a fully local Home Assistant integration.

### GATT Profile

| Property | Value |
|----------|-------|
| Device name | `TT` |
| Advertisement service UUID | `0xA201` |
| Service UUID | `00001910-0000-1000-8000-00805f9b34fb` |
| Write characteristic | `00002b11-0000-1000-8000-00805f9b34fb` (ATT handle `0x0003`) — write-without-response |
| Notify characteristic | `00002b10-0000-1000-8000-00805f9b34fb` (ATT handle `0x0005`) — notify |
| CCCD descriptor | ATT handle `0x0006` |

All telemetry is received via notifications on `00002b10`. Commands and the init/keepalive sequence are written to `00002b11`.

---

### BLE Advertising & Device Discovery

The device continuously broadcasts BLE advertisements that the Cleanergy app uses to discover and identify it. Confirmed from a btsnoop HCI capture (`bugreport19/btsnoop_hci.log`) of the official app's initial device setup flow.

**Advertisement packets:** Two distinct AD payloads are broadcast (one ADV_SCAN_IND + one ADV_SCAN_RSP):

| AD Structure | Type | Content |
|---|---|---|
| Flags | `0x01` | `0x06` (LE General Discoverable, BR/EDR Not Supported) |
| Incomplete 16-bit UUIDs | `0x02` | `0xA201` |
| Service Data (UUID=0xA201) | `0x16` | 2-byte header + `device_product_id` (6 bytes ASCII) + reversed MAC (6 bytes) |
| Complete Local Name | `0x09` | `TT` |
| Manufacturer Specific Data | `0xFF` | See structure below |

**Manufacturer data structure** (25 bytes including AD type):

```
Byte  0:     Flag (0x00 or 0x01 — observed toggling; possibly pairing state)
Bytes 1-10:  device_id (10 raw bytes → 20-char hex string)
Bytes 11-16: device_product_id (6 ASCII bytes, e.g. "O44A5o")
Bytes 17-22: MAC address in reverse byte order
Bytes 23-24: Zero padding
```

> **Example:** Raw manufacturer payload (after company_id extraction):
> `<device_id_bytes_2_10><product_id_hex><reversed_mac_hex>00`
>
> - device_id bytes `[XX] YY YY YY YY YY YY YY YY YY` → `"<your_20_char_device_id>"`
>   (the first byte is consumed by the BLE company_id field as its high byte)
> - product_id: `4f343441356f` → `"O44A5o"`
> - reversed MAC: `XX XX XX XX XX XX` → `XX:XX:XX:XX:XX:XX`

---

### Obtaining the `device_key`

The `device_key` is required for BLE init packet 6 and for the WiFi cloud protocol. **It is NOT present in the BLE advertising data.**

The `device_key` is **account-scoped** — derived from the user's cloud UID and shared across all devices on the same account. Confirmed from `KeyUtils.java` in the Cleanergy APK:

```java
// KeyUtils.java
public static String createDeviceKey(String userId) {
    return MD5Util.encodeMD5(userId).substring(0, 10);
}
```

Example: uid=`60859` → `MD5("60859")[:10]` = `bd236b1695`. The same key was observed in both the Mega 1 and Mega 2 broker sessions for the same account.

The cloud server stores this key and returns it via `/api/app/device/list`. It is also held in the app's local MMKV storage (`/data/user/0/com.cleanergy.app/files/mmkv/http_mmkv`). The internal device config JSON (confirmed from Android bugreport logcat):

```json
{
  "addtime": 0,
  "batteryPower": 0,
  "device_id": "<your_device_id>",
  "device_key": "<your_device_key>",
  "device_product_id": "O44A5o",
  "firmware_version": "1.2.0",
  "img": "http://static.upspowerstation.top/upload/image/...",
  "mac_address": "<your_mac_address>",
  "name": "MEGA1",
  "online": 0,
  "shellyId": -1,
  "uid": 0,
  "updatetime": 1775532103
}
```

**Discovery flow (confirmed from btsnoop19 + logcat timeline):**

```
1. App performs BLE scan filtering for service UUID 0xA201
2. TT device seen → app extracts device_id from manufacturer data
3. App calls cloud API (api.upspowerstation.top) with device_id
   → cloud returns device_key, product_id, firmware_version, product image, etc.
4. App stores config locally in MMKV
5. App connects BLE → GATT discovery → CCCD enable → handshake
6. Device responds to handshake with: device_id + product_id + reversed MAC
7. App sends init packets including device_key in packet 6
8. Telemetry streaming begins
```

**The `device_id` in the advertising data provides a path to automate `device_key` retrieval** via the cloud API. The HA integration does this automatically during setup:

#### Automated retrieval (recommended)

The HA integration's config flow offers three methods to provide the device key during setup:

1. **Create New Key** — pairs the device directly over BLE using the [pairing protocol](#ble-pairing--re-keying-protocol) below. No prior knowledge of the key is needed — just factory-reset the device first (hold IoT button 5 s). This is the recommended method for new setups.
2. **Cloud Login** — logs in to the OUPES cloud API with your Cleanergy credentials, fetches the device list, and extracts the key. Credentials are used once and not stored.
3. **Enter Existing Key** — paste a key you already know from a previous setup or capture.

For cloud login, the full protocol is:

```
1. User enters Cleanergy email + password in HA integration setup
2. Integration POSTs to /api/app/user/login → receives session token
3. Integration GETs /api/app/device/list?token=... → receives all bound devices
4. Matches device by device_id (from BLE advertising) or MAC address
5. Extracts 10-char hex device_key → stores in config entry
6. Credentials are discarded — only the device_key is persisted
```

#### Manual retrieval (alternative)

If you prefer not to use cloud login, the key can be obtained manually:

1. **BLE packet capture:** Use nRF Connect, PCAPdroid, or btsnoop to capture the init sequence. Packet 6 (byte 0 = slot, byte 1 = `0x06`) contains the device_key at bytes 4–13 as ASCII hex.
2. **Android bugreport:** Enable BT HCI snoop log, pair via the Cleanergy app, take a bugreport. The device config JSON appears in logcat with tag `TAG` and a `888...==` prefix.
3. **MMKV extraction:** On a rooted device, read `/data/user/0/com.cleanergy.app/files/mmkv/http_mmkv` directly.

---

### Connection Sequence

The device requires a specific handshake before it will sustain a session and stream telemetry. Confirmed from HCI snoop captures (`btsnoop_hci.log`) of the Cleanergy app across multiple sessions.

```
1. BLE connect
   • The first 1–2 connection attempts may drop immediately in ~300 ms with
     HCI disconnect reason 0x3e ("Connection Failed to be Established").
     This is a normal cold-probe behaviour — simply retry.

2. Wait ~1.8 s
   • Android's BLE stack spends ~1.8 s performing GATT service discovery
     before writing CCCD.  Replicating this delay is required — writing CCCD
     immediately at t=0 produces different (broken) device behaviour.

3. Subscribe: Write CCCD 0x0100 to handle 0x0006 (enables notifications)
   • In bleak: client.start_notify(CHAR_NOTIFY, handler) handles CCCD internally.

4. Wait ~200 ms then send the 11-packet init sequence to write char (0x0003)
   See Init Sequence section below.

5. Device responds with 3x 0x80 handshake ACK packets:
     RX: 0180 0101 00 423054cc3f 0058c93f 8417ca3f 02 0b
     RX: 0180 0a00 01 000000000000000000000000000000 64
     RX: 0180 0a00 01 000000000000000000000000000000 64
   After these, telemetry data packets begin streaming automatically.

6. Send keepalive every 10 s to prevent session timeout
   TX: 0180030254010000000000000000000000000076
   • First keepalive at ~6 s after init (matching app timing).
   • Device echoes the keepalive back as an ACK notification.
   • Without keepalive the device disconnects at exactly t+10 s.
```

---

### Init Sequence

Sent as 11 write-without-response commands to the write characteristic (`00002b11`, handle `0x0003`), each 20 bytes. Captured from the Cleanergy app via Android HCI snoop log.

Packet 7 (index 6) contains the `device_key` at bytes 4–13 (ASCII, zero-padded to 10 bytes). This token is **stable across sessions** — it is the same `device_key` used in the WiFi cloud protocol.

```python
APP_INIT_SEQUENCE = [
    bytes.fromhex("0100019901010101010101010101010101010192"),
    bytes.fromhex("010101010101010101010101010101010101018f"),
    bytes.fromhex("0102000000000000000000000000000000000082"),
    bytes.fromhex("01030000000000000000000000000000000000a8"),
    bytes.fromhex("010400000000000000000000000000000000007e"),
    bytes.fromhex("0105000000000000000000000000000000000054"),
    bytes.fromhex("01060000000000000000000000000000000000XX"),  # bytes 4-13 = device_key ASCII (replace with yours)
    bytes.fromhex("0107000000000000000000000000000000000000"),
    bytes.fromhex("0108000000000000000000000000000000000081"),
    bytes.fromhex("01890000000000000000000000000000000000c0"),
    bytes.fromhex("0180020101000000000000000000000000000016"),
]

# Session keepalive — send every 10 s, first send at ~6 s after init completes
KEEPALIVE = bytes.fromhex("0180030254010000000000000000000000000076")
```

> **Device-specific token:** Init packet 7 (index 6) must contain your device's `device_key` at bytes 4–13 as 10-character ASCII hex. The HA integration builds this packet automatically via `build_init_sequence(device_key)` in `protocol.py`. Replace the zeros in the example above with your key’s hex encoding. Obtain your key via the cloud login API (see below) or from a btsnoop/PCAPdroid capture.

Subscribe to notifications on `00002b10` — the device pushes telemetry packets continuously after the handshake is complete.

---

### BLE Pairing / Re-keying Protocol

The Cleanergy app uses a multi-phase BLE handshake to pair a new device or
re-key an existing one. The sequence was reverse-engineered from btsnoop HCI
captures and confirmed live against hardware.

**Prerequisite — factory reset:** Press and hold the IoT button for **5 seconds**
until the LED changes to rapid flashing. This clears the stored key and puts
the device into pairing mode. Without this, the CLAIM sequence is rejected.
After the reset the IoT module turns off — press the IoT button once to turn
it back on before proceeding.

All packets are **20 bytes**, with byte 19 = CRC-8/SMBUS (poly `0x07`, init `0x00`)
over bytes 0–18.

#### Phase 1 — AUTH (11 packets, slot `0x01`)

The 11-packet AUTH sequence is identical to the normal init sequence (see above),
with the device key embedded in packet 6 at bytes `[4:14]` as 10-byte ASCII hex.
Packets are sent with ~80 ms inter-packet delay.

#### Phase 2 — Handshake polling (slot `0x03`, ~5 s)

After AUTH, send timestamp probe packets every 300 ms for ~17 iterations:

```
Byte:   [0]   [1]   [2:3]   [4:7]            [8:18]  [19]
        0x03  0x80  0x0004  <unix_ts LE u32>  zeros   CRC
```

The device responds via notifications. Response decoding:

| Pattern | Meaning |
|---------|---------|
| `[0]=0x03, [1]=0x80, [2]=0x01, [4]=0x00` | **GO** — ready to accept CLAIM data |
| `[0]=0x01, [1]=0x80, [2]=0x01, [4]=0x00` | **Configured** — device already has this key |
| `[0]=0x01, [1]=0x80, [2]=0x01, [4]=0x03` | **Wait** — device unconfigured, not yet ready |
| `[0]=0x01, [1]∈{0x00,0x81}, [2]=0x0A` | **Telemetry** — device streaming data (already paired) |

#### Phase 3 — Second AUTH + poll

Send one `0x01`-slot timestamp packet, then resend the full 11-packet AUTH
sequence, followed by another round of ~17 handshake polls. The Cleanergy app
always double-sends AUTH — this mirrors that behaviour.

#### Phase 4 — CLAIM (10 packets, slot `0x03`)

The CLAIM sequence writes the device key and WiFi credentials. The 40-byte
blob (`key[10] + wifi_psk + wifi_ssid`) is spread across packets 6, 7, and 8.
The Cleanergy app sends the user's real WiFi SSID and PSK so the device can
connect to the cloud broker. For BLE-only operation, a 30-byte dummy string
is used instead.

| Pkt | Byte[1] | Content |
|-----|---------|---------|
| 0 | `0x00` | Header: `0x99` flag, bytes 3–18 = `0x02` padding |
| 1 | `0x01` | All `0x02` padding |
| 2–5 | `0x02`–`0x05` | Zero payload |
| 6 | `0x06` | `[4:14]` = key, `[14:19]` = wifi blob bytes 0–4 |
| 7 | `0x07` | `[2:19]` = wifi blob bytes 5–21 (PSK continuation) |
| 8 | `0x08` | `[2:10]` = wifi blob bytes 22–29 (SSID), rest zeros |
| 9 | `0x89` | Terminator (high bit set) |

**WiFi credential layout** (confirmed from btsnoop captures of real Cleanergy app pairing):

The 30-byte wifi blob = `PSK_prefix(5) + PSK_body(17) + SSID(8)`, split as:
- Packet 6 bytes `[14:19]`: first 5 bytes of WiFi PSK
- Packet 7 bytes `[2:19]`: next 17 bytes of WiFi PSK (total PSK = 22 chars)
- Packet 8 bytes `[2:10]`: WiFi SSID (up to 8 chars, null-padded)

Example from a real capture: PSK = `siimPDqjxJCRBelB2cf10I[` (22 chars), SSID = `Lo43Lyvk` (8 chars).

For BLE-only / local-only operation (no cloud push), use a dummy string:
`HALOCAL000000000000000000000000` (30 ASCII bytes).

Packets are sent with ~50 ms inter-packet delay.

#### Phase 5 — Keepalive + confirmation

Send the keepalive packet and wait 5–10 s. Success is indicated by any of:
- `claim_accepted` response (slot `0x03` GO pattern)
- `auth_configured` response (slot `0x01` configured pattern)
- Telemetry packets arriving (device is streaming data)

If none of these occur, disconnect, wait 3–5 s, and reconnect for a new cycle.
The Cleanergy app uses the same multi-cycle reconnect approach.
Typical pairing completes in a single cycle (~18 seconds).

---

### Packet Format

Each BLE notification is a 20-byte packet:

```
[0x01][type][TLV data...][checksum]
```

| Byte | Meaning |
|------|---------|
| `0x01` | Always `0x01` — packet start marker |
| `type` | Packet type / continuation flag (see below) |
| `...` | TLV-encoded attribute data |
| Last byte | Checksum (XOR or sum of body bytes) |

**Packet types:**

| Type | Meaning |
|------|---------|
| `0x00` | First (or only) packet in a TLV data group |
| `0x01` | Continuation of a TLV data group |
| `0x80` | Handshake / keepalive ACK packet (non-TLV format) |
| `0x81` | Handshake continuation — **also carries standard TLV data** |
| `0x82` | End-of-group marker (body is all zeros) |

**TLV encoding within the packet body:**

Each attribute is encoded as:
```
0x0A  <length>  <attr_number>  <value bytes (little-endian)>
```

Example — parsing `0A 02 03 64` → marker `0A`, length `2`, attr `0x03` (3 = battery%), value `0x64` = 100%.

**Python parser:**

```python
def parse_ble_packet(data: bytes) -> dict:
    results = {}
    i = 2  # skip header bytes
    while i < len(data) - 1:  # skip checksum
        if data[i] == 0x0A and i + 2 < len(data):
            length = data[i + 1]
            if length >= 1 and i + 2 + length <= len(data) - 1:
                attr = data[i + 2]
                val_bytes = data[i + 3:i + 2 + length]
                results[attr] = int.from_bytes(val_bytes, 'little') if val_bytes else 0
            i += 2 + length
        else:
            i += 1
    return results
```

The attr numbers in BLE packets are **identical** to the attr numbers used in the WiFi/cloud protocol — the same attribute map applies to both.

---

### BLE Output Control

The three output ports (AC, DC 12V, USB) are controlled by writing a single bitmask to attr 1 via the write characteristic. Confirmed by correlating HCI write commands from btsnoop captures with matching attr-1 notification values.

**Write command format (20 bytes):**
```
01 80 03 02 01 [BITMASK] 00 00 00 00 00 00 00 00 00 00 00 00 00 [CRC8]
```

| Byte | Role |
|------|------|
| `0x01` | Packet start |
| `0x80 0x03 0x02 0x01` | Command header (write attr 1) |
| `[BITMASK]` | New output state: OR of desired `OUTPUT_*_BIT` constants |
| `0x00 × 13` | Padding |
| `[CRC8]` | CRC-8/SMBUS over bytes 0–18 (poly=0x07, init=0x00) |

**Bitmask constants:**
```python
OUTPUT_AC_BIT    = 0x01   # bit 0 — AC inverter output
OUTPUT_DC12V_BIT = 0x02   # bit 1 — DC 12V cigarette-lighter output
OUTPUT_USB_BIT   = 0x04   # bit 2 — USB-A / USB-C combined output
```

**Confirmed captured writes:**
```
01800302 01 00 00...00 1e   # bitmask 0x00 = all off
01800302 01 01 00...00 fb   # bitmask 0x01 = AC only
01800302 01 04 00...00 83   # bitmask 0x04 = USB only
01800302 01 05 00...00 66   # bitmask 0x05 = AC + USB
01800302 01 07 00...00 ab   # bitmask 0x07 = AC + DC12V + USB (all on)
```

**Python helper:**
```python
def _crc8(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc

def build_output_command(bitmask: int) -> bytes:
    pkt = bytearray(20)
    pkt[0], pkt[1], pkt[2], pkt[3], pkt[4] = 0x01, 0x80, 0x03, 0x02, 0x01
    pkt[5] = bitmask & 0xFF
    pkt[19] = _crc8(bytes(pkt[:19]))
    return bytes(pkt)

# Examples:
build_output_command(OUTPUT_AC_BIT | OUTPUT_USB_BIT)  # turn on AC + USB, leave DC12V off
build_output_command(0)                               # all off
```

Write the result to `WRITE_CHAR_UUID` (`00002b11`) with `response=False`. To toggle one output without affecting the others, read the current attr-1 bitmask from the latest telemetry, set or clear the relevant bit, then send the new full bitmask.

---

### BLE Python Example

Requires the `bleak` library: `pip install bleak`

See [`scan_ble.py`](scan_ble.py) for the full implementation. Condensed version:

```python
import asyncio
from bleak import BleakScanner, BleakClient, BleakError

CHAR_NOTIFY = "00002b10-0000-1000-8000-00805f9b34fb"
CHAR_WRITE  = "00002b11-0000-1000-8000-00805f9b34fb"

APP_INIT_SEQUENCE = [
    bytes.fromhex("0100019901010101010101010101010101010192"),
    bytes.fromhex("010101010101010101010101010101010101018f"),
    bytes.fromhex("0102000000000000000000000000000000000082"),
    bytes.fromhex("01030000000000000000000000000000000000a8"),
    bytes.fromhex("010400000000000000000000000000000000007e"),
    bytes.fromhex("0105000000000000000000000000000000000054"),
    bytes.fromhex("01060000000000000000000000000000000000XX"),  # replace with your device_key
    bytes.fromhex("0107000000000000000000000000000000000000"),
    bytes.fromhex("0108000000000000000000000000000000000081"),
    bytes.fromhex("01890000000000000000000000000000000000c0"),
    bytes.fromhex("0180020101000000000000000000000000000016"),
]
KEEPALIVE = bytes.fromhex("0180030254010000000000000000000000000076")

def parse_ble_packet(data: bytearray) -> dict:
    results = {}
    if len(data) < 2 or data[1] == 0x82:
        return results
    i = 2  # skip 2-byte header
    while i < len(data) - 1:  # last byte is checksum
        if data[i] == 0x0A and i + 2 < len(data):
            length = data[i + 1]
            if length >= 1 and i + 2 + length <= len(data) - 1:
                attr = data[i + 2]
                val = data[i + 3:i + 2 + length]
                results[attr] = int.from_bytes(val, 'little') if val else 0
            i += 2 + length
        else:
            i += 1
    return results

async def main():
    results = await BleakScanner.discover(timeout=10, return_adv=True)
    device = next((d for d, _ in results.values() if (d.name or "").upper() == "TT"), None)
    if not device:
        print("Device not found")
        return

    disconnected = asyncio.Event()

    async with BleakClient(device.address, timeout=15,
                           disconnected_callback=lambda _: disconnected.set()) as client:
        await asyncio.sleep(1.8)                          # match Android GATT discovery delay
        await client.start_notify(CHAR_NOTIFY,            # writes CCCD 0x0100
            lambda _, d: print(parse_ble_packet(d)))
        await asyncio.sleep(0.2)
        for pkt in APP_INIT_SEQUENCE:                     # send handshake
            await client.write_gatt_char(CHAR_WRITE, pkt, response=False)
            await asyncio.sleep(0.01)

        async def keepalive_loop():
            await asyncio.sleep(6)                        # first keepalive ~6 s after init
            while not disconnected.is_set():
                await client.write_gatt_char(CHAR_WRITE, KEEPALIVE, response=False)
                await asyncio.sleep(10)

        ka = asyncio.create_task(keepalive_loop())
        try:
            await asyncio.wait_for(disconnected.wait(), timeout=3600)
        finally:
            ka.cancel()

asyncio.run(main())
```

---

## Telemetry Attribute Map

The following attr numbers apply to **both** the WiFi cloud protocol and the BLE protocol.

Confidence levels: ✅ Confirmed against app display | ⚠️ Likely but unverified | ❓ Unknown

| Attr | Dec | Meaning | Unit / Scaling | Confidence |
|------|-----|---------|----------------|------------|
| `0x01` | 1 | **Output enable bitmask** | bit0=`0x01`=AC, bit1=`0x02`=DC 12V, bit2=`0x04`=USB — value is OR of currently-enabled bits; live-confirmed values: `4`=USB only, `5`=AC+USB, `7`=AC+DC12V+USB (all on) | ✅ Confirmed via BLE write captures (see [BLE Output Control](#ble-output-control)) and live read telemetry |
| `0x02` | 2 | Unknown (possibly legacy output flag) | — | ❓ Superseded by attr 1 bitmask; original "DC output enabled" assumption unconfirmed |
| `0x03` | 3 | Battery percentage | `0`–`100` | ✅ App shows 100% |
| `0x04` | 4 | **Total Output Power** | Watts — sum of all active outputs (AC inverter + USB-C + USB-A + DC 12V); confirmed: with AC off and USB-C at 2W, attr 4 = 2W; with AC on at ~540W and USB at 2W, attr 4 ≈ 542W | ✅ Confirmed via AC-off test |
| `0x05` | 5 | **AC Inverter Output Power** | Watts — pure AC inverter output only; goes to exactly 0W when AC output is disabled regardless of USB/DC load; confirmed identical to attr 4 minus non-AC loads | ✅ Confirmed via AC-off test: attr 5 = 0, attr 4 = USB wattage |
| `0x06` | 6 | DC 12V Output power (cigarette-lighter port) | Watts | ✅ Output port, not input |
| `0x07` | 7 | USB-C Output power | Watts | ✅ |
| `0x08` | 8 | USB-A Output power | Watts | ✅ |
| `0x09` | 9 | Unknown | — | ❓ |
| `0x15` | 21 | Total Input Power (grid + solar) | Watts | ✅ Consistently 1W higher than attr 22 — accounts for solar MPPT noise floor |
| `0x16` | 22 | Grid Input Power (wall charge) | Watts, 1:1 raw | ✅ Confirmed vs app "Grid" display |
| `0x17` | 23 | Solar Input Power (MPPT) | Watts — MPPT circuit reports `1` intermittently even with nothing connected (confirmed in official app); `0` when grid is off/device idle; tested with a DC battery source on the solar port (real solar panel not available) | ✅ Tracks app SOLAR reading; confirmed the solar port accepts any DC input, not just panels |
| `0x1E` | 30 | Remaining Runtime (main unit) | **Minutes** (inaccurate under variable load; e.g. 5940 ≈ 99h shown when outputs off) | ✅ |
| `0x20` | 32 | **Main Unit Temperature** | ÷10 = °F (e.g. raw 963 → 96.3 °F at idle) | ✅ Confirmed — raw ~960 at idle matches app temperature display |
| `0x33` | 51 | **Connected Expansion Battery Count** | Number of B2 expansion batteries currently plugged in (0 = none, 2 = two batteries, etc.) — transitions to exactly 0 the instant batteries are disconnected; was constant=2 across all captures while both B2 batteries were connected | ✅ Confirmed via live battery disconnect event |
| `0x35` | 53 | **B2 Expansion Battery — Input Power** | Watts — power entering the B2 via its secondary MPPT/DC port (solar panel or DC source). The app labels this "INPUT W". Also non-zero when the same port is used as a DC 12V output (bidirectional converter); firmware reports absolute watts regardless of direction. 0 when no solar/DC connected to that port. | ✅ Confirmed: app INPUT W = 0W when attr 53 = 0; matches solar ramp (2→32W) and DC 12V output test (17→67W) |
| `0x36` | 54 | **B2 Expansion Battery — Output Power** | Watts — total power leaving the B2 to loads. The app labels this "OUTPUT W". Covers both chain-cable discharge to the Mega (~100W during normal discharge) and USB/accessory ports. When the Mega is on grid and not drawing from the B2, chain = 0 and only USB draw is visible (~3–6W). | ✅ Confirmed: app OUTPUT 101W on Backup Battery 2 = attr 54 slot 2 ≈ 99–105W (app screenshot at 12:58 vs CSV data) |
| `0x4E` | 78 | **Per-module multiplexed field** (slot-indexed by attr 101) | **Three distinct ranges:** (1) `0–6000` = remaining runtime in **minutes** (5940 = normal max; sentinel values in the tens of thousands when at 100% SoC); (2) `44000–61000` = per-module battery **voltage in mV** (÷1000 = V; fast-charge terminal peak up to 60.050 V; float-charge pulses up to 57.3 V); (3) `6001–43999` = **per-module float-charge measurement** (see Notes) — emitted continuously while SoC=100% and grid connected, only on the actively-charging module | ✅ All three ranges confirmed in extended live session |
| `0x4F` | 79 | **External Battery SoC** | Direct battery % (raw value = %; e.g. raw 15 = 15%) | ✅ Confirmed: raw 15 reported at ~15% after a few hours of charging |
| `0x50` | 80 | **External Battery Temperature** (slot-indexed by attr 101) | ÷10 = °F (e.g. 878 → 87.8 °F) | ✅ Confirmed vs app temperature display |
| `0x54` | 84 | AC output control (**write only**) | `1` = on, `0` = off — device never broadcasts this attr back; actual AC state is read from attr 1 bit 0 | ⚠️ Observed in app control capture; never received in BLE notifications |
| `0x65` | 101 | Battery slot index | `1` or `2` (or higher with multiple B2 batteries) — identifies which connected expansion battery attrs 78/79/80 belong to in a given packet | ✅ |
| `0x69` | 105 | **Charge Mode** | `0` = Slow charge, `1` = Fast charge (default/pre-seed). Writable via Cmd3. Confirmed from `S2_V2SettingFragment` and `D2SettingFragment` in the APK. Applied universally across Mega 1/2/3/5 and Guardian series. The earlier "AC Inverter Protection" interpretation was incorrect — that was based on correlating a `1` reading with thermal events on the Mega 1, but the value is simply the charge mode state. | ✅ Confirmed from APK source; Mega 1 observed `1` at rest = Fast mode default |

> **Note:** The main unit temperature is attr 32 (÷10 = °F, confirmed fixed in °F regardless of app unit setting — btsnoop across F→C→F app switch confirmed raw value does not change). The main unit remaining runtime is attr 30 (minutes; **sentinel values in the tens of thousands at 100% SoC** — observed values include 39,689 and 46,956; normal max during charge/discharge is 5,940). Attr 79 is direct battery % (raw value = %, confirmed: raw 15 = 15% observed during charging; bounces rapidly between 99 and 100 at the SoC=100% boundary due to ADC noise). Attr 80 is battery module temperature ÷10 in °F, confirmed against app display.

---

## App Display Reference

Screenshots of the Cleanergy app were used to calibrate attr values. The app displays the following fields:

| App Display | Value at Capture | Confirmed Attr |
|-------------|-----------------|----------------|
| Battery % | 100% | attr 3 ✅ |
| Temperature (main unit) | ~96 °F at idle | attr 32 ÷10 ✅ |
| Remaining time (main unit) | Minutes (5940 shown when outputs off/low) | attr 30 ✅ |
| Total output watts | 494–502W | attr 4 ✅ |
| AC output watts only | 494–502W (0W when AC off) | attr 5 ✅ |
| DC 12V output watts | Watts | attr 6 ✅ |
| USB-C output watts | Watts | attr 7 ✅ |
| USB-A output watts | Watts | attr 8 ✅ |
| Grid input | 30W | attr 22 ✅ |
| Solar input | Watts (1 = MPPT noise floor; 0 when idle) | attr 23 ✅ |
| AC + Solar total input | Watts | attr 21 ✅ |
| Battery runtime (slot 1, 2, …) | Minutes (0–6000; >6000 = 100% SoC sentinel) | attr 78 (runtime range) + slot from attr 101 ✅ |
| Battery voltage (slot 2 only on Mega 1) | mV ÷ 1000 = V (e.g. 46310 → 46.310 V) | attr 78 (voltage range 44000–61000) + slot from attr 101 ✅ |
| Battery temperature (slot 1, 2, …) | ÷10 °F (e.g. 878 → 87.8 °F) | attr 80 + slot from attr 101 ✅ |
| Charge Mode (0=Slow, 1=Fast) | boolean | attr 105 ✅ (APK confirmed; pre-seed = 1) |

> The app also shows "Grid" and "Solar" as separate input sources on the flow diagram screen, suggesting there may be distinct attrs for each not yet fully identified.

---

## WiFi Local Port Investigation

The OUPES Mega 1 has an ESP32 with WiFi capability. During BLE pairing via the
Cleanergy app, a 30-char random binding token is transmitted in the CLAIM
sequence (see [Phase 4](#phase-4--claim-10-packets-slot-0x03) above) — originally
misidentified as WiFi SSID + PSK, but actually `generateRandomString(30)` from the APK.
How the device obtains its WiFi credentials is unknown. The device
connects to the Alibaba Cloud broker at `wp-cn.doiting.com:8896` and maintains a
`cmd=ping` / `cmd=pong` heartbeat. However, this cloud connection is **only active
while a phone is BLE-paired** — it drops immediately when BLE disconnects.

### What we know from capture analysis

| Source | Finding |
|--------|---------|
| Router pcap (`192.168.1.209`) | Device connects outbound to `wp-cn.doiting.com:8896` (TCP). Only `cmd=ping` / `cmd=pong` heartbeats observed — no telemetry data in the capture. |
| btsnoop (17 bugreports) | CLAIM packets 6/7/8 carry a 30-char random alphanumeric binding token (`generateRandomString(30)`), NOT WiFi credentials as originally assumed. Same token per pairing session; different token each session. |
| UDP broadcast (app) | App sends `{"cmd":0,"pv":0,"sn":"..."}` to `255.255.255.255:6095`. Device does **not** respond. One-way only. |
| Cloud TCP 8896 | Requires active BLE session. Device publishes `num=0` (no subscribers) when no phone is BLE-connected. Not viable for unattended HA use. |

### Open question: local API?

It's unknown whether the ESP32 firmware exposes any **local** TCP or UDP services
(HTTP, MQTT, telnet, custom protocol) while WiFi-connected. If it does, that
would enable a cloud-free WiFi communication path — potentially faster and more
reliable than BLE for Home Assistant.

### Port scan script

[`scan_wifi_ports.py`](scan_wifi_ports.py) scans the device's local IP for open
TCP ports (and optionally UDP). Run it while the device is WiFi-connected:

```bash
# Scan common ports (1-1024 + known IoT ports)
python debug_info/scan_wifi_ports.py 192.168.1.209

# Full 65535-port scan (takes a few minutes)
python debug_info/scan_wifi_ports.py 192.168.1.209 --ports 1-65535 --timeout 0.3

# Include UDP scan
python debug_info/scan_wifi_ports.py 192.168.1.209 --udp
```

**Important:** The device must be WiFi-connected when you run the scan. Use
the HA BLE integration's "Create New Key" config flow (which pairs and
provisions WiFi automatically), or use `pair_device.py` / `provision_wifi.py`
manually, then run the scan while the device is connected.

### Provisioning WiFi via BLE — Working

> **Correction (2026-04-13):** The earlier analysis in this section concluded
> that CLAIM packets carry random tokens, not WiFi credentials, and that WiFi
> provisioning via BLE was "not possible". **This was wrong.** The 30-byte
> field in CLAIM packets 6–8 DOES carry the WiFi SSID + PSK, padded to 30
> bytes. The confusion arose because the APK's `generateRandomString(30)` is
> used for a *different* code path (the `openId` binding token); the actual
> WiFi provisioning path calls `toSendConfigNetData` which encodes real
> credentials.

WiFi provisioning via BLE CLAIM packets is **fully working**. The HA WiFi proxy
integration (`oupes_mega_wifi_proxy`) implements this in its device sub-entry
flow under the "Generate new device key" option — it collects SSID/PSK and
passes them to `async_pair_device()` during BLE pairing. The BLE integration
(`oupes_mega_ble`) does not yet collect WiFi credentials in its config flow,
though the underlying protocol layer supports it (a future enhancement). The
standalone script [`provision_wifi.py`](provision_wifi.py) also demonstrates
the protocol.

**How it works:**

1. BLE connect → handshake → init sequence (as documented above)
2. CLAIM packets 1–5: pairing handshake (device_key exchange)
3. **CLAIM packet 6:** WiFi SSID (padded to 30 bytes)
4. **CLAIM packet 7:** WiFi PSK (padded to 30 bytes)
5. **CLAIM packet 8:** Confirmation / commit
6. Device disconnects BLE, connects to WiFi, and begins the SiBo bind → broker connect sequence

Five bugs were fixed in `protocol.py` to make this work:
- CRC-8 checksum calculation was incorrect
- Packet framing offsets were wrong
- SSID/PSK padding was not applied
- Slot byte was hardcoded incorrectly
- Packet length field did not account for the full payload

After successful provisioning, the device reboots its WiFi stack and connects
to the configured network within ~5–10 seconds. No factory reset or Cleanergy
app is required — the WiFi proxy integration handles the entire flow during
device setup.

---

## Firmware Update (OTA)

The OUPES firmware update system was reverse-engineered from the Cleanergy APK (v1.4.1) and confirmed by live API testing against the production server.

### OTA Architecture

The device uses a **WiFi-centric OTA** design. Firmware binaries are **never** pushed over BLE — the app sends the device a URL, and the device's ESP32 WiFi module downloads the binary independently.

```
┌─────────────┐    1. POST /api/app/ota/new_version    ┌──────────────────────┐
│  App / HA   │ ──────────────────────────────────────→ │  api.upspowerstation │
│  (client)   │ ←────────────────────────────────────── │      .top            │
│             │    Returns: {url, fs, sv, hash, target} │                      │
└──────┬──────┘                                         └──────────────────────┘
       │
       │  2. Cmd5 JSON via WiFi TCP   ┌─────────────────────┐
       │     (or cloud relay)         │  OUPES Device       │
       └─────────────────────────────→│  (ESP32 + MCUs)     │
                                      │                     │
                                      │  3. Device fetches  │
                                      │     firmware from   │
                                      │     URL in Cmd5     │──→ static.upspowerstation.top
                                      │                     │←── (firmware .bin file)
                                      │  4. Applies update, │
                                      │     reports progress│
                                      └─────────────────────┘
```

**Three communication paths for Cmd5:**

| Path | Transport | When Used |
|------|-----------|-----------|
| Local WiFi TCP | Direct TCP to device's localhost port | App and device on same LAN |
| Cloud relay | TCP via `wp-us.doiting.com:8896` → device | Remote / fallback |
| BLE signaling | BLE Cmd 99 (prepare), Cmd 0 (version query) | Pre-OTA handshake only — no data transfer |

**BLE is signaling-only for OTA.** The `SingleBleDevice.getCmd5()` method returns an empty string for binary data. The Jieli BLE-OTA stack exists in the codebase but is for audio/SPP devices on a different product line.

### OTA Version Check API

```
POST http://api.upspowerstation.top/api/app/ota/new_version
Content-Type: application/json

{
  "product_id": "O44A5o",
  "sku": 0,
  "is_test": 0,
  "token": "<session_token>",
  "platform": "android",
  "lang": "en"
}
```

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `product_id` | string | 6-char device product ID (e.g. `O44A5o` = Mega 1) |
| `sku` | int | Stock keeping unit (observed as `0` for all queries) |
| `is_test` | int | `0` = production releases, `1` = test/beta firmware |
| `token` | string | Auth token from `/api/app/user/login` |

**Response** (when firmware is available):

```json
{
  "ret": 1,
  "desc": "succes",
  "info": {
    "id": 21,
    "product_id": 79,
    "status": 1,
    "version_num": 23,
    "sv": "0.2.4",
    "ota_id": "[{\"id\":51,\"sv\":\"0.0.5\",\"target\":161}, ...]",
    "desc": "esp32",
    "sku": 0,
    "is_test": 1,
    "ota": [
      {
        "target": 161,
        "fs": 61284,
        "hash": "c5da5bf17c08463...",
        "sv": "0.0.5",
        "version_num": 49,
        "url": "http://static.upspowerstation.top/upload/file/20260301/de63337b_D2_INV_2500W.bin",
        "sku": 0
      },
      {
        "target": 255,
        "fs": 1235168,
        "hash": "0",
        "sv": "0.4.8",
        "version_num": 91,
        "url": "http://static.upspowerstation.top/upload/file/20260318/16aa8659_wifi.bin",
        "sku": 0
      }
    ]
  }
}
```

When no firmware is available, `info` is an empty string `""`.

**Live API test results (2026-04-10):**

| Product ID | Model | Production | Test |
|------------|-------|------------|------|
| `O44A5o` | Mega 1 | None | None |
| `YRWj81` | Mega 2 | None | None |
| `EFDayi` | Mega 3 | None | None |
| `JTEnK3` | Mega 5 | None | None |
| `Hr9Uhd` | Exodus 1200 | None | None |
| `pba1j6` | Exodus 1500 | None | None |
| `IDaSL8` | Exodus 2400 | None | None |
| `LtQmdj` | HP2500 (D2) | None | 4 boards (INV, BMS, DC, ESP32) |
| `uAsyax` | UPS 1200 | None | Inverter board |
| `QWlryl` | UPS 1800 | None | Inverter board |
| `oB6OKs` | Guardian 6000 | None | None |

The Mega series has no firmware currently posted on the OTA server.

### OTA Command Protocol (Cmd5)

The app triggers the firmware download by sending a Cmd5 JSON payload to the device via WiFi TCP.

**Pre-OTA BLE handshake:**

1. App sends **BLE Cmd 99** — tells device to prepare for OTA
2. App sends **BLE Cmd 0** — queries current firmware version (response = `BleCmd0Result`)
3. App navigates to `FirmwareUpgradeFragment2`

**Cmd5 payload (single board):**

```json
{"url": "http://static.upspowerstation.top/.../firmware.bin", "fs": 1235168, "sv": "0.4.8"}
```

**Cmd5 payload (multi-board / module update):**

```json
{
  "url": "http://static.upspowerstation.top/.../D2_INV_2500W.bin",
  "fs": 61284,
  "sv": "0.0.5",
  "target": 161,
  "module_addr": 5000,
  "hash": "c5da5bf17c0846379099eed25edefb0204605e2a47486b3a30036c47c4abddd9"
}
```

**Multi-file format (all boards at once):**

```json
{
  "total": 3,
  "files": [
    {"url": "...", "type": 161, "size": 61284, "vs": "0.0.5"},
    {"url": "...", "type": 163, "size": 42364, "vs": "0.0.3"},
    {"url": "...", "type": 255, "size": 1235168, "vs": "0.4.8"}
  ]
}
```

**Device response flow:**

| Step | Direction | Content |
|------|-----------|---------|
| Cmd5 sent | App → Device | JSON with firmware URL |
| `ota_code: -12` (=244 unsigned) | Device → App | "Command received, ready to update" |
| Progress type 1 | Device → App | Download progress (device fetching from URL) |
| Progress type 2 | Device → App | Flash/apply progress |
| Complete / Error | Device → App | Final status or OTA error code |

The app retries sending the Cmd5 payload every 1 second for up to 10 seconds until it receives the `ota_code: -12` acknowledgment.

### Board Targets

Each OUPES device contains multiple independently-updatable boards:

| Target ID | Board | Description |
|-----------|-------|-------------|
| 161 | Inverter | Main AC inverter MCU (GD32F303 ARM Cortex-M) |
| 162 | PV | Solar MPPT controller |
| 163 | BMS | Battery management system (GD32F303) |
| 164 | DC | DC output controller (GD32F303) |
| 255 | ESP32 | WiFi/BLE communication module |

Target priorities (from `FirmwareUpgradeFragment2`):
- Priority 0: Target 255 (ESP32 — updated first)
- Priority 1: Targets 161, 162 (Inverter and PV)
- Priority 2: Target 163 (BMS — updated last)

### Firmware Binary Format

Firmware files served by the OTA server are **unencrypted, unsigned raw binaries**:

- **ESP32 (target 255):** Standard ESP-IDF flash image. Magic byte `0xE9` at offset 0. Example: `wifi.bin` (1.2 MB). These can be analyzed with `esptool.py image_info`.
- **MCU boards (targets 161–164):** Raw ARM Cortex-M vector tables for GD32F303 (STM32-compatible). Starts with the initial stack pointer at offset 0 (e.g. `E0 19 00 20` = SP @ `0x200019E0`) followed by exception vectors. Standard `.bin` format produced by `arm-none-eabi-objcopy -O binary`.

Integrity verification uses **SHA-256 hash** only (the `hash` field in `OtaVersionBean`). There is no cryptographic signature verification — the device trusts whatever binary it downloads from the URL provided in the Cmd5 command.

### DNS Redirect Attack Surface

Because the firmware update protocol relies on unencrypted HTTP URLs and has no signature verification, a DNS redirect is a viable method for serving custom firmware:

1. **Point `static.upspowerstation.top` to a local server** (via DNS override, hosts file, or router DNS)
2. **Serve a custom firmware binary** at the same URL path the device expects
3. **Trigger the update** either via:
   - The official Cleanergy app (if an OTA entry exists on the server)
   - Direct Cmd5 JSON sent via WiFi TCP to the device's local port
   - Cloud relay via the TCP 8896 broker

**Requirements for DNS redirect approach:**

| Requirement | Details |
|-------------|---------|
| DNS control | Override `static.upspowerstation.top` resolution |
| HTTP server | Serve the .bin file at the expected path |
| Firmware binary | A valid ESP32 or GD32 binary for the target board |
| SHA-256 hash | Must match the hash sent in the Cmd5 command (or the device may not verify — untested) |
| Cmd5 trigger | Either use the app or send the JSON directly |

**Alternative: Local WiFi TCP direct.** If you know the device's local TCP port (obtained during BLE pairing), you can send the Cmd5 JSON directly without DNS redirection, pointing the URL at any HTTP server on the local network.

> **Caution:** Flashing incorrect firmware can brick the device. The MCU boards (inverter, BMS, DC) control high-voltage power electronics. Only flash firmware you have verified is correct for your specific hardware revision.

---

## Notes & Unknowns

- **Attr 30 = main unit remaining runtime in minutes.** Values are inaccurate under variable load. Goes **above 6000** specifically at 100% SoC (float-charge sentinel) — treat any value >6000 as "fully charged". Normal max during charge/discharge cycle is 5940.
- **Attr 32 = main unit temperature ÷10 in °F.** Confirmed: raw ~960 at idle = ~96 °F, consistent with app display. The earlier "probably °C" hypothesis was wrong — 96 °F is a perfectly reasonable idle temperature for the inverter internals. Temperature reports in discrete firmware steps (e.g. 956=95.6°F, 949=94.9°F, 942=94.2°F, 935=93.5°F, 928=92.8°F); during sustained 500W discharge the reading pegged at 956 for several hours then stepped down gradually as the ambient cooled.
- **Attr 78 = per-slot multiplexed field (slot-indexed by attr 101).** Three value ranges carry distinct data: runtime in minutes (≤6000; 5940 = normal max during charge/discharge), voltage in mV (44000–61000; ÷1000 = V; fast-charge peak up to 60.050 V, float-charge up to 57.3 V), and a live float-charge measurement (6001–43999; continuously emitted at SoC=100% with grid on — see Notes). On current Mega 1 firmware, only slot 2 broadcasts voltage readings in attr 78; slot 1 does not — confirmed in 19-hour log: slot 1 had zero readings ≥44000 mV across 28,000+ observations while slot 2 had 1,242 voltage readings. The Voltage entity for slot 1 shows Unavailable.
- **Attr 79 = External Battery Charge (direct percentage).** Raw value = battery %; confirmed raw 15 = 15% observed during charging; raw 0 = 0% confirmed at end of full discharge-to-zero session. The integration reports this as-is with no scaling.
- **Attr 80 = External Battery Temperature ÷10 in °F.** Confirmed against app temperature display (e.g. raw 878 → 87.8 °F). The earlier "section voltage" assumption was incorrect.
- **Attr 105 = Charge Mode (0 = Slow, 1 = Fast).** Writable via Cmd3; the HA integration exposes it as a "Fast Charge" switch on all Mega and Guardian series. The device default / pre-seed state is `1` (Fast). The earlier interpretation as an "AC Inverter Protection flag" on the Mega 1 was incorrect — correlating `1` readings with thermal events was coincidental since Fast mode is the default and the device was almost always in Fast mode during testing. **Confirmed:** APK source (`S2_V2SettingFragment`, `D2SettingFragment`) shows DPID 105 as the charge mode selection across all D2/S2 model families. Pre-condition for toggling: AC output must be off and AC input power = 0W (enforced in the HA integration via `HomeAssistantError`). a complete deep-discharge session (97%→0% SoC; BLE logging gap from 15%→0% during which the device ran unmonitored for ~2h43min) produced zero attr 105 events throughout. At the moment SoC=0% was first logged, attr 4=500W and attr 5=506W — the device was still actively outputting ~500W with all outputs on (attr 1=7) and attr 105=0. Peak temperature was 95.6°F (raw 956) across this entire run — no protection triggered. Earlier sessions that did trigger protection (raw attr 32 rising to 970 = 97.0°F) were almost certainly thermal: the device was in a warmer environment or enclosure. The protection threshold lies somewhere above 95.6°F / 35.3°C.
- **Attrs 21 and 22 are likely Total Input and Grid Input respectively.** Attr 21 is consistently exactly 1W higher than attr 22 across all captures and live readings (e.g. 36 vs 35, 30 vs 29). This is consistent with attr 21 = total charging input (grid + solar) and attr 22 = grid-only input. The 1W difference is the MPPT noise floor from attr 23 — confirmed to appear in the official app even with no panel connected, and confirmed in live CSV data (attr 23 = 1 during grid-on periods, 0 when grid is off). The original mapping (21 = Grid, 22 = Solar) was based on matching "Grid 30W" in the app against a value of 30, but attr 22 = 29 at that time is equally plausible as the actual grid reading. **Confirmed: attr 21 is a system-wide total that includes solar power entering connected B2 expansion batteries via their secondary MPPT ports.** 19-hour log (2026-04-05 23:42 – 2026-04-06 18:54) confirms this definitively at the 14:04–14:30 window: AC disconnected at 14:05 (attr 22 → 0); chain cable remained physically connected — attr 54 slot 2 briefly showed ~104W as the B2 chain-discharged into the Mega during the AC-off handover, then naturally dropped to 0 at ~14:07 as the solar MPPT took over. Attr 53 slot 2 and attr 21 then tracked each other in per-minute lockstep: 14:08: 7W, 14:09: 9W, 14:10: 31W, 14:11: 42W, 14:12: 50W, 14:13: 58W, 14:14: 93W, 14:15: 103W, stable ~102–107W until 14:30 when both simultaneously dropped to 0. Attr 22 and attr 23 remained 0 throughout the solar ramp. Attr 53 slot 1 = 0 throughout — slot 1 has no solar panel. Notable: attr 23 (main unit solar input) stayed 0 throughout — B2 secondary-port solar does NOT appear in attr 23; it only rolls up into attr 21 directly.
- **Solar port and attr 23 testing used a DC battery source,** not a real solar panel (no panel available). The port accepted DC input from the battery correctly; attr 23 reflected the input wattage as expected. Attr 23 = 1 (noise floor) is common during grid-on periods with nothing connected — this matches what the official app displays.
- **~~Cloud connection is BLE-dependent.~~** **CORRECTED (2026-04-13):** The device firmware maintains its own independent cloud TCP broker connection via the SiBo bind sequence (unbind → bind → TCP connect). It does NOT require an active BLE connection from the app. The earlier `num=0` observations were caused by the device not completing the SiBo bind (our intercepting HTTP server was returning an incomplete response missing the `tcp_ip`/`tcp_port` fields). With the correct bind response, the device connects to the broker within 300ms and streams telemetry continuously. The BLE connection from the app triggers a separate *app-side* broker subscription (`from=control`) which is relayed alongside the device's own telemetry stream.
- **Broker token appears long-lived.** The same token was observed across multiple separate capture sessions. It does not appear to be a short-lived session token.
- **Client keepalive is `cmd=keep`**, not `cmd=is_online`. The app sends `cmd=keep\r\n` and receives `cmd=keep&timestamp=...&res=1`. The `cmd=is_online` command is a separate per-device check sent every ~5 seconds by the app.
- **`num=0` vs `num=1`** in publish responses indicates whether the device received the message. `num=1` means the device is cloud-connected and got the request; `num=0` means it is not reachable via the broker.
- **UDP port 6095:** The app broadcasts `{"cmd":0,"pv":0,"sn":"...","msg":{}}` to `255.255.255.255:6095` but the device does not respond. One-way only, not useful for data retrieval.
- **`wp-cn.doiting.com` (8.135.109.78):** Serves two roles: (1) The **app** connects over TLS 443 at startup to obtain a broker auth token (encrypted, cannot be read without TLS interception). (2) The **device firmware** connects on plain HTTP port 80 for the bind/unbind sequence (sends device_id, device_key, product_id, firmware_version; receives tcp_ip/tcp_port for broker). For HA interception, pfSense NAT forwards both `:80 → HA:8897` (HTTP intercept server) and `:443 → HA:8898` (SiBo HTTPS mock).
- **Control via attr 84:** The app was observed sending `cmd=3` (write) with `attr=[84], data={"84":1}` — likely toggles AC output. Other control attrs are unconfirmed.
- **BLE connection is fully confirmed working.** The complete connection sequence (1.8s GATT delay → CCCD write → init sequence → keepalive every 10s) has been validated live against the device, receiving 72+ telemetry packets across a sustained 27s session. See [`scan_ble.py`](scan_ble.py).
- **BLE keepalive is mandatory.** Without sending `0180030254010000000000000000000000000076` at least once every 10 seconds, the device disconnects at exactly t+10 s. Confirmed across multiple btsnoop captures — the Cleanergy app sends it every 10 s starting ~6 s after init.
- **BLE init token = `device_key`.** Bytes 4–13 of init packet 7 (index 6) contain the `device_key` as a 10-character ASCII hex string. This is identical to the `device_key` used in the WiFi cloud protocol and is stable across all sessions.
- **Cold-probe drops are normal.** The first BLE connection attempt often drops in ~300 ms with HCI disconnect reason `0x3e` before any ATT data is exchanged. The Cleanergy app silently retries — the subsequent connection proceeds normally.
- **BLE packet type `0x80`:** The handshake ACK (`0180 0101 00 423054cc3f...`) does not follow TLV format. The device always sends exactly 3 of these in response to the init sequence. `0x81` packets that follow normal data groups **do** carry standard TLV data and decode identically to `0x01` continuation packets.
- **Attr 5 is the pure AC inverter output;  attr 4 is total output power.** Confirmed via AC-off test: turning off the AC output while USB-C runs at 2W → attr 5 goes to exactly 0W, attr 4 = 2W (matching USB-C wattage). When AC is active at high loads (~540W), the USB contribution (~2W) is negligible so the two values appear nearly identical, which led to the earlier incorrect conclusion that they are the same measurement. The correct interpretation: attr 4 = AC + DC 12V + USB-C + USB-A combined; attr 5 = AC inverter only.
- **Attr 1 live read values confirmed:** `4` (`0b100`) = USB only; `5` (`0b101`) = AC + USB; `7` (`0b111`) = AC + DC12V + USB (all on). These were observed in live telemetry during the overnight session, confirming the bitmask interpretation for all three output bits including DC12V. Transitional values also confirmed in the 19-hour log: `2` (`0b010` = DC 12V only), `3` (`0b011` = AC + DC 12V), `6` (`0b110` = DC 12V + USB) — appear briefly during AC switching transitions.
- **Attr 101 is the battery slot index** (`1`, `2`, …), identifying which expansion battery the accompanying attrs 78/79/80 data belongs to in a given packet. Slots 1 and 2 are the first two connected batteries; with additional B2 units the slot number increases.
- **Unpolled attr ranges:** The device silently ignores attrs it doesn't recognise. Main unit temperature (attr 32) and remaining runtime (attr 30) are now confirmed. Attr 2 is unclear — may be a legacy flag superseded by the attr 1 bitmask.
- **Attr 78 voltage readings** span two distinct charging states: (1) **fast-charge** (CC/CV bulk charging below 100% SoC): voltage reflects charging terminal voltage — observed peak 60.050 V (slot 2 at ~79–80% B2 SoC during rapid recharge in the 19-hour log); (2) **float-charge** (100% SoC trickle/balance): rapid oscillation ≈45–57 V (observed range 45.464–57.312 V) as the BMS pulses for cell balancing. Once bulk charging completes and pure discharge begins, all attr 78 readings revert to runtime values — no voltage readings are emitted during discharge. With 1,031+ readings in a 9-hour session and 1,242 in the 19-hour log, the voltage entity reliably holds recent readings.
- **Attr 78 multiplexes three distinct value ranges across the same attribute number.** Within a single BLE session it can carry: (1) runtime in minutes (≤6000), (2) per-module battery voltage in mV (44000–61000; ÷1000 gives volts in the LiFePO4 operating range ~44V–60V, fast-charge terminal peak up to 60.050V), or (3) mystery status codes (6001–43999). The active range changes within the session; both (1) and (2) were observed in the same live session. Range selection appears to be firmware-driven (not poll-controlled).
- **Attr 78 mystery value range (6001–43999) — REVISED:** Earlier analysis concluded these were initial-BLE-session-only status codes. **This was wrong.** Extended overnight logging (9 hours, 560K rows) showed 5,766 mystery observations with 283 distinct values — appearing continuously for hours while the device is at SoC=100% with grid connected. They are a **live fluctuating measurement** emitted in place of runtime during float-charge state, not one-time status codes. Observations: always SoC=100%; almost always grid-on (5,763/5,766); primarily slot 1 (5,742 of 5,766); values range continuously across the entire 6001–43999 window. The original 4 fixed values (6838, 16863, 27499, 37873) seen in the first session were coincidentally at a 100% SoC grid-connect moment and not representative. **Probable interpretation:** a per-module BMS measurement only computed during CV (constant-voltage) float-charge phase — candidates include instantaneous charging current (mA), per-module power (mW), or cell-balance data. Correlation with grid wattage is suggestive but not conclusive. A test with a known current source on the solar port and a logging multimeter would be needed to confirm the unit.
- **Attr 30 sentinel at 100% SoC:** Remaining runtime (attr 30) goes well above 6000 specifically when SoC = 100% and the device is in float-charge mode (observed values: 39,689 and 46,956 in different sessions). Normal charging/discharging max is 5,940. The value at 100% SoC is not consistent — treat any value >6000 as a sentinel meaning "fully charged / no meaningful runtime estimate". In the 19-hour log (1.17M rows), 3,454 float-charge readings above 5,940 were recorded spanning 6,940–46,956, with multiple distinct values within the same minute — firmware noise rather than a stable sentinel (runtime is undefined when fully charged). The integration correctly ignores any attr 30 > 5,940 during charging. At genuinely low SoC during discharge, attr 30 produces valid runtime estimates: at 15% SoC / 490W output, attr 30 = 93 minutes (actual time from 15%→0% at ~500W was ~2h43min, so the estimate was pessimistic at that load level). At 0% SoC (after AC was reconnected and charging began), attr 30 returns to 5940 sentinel — the sentinel applies whenever the device is in a charging state, regardless of whether SoC is near 0% or 100%. The firmware reports the full SoC range including true 0%: attr 3=0 was confirmed in telemetry at the moment of AC reconnect, with the device still actively outputting ~500W at that instant.
- **Attr 51 = Connected Expansion Battery Count.** Confirmed via live disconnect event: attr 51 = 2 continuously while both B2 batteries were connected; transitioned to exactly 0 one second after the last ext-battery telemetry burst (11:36:19 in the capture). Previously dismissed as "constant=2" because no disconnect had been captured. The firmware broadcasts this attr on every telemetry cycle alongside the main unit attrs.
- **Attr 54 = B2 Output Power (watts). Confirmed — matches app "OUTPUT W" field exactly.** Direct screenshot comparison: app showed "Backup Battery 2: OUTPUT 101W" while CSV attr 54 slot 2 = 99–105W simultaneously. This is the *total* power leaving the B2 from the B2's own perspective: chain-cable discharge to the Mega plus any USB/accessory port draws on the B2 itself. **Attr 54 is purely a B2-local measurement** — the Mega does not separately report the chain-cable power it receives from connected B2s as an "input" in attrs 21/22; those attrs only reflect actual external charging sources (grid AC, solar). The chain-cable power is topologically invisible to the Mega's input telemetry (the B2 discharges into the system bus and the Mega draws from it directly). During earlier USB-toggle tests the Mega was on AC grid (not drawing from B2 via chain), so chain = 0 and only USB draw (3–6W) was visible — which led to the incorrect "USB Output Power" label. The USB on/off switching pattern was real; it's just that USB was the only active output component at the time.
- **Attr 53 = B2 Input Power (watts). Confirmed — matches app "INPUT W" field.** 0 when no solar/DC source connected to the B2's secondary port (app shows INPUT 0W simultaneously). Non-zero during solar connect test (2→32W MPPT ramp) and DC 12V port activity (17→67W). The B2's secondary port (solar MPPT in / DC 12V cigarette-lighter out) is bidirectional — firmware reports absolute watts regardless of whether power is entering (solar charging) or leaving (DC 12V output). **Attr 53 is purely the B2's secondary port power — chain-cable energy is invisible to it.** Confirmed in the 19-hour natural discharge session (2026-04-06): AC disconnected at 14:05; chain cable left connected (briefly showing ~104W on attr 54 before naturally dropping to 0 at ~14:07 as solar MPPT took over); attr 53 slot 2 tracked attr 21 (Total Input) in per-minute lockstep (14:08: 7W → 14:15: 103W → stable ~102–107W until 14:30 when solar stopped) while attr 22 (grid) and attr 23 (main unit solar) remained 0 throughout. B2 secondary-port solar does NOT appear in attr 23; it rolls directly into attr 21. Arrives in the same BLE packet as attr 101 (slot index) and attr 54.
- **B2 solar slot assignment confirmed:** In a two-B2 setup, solar is exclusively on one slot. In the 19-hour log, attr 53 slot 1 was 0 across all 7,877 observations (max=0W) — slot 1 has no panel on its secondary port. Attr 53 slot 2 peaked at 108W — slot 2 is the solar-connected battery (the second battery in the chain, physically further from the Mega). Callers can inspect per-slot attr 53 readings at startup to identify which B2 has an active solar source.
- **Attr 54 chain-cable handover burst on AC disconnect:** When AC power cuts while a solar-equipped B2 is connected via chain, attr 54 briefly shows the full chain discharge power (~104W) for ~1–2 minutes before solar MPPT locks in and chain power naturally drops to 0. This is the normal AC-to-solar handover window — the B2 provides energy via chain until the MPPT circuit has tracked the panel and is generating sufficient power independently.
- **Expansion battery disconnect final burst:** When a B2 battery is removed, the firmware sends a final zeroed telemetry packet for that slot: attr 78=0, attr 79=0 (SoC), attr 80=320 (=32.0°F = 0°C sentinel), attr 101=slot number. This is a "battery removed" notification — the app uses these zeros to clear the battery display. The main-unit attr 51 transitions to the new battery count one telemetry cycle later.
- **B2 SoC divergence during extended discharge:** In the 19-hour log's final ~4.5-hour discharge (main unit 90%→24%), both B2 slots ended at 36–37% while the main unit ended at 24–25% — roughly a 12 percentage-point gap at end of discharge. Both B2 slots tracked each other identically throughout (in a two-B2 setup, both batteries share the chain and discharge in tandem). The divergence is expected: the Mega and B2 use separate SoC estimation algorithms across different cell capacities.
- **Attr 9 is main-unit-scoped** (not expansion battery related). It was present throughout the entire capture including after the expansion batteries were disconnected — unlike attrs 53/54/78/79/80 which ceased at disconnect. Value always 0 in all 37,000+ observations.
- **btsnoop bugreport18 cross-validation (2026-04-06 12:57–12:58).** The official Cleanergy app was connected for ~44 seconds while screenshots were taken. Direct cross-referencing of the btsnoop ATT notification stream against the app UI confirms: attr 54 slot 2 = 100–104W while app showed "Backup Battery 2 OUTPUT 101W" ✅; attr 53 = 0 while app showed "INPUT 0W" for both batteries ✅; attr 5 (AC-only output) = 493–544W while app showed AC 517–535W ✅; attr 4 (total output) = 594–644W = AC (~520W) + B2 chain discharge (~101W) ✅; attr 21/22 (total/grid input) = 443–453W / 442–452W while app showed GRID 452W ✅; attr 30 = 5940 min = 4d 3h ✅; attr 32 = 956 = 95.6°F ✅; attr 79 = 82–83% matching app B2 SoC ✅; attr 80 = 822 = 82.2°F matching app B2 temp ✅.
- **Attrs 81, 87, 107 (0x51, 0x57, 0x6b) — app requests but device never responds.** The official app sends a "subscribe to ext-battery attrs" write containing: 51, 53, 54, 78, 79, 80, 101, **81, 87, 107**. The last three were never observed in any device notification across 820K+ rows of CSV logging. They are likely Mega 2/3 or newer B2 firmware features not present in this hardware revision.
- **App keepalive: writes attr 84 (AC control=ON) every 10 seconds** while AC output is active. This is a periodic heartbeat from the app to the device — the device probably requires repeated confirmation to keep AC on, or the app simply re-asserts desired state on a timer. Our HA integration does not need to replicate this; the device firmware maintains state between writes.
- **App identity / `device_key` write on connect — per-device.** On each new BLE connection the app writes a 10-character hex token to the write characteristic before subscribing to notifications. This is byte-for-byte identical to the `device_key` used in the WiFi/cloud protocol and is embedded at bytes 4–13 of init packet 6. **This value is per-device**: a second user's Mega 1 did not connect when a different unit's key was used, confirming it is not universal across all units. Users must supply their own key (fetched automatically via cloud login during HA integration setup, or captured manually via btsnoop/PCAPdroid). The HA integration requires a valid device_key — setup will fail without one.
- **No standalone battery voltage attribute exists — confirmed exhaustively.** A 23-hour BLE logging session (2026-04-05 23:42 – 2026-04-06 22:56, 1,254,494 decoded attr rows, 357,752 raw packets) covering every major operating state — grid float-charge at 100% SoC, bulk charge from 0%→100%, full discharge from 100%→0%, solar-only operation, B2 hot-plug disconnect/reconnect — was scanned for any undiscovered attributes or voltage-like value ranges. **Results:** (1) Zero unknown TLV attribute numbers were found — every byte in every raw packet decodes to an already-documented attr; (2) No attribute other than attr 78 carries values in any plausible voltage range (checked mV, cV, dV, and V scalings for the entire LiFePO4 44–60V window); (3) Attrs 2 and 9 remain always-zero across all 60,427 main-unit poll cycles. The firmware simply does not expose a reliable battery voltage metric. The only voltage data available is attr 78 on slot 2 during active charging (see below).
- **Attr 78 voltage is charging-only and slot-2-only — comprehensively confirmed.** In the 23-hour session: slot 2 produced 1,242 voltage readings (44.324–60.050 V); slot 1 produced **zero** voltage readings across 29,325 observations. Voltage readings appeared in three windows: (1) the initial trickle/float state at session start (23:42–23:44, ~2 min), (2) a sustained float-charge period (03:44–09:31, ~6 hours of oscillation 44–57V at SoC=100%, grid ~28–43W), and (3) a brief fast-charge burst during B2 reconnection (12:23–12:24, ~30s, locked at 60.050V = CV clamp limit). 1,228 of 1,242 readings were at SoC=100% (float); only 13 were at SoC=95% (the fast-charge CV burst). During the entire 8.5-hour discharge (100%→0% main SoC, 100%→0% ext SoC), attr 78 reported only runtime estimates — no voltage readings were emitted at any point during discharge. **A persistent voltage entity is not feasible with the current firmware telemetry.**
- **Fast-charge CV voltage burst at B2 reconnect.** When the B2 expansion batteries were physically reconnected (12:23:39, SoC jumped from 0→79% as the slot came back online), slot 2 immediately began reporting 60.050V — the constant-voltage (CV) limit of the LiFePO4 charger. This reading persisted for ~30 seconds at grid power 446–662W, then switched back to runtime=5940 as the BMS transitioned to bulk CC mode. This is the only time voltage appeared during non-100% SoC in this entire 23-hour dataset.
- **Attr 51 hot-plug transitions captured.** The expansion battery count transitioned through multiple states: 2→0 at 11:36:19 (physical disconnect of both B2s), 0→1 at 12:11:01 (first B2 reconnected), 1→0 at 12:23:02 (brief dropout), 0→2 at 12:23:40 (both B2s fully reconnected). The final zeroed-packet pattern confirmed at each disconnect (attr 79=0, attr 80=320=32.0°F sentinel).
- **Full 0% SoC discharge captured on both main unit and ext batteries.** Main unit SoC reached true 0% at 22:33:46 while actively outputting ~500W at attr 1=7 (all outputs on). Ext battery SoC (slot 2) simultaneously dropped from 24%→0%. Grid was reconnected at the same moment (418W). Attr 78 runtime for both slots was 4–5 minutes at the moment of 0%. Temperature peaked at 96.3°F (raw 963) during the charging phase, dropped to 85.5°F (raw 855) overnight at lower ambient.
- **Slot 1 mystery values (attr 78 range 6001–43999):** 6,633 readings across the float-charge period. The dominant value is 27,499 appearing in ~73% of readings; occasional excursions span the full 6,097–40,088 range. No scaling maps these to a plausible voltage. The slot 2 mystery count was only 26 (vs 6,633 for slot 1), reinforcing that the mystery range is a per-module BMS measurement asymmetrically reported across slots — likely related to float-charge balancing current or cell-level data that only one BMS controller exposes.
- **`device_id` is embedded in BLE advertising data — confirmed from btsnoop19.** The manufacturer-specific data (AD type 0xFF) in the TT device's advertisement contains the full 20-char `device_id` at bytes 1–10 of the raw manufacturer payload, followed by the 6-byte ASCII `device_product_id` and the reversed MAC address. The first byte of the manufacturer data is a flag (observed as 0x00 or 0x01, possibly toggling pairing state). The BLE company_id (0x7501 in LE) is not a real Bluetooth SIG assignment — its high byte (`0x75`) is actually the first byte of `device_id`. **The `device_id` in ADV data could be used to query the cloud API for the `device_key`, potentially automating the setup process.**
- **`device_key` is NOT in advertising data.** Exhaustive search of all AD structures in 691 advertising reports from the TT device confirmed that the 10-char device_key does not appear anywhere in the advertisement payload. The key is obtained from the cloud API (`api.upspowerstation.top`) using the `device_id` from the advertising data, and stored locally in the app's MMKV storage.
- **`device_key` is account-derived, not device-unique.** Confirmed from `KeyUtils.java`: `createDeviceKey(userId) = MD5(userId)[:10]`. All devices bound to the same Cleanergy account have the same key (e.g. uid=60859 → `bd236b1695` on both Mega 1 and Mega 2). The cloud server stores and returns this value via `/api/app/device/list`. A second `createWifiBleDeviceKey(wifiDeviceKey)` variant (last 10 chars of the WiFi key) exists in the APK for WiFi-provisioned setups but is not used in normal broker sessions.
- **BT MAC = WiFi MAC + 2.** The device's Bluetooth MAC is always WiFi MAC + 2. Confirmed on Mega 1 (WiFi `8cd0b2a8e142`, BT `8cd0b2a8e144`) and Mega 2 (WiFi `ecda3b30b9c0`, BT `ecda3b30b9c2`). The bind request's `additional_detail.bt_mac` field also confirms this arithmetic.
- **`device_product_id` (`O44A5o`) appears in multiple contexts:** BLE service data (UUID=0xA201), BLE manufacturer data, BLE handshake response, and the cloud API JSON config. This 6-byte ASCII string is a per-product-model identifier (the same across units of the same model). It is distinct from the `device_key` (which is per-unit) and the `device_id` (which is per-unit).
- **BLE handshake response decoded.** After the app sends the initial 0x80/0x00 handshake packet, the device responds with two notification packets containing: (1) the full `device_id` as raw bytes, and (2) the reversed MAC address + `device_product_id` in ASCII. The app uses these to confirm it connected to the expected device before proceeding with the init sequence.
- **Cleanergy app uses two BLE slots:** The btsnoop19 capture shows the app initializing both slot 3 (first) and slot 1 (second). Slot 3 init packets carry the `device_key` plus an MQTT `client_id` string (29 chars spread across packets 6–8). Slot 1 init packets carry only the `device_key` (no MQTT data — zeros in packets 7–8). The slot 3 handshake is sent first, then slot 1. The HA integration's working init sequence uses only slot 1 — the slot 3 MQTT setup is only needed for cloud push functionality.
- **Cloud product catalog observed in logcat.** The Cleanergy app downloads a full product model catalog from the cloud on startup, containing SKU mappings and product images for all OUPES models: UPS-800, UPS-1200, UPS-1800, S2-V2, HP6000_V2, PB300, LP700, LP350, S024 Lite, S1 Lite, HP2500 (MEGA2PRO), Guardian 6000, DC 800, MEGA 5, MEGA2, MEGA3, MEGA1, Exodus 1200/1500/2400, shelly plug, shelly meter. This confirms the product line scope and suggests the protocol may be shared across the entire range.

---

## Device Settings — Write Commands (Cmd3)

The Cleanergy app writes device settings via a **Cmd3** protocol — distinct from
the output bitmask toggle (Cmd via `0180 0302 01 <bitmask>`) documented above.
Cmd3 is used for all configurable parameters: standby timeouts, ECO mode, silent
mode, breath light, etc.

**Source:** `SingleBleDevice.getBleToDeviceSetDevicePropertyCmd3()` in the
decompiled APK. Settings are sent as JSON `{"data": {"<DPID>": <value>}}` which
the device link layer serializes to BLE hex packets.

### WiFi/Cloud write format

Over the TCP 8896 cloud channel, settings are written with `cmd=3`:

```json
{
  "msg": {"attr": [47], "data": {"47": 600}},
  "pv": 0,
  "cmd": 3,
  "sn": "<timestamp_ms_string>"
}
```

### BLE write format (Cmd3 hex packets)

Each DPID value is encoded as a variable-length hex packet segment:

```
03  <total_len>  <dpid_hex>  <value_hex>
```

| Field | Description |
|-------|-------------|
| `03` | Fixed command type byte |
| `<total_len>` | Byte count of (dpid + value), 2-char hex |
| `<dpid_hex>` | DPID as 2-char hex (e.g. DPID 47 → `2F`) |
| `<value_hex>` | Integer value in little-endian hex, variable width (1–4 bytes, determined by `getIntByteSize()`) |

Multiple DPID segments are concatenated into a single BLE write. The BLE
transport splits the concatenated hex into 20-byte packets with the standard
`01 <slot> ...` framing and CRC-8 checksum.

**Example:** Set USB/Car standby timeout to 10 minutes (600 seconds):
```
DPID 47 (0x2F), value 600 (0x0258) → segment: 03 03 2F 5802
```
(Value is little-endian: 600 = 0x0258 → bytes `58 02`; total_len = 1 (dpid) + 2 (value) = 3)

### Writable Device Settings (DPIDs)

> **Status:** Reverse-engineered from APK UI fragments and BatteryData model.
> DPIDs are **unverified against hardware** unless noted. The Mega 1 may not
> support all DPIDs — some are for other models (D2, LP350, UPS, etc.).

#### Standby / Auto-off Timeouts

These control how long the device waits before automatically turning off an
idle output or entering sleep mode.

| DPID | BatteryData Field | Setting | Unit | Preset Values |
|------|-------------------|---------|------|---------------|
| 40 | `deviceStandByTime` (or similar) | **Device-level standby / car port timeout** (Mega 2 specific — observed written with `0` = disable and `86400` = 24 h by app immediately after subscribe) | Seconds | 0 (never), 86400 | 
| 45 | `standby_time` | **Machine standby** (whole device sleep) | Seconds | 0 (never), 3600, 10800, 21600, 43200 |
| 46 | `wifiStandByTime` | **WiFi standby** | Seconds | 0, 21600, 43200, 86400 |
| 47 | `usbCarCloseTime` / `usbStandByTime` | **USB/Car port standby** | Seconds | 600, 1800, 3600, 10800, 21600 |
| 48 | `xt90CloseTime` / `xt90StandByTime` | **XT90 port standby** | Seconds | 600, 1800, 3600, 10800, 21600 |
| 49 | `acCloseTime` / `acStandByTime` | **AC output standby** | Seconds | 600, 1800, 3600, 10800, 21600 |

> **Conversion note:** The app UI shows minutes/hours but sends **seconds** to
> the device (value × 60 from the UI). Value `0` = never auto-off.

#### ECO Mode (Low-load Auto-off)

| DPID | BatteryData Field | Setting | Unit | Values |
|------|-------------------|---------|------|--------|
| 110 | `AC_Eco_Switch` | **AC ECO mode switch** | Boolean | 0 = off, 1 = on |
| 111 | `AC_Eco_threshold` | **AC ECO power threshold** | Watts | 0–100 (below this → auto-off) |
| 112 | `DC_Eco_Switch` | **DC ECO mode switch** | Boolean | 0 = off, 1 = on |
| 113 | `DC_Eco_threshold` | **DC ECO power threshold** | Watts | 0–100 |
| 114 | `AC_Eco_Time` | **AC ECO auto-off delay** | Seconds | Time before AC ECO shuts off |
| 115 | `DC_Eco_Time` | **DC ECO auto-off delay** | Seconds | Time before DC ECO shuts off |

#### System Toggles

| DPID | BatteryData Field | Setting | Values |
|------|-------------------|---------|--------|
| 41 | `displayAutoCloseTime` | **Screen/display auto-off timeout** | Seconds (0 = never) |
| 58 | `breathLightSwitch` | **Breath light (LED ring)** | 0 = off, 1 = on |
| 61 | `frequencySwitch` | **50/60 Hz frequency select** | 0 or 1 |
| 62 | `bluetoothSwitch` | **Bluetooth on/off** | 0 = off, 1 = on |
| 63 | `silentMode` | **Silent mode** (fan speed reduction) | 0 = off, 1 = on |
| 64 | `nightMode` | **Night mode** (display dim + quiet) | 0 = off, 1 = on |
| 65 | `nightModeTime` | **Night mode duration** | Seconds |

#### Output State Memory

| DPID | BatteryData Field | Setting | Values |
|------|-------------------|---------|--------|
| 27 | `acDcMemDcSwitch` | **DC output memory** (restore state after power cycle) | 0 = off, 1 = on |
| 224 | `energySwitch` | **Energy management / memory function** | 0 = off, 1 = on |

> **Note:** DPID 27 may be a raw bitmask containing both AC and DC memory flags.
> The BatteryData model has separate `acDcMemAcSwitch` and `acDcMemDcSwitch`
> fields but they appear to be parsed from the same raw value (`acDcMemSwitchRaw`).

#### DFC Charger Timeouts (for DFC / external charger accessories)

| DPID | BatteryData Field | Setting | Unit | Preset Values |
|------|-------------------|---------|------|---------------|
| 218 | `dfc_charge_timeout` | **Charge timeout** | Minutes | 5, 30, 60, 360, 720, 1440 |
| 219 | `dfc_discharge_timeout` | **Discharge timeout** | Minutes | 5, 30, 60, 360, 720, 1440 |
| 220 | `dfc_save_timeout` | **Save/standby timeout** | Minutes | 5, 30, 60, 360, 720, 1440 |

#### Charge Power Limits

| DPID | BatteryData Field | Setting | Unit |
|------|-------------------|---------|------|
| 103 | `rcBatteryChargeVoltageMv` or `rcBatteryChargePower` | **Charge configuration read-back (voltage or power)** — queried by app on Mega 2 alongside DPID 104 and 105; exact field mapping unconfirmed | Raw |
| 104 | `rcBatteryChargeCurrentMa` or `rcBatteryChargeVoltageCurrentRaw` | **Charge configuration read-back (current or composite)** — queried by app on Mega 2; exact field mapping unconfirmed | Raw |
| 106 | `acChargingPowerMax` | **AC charger max power** | Watts |

> **Note:** DPID 105 is the **Charge Mode** setting (0 = Slow charge, 1 = Fast
> charge), writable via Cmd3 on all Mega and Guardian series. Default/pre-seed
> is `1` (Fast). Confirmed from the APK's `S2_V2SettingFragment` and
> `D2SettingFragment`. The earlier "AC inverter protection" label was a
> misinterpretation. DPID 106 is the **writable** AC charging power max setting.

#### Scheduled Tasks (D2/D5 models only)

| DPID | BatteryData Field | Setting |
|------|-------------------|---------|
| 69 | `taskIndex` | Task hour/minute A |
| 70 | `taskNumber` | Task hour/minute B |
| 71 | `taskSwitch` | Task enable/disable (0/1) |
| 72 | `taskSlot` | Task time slot number |
| 73 | `taskActionFlag` | Task action flag |
| 74 | `taskRepeatTimeFlag` | Repeat flag (0–127 days) |
| 75 | `taskRepeatTime` | Weekday bitmask |

#### Low Battery Alarm

| DPID | BatteryData Field | Setting | Unit |
|------|-------------------|---------|------|
| — | `lowBatteryAlarmSwitch` | **Low battery alarm on/off** | 0/1 |
| — | `lowBatteryAlarmThreshold` | **Low battery alarm level** | % |
| — | `lowBatteryAlarmDuration` | **Alarm duration** | Seconds (float) |

> The DPIDs for the low battery alarm fields are not confirmed — they may be
> set via a composite/array DPID rather than individual writes.

#### Additional Read-only Status Fields (not writable)

| DPID | BatteryData Field | Description |
|------|-------------------|-------------|
| 208 | `battery_output_power` | Battery output power (display only) |
| 209 | `battery_starting_voltage` | Battery starting voltage (display only) |

### What the HA Integrations Currently Support

**BLE integration (`oupes_mega_ble`) — full read/write:**
- Output switches: AC, DC 12V, USB on/off via `build_output_command()` (attr 1 bitmask)
- Device settings via Cmd3: silent mode (63), breath light (58), fast charge (105), screen timeout (41), standby timeouts (45–49), AC ECO (110/111/114), DC ECO (112/113/115)
- All telemetry sensors and binary sensors

**WiFi client integration (`oupes_mega_wifi_client`) — read + output control:**
- Output switches: AC, DC 12V, USB on/off (via cloud broker Cmd3 with attr 84/1)
- All telemetry sensors and binary sensors
- **No device settings** — the device firmware does not echo setting DPIDs (41–63, 105, 110–115) back over the WiFi/cloud broker channel, so there is no way to confirm or track setting state. Settings must be changed via BLE.

**Not yet implemented (candidates for future BLE entities):**

| Priority | DPIDs | Entity Type | Rationale |
|----------|-------|-------------|-----------|
| High | 47, 49 | `number` (seconds) | USB+Car/AC standby timeouts — most commonly adjusted |
| High | 48 | `number` (seconds) | XT90 standby timeout — Guardian/HP2500 only |
| High | 45 | `number` (seconds) | Machine standby timeout — prevents unexpected sleep |
| High | 111, 113 | `number` (watts) | AC/DC ECO power thresholds (Exodus series) |
| Medium | 64 | `switch` | Night mode |
| Low | 61 | `select` | 50/60 Hz frequency |
| Low | 62 | `switch` | Bluetooth on/off |
| Low | 224 | `switch` | Energy management / memory function |
| Low | 12 | `number` (V) | Anderson/XT90 output voltage setpoint (Mega 2/3/5 + Guardian) |

---

## Product Model IDs

The `device_product_id` is a 6-character ASCII string broadcast in BLE
advertising data (service data UUID `0xA201` and manufacturer-specific data).
It identifies the product model and is the same across all units of the same
model. The HA integration currently does not use this value, but it could be
used to auto-detect device capabilities and adjust available entities per model.

**Source:** `AppParams.java` in the decompiled APK (Cleanergy app v1.4.1).

### Known Product IDs

| Product ID | Model | Series | Notes |
|------------|-------|--------|-------|
| `O44A5o` | **MEGA 1** | `mega_1` | Currently tested, confirmed working. Separate series from Mega 2/3/5 due to different bit2 port assignment (USB-only vs Anderson+USB) |
| `YRWj81` | **MEGA 2** | Mega | **Confirmed working (2026-04-14).** Identical WiFi protocol to Mega 1 — same broker `wp-cn.doiting.com:8896`, same streaming attr groups, same keepalive mechanism, same firmware version 1.2.0. Tested: device_id=`69560885ecda3b30b9c0`, BT MAC = WiFi MAC + 2. App additionally queries DPIDs [40, 41, 49, 114] and [103, 104, 105] for Mega 2 (40, 103, 104 not observed on Mega 1). |
| `EFDayi` | **MEGA 3** | Mega | |
| `JTEnK3` | **MEGA 5** | Mega | |
| `Hr9Uhd` | **Exodus 1200 / S012** | Exodus | |
| `pba1j6` | **Exodus 1500 / S015** | Exodus | |
| `IDaSL8` | **Exodus 2400 / S024** | Exodus | |
| `gF7XRS` | **S024 Lite** | Exodus | |
| `H99Evi` | **S1 Lite** | Exodus | |
| `oB6OKs` | **Guardian 6000 / D5** | Guardian | Also HP6000 |
| `LtQmdj` | **HP2500 / D2** | Guardian | Also MEGA2PRO |
| `95haDY` | **D5 V2** | Guardian | Newer revision |
| `xLtGhT` | **S2 V2** | — | Newer revision |
| `5cY3Mf` | **DC 800** | — | |
| `zcWgyE` | **LP350 / L350** | LP | |
| `fckIgv` | **LP700 / L700** | LP | |
| `ZlD25j` | **PB300** | Portable | |
| `uAsyax` | **UPS 1200** | UPS | |
| `QWlryl` | **UPS 1800** | UPS | |

### Where the Product ID Appears

1. **BLE service data** (UUID `0xA201`): 2-byte header + 6-byte product_id + reversed MAC
2. **BLE manufacturer data** (AD type `0xFF`): bytes 11–16 of the raw payload
3. **BLE handshake response**: in the second notification after init
4. **Cloud API** (`/api/app/device/list`): `device_product_id` field in JSON
5. **Cloud API** (`/api/app/device/model`): full product catalog with images

### Multi-device Support Considerations

The BLE and cloud protocols appear to be shared across the entire OUPES product
range. Key differences between models are likely:

- **Supported DPIDs:** Not all models support all settings. The Mega 1 may not
  respond to DFC charger DPIDs (218–220) or scheduled task DPIDs (69–75).
  Conversely, UPS models have alarm-specific DPIDs not present on power stations.
- **Telemetry attributes:** The core attrs (1–9, 21–23, 30, 32, 51, 78–80, 101,
  105) are likely universal. Higher attrs may differ per model.
- **Output bitmask:** The attr 1 bit assignments differ by series (confirmed from
  APK `dcXt90Switch`/`dcUsbCarSwitch`/`dcUsbSwitch` in the respective Detail fragments):
  - **Mega 1** (`mega_1`): bit0=AC, bit1=Car port only, bit2=USB only
  - **Mega 2/3/5** (`mega`): bit0=AC, bit1=Car+12V barrel jacks (combined), bit2=Anderson+USB (combined)
  - **Guardian/HP2500** (`guardian`): bit0=AC, bit1=Car+12V (combined), bit2=XT90 output
  - **Exodus/LP/other**: bit0=AC, bit1=Car port, bit2=USB (assumed same as Mega 1; no Anderson/XT90)
- **Battery module count:** Models without expansion battery support won't have
  slot-indexed attrs.

The `device_product_id` from BLE advertising could be used at integration setup
to auto-configure which entities to create and which DPIDs to expose.

---

## HTTP REST API — Full Endpoint List

All endpoints use base URL `http://api.upspowerstation.top` (unencrypted HTTP).
Required query params on all requests: `token`, `platform=android`, `lang=en`,
`systemVersion=36`.

### Confirmed Endpoints (observed in pcap / app decompilation)

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| POST | `/api/app/user/login` | Login, returns session token + MQTT creds | None (body has email+passwd) |
| POST | `/api/app/user/registerSendCode` | Send registration verification code | None |
| POST | `/api/app/user/register` | Register new account | None |
| POST | `/api/app/user/forgetPasswordSendCode` | Password reset code | None |
| POST | `/api/app/user/resetPasswd` | Reset password | None |
| GET | `/api/app/user/info` | User profile info | Token |
| POST | `/api/app/user/update` | Update user profile | Token |
| POST | `/api/app/user/delete` | Delete account | Token |
| GET | `/api/app/device/list` | All devices bound to account | Token |
| GET | `/api/app/device/info` | Single device metadata | Token + device_id |
| POST | `/api/app/device/sync` | Sync device registration | Token |
| POST | `/api/app/device/rename` | Rename a device | Token |
| POST | `/api/app/device/remove` | Unbind/remove a device | Token |
| GET | `/api/app/device/model` | Full product model catalog (images, SKUs) | Token |
| GET | `/api/app/device/share_list` | List of shared device access | Token |
| POST | `/api/app/device/share` | Share device with another user | Token |
| POST | `/api/app/device/share_remove` | Remove shared access | Token |
| GET | `/api/app/config/weburl` | App configuration URLs | Token |
| GET | `/api/app/config/version` | Latest app version info | Token |
| GET | `/api/app/message/list` | User push notifications | Token |
| GET | `/api/app/home/list` | Home/system list (grid-tied) | Token |
| POST | `/api/app/home/create` | Create home/system | Token |
| POST | `/api/app/home/update` | Update home/system | Token |
| GET | `/api/app/home/detail` | Home system detail (for grid-tied setups) | Token |
| GET | `/api/app/statistics/*` | Energy statistics (daily/monthly/yearly) | Token |
| POST | `/api/app/ota/new_version` | Check for firmware OTA (see [Firmware Update](#firmware-update-ota)) | Token |
| POST | `/api/app/ota/addcmd` | Submit OTA test command | Token |

### TCP 8896 Broker Commands

| Command | Direction | Description |
|---------|-----------|-------------|
| `cmd=auth` | Client → Broker | Authenticate with broker token |
| `cmd=subscribe` | Client → Broker | Subscribe to device topic |
| `cmd=publish` (with `cmd=2` in message) | Client → Broker → Device | Read telemetry attrs |
| `cmd=publish` (with `cmd=3` in message) | Client → Broker → Device | Write device settings (Cmd3) |
| `cmd=publish` (with `cmd=10` in message) | Device → Broker → Client | Telemetry response |
| `cmd=keep` | Client → Broker | Client keepalive |
| `cmd=ping` / `cmd=pong` | Device ↔ Broker | Device heartbeat |
| `cmd=is_online` | Client → Broker | Check if device is reachable |