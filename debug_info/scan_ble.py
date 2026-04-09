"""
scan_ble.py — Scan for OUPES Mega 1 "TT" BLE devices and display live telemetry.

Usage:
    python scan_ble.py --key <your_10_hex_char_device_key>

Requires:
    pip install bleak

Key BLE facts (from HCI snoop capture):
  Service:          00001910-0000-1000-8000-00805f9b34fb
  Write char:       00002b11  (handle 0x0003)  write-without-response
  Notify char:      00002b10  (handle 0x0005)  notify
  CCCD descriptor:  handle 0x0006  → write 0x0100 to enable notifications

  The device pushes telemetry automatically upon connection (~350ms in).
  Explicitly writing CCCD 0x0100 to handle 0x0006 is required on Windows
  so the WinRT stack delivers the notifications to the application.

  The app also sends an 11-packet initialization sequence to handle 0x0003
  immediately after the CCCD write.
"""

import asyncio
import sys
import argparse
from datetime import timedelta
from bleak import BleakScanner, BleakClient
from bleak.exc import BleakError

SERVICE_UUID = "00001910-0000-1000-8000-00805f9b34fb"
CHAR_UUID    = "00002b10-0000-1000-8000-00805f9b34fb"

# Attribute map — numbers match both BLE and WiFi/cloud protocol
ATTR_MAP = {
    1:   ("Output Enable Bitmask",       "raw"),   # bit0=AC, bit1=DC12V, bit2=USB
    2:   ("Unknown (attr 2)",           "raw"),   # possibly legacy output flag; always 0
    3:   ("Battery %",                  "pct"),
    4:   ("Total Output Power",         "W"),     # AC + DC12V + USB-C + USB-A combined
    5:   ("AC Inverter Output Power",   "W"),     # pure AC output; 0 when AC disabled
    6:   ("DC 12V Output Power",        "W"),     # cigarette-lighter port
    7:   ("USB-C Output Power",         "W"),
    8:   ("USB-A Output Power",         "W"),
    9:   ("Unknown (attr 9)",           "raw"),   # always 0 in all captures
    21:  ("Total Input Power",           "W"),    # grid + solar (incl. B2 secondary ports)
    22:  ("Grid Input Power",            "W"),
    23:  ("Solar Input Power",           "W"),    # MPPT; 1 = noise floor with nothing connected
    30:  ("Remaining Runtime",          "min"),
    32:  ("Main Unit Temperature",       "F/10"), # ÷10 = °F (e.g. 963 → 96.3 °F)
    51:  ("Ext Battery Count",           "raw"),  # number of B2 expansion batteries connected
    53:  ("B2 Input Power",             "W"),    # B2 secondary port (solar/DC in)
    54:  ("B2 Output Power",            "W"),    # B2 total output (chain + USB/accessories)
    84:  ("AC Output Control",          "bool"),
    105: ("AC Inverter Protection",    "bool"),  # 1 = inverter protection/thermal warning (~60s delayed after hardware trip)
                                                   #   AC output suppressed for 8-10 min recovery; fans may struggle; also 1 at elevated temp during run
                                                   # 0 = normal; attr 32 rises 949→970 correlated with thermal events
}

# Attrs that belong to a specific external battery slot.
# Attr 101 carries the slot index (1 or 2) in the same packet as these.
EXT_BATTERY_ATTRS = {78, 79, 80}
EXT_BATTERY_LABELS = {
    78: ("Remaining Runtime",  "min"),
    79: ("Battery %",          "pct"),
    80: ("Temperature",        "F/10"),
}

CHARGE_MODE = {2: "AC Charging"}
CHARGE_SRC  = {1: "AC", 2: "Solar/DC"}


def format_value(raw: int, unit: str) -> str:
    if unit == "bool":
        return "On" if raw else "Off"
    if unit == "pct":
        return f"{raw}%"
    if unit == "W":
        return f"{raw} W"
    if unit == "min":
        td = timedelta(minutes=raw)
        days = td.days
        hours, rem = divmod(td.seconds, 3600)
        mins = rem // 60
        return f"{raw} min  ({days}d {hours}h {mins}m)"
    if unit == "V/10":
        return f"{raw / 10:.1f} V"
    if unit == "V/100":
        return f"{raw / 100:.2f} V"
    if unit == "F/10":
        return f"{raw / 10:.1f} °F"
    if unit == "chargemode":
        return CHARGE_MODE.get(raw, f"unknown ({raw})")
    if unit == "chargesrc":
        return CHARGE_SRC.get(raw, f"unknown ({raw})")
    return str(raw)


def parse_ble_packet(data: bytearray) -> dict[int, int]:
    """Parse a BLE notification packet into {attr: raw_value}.

    Handles two formats observed in HCI capture:
      Type 0x00 / 0x01 / 0x81 / 0x82 — standard TLV stream:
        [0x01][type][0x0A][len][attr][value bytes...][checksum]
      Type 0x80 — single-value response packet (different layout):
        [0x01][0x80][subtype][...raw fields...]
        Not TLV-encoded; logged as raw for now.
    """
    results = {}
    if len(data) < 3:
        return results

    pkt_type = data[1]

    # 0x80 / 0x81 — not standard TLV; try to extract field at fixed offsets
    if pkt_type in (0x80, 0x81):
        # Try treating bytes 2+ as TLV anyway (some 0x81 packets do carry TLV)
        i = 2
        while i < len(data) - 1:
            if data[i] == 0x0A and i + 2 < len(data):
                length = data[i + 1]
                if length >= 1 and i + 2 + length <= len(data) - 1:
                    attr = data[i + 2]
                    val_bytes = data[i + 3 : i + 2 + length]
                    results[attr] = int.from_bytes(val_bytes, "little") if val_bytes else 0
                i += 2 + length
            else:
                i += 1
        return results

    # 0x82 — end-of-group marker, all zeros body; skip
    if pkt_type == 0x82:
        return results

    # 0x00 / 0x01 — standard TLV packets
    i = 2  # skip 2-byte header (start marker + type)
    while i < len(data) - 1:  # last byte is checksum
        if data[i] == 0x0A and i + 2 < len(data):
            length = data[i + 1]
            if length >= 1 and i + 2 + length <= len(data) - 1:
                attr = data[i + 2]
                val_bytes = data[i + 3 : i + 2 + length]
                results[attr] = int.from_bytes(val_bytes, "little") if val_bytes else 0
            i += 2 + length
        else:
            i += 1
    return results


class DeviceState:
    """Accumulates telemetry attrs across multiple BLE packets for one device.

    External battery data (attrs 78/79/80) arrives in groups tagged by attr 101
    (value 1 or 2 = which battery slot).  We store them in separate per-slot
    dicts so the summary can display both batteries independently.
    """

    def __init__(self, address: str, name: str):
        self.address = address
        self.name = name
        self.attrs: dict[int, int] = {}
        # ext_batteries[slot] = {78: runtime, 79: pct, 80: temp}
        self.ext_batteries: dict[int, dict[int, int]] = {}  # keyed by slot index; created on first seen
        self._current_slot: int = 1  # updated when attr 101 is seen
        self.packet_count = 0

    def update(self, new_attrs: dict[int, int]) -> None:
        self.packet_count += 1
        # If this packet includes the group/slot index, update current slot
        if 101 in new_attrs:
            slot = new_attrs[101]
            self._current_slot = slot
            if slot not in self.ext_batteries:
                self.ext_batteries[slot] = {}
        # Route ext battery attrs into their slot; everything else into main attrs
        for attr, val in new_attrs.items():
            if attr in EXT_BATTERY_ATTRS:
                self.ext_batteries[self._current_slot][attr] = val
            elif attr != 101:  # don't surface the internal group index
                self.attrs[attr] = val

    def display(self) -> None:
        print(f"\n{'='*60}")
        print(f"  Device : {self.name}  [{self.address}]")
        print(f"  Packets received : {self.packet_count}")
        print(f"{'='*60}")
        if not self.attrs and not any(self.ext_batteries.values()):
            print("  (no data yet)")
            return
        for attr_num in sorted(self.attrs):
            raw = self.attrs[attr_num]
            if attr_num in ATTR_MAP:
                label, unit = ATTR_MAP[attr_num]
                value_str = format_value(raw, unit)
                confidence = ""
            else:
                label = f"attr_{attr_num}"
                value_str = str(raw)
                confidence = " (?)"
            print(f"  [{attr_num:>3}] {label:<35} {value_str}{confidence}")
        # Show each external battery slot that has data (any number of slots)
        for slot in sorted(self.ext_batteries):
            batt = self.ext_batteries[slot]
            if not batt:
                continue
            print(f"  --- Ext Battery {slot} ---")
            for attr_num in (79, 78, 80):  # pct, runtime, temp
                if attr_num not in batt:
                    continue
                label, unit = EXT_BATTERY_LABELS[attr_num]
                print(f"  [{attr_num:>3}] {label:<35} {format_value(batt[attr_num], unit)}")
        print()


# Known ATT handles (from HCI snoop capture — no GATT discovery needed)
HANDLE_WRITE = 0x0003  # 00002b11 write-without-response
HANDLE_NOTIF = 0x0005  # 00002b10 notify
HANDLE_CCCD  = 0x0006  # CCCD descriptor for notify char

# Keepalive packet — sent every 10 s after init to prevent session timeout.
# The device disconnects ~10 s after the last keepalive if none is received.
# First keepalive is sent ~6 s after init (matching the Cleanergy app timing).
# The device echoes the packet back as an ACK.
KEEPALIVE_PKT           = bytes.fromhex("0180030254010000000000000000000000000076")
KEEPALIVE_FIRST_DELAY   = 6.0   # seconds after init sequence completes
KEEPALIVE_INTERVAL      = 10.0  # seconds between subsequent keepalives

# Initialization sequence captured from the Cleanergy app.
# Sent to HANDLE_WRITE after connecting. The device sends telemetry
# autonomously but this sequence completes the app handshake.
# Packet 6 (index 5) contains a device serial/token string embedded at bytes 4-13.

def _crc8(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc

def build_init_sequence(device_key: str) -> list[bytes]:
    """Build the 11-packet init sequence with the given 10-char hex device key."""
    # Base packets — packet 6 will have the key injected
    base = [
        bytearray.fromhex("0100019901010101010101010101010101010100"),
        bytearray.fromhex("0101010101010101010101010101010101010100"),
        bytearray.fromhex("0102000000000000000000000000000000000000"),
        bytearray.fromhex("0103000000000000000000000000000000000000"),
        bytearray.fromhex("0104000000000000000000000000000000000000"),
        bytearray.fromhex("0105000000000000000000000000000000000000"),
        bytearray.fromhex("0106000000000000000000000000000000000000"),  # key goes here
        bytearray.fromhex("0107000000000000000000000000000000000000"),
        bytearray.fromhex("0108000000000000000000000000000000000000"),
        bytearray.fromhex("0189000000000000000000000000000000000000"),
        bytearray.fromhex("0180020101000000000000000000000000000000"),
    ]
    # Inject device_key ASCII into packet 6, bytes [4:14]
    key_bytes = device_key.encode("ascii")
    base[6][4:4 + len(key_bytes)] = key_bytes
    # Compute CRC-8 for each packet
    result = []
    for pkt in base:
        pkt[19] = _crc8(bytes(pkt[:19]))
        result.append(bytes(pkt))
    return result


async def _connect_and_collect(
    address: str,
    write_char: str,
    handler,
    duration: float,
    init_sequence: list[bytes],
) -> tuple[bool, bool]:
    """
    Single connection attempt.  Returns (dropped_quickly, got_data).
    dropped_quickly = device dropped in <2 s with no data (cold-probe drop — retry).
    got_data        = at least one parsed TLV packet was received.
    """
    import time as _time

    disconnected_event = asyncio.Event()
    connect_ts = _time.monotonic()
    got_data = False

    def on_disconnect(_client):
        uptime = _time.monotonic() - connect_ts
        print(f"  *** Device disconnected (after {uptime:.1f}s) ***")
        disconnected_event.set()

    try:
        async with BleakClient(address, timeout=15.0,
                               disconnected_callback=on_disconnect) as client:
            print("  Connected.")

            # ── Step 1: wait ~1.8 s to match Android GATT-discovery timing ───
            # Android spends ~1.8 s on GATT service discovery before writing
            # CCCD.  If we write CCCD immediately the device behaves differently.
            await asyncio.sleep(1.8)
            if disconnected_event.is_set():
                uptime = _time.monotonic() - connect_ts
                return (uptime < 2.0 and not got_data), got_data

            # ── Step 2: subscribe (writes CCCD 0x0100) ────────────────────────
            def _handler(sender, data: bytearray) -> None:
                nonlocal got_data
                pkt_type = data[1] if len(data) > 1 else 0
                parsed = parse_ble_packet(data)
                label = {0x00: "data-first", 0x01: "data-cont",
                         0x80: "handshake",  0x81: "handshake-cont",
                         0x82: "end-marker"}.get(pkt_type, f"0x{pkt_type:02x}")
                if parsed:
                    print(f"  [RX {label}] {data.hex()}  → {parsed}")
                    got_data = True
                    handler(parsed)
                else:
                    print(f"  [RX {label}] {data.hex()}")

            await client.start_notify(CHAR_UUID, _handler)
            print(f"  Subscribed to {CHAR_UUID}")

            # ── Step 3: send init sequence (~200 ms after CCCD, per capture) ──
            await asyncio.sleep(0.2)
            if disconnected_event.is_set():
                return True, got_data
            print(f"  Sending {len(init_sequence)}-packet init sequence ...")
            for i, pkt in enumerate(init_sequence):
                if disconnected_event.is_set():
                    print(f"  Disconnected during init at packet {i}")
                    return True, got_data
                try:
                    await client.write_gatt_char(write_char, pkt, response=False)
                    await asyncio.sleep(0.01)
                except BleakError as exc:
                    print(f"  Init packet {i} failed: {exc}")

            # ── Step 4: keepalive loop + collect notifications ────────────────
            # After init the device requires a keepalive every 10 s or it drops
            # the connection.  The first keepalive is sent ~6 s after init
            # (matching Cleanergy app timing from HCI capture).
            async def _keepalive_loop() -> None:
                await asyncio.sleep(KEEPALIVE_FIRST_DELAY)
                ka_count = 0
                while not disconnected_event.is_set():
                    try:
                        await client.write_gatt_char(write_char, KEEPALIVE_PKT,
                                                     response=False)
                        ka_count += 1
                        print(f"  [TX keepalive #{ka_count}]")
                    except BleakError:
                        break
                    await asyncio.sleep(KEEPALIVE_INTERVAL)

            keepalive_task = asyncio.create_task(_keepalive_loop())

            print(f"  Listening for {duration:.0f}s (keepalive every {KEEPALIVE_INTERVAL:.0f}s) ...")
            try:
                await asyncio.wait_for(disconnected_event.wait(), timeout=duration)
                dropped_quickly = False  # stayed long enough to be useful
            except asyncio.TimeoutError:
                dropped_quickly = False  # full duration completed normally

            keepalive_task.cancel()
            try:
                await keepalive_task
            except (asyncio.CancelledError, BleakError):
                pass

            try:
                await client.stop_notify(CHAR_UUID)
            except BleakError:
                pass
            return dropped_quickly, got_data

    except BleakError as exc:
        print(f"  BLE error on {address}: {exc}")
    except asyncio.TimeoutError:
        print(f"  Timeout connecting to {address}")

    return False, got_data


async def monitor_device(device, duration: float = 20.0, device_key: str = "") -> DeviceState:
    """Connect to a single TT device, collect notifications for `duration` seconds.

    The OUPES Mega 1 sometimes makes a "cold probe" connection that drops in
    <400 ms with no data (BLE reason 0x3e).  This is normal — just retry.
    Once a real session is established:
      • CCCD is written at ~1.8 s (after GATT discovery delay)
      • Init sequence is sent ~200 ms later
      • Device responds with 3× 0x80 handshake ACK packets
      • Telemetry (TLV) packets stream continuously
      • A keepalive must be sent every 10 s or the device drops the connection
    """
    state = DeviceState(device.address, device.name or "TT")
    write_char = CHAR_UUID.replace("2b10", "2b11")
    init_sequence = build_init_sequence(device_key) if device_key else build_init_sequence("0000000000")
    print(f"\nConnecting to {device.name} [{device.address}] ...")

    def accumulate(parsed: dict) -> None:
        state.update(parsed)

    MAX_ATTEMPTS = 5
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if attempt > 1:
            wait = 3 if attempt <= 3 else 6
            print(f"\n  Retry attempt {attempt}/{MAX_ATTEMPTS} (waiting {wait} s) ...")
            await asyncio.sleep(wait)

        dropped_quickly, got_data = await _connect_and_collect(
            device.address, write_char, accumulate, duration, init_sequence
        )

        if not dropped_quickly:
            break  # either ran full duration or got a proper session
        print(f"  Cold-probe drop detected — will retry.")

    return state


async def main() -> None:
    parser = argparse.ArgumentParser(description="Scan for OUPES Mega 1 'TT' BLE devices and display live telemetry.")
    parser.add_argument("--key", required=True, help="10-character hex device key (e.g., bd236b1695)")
    args = parser.parse_args()

    device_key = args.key.strip().lower()
    if len(device_key) != 10 or not all(c in "0123456789abcdef" for c in device_key):
        print("Error: --key must be exactly 10 hex characters (e.g., bd236b1695)")
        sys.exit(1)

    print("Scanning for 'TT' BLE devices (10 s) ...")
    scan_results: dict = await BleakScanner.discover(timeout=10.0, return_adv=True)
    # return_adv=True → {address: (BLEDevice, AdvertisementData)}
    tt_devices = [
        (dev, adv)
        for dev, adv in scan_results.values()
        if (dev.name or "").strip().upper() == "TT"
    ]

    if not tt_devices:
        print("No 'TT' devices found. Make sure the power station is on and in range.")
        sys.exit(0)

    print(f"Found {len(tt_devices)} 'TT' device(s):")
    for dev, adv in tt_devices:
        print(f"  {dev.name}  [{dev.address}]  RSSI: {adv.rssi} dBm")

    # Connect to all devices concurrently — each runs its own 20 s session in
    # parallel so total wall-clock time stays ~20 s regardless of device count.
    states: list[DeviceState] = await asyncio.gather(
        *(monitor_device(dev, duration=20.0, device_key=device_key) for dev, _ in tt_devices)
    )

    # Final summary
    print("\n\n" + "#" * 60)
    print("  TELEMETRY SUMMARY")
    print("#" * 60)
    for state in states:
        state.display()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
