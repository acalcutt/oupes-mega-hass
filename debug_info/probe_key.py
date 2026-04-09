#!/usr/bin/env python3
"""
probe_key.py — Try candidate keys against an OUPES Mega to find the stored one.

Tries each candidate key with the 0x01 AUTH sequence and reports which one
gets a notification response from the device.

Usage:
    python probe_key.py <MAC> --uid 12345 --uid 67890
    python probe_key.py <MAC> --key bd236b1695 --key cfcd208495
"""
import asyncio, sys, hashlib, argparse
from bleak import BleakClient, BleakScanner

WRITE_CHAR  = "00002b11-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR = "00002b10-0000-1000-8000-00805f9b34fb"

def md5key(uid): return hashlib.md5(str(uid).encode()).hexdigest()[:10]

def _crc8(data):
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc

def _pkt(prefix, cmd, payload=b""):
    p = bytearray(20)
    p[0], p[1], p[2], p[3] = prefix, cmd, 0, 0
    p[4:4+len(payload[:15])] = payload[:15]
    p[19] = _crc8(bytes(p[:19]))
    return bytes(p)

def build_auth(key):
    kb = key.encode().ljust(10, b"\x00")[:10]
    return [
        bytes.fromhex("0100019901010101010101010101010101010192"),
        bytes.fromhex("010101010101010101010101010101010101018f"),
        bytes.fromhex("0102000000000000000000000000000000000082"),
        bytes.fromhex("01030000000000000000000000000000000000a8"),
        bytes.fromhex("010400000000000000000000000000000000007e"),
        bytes.fromhex("0105000000000000000000000000000000000054"),
        _pkt(0x01, 0x06, kb + b"\x00\x00\x00\x00\x00"),
        bytes.fromhex("0107000000000000000000000000000000000000"),
        bytes.fromhex("0108000000000000000000000000000000000081"),
        bytes.fromhex("01890000000000000000000000000000000000c0"),
        bytes.fromhex("0180020101000000000000000000000000000016"),
    ]

async def probe_key(key, label):
    print(f"\n  Trying key {key!r}  ({label})")
    device = await BleakScanner.find_device_by_address(MAC, timeout=10.0)
    if not device:
        print("  ERROR: device not found")
        return False, 0

    received = []
    async with BleakClient(device, timeout=15.0) as client:
        def on_notify(_, data):
            received.append(bytes(data))

        try:
            await client.start_notify(NOTIFY_CHAR, on_notify)
        except Exception as e:
            print(f"  start_notify failed: {e}")
            return False, 0

        await asyncio.sleep(1.5)

        for pkt in build_auth(key):
            await client.write_gatt_char(WRITE_CHAR, pkt, response=False)
            await asyncio.sleep(0.08)

        await asyncio.sleep(6.0)

        try:
            await client.stop_notify(NOTIFY_CHAR)
        except Exception:
            pass

    telemetry = [p for p in received if len(p) >= 2 and p[1] not in (0x80, 0x82)]
    print(f"  → Got {len(received)} packet(s), {len(telemetry)} telemetry")
    if received:
        for p in received[:3]:
            print(f"     {p.hex()}")
    return len(received) > 0, len(telemetry)

async def main():
    parser = argparse.ArgumentParser(description="Try candidate keys against an OUPES Mega.")
    parser.add_argument("mac", help="Bluetooth MAC address (e.g., 8C:D0:B2:A8:E1:44)")
    parser.add_argument("--uid", action="append", type=int, default=[],
                        help="Cleanergy user ID to derive a key from (repeatable)")
    parser.add_argument("--key", action="append", default=[],
                        help="Raw 10-hex-char key to try (repeatable)")
    args = parser.parse_args()

    global MAC
    MAC = args.mac

    candidates = []
    # Always try uid=0 (app fallback key)
    candidates.append(("uid=0 (app fallback)", md5key(0)))
    for uid in args.uid:
        candidates.append((f"uid={uid}", md5key(uid)))
    for key in args.key:
        key = key.strip().lower()
        if len(key) != 10 or not all(c in "0123456789abcdef" for c in key):
            print(f"Warning: skipping invalid key {key!r} (must be 10 hex chars)")
            continue
        candidates.append((f"key={key}", key))

    if len(candidates) == 1 and not args.uid and not args.key:
        print("Tip: supply --uid <your_cleanergy_uid> or --key <hex_key> to test specific keys.")
        print("     Running with just the uid=0 fallback key.\n")

    print(f"Key probe for {MAC}")
    print(f"Trying {len(candidates)} candidate key(s)...\n")

    for label, key in candidates:
        got_response, n_telem = await probe_key(key, label)
        if got_response:
            print(f"\n✅  KEY FOUND: {key!r}  ({label})")
            if n_telem:
                print(f"   Device is streaming — this key is currently active.")
            else:
                print(f"   Got response but no telemetry — key may be correct but device is busy.")
            return
        await asyncio.sleep(2.0)  # let device settle between attempts

    print("\n❌  None of the candidate keys matched.")
    print("   The device may have been re-keyed to an unknown key.")
    print("   Hold IoT button 5s to factory-reset, then use pair_device.py to set a new key.")

asyncio.run(main())
