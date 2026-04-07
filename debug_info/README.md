# OUPES Mega 1 — Reverse Engineered Telemetry API

> **Status:** Research/Proof of Concept  
> **Device:** OUPES Mega 1 Power Station (WiFi+BLE model)  
> **App:** Cleanergy (`com.cleanergy.app`)  
> **Firmware:** 1.2.0  

This document summarizes findings from reverse engineering the OUPES Mega 1's communication protocols, captured via PCAPdroid (Android), Wireshark, and pfSense firewall captures. The goal was to enable telemetry access for Home Assistant integration without relying on the official app.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [WiFi / Cloud API](#wifi--cloud-api)
  - [HTTP REST API](#http-rest-api)
  - [Cloud Login API](#cloud-login-api)
  - [TCP Telemetry Channel (Port 8896)](#tcp-telemetry-channel-port-8896)
  - [Required Credentials](#required-credentials)
  - [Connection Sequence](#connection-sequence)
  - [Requesting Telemetry](#requesting-telemetry)
  - [Working Python Example](#working-python-example)
- [Bluetooth LE (BLE)](#bluetooth-le-ble)
  - [GATT Profile](#gatt-profile)
  - [BLE Advertising & Device Discovery](#ble-advertising--device-discovery)
  - [Obtaining the `device_key`](#obtaining-the-device_key)
  - [Connection Sequence](#connection-sequence-1)
  - [Init Sequence](#init-sequence)
  - [Packet Format](#packet-format)
  - [BLE Output Control](#ble-output-control)
  - [BLE Python Example](#ble-python-example)
- [Telemetry Attribute Map](#telemetry-attribute-map)
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
│  │  "TT" device │        │  → 47.252.10.9        │  │
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
- The device only maintains its cloud TCP connection **while actively paired via BLE to the app**. When no phone is BLE-connected, the device drops its cloud session entirely. This was confirmed by observing `num=0` (zero subscribers) on publish responses even when the device was reachable on the local network — and the official app experienced the same `num=0` failure simultaneously.
- While cloud-connected, the device sends a `cmd=ping` / `cmd=pong` heartbeat approximately every 90 seconds.
- The device does **not** push telemetry autonomously — a subscribed client must explicitly poll attribute groups to trigger a `cmd=10` response.
- The BLE channel is fully independent and does not require cloud connectivity — it is the **recommended approach** for a permanent Home Assistant integration.

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

**Server:** `47.252.10.9:8896` (Alibaba Cloud)

This is a custom text-based pub/sub protocol over raw TCP — not MQTT, but conceptually similar. All messages are `key=value&key=value` pairs terminated with `\r\n`.

> **Critical limitation:** The device only connects to this cloud broker while a phone is actively BLE-paired to it via the Cleanergy app. With no BLE connection, the device drops its cloud session and all publish requests return `num=0` (no subscribers). This makes the WiFi/cloud path unreliable for unattended automation.

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

### Connection Sequence

The full sequence to establish a working telemetry session on TCP 8896:

```
1. TCP connect → 47.252.10.9:8896

2. TX: cmd=auth&token=<auth_token>\r\n
   RX: (acknowledged silently or with res=1)

3. TX: cmd=subscribe&topic=device_<device_id>&from=control&device_id=<device_id>&device_key=<device_key>\r\n
   RX: cmd=subscribe&topic=device_<device_id>&res=1\r\n

4. Poll attr groups (see below) →
   RX: cmd=publish&device_id=...&topic=device_<device_id>&message={...cmd=10 telemetry...}\r\n
   NOTE: If device is not cloud-connected, response will be cmd=publish&res=1&num=0 (no data)

5. Client keepalive (send periodically to keep broker connection alive):
   TX: cmd=keep\r\n
   RX: cmd=keep&timestamp=<unix_ms>&res=1\r\n

6. Check if device is reachable on broker:
   TX: cmd=is_online&device_id=<device_id>\r\n
   RX: cmd=keep&timestamp=<unix_ms>&res=1\r\n
```

The device sends its own independent heartbeat to the broker while cloud-connected:
```
TX (device): cmd=ping\r\n
RX (server): cmd=pong&res=1\r\n
```
approximately every 90 seconds.

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

HOST       = '47.252.10.9'
PORT       = 8896
DEVICE_ID  = 'YOUR_DEVICE_ID'
DEVICE_KEY = 'YOUR_DEVICE_KEY'
TOKEN      = 'YOUR_BROKER_AUTH_TOKEN'

ATTR_GROUPS = [
    [1, 2, 3, 4, 5, 6, 7, 8, 9],
    [21, 22, 23, 30, 32],
    [51],
    [101, 53, 54, 78, 79, 80],
]

def send(s, msg):
    s.send((msg + '\r\n').encode())

def connect():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((HOST, PORT))
    s.settimeout(15)
    send(s, f"cmd=auth&token={TOKEN}")
    time.sleep(0.2)
    send(s, f"cmd=subscribe&topic=device_{DEVICE_ID}&from=control"
           f"&device_id={DEVICE_ID}&device_key={DEVICE_KEY}")
    time.sleep(0.3)
    return s

def poll(s):
    for attrs in ATTR_GROUPS:
        msg = json.dumps({
            "msg": {"attr": attrs},
            "pv": 0,
            "cmd": 2,
            "sn": str(int(time.time() * 1000))
        })
        send(s, f"cmd=publish&device_id={DEVICE_ID}"
               f"&topic=control_{DEVICE_ID}&device_key={DEVICE_KEY}"
               f"&message={msg}")
        time.sleep(0.15)

def listen(s):
    buf = ""
    while True:
        try:
            buf += s.recv(4096).decode(errors='replace')
            while '\r\n' in buf:
                line, buf = buf.split('\r\n', 1)
                if f'topic=device_{DEVICE_ID}' in line and 'message=' in line:
                    try:
                        payload = json.loads(line[line.index('message=') + 8:])
                        if payload.get('cmd') == 10:
                            return payload['msg']['data']
                    except Exception:
                        pass
        except socket.timeout:
            return {}

# Main loop with auto-reconnect
while True:
    try:
        print("Connecting...")
        sock = connect()
        print("Connected. Polling every 30s.")
        while True:
            poll(sock)
            data = listen(sock)
            if data:
                print(f"Battery: {data.get('3','?')}% | "
                      f"AC In: {data.get('5','?')}W | "
                      f"AC Out: {data.get('4','?')}W | "
                      f"Temp: {int(data.get('32', 0)) / 10:.1f}°C | "
                      f"Solar: {data.get('23','?')}W")
            time.sleep(30)
    except (ConnectionResetError, BrokenPipeError, OSError) as e:
        print(f"Disconnected ({e}), reconnecting in 10s...")
        time.sleep(10)
    except KeyboardInterrupt:
        print("Stopped.")
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

The `device_key` is assigned by the cloud server and stored in the app's local MMKV storage (`/data/user/0/com.cleanergy.app/files/mmkv/http_mmkv`). The internal device config JSON (confirmed from Android bugreport logcat):

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

The HA integration's config flow logs in to the OUPES cloud API with your Cleanergy account credentials, fetches the device list, and extracts the `device_key` for your device. Credentials are used once during setup and are **not** stored. See the [Cloud Login API](#cloud-login-api) section above for the full protocol.

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
| `0x69` | 105 | **AC Inverter Protection** (thermal/overcurrent flag) | `1` = protection active (~60s after hard trip, or during elevated-temp warning); `0` = normal | ✅ Goes `1` during 8–10 min thermal recovery; confirmed via btsnoop timestamps |

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
| AC inverter protection active | boolean | attr 105 ✅ |

> The app also shows "Grid" and "Solar" as separate input sources on the flow diagram screen, suggesting there may be distinct attrs for each not yet fully identified.

---

## Notes & Unknowns

- **Attr 30 = main unit remaining runtime in minutes.** Values are inaccurate under variable load. Goes **above 6000** specifically at 100% SoC (float-charge sentinel) — treat any value >6000 as "fully charged". Normal max during charge/discharge cycle is 5940.
- **Attr 32 = main unit temperature ÷10 in °F.** Confirmed: raw ~960 at idle = ~96 °F, consistent with app display. The earlier "probably °C" hypothesis was wrong — 96 °F is a perfectly reasonable idle temperature for the inverter internals. Temperature reports in discrete firmware steps (e.g. 956=95.6°F, 949=94.9°F, 942=94.2°F, 935=93.5°F, 928=92.8°F); during sustained 500W discharge the reading pegged at 956 for several hours then stepped down gradually as the ambient cooled. The AC inverter protection threshold (attr 105) was not triggered at 95.6°F but was triggered in a prior session that reached raw 970 = 97.0°F — the protection threshold is somewhere in between.
- **Attr 78 = per-slot multiplexed field (slot-indexed by attr 101).** Three value ranges carry distinct data: runtime in minutes (≤6000; 5940 = normal max during charge/discharge), voltage in mV (44000–61000; ÷1000 = V; fast-charge peak up to 60.050 V, float-charge up to 57.3 V), and a live float-charge measurement (6001–43999; continuously emitted at SoC=100% with grid on — see Notes). On current Mega 1 firmware, only slot 2 broadcasts voltage readings in attr 78; slot 1 does not — confirmed in 19-hour log: slot 1 had zero readings ≥44000 mV across 28,000+ observations while slot 2 had 1,242 voltage readings. The Voltage entity for slot 1 shows Unavailable.
- **Attr 79 = External Battery Charge (direct percentage).** Raw value = battery %; confirmed raw 15 = 15% observed during charging; raw 0 = 0% confirmed at end of full discharge-to-zero session. The integration reports this as-is with no scaling.
- **Attr 80 = External Battery Temperature ÷10 in °F.** Confirmed against app temperature display (e.g. raw 878 → 87.8 °F). The earlier "section voltage" assumption was incorrect.
- **Attr 105 = AC Inverter Protection flag.** Goes `1` approximately 60 seconds after the AC output is hardware-tripped (overcurrent or thermal) and remains `1` during the 8–10 minute thermal recovery window. Also activates during elevated-temperature normal operation as a thermal warning without a hard trip. Goes `0` once the device recovers. **Confirmed NOT voltage-triggered:** a complete deep-discharge session (97%→0% SoC; BLE logging gap from 15%→0% during which the device ran unmonitored for ~2h43min) produced zero attr 105 events throughout. At the moment SoC=0% was first logged, attr 4=500W and attr 5=506W — the device was still actively outputting ~500W with all outputs on (attr 1=7) and attr 105=0. Peak temperature was 95.6°F (raw 956) across this entire run — no protection triggered. Earlier sessions that did trigger protection (raw attr 32 rising to 970 = 97.0°F) were almost certainly thermal: the device was in a warmer environment or enclosure. The protection threshold lies somewhere above 95.6°F / 35.3°C.
- **Attrs 21 and 22 are likely Total Input and Grid Input respectively.** Attr 21 is consistently exactly 1W higher than attr 22 across all captures and live readings (e.g. 36 vs 35, 30 vs 29). This is consistent with attr 21 = total charging input (grid + solar) and attr 22 = grid-only input. The 1W difference is the MPPT noise floor from attr 23 — confirmed to appear in the official app even with no panel connected, and confirmed in live CSV data (attr 23 = 1 during grid-on periods, 0 when grid is off). The original mapping (21 = Grid, 22 = Solar) was based on matching "Grid 30W" in the app against a value of 30, but attr 22 = 29 at that time is equally plausible as the actual grid reading. **Confirmed: attr 21 is a system-wide total that includes solar power entering connected B2 expansion batteries via their secondary MPPT ports.** 19-hour log (2026-04-05 23:42 – 2026-04-06 18:54) confirms this definitively at the 14:04–14:30 window: AC disconnected at 14:05 (attr 22 → 0); chain cable remained physically connected — attr 54 slot 2 briefly showed ~104W as the B2 chain-discharged into the Mega during the AC-off handover, then naturally dropped to 0 at ~14:07 as the solar MPPT took over. Attr 53 slot 2 and attr 21 then tracked each other in per-minute lockstep: 14:08: 7W, 14:09: 9W, 14:10: 31W, 14:11: 42W, 14:12: 50W, 14:13: 58W, 14:14: 93W, 14:15: 103W, stable ~102–107W until 14:30 when both simultaneously dropped to 0. Attr 22 and attr 23 remained 0 throughout the solar ramp. Attr 53 slot 1 = 0 throughout — slot 1 has no solar panel. Notable: attr 23 (main unit solar input) stayed 0 throughout — B2 secondary-port solar does NOT appear in attr 23; it only rolls up into attr 21 directly.
- **Solar port and attr 23 testing used a DC battery source,** not a real solar panel (no panel available). The port accepted DC input from the battery correctly; attr 23 reflected the input wattage as expected. Attr 23 = 1 (noise floor) is common during grid-on periods with nothing connected — this matches what the official app displays.
- **Cloud connection is BLE-dependent.** The device only connects to the cloud broker while a phone is actively BLE-paired via the Cleanergy app. With no BLE connection, all publish requests return `num=0`. This was confirmed by the official app experiencing the same failure simultaneously. **This makes the WiFi/cloud path unsuitable for unattended Home Assistant integration.**
- **Broker token appears long-lived.** The same token was observed across multiple separate capture sessions. It does not appear to be a short-lived session token.
- **Client keepalive is `cmd=keep`**, not `cmd=is_online`. The app sends `cmd=keep\r\n` and receives `cmd=keep&timestamp=...&res=1`. The `cmd=is_online` command is a separate per-device check sent every ~5 seconds by the app.
- **`num=0` vs `num=1`** in publish responses indicates whether the device received the message. `num=1` means the device is cloud-connected and got the request; `num=0` means it is not reachable via the broker.
- **UDP port 6095:** The app broadcasts `{"cmd":0,"pv":0,"sn":"...","msg":{}}` to `255.255.255.255:6095` but the device does not respond. One-way only, not useful for data retrieval.
- **`wp-cn.doiting.com`:** The app connects to this host over TLS 443 at startup. This is where the broker auth token is obtained. The connection is encrypted and cannot be read without TLS interception.
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
- **`device_product_id` (`O44A5o`) appears in multiple contexts:** BLE service data (UUID=0xA201), BLE manufacturer data, BLE handshake response, and the cloud API JSON config. This 6-byte ASCII string is a per-product-model identifier (the same across units of the same model). It is distinct from the `device_key` (which is per-unit) and the `device_id` (which is per-unit).
- **BLE handshake response decoded.** After the app sends the initial 0x80/0x00 handshake packet, the device responds with two notification packets containing: (1) the full `device_id` as raw bytes, and (2) the reversed MAC address + `device_product_id` in ASCII. The app uses these to confirm it connected to the expected device before proceeding with the init sequence.
- **Cleanergy app uses two BLE slots:** The btsnoop19 capture shows the app initializing both slot 3 (first) and slot 1 (second). Slot 3 init packets carry the `device_key` plus an MQTT `client_id` string (29 chars spread across packets 6–8). Slot 1 init packets carry only the `device_key` (no MQTT data — zeros in packets 7–8). The slot 3 handshake is sent first, then slot 1. The HA integration's working init sequence uses only slot 1 — the slot 3 MQTT setup is only needed for cloud push functionality.
- **Cloud product catalog observed in logcat.** The Cleanergy app downloads a full product model catalog from the cloud on startup, containing SKU mappings and product images for all OUPES models: UPS-800, UPS-1200, UPS-1800, S2-V2, HP6000_V2, PB300, LP700, LP350, S024 Lite, S1 Lite, HP2500 (MEGA2PRO), Guardian 6000, DC 800, MEGA 5, MEGA2, MEGA3, MEGA1, Exodus 1200/1500/2400, shelly plug, shelly meter. This confirms the product line scope and suggests the protocol may be shared across the entire range.