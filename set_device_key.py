#!/usr/bin/env python3
"""
set_device_key.py — Claim an OUPES Mega over BLE with a chosen key.

BEFORE RUNNING:
  1. Hold the Bluetooth/WiFi button on the device until it enters pairing mode.
  2. Run this script — it will connect, send the 0x03 claim sequence, then
     reconnect and verify with a 0x01 auth that telemetry flows.

Usage
-----
    pip install bleak
    python set_device_key.py <MAC> [--key KEY] [--current-key CURRENT_KEY]

    --key          10-char ASCII key to program. Defaults to a random key.
                   The chosen key is printed at the start — use it in HA.
    --current-key  Current key stored on the device. When provided, the script
                   authenticates with this key first (no button hold needed).
                   Omit if the device is already in pairing mode (button held).

Examples
--------
    # Pairing mode path — hold button until light changes, THEN run:
    python set_device_key.py 8C:D0:B2:A8:E1:44

    # Current-key path — no button hold needed if you know the stored key:
    python set_device_key.py 8C:D0:B2:A8:E1:44 --current-key cfcd208495

    # Set a specific new key:
    python set_device_key.py 8C:D0:B2:A8:E1:44 --current-key cfcd208495 --key 4c282b63af
"""

import asyncio
import argparse
import random
import hashlib
import sys

from bleak import BleakClient, BleakScanner

WRITE_CHAR  = "00002b11-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR = "00002b10-0000-1000-8000-00805f9b34fb"


def _crc8(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _pkt(prefix: int, cmd: int, payload: bytes) -> bytes:
    """Build a 20-byte packet: [prefix][cmd][0x00][0x00][payload 15 bytes padded][crc8]."""
    p = bytearray(20)
    p[0] = prefix
    p[1] = cmd
    p[2] = 0x00
    p[3] = 0x00
    payload = payload[:15]
    p[4:4 + len(payload)] = payload
    p[19] = _crc8(bytes(p[:19]))
    return bytes(p)


def factory_reset_packet() -> bytes:
    """Software factory reset: getBleToDeviceRestoreFactoryCmd4() = '040101'
    Built by reqCmdToPkg('040101', dataLen=34, version='01') — single packet.
    Format: deviceProtocol(01) + pkgSn(80) + data_chunk(040101 + 14 zeros) + crc8.
    Data goes at byte[2] directly (not [4] like _pkt helper).
    """
    p = bytearray(20)
    p[0] = 0x01  # deviceProtocol "01"
    p[1] = 0x80  # pkgSn = 0x80|0 (single and last packet)
    p[2] = 0x04  # factory reset command data
    p[3] = 0x01
    p[4] = 0x01
    # bytes 5-18 remain 0x00
    p[19] = _crc8(bytes(p[:19]))
    return bytes(p)


def build_claim_sequence(key: str) -> list[bytes]:
    """0x03 CLAIM — programs the key into the device.
    10-packet sequence matching the real app (seq 0x00..0x08, 0x89).
    Packets 0x00 and 0x01 are fixed handshake bytes (mirror of auth but with 0x03 prefix/0x02 fill).
    """
    key_bytes = key.encode("ascii").ljust(10, b"\x00")[:10]
    return [
        bytes.fromhex("0300019902020202020202020202020202020202b8"),  # seq 0x00 handshake
        bytes.fromhex("0301020202020202020202020202020202020202b1"),  # seq 0x01 handshake
        _pkt(0x03, 0x02, b""),
        _pkt(0x03, 0x03, b""),
        _pkt(0x03, 0x04, b""),
        _pkt(0x03, 0x05, b""),
        _pkt(0x03, 0x06, key_bytes + b"\x00\x00\x00\x00\x00"),  # key packet
        _pkt(0x03, 0x07, b""),  # WiFi PSK  (empty — BLE-only)
        _pkt(0x03, 0x08, b""),  # WiFi SSID (empty — BLE-only)
        _pkt(0x03, 0x89, b""),  # terminator
    ]


def build_auth_sequence(key: str) -> list[bytes]:
    """0x01 AUTH — regular authenticated connection. Used to verify the key after claiming."""
    key_bytes = key.encode("ascii").ljust(10, b"\x00")[:10]
    return [
        bytes.fromhex("0100019901010101010101010101010101010192"),
        bytes.fromhex("010101010101010101010101010101010101018f"),
        bytes.fromhex("0102000000000000000000000000000000000082"),
        bytes.fromhex("01030000000000000000000000000000000000a8"),
        bytes.fromhex("010400000000000000000000000000000000007e"),
        bytes.fromhex("0105000000000000000000000000000000000054"),
        _pkt(0x01, 0x06, key_bytes + b"\x00\x00\x00\x00\x00"),  # key packet
        bytes.fromhex("0107000000000000000000000000000000000000"),
        bytes.fromhex("0108000000000000000000000000000000000081"),
        bytes.fromhex("01890000000000000000000000000000000000c0"),
        bytes.fromhex("0180020101000000000000000000000000000016"),
    ]


def random_key(length: int = 10) -> str:
    return "".join(random.choices("0123456789abcdef", k=length))


async def find_device(mac: str, label: str) -> object:
    print(f"Scanning for {mac} ({label})...")
    device = await BleakScanner.find_device_by_address(mac, timeout=20.0)
    if device is None:
        print(f"ERROR: Device {mac} not found. Is it powered on and in BLE range?")
        sys.exit(1)
    print(f"  Found: {device.name}  rssi={getattr(device, 'rssi', '?')}")
    return device


async def claim_and_verify(mac: str, key: str, current_key: str | None = None) -> None:
    # If current_key is provided: auth → software factory reset → reconnect → claim → auth verify.
    # If not provided: device must be in pairing mode (button held, no stored key).
    # Either way, claim and final auth happen in ONE connection, matching the app's behaviour.
    print("\n" + "=" * 55)
    if current_key:
        print("Mode: AUTH → FACTORY RESET → CLAIM (no button hold needed)")
    else:
        print("Mode: PAIRING MODE (device must have been factory-reset via button)")
    print("=" * 55)

    if current_key:
        # ── Phase A: auth + software factory reset ────────────────────────
        print("\nPHASE A: Authenticate and send software factory reset")
        device = await find_device(mac, "auth + reset")
        phase_a: list[bytes] = []

        async with BleakClient(device, timeout=30.0) as client:
            print("Connected.")

            def on_notify_a(_handle, data: bytearray):
                phase_a.append(bytes(data))
                b1 = data[1] if len(data) >= 2 else 0xFF
                print(f"  << NOTIFY [A{len(phase_a):03d}]: {data.hex()}  (type=0x{b1:02x})")

            await client.start_notify(NOTIFY_CHAR, on_notify_a)
            await asyncio.sleep(2.0)

            auth_cur = build_auth_sequence(current_key)
            print(f"\nA1: AUTH with current key {current_key!r} ({len(auth_cur)} packets)...")
            for i, pkt in enumerate(auth_cur):
                print(f"  >> [{i:2d}] {pkt.hex()}")
                await client.write_gatt_char(WRITE_CHAR, pkt, response=False)
                await asyncio.sleep(0.1)
            print("Waiting 4s for auth to settle...")
            await asyncio.sleep(4.0)

            if not phase_a:
                print("\nWARNING: No auth response. Is the current key correct?")
                print("  Sending factory reset anyway...")
            else:
                print(f"  Auth got {len(phase_a)} response(s) ✓")

            rst = factory_reset_packet()
            print(f"\nA2: Sending factory reset packet: {rst.hex()}")
            await client.write_gatt_char(WRITE_CHAR, rst, response=False)
            print("Waiting 5s for device to reset (may disconnect)...")
            await asyncio.sleep(5.0)

            try:
                await client.stop_notify(NOTIFY_CHAR)
            except Exception:
                pass  # device may have disconnected

        print("\nFactory reset sent. Waiting 3s before reconnecting...")
        await asyncio.sleep(3.0)

    # ── Phase B: claim + auth verify (single connection) ─────────────────
    print("\nPHASE B: Connect, claim new key, verify with auth")
    device2 = await find_device(mac, "claim + auth")
    received_all: list[bytes] = []
    n_after_claim = 0

    async with BleakClient(device2, timeout=30.0) as client2:
        print("Connected.")

        def on_notify(_handle, data: bytearray):
            received_all.append(bytes(data))
            b1 = data[1] if len(data) >= 2 else 0xFF
            phase = "CLAIM" if len(received_all) <= 10 else "AUTH "
            print(f"  << NOTIFY [{len(received_all):3d}] {phase}: {data.hex()}  (type=0x{b1:02x})")

        await client2.start_notify(NOTIFY_CHAR, on_notify)
        await asyncio.sleep(2.0)

        claim_seq = build_claim_sequence(key)
        print(f"\nB1: 0x03 CLAIM with new key {key!r} ({len(claim_seq)} packets)...")
        for i, pkt in enumerate(claim_seq):
            print(f"  >> [{i:2d}] {pkt.hex()}")
            await client2.write_gatt_char(WRITE_CHAR, pkt, response=False)
            await asyncio.sleep(0.1)
        print("Waiting 5s for claim to settle...")
        await asyncio.sleep(5.0)
        n_after_claim = len(received_all)

        auth_new = build_auth_sequence(key)
        print(f"\nB2: 0x01 AUTH with new key {key!r} ({len(auth_new)} packets)...")
        for i, pkt in enumerate(auth_new):
            print(f"  >> [{i:2d}] {pkt.hex()}")
            await client2.write_gatt_char(WRITE_CHAR, pkt, response=False)
            await asyncio.sleep(0.1)
        print("Waiting 15s for telemetry...")
        await asyncio.sleep(15.0)
        try:
            await client2.stop_notify(NOTIFY_CHAR)
        except Exception:
            pass

    claim_pkts = received_all[:n_after_claim]
    new_pkts   = received_all[n_after_claim:]
    telemetry  = [p for p in new_pkts if len(p) >= 2 and p[1] not in (0x80, 0x82)]

    print("\n" + "=" * 55)
    print("FINAL RESULT")
    print("=" * 55)
    print(f"Claim phase responses : {len(claim_pkts)}")
    print(f"Auth phase responses  : {len(new_pkts)}  ({len(telemetry)} telemetry)")

    if telemetry:
        print(f"\n✅  SUCCESS — device accepted new key and is streaming telemetry!")
        print(f"\n    Key to use in Home Assistant: {key!r}")
        print(f"      device_key: \"{key}\"")
        print(f"\nSample telemetry packets:")
        for p in telemetry[:5]:
            print(f"  {p.hex()}")
    elif new_pkts:
        print(f"\n⚠️  Auth returned {len(new_pkts)} packet(s) but no telemetry.")
        print(f"   Packets: {[p.hex() for p in new_pkts]}")
        print(f"   Try: python test_reset_key.py {mac} --mode auth --key {key}")
    elif claim_pkts:
        print(f"\n⚠️  Claim got a response but final auth got nothing.")
        print(f"   Try running again — device may need more time after reset.")
    else:
        print("❌  No response at all.")
        if current_key:
            print("   The software factory reset may not have worked.")
            print("   → Try holding the physical button to factory-reset, then re-run without --current-key.")
        else:
            print("   → Hold the button until the light changes, then re-run.")
            print(f"   OR: python set_device_key.py {mac} --current-key cfcd208495 --key {key}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OUPES Mega BLE key setter")
    parser.add_argument("mac", help="Device MAC address e.g. 8C:D0:B2:A8:E1:44")
    parser.add_argument("--key", default=None, help="10-char ASCII key to set (default: random)")
    parser.add_argument("--current-key", default=None, metavar="CURRENT_KEY",
                        help="Stored key on device — skips button-hold requirement")
    args = parser.parse_args()

    if args.key:
        if len(args.key) != 10 or not all(c in "0123456789abcdefABCDEF" for c in args.key):
            print(f"ERROR: Key must be exactly 10 hex characters (0-9, a-f), got: {args.key!r}")
            sys.exit(1)
        key = args.key.lower()
    else:
        key = random_key()

    current_key = args.current_key

    print("=" * 55)
    print("OUPES Mega BLE Key Setter")
    print("=" * 55)
    print(f"  MAC         : {args.mac}")
    print(f"  New key     : {key}  ← note this down for HA!")
    if current_key:
        print(f"  Current key : {current_key}  (will auth with this first)")
        print()
        print("No button hold needed — authenticating with current key.")
    else:
        print()
        print("Make sure you have HELD the Bluetooth/WiFi button to put")
        print("the device into pairing mode before continuing.")
        print("(Or re-run with --current-key cfcd208495 to skip this.)")
    print()
    input("Press Enter when ready...")

    asyncio.run(claim_and_verify(args.mac, key, current_key))
