"""DataUpdateCoordinator that connects to the OUPES WiFi proxy broker.

Acts as a TCP client to the proxy's broker (port 8896), using the same
protocol the real Android app uses:
  1. cmd=auth&token=<token>
  2. cmd=subscribe&topic=device_<id>&from=control&device_id=<id>&device_key=<key>
  3. Receives cmd=publish messages with cmd=10 telemetry payloads
  4. Sends cmd=publish for commands (cmd=2 attr queries, cmd=3 writes)
  5. cmd=keep heartbeat every 60 s

The coordinator accumulates partial telemetry updates into a single snapshot
using the same OUPESData format as the BLE coordinator:
  {"attrs": {int: int}, "ext_batteries": {slot_int: {attr_int: int}}}
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    ATTR78_RUNTIME_MAX,
    DOMAIN,
    EXT_BATTERY_ATTRS,
)

_LOGGER = logging.getLogger(__name__)

# Attribute groups to poll — same as the proxy server uses.
# Note: setting DPIDs (41, 45, 46, 47, 49, 58, 63) are NOT queryable over WiFi;
# the device firmware ignores cmd=2 for them (BLE-only).  Settings are written
# via cmd=3 and the device echoes the value back in the cmd=3 ACK.
_ATTR_GROUPS: list[list[int]] = [
    [1, 2, 3, 4, 5, 6, 7, 8, 9],
    [21, 22, 23, 30, 32],
    [51],
    [101, 53, 54, 78, 79, 80],
]

_KEEPALIVE_INTERVAL = 60.0
_IS_ONLINE_INTERVAL = 5.0
_ATTR84_KEEPALIVE_INTERVAL = 10.0
_RECONNECT_DELAY = 10.0

# Type alias
OUPESData = dict


class OUPESWiFiCoordinator(DataUpdateCoordinator[OUPESData]):
    """Connects to the WiFi proxy TCP broker as an app-side client."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        host: str,
        tcp_port: int,
        device_id: str,
        device_key: str,
        device_name: str,
        product_id: str = "",
        mac_address: str = "",
        token: str = "",
        runtime_max_minutes: int = ATTR78_RUNTIME_MAX,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{device_id}",
            # No polling interval — we push data from the TCP stream.
            update_interval=None,
        )
        self.host = host
        self.tcp_port = tcp_port
        self.device_id = device_id
        self.device_key = device_key
        self.device_name = device_name
        self.product_id = product_id
        self.mac_address = mac_address
        self.token = token
        self.last_successful_update: datetime | None = None
        self.stale_timeout = timedelta(minutes=5)
        self._runtime_max = runtime_max_minutes

        # Accumulated telemetry state
        self._attrs: dict[int, int] = {105: 1}  # pre-seed charge mode
        self._ext_batteries: dict[int, dict[int, int]] = {}
        self._current_slot: int = 1

        # Connection state
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connection_task: asyncio.Task | None = None
        self._stopping = False
        self._pending_commands: list[str] = []

    # ── Public interface ──────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the persistent TCP connection background task."""
        if self._connection_task is None or self._connection_task.done():
            self._stopping = False
            self._connection_task = self.hass.async_create_background_task(
                self._run_connection_loop(),
                name=f"oupes_wifi_{self.device_id}",
            )

    def stop(self) -> None:
        """Cancel the background connection task."""
        self._stopping = True
        if self._connection_task and not self._connection_task.done():
            self._connection_task.cancel()
        self._connection_task = None
        self._close_writer()

    def send_command(self, cmd: int, data: dict) -> None:
        """Queue a command to be sent to the device via the broker.

        Args:
            cmd: Protocol command number (2=query, 3=write).
            data: The msg payload dict.
        """
        msg = json.dumps({
            "msg": data,
            "pv": 0,
            "cmd": cmd,
            "sn": str(int(time.time() * 1000)),
        }, separators=(",", ":"))
        line = (
            f"cmd=publish"
            f"&device_id={self.device_id}"
            f"&topic=control_{self.device_id}"
            f"&device_key={self.device_key}"
            f"&message={msg}"
        )
        self._pending_commands.append(line)

    def send_output_command(self, bitmask: int) -> None:
        """Send a cmd=3 output-bitmask write to the device."""
        self.send_command(3, {"attr": [1], "data": {"1": bitmask}})

    def optimistic_set_attr(self, attr: int, value: int) -> None:
        """Optimistically set an attr in the source-of-truth dict.

        Call this before async_write_ha_state() so the optimistic UI state
        is not overwritten by the next _apply_telemetry → async_set_updated_data
        call (which rebuilds coordinator.data from self._attrs).
        """
        self._attrs[attr] = value

    def send_setting_command(self, dpid: int, value: int) -> None:
        """Send a cmd=3 setting write (DPID) to the device."""
        self.send_command(3, {"attr": [dpid], "data": {str(dpid): value}})

    async def async_query_attrs(self) -> None:
        """Send cmd=2 queries for all attribute groups."""
        for attrs in _ATTR_GROUPS:
            self.send_command(2, {"attr": attrs})
            await asyncio.sleep(0.1)

    # ── DataUpdateCoordinator override ────────────────────────────────────────

    async def _async_update_data(self) -> OUPESData:
        """Return a snapshot of the current accumulated data."""
        return {
            "attrs": dict(self._attrs),
            "ext_batteries": {
                s: dict(sd) for s, sd in self._ext_batteries.items()
            },
        }

    # ── TCP connection loop ───────────────────────────────────────────────────

    async def _run_connection_loop(self) -> None:
        """Reconnect loop — runs until stop() is called."""
        while not self._stopping:
            try:
                await self._connect_and_run()
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning(
                    "WiFi coordinator %s connection error: %s — reconnecting in %ds",
                    self.device_id, exc, _RECONNECT_DELAY,
                )
            if not self._stopping:
                await asyncio.sleep(_RECONNECT_DELAY)

    async def _connect_and_run(self) -> None:
        """Single TCP connection lifecycle."""
        _LOGGER.info(
            "WiFi coordinator: connecting to %s:%d for device %s",
            self.host, self.tcp_port, self.device_id,
        )
        self._reader, self._writer = await asyncio.open_connection(
            self.host, self.tcp_port,
        )
        try:
            # 1. Auth (the proxy accepts any token — this step is optional
            #    but matches real app behaviour)
            if self.token:
                self._send_line(f"cmd=auth&token={self.token}")
                await asyncio.sleep(0.2)

            # 2. Subscribe to device telemetry topic
            self._send_line(
                f"cmd=subscribe"
                f"&topic=device_{self.device_id}"
                f"&from=control"
                f"&device_id={self.device_id}"
                f"&device_key={self.device_key}"
            )
            await asyncio.sleep(0.3)

            # 3. Send initial attr 84 keepalive — tells the device to start
            #    streaming (same as the BLE KEEPALIVE_PKT: cmd=3, attr 84=1).
            self._send_attr84_keepalive()
            await asyncio.sleep(0.2)

            # 4. Run read/keepalive loop (proxy server handles polling)
            await self._run_session()

        finally:
            self._close_writer()
            _LOGGER.info(
                "WiFi coordinator: disconnected from %s:%d (device %s)",
                self.host, self.tcp_port, self.device_id,
            )

    async def _run_session(self) -> None:
        """Read incoming data and send keepalives.

        The proxy server handles periodic polling; this coordinator only
        listens for forwarded telemetry and flushes on-demand commands.
        """
        buf = ""
        last_keepalive = time.monotonic()
        last_is_online = time.monotonic()
        last_attr84 = time.monotonic()

        while not self._stopping:
            # Flush any pending commands
            while self._pending_commands:
                line = self._pending_commands.pop(0)
                self._send_line(line)
                await asyncio.sleep(0.05)

            # Read with timeout for keepalive scheduling
            try:
                chunk = await asyncio.wait_for(
                    self._reader.read(4096), timeout=2.0,
                )
            except asyncio.TimeoutError:
                chunk = b""
            except (ConnectionResetError, asyncio.IncompleteReadError):
                break

            if chunk:
                buf += chunk.decode(errors="replace")
                while "\r\n" in buf:
                    line, buf = buf.split("\r\n", 1)
                    line = line.strip()
                    if line:
                        self._handle_line(line)
            elif self._reader.at_eof():
                break

            now = time.monotonic()

            # is_online heartbeat — tells the broker (and device) a client
            # is actively watching.  The real app sends this every 5 s;
            # without it the device stops streaming after ~30 s.
            if now - last_is_online >= _IS_ONLINE_INTERVAL:
                self._send_line(
                    f"cmd=is_online&device_id={self.device_id}"
                )
                last_is_online = now

            # Attr 84 keepalive — cmd=3 write of {84:1}, same as BLE.
            # The device sends this to the cloud every ~10 s while streaming;
            # sending it TO the device keeps the session alive.
            if now - last_attr84 >= _ATTR84_KEEPALIVE_INTERVAL:
                self._send_attr84_keepalive()
                last_attr84 = now

            # Keepalive
            if now - last_keepalive >= _KEEPALIVE_INTERVAL:
                self._send_line("cmd=keep")
                last_keepalive = now

    # ── Line handling ─────────────────────────────────────────────────────────

    def _handle_line(self, line: str) -> None:
        """Process one received protocol line."""
        kv = self._parse_kv(line)
        cmd = kv.get("cmd", "")

        if cmd == "publish":
            raw_msg = kv.get("message", "")
            try:
                payload = json.loads(raw_msg)
            except (json.JSONDecodeError, ValueError):
                return
            payload_cmd = payload.get("cmd")
            if payload_cmd in (2, 3, 10):
                data = payload.get("msg", {}).get("data", {})
                self._apply_telemetry(data)

        elif cmd == "subscribe":
            _LOGGER.debug("WiFi coordinator %s: subscribe ACK", self.device_id)

        elif cmd == "keep":
            _LOGGER.debug("WiFi coordinator %s: keep ACK", self.device_id)

        elif cmd == "pong":
            pass  # server heartbeat response

    def _apply_telemetry(self, data: dict) -> None:
        """Merge a cmd=10 telemetry data dict into accumulated state."""
        if not data:
            return

        for k, v in data.items():
            try:
                attr = int(k)
                val = int(v)
            except (ValueError, TypeError):
                continue

            if attr == 101:
                self._current_slot = val
                if val not in self._ext_batteries:
                    self._ext_batteries[val] = {}
                continue

            if attr in EXT_BATTERY_ATTRS:
                self._ext_batteries.setdefault(self._current_slot, {})[attr] = val
                if attr == 78:
                    self._ext_batteries[self._current_slot]["last_runtime_min"] = min(
                        val, self._runtime_max
                    )
            else:
                self._attrs[attr] = val
                if attr == 30:
                    self._attrs["last_runtime_min"] = min(val, self._runtime_max)

        self.last_successful_update = datetime.now()

        # Push update to HA entities
        snapshot: OUPESData = {
            "attrs": dict(self._attrs),
            "ext_batteries": {
                s: dict(sd) for s, sd in self._ext_batteries.items()
            },
        }
        self.async_set_updated_data(snapshot)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _send_attr84_keepalive(self) -> None:
        """Send cmd=3 write of attr 84=1 to the device via the broker.

        This is the WiFi equivalent of the BLE KEEPALIVE_PKT.  The device
        expects this periodically; without it the telemetry stream stops.
        """
        msg = json.dumps({
            "msg": {"attr": [84], "data": {"84": 1}},
            "pv": 0,
            "cmd": 3,
            "sn": str(int(time.time() * 1000)),
        }, separators=(",", ":"))
        self._send_line(
            f"cmd=publish"
            f"&device_id={self.device_id}"
            f"&topic=control_{self.device_id}"
            f"&device_key={self.device_key}"
            f"&message={msg}"
        )

    def _send_line(self, line: str) -> None:
        if self._writer and not self._writer.is_closing():
            try:
                self._writer.write((line + "\r\n").encode())
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug(
                    "WiFi coordinator %s: send error: %s", self.device_id, exc
                )

    def _close_writer(self) -> None:
        if self._writer and not self._writer.is_closing():
            try:
                self._writer.close()
            except Exception:  # noqa: BLE001
                pass
        self._writer = None
        self._reader = None

    @staticmethod
    def _parse_kv(line: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for part in line.strip().split("&"):
            if "=" in part:
                k, _, v = part.partition("=")
                result[k] = v
        return result
