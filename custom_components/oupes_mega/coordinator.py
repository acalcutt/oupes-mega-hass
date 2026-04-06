"""DataUpdateCoordinator that manages the BLE connection to an OUPES Mega."""
from __future__ import annotations

import asyncio
import csv
import logging
import time as _time
from datetime import datetime
from pathlib import Path

from bleak import BleakClient
from bleak.exc import BleakError

from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    ATTR78_MV_MAX,
    ATTR78_MV_MIN,
    ATTR78_RUNTIME_MAX,
    DOMAIN,
    MAX_ATTEMPTS,
    SCAN_DURATION,
    STALE_TIMEOUT,
    UPDATE_INTERVAL,
)
from .protocol import (
    build_init_sequence,
    ATTR_MAP,
    EXT_BATTERY_ATTRS,
    EXT_BATTERY_MAP,
    KEEPALIVE_FIRST_DELAY,
    KEEPALIVE_INTERVAL,
    KEEPALIVE_PKT,
    NOTIFY_CHAR_UUID,
    WRITE_CHAR_UUID,
    parse_ble_packet,
    build_output_command,  # noqa: F401 – re-exported for switch.py
)

_LOGGER = logging.getLogger(__name__)

# Type alias for the data dict stored in coordinator.data
# {"attrs": dict[int,int], "ext_batteries": dict[int,dict[int,int]]}
OUPESData = dict


class OUPESMegaCoordinator(DataUpdateCoordinator):
    """Polls one OUPES Mega device via BLE on a fixed interval.

    Each poll: connect → 1.8s GATT delay → subscribe → init sequence →
    collect notifications for SCAN_DURATION seconds (with keepalives) →
    disconnect → store data.

    Cold-probe drops (device disconnects in <2 s with no data) are retried
    up to MAX_ATTEMPTS times — this matches normal Cleanergy app behaviour.
    """

    # Attrs recognised by the protocol (used to detect unknown attrs in debug mode)
    _KNOWN_ATTRS: frozenset[int] = frozenset(ATTR_MAP) | frozenset(EXT_BATTERY_MAP) | {101}

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        name: str,
        *,
        device_key: str = "bd236b1695",
        continuous: bool = False,
        debug_attrs: bool = False,
        debug_raw: bool = False,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{address}",
            update_interval=UPDATE_INTERVAL,
        )
        self.address = address
        self.device_name = name
        self.last_successful_poll: datetime | None = None
        self._pending_command: bytes | None = None
        self._init_sequence = build_init_sequence(device_key)
        self._continuous = continuous
        self._live_data: OUPESData | None = None
        self._continuous_task: asyncio.Task | None = None
        self._debug_attrs = debug_attrs
        self._debug_raw = debug_raw
        self._attr_csv_path: Path | None = None
        self._raw_csv_path: Path | None = None
        if debug_attrs or debug_raw:
            self._init_debug_files()

    def _init_debug_files(self) -> None:
        """Create/open debug CSV files in the HA config directory."""
        safe_addr = self.address.replace(":", "")
        config_dir = Path(self.hass.config.config_dir)
        if self._debug_attrs:
            path = config_dir / f"oupes_mega_{safe_addr}_attrs.csv"
            is_new = not path.exists()
            self._attr_csv_path = path
            with path.open("a", newline="", encoding="utf-8") as f:
                if is_new:
                    csv.writer(f).writerow(
                        ["timestamp", "attr", "attr_hex", "value", "known",
                         "slot", "soc", "grid_w", "note"]
                    )
            _LOGGER.info("OUPES debug: attr log → %s", path)
        if self._debug_raw:
            path = config_dir / f"oupes_mega_{safe_addr}_raw.csv"
            is_new = not path.exists()
            self._raw_csv_path = path
            with path.open("a", newline="", encoding="utf-8") as f:
                if is_new:
                    csv.writer(f).writerow(["timestamp", "hex"])
            _LOGGER.info("OUPES debug: raw log → %s", path)

    def _debug_log_packet(
        self,
        ts: str,
        raw: bytearray,
        parsed: dict[int, int],
        slot: int,
        soc: int,
        grid_w: int,
    ) -> None:
        """Write debug rows for one notification packet."""
        if self._debug_raw and self._raw_csv_path:
            try:
                with self._raw_csv_path.open("a", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow([ts, raw.hex()])
            except OSError as exc:
                _LOGGER.debug("OUPES raw CSV write error: %s", exc)

        if self._debug_attrs and self._attr_csv_path:
            rows = []
            for attr, val in parsed.items():
                known = attr in self._KNOWN_ATTRS
                note = ""
                # Flag attr-78 middle-range mystery values
                if attr == 78 and ATTR78_RUNTIME_MAX < val < ATTR78_MV_MIN:
                    note = f"attr78_mystery (not runtime, not voltage)"
                    _LOGGER.warning(
                        "OUPES attr 78 mystery value: slot=%d val=%d (not runtime ≤%d, "
                        "not voltage %d–%d) SoC=%d%% grid=%dW",
                        slot, val, ATTR78_RUNTIME_MAX,
                        ATTR78_MV_MIN, ATTR78_MV_MAX, soc, grid_w,
                    )
                elif attr == 78 and ATTR78_MV_MIN <= val <= ATTR78_MV_MAX:
                    note = f"attr78_voltage {val/1000:.3f}V"
                elif attr == 78:
                    note = f"attr78_runtime {val}min"
                elif not known:
                    note = "unknown_attr"
                rows.append([
                    ts, attr, f"0x{attr:02x}", val,
                    "yes" if known else "NO",
                    slot if attr in EXT_BATTERY_ATTRS else "",
                    soc if soc >= 0 else "",
                    grid_w,
                    note,
                ])
            if rows:
                try:
                    with self._attr_csv_path.open("a", newline="", encoding="utf-8") as f:
                        csv.writer(f).writerows(rows)
                except OSError as exc:
                    _LOGGER.debug("OUPES attr CSV write error: %s", exc)

    # ── Public coordinator interface ──────────────────────────────────────────

    async def _async_update_data(self) -> OUPESData:
        """Called by the coordinator on each update interval."""
        # In continuous mode the background task keeps data fresh via
        # async_set_updated_data; just return the cached snapshot quickly.
        if self._continuous and self._live_data is not None:
            return self._live_data

        last_exc: Exception | None = None

        for attempt in range(MAX_ATTEMPTS):
            if attempt > 0:
                wait = 3 if attempt < 2 else 6
                _LOGGER.debug(
                    "Retry attempt %d/%d for %s (waiting %ds)",
                    attempt + 1, MAX_ATTEMPTS, self.address, wait,
                )
                await asyncio.sleep(wait)

            # Prefer a device object from HA's BT scanner cache (gives the
            # right adapter hint), but fall back to the raw MAC string so a
            # single missed advertisement window doesn't abort all retries.
            ble_device = async_ble_device_from_address(
                self.hass, self.address, connectable=True
            )
            if ble_device is None:
                _LOGGER.debug(
                    "BLE scanner hasn't seen %s recently — attempting direct "
                    "connect by MAC address (attempt %d/%d)",
                    self.address, attempt + 1, MAX_ATTEMPTS,
                )
                ble_device = self.address  # bleak accepts a raw MAC string

            try:
                dropped_quickly, data = await self._connect_once(ble_device)
            except UpdateFailed as exc:
                last_exc = exc
                _LOGGER.debug(
                    "Connection error on %s (attempt %d): %s",
                    self.address, attempt + 1, exc,
                )
                continue

            if not dropped_quickly:
                if not data["attrs"] and not any(data["ext_batteries"].values()):
                    last_exc = UpdateFailed(
                        "Connected successfully but received no telemetry data"
                    )
                    continue  # retry rather than immediately failing
                self.last_successful_poll = datetime.now()
                return data

            _LOGGER.debug(
                "Cold-probe drop on %s (attempt %d) — will retry",
                self.address, attempt + 1,
            )

        raise last_exc or UpdateFailed(
            f"Device {self.address} failed to provide data after "
            f"{MAX_ATTEMPTS} attempts"
        )

    def queue_command(self, command: bytes) -> None:
        """Schedule a write command to be sent on the next BLE connection.

        Call async_request_refresh() immediately after to ensure the command
        is dispatched without waiting for the normal polling interval.
        """
        self._pending_command = command

    # ── Continuous connection management ──────────────────────────────────────

    def start_continuous_connection(self) -> None:
        """Start the persistent BLE connection background task."""
        if self._continuous_task is None or self._continuous_task.done():
            self._continuous_task = self.hass.async_create_background_task(
                self._run_continuous_connection(),
                name=f"oupes_mega_continuous_{self.address}",
            )
            _LOGGER.debug("Started continuous BLE task for %s", self.address)

    def stop_continuous_connection(self) -> None:
        """Cancel the persistent BLE connection background task (called on unload)."""
        if self._continuous_task and not self._continuous_task.done():
            self._continuous_task.cancel()
            _LOGGER.debug("Cancelled continuous BLE task for %s", self.address)
        self._continuous_task = None

    async def _run_continuous_connection(self) -> None:
        """Loop: connect → run indefinitely → reconnect after disconnect/error."""
        _RECONNECT_DELAY = 30
        while True:
            try:
                await self._connect_continuous()
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning(
                    "Continuous BLE error for %s: %s — reconnecting in %ds",
                    self.address, exc, _RECONNECT_DELAY,
                )
            await asyncio.sleep(_RECONNECT_DELAY)

    async def _connect_continuous(self) -> None:
        """Hold an open BLE connection, pushing data to HA after each keepalive."""
        # Pre-seed attrs that the firmware only sends sporadically so their
        # entities hold a stable value instead of oscillating to Unknown.
        attrs: dict[int, int] = {105: 0}  # AC Inverter Protection default = off
        ext_batteries: dict[int, dict[int, int]] = {}
        current_slot = 1
        disconnected_event = asyncio.Event()

        def on_disconnect(_client: BleakClient) -> None:
            _LOGGER.debug("Continuous connection: %s disconnected", self.address)
            disconnected_event.set()

        c_rolling_soc: list[int] = [-1]
        c_rolling_grid: list[int] = [0]

        def notification_handler(_sender: int, raw: bytearray) -> None:
            nonlocal current_slot
            parsed = parse_ble_packet(raw)
            if not parsed:
                return
            if 101 in parsed:
                slot = parsed[101]
                current_slot = slot
                if slot not in ext_batteries:
                    ext_batteries[slot] = {}
            if 3 in parsed:
                c_rolling_soc[0] = parsed[3]
            if 22 in parsed:
                c_rolling_grid[0] = parsed[22]
            for attr, val in parsed.items():
                if attr in EXT_BATTERY_ATTRS:
                    ext_batteries[current_slot][attr] = val
                    if attr == 78 and ATTR78_MV_MIN <= val <= ATTR78_MV_MAX:
                        ext_batteries[current_slot]["last_voltage_mv"] = val
                elif attr != 101:
                    attrs[attr] = val
            if self._debug_attrs or self._debug_raw:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                self._debug_log_packet(
                    ts, raw, parsed,
                    current_slot, c_rolling_soc[0], c_rolling_grid[0],
                )

        ble_device = (
            async_ble_device_from_address(self.hass, self.address, connectable=True)
            or self.address
        )

        try:
            async with BleakClient(
                ble_device,
                timeout=15.0,
                disconnected_callback=on_disconnect,
            ) as client:
                await asyncio.sleep(1.8)
                if disconnected_event.is_set():
                    return

                await client.start_notify(NOTIFY_CHAR_UUID, notification_handler)
                await asyncio.sleep(0.2)
                if disconnected_event.is_set():
                    return

                for i, pkt in enumerate(self._init_sequence):
                    if disconnected_event.is_set():
                        return
                    try:
                        await client.write_gatt_char(
                            WRITE_CHAR_UUID, pkt, response=False
                        )
                        await asyncio.sleep(0.01)
                    except BleakError as exc:
                        _LOGGER.warning(
                            "Init packet %d error (continuous) on %s: %s",
                            i, self.address, exc,
                        )

                if self._pending_command is not None:
                    cmd = self._pending_command
                    self._pending_command = None
                    try:
                        await client.write_gatt_char(
                            WRITE_CHAR_UUID, cmd, response=False
                        )
                        await asyncio.sleep(0.1)
                    except BleakError as exc:
                        _LOGGER.warning(
                            "Queued command error (continuous) on %s: %s",
                            self.address, exc,
                        )

                # Keepalive + data-push loop — runs until disconnected or cancelled
                await asyncio.sleep(KEEPALIVE_FIRST_DELAY)
                while not disconnected_event.is_set():
                    if attrs or any(ext_batteries.values()):
                        data: OUPESData = {
                            "attrs": dict(attrs),
                            "ext_batteries": {
                                s: dict(sd) for s, sd in ext_batteries.items()
                            },
                        }
                        self._live_data = data
                        self.last_successful_poll = datetime.now()
                        self.async_set_updated_data(data)

                    try:
                        await client.write_gatt_char(
                            WRITE_CHAR_UUID, KEEPALIVE_PKT, response=False
                        )
                    except BleakError:
                        break

                    if self._pending_command is not None:
                        cmd = self._pending_command
                        self._pending_command = None
                        try:
                            await client.write_gatt_char(
                                WRITE_CHAR_UUID, cmd, response=False
                            )
                        except BleakError as exc:
                            _LOGGER.warning(
                                "Command error (continuous) on %s: %s",
                                self.address, exc,
                            )

                    try:
                        await asyncio.wait_for(
                            disconnected_event.wait(), timeout=KEEPALIVE_INTERVAL
                        )
                    except asyncio.TimeoutError:
                        pass

        except BleakError as exc:
            _LOGGER.debug(
                "BLE error in continuous connection for %s: %s", self.address, exc
            )
            raise

    # ── Internal BLE connection logic ─────────────────────────────────────────

    async def _connect_once(self, ble_device) -> tuple[bool, OUPESData]:
        """Single BLE connection attempt.

        Returns:
            (dropped_quickly, data)
            dropped_quickly — True if device disconnected in <2 s with no data
                              (caller should retry); False otherwise.
            data            — dict with keys 'attrs' and 'ext_batteries'.
        """
        # Pre-seed attrs that the firmware only sends sporadically so their
        # entities hold a stable value instead of oscillating to Unknown.
        attrs: dict[int, int] = {105: 0}  # AC Inverter Protection default = off
        ext_batteries: dict[int, dict[int, int]] = {}
        current_slot = 1
        got_data = False
        disconnected_event = asyncio.Event()
        connect_ts = _time.monotonic()
        data: OUPESData = {"attrs": attrs, "ext_batteries": ext_batteries}

        def on_disconnect(_client: BleakClient) -> None:
            uptime = _time.monotonic() - connect_ts
            _LOGGER.debug("Device %s disconnected after %.1fs", self.address, uptime)
            disconnected_event.set()

        rolling_soc: list[int] = [-1]
        rolling_grid: list[int] = [0]

        def notification_handler(_sender: int, raw: bytearray) -> None:
            nonlocal got_data, current_slot
            parsed = parse_ble_packet(raw)
            if not parsed:
                return
            got_data = True
            if 101 in parsed:
                slot = parsed[101]
                current_slot = slot
                if slot not in ext_batteries:
                    ext_batteries[slot] = {}
            if 3 in parsed:
                rolling_soc[0] = parsed[3]
            if 22 in parsed:
                rolling_grid[0] = parsed[22]
            for attr, val in parsed.items():
                if attr in EXT_BATTERY_ATTRS:
                    ext_batteries[current_slot][attr] = val
                    if attr == 78 and ATTR78_MV_MIN <= val <= ATTR78_MV_MAX:
                        ext_batteries[current_slot]["last_voltage_mv"] = val
                elif attr != 101:
                    attrs[attr] = val
            if self._debug_attrs or self._debug_raw:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                self._debug_log_packet(
                    ts, raw, parsed,
                    current_slot, rolling_soc[0], rolling_grid[0],
                )

        try:
            async with BleakClient(
                ble_device,
                timeout=15.0,
                disconnected_callback=on_disconnect,
            ) as client:

                # ── Step 1: wait for GATT discovery (match Android timing) ───
                await asyncio.sleep(1.8)
                if disconnected_event.is_set():
                    uptime = _time.monotonic() - connect_ts
                    return (uptime < 2.0 and not got_data), data

                # ── Step 2: subscribe to notifications ───────────────────────
                await client.start_notify(NOTIFY_CHAR_UUID, notification_handler)
                await asyncio.sleep(0.2)
                if disconnected_event.is_set():
                    return True, data

                # ── Step 3: send 11-packet init sequence ─────────────────────
                for i, pkt in enumerate(self._init_sequence):
                    if disconnected_event.is_set():
                        uptime = _time.monotonic() - connect_ts
                        return (uptime < 2.0 and not got_data), data
                    try:
                        await client.write_gatt_char(
                            WRITE_CHAR_UUID, pkt, response=False
                        )
                        await asyncio.sleep(0.01)
                    except BleakError as exc:
                        _LOGGER.warning(
                            "Init packet %d error on %s: %s", i, self.address, exc
                        )

                # ── Step 3b: send any queued command ─────────────────────────
                if self._pending_command is not None:
                    cmd = self._pending_command
                    self._pending_command = None
                    try:
                        await client.write_gatt_char(
                            WRITE_CHAR_UUID, cmd, response=False
                        )
                        await asyncio.sleep(0.1)
                    except BleakError as exc:
                        _LOGGER.warning(
                            "Failed to send queued command to %s: %s",
                            self.address, exc,
                        )

                # ── Step 4: keepalive loop + collect notifications ────────────
                async def keepalive_loop() -> None:
                    await asyncio.sleep(KEEPALIVE_FIRST_DELAY)
                    while not disconnected_event.is_set():
                        try:
                            await client.write_gatt_char(
                                WRITE_CHAR_UUID, KEEPALIVE_PKT, response=False
                            )
                        except BleakError:
                            break
                        await asyncio.sleep(KEEPALIVE_INTERVAL)

                keepalive_task = asyncio.create_task(keepalive_loop())
                try:
                    await asyncio.wait_for(
                        disconnected_event.wait(), timeout=SCAN_DURATION
                    )
                except asyncio.TimeoutError:
                    pass  # normal — full duration elapsed
                finally:
                    keepalive_task.cancel()
                    try:
                        await keepalive_task
                    except (asyncio.CancelledError, BleakError):
                        pass

                try:
                    await client.stop_notify(NOTIFY_CHAR_UUID)
                except BleakError:
                    pass

        except BleakError as exc:
            _LOGGER.debug("BLE error on %s: %s", self.address, exc)
            raise UpdateFailed(f"BLE connection failed for {self.address}: {exc}") from exc

        return False, data
