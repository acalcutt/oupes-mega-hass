#!/usr/bin/env python3
"""
provision_wifi.py — Send WiFi credentials to an already-paired OUPES Mega over BLE.

Unlike pair_device.py (which factory-resets and re-pairs), this script connects
to a device that is *already paired* with a known key and sends WiFi credentials
via the AUTH (0x01) packet sequence.  No CLAIM or handshake polling is needed.

Reverse-engineered from Cleanergy app btsnoop captures (2026-04-12):
  The app sends the WiFi-mode AUTH sequence on an existing BLE connection.
  The device responds with AUTH RESP status=0 (accepted) and begins
  connecting to the specified WiFi network.

Usage:
  python provision_wifi.py 8C:D0:B2:A8:E1:44 --key e98ff526ad --ssid "MyWiFi_2.4GHz" --psk "MyPassword"
  python provision_wifi.py 8C:D0:B2:A8:E1:44 --key e98ff526ad --ssid MyWiFi --psk pass123 --region wp-cn
"""

import asyncio
import argparse
import sys
import time

from bleak import BleakClient, BleakScanner

# Re-use packet-building helpers from pair_device.py
from pair_device import (
    WRITE_CHAR,
    NOTIFY_CHAR,
    KEEPALIVE,
    build_auth,
    _crc8,
    _ts_pkt,
)

TIMEOUT_CONNECT = 20.0
TIMEOUT_SCAN = 15.0


def _auth_resp(data: bytes) -> bytes:
    """Build a 20-byte AUTH RESP packet: cmd=0x01, sub=0x80, then payload, with CRC."""
    p = bytearray(20)
    p[0] = 0x01
    p[1] = 0x80
    for i, b in enumerate(data):
        p[2 + i] = b
    p[19] = _crc8(bytes(p[:19]))
    return bytes(p)


# Post-credential handshake packets observed from Cleanergy app btsnoop:
# These subscribe to telemetry attributes and activate the WiFi TCP connection.
AUTH_RESP_CONFIRM  = _auth_resp(b'\x02\x01\x01')           # idx=1 status=1 (confirm auth)
AUTH_RESP_POLL     = _auth_resp(b'\x03\x02\x54\x01')       # idx=2 status=0x54 (wifi poll)
AUTH_RESP_SUB9     = _auth_resp(b'\x02\x09\x01\x02\x03\x04\x05\x06\x07\x08\x09')  # subscribe attrs 1-9
AUTH_RESP_SUB5     = _auth_resp(b'\x02\x05\x15\x16\x17\x1e\x20')                   # subscribe attrs 0x15..0x20
AUTH_RESP_CFG33    = _auth_resp(b'\x02\x01\x33')            # config 0x33
AUTH_RESP_ACTIVATE = _auth_resp(b'\x04\x01\x01')            # activate telemetry


async def provision_wifi(mac: str, key: str, ssid: str, psk: str,
                         region: str = "wp-cn") -> bool:
    """Connect to an already-paired device and send WiFi credentials.
    Returns True if the device acknowledged the WiFi configuration."""

    print(f"\n{'='*60}")
    print(f"  WiFi Provisioning")
    print(f"{'='*60}")
    print(f"  MAC    : {mac}")
    print(f"  Key    : {key}")
    print(f"  SSID   : {ssid}")
    print(f"  PSK    : {'*' * len(psk)}")
    print(f"  Region : {region}")
    print()

    # Scan
    print("  Scanning for device...")
    dev = await BleakScanner.find_device_by_address(mac, timeout=TIMEOUT_SCAN)
    if not dev:
        print(f"  ERROR: Device {mac} not found after {TIMEOUT_SCAN}s")
        return False
    print(f"  Found: {dev.name}  rssi={getattr(dev, 'rssi', '?')}")

    received: list[bytes] = []
    wifi_accepted = False
    wifi_rejected = False
    got_telemetry = False
    t0 = time.monotonic()

    def on_notify(_h, data: bytearray):
        nonlocal wifi_accepted, wifi_rejected, got_telemetry
        pkt = bytes(data)
        received.append(pkt)
        elapsed = time.monotonic() - t0

        prefix = pkt[0] if pkt else 0
        tag = ""

        if len(pkt) >= 5:
            # AUTH RESP: 01 80 01 01 <status>
            if prefix == 0x01 and pkt[1] == 0x80 and pkt[2] == 0x01:
                status = pkt[4]
                if status == 0x00:
                    wifi_accepted = True
                    tag = " <<< WiFi ACCEPTED (status=0)"
                elif status == 0x03:
                    wifi_rejected = True
                    tag = " <<< WiFi REJECTED (status=3)"
                elif status == 0x01:
                    tag = " (status=1, configured)"
                else:
                    tag = f" (status=0x{status:02x})"
            # Telemetry
            elif prefix == 0x01 and pkt[1] in (0x00, 0x81) and pkt[2] == 0x0a:
                got_telemetry = True
                tag = " (telemetry)"

        print(f"  << [{len(received):3d}] t={elapsed:5.1f}s "
              f"[0x{prefix:02x}] : {pkt.hex()[:44]}{tag}")

    try:
        async with BleakClient(dev, timeout=TIMEOUT_CONNECT) as client:
            print(f"  Connected")

            # Enable notifications (CCCD)
            await client.start_notify(NOTIFY_CHAR, on_notify)
            await asyncio.sleep(2.0)

            # Build and send WiFi AUTH sequence
            auth_seq = build_auth(key, ssid=ssid, psk=psk, region=region)
            print(f"\n  >> Sending WiFi AUTH ({len(auth_seq)} packets)...")
            for i, pkt in enumerate(auth_seq):
                ascii_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in pkt)
                print(f"     [{i:2d}] {pkt.hex()}  {ascii_part}")
                await client.write_gatt_char(WRITE_CHAR, pkt, response=False)
                await asyncio.sleep(0.08)

            # Wait for initial response
            print(f"\n  >> Waiting for device response...")
            for _ in range(20):  # up to 2 seconds
                await asyncio.sleep(0.1)
                if wifi_accepted or wifi_rejected:
                    break

            # ── Post-credential handshake (from btsnoop analysis) ──
            # The app sends AUTH RESP confirm, WiFi poll, attribute
            # subscriptions, and activation commands.  Without these
            # the device stays in WiFi-idle (gratuitous ARPs only).

            print(f"\n  >> Sending post-credential handshake...")

            # Confirm auth
            print(f"     AUTH RESP confirm:  {AUTH_RESP_CONFIRM.hex()}")
            await client.write_gatt_char(WRITE_CHAR, AUTH_RESP_CONFIRM, response=False)
            await asyncio.sleep(0.1)

            # WiFi poll (idx=2 status=0x54 'T')
            print(f"     AUTH RESP poll:     {AUTH_RESP_POLL.hex()}")
            await client.write_gatt_char(WRITE_CHAR, AUTH_RESP_POLL, response=False)
            await asyncio.sleep(0.1)
            await client.write_gatt_char(WRITE_CHAR, AUTH_RESP_POLL, response=False)
            await asyncio.sleep(0.5)

            # Subscribe to attribute groups
            print(f"     AUTH RESP sub9:     {AUTH_RESP_SUB9.hex()}")
            await client.write_gatt_char(WRITE_CHAR, AUTH_RESP_SUB9, response=False)
            await asyncio.sleep(0.05)
            print(f"     AUTH RESP sub5:     {AUTH_RESP_SUB5.hex()}")
            await client.write_gatt_char(WRITE_CHAR, AUTH_RESP_SUB5, response=False)
            await asyncio.sleep(0.05)
            print(f"     AUTH RESP cfg33:    {AUTH_RESP_CFG33.hex()}")
            await client.write_gatt_char(WRITE_CHAR, AUTH_RESP_CFG33, response=False)
            await asyncio.sleep(0.05)

            # Activate telemetry (sent twice, matching app behavior)
            print(f"     AUTH RESP activate: {AUTH_RESP_ACTIVATE.hex()}")
            await client.write_gatt_char(WRITE_CHAR, AUTH_RESP_ACTIVATE, response=False)
            await asyncio.sleep(0.05)
            await client.write_gatt_char(WRITE_CHAR, AUTH_RESP_ACTIVATE, response=False)
            await asyncio.sleep(0.05)

            # Time sync
            ts_pkt = _ts_pkt(0x01)
            print(f"     Time sync:         {ts_pkt.hex()}")
            await client.write_gatt_char(WRITE_CHAR, ts_pkt, response=False)
            await asyncio.sleep(0.1)

            # Continue polling WiFi status for ~30s (matching app's 5s interval)
            print(f"\n  >> Polling WiFi/cloud status (30s, every 5s)...")
            for poll_i in range(6):
                await client.write_gatt_char(WRITE_CHAR, AUTH_RESP_POLL, response=False)
                for _ in range(50):  # 5s in 100ms increments
                    await asyncio.sleep(0.1)
                    if got_telemetry:
                        print(f"     Poll {poll_i+1}: telemetry flowing!")
                        break
                if got_telemetry:
                    break
                print(f"     Poll {poll_i+1}/6: waiting...")

            # Send keepalive
            await client.write_gatt_char(WRITE_CHAR, KEEPALIVE, response=False)
            await asyncio.sleep(2.0)

            try:
                await client.stop_notify(NOTIFY_CHAR)
            except Exception:
                pass

    except Exception as e:
        print(f"  CONNECTION ERROR: {e}")
        return False

    # Summary
    print(f"\n{'='*60}")
    print(f"  Result: {len(received)} packets received")

    if wifi_accepted:
        print(f"  WiFi credentials ACCEPTED by device")
        print(f"  The device should now connect to: {ssid}")
        print(f"  Check your router's DHCP table for the device's IP.")
        print(f"{'='*60}")
        return True
    elif wifi_rejected:
        print(f"  WiFi credentials REJECTED (status=3)")
        print(f"  The device key may be wrong. Verify with probe_key.py.")
        print(f"{'='*60}")
        return False
    else:
        print(f"  No explicit WiFi ACK/NACK received")
        if got_telemetry:
            print(f"  Telemetry is flowing — device accepted the connection.")
            print(f"  WiFi may have been accepted silently.")
        else:
            print(f"  Try verifying WiFi connectivity on your router.")
        print(f"{'='*60}")
        return got_telemetry


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Provision WiFi on an already-paired OUPES Mega over BLE")
    parser.add_argument("mac", help="Device MAC (e.g. 8C:D0:B2:A8:E1:44)")
    parser.add_argument("--key", required=True,
                        help="10-char hex key the device is already paired with")
    parser.add_argument("--ssid", required=True,
                        help="WiFi SSID to provision (max 32 chars)")
    parser.add_argument("--psk", required=True,
                        help="WiFi password to provision (max 17 chars)")
    parser.add_argument("--region", default="wp-cn",
                        help="Server region code (default: wp-cn)")
    args = parser.parse_args()

    k = args.key.lower()
    if len(k) != 10 or not all(c in "0123456789abcdef" for c in k):
        print(f"ERROR: --key must be 10 hex chars, got: {args.key!r}")
        sys.exit(1)
    if len(args.ssid) > 32:
        print(f"ERROR: --ssid max 32 chars, got {len(args.ssid)}")
        sys.exit(1)
    if len(args.psk) > 17:
        print(f"ERROR: --psk max 17 chars, got {len(args.psk)}")
        sys.exit(1)

    result = asyncio.run(provision_wifi(args.mac, k, args.ssid, args.psk,
                                        region=args.region))
    sys.exit(0 if result else 1)
