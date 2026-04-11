"""BLE protocol constants and packet parser for the OUPES Mega 1.

All values here were reverse-engineered from an Android HCI snoop capture
(btsnoop_hci.log) of the official Cleanergy app.
"""

# ── GATT identifiers ──────────────────────────────────────────────────────────

SERVICE_UUID     = "00001910-0000-1000-8000-00805f9b34fb"
WRITE_CHAR_UUID  = "00002b11-0000-1000-8000-00805f9b34fb"  # write-without-response
NOTIFY_CHAR_UUID = "00002b10-0000-1000-8000-00805f9b34fb"  # notify

# ── Keepalive ─────────────────────────────────────────────────────────────────
# Without this the device terminates the session exactly 10 s after last tx.

KEEPALIVE_PKT          = bytes.fromhex("0180030254010000000000000000000000000076")
KEEPALIVE_FIRST_DELAY  = 6.0   # seconds after init sequence completes
KEEPALIVE_INTERVAL     = 10.0  # seconds between subsequent keepalives

# ── Initialization sequence ───────────────────────────────────────────────────
# The 11 packets below are sent to WRITE_CHAR_UUID immediately after
# subscribing to notifications.  Packet index 6 embeds a per-device token
# ("bd236b1695") at bytes 4-13; replace this if connecting to a different unit.

APP_INIT_SEQUENCE = [
    bytes.fromhex("0100019901010101010101010101010101010192"),
    bytes.fromhex("010101010101010101010101010101010101018f"),
    bytes.fromhex("0102000000000000000000000000000000000082"),
    bytes.fromhex("01030000000000000000000000000000000000a8"),
    bytes.fromhex("010400000000000000000000000000000000007e"),
    bytes.fromhex("0105000000000000000000000000000000000054"),
    bytes.fromhex("01060000626432333662313639350000000000d7"),  # "bd236b1695" token
    bytes.fromhex("0107000000000000000000000000000000000000"),
    bytes.fromhex("0108000000000000000000000000000000000081"),
    bytes.fromhex("01890000000000000000000000000000000000c0"),
    bytes.fromhex("0180020101000000000000000000000000000016"),
]

# ── Attribute maps ────────────────────────────────────────────────────────────
# Attr numbers are consistent between BLE and the WiFi/cloud protocol.
# "bool" attrs are exposed as binary sensors; all others as regular sensors.

ATTR_MAP: dict[int, tuple[str, str]] = {
    1:   ("AC Output",               "bool"),
    2:   ("DC Output",               "bool"),
    3:   ("Battery",                 "pct"),
    4:   ("AC Output Power",         "W"),
    5:   ("Unknown (attr 5)",       "raw"),  # ⚠️ mirrors AC output W in all conditions (incl. pure discharge); likely a second AC output measurement point
    6:   ("DC 12V Output",          "W"),   # cigarette lighter / car charger port
    7:   ("USB-C Output",            "W"),   # confirmed USB-C port output wattage
    8:   ("USB-A Output",            "W"),
    9:   ("Unknown",                 "raw"),
    21:  ("Total Input Power",       "W"),
    22:  ("Grid Input Power",        "W"),
    23:  ("Solar Input Power",       "W"),   # confirmed: 0 with no solar, tracks app SOLAR reading exactly
    30:  ("Remaining Runtime",       "min"),   # very inaccurate with no load or variable load (e.g. 5940 = 99h when outputs off/low)
    32:  ("Main Unit Temperature",    "F/10"),  # ÷10 = temperature in °F — confirmed always °F regardless of app unit setting
                                                   # (btsnoop across F→C→F app switch showed smooth cooling trend, never
                                                   # dropped to ~357 range; firmware always sends in °F)
    51:  ("Unknown (attr 51)",       "raw"),  # constant=2 in all sessions; attr 51=2 in both confirmed-Slow (br8) AND confirmed-Fast (br9) charging modes → NOT the charging mode indicator
    53:  ("Unknown (attr 53)",       "raw"),
    54:  ("Unknown (attr 54)",       "raw"),
    84:  ("AC Output Control",       "bool"),
    105: ("Charge Mode",               "bool"),  # 1 = Fast Charge (factory default), 0 = Slow Charge
                                                    #   APK: DeviceSettingFragment clickFastCharge/clickSlowCharge → Cmd3 DPID 105
                                                    #   S2_V2DetailFragment queries {105} via Cmd2 at initData()
                                                    #   Pre-conditions (APK): ledSw0==0 AND acInput==0 before toggling
}

# Attrs that arrive grouped by slot; attr 101 carries the slot index (1 or 2).
# On the OUPES Mega 1, up to 2 external battery packs connect via a single
# expansion port.  Attr 51 reflects the count of connected packs;
# attrs 78+101 are in one packet type; attrs 79+80 in a separate packet type
# (never share a packet with 78/101):
#   78 + 101: per-pack MULTIPLEXED data — see attr 78 note below
#   79 + 80:  external battery SoC (direct %) + battery temperature in 0.1 °F
EXT_BATTERY_ATTRS: set[int] = {53, 54, 78, 79, 80}
EXT_BATTERY_MAP: dict[int, tuple[str, str]] = {
    78: ("Remaining Runtime / Voltage", "min/mV"),
                                                  # MULTIPLEXED by value range:
                                                  #   0–5940      = per-pack remaining runtime in minutes
                                                  #                 (5940 = charging/idle sentinel ≈ 99 h)
                                                  #   44000–58500 = battery pack voltage in millivolts ← CONFIRMED
                                                  #                 e.g. 53025 = 53.025 V (51.2 V nominal LiFePO4,
                                                  #                 range 44 V empty → 58.4 V full charge)
                                                  #                 correlation: higher mV = higher SoC ✓
                                                  #   8000–30000  = unknown; values spaced ~1690 apart,
                                                  #                 possibly time-to-full estimate in some unit
    79: ("External Battery SoC",        "%"),     # direct battery % (0–100); raw value = % confirmed
    80: ("Temperature",                 "F/10"),  # external battery temperature ×0.1 °F (e.g. 878 → 87.8 °F) — confirmed vs app display
}

# Convenience set of attrs that should become binary sensors
BOOL_ATTRS = {attr for attr, (_, unit) in ATTR_MAP.items() if unit == "bool"}

# ── Output bitmask bits (attr 1) ──────────────────────────────────────────────
# Bit positions in the attr-1 bitmask sent by the device and written to control
# each output independently.  Confirmed by correlating HCI write commands with
# matching attr-1 notification values in the btsnoop captures.
OUTPUT_AC_BIT    = 0x01   # bit 0 — AC inverter output
OUTPUT_DC12V_BIT = 0x02   # bit 1 — DC 12 V cigarette-lighter output
OUTPUT_USB_BIT   = 0x04   # bit 2 — USB-A / USB-C combined output


def _crc8(data: bytes) -> int:
    """CRC-8 (SMBUS) over `data` using polynomial 0x07, init 0x00."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def build_output_command(bitmask: int) -> bytes:
    """Build a 20-byte BLE write command that sets the output-enable bitmask.

    Args:
        bitmask: new value for attr 1 (OR the OUTPUT_*_BIT constants you want ON).

    Returns:
        20-byte packet ready to be written to WRITE_CHAR_UUID.
    """
    pkt = bytearray(20)
    pkt[0] = 0x01
    pkt[1] = 0x80
    pkt[2] = 0x03
    pkt[3] = 0x02
    pkt[4] = 0x01       # attr number
    pkt[5] = bitmask & 0xFF
    pkt[19] = _crc8(bytes(pkt[:19]))
    return bytes(pkt)


def _int_byte_size(value: int) -> int:
    """Return the minimum number of bytes needed to represent *value* (1, 2, or 4)."""
    if value < 0:
        value = value & 0xFFFFFFFF  # treat as unsigned 32-bit
    if value <= 0xFF:
        return 1
    if value <= 0xFFFF:
        return 2
    return 4


def build_setting_command(dpid: int, value: int) -> bytes:
    """Build a 20-byte BLE Cmd3 packet that writes a single device setting.

    This is the Cmd3 protocol used by the Cleanergy app to set standby
    timeouts, ECO mode, silent mode, etc.

    Packet layout: ``01 80 03 <total_len> <dpid> <value_le…> 00… <crc8>``

    Args:
        dpid:  The device property ID (e.g. 45 for machine standby).
        value: Integer value to write (in the unit expected by the device,
               e.g. seconds for standby timeouts).

    Returns:
        20-byte packet ready to be written to WRITE_CHAR_UUID.
    """
    val_size = _int_byte_size(value)
    val_bytes = (value & (0xFFFFFFFF if val_size == 4 else (1 << (val_size * 8)) - 1)).to_bytes(val_size, "little")
    # total_len = 1 (dpid byte) + val_size
    total_len = 1 + val_size

    pkt = bytearray(20)
    pkt[0] = 0x01
    pkt[1] = 0x80
    pkt[2] = 0x03       # Cmd3
    pkt[3] = total_len
    pkt[4] = dpid & 0xFF
    pkt[5: 5 + val_size] = val_bytes
    pkt[19] = _crc8(bytes(pkt[:19]))
    return bytes(pkt)


def build_query_command(dpids: list[int]) -> bytes:
    """Build a 20-byte BLE Cmd2 packet that queries current setting values.

    The device responds with TLV notification packets containing the current
    values of the requested DPIDs, using the same format as telemetry.

    Packet layout: ``01 80 02 <count> <dpid1> <dpid2> ... 00… <crc8>``

    Args:
        dpids: List of DPID numbers to query (max 15 per packet).

    Returns:
        20-byte packet ready to be written to WRITE_CHAR_UUID.
    """
    count = min(len(dpids), 15)  # bytes 4-18 = max 15 DPIDs
    pkt = bytearray(20)
    pkt[0] = 0x01
    pkt[1] = 0x80
    pkt[2] = 0x02       # Cmd2 = query/read
    pkt[3] = count
    for i in range(count):
        pkt[4 + i] = dpids[i] & 0xFF
    pkt[19] = _crc8(bytes(pkt[:19]))
    return bytes(pkt)


def build_query_commands(dpids: list[int], batch_size: int = 7) -> list[bytes]:
    """Build one or more Cmd2 query packets, split into small batches.

    The official Cleanergy app sends settings queries in groups of 5-8 DPIDs.
    Splitting large DPID lists into small batches avoids potential firmware
    limitations on single-packet query size.

    Args:
        dpids:      Sorted list of DPID numbers to query.
        batch_size: Max DPIDs per packet (default 7, matching app behaviour).

    Returns:
        List of 20-byte packets.
    """
    packets: list[bytes] = []
    for start in range(0, len(dpids), batch_size):
        batch = dpids[start : start + batch_size]
        packets.append(build_query_command(batch))
    return packets


def build_init_sequence(device_key: str = "bd236b1695") -> list[bytes]:
    """Return APP_INIT_SEQUENCE with packet 6 rebuilt for *device_key*.

    Packet 6 (index 6) embeds the per-device 10-character ASCII hex token at
    bytes 4–13.  All other packets are identical across devices.
    """
    key_bytes = device_key.encode("ascii").ljust(10, b"\x00")[:10]
    pkt6 = bytearray(20)
    pkt6[0:4] = b"\x01\x06\x00\x00"
    pkt6[4:14] = key_bytes
    pkt6[19] = _crc8(bytes(pkt6[:19]))
    pkts = list(APP_INIT_SEQUENCE)
    pkts[6] = bytes(pkt6)
    return pkts


# ── Packet parser ─────────────────────────────────────────────────────────────

def parse_ble_packet(data: bytearray) -> dict[int, int]:
    """Parse a BLE notification packet into {attr: raw_value}.

    Byte 1 of each 20-byte notification is ``pkgSn``: a packet-sequence number
    where the low 7 bits are the packet index and bit 7 is the "last" flag.

    Two TLV formats appear on the wire:

    * **Standard (Cmd1 telemetry):** ``[0x0A][length][attr][value…]``
      Each entry is prefixed by a 0x0A tag byte.

    * **Compact (Cmd2/Cmd3 settings responses):** ``[length][attr][value…]``
      No 0x0A tag.  After the cmd-echo byte (0x02 or 0x03 at data[2]),
      entries are packed back-to-back with a 0x00 length terminator.
      Confirmed by ``BleCmdResultBuildParser.getCmd2_3_10Result`` in the APK.

    Index-0 packets (pkgSn 0x00 or 0x80) carry the cmd-echo byte at
    data[2].  Continuation packets (index > 0) continue the TLV stream.
    """
    results: dict[int, int] = {}
    if len(data) < 3:
        return results

    pkt_type = data[1]
    pkt_index = pkt_type & 0x7F

    # Index-0 packets carry a cmd-echo byte at data[2] for Cmd2/Cmd3
    # responses.  Telemetry (Cmd1) packets start with 0x0A instead.
    is_settings_response = pkt_index == 0 and data[2] in (0x02, 0x03)

    if is_settings_response:
        i = 3  # skip cmd-echo; compact TLV follows
    else:
        i = 2

    while i < len(data) - 1:  # last byte is checksum
        if data[i] == 0x0A and i + 2 < len(data):
            # Standard form: [0x0A][length][attr][val…]
            length = data[i + 1]
            if length >= 1 and i + 2 + length <= len(data) - 1:
                attr = data[i + 2]
                val_bytes = data[i + 3: i + 2 + length]
                results[attr] = int.from_bytes(val_bytes, "little") if val_bytes else 0
            i += 2 + length
        elif (is_settings_response or pkt_index > 0) and 1 <= data[i] <= 8 and i + 1 + data[i] <= len(data) - 1:
            # Compact form: [length][attr][val…]  — no 0x0A tag.
            # Used in Cmd2/Cmd3 settings responses (confirmed by APK) and
            # continuation packets where the firmware omits the tag byte.
            length = data[i]
            attr = data[i + 1]
            val_bytes = data[i + 2: i + 1 + length]
            results[attr] = int.from_bytes(val_bytes, "little") if val_bytes else 0
            i += 1 + length
        elif is_settings_response and data[i] == 0x00:
            break  # Cmd2/3 length-zero terminator
        else:
            i += 1

    return results


def parse_packet_sequence(packets: list[bytearray]) -> dict[int, int]:
    """Reassemble a multi-packet BLE sequence and parse TLVs.

    Each 20-byte BLE notification has:
      byte 0  – fixed header (0x01)
      byte 1  – pkgSn (low 7 bits = index, bit 7 = last flag)
      bytes 2–18 – payload (TLV data)
      byte 19 – checksum

    When a TLV spans the boundary between two packets, parsing each one
    independently truncates it.  This function concatenates the payloads
    of all packets in a sequence, then parses the combined TLV stream.

    For index-0 packets that are Cmd2/Cmd3 settings responses (echo byte
    0x02 or 0x03 at data[2]), the echo byte is stripped before TLV parsing.
    """
    if not packets:
        return {}

    # Single-packet fast path: delegate to per-packet parser
    first = packets[0]
    if len(packets) == 1:
        return parse_ble_packet(first)

    # Determine if this is a settings response from the first packet
    is_settings = len(first) >= 3 and (first[1] & 0x7F) == 0 and first[2] in (0x02, 0x03)

    # Extract payload from each packet (strip header, pkgSn, checksum)
    payload = bytearray()
    for idx, pkt in enumerate(packets):
        if len(pkt) < 3:
            continue
        start = 3 if (idx == 0 and is_settings) else 2
        end = len(pkt) - 1  # exclude checksum
        if start < end:
            payload.extend(pkt[start:end])

    # Parse the combined TLV stream
    results: dict[int, int] = {}
    j = 0
    while j < len(payload):
        if payload[j] == 0x0A and j + 2 < len(payload):
            length = payload[j + 1]
            if length >= 1 and j + 2 + length <= len(payload):
                attr = payload[j + 2]
                val_bytes = payload[j + 3 : j + 2 + length]
                results[attr] = int.from_bytes(val_bytes, "little") if val_bytes else 0
            j += 2 + length
        elif is_settings and 1 <= payload[j] <= 8 and j + 1 + payload[j] <= len(payload):
            length = payload[j]
            attr = payload[j + 1]
            val_bytes = payload[j + 2 : j + 1 + length]
            results[attr] = int.from_bytes(val_bytes, "little") if val_bytes else 0
            j += 1 + length
        elif is_settings and payload[j] == 0x00:
            break
        else:
            j += 1

    return results
