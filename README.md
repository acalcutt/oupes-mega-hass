# OUPES Mega — Home Assistant Custom Integration

A local Bluetooth (BLE) integration for **OUPES Mega** power stations.
Connects via BLE (continuous or polled) and exposes sensors, binary sensors,
toggle switches, and writable device settings — all with no cloud dependency.

---

## Requirements

| Component | Notes |
|-----------|-------|
| Home Assistant | 2023.6 or later (Python 3.11+) |
| HA `bluetooth` integration | Built-in — must be enabled and working |
| USB Bluetooth adapter | Plug into your HA server if it has no built-in BT |
| OUPES power station | Any supported model (see below). Power on, BLE enabled — press the IoT button on the unit to enable it (indicator flashes rapidly); the setting persists after that. Hold for 5 s to factory-reset the BLE pairing. |

### Supported Models

The integration recognises the following models by their BLE product ID and
applies model-specific feature sets (settings, entity availability) automatically:

| Series | Models | Settings |
|--------|--------|----------|
| **Mega 1** | Mega 1 | Screen timeout, machine/WiFi/USB-car/AC standby timeouts, breath light, silent mode, charge mode |
| **Mega 2/3/5** | Mega 2, Mega 3, Mega 5 | Same as Mega 1, plus Car & 12V standby timeout |
| **Exodus** | Exodus 1200, Exodus 1500, Exodus 2400, S024 Lite, S1 Lite | Screen timeout, breath light, silent mode, charge mode, AC/DC ECO modes + thresholds |
| **Guardian** | Guardian 6000, HP2500, D5 V2 | Same as Mega 2/3/5, plus XT90 standby timeout |
| **Other** | S2 V2, DC 800, LP350, LP700, PB300, UPS 1200, UPS 1800 | Subset of settings per model |

Unrecognised product IDs get a conservative safe set of settings.

### USB Bluetooth adapter for HA

1. Plug any standard USB Bluetooth 4.0+ adapter into your HA server.
2. In HA, go to **Settings → System → Hardware** and confirm the adapter appears.
3. Go to **Settings → Devices & Services** and verify the **Bluetooth** integration
   is listed and shows "Running".
   - If it doesn't appear, add it via **+ Add Integration → Bluetooth**.
4. You're ready — no further adapter configuration is needed.

---

## Installation

### Option A — Manual (no HACS)

1. Copy the `custom_components/oupes_mega/` folder into your HA configuration
   directory so the path becomes:

   ```
   <ha-config>/custom_components/oupes_mega/
   ```

2. Restart Home Assistant.

### Option B — HACS (future)

Not yet published to HACS. Use Option A for now.

---

## Adding the Integration

### Step 1 — Device discovery

**Automatic (recommended):** If your OUPES Mega is powered on and in Bluetooth
range, HA auto-discovers the `TT` BLE advertisement and shows a **"New device
discovered"** notification. Click it, confirm the device name and MAC, and
click **Submit**.

**Manual:** Go to **Settings → Devices & Services → + Add Integration →
OUPES Mega** and enter the Bluetooth MAC address (e.g., `8C:D0:B2:A7:EC:AF`).

> **Tip:** The MAC address is printed on a label on the bottom of the unit,
> or you can find it by running `python debug_info/scan_ble.py` from this repo.

### Step 2 — Provide the device key

After discovery, the integration asks you to choose how to provide the
**device key** — a 10-character hex string the device uses to authenticate BLE
sessions. You'll see three options:

#### Option A — Create New Key (BLE pairing)

Pairs the device with a new key directly over BLE, with no cloud or app
dependency. This replicates the Cleanergy app's pairing protocol.

1. **Factory-reset the device first:** Press and hold the IoT button for
   **5 seconds** until the indicator light changes to rapid flashing. This
   clears the stored pairing key and puts the device into pairing mode.
2. A random 10-hex-character key is pre-filled (you can change it).
3. Click **Submit** — a progress spinner appears while pairing runs (~20 s).
4. When pairing completes, the integration is ready.

> This is the best option if you're setting up from scratch, taking ownership
> of a used unit, or don't want to use the Cleanergy cloud at all.

#### Option B — Enter Existing Key

Enter a device key you already know — for example, from a previous setup, a
btsnoop capture, or `adb logcat` output from the Cleanergy app.

The key must be exactly 10 lowercase hex characters (e.g., `bd236b1695`).

#### Option C — Fetch from Cleanergy Cloud

Log in with your Cleanergy account credentials. The integration calls the
OUPES cloud API (`api.upspowerstation.top`) to fetch the device key
automatically. Your credentials are used once and **not stored** — only the
retrieved key is saved.

> **Note:** The OUPES cloud API uses unencrypted HTTP (matching the official
> app's own behaviour). Your device must already be registered in the
> Cleanergy app for it to appear in the device list.

---

## Entities Created

### Sensors (numeric)

| Entity | Attr | Unit | Notes |
|--------|------|------|-------|
| Battery Charge | 3 | % | State of charge |
| AC Output Power | 4 | W | Load on AC outlets |
| DC 12V Output | 6 | W | 12 V cigarette lighter output |
| USB-C Output | 7 | W | USB-C port output power |
| USB-A Output | 8 | W | USB-A port output power |
| Total Input Power | 21 | W | Grid + solar combined |
| Grid Input Power | 22 | W | Grid only |
| Solar Input Power | 23 | W | MPPT solar input |
| Remaining Runtime | 30 | min | At current discharge rate (inaccurate under variable load) |
| Main Unit Temperature | 32 | °F | Internal temperature (÷10; e.g. raw 963 → 96.3 °F) |
| External Battery 1–N Runtime | 78 | min | Per battery module; 5940 = charging/idle max |
| External Battery 1–N Charge | 79 | % | Direct battery percentage (raw value = %) |
| External Battery 1–N Temperature | 80 | °F | Per module temperature (÷10; e.g. 878 → 87.8 °F) |
| Connected Expansion Batteries | 51 | — | Count of connected expansion battery packs (0–2 on Mega 1) |

> **Battery modules:** The Mega 1 has two **internal** battery modules (slots
> 1 and 2). External **OUPES B2 Expansion Batteries** (LiFePO4, ~2 kWh each)
> connect via cable and appear as additional slots in the BLE telemetry:
>
> | Model | Internal modules | Max B2 expansion batteries |
> |-------|-----------------|-----------------------------|
> | Mega 1 | 2 | 2 |
> | Mega 2 | ? | 4 |
> | Mega 3 | ? | 6 |
>
> This integration creates sensor entities for up to 6 slots. Slots with no
> data are automatically marked **Unavailable** and become active when data
> arrives. All entities retain their last known value for up to 10 minutes if
> a poll fails. They only go unavailable if the device has been unreachable
> for longer than that.

### Binary Sensors (on/off)

| Entity | Attr | Notes |
|--------|------|-------|
| AC Output | 1 (bit 0) | AC outlets enabled |
| Car Port / Car & 12V Output | 1 (bit 1) | Name depends on model: "Car Port" on Mega 1; "Car & 12V Output" on Mega 2/3/5 and Guardian |
| USB Output / Anderson & USB Output / XT90 Output | 1 (bit 2) | Name depends on model: "USB Output" on Mega 1; "Anderson & USB Output" on Mega 2/3/5; "XT90 Output" on Guardian |

### Switches (toggleable)

**Output switches** (attr-1 bitmask — always available):

| Entity | Notes |
|--------|-------|
| AC Output | Turns the AC inverter output on or off |
| Car Port / Car & 12V Output | Turns the car port (and 12V barrel jacks on Mega 2/3/5+) on or off. Name varies by model. |
| USB Output / Anderson & USB Output / XT90 Output | Turns USB ports on Mega 1, Anderson+USB on Mega 2/3/5, or XT90 on Guardian on or off. Name varies by model. |

**Setting switches** (Cmd3 DPID write — filtered by model series):

| Entity | DPID | Notes |
|--------|------|-------|
| Silent Mode | 63 | Fan speed limiting |
| Breath Light | 58 | Front LED breathing effect |
| Fast Charge | 105 | On = fast, off = slow (requires AC output off and no grid input) |
| AC ECO Mode | 110 | Auto-shutoff at low AC load (Exodus series only) |
| DC ECO Mode | 112 | Auto-shutoff at low DC load (Exodus series only) |

### Number Entities (writable settings)

**Standby timeout settings** (Cmd3 DPID write — filtered by model series):

| Entity | DPID | Unit | Range | Notes |
|--------|------|------|-------|-------|
| Screen Timeout | 41 | seconds | 0–3600 | Display auto-off delay |
| Machine Standby Timeout | 45 | seconds | 0–43200 | Full device auto-off; 0 = disabled |
| WiFi Standby Timeout | 46 | seconds | 0–86400 | WiFi module auto-off |
| USB/Car Port Standby Timeout | 47 | seconds | 0–21600 | USB + 12 V auto-off |
| XT90 Standby Timeout | 48 | seconds | 0–21600 | XT90 expansion port auto-off |
| AC Output Standby Timeout | 49 | seconds | 0–21600 | AC inverter auto-off |
| AC ECO Threshold | 111 | W | 0–100 | Exodus series only |
| DC ECO Threshold | 113 | W | 0–100 | Exodus series only |

Toggling a switch or setting a number writes the value to the device over BLE.
The UI updates immediately (optimistic); the device's confirmed response is
reflected on the next telemetry cycle.

---

## How It Works

The integration supports two connection modes (configurable per device):

### Continuous mode (default)

Holds a persistent BLE connection:

1. HA's Bluetooth scanner finds the `TT` advertisement.
2. Connects and waits ~1.8 s (matching the Android GATT timing the device expects).
3. Subscribes to notifications and sends the 11-packet init sequence (packet 7 carries the device key).
4. Sends **Cmd2 settings queries** to read current values for supported DPIDs
   (screen timeout, standby timeouts, breath light, silent mode, etc.).
5. Stays connected, receiving continuous telemetry (Cmd1) packets ~1/second.
6. Sends keepalive packets every 10 s to prevent the device from dropping the session.
7. Automatically reconnects on disconnect.

### Polled mode

Connects periodically (configurable interval, default 30 s):

1. Connects, initializes, sends settings queries, collects telemetry for ~15 s.
2. Disconnects and updates entities.
3. Waits for the next poll interval.

### BLE protocol overview

The device uses three packet types:

| Type | Direction | Purpose |
|------|-----------|--------|
| **Cmd1** (telemetry) | Device → HA | Continuous sensor data (battery %, power, temperature, etc.) |
| **Cmd2** (query) | HA → Device | Request current setting values for specific DPIDs |
| **Cmd3** (write/response) | Both | Write a setting value; device echoes confirmation |

Telemetry uses standard TLV encoding (`[0x0A][length][attr][value…]`).
Settings use compact TLV (`[length][attr][value…]`) — confirmed by
`BleCmdResultBuildParser.getCmd2_3_10Result` in the Cleanergy APK.

Packets are 20 bytes with multi-packet reassembly for larger payloads (byte 1
low 7 bits = packet index, bit 7 = last flag, byte 19 = CRC-8 checksum).

### Connection notes

- If a poll or connection fails, entities **retain their last known values** for
  a configurable period (default 15 minutes) before going unavailable.
- Cold-probe drops (device disconnects in <400 ms) are retried automatically.
- The device only supports **one BLE connection at a time.** Close the Cleanergy
  app when HA is connected.
- For devices far from the HA server, an **ESPHome Bluetooth Proxy** placed near
  the device significantly improves reliability — especially for settings queries.

---

## Connection Options

All connection settings are configurable per device through the HA UI:
**Settings → Devices & Services → OUPES Mega → Configure**

| Option | Default | Description |
|--------|---------|-------------|
| Continuous connection | On | Hold BLE connection open permanently vs. periodic polling |
| Poll interval | 30 s | (Polled mode only) How often to reconnect and collect telemetry |
| Stale timeout | 15 min | How long to keep last known values before marking entities unavailable |
| Debug attr logging | Off | Log parsed attribute values to a CSV in the HA config directory |
| Debug raw logging | Off | Log every raw BLE packet (hex) to a separate CSV |

Changes take effect immediately — no restart required.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Entities always Unavailable | Device out of range or off | Check device is on and in range |
| Entities always Unavailable | BLE disabled on unit | Press the IoT button on the device to re-enable it (indicator flashes rapidly) |
| "BLE device not found" in logs | HA hasn't scanned recently | Check Bluetooth integration is running |
| Entities always Unavailable | App open on phone | The device only allows one BLE connection at a time — close the Cleanergy app and wait for the next poll |
| Settings entities blank/unknown | Device too far from BLE adapter | Cmd2 settings queries are request-response; if the signal is weak, responses get lost. Move the device closer or use an ESPHome BLE proxy |
| Settings entities blank/unknown | Frequent connection gaps | Enable debug raw logging and check for gaps >2 min — indicates BLE range issues |
| "Cold-probe drop" repeated | BLE interference or device busy | Usually self-resolves on next poll |
| Setup fails with ConfigEntryNotReady | Device not reachable at startup | HA will retry — power on the device |

Enable debug logging for more detail:

```yaml
# configuration.yaml
logger:
  logs:
    custom_components.oupes_mega: debug
```

---

## Understanding the Device Key

The **device key** (`device_key`) is a 10-character hex string that
authenticates BLE sessions. The device and the client must agree on the same
key for telemetry to flow.

### How the key is derived (Cleanergy app convention)

The official Cleanergy app derives the key from your numeric cloud user ID:

```python
import hashlib
device_key = hashlib.md5(str(user_uid).encode()).hexdigest()[:10]
```

However, the device doesn't enforce this derivation — **any 10-character hex
string works as a key.** The uid-based MD5 is purely a convention in the app.
Keys are per-device: a key that works on one unit will not work on another
unless it has been paired with that same key.

> **Note:** If the Cleanergy app runs before login completes, it falls back to
> uid `"0"` (giving key `cfcd208495`). This can happen if the app is opened
> immediately after a cache clear before the login HTTP response arrives.

### Where the key comes from

During the integration setup (Step 2 above), you choose one of three methods:

- **Create New Key:** The integration pairs a new key over BLE. No prior
  knowledge of the key is needed — just factory-reset the device first.
- **Existing Key:** You supply a key you already know (from a btsnoop capture,
  `adb logcat`, or a previous setup).
- **Cloud Login:** The integration fetches the key from the OUPES cloud API
  using your Cleanergy account.

### Standalone pairing script

If you want to pair outside of HA — for example, on a laptop for testing —
use `debug_info/pair_device.py` from this repo:

```powershell
# Factory-reset the device first: hold IoT button 5 s until rapid flashing
python debug_info/pair_device.py <MAC> --key <new_10_hex_char_key>
```

The script replicates the exact BLE pairing protocol the Cleanergy app uses
(AUTH → handshake polling → CLAIM with key + dummy MQTT token), with no cloud
or app dependency. Typical pairing completes in one cycle (~18 seconds).

### Finding an existing key (without cloud login)

If the device is already paired and you need to recover the key:

**ADB logcat (easiest, no root):** Connect your Android phone via USB with
USB debugging enabled, then:

```
adb logcat | findstr "8888888888888888888"
```

Open the Cleanergy app — within seconds the terminal prints a JSON line
containing `"device_key":"<your_key>"`.

**PowerShell (no phone needed):**

```powershell
$body = '{"account":"YOUR_EMAIL","password":"YOUR_PASSWORD","client_type":2}'
$r = Invoke-RestMethod -Uri "http://api.upspowerstation.top/api/app/user/login" `
     -Method POST -Body $body -ContentType "application/json"
$token = $r.data.token

$devices = Invoke-RestMethod -Uri "http://api.upspowerstation.top/api/app/device/list" `
           -Headers @{Authorization = "Bearer $token"}
$devices.data | Select-Object name, mac_address, device_id, device_key | Format-Table -AutoSize
```

---

## Debug Tools

The [`debug_info/`](debug_info/) directory contains standalone diagnostic
scripts and a detailed [protocol reference](debug_info/README.md) covering the
full BLE and WiFi/cloud communication protocol.

Key tools for troubleshooting:

| Script | Purpose |
|--------|---------|
| `debug_info/scan_ble.py` | Scan for TT devices and display live telemetry |
| `debug_info/parse_btsnoop.py` | Parse btsnoop HCI logs from Android bugreports |
| `debug_info/pair_device.py` | Standalone BLE pairing (factory-reset + re-key) |
| `debug_info/probe_key.py` | Try candidate keys against a device to find the right one |
| `debug_info/ble_diag.py` | Verify BLE GATT services and characteristics |

See [`debug_info/README.md`](debug_info/README.md) for the complete protocol
documentation, including the pairing/claiming protocol, packet format,
telemetry attribute map, and output control commands.
