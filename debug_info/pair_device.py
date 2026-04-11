#!/usr/bin/env python3
"""
pair_device.py — Pair an OUPES Mega over BLE, replicating the exact Cleanergy app flow.

Reverse-engineered from bugreport23 btsnoop HCI capture of a real pairing session.

The app's pairing cycle (on a single connection):
  1. CCCD enable notifications
  2. 0x01 AUTH (11 packets, key in packet 6)
  3. 0x03 handshake polling (timestamp packets every 300ms for ~5s)
  4. 0x01 timestamp + 0x01 AUTH again
  5. Continue 0x03 handshake polling
  6. 0x03 CLAIM data (10 packets: key + binding token in packets 6-8)
  7. Keepalives on slot 1
  8. If no success, disconnect, reconnect, repeat from step 1

The CLAIM includes the device_key + a 30-char random binding token spanning
packets 6-8.  The Cleanergy app generates this via generateRandomString(30)
using [A-Za-z0-9].  For BLE-only (no cloud), we use a fixed dummy token.

Usage:
  1. Hold IoT button 5 seconds (rapid flash = factory reset)
  2. python pair_device.py 8C:D0:B2:A8:E1:44 --key 4c282b63af
"""

import asyncio
import argparse
import struct
import sys
import time
import hashlib

from bleak import BleakClient, BleakScanner

WRITE_CHAR  = "00002b11-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR = "00002b10-0000-1000-8000-00805f9b34fb"

KEEPALIVE = bytes.fromhex("0180030254010000000000000000000000000076")

# Binding token (30 bytes).  The Cleanergy app generates a random 30-char
# alphanumeric string via generateRandomString(30) for cloud binding.
# For BLE-only (no cloud), the device accepts any 30-byte string.
DUMMY_TOKEN = b"HALOCAL000000000000000000000000"[:30]


def _crc8(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _ts_pkt(prefix: int) -> bytes:
    """Build a timestamp handshake packet: [prefix] 80 00 04 <LE timestamp> zeros crc."""
    now = int(time.time())
    p = bytearray(20)
    p[0] = prefix
    p[1] = 0x80
    p[2] = 0x00
    p[3] = 0x04
    p[4:8] = struct.pack('<I', now)
    p[19] = _crc8(bytes(p[:19]))
    return bytes(p)


def build_auth(key: str) -> list[bytes]:
    """Standard 0x01 AUTH sequence (11 packets)."""
    kb = key.encode("ascii").ljust(10, b"\x00")[:10]
    p6 = bytearray(20)
    p6[0], p6[1] = 0x01, 0x06
    p6[4:14] = kb
    p6[19] = _crc8(bytes(p6[:19]))
    return [
        bytes.fromhex("0100019901010101010101010101010101010192"),
        bytes.fromhex("010101010101010101010101010101010101018f"),
        bytes.fromhex("0102000000000000000000000000000000000082"),
        bytes.fromhex("01030000000000000000000000000000000000a8"),
        bytes.fromhex("010400000000000000000000000000000000007e"),
        bytes.fromhex("0105000000000000000000000000000000000054"),
        bytes(p6),
        bytes.fromhex("0107000000000000000000000000000000000000"),
        bytes.fromhex("0108000000000000000000000000000000000081"),
        bytes.fromhex("01890000000000000000000000000000000000c0"),
        bytes.fromhex("0180020101000000000000000000000000000016"),
    ]


def build_claim(key: str, token: bytes = DUMMY_TOKEN) -> list[bytes]:
    """0x03 CLAIM data sequence (10 packets).
    Packets 6-8 carry the key + binding token as a continuous 40-byte string:
      pkt 6: bytes[2:4]=00 00, bytes[4:14]=key(10), bytes[14:19]=token[0:5]
      pkt 7: bytes[2:19]=token[5:22]  (17 bytes)
      pkt 8: bytes[2:10]=token[22:30], bytes[10:19]=zeros
    """
    kb = key.encode("ascii").ljust(10, b"\x00")[:10]
    tok = token.ljust(30, b"\x00")[:30]
    full = kb + tok  # 40 bytes

    def _raw(cmd: int, data: bytes) -> bytes:
        p = bytearray(20)
        p[0] = 0x03
        p[1] = cmd
        d = data.ljust(17, b"\x00")[:17]
        p[2:2+len(d)] = d
        p[19] = _crc8(bytes(p[:19]))
        return bytes(p)

    return [
        bytes.fromhex("03000199020202020202020202020202020202b8"),  # 0x00
        bytes.fromhex("03010202020202020202020202020202020202b1"),  # 0x01
        _raw(0x02, b""),                                           # 0x02
        _raw(0x03, b""),                                           # 0x03
        _raw(0x04, b""),                                           # 0x04
        _raw(0x05, b""),                                           # 0x05
        _raw(0x06, b"\x00\x00" + full[0:15]),                     # 0x06: key + token start
        _raw(0x07, full[15:32]),                                   # 0x07: token middle
        _raw(0x08, full[32:]),                                     # 0x08: token end
        _raw(0x89, b""),                                           # 0x89: terminator
    ]


async def scan(mac: str, timeout: float = 15.0):
    dev = await BleakScanner.find_device_by_address(mac, timeout=timeout)
    if dev:
        print(f"  Found: {dev.name}  rssi={getattr(dev, 'rssi', '?')}")
    else:
        print(f"  Device not found after {timeout}s")
    return dev


async def pairing_cycle(mac: str, key: str, attempt: int) -> bool:
    """One full pairing cycle matching the app's btsnoop flow.
    Returns True if device transitioned to configured (status=0x00)."""

    print(f"\n{'='*60}")
    print(f"  Pairing cycle {attempt}")
    print(f"{'='*60}")

    dev = await scan(mac)
    if not dev:
        return False

    received: list[bytes] = []
    got_03_ack = False
    got_01_configured = False
    got_telemetry = False
    t0 = time.monotonic()

    def on_notify(_h, data: bytearray):
        nonlocal got_03_ack, got_01_configured, got_telemetry
        pkt = bytes(data)
        received.append(pkt)
        elapsed = time.monotonic() - t0
        t = pkt[1] if len(pkt) >= 2 else 0xFF
        st = pkt[4] if len(pkt) >= 5 else None
        st_str = f" status=0x{st:02x}" if st is not None else ""
        prefix = pkt[0] if pkt else 0

        # Detect key events
        tag = ""
        if len(pkt) >= 5:
            if prefix == 0x03 and pkt[1] == 0x80 and pkt[2] == 0x01 and pkt[4] == 0x00:
                got_03_ack = True
                tag = " <<< 0x03 CLAIM ACCEPTED"
            elif prefix == 0x01 and pkt[1] == 0x80 and pkt[2] == 0x01 and pkt[4] == 0x00:
                got_01_configured = True
                tag = " <<< 0x01 CONFIGURED!"
            elif prefix == 0x01 and pkt[1] == 0x80 and pkt[2] == 0x01 and pkt[4] == 0x03:
                tag = " (unconfigured)"
            elif prefix == 0x01 and t in (0x00, 0x81) and len(pkt) >= 3 and pkt[2] == 0x0a:
                # Telemetry: type=0x00/0x81 with TLV tag 0x0a (measurements)
                # Identity responses start with 0x00/0x10 at byte 2, not 0x0a
                got_telemetry = True
                tag = " <<< TELEMETRY"
            elif prefix == 0x01 and t == 0x00 and len(pkt) >= 3 and pkt[2] == 0x00:
                tag = " (identity)"

        print(f"  << [{len(received):3d}] t={elapsed:5.1f}s [0x{prefix:02x}] type=0x{t:02x}{st_str} : {pkt.hex()[:44]}{tag}")

    try:
        async with BleakClient(dev, timeout=20.0) as client:
            print(f"  Connected")

            await client.start_notify(NOTIFY_CHAR, on_notify)
            await asyncio.sleep(2.0)  # CCCD settle

            # ── Step 1: 0x01 AUTH ──
            auth_seq = build_auth(key)
            print(f"\n  >> 0x01 AUTH ({len(auth_seq)} packets)...")
            for pkt in auth_seq:
                await client.write_gatt_char(WRITE_CHAR, pkt, response=False)
                await asyncio.sleep(0.08)

            # Brief wait for handshake ACK
            await asyncio.sleep(1.0)

            # ── Step 2: 0x03 handshake polling (~5 seconds) ──
            print(f"\n  >> 0x03 handshake polling (5s, every 300ms)...")
            for _ in range(17):  # ~5.1s at 300ms intervals
                ts_pkt = _ts_pkt(0x03)
                await client.write_gatt_char(WRITE_CHAR, ts_pkt, response=False)
                await asyncio.sleep(0.3)
                if got_03_ack or got_01_configured:
                    break

            # ── Step 3: 0x01 timestamp + re-AUTH ──
            print(f"\n  >> 0x01 timestamp + re-AUTH...")
            ts01 = _ts_pkt(0x01)
            await client.write_gatt_char(WRITE_CHAR, ts01, response=False)
            await asyncio.sleep(0.05)
            for pkt in auth_seq:
                await client.write_gatt_char(WRITE_CHAR, pkt, response=False)
                await asyncio.sleep(0.08)

            # ── Step 4: More 0x03 handshake polling ──
            print(f"\n  >> 0x03 handshake polling (another 5s)...")
            for _ in range(17):
                ts_pkt = _ts_pkt(0x03)
                await client.write_gatt_char(WRITE_CHAR, ts_pkt, response=False)
                await asyncio.sleep(0.3)
                if got_03_ack or got_01_configured:
                    break

            # ── Step 5: 0x03 CLAIM data ──
            claim_seq = build_claim(key)
            print(f"\n  >> 0x03 CLAIM data ({len(claim_seq)} packets)...")
            for i, pkt in enumerate(claim_seq):
                print(f"     [{i:2d}] {pkt.hex()}")
                await client.write_gatt_char(WRITE_CHAR, pkt, response=False)
                await asyncio.sleep(0.05)

            # ── Step 6: Keepalive and wait ──
            print(f"\n  >> Keepalive + wait 10s...")
            await client.write_gatt_char(WRITE_CHAR, KEEPALIVE, response=False)
            await asyncio.sleep(5.0)

            if not (got_03_ack or got_01_configured or got_telemetry):
                # Send another keepalive
                await client.write_gatt_char(WRITE_CHAR, KEEPALIVE, response=False)
                await asyncio.sleep(5.0)

            # ── Step 7: Final AUTH check ──
            if not got_01_configured and not got_telemetry:
                print(f"\n  >> Final 0x01 AUTH attempt...")
                ts01 = _ts_pkt(0x01)
                await client.write_gatt_char(WRITE_CHAR, ts01, response=False)
                await asyncio.sleep(0.05)
                for pkt in auth_seq:
                    await client.write_gatt_char(WRITE_CHAR, pkt, response=False)
                    await asyncio.sleep(0.08)
                await asyncio.sleep(5.0)

            try:
                await client.stop_notify(NOTIFY_CHAR)
            except Exception:
                pass

    except Exception as e:
        print(f"  CONNECTION ERROR: {e}")

    # Summary
    handshake = [p for p in received if len(p) >= 2 and p[1] == 0x80]
    telemetry = [p for p in received if len(p) >= 2 and p[1] not in (0x80, 0x82)]
    print(f"\n  Cycle {attempt} result: {len(received)} packets "
          f"({len(handshake)} handshake, {len(telemetry)} telemetry)")
    print(f"  0x03 ACK: {got_03_ack}  |  0x01 configured: {got_01_configured}  |  telemetry: {got_telemetry}")

    if got_01_configured or got_telemetry:
        return True

    # Even got_03_ack without got_01_configured is progress
    if got_03_ack:
        print(f"  0x03 claim accepted but 0x01 not yet configured — reconnecting...")

    return False


async def main(mac: str, key: str, max_cycles: int = 6):
    print("=" * 60)
    print("OUPES Mega — BLE Pairing (btsnoop-matched flow)")
    print("=" * 60)
    print(f"  MAC : {mac}")
    print(f"  Key : {key}")
    print()
    print("Make sure device is in pairing mode (5s button hold).")
    print("This may take 2-3 minutes with multiple reconnects,")
    print("matching the official app's behavior.")
    print()
    input("Press Enter when ready...")

    for cycle in range(1, max_cycles + 1):
        success = await pairing_cycle(mac, key, cycle)
        if success:
            print(f"\n{'='*60}")
            print(f"  SUCCESS! Device paired with key: {key}")
            print(f"{'='*60}")
            print(f"\n  Use in Home Assistant:")
            print(f"    oupes_mega:")
            print(f"      mac: \"{mac}\"")
            print(f"      device_key: \"{key}\"")
            return True

        if cycle < max_cycles:
            wait = 5
            print(f"\n  Waiting {wait}s before next cycle...")
            await asyncio.sleep(wait)

    print(f"\n{'='*60}")
    print(f"  Not confirmed after {max_cycles} cycles.")
    print(f"{'='*60}")
    print(f"  The claim may still have been accepted.")
    print(f"  Try verifying with probe_key.py or the HA integration.")
    print(f"\n  python probe_key.py {mac}")
    return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Pair OUPES Mega over BLE (cloud-free)")
    parser.add_argument("mac", help="Device MAC (e.g. 8C:D0:B2:A8:E1:44)")
    parser.add_argument("--key", required=True,
                        help="10-char hex key to set (e.g. 4c282b63af)")
    parser.add_argument("--cycles", type=int, default=6,
                        help="Max pairing cycles (default: 6)")
    args = parser.parse_args()

    k = args.key.lower()
    if len(k) != 10 or not all(c in "0123456789abcdef" for c in k):
        print(f"ERROR: --key must be 10 hex chars, got: {args.key!r}")
        sys.exit(1)

    result = asyncio.run(main(args.mac, k, args.cycles))
    sys.exit(0 if result else 1)
