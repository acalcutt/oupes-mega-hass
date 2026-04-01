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
  - [TCP Telemetry Channel (Port 8896)](#tcp-telemetry-channel-port-8896)
  - [Required Credentials](#required-credentials)
  - [Connection Sequence](#connection-sequence)
  - [Requesting Telemetry](#requesting-telemetry)
  - [Working Python Example](#working-python-example)
- [Bluetooth LE (BLE)](#bluetooth-le-ble)
  - [GATT Profile](#gatt-profile)
  - [Packet Format](#packet-format)
  - [Working Python Example](#ble-python-example)
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
                      f"Temp: {int(data.get('80', 0)) / 10:.1f} | "
                      f"Batt V: {int(data.get('32', 0)) / 100:.2f}V")
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
    bytes.fromhex("01060000626432333662313639350000000000d7"),  # bytes 4-13 = device_key ASCII
    bytes.fromhex("0107000000000000000000000000000000000000"),
    bytes.fromhex("0108000000000000000000000000000000000081"),
    bytes.fromhex("01890000000000000000000000000000000000c0"),
    bytes.fromhex("0180020101000000000000000000000000000016"),
]

# Session keepalive — send every 10 s, first send at ~6 s after init completes
KEEPALIVE = bytes.fromhex("0180030254010000000000000000000000000076")
```

> **Device-specific token:** The example above contains `bd236b1695` as the device_key (encoded `6264 3233 3662 3136 3935`). Replace with your own device's `device_key` from the HTTP REST API or a PCAPdroid/btsnoop capture.

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
    bytes.fromhex("01060000626432333662313639350000000000d7"),  # replace with your device_key
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
| `0x01` | 1 | AC output enabled | `1` = on, `0` = off | ✅ |
| `0x02` | 2 | DC output enabled | `1` = on, `0` = off | ✅ |
| `0x03` | 3 | Battery percentage | `0`–`100` | ✅ App shows 100% |
| `0x04` | 4 | AC output power | Watts | ✅ App shows 494–502W |
| `0x05` | 5 | AC input (grid) power | Watts | ✅ App shows ~490–510W |
| `0x06` | 6 | DC / car charger input | Watts | ⚠️ Always 0 in captures |
| `0x07` | 7 | Solar input power (MPPT) | Watts | ⚠️ App shows 1W; was 0 at capture time |
| `0x08` | 8 | Unknown input | — | ❓ |
| `0x09` | 9 | Unknown | — | ❓ |
| `0x15` | 21 | Total charging input (grid + solar) | Watts, 1:1 raw | ⚠️ Always 1W higher than attr 22 — consistent with solar reading ~1W noise when unplugged |
| `0x16` | 22 | Grid input (wall charge) | Watts, 1:1 raw (e.g. 29 → 29W) | ⚠️ Previously matched app "Grid 30W"; may be grid-only portion |
| `0x17` | 23 | AC input connected | `1` = yes, `0` = no | ✅ |
| `0x1E` | 30 | Remaining runtime | **Minutes** (e.g. 5940 = 4d 3h) | ✅ Confirmed vs app display |
| `0x20` | 32 | Battery pack voltage | ÷10 → Volts (e.g. 909 → 90.9V) | ✅ Full pack voltage confirmed live |
| `0x33` | 51 | Charge mode / state | `2` = AC charging | ⚠️ |
| `0x35` | 53 | Unknown | — | ❓ |
| `0x36` | 54 | Unknown | — | ❓ |
| `0x4E` | 78 | External battery remaining runtime | **Minutes** (e.g. 27499 = 19d 2h) | ✅ Confirmed vs app "Backup battery 2" |
| `0x4F` | 79 | Battery percentage (duplicate of attr 3) | `0`–`100` | ✅ |
| `0x50` | 80 | External battery temperature | ÷10 = °F (e.g. 878 → 87.8°F) | ✅ Confirmed vs app "Backup battery" temps |
| `0x54` | 84 | AC output control (write only) | `1` = on, `0` = off | ⚠️ Observed in app control capture |
| `0x65` | 101 | Data group index | `1` or `2` — alternates between broadcast groups; not a charging source flag | ⚠️ |
| `0x69` | 105 | Unknown flag | — | ❓ |

> **Note:** The main unit temperature (91°F in app) and main unit remaining runtime (13d 11h) have not yet been mapped to attr numbers. They likely exist in attr groups not yet polled (e.g. attrs 10–20, 40–50, or 60–70). Expand the `ATTR_GROUPS` poll list to discover them.

---

## App Display Reference

Screenshots of the Cleanergy app were used to calibrate attr values. The app displays the following fields:

| App Display | Value at Capture | Confirmed Attr |
|-------------|-----------------|----------------|
| Battery % | 100% | attr 3 / attr 79 ✅ |
| Temperature (main unit) | 91°F | **Not yet mapped** |
| Remaining time (main unit) | 13d 11h | **Not yet mapped** |
| AC output watts | 494–502W | attr 4 ✅ |
| DC output watts | 0W | attr 2 (on/off) + unknown watts attr |
| Grid input | 30W | attr 21 (raw = watts) ✅ |
| Solar input | 1W | attr 22 (raw = watts) ⚠️ |
| Backup battery 1 temp | 88°F | attr 80 ÷10 ✅ |
| Backup battery 2 temp | 87°F | attr 80 ÷10 ✅ |
| Backup battery 1 remaining | 4d 3h = 5940 min | attr 30 ✅ |
| Backup battery 2 remaining | 19d 2h ≈ 27480 min | attr 78 ✅ |
| Backup battery % | 100% | attr 79 ✅ |

> The app also shows "Grid" and "Solar" as separate input sources on the flow diagram screen, suggesting there may be distinct attrs for each not yet fully identified.

---

## Notes & Unknowns

- **Attr 30 confirmed = remaining runtime in minutes.** Cross-referenced against app display: raw `5940` = 4d 3h, raw `27499` ≈ 19d 2h. Values fluctuate because they reflect real-time calculation based on current load.
- **Attr 78 confirmed = external battery remaining runtime in minutes**, matching the "Backup battery 2" display in the app.
- **Attr 80 confirmed = external battery temperature ÷10 in °F.** The main unit temperature (91°F in app) has not been mapped yet — it likely resides in an attr group not yet polled. Attempts to poll unknown attr ranges (10–20, 40–50, 60–70, 90–100) returned no data, suggesting the device silently ignores unrecognized attrs.
- **Attrs 21 and 22 are likely Total Input and Grid Input respectively.** Attr 21 is consistently exactly 1W higher than attr 22 across all captures and live readings (e.g. 36 vs 35, 30 vs 29). This is consistent with attr 21 = total charging input (grid + solar) and attr 22 = grid-only input, with the MPPT solar input always reporting ~1W of noise even with no panel connected. The original mapping (21 = Grid, 22 = Solar) was based on matching "Grid 30W" in the app against a value of 30, but attr 22 = 29 at that time is equally plausible as the actual grid reading.
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
- **Attr 32 = full pack voltage ÷10.** Raw `909` → 90.9 V at 100% SoC. This is the full series-stack voltage of a high-voltage LiFePO4 pack. The earlier ÷100 assumption was wrong.
- **Attr 101 is a data group index**, not a charging source. It alternates between `1` and `2` across successive broadcast cycles, labelling which of two interleaved data groups a set of packets belongs to.
- **Unpolled attr ranges:** The device silently ignores attrs it doesn't recognise. The main unit temperature and remaining runtime fields visible in the app have not been mapped — they may only be accessible via BLE or may require a different polling approach.