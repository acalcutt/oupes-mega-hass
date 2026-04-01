# OUPES Mega — Home Assistant Custom Integration

A local Bluetooth (BLE) integration for the **OUPES Mega 1** power station.
Polls the device every minute over BLE and exposes ~20 Home Assistant entities
(sensors + binary sensors) with no cloud dependency.

---

## Requirements

| Component | Notes |
|-----------|-------|
| Home Assistant | 2023.6 or later (Python 3.11+) |
| HA `bluetooth` integration | Built-in — must be enabled and working |
| USB Bluetooth adapter | Plug into your HA server if it has no built-in BT |
| OUPES Mega 1 | Power on, BLE enabled — press the BLE/WiFi button on the unit to enable it; the setting persists after that |

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

### Automatic discovery (recommended)

If your OUPES Mega is powered on and in Bluetooth range when HA starts (or at
any time after adding the adapter), HA will notice the `TT` BLE advertisement
and show a **"New device discovered"** notification.

1. Click the notification (or go to **Settings → Devices & Services → + Add Integration → OUPES Mega**).
2. A confirmation dialog shows the device name and MAC address.
3. Click **Submit** — done.

### Manual setup

If auto-discovery doesn't trigger (e.g., device was off at startup):

1. **Settings → Devices & Services → + Add Integration → OUPES Mega**
2. Enter the Bluetooth MAC address (e.g., `8C:D0:B2:A7:EC:AF`) and a friendly name.
3. Click **Submit**.

> **Tip:** The MAC address is printed on a label on the bottom of the unit,  
> or you can find it by running `python scan_ble.py` from this repo.

---

## Entities Created

### Sensors (numeric)

| Entity | Attr | Unit | Notes |
|--------|------|------|-------|
| Battery | 3 | % | State of charge |
| AC Output Power | 4 | W | Load on AC outlets |
| AC Input Power | 5 | W | Grid/shore power draw |
| DC Car Charger Input | 6 | W | 12 V car port input |
| Solar Input | 7 | W | MPPT solar input |
| Total Input Power | 21 | W | Grid + solar combined |
| Grid Input Power | 22 | W | Grid only |
| Remaining Runtime | 30 | min | At current discharge rate |
| Main Unit Temperature | 32 | °F | Internal temperature of the unit |
| Charge Mode | 51 | — | "AC Charging" etc. |
| Ext Battery 1–6 Charge | 79 | % | Per connected B2 battery |
| Ext Battery 1–6 Runtime | 78 | min | |
| Ext Battery 1–6 Temperature | 80 | °F | |

> Entities for all 6 slots are registered at setup. Slots with no connected
> battery show as **Unavailable** and become available automatically when
> a battery is detected. (Mega 1: up to 2 slots, Mega 2: up to 4, Mega 3: up to 6.)
>
> All entities retain their last known value for up to 10 minutes if a poll
> fails (e.g. transient BLE drop). They only go unavailable if the device
> has been unreachable for longer than that.

### Binary Sensors (on/off)

| Entity | Attr | Notes |
|--------|------|-------|
| AC Output | 1 | AC outlets enabled |
| DC Output | 2 | DC / car port enabled |
| AC Input Connected | 23 | Grid power present |
| AC Output Control | 84 | |

---

## How It Works

Each update cycle (default: every 1 minute):

1. HA's Bluetooth scanner finds the `TT` advertisement and provides a `BLEDevice`.
   If the device hasn't advertised recently, a direct MAC address connection is
   attempted as a fallback so a single missed scan window doesn't cause a failure.
2. The coordinator connects via BleakClient.
3. Waits ~1.8 s (matching Android GATT discovery timing the device expects).
4. Subscribes to the notify characteristic and sends an 11-packet init sequence.
5. Collects BLE notification packets for ~15 seconds (with keepalive writes every
   10 s to prevent the device from dropping the connection).
6. Disconnects and updates all sensor entities with the collected data.

If the device makes a "cold-probe" drop (disconnects in <400 ms — normal BLE
behaviour) the coordinator retries up to 5 times automatically.

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
UPDATE_INTERVAL = timedelta(minutes=1)    # default — how often to poll
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
| Entities always Unavailable | BLE disabled on unit | Press the BLE/WiFi button on the device to re-enable it |
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
