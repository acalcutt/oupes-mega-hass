# OUPES Mega — Home Assistant Integrations

Custom Home Assistant integrations for **OUPES Mega** power stations — fully
local, no cloud dependency required. Supports both **Bluetooth (BLE)** and
**WiFi** communication channels.

---

## Integrations

This repo contains three custom components that can be installed independently
depending on your setup:

### 1. BLE Integration (`oupes_mega_ble`)

**Direct Bluetooth connection** — the simplest setup. Connects to your OUPES
device over BLE and exposes sensors, switches, and settings entities.

- No network infrastructure needed — just a Bluetooth adapter on your HA server
- Continuous or polled connection modes
- Supports BLE pairing (no Cleanergy app/cloud needed)
- Device control: toggle AC/DC/USB outputs, adjust settings

**[Full documentation →](custom_components/oupes_mega_ble/README.md)**

### 2. WiFi Proxy (`oupes_mega_wifi_proxy`)

**Local cloud replacement** — intercepts the device's outbound connection to the
OUPES cloud broker and serves it locally. Required for WiFi-based telemetry.

- TCP broker proxy (port 8896) — device connects here instead of the cloud
- HTTP API emulator (port 8897) — handles Cleanergy app REST calls
- SiBo HTTPS stub (port 8898) — prevents app "token error" login loops
- Requires firewall NAT rules to redirect device/app traffic

**[Full documentation →](custom_components/oupes_mega_wifi_proxy/README.md)**

### 3. WiFi Client (`oupes_mega_wifi_client`)

**WiFi entity provider** — connects to the local proxy broker as a TCP client
and exposes WiFi telemetry as HA entities. Requires the WiFi Proxy above.

- Push-based telemetry (no polling) — entities update in real time
- Same sensors as BLE (battery, power, temperature, runtime)
- Output control via TCP commands
- Automatic reconnection

**[Full documentation →](custom_components/oupes_mega_wifi_client/README.md)**

---

## Which should I use?

| Scenario | Install |
|----------|---------|
| Simple local-only setup, device within BLE range | `oupes_mega_ble` only |
| Device too far for BLE, or you want WiFi telemetry | `oupes_mega_wifi_proxy` + `oupes_mega_wifi_client` |
| Want both channels for redundancy | All three |

**BLE is the easiest path.** It works entirely over Bluetooth with zero network
configuration — no firewall rules, no port forwarding, no DNS tricks. Just plug
in a USB Bluetooth adapter (or use ESPHome BLE Proxy) and go.

**WiFi requires network-level redirection.** The device firmware hardcodes the
cloud broker IP (`47.252.10.9`), so you need firewall NAT rules
to intercept the device's outbound connections and redirect them to your HA
instance. This is more powerful (push-based, real-time data, works at any
distance) but involves a more complex setup. See the
[WiFi Proxy README](custom_components/oupes_mega_wifi_proxy/README.md) for the
full NAT rule table.

The BLE and WiFi integrations can run simultaneously — they use independent
communication channels and create separate device/entity sets.

---

## Quick Start (BLE)

1. Copy `custom_components/oupes_mega_ble/` into your HA config directory.
2. Restart Home Assistant.
3. Power on the OUPES device and press the IoT button (indicator flashes).
4. HA auto-discovers the device — click the notification to set up.
5. Choose **Create New Key** (factory-reset the device first: hold IoT 5 s).

## Quick Start (WiFi)

1. Copy both `custom_components/oupes_mega_wifi_proxy/` and
   `custom_components/oupes_mega_wifi_client/` into your HA config directory.
2. Restart Home Assistant.
3. Add the **OUPES Mega WiFi Proxy** integration first — configure ports.
4. Set up NAT rules on your firewall/router to redirect `47.252.10.9:8896` → HA.
5. Add the **OUPES Mega WiFi Client** integration — log in to discover devices.

---

## Supported Models

| Series | Models |
|--------|--------|
| **Mega** | Mega 1, Mega 2, Mega 3, Mega 5 |
| **Exodus** | Exodus 1200, Exodus 1500, Exodus 2400, S024 Lite, S1 Lite |
| **Guardian** | Guardian 6000, HP2500, D5 V2 |
| **Other** | S2 V2, DC 800, LP350, LP700, PB300, UPS 1200, UPS 1800 |

Model-specific features (settings, entity names) are applied automatically
based on the BLE product ID.

---

## Protocol Documentation

See [`debug_info/README.md`](debug_info/README.md) for the complete
reverse-engineered protocol reference, covering:

- BLE GATT profile, pairing/claiming protocol, packet format
- WiFi TCP broker protocol, streaming activation sequence
- Telemetry attribute map (shared across BLE and WiFi)
- Cloud API endpoints (HTTP REST + SiBo)
- Device firmware boot sequence

## Debug Tools

The [`debug_info/`](debug_info/) directory contains standalone tools:

| Script | Purpose |
|--------|---------|
| `pair_device.py` | BLE pairing + WiFi provisioning |
| `scan_ble.py` | Live BLE telemetry scanner |
| `parse_btsnoop.py` | Parse Android btsnoop HCI logs |
| `probe_key.py` | Test candidate device keys |
| `ble_diag.py` | BLE GATT diagnostics |
| `provision_wifi.py` | Send WiFi credentials to paired device |
| `scan_wifi_ports.py` | Scan device for open network ports |
| `analyze_attr_csv.py` | Analyze BLE attribute debug logs |

---

## License

See [LICENSE](LICENSE).
