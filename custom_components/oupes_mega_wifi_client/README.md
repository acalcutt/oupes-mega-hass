# OUPES Mega WiFi Client — Home Assistant Custom Integration

A Home Assistant custom integration that connects to the local OUPES WiFi proxy broker and exposes device telemetry as HA entities (sensors, binary sensors, switches).

> **Note:** Device settings (Silent Mode, Breath Light, Fast Charge, Screen Timeout,
> standby timeouts, etc.) are **not available over WiFi** — the device firmware does
> not report setting values on the WiFi channel. Use the companion
> **[`oupes_mega_ble`](../oupes_mega_ble/README.md)** integration for settings control.

This is the **entity/client** integration. It requires the companion **[`oupes_mega_wifi_proxy`](../oupes_mega_wifi_proxy/README.md)** proxy integration to be installed and running.

---

## Architecture

```
┌──────────────┐                    ┌──────────────────────────────────┐
│  OUPES Device│  ◄──TCP 8896──►   │  oupes_mega_wifi_proxy (proxy)        │
│  (firmware)  │   topic routing    │  server.py — TCP broker           │
└──────────────┘                    └──────────────┬───────────────────┘
                                                   │
                                    ┌──────────────▼───────────────────┐
                                    │  oupes_mega_wifi_client           │
                                    │  coordinator.py — TCP client      │
                                    │                                   │
                                    │  ┌─────────────────────────────┐ │
                                    │  │ Sensors (battery, power,    │ │
                                    │  │   temp, runtime, etc.)      │ │
                                    │  │ Binary Sensors              │ │
                                    │  │ Switches (AC, DC, USB)      │ │
                                    │  └─────────────────────────────┘ │
                                    └──────────────────────────────────┘
```

The client integration maintains a **persistent TCP connection** to the proxy broker, behaving exactly like the real Cleanergy Android app. It subscribes to the device's telemetry topic, sends the required heartbeats to sustain the data stream, and pushes telemetry updates to HA entities in real time.

---

## Prerequisites

1. **`oupes_mega_wifi_proxy` integration** must be installed and running (provides the TCP broker on port 8896)
2. **NAT rules** must be configured on your firewall/router to redirect device traffic to HA (see the [proxy README](../oupes_mega_wifi_proxy/README.md))
3. **OUPES device** must be online and connected to the broker (IoT button on, device bound)

---

## Installation

1. Copy the `oupes_mega_wifi_client` folder to `<config>/custom_components/`.
2. Restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration** and search for **OUPES Mega WiFi Client**.

---

## Configuration

The config flow has two steps:

### Step 1 — Connection Details

| Field | Default | Description |
|-------|---------|-------------|
| Host | `localhost` | IP or hostname of the proxy broker (usually the same HA instance). |
| TCP Port | `8896` | TCP broker port (must match `oupes_mega_wifi_proxy` setting). |
| HTTP Port | `8897` | HTTP API port (used to fetch device list during setup). |
| Email | — | Cleanergy account email (used once to fetch device list). |
| Password | — | Cleanergy account password (used once, not stored). |

### Step 2 — Device Selection

The integration logs in to the proxy's HTTP API, fetches the device list, and presents a picker. Select the device to monitor.

The following are stored in the config entry: `host`, `tcp_port`, `device_id`, `device_key`, `device_name`, `product_id`, `token`.

---

## How It Works

### Connection Sequence

1. **Auth:** Sends `cmd=auth&token=<token>` to the broker
2. **Subscribe:** Sends `cmd=subscribe&topic=device_<id>&from=control&...` — subscribes to the device's telemetry topic
3. **Activate:** Sends attr 84 keepalive (`cmd=3` with `{"84":1}`) — triggers the device to start streaming
4. **Stream:** Enters the main read/heartbeat loop

### Heartbeats (sustain the telemetry stream)

The coordinator sends three periodic heartbeats, matching what the real Cleanergy app sends:

| Heartbeat | Interval | Purpose |
|-----------|----------|---------|
| `cmd=is_online` | Every 5s | Tells broker/device a client is still watching. Device stops streaming after ~30s without this. |
| Attr 84 keepalive | Every 10s | WiFi equivalent of BLE `KEEPALIVE_PKT`. `cmd=3` with `{"attr":[84],"data":{"84":1}}`. |
| `cmd=keep` | Every 60s | TCP session liveness ping. |

### Telemetry Processing

- Data arrives as `cmd=10` publish messages with a JSON payload containing `msg.data` (integer attr→value pairs)
- The coordinator also processes `cmd=2` and `cmd=3` responses (same `msg.data` format)
- The coordinator accumulates attributes in an internal `_attrs` dict
- **Attr 101** indicates an expansion battery slot — subsequent ext-battery attributes are stored per-slot in `_ext_batteries`
- **Attr 30** and **78** (runtime minutes) are clamped to a maximum to filter sensor noise
- After each update, `async_set_updated_data()` pushes a snapshot to all HA entities immediately

### Push-Based Updates (No Polling)

The coordinator uses `update_interval=None` — it does **not** poll. All data is push-driven via the persistent TCP connection. HA entities update instantly when new telemetry arrives.

### Reconnection

If the TCP connection drops, the coordinator waits 10 seconds and reconnects automatically. The activation sequence is re-sent on each reconnect.

---

## Entities

### Sensors (Main Unit)

| Sensor | Attribute | Unit | Notes |
|--------|-----------|------|-------|
| Battery Charge | 3 | % | |
| Total Output Power | 4 | W | |
| AC Output Power | 5 | W | |
| DC 12V Output Power | 6 | W | Label varies by product series |
| USB-C Output Power | 7 | W | |
| USB-A Output Power | 8 | W | |
| Total Input Power | 21 | W | |
| Grid Input Power | 22 | W | |
| Solar Input Power | 23 | W | |
| Remaining Runtime | 30 | min | Clamped to max value to filter noise |
| Temperature | 32 | °F | Raw value ÷ 10 |
| Expansion Battery Count | 51 | — | Number of connected expansion batteries |

### Sensors (Expansion Batteries — per slot)

Dynamically created when expansion batteries are detected:

| Sensor | Attribute | Unit |
|--------|-----------|------|
| Charge | 79 | % |
| Runtime | 78 | min |
| Temperature | 80 | °F (÷10) |
| Output Power | 54 | W |
| Input Power | 53 | W |

### Switches

Output port control via `cmd=3` writes to attr 1 (bitmask):

| Switch | Bit | Description |
|--------|-----|-------------|
| AC Output | 0x01 | AC inverter output |
| DC 12V Output | 0x02 | DC cigarette-lighter port |
| USB Output | 0x04 | USB-A and USB-C combined |

### Availability

Entities are marked **unavailable** if no successful telemetry update has been received within 5 minutes. Expansion battery sensors additionally require their slot to be present in the latest data.

---

## Sending Commands

The coordinator exposes methods for sending commands to the device:

- **`send_output_command(bitmask)`** — Sets output port state (OR of AC/DC/USB bits)
- **`send_command(cmd, data)`** — Sends an arbitrary protocol command

Commands are queued and sent during the next read loop iteration (within 2 seconds).

---

## Source Reference

- Protocol details: see [`debug_info/README.md`](../../debug_info/README.md) for the full reverse-engineered protocol documentation
- Proxy server: see [`oupes_mega_wifi_proxy/README.md`](../oupes_mega_wifi_proxy/README.md) for the broker/proxy setup
