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
  - [BLE Output Control](#ble-output-control)
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
| `0x33` | 51 | Unknown (constant=2 in all captures) | — | ❓ Not a charging mode indicator |
| `0x35` | 53 | Unknown | — | ❓ |
| `0x36` | 54 | Unknown | — | ❓ |
| `0x4E` | 78 | **Per-module multiplexed field** (slot-indexed by attr 101) | **Three distinct ranges:** (1) `0–6000` = remaining runtime in **minutes** (5940 = normal max; sentinel values in the tens of thousands when at 100% SoC); (2) `44000–58500` = per-module battery **voltage in mV** (÷1000 = V; observed up to 57.312 V during BMS float-charge pulses); (3) `6001–43999` = **per-module float-charge measurement** (see Notes) — emitted continuously while SoC=100% and grid connected, only on the actively-charging module | ✅ All three ranges confirmed in extended live session |
| `0x4F` | 79 | **External Battery SoC** | Direct battery % (raw value = %; e.g. raw 15 = 15%) | ✅ Confirmed: raw 15 reported at ~15% after a few hours of charging |
| `0x50` | 80 | **External Battery Temperature** (slot-indexed by attr 101) | ÷10 = °F (e.g. 878 → 87.8 °F) | ✅ Confirmed vs app temperature display |
| `0x54` | 84 | AC output control (write only) | `1` = on, `0` = off | ⚠️ Observed in app control capture |
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
| Battery voltage (slot 2 only on Mega 1) | mV ÷ 1000 = V (e.g. 46310 → 46.310 V) | attr 78 (voltage range 44000–58500) + slot from attr 101 ✅ |
| Battery temperature (slot 1, 2, …) | ÷10 °F (e.g. 878 → 87.8 °F) | attr 80 + slot from attr 101 ✅ |
| AC inverter protection active | boolean | attr 105 ✅ |

> The app also shows "Grid" and "Solar" as separate input sources on the flow diagram screen, suggesting there may be distinct attrs for each not yet fully identified.

---

## Notes & Unknowns

- **Attr 30 = main unit remaining runtime in minutes.** Values are inaccurate under variable load. Goes **above 6000** specifically at 100% SoC (float-charge sentinel) — treat any value >6000 as "fully charged". Normal max during charge/discharge cycle is 5940.
- **Attr 32 = main unit temperature ÷10 in °F.** Confirmed: raw ~960 at idle = ~96 °F, consistent with app display. The earlier "probably °C" hypothesis was wrong — 96 °F is a perfectly reasonable idle temperature for the inverter internals.
- **Attr 78 = per-slot multiplexed field (slot-indexed by attr 101).** Three value ranges carry distinct data: runtime in minutes (≤6000; 5940 = normal max during charge/discharge), voltage in mV (44000–58500; ÷1000 = V; up to 57.3 V during float-charge pulses), and a live float-charge measurement (6001–43999; continuously emitted at SoC=100% with grid on — see Notes). On current Mega 1 firmware, only slot 2 broadcasts voltage readings in attr 78; slot 1 does not — the Voltage entity for slot 1 shows Unavailable.
- **Attr 79 = External Battery Charge (direct percentage).** Raw value = battery %; confirmed raw 15 = 15% observed during charging. The integration reports this as-is with no scaling.
- **Attr 80 = External Battery Temperature ÷10 in °F.** Confirmed against app temperature display (e.g. raw 878 → 87.8 °F). The earlier "section voltage" assumption was incorrect.
- **Attr 105 = AC Inverter Protection flag.** Goes `1` approximately 60 seconds after the AC output is hardware-tripped (overcurrent or thermal) and remains `1` during the 8–10 minute thermal recovery window. Also activates during elevated-temperature normal operation as a thermal warning without a hard trip. Goes `0` once the device recovers.
- **Attrs 21 and 22 are likely Total Input and Grid Input respectively.** Attr 21 is consistently exactly 1W higher than attr 22 across all captures and live readings (e.g. 36 vs 35, 30 vs 29). This is consistent with attr 21 = total charging input (grid + solar) and attr 22 = grid-only input. The 1W difference is the MPPT noise floor from attr 23 — confirmed to appear in the official app even with no panel connected, and confirmed in live CSV data (attr 23 = 1 during grid-on periods, 0 when grid is off). The original mapping (21 = Grid, 22 = Solar) was based on matching "Grid 30W" in the app against a value of 30, but attr 22 = 29 at that time is equally plausible as the actual grid reading.
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
- **Attr 1 live read values confirmed:** `4` (`0b100`) = USB only; `5` (`0b101`) = AC + USB; `7` (`0b111`) = AC + DC12V + USB (all on). These were observed in live telemetry during the overnight session, confirming the bitmask interpretation for all three output bits including DC12V.
- **Attr 101 is the battery slot index** (`1`, `2`, …), identifying which expansion battery the accompanying attrs 78/79/80 data belongs to in a given packet. Slots 1 and 2 are the first two connected batteries; with additional B2 units the slot number increases.
- **Unpolled attr ranges:** The device silently ignores attrs it doesn't recognise. Main unit temperature (attr 32) and remaining runtime (attr 30) are now confirmed. Attr 2 is unclear — may be a legacy flag superseded by the attr 1 bitmask.
- **Attr 78 voltage readings during float-charge** show rapid oscillation between ≈45 V and ≈57 V (observed range: 45.464 V – 57.312 V). This reflects the BMS doing pulse/burst charging at 100% SoC to maintain cell balance — the module voltage rises to the CV target (~57 V) during a charge pulse and falls back to resting voltage (~45 V) between pulses. With 1,031+ readings in a 9-hour session, the voltage entity reliably holds a recent reading even during high update-rate periods.
- **Attr 78 multiplexes three distinct value ranges across the same attribute number.** Within a single BLE session it can carry: (1) runtime in minutes (≤6000), (2) per-module battery voltage in mV (44000–58500; ÷1000 gives volts in the expected LiFePO4 operating range of ~44V–58V), or (3) mystery status codes (6001–43999). The active range changes within the session; both (1) and (2) were observed in the same live session. Range selection appears to be firmware-driven (not poll-controlled).
- **Attr 78 mystery value range (6001–43999) — REVISED:** Earlier analysis concluded these were initial-BLE-session-only status codes. **This was wrong.** Extended overnight logging (9 hours, 560K rows) showed 5,766 mystery observations with 283 distinct values — appearing continuously for hours while the device is at SoC=100% with grid connected. They are a **live fluctuating measurement** emitted in place of runtime during float-charge state, not one-time status codes. Observations: always SoC=100%; almost always grid-on (5,763/5,766); primarily slot 1 (5,742 of 5,766); values range continuously across the entire 6001–43999 window. The original 4 fixed values (6838, 16863, 27499, 37873) seen in the first session were coincidentally at a 100% SoC grid-connect moment and not representative. **Probable interpretation:** a per-module BMS measurement only computed during CV (constant-voltage) float-charge phase — candidates include instantaneous charging current (mA), per-module power (mW), or cell-balance data. Correlation with grid wattage is suggestive but not conclusive. A test with a known current source on the solar port and a logging multimeter would be needed to confirm the unit.
- **Attr 30 sentinel at 100% SoC:** Remaining runtime (attr 30) goes well above 6000 specifically when SoC = 100% and the device is in float-charge mode (observed values: 39,689 and 46,956 in different sessions). Normal charging/discharging max is 5,940. The value at 100% SoC is not consistent — treat any value >6000 as a sentinel meaning "fully charged / no meaningful runtime estimate". The exact value appears to accumulate or vary with float-charge duration.