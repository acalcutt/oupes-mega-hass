#!/usr/bin/env python3
"""
scan_wifi_ports.py — Scan the OUPES Mega 1 for open TCP/UDP ports on its local IP.

The OUPES Mega 1 connects to the Alibaba Cloud broker at 47.252.10.9:8896
only while a phone is BLE-paired via the Cleanergy app.  This script probes
the device's local IP to determine whether it exposes any local services
(HTTP, MQTT, telnet, custom TCP, etc.) that could be used for cloud-free
WiFi communication.

Prerequisites:
  - The OUPES device must be WiFi-connected (pair via Cleanergy app first).
  - You must know the device's local IP (check your router's DHCP table).
  - Run on the same LAN as the device.

Usage:
  python scan_wifi_ports.py 192.168.1.209
  python scan_wifi_ports.py 192.168.1.209 --ports 1-1024
  python scan_wifi_ports.py 192.168.1.209 --ports 1-65535 --timeout 0.3
  python scan_wifi_ports.py 192.168.1.209 --udp
"""

import argparse
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# Well-known ports to always highlight if open
KNOWN_PORTS = {
    22: "SSH",
    23: "Telnet",
    53: "DNS",
    80: "HTTP",
    443: "HTTPS",
    554: "RTSP",
    1883: "MQTT",
    4443: "Pharos",
    5353: "mDNS",
    6095: "OUPES UDP broadcast (known, one-way)",
    8080: "HTTP-alt",
    8266: "ESP8266 OTA",
    8883: "MQTT-TLS",
    8896: "OUPES cloud broker port",
    9000: "SonarQube / misc",
    48101: "ESP32 debug",
}


def scan_tcp_port(host: str, port: int, timeout: float) -> tuple[int, bool, str]:
    """Try to connect to a TCP port. Returns (port, is_open, banner)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            result = s.connect_ex((host, port))
            if result == 0:
                banner = ""
                try:
                    s.settimeout(1.0)
                    s.sendall(b"\r\n")
                    data = s.recv(256)
                    if data:
                        banner = data[:128].decode("ascii", errors="replace").strip()
                except Exception:
                    pass
                return (port, True, banner)
    except Exception:
        pass
    return (port, False, "")


def scan_udp_port(host: str, port: int, timeout: float) -> tuple[int, bool, str]:
    """Send a UDP probe and check for a response. Returns (port, is_open, data)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            # Send a generic probe
            s.sendto(b"\x00", (host, port))
            try:
                data, _ = s.recvfrom(256)
                return (port, True, data[:64].hex())
            except socket.timeout:
                pass
    except Exception:
        pass
    return (port, False, "")


def parse_port_range(spec: str) -> list[int]:
    """Parse a port range like '1-1024' or '80,443,8896' or '1-1024,8896'."""
    ports = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            lo, hi = int(lo), int(hi)
            if lo < 1:
                lo = 1
            if hi > 65535:
                hi = 65535
            ports.update(range(lo, hi + 1))
        else:
            p = int(part)
            if 1 <= p <= 65535:
                ports.add(p)
    return sorted(ports)


def main():
    parser = argparse.ArgumentParser(
        description="Scan OUPES Mega 1 local IP for open ports"
    )
    parser.add_argument("host", help="Device IP address (e.g. 192.168.1.209)")
    parser.add_argument(
        "--ports", default="1-1024,1883,4443,5353,6095,8080,8266,8883,8896,9000,48101",
        help="Port range to scan (default: 1-1024 + known IoT ports)"
    )
    parser.add_argument(
        "--timeout", type=float, default=0.5,
        help="Connection timeout per port in seconds (default: 0.5)"
    )
    parser.add_argument(
        "--threads", type=int, default=100,
        help="Number of parallel scan threads (default: 100)"
    )
    parser.add_argument(
        "--udp", action="store_true",
        help="Also scan UDP ports (slower, less reliable)"
    )
    args = parser.parse_args()

    ports = parse_port_range(args.ports)
    print(f"Scanning {args.host} — {len(ports)} TCP ports (timeout={args.timeout}s, threads={args.threads})")

    # Quick ping check
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            # Just check if host is reachable at all (try port 8896 first)
            result = s.connect_ex((args.host, 8896))
            if result == 0:
                print(f"  Host reachable — port 8896 (cloud broker) is OPEN")
            else:
                print(f"  Port 8896 closed — device may not be WiFi-connected")
    except Exception:
        print(f"  WARNING: Cannot reach {args.host} — is the device on WiFi?")

    # TCP scan
    open_ports = []
    start = time.time()

    with ThreadPoolExecutor(max_workers=args.threads) as executor:
        futures = {
            executor.submit(scan_tcp_port, args.host, port, args.timeout): port
            for port in ports
        }
        done_count = 0
        for future in as_completed(futures):
            done_count += 1
            port, is_open, banner = future.result()
            if is_open:
                label = KNOWN_PORTS.get(port, "")
                open_ports.append((port, banner, label))
                print(f"  ** TCP {port:5d} OPEN  {label}  {banner}")
            if done_count % 200 == 0:
                print(f"  ... {done_count}/{len(ports)} ports scanned", end="\r")

    elapsed = time.time() - start
    print(f"\nTCP scan complete in {elapsed:.1f}s — {len(open_ports)} open port(s)")

    if open_ports:
        print("\n  Open TCP ports:")
        for port, banner, label in sorted(open_ports):
            line = f"    {port:5d}"
            if label:
                line += f"  ({label})"
            if banner:
                line += f"  banner: {banner[:80]}"
            print(line)
    else:
        print("\n  No open TCP ports found.")
        print("  The device likely does not expose any local services.")

    # UDP scan (optional)
    if args.udp:
        print(f"\nScanning {len(ports)} UDP ports...")
        udp_open = []
        with ThreadPoolExecutor(max_workers=args.threads) as executor:
            futures = {
                executor.submit(scan_udp_port, args.host, port, args.timeout): port
                for port in ports
            }
            for future in as_completed(futures):
                port, is_open, data = future.result()
                if is_open:
                    label = KNOWN_PORTS.get(port, "")
                    udp_open.append((port, data, label))
                    print(f"  ** UDP {port:5d} OPEN  {label}  data={data}")

        print(f"\nUDP scan complete — {len(udp_open)} responsive port(s)")
        if not udp_open:
            print("  No UDP ports responded (note: UDP scanning is unreliable).")

    print("\n--- Summary ---")
    if not open_ports and not (args.udp and udp_open):
        print("No local services detected. The OUPES Mega 1 likely only communicates")
        print("via BLE (local) and TCP 8896 outbound to the cloud broker (47.252.10.9).")
        print("There is no evidence of a local WiFi API on this firmware version.")
    else:
        print("Open ports detected! These may be explorable for local WiFi control.")
        print("Next steps: try connecting to each open port and sending known OUPES")
        print("protocol payloads (cmd=ping, cmd=3 telemetry requests, etc.)")


if __name__ == "__main__":
    main()
