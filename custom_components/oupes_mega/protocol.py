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
    105: ("AC Inverter Protection",    "bool"),  # 1 = inverter protection/thermal warning active:
                                                   #   goes 1 ~60s after AC output is hardware-tripped (overcurrent/thermal)
                                                   #   while 1 with AC off: device is in 8-10 min thermal recovery, fans may struggle
                                                   #   also goes 1 during elevated-temp normal operation (thermal warning, no hard trip)
                                                   # 0 = normal operating state; attr 32 temp rises 949→970 during protection events
}

# Attrs that arrive grouped by slot; attr 101 carries the slot index (1 or 2).
# On the OUPES Mega 1 these reflect the device's two INTERNAL battery modules,
# NOT external B2 expansion batteries.  Attrs 78+101 are in one packet type;
# attrs 79+80 are in a separate packet type (never share a packet with 78/101):
#   78 + 101: per-module remaining runtime (0→5940; 5940 = charging/idle max)
#   79 + 80:  BMS cell-group scan index (0–14) + battery temperature in 0.1 °F
EXT_BATTERY_ATTRS: set[int] = {78, 79, 80}
EXT_BATTERY_MAP: dict[int, tuple[str, str]] = {
    78: ("Remaining Runtime",  "min"),   # per-module; 5940 = charging/idle max
    79: ("Cell Group Index",   "raw"),   # BMS scan index 0-14 (NOT battery %)
    80: ("Temperature",        "F/10"),  # battery module temperature ×0.1 °F (e.g. 878 → 87.8 °F) — confirmed vs app display
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


# ── Packet parser ─────────────────────────────────────────────────────────────

def parse_ble_packet(data: bytearray) -> dict[int, int]:
    """Parse a BLE notification packet into {attr: raw_value}.

    Formats observed in HCI capture:
      Type 0x00 / 0x01  — standard TLV stream (tag 0x0a, length, attr, value…)
      Type 0x81         — secondary data; uses same TLV but some firmware
                          versions omit the 0x0a tag and send [length][attr][val]
                          directly (compact form, length 1-4).
      Type 0x80         — handshake-only, not a TLV payload; skip.
      Type 0x82         — end-of-group marker; skip.
    """
    results: dict[int, int] = {}
    if len(data) < 3:
        return results

    pkt_type = data[1]

    if pkt_type in (0x82, 0x80):
        return results  # no TLV payload

    i = 2
    while i < len(data) - 1:  # last byte is checksum
        if data[i] == 0x0A and i + 2 < len(data):
            # Standard form: [0x0a][length][attr][val…]
            length = data[i + 1]
            if length >= 1 and i + 2 + length <= len(data) - 1:
                attr = data[i + 2]
                val_bytes = data[i + 3: i + 2 + length]
                results[attr] = int.from_bytes(val_bytes, "little") if val_bytes else 0
            i += 2 + length
        elif pkt_type == 0x81 and 1 <= data[i] <= 4 and i + 1 + data[i] <= len(data) - 1:
            # Compact form (0x81 only): [length][attr][val…]  — firmware omits 0x0a tag.
            # Seen for ext-battery % (attr 79) when value fits in one byte.
            length = data[i]
            attr = data[i + 1]
            val_bytes = data[i + 2: i + 1 + length]
            results[attr] = int.from_bytes(val_bytes, "little") if val_bytes else 0
            i += 1 + length
        else:
            i += 1

    return results
