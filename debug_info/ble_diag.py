#!/usr/bin/env python3
"""
ble_diag.py — Diagnose BLE characteristics and notification support for OUPES Mega.
Run this BEFORE set_device_key.py to verify the write/notify UUIDs are accessible.
"""
import asyncio
import sys
from bleak import BleakClient, BleakScanner

WRITE_CHAR  = "00002b11-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR = "00002b10-0000-1000-8000-00805f9b34fb"

MAC = sys.argv[1] if len(sys.argv) > 1 else "8C:D0:B2:A8:E1:44"

async def diagnose():
    print(f"Scanning for {MAC}...")
    device = await BleakScanner.find_device_by_address(MAC, timeout=15.0)
    if device is None:
        print("ERROR: not found")
        return
    print(f"Found: name={device.name!r} address={device.address}")

    async with BleakClient(device, timeout=20.0) as client:
        print(f"Connected. MTU={getattr(client, 'mtu_size', '?')}")
        print()

        # Print all services and characteristics
        for service in client.services:
            print(f"Service: {service.uuid}")
            for char in service.characteristics:
                props = char.properties
                marker_w = " ← WRITE" if char.uuid.lower() == WRITE_CHAR.lower() else ""
                marker_n = " ← NOTIFY" if char.uuid.lower() == NOTIFY_CHAR.lower() else ""
                print(f"  Char: {char.uuid}  props={props}  handle=0x{char.handle:04x}{marker_w}{marker_n}")
                for desc in char.descriptors:
                    print(f"    Desc: {desc.uuid}  handle=0x{desc.handle:04x}")
        print()

        # Try subscribing to notifications
        received = []
        def on_notify(handle, data):
            received.append(bytes(data))
            print(f"  NOTIFY: {data.hex()}")

        print(f"Subscribing to NOTIFY_CHAR {NOTIFY_CHAR}...")
        try:
            await client.start_notify(NOTIFY_CHAR, on_notify)
            print("  start_notify: OK")
        except Exception as e:
            print(f"  start_notify FAILED: {e}")
            return

        await asyncio.sleep(3.0)
        print(f"  Notifications received in 3s: {len(received)}")

        # Send one test packet (the first auth packet, zero-key)
        test_pkt = bytes.fromhex("0100019901010101010101010101010101010192")
        print(f"\nSending test packet with response=False: {test_pkt.hex()}")
        try:
            await client.write_gatt_char(WRITE_CHAR, test_pkt, response=False)
            print("  write_gatt_char(response=False): OK")
        except Exception as e:
            print(f"  write_gatt_char(response=False) FAILED: {e}")
            print("  Trying response=True...")
            try:
                await client.write_gatt_char(WRITE_CHAR, test_pkt, response=True)
                print("  write_gatt_char(response=True): OK")
            except Exception as e2:
                print(f"  write_gatt_char(response=True) FAILED: {e2}")

        await asyncio.sleep(3.0)
        print(f"\nTotal notifications received: {len(received)}")
        await client.stop_notify(NOTIFY_CHAR)

asyncio.run(diagnose())
