"""
parse_btsnoop.py — Parse a btsnoop HCI log and extract ATT packets
                   to/from the TT device (OUPES Mega 1).

Usage:
    python parse_btsnoop.py C:\\Users\\Andrew\\Downloads\\btsnoop.log

Looks for:
  - ATT Write Command / Write Request       (opcode 0x52 / 0x12) → what the app SENDS
  - ATT Handle Value Notification           (opcode 0x1b)         → what the device SENDS
  - L2CAP Connection events (to map conn handles → MAC addresses)
"""

import struct
import sys
from datetime import datetime, timedelta

BTSNOOP_MAGIC = b"btsnoop\x00"

# HCI packet type bytes
HCI_CMD   = 0x01
HCI_ACL   = 0x02
HCI_SCO   = 0x03
HCI_EVENT = 0x04

# ATT opcodes we care about
ATT_WRITE_REQ    = 0x12  # Write Request (expects response)
ATT_WRITE_CMD    = 0x52  # Write Command  (write-without-response)
ATT_HANDLE_NOTIF = 0x1b  # Handle Value Notification
ATT_READ_REQ     = 0x0a
ATT_READ_RESP    = 0x0b
ATT_WRITE_RESP   = 0x13
ATT_FIND_INFO    = 0x04
ATT_EXCHANGE_MTU = 0x02

ATT_OPCODE_NAMES = {
    0x01: "Error Response",
    0x02: "Exchange MTU Req",
    0x03: "Exchange MTU Rsp",
    0x04: "Find Info Req",
    0x05: "Find Info Rsp",
    0x08: "Read By Type Req",
    0x09: "Read By Type Rsp",
    0x0a: "Read Req",
    0x0b: "Read Rsp",
    0x10: "Read By Group Req",
    0x11: "Read By Group Rsp",
    0x12: "Write Req",
    0x13: "Write Rsp",
    0x16: "Prepare Write Req",
    0x17: "Prepare Write Rsp",
    0x18: "Execute Write Req",
    0x19: "Execute Write Rsp",
    0x1b: "Handle Value Notif",
    0x1d: "Handle Value Ind",
    0x1e: "Handle Value Cnf",
    0x52: "Write Cmd (no rsp)",
}

# HCI event codes
HCI_EVT_LE_META          = 0x3E
HCI_LE_CONN_COMPLETE     = 0x01
HCI_LE_ENHANCED_CONN     = 0x0A
HCI_EVT_DISCONN_COMPLETE = 0x05

TARGET_MAC = "8C:D0:B2:A7:EC:AF"


def mac_from_bytes(b: bytes) -> str:
    """Little-endian 6 bytes → colon-separated MAC (uppercase)."""
    return ":".join(f"{x:02X}" for x in reversed(b))


def parse_btsnoop(path: str):
    with open(path, "rb") as f:
        data = f.read()

    if not data.startswith(BTSNOOP_MAGIC):
        print("ERROR: Not a btsnoop file (bad magic)")
        sys.exit(1)

    version  = struct.unpack_from(">I", data, 8)[0]
    datalink = struct.unpack_from(">I", data, 12)[0]
    print(f"btsnoop version={version}  datalink={datalink}")
    # Datalink 1001 = HCI UART (H4), 1002 = HCI UART w/ flow control

    pos = 16  # skip 16-byte file header

    # conn_handle → MAC address map
    handle_to_mac: dict[int, str] = {}
    # conn_handle → connection index (for labelling)
    handle_to_idx: dict[int, int] = {}
    conn_idx = 0

    packets_of_interest: list[dict] = []
    total = 0

    while pos + 24 <= len(data):
        orig_len, incl_len, flags, drops = struct.unpack_from(">IIII", data, pos)
        ts_us = struct.unpack_from(">q", data, pos + 16)[0]
        pos += 24

        if pos + incl_len > len(data):
            break
        pkt = data[pos : pos + incl_len]
        pos += incl_len
        total += 1

        # flags bit 0: 0=sent to controller (TX), 1=received from controller (RX)
        # flags bit 1: 0=data, 1=command/event
        direction = "TX" if (flags & 1) == 0 else "RX"
        is_cmd_evt = bool(flags & 2)

        # Convert timestamp (microseconds since 00:00:00.000 Jan 1, 0000)
        # btsnoop epoch is midnight Jan 1, 0000 — offset from Unix epoch:
        BTSNOOP_EPOCH_OFFSET_US = 0x00dcddb30f2f8000  # microseconds from year 0 to 1970
        unix_us = ts_us - BTSNOOP_EPOCH_OFFSET_US
        try:
            ts = datetime(1970, 1, 1) + timedelta(microseconds=unix_us)
            ts_str = ts.strftime("%H:%M:%S.%f")
        except Exception:
            ts_str = f"ts={ts_us}"

        if not pkt:
            continue

        hci_type = pkt[0]

        # ── HCI Events: track LE connection/disconnection ──────────────────────
        if hci_type == HCI_EVENT and len(pkt) >= 3:
            evt_code = pkt[1]
            # LE Meta event
            if evt_code == HCI_EVT_LE_META and len(pkt) >= 4:
                subevent = pkt[3]
                if subevent == HCI_LE_CONN_COMPLETE and len(pkt) >= 16:
                    handle = struct.unpack_from("<H", pkt, 5)[0]
                    role   = pkt[7]   # 0=Master, 1=Slave
                    mac    = mac_from_bytes(pkt[9:15])
                    conn_idx += 1
                    handle_to_mac[handle] = mac
                    handle_to_idx[handle] = conn_idx
                    print(f"\n[{ts_str}] LE Connection #{conn_idx}  handle=0x{handle:04x}  "
                          f"{'Central' if role == 0 else 'Peripheral'}  peer={mac}")
                elif subevent == HCI_LE_ENHANCED_CONN and len(pkt) >= 26:
                    handle = struct.unpack_from("<H", pkt, 5)[0]
                    role   = pkt[7]
                    mac    = mac_from_bytes(pkt[9:15])
                    conn_idx += 1
                    handle_to_mac[handle] = mac
                    handle_to_idx[handle] = conn_idx
                    print(f"\n[{ts_str}] LE Enh. Connection #{conn_idx}  handle=0x{handle:04x}  "
                          f"{'Central' if role == 0 else 'Peripheral'}  peer={mac}")
            elif evt_code == HCI_EVT_DISCONN_COMPLETE and len(pkt) >= 6:
                handle = struct.unpack_from("<H", pkt, 4)[0]
                reason = pkt[6] if len(pkt) > 6 else 0
                mac    = handle_to_mac.get(handle, "?")
                print(f"[{ts_str}] Disconnected  handle=0x{handle:04x}  peer={mac}  reason=0x{reason:02x}")
                handle_to_mac.pop(handle, None)

        # ── HCI ACL: extract L2CAP/ATT payload ────────────────────────────────
        if hci_type == HCI_ACL and len(pkt) >= 5:
            handle_flags = struct.unpack_from("<H", pkt, 1)[0]
            conn_handle  = handle_flags & 0x0FFF
            acl_len      = struct.unpack_from("<H", pkt, 3)[0]
            if len(pkt) < 5 + acl_len:
                continue
            l2cap = pkt[5 : 5 + acl_len]
            if len(l2cap) < 5:
                continue
            l2cap_len   = struct.unpack_from("<H", l2cap, 0)[0]
            l2cap_cid   = struct.unpack_from("<H", l2cap, 2)[0]
            att_payload = l2cap[4 : 4 + l2cap_len]

            # CID 0x0004 = ATT
            if l2cap_cid != 0x0004 or not att_payload:
                continue

            att_op = att_payload[0]
            mac    = handle_to_mac.get(conn_handle, "?")
            idx    = handle_to_idx.get(conn_handle, 0)
            op_name = ATT_OPCODE_NAMES.get(att_op, f"0x{att_op:02x}")

            if att_op in (ATT_WRITE_REQ, ATT_WRITE_CMD) and len(att_payload) >= 3:
                att_handle = struct.unpack_from("<H", att_payload, 1)[0]
                value      = att_payload[3:]
                entry = {
                    "ts": ts_str, "dir": direction, "mac": mac,
                    "conn": idx, "op": op_name, "handle": att_handle,
                    "value": value,
                }
                packets_of_interest.append(entry)
                marker = " *** TARGET ***" if mac == TARGET_MAC else ""
                print(f"[{ts_str}] {direction} conn#{idx} ({mac}){marker}  "
                      f"{op_name}  handle=0x{att_handle:04x}  value={value.hex()}")

            elif att_op == ATT_HANDLE_NOTIF and len(att_payload) >= 3:
                att_handle = struct.unpack_from("<H", att_payload, 1)[0]
                value      = att_payload[3:]
                entry = {
                    "ts": ts_str, "dir": direction, "mac": mac,
                    "conn": idx, "op": op_name, "handle": att_handle,
                    "value": value,
                }
                packets_of_interest.append(entry)
                marker = " *** TARGET ***" if mac == TARGET_MAC else ""
                print(f"[{ts_str}] {direction} conn#{idx} ({mac}){marker}  "
                      f"Notification  handle=0x{att_handle:04x}  value={value.hex()}")

    print(f"\n\nTotal HCI records: {total}")
    print(f"ATT write/notify packets: {len(packets_of_interest)}")

    # Summary of writes to the TT device
    tt_writes = [p for p in packets_of_interest if p["mac"] == TARGET_MAC and "Write" in p["op"]]
    tt_notifs = [p for p in packets_of_interest if p["mac"] == TARGET_MAC and "Notif" in p["op"]]
    print(f"\nWrites TO TT ({TARGET_MAC}): {len(tt_writes)}")
    for p in tt_writes:
        print(f"  [{p['ts']}] handle=0x{p['handle']:04x}  {p['value'].hex()}")
    print(f"\nNotifications FROM TT: {len(tt_notifs)}")
    for p in tt_notifs[:20]:  # first 20
        print(f"  [{p['ts']}] handle=0x{p['handle']:04x}  {p['value'].hex()}")
    if len(tt_notifs) > 20:
        print(f"  ... ({len(tt_notifs) - 20} more)")


def dump_connections(path: str):
    """Just list every LE connection event in the log."""
    with open(path, "rb") as f:
        data = f.read()
    pos = 16
    seen = []
    while pos + 24 <= len(data):
        orig_len, incl_len, flags, drops = struct.unpack_from(">IIII", data, pos)
        ts_us = struct.unpack_from(">q", data, pos + 16)[0]
        pos += 24
        if pos + incl_len > len(data):
            break
        pkt = data[pos : pos + incl_len]
        pos += incl_len
        if not pkt or pkt[0] != HCI_EVENT:
            continue
        if len(pkt) < 4:
            continue
        evt_code = pkt[1]
        if evt_code == HCI_EVT_LE_META:
            subevent = pkt[3]
            if subevent in (HCI_LE_CONN_COMPLETE, HCI_LE_ENHANCED_CONN) and len(pkt) >= 15:
                handle = struct.unpack_from("<H", pkt, 5)[0]
                mac    = mac_from_bytes(pkt[9:15])
                BTSNOOP_EPOCH_OFFSET_US = 0x00dcddb30f2f8000
                unix_us = ts_us - BTSNOOP_EPOCH_OFFSET_US
                try:
                    ts = datetime(1970, 1, 1) + timedelta(microseconds=unix_us)
                    ts_str = ts.strftime("%Y-%m-%d %H:%M:%S.%f")
                except Exception:
                    ts_str = str(ts_us)
                seen.append((ts_str, handle, mac))
                print(f"  [{ts_str}]  handle=0x{handle:04x}  MAC={mac}")
        elif evt_code == HCI_EVT_DISCONN_COMPLETE and len(pkt) >= 6:
            handle = struct.unpack_from("<H", pkt, 4)[0]
            reason = pkt[6] if len(pkt) > 6 else 0
            BTSNOOP_EPOCH_OFFSET_US = 0x00dcddb30f2f8000
            unix_us = ts_us - BTSNOOP_EPOCH_OFFSET_US
            try:
                ts = datetime(1970, 1, 1) + timedelta(microseconds=unix_us)
                ts_str = ts.strftime("%H:%M:%S.%f")
            except Exception:
                ts_str = str(ts_us)
            print(f"  [{ts_str}]  DISCONNECT handle=0x{handle:04x}  reason=0x{reason:02x}")
    print(f"Total LE connections seen: {len(seen)}")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\Andrew\Downloads\btsnoop.log"
    print("=== All LE Connection Events ===")
    dump_connections(path)
    print("\n=== ATT packets for target device ===")
    parse_btsnoop(path)
