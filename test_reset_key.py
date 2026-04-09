#!/usr/bin/env python3
"""
test_reset_key.py — Test BLE pairing/claiming on an OUPES Mega.

Reverse-engineered from btsnoop HCI captures: the Cleanergy app sends two
distinct init sequences:
  - 0x01 sequence: AUTHENTICATE (regular HA/app connection, key must match)
  - 0x03 sequence: CLAIM/PAIR   (programs a new key into the device)

Modes
-----
  auth        Send the 0x01 auth sequence with KEY.  Safe — no changes to
              device.  Use to verify a known key works.

  claim-test  Send the 0x03 claim sequence with the CURRENT known key.
              Safe — just tests whether the device accepts the 0x03 opcode
              and responds.  Does NOT change the key.

  claim       Send the 0x03 claim sequence with a NEW random key.
              ⚠️  This will re-key the device.  HA and the Cleanergy app will
              stop working until re-paired.  Only use this if you want to
              take full ownership from HA without the cloud.

Usage
-----
    pip install bleak
    python test_reset_key.py <MAC> [--mode auth|claim-test|claim] [--key KEY]

    --mode   default: auth
    --key    10-char ASCII key (default: bd236b1695 for auth/claim-test,
             random for claim)

Examples
--------
    # Safe test: auth with known key
    python test_reset_key.py 8C:D0:B2:A8:E1:44

    # Safe test: verify 0x03 opcode accepted
    python test_reset_key.py 8C:D0:B2:A8:E1:44 --mode claim-test

    # Re-key the device (prints the new key to configure in HA)
    python test_reset_key.py 8C:D0:B2:A8:E1:44 --mode claim
"""

import asyncio
import sys
import random
import string
import argparse

from bleak import BleakClient, BleakScanner

WRITE_CHAR  = "00002b11-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR = "00002b10-0000-1000-8000-00805f9b34fb"

KNOWN_KEY = "bd236b1695"


def _crc8(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _pkt(first_byte: int, cmd: int, payload: bytes) -> bytes:
    """Build a 20-byte packet: [first_byte][cmd][0x00][0x00][payload 15 bytes padded][crc]."""
    p = bytearray(20)
    p[0] = first_byte
    p[1] = cmd
    p[2] = 0x00
    p[3] = 0x00
    payload = payload[:15]
    p[4:4 + len(payload)] = payload
    p[19] = _crc8(bytes(p[:19]))
    return bytes(p)


# ── 0x01 AUTH sequence (regular connection) ──────────────────────────────────
AUTH_SEQUENCE_BASE = [
    bytes.fromhex("0100019901010101010101010101010101010192"),
    bytes.fromhex("010101010101010101010101010101010101018f"),
    bytes.fromhex("0102000000000000000000000000000000000082"),
    bytes.fromhex("01030000000000000000000000000000000000a8"),
    bytes.fromhex("010400000000000000000000000000000000007e"),
    bytes.fromhex("0105000000000000000000000000000000000054"),
    None,   # slot 6: key packet — built dynamically
    bytes.fromhex("0107000000000000000000000000000000000000"),
    bytes.fromhex("0108000000000000000000000000000000000081"),
    bytes.fromhex("01890000000000000000000000000000000000c0"),
    bytes.fromhex("0180020101000000000000000000000000000016"),
]


def build_auth_sequence(key: str) -> list[bytes]:
    seq = list(AUTH_SEQUENCE_BASE)
    key_bytes = key.encode("ascii").ljust(10, b"\x00")[:10]
    seq[6] = _pkt(0x01, 0x06, key_bytes + b"\x00\x00\x00\x00\x00")
    return seq


# ── 0x03 CLAIM/PAIR sequence (programs key into device) ──────────────────────
# Observed in btsnoop during Cleanergy app pairing.
# Pkt 07 and 08 carried WiFi credentials in the app capture (DqjxJCRBelB2cf10I[
# and Lo43Lyvk). For BLE-only HA use we send zeros — the device doesn't need WiFi.
# The 5 trailing bytes in pkt 06 ("siimP" in the capture) are sent as zeros here;
# if the device rejects that, set them to b"siimP".

def build_claim_sequence(key: str) -> list[bytes]:
    key_bytes = key.encode("ascii").ljust(10, b"\x00")[:10]
    return [
        bytes.fromhex("0300019902020202020202020202020202020202b8"),  # seq 0x00 handshake
        bytes.fromhex("0301020202020202020202020202020202020202b1"),  # seq 0x01 handshake
        _pkt(0x03, 0x02, b""),                          # seq 0x02
        _pkt(0x03, 0x03, b""),                          # seq 0x03
        _pkt(0x03, 0x04, b""),                          # seq 0x04
        _pkt(0x03, 0x05, b""),                          # seq 0x05
        _pkt(0x03, 0x06, key_bytes + b"\x00\x00\x00\x00\x00"),  # seq 0x06: key packet
        _pkt(0x03, 0x07, b""),                          # seq 0x07: WiFi PSK (empty)
        _pkt(0x03, 0x08, b""),                          # seq 0x08: WiFi SSID (empty)
        _pkt(0x03, 0x89, b""),                          # seq 0x89: terminator
    ]


def random_key(length: int = 10) -> str:
    chars = string.ascii_lowercase + string.digits
    return "".join(random.choices(chars, k=length))


async def run_test(mac: str, mode: str, key: str) -> None:
    print(f"Target MAC : {mac}")
    print(f"Mode       : {mode}")
    print(f"Key        : {key}")
    print()

    if mode == "claim":
        print("⚠️  CLAIM mode: this will re-key the device with the key above.")
        print("   HA and the Cleanergy app will need to be updated to use this key.")
        print()

    if mode == "auth":
        sequence = build_auth_sequence(key)
        label = "0x01 AUTH"
    else:
        sequence = build_claim_sequence(key)
        label = "0x03 CLAIM"

    packets_received = []

    def notification_handler(handle, data: bytearray):
        packets_received.append(bytes(data))
        type_byte = data[1] if len(data) >= 2 else 0xFF
        print(f"  << NOTIFY [{len(packets_received):3d}]: {data.hex()}  type=0x{type_byte:02x}")

    print("Scanning for device...")
    device = await BleakScanner.find_device_by_address(mac, timeout=15.0)
    if device is None:
        print(f"ERROR: Device {mac} not found. Is it on and in BLE range?")
        return

    print(f"Found: {device.name}  rssi={getattr(device, 'rssi', '?')}")
    print("Connecting...")

    async with BleakClient(device, timeout=20.0) as client:
        print("Connected.")
        await client.start_notify(NOTIFY_CHAR, notification_handler)
        await asyncio.sleep(1.8)

        print(f"Sending {label} sequence ({len(sequence)} packets)...")
        for i, pkt in enumerate(sequence):
            print(f"  >> [{i:2d}] {pkt.hex()}")
            await client.write_gatt_char(WRITE_CHAR, pkt, response=False)
            await asyncio.sleep(0.05)

        print(f"\nWaiting for response (15s)...")
        await asyncio.sleep(15.0)
        await client.stop_notify(NOTIFY_CHAR)

    print(f"\n{'=' * 50}")
    print(f"Packets received: {len(packets_received)}")

    if not packets_received:
        print("RESULT: No response — device rejected this sequence/key.")
        if mode == "claim-test":
            print("  → 0x03 opcode NOT accepted. Device requires the correct key to respond.")
        elif mode == "auth":
            print("  → Key is wrong or device is busy (HA integration polling?).")
        return

    telemetry = [p for p in packets_received if len(p) >= 2 and p[1] not in (0x80, 0x82)]
    print(f"Telemetry packets (non-handshake): {len(telemetry)}")

    if mode == "claim-test":
        if telemetry:
            print("\nRESULT: ✅ 0x03 opcode ACCEPTED — device responded with telemetry!")
            print("  → The device accepts the claim sequence.")
            print("  → You can now run with --mode claim to re-key with a HA-generated key.")
        else:
            print("\nRESULT: ⚠️  Got handshake-only packets — unclear. Check telemetry above.")

    elif mode == "claim":
        if telemetry:
            print(f"\nRESULT: ✅ CLAIM SUCCEEDED — device accepted new key: {key}")
            print(f"  → Update your HA integration config entry to use key: {key}")
        else:
            print("\nRESULT: ⚠️  Claim sent but no telemetry returned.")
            print("  → Try running auth mode with the new key to confirm.")

    elif mode == "auth":
        if telemetry:
            print(f"\nRESULT: ✅ Auth OK — key {key!r} works.")
        else:
            print("\nRESULT: ❌ Auth returned only handshake packets, no telemetry.")

    for p in telemetry[:5]:
        print(f"  {p.hex()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OUPES Mega BLE pairing/auth tester")
    parser.add_argument("mac", help="Device MAC address e.g. 8C:D0:B2:A8:E1:44")
    parser.add_argument("--mode", choices=["auth", "claim-test", "claim"], default="auth")
    parser.add_argument("--key", default=None, help="10-char ASCII key")
    args = parser.parse_args()

    if args.key:
        key = args.key
    elif args.mode == "claim":
        key = random_key()
        print(f"Generated new random key: {key}")
    else:
        key = KNOWN_KEY

    asyncio.run(run_test(args.mac, args.mode, key))
