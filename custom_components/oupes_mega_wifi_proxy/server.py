"""Async TCP server that emulates the OUPES cloud broker on port 8896.

The real broker is at 47.252.10.9:8896.  DNS-redirect the device's broker
hostname (resolved during provisioning) to this HA instance so the device
connects here instead.

Protocol summary (text-based, line-terminated with \\r\\n):
  Device → server (confirmed via pfSense PCAP 2026-04-13):
    cmd=subscribe&from=device&topic=control_<id>&device_id=<id>&device_key=<key>
    cmd=keep&device_id=<id>&device_key=<key>
    cmd=ping                         (device heartbeat, ~every 90 s)
    cmd=publish&topic=device_<id>&device_id=<id>&device_key=<key>&message=<json>

  App → server:
    cmd=auth&token=<token>
    cmd=subscribe&topic=device_<id>&from=control&device_id=<id>&device_key=<key>
    cmd=keep                         (client keepalive)
    cmd=is_online&device_id=<id>
    cmd=publish&...&message=<json>   (cmd=2 telemetry request)

  Server → device/app:
    cmd=subscribe&topic=<echoed_topic>&res=1
    cmd=pong&res=1
    cmd=keep&timestamp=<unix_seconds>&res=1
    cmd=publish&res=1&num=1          (ACK a publish)

  Server → device (telemetry request):
    cmd=publish&device_id=<id>&topic=control_<id>&device_key=<key>&message=<json>
    where <json> = {"msg":{"attr":[...]},"pv":0,"cmd":2,"sn":"<ts_ms>"}

For this first version the server:
  - Accepts any connection
  - Sends correct protocol responses so the device stays connected
  - Logs every message it receives
  - Parses and logs cmd=10 telemetry payloads
  - Periodically polls attribute groups to trigger cmd=10 responses
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import time
from pathlib import Path
from typing import Any

from .const import VALIDATION_ACCEPT_ALL, VALIDATION_ACCEPT_REGISTERED, VALIDATION_LOG_ONLY

_LOGGER = logging.getLogger(__name__)

# Attribute groups to poll — same as the cloud client example in the debug docs.
# Note: setting DPIDs (41, 45, 46, 47, 49, 58, 63) are NOT queryable over WiFi;
# the device firmware ignores cmd=2 for them (BLE-only).  Setting values are
# echoed back only when written via cmd=3.
_ATTR_GROUPS: list[list[int]] = [
    [1, 2, 3, 4, 5, 6, 7, 8, 9],
    [21, 22, 23, 30, 32],
    [51],
    [101, 53, 54, 78, 79, 80],
]

# How often (seconds) to send a polling batch while a device is connected.
_POLL_INTERVAL = 30.0


def _ts_ms() -> str:
    return str(int(time.time() * 1000))


def _ts_s() -> str:
    """Unix timestamp in seconds (matches real broker keep/bind responses)."""
    return str(int(time.time()))


def _parse_kv(line: str) -> dict[str, str]:
    """Parse a key=value&key=value line into a dict."""
    result: dict[str, str] = {}
    for part in line.strip().split("&"):
        if "=" in part:
            k, _, v = part.partition("=")
            result[k] = v
    return result


class _DeviceSession:
    """Handles one connected device over the broker TCP connection."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        on_telemetry: Any,
        device_registry: dict[str, str] | None = None,
        validation_mode: str = VALIDATION_ACCEPT_ALL,
        debug_file: Path | None = None,
        debug_raw_lines: bool = False,
        debug_telemetry: bool = False,
        connected_devices: dict[str, str] | None = None,
        topic_subscriptions: dict[str, list] | None = None,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._on_telemetry = on_telemetry
        self._device_registry: dict[str, str] = device_registry or {}
        self._validation_mode = validation_mode
        self._debug_file = debug_file
        self._debug_raw_lines = debug_raw_lines
        self._debug_telemetry = debug_telemetry
        self._connected_devices = connected_devices if connected_devices is not None else {}
        self._topic_subs: dict[str, list] = topic_subscriptions if topic_subscriptions is not None else {}
        self._device_id: str | None = None
        self._device_key: str | None = None
        self._peer = writer.get_extra_info("peername")
        self._buf = ""
        self._poll_task: asyncio.Task | None = None
        self._subscribed_topics: list[str] = []

    def _debug_write(self, record: dict) -> None:
        """Append one JSON-lines record to the debug file."""
        if self._debug_file is None:
            return
        record["ts"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        record["peer"] = str(self._peer)
        try:
            with self._debug_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        except OSError:
            pass

    def _send(self, msg: str) -> None:
        try:
            self._writer.write((msg + "\r\n").encode())
            if self._debug_raw_lines:
                self._debug_write({"dir": "TX", "raw": msg})
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("OUPES WiFi proxy: send error to %s: %s", self._peer, exc)

    async def _poll_loop(self) -> None:
        """Periodically send cmd=2 (read request) for each attribute group."""
        await asyncio.sleep(2.0)  # brief settle after subscribe
        while True:
            if self._device_id and self._device_key:
                for attrs in _ATTR_GROUPS:
                    msg = json.dumps({
                        "msg": {"attr": attrs},
                        "pv": 0,
                        "cmd": 2,
                        "sn": _ts_ms(),
                    }, separators=(",", ":"))
                    self._send(
                        f"cmd=publish"
                        f"&device_id={self._device_id}"
                        f"&topic=control_{self._device_id}"
                        f"&device_key={self._device_key}"
                        f"&message={msg}"
                    )
                    await asyncio.sleep(0.15)
            await asyncio.sleep(_POLL_INTERVAL)

    def _handle_line(self, line: str) -> None:
        """Dispatch one received line."""
        if self._debug_raw_lines:
            self._debug_write({"dir": "RX", "raw": line})
        kv = _parse_kv(line)
        cmd = kv.get("cmd", "")

        if cmd == "auth":
            token = kv.get("token", "")
            _LOGGER.info(
                "OUPES WiFi proxy [%s]: auth token=%s…", self._peer, token[:8]
            )
            # The real broker ACKs silently or with res=1; send nothing for now.

        elif cmd == "subscribe":
            device_id = kv.get("device_id", "")
            device_key = kv.get("device_key", "")
            topic = kv.get("topic", "")
            self._device_id = device_id
            self._device_key = device_key
            _LOGGER.info(
                "OUPES WiFi proxy [%s]: subscribe device_id=%s key=%s…",
                self._peer, device_id, device_key[:4] if device_key else "",
            )
            # Validate credentials in strict or log-only mode.
            if self._validation_mode in (VALIDATION_ACCEPT_REGISTERED, VALIDATION_LOG_ONLY):
                reject = self._validation_mode == VALIDATION_ACCEPT_REGISTERED
                expected_key = self._device_registry.get(device_id)
                if expected_key is None:
                    _LOGGER.warning(
                        "OUPES WiFi proxy [%s]: unregistered device_id=%s%s",
                        self._peer, device_id,
                        " — closing connection" if reject else " (log-only, staying connected)",
                    )
                    if reject:
                        try:
                            self._writer.close()
                        except Exception:  # noqa: BLE001
                            pass
                        return
                elif expected_key != device_key:
                    _LOGGER.warning(
                        "OUPES WiFi proxy [%s]: device_id=%s wrong device_key%s",
                        self._peer, device_id,
                        " — closing connection" if reject else " (log-only, staying connected)",
                    )
                    if reject:
                        try:
                            self._writer.close()
                        except Exception:  # noqa: BLE001
                            pass
                        return
            self._send(f"cmd=subscribe&topic={topic}&res=1")
            # Register topic subscription for message routing.
            if topic:
                self._subscribed_topics.append(topic)
                self._topic_subs.setdefault(topic, []).append(self)
                _LOGGER.debug(
                    "OUPES WiFi proxy [%s]: subscribed to topic %s",
                    self._peer, topic,
                )
            # Register this device as online (only for device-side sessions).
            from_field = kv.get("from", "")
            if from_field == "device" and device_id:
                peer_host = self._peer[0] if self._peer else "unknown"
                self._connected_devices[device_id] = peer_host

                # Immediately notify the device that clients are listening.
                # On the real cloud broker, the device gets this signal at
                # connection time which triggers continuous telemetry.
                device_topic = f"device_{device_id}"
                has_clients = bool(self._topic_subs.get(device_topic))
                if has_clients:
                    self._send(f"cmd=is_online&device_id={device_id}")
                    # Send cmd=3 attr 84=1 (streaming keepalive, same as BLE).
                    ka_msg = json.dumps({
                        "msg": {"attr": [84], "data": {"84": 1}},
                        "pv": 0,
                        "cmd": 3,
                        "sn": _ts_ms(),
                    }, separators=(",", ":"))
                    self._send(
                        f"cmd=publish"
                        f"&device_id={device_id}"
                        f"&topic=control_{device_id}"
                        f"&message={ka_msg}"
                    )
                    # Send initial cmd=2 query with attr=[1] (matching real app).
                    q_msg = json.dumps({
                        "msg": {"attr": [1]},
                        "pv": 0,
                        "cmd": 2,
                        "sn": _ts_ms(),
                    }, separators=(",", ":"))
                    self._send(
                        f"cmd=publish"
                        f"&device_id={device_id}"
                        f"&topic=control_{device_id}"
                        f"&message={q_msg}"
                    )
            elif from_field != "device" and device_id:
                # Client-side session — don't pollute _connected_devices.
                # But DO notify the device that a client just subscribed.
                control_topic = f"control_{device_id}"
                for dev_sess in self._topic_subs.get(control_topic, []):
                    dev_sess._send(f"cmd=is_online&device_id={device_id}")

            # Start polling only for device-side sessions (from=device),
            # not for app-side client sessions (from=control).
            if from_field == "device" and (self._poll_task is None or self._poll_task.done()):
                self._poll_task = asyncio.ensure_future(self._poll_loop())

        elif cmd == "ping":
            self._send("cmd=pong&res=1")

        elif cmd == "keep":
            self._send(f"cmd=keep&timestamp={_ts_s()}&res=1")

        elif cmd == "is_online":
            device_id = kv.get("device_id", "")
            online = device_id in self._connected_devices
            _LOGGER.debug(
                "OUPES WiFi proxy [%s]: is_online device_id=%s online=%s",
                self._peer, device_id, online,
            )
            # Forward is_online to the device so it knows a client is connected.
            # The real cloud broker does this; without it the device stops
            # streaming telemetry after its initial burst (~30 s).
            if online:
                control_topic = f"control_{device_id}"
                for dev_sess in self._topic_subs.get(control_topic, []):
                    dev_sess._send(f"cmd=is_online&device_id={device_id}")
            # Respond to the requesting client.
            self._send(f"cmd=is_online&res=1&online={'1' if online else '0'}")

        elif cmd == "publish":
            raw_msg = kv.get("message", "")
            topic = kv.get("topic", "")
            device_id = kv.get("device_id", self._device_id or "unknown")
            device_key = kv.get("device_key", self._device_key or "")
            # ACK the publish so the device considers it delivered.
            self._send("cmd=publish&res=1&num=1")

            # ── Route the message to all sessions subscribed to this topic ──
            if topic:
                subscribers = self._topic_subs.get(topic, [])
                for sub in subscribers:
                    if sub is self:
                        continue  # don't echo back to the sender
                    # Real broker omits device_key in forwarded messages.
                    fwd = (
                        f"cmd=publish"
                        f"&device_id={device_id}"
                        f"&topic={topic}"
                        f"&message={raw_msg}"
                    )
                    sub._send(fwd)

            # Parse and log the telemetry payload.
            try:
                payload = json.loads(raw_msg)
            except (json.JSONDecodeError, ValueError):
                _LOGGER.debug(
                    "OUPES WiFi proxy [%s]: unparseable publish: %s",
                    self._peer, raw_msg[:200],
                )
                return

            payload_cmd = payload.get("cmd")
            if payload_cmd == 10:
                # cmd=10 = telemetry response from device.
                data: dict = payload.get("msg", {}).get("data", {})
                _LOGGER.info(
                    "OUPES WiFi proxy [%s]: telemetry device_id=%s data=%s",
                    self._peer, device_id, data,
                )
                if self._debug_telemetry:
                    self._debug_write({
                        "type": "telemetry",
                        "device_id": device_id,
                        "data": data,
                    })
                if self._on_telemetry:
                    self._on_telemetry(device_id, data)
            else:
                _LOGGER.debug(
                    "OUPES WiFi proxy [%s]: publish cmd=%s payload=%s",
                    self._peer, payload_cmd, raw_msg[:400],
                )

        else:
            _LOGGER.debug(
                "OUPES WiFi proxy [%s]: unknown cmd=%r line=%s",
                self._peer, cmd, line[:200],
            )

    async def run(self) -> None:
        """Read lines from the device until the connection closes."""
        _LOGGER.info("OUPES WiFi proxy: device connected from %s", self._peer)
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        self._reader.read(4096), timeout=120.0
                    )
                except asyncio.TimeoutError:
                    _LOGGER.debug(
                        "OUPES WiFi proxy [%s]: read timeout, closing", self._peer
                    )
                    break
                if not chunk:
                    break
                self._buf += chunk.decode(errors="replace")
                while "\r\n" in self._buf:
                    line, self._buf = self._buf.split("\r\n", 1)
                    line = line.strip()
                    if line:
                        self._handle_line(line)
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("OUPES WiFi proxy [%s]: error: %s", self._peer, exc)
        finally:
            if self._poll_task and not self._poll_task.done():
                self._poll_task.cancel()
            # Deregister from connected devices.
            if self._device_id and self._device_id in self._connected_devices:
                del self._connected_devices[self._device_id]
            # Unsubscribe from all topics.
            for topic in self._subscribed_topics:
                subs = self._topic_subs.get(topic, [])
                try:
                    subs.remove(self)
                except ValueError:
                    pass
                if not subs:
                    self._topic_subs.pop(topic, None)
            self._subscribed_topics.clear()
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            _LOGGER.info("OUPES WiFi proxy: device disconnected from %s", self._peer)


class OUPESWiFiProxyServer:
    """TCP server that mimics the OUPES cloud broker."""

    def __init__(
        self,
        port: int,
        on_telemetry: Any = None,
        device_registry: dict[str, str] | None = None,
        validation_mode: str = VALIDATION_ACCEPT_ALL,
        debug_file: Path | None = None,
        debug_raw_lines: bool = False,
        debug_telemetry: bool = False,
    ) -> None:
        self._port = port
        self._on_telemetry = on_telemetry
        self._device_registry: dict[str, str] = device_registry or {}
        self._validation_mode = validation_mode
        self._debug_file = debug_file
        self._debug_raw_lines = debug_raw_lines
        self._debug_telemetry = debug_telemetry
        self._server: asyncio.Server | None = None
        # Maps device_id -> peer IP for currently-connected devices.
        self._connected_devices: dict[str, str] = {}
        # Maps topic -> list of sessions subscribed to that topic.
        self._topic_subscriptions: dict[str, list[_DeviceSession]] = {}

    def is_device_online(self, device_id: str) -> bool:
        """Return True if the device currently has an active TCP broker connection."""
        return device_id in self._connected_devices

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, "0.0.0.0", self._port
        )
        _LOGGER.info(
            "OUPES WiFi proxy: TCP broker listening on port %d", self._port
        )

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            _LOGGER.info("OUPES WiFi proxy: TCP broker stopped")

    def update_device_registry(self, registry: dict[str, str]) -> None:
        """Hot-swap the device registry (called when sub-entries are added/removed)."""
        self._device_registry = registry
        _LOGGER.debug("OUPES TCP: device registry updated (%d devices)", len(registry))

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        session = _DeviceSession(
            reader, writer, self._on_telemetry,
            self._device_registry, self._validation_mode,
            self._debug_file, self._debug_raw_lines, self._debug_telemetry,
            self._connected_devices,
            self._topic_subscriptions,
        )
        await session.run()
