# OUPES Mega — Home Assistant Custom Integration

A local Bluetooth (BLE) integration for the **OUPES Mega 1** power station.
Polls the device every minute over BLE and exposes sensors, binary sensors, and
toggle switches with no cloud dependency.

---

## Requirements

| Component | Notes |
|-----------|-------|
| Home Assistant | 2023.6 or later (Python 3.11+) |
| HA `bluetooth` integration | Built-in — must be enabled and working |
| USB Bluetooth adapter | Plug into your HA server if it has no built-in BT |
| OUPES Mega 1 | Power on, BLE enabled — press the IoT button on the unit to enable it (indicator flashes rapidly); the setting persists after that. Hold for 5 s to factory-reset the BLE pairing. |

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
| Unknown (attr 51) | 51 | — | Constant 2 in all captures; meaning unconfirmed |

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
| DC 12V Output | 1 (bit 1) | 12 V cigarette lighter enabled |
| USB Output | 1 (bit 2) | USB-A and USB-C ports enabled |
| AC Output Control | 84 | |
| AC Inverter Protection | 105 | On when the inverter is in thermal protection / recovery mode |

### Switches (toggleable)

| Entity | Notes |
|--------|-------|
| AC Output | Turns the AC inverter output on or off |
| DC 12V Output | Turns the 12 V cigarette-lighter port on or off |
| USB Output | Turns the combined USB-A / USB-C output on or off |

Toggling a switch writes the updated output-enable bitmask to the device over
BLE. The UI updates immediately (optimistic); the device's confirmed response
is reflected on the next poll (~1 minute) or sooner if the coordinator refresh
triggers quickly.

---

## How It Works

Each update cycle (default: every 1 minute):

1. HA's Bluetooth scanner finds the `TT` advertisement and provides a `BLEDevice`.
2. The coordinator connects via `bleak_retry_connector.establish_connection()`.
3. Waits ~1.8 s (matching Android GATT discovery timing the device expects).
4. Subscribes to the notify characteristic and sends an 11-packet init sequence
   (packet 7 carries the device key).
5. Collects BLE notification packets for ~15 seconds (with keepalive writes every
   10 s to prevent the device from dropping the connection).
6. Disconnects and updates all sensor entities with the collected data.

If the device makes a "cold-probe" drop (disconnects in <400 ms — normal BLE
behaviour) the coordinator retries automatically.

If a poll fails entirely, entities **retain their last known values** for up to
10 minutes before going unavailable. This prevents flickering from transient
BLE issues.

> **Note:** The device only supports **one BLE connection at a time.** While the
> integration is actively polling (~15 s per minute), the Cleanergy app cannot
> connect. Conversely, if you have the app open, the integration's poll that
> minute will fail and fall back to cached values. Close the app when you don't
> need it to let HA poll freely.

---

## Changing the Poll Interval

Edit `const.py` in the integration folder and change `UPDATE_INTERVAL` and/or
`STALE_TIMEOUT`:

```python
UPDATE_INTERVAL = timedelta(seconds=30)   # default — how often to poll
UPDATE_INTERVAL = timedelta(minutes=5)    # less frequent

STALE_TIMEOUT = timedelta(minutes=10)     # default — grace period before unavailable
STALE_TIMEOUT = timedelta(minutes=30)     # longer grace period
```

Restart HA after changing.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Entities always Unavailable | Device out of range or off | Check device is on and in range |
| Entities always Unavailable | BLE disabled on unit | Press the IoT button on the device to re-enable it (indicator flashes rapidly) |
| "BLE device not found" in logs | HA hasn't scanned recently | Check Bluetooth integration is running |
| Entities always Unavailable | App open on phone | The device only allows one BLE connection at a time — close the Cleanergy app and wait for the next poll |
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
