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
    5:   ("AC Input Power",          "W"),
    6:   ("DC Car Charger Input",    "W"),
    7:   ("Solar Input",             "W"),
    8:   ("Unknown Input",           "raw"),
    9:   ("Unknown",                 "raw"),
    21:  ("Total Input Power",       "W"),
    22:  ("Grid Input Power",        "W"),
    23:  ("AC Input Connected",      "bool"),
    30:  ("Remaining Runtime",       "min"),
    32:  ("Battery Pack Voltage",    "V/10"),
    51:  ("Charge Mode",             "chargemode"),
    53:  ("Unknown (attr 53)",       "raw"),
    54:  ("Unknown (attr 54)",       "raw"),
    84:  ("AC Output Control",       "bool"),
    105: ("Unknown Flag (attr 105)", "raw"),
}

# Attrs that arrive grouped by slot; attr 101 carries the slot index (1 or 2).
EXT_BATTERY_ATTRS: set[int] = {78, 79, 80}
EXT_BATTERY_MAP: dict[int, tuple[str, str]] = {
    78: ("Remaining Runtime", "min"),
    79: ("Battery",           "pct"),
    80: ("Temperature",       "F/10"),
}

CHARGE_MODES = {2: "AC Charging"}

# Convenience set of attrs that should become binary sensors
BOOL_ATTRS = {attr for attr, (_, unit) in ATTR_MAP.items() if unit == "bool"}


# ── Packet parser ─────────────────────────────────────────────────────────────

def parse_ble_packet(data: bytearray) -> dict[int, int]:
    """Parse a BLE notification packet into {attr: raw_value}.

    Two formats observed in HCI capture:
      Type 0x00 / 0x01  — standard TLV stream
      Type 0x80 / 0x81  — handshake / secondary packets; try TLV from byte 2
      Type 0x82          — end-of-group marker; skip
    """
    results: dict[int, int] = {}
    if len(data) < 3:
        return results

    pkt_type = data[1]

    if pkt_type == 0x82:
        return results  # end-of-group marker, body is all zeros

    # For 0x80/0x81, try TLV starting at byte 2
    start = 2 if pkt_type in (0x80, 0x81) else 2

    i = start
    while i < len(data) - 1:  # last byte is checksum
        if data[i] == 0x0A and i + 2 < len(data):
            length = data[i + 1]
            if length >= 1 and i + 2 + length <= len(data) - 1:
                attr = data[i + 2]
                val_bytes = data[i + 3: i + 2 + length]
                results[attr] = int.from_bytes(val_bytes, "little") if val_bytes else 0
            i += 2 + length
        else:
            i += 1

    return results
