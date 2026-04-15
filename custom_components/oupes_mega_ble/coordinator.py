"""DataUpdateCoordinator that manages the BLE connection to an OUPES Mega."""
from __future__ import annotations

import asyncio
import csv
import logging
import time as _time
from datetime import datetime, timedelta
from pathlib import Path

from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection

from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    ATTR78_RUNTIME_MAX,
    DOMAIN,
    MAX_ATTEMPTS,
    SCAN_DURATION,
    SERIES_SETTINGS,
    model_name_from_product_id,
    series_from_product_id,
)
from .protocol import (
    build_init_sequence,
    build_query_commands,
    ATTR_MAP,
    EXT_BATTERY_ATTRS,
    EXT_BATTERY_MAP,
    KEEPALIVE_FIRST_DELAY,
    KEEPALIVE_INTERVAL,
    KEEPALIVE_PKT,
    NOTIFY_CHAR_UUID,
    WRITE_CHAR_UUID,
    parse_ble_packet,
    parse_packet_sequence,
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
        product_id: str = "",
        continuous: bool = False,
        poll_interval_seconds: int = 30,
        stale_timeout_minutes: int = 15,
        debug_attrs: bool = False,
        debug_raw: bool = False,
        runtime_max_minutes: int = ATTR78_RUNTIME_MAX,
    ) -> None:
        effective_interval = timedelta(seconds=poll_interval_seconds)
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{address}",
            update_interval=effective_interval,
        )
        self.address = address
        self.device_name = name
        self.product_id = product_id
        self.model_name = model_name_from_product_id(product_id)
        self.last_successful_poll: datetime | None = None
        self.stale_timeout = timedelta(minutes=stale_timeout_minutes)
        self._pending_command: bytes | None = None
        self._init_sequence = build_init_sequence(device_key)
        self._continuous = continuous
        self._live_data: OUPESData | None = None
        self._continuous_task: asyncio.Task | None = None
        # Build Cmd2 query packets that read this device's supported settings.
        # Split into small batches (7 DPIDs each) matching the Cleanergy app.
        series = series_from_product_id(product_id)
        supported_dpids = sorted(SERIES_SETTINGS.get(series, frozenset()))
        self._settings_query_pkts: list[bytes] = (
            build_query_commands(supported_dpids) if supported_dpids else []
        )
        self._debug_attrs = debug_attrs
        self._debug_raw = debug_raw
        self._runtime_max_minutes = runtime_max_minutes
        self._attr_csv_path: Path | None = None
        self._raw_csv_path: Path | None = None
        if debug_attrs or debug_raw:
            self._init_debug_files()

    def _init_debug_files(self) -> None:
        """Create/open debug CSV files in the HA config directory."""
        safe_addr = self.address.replace(":", "")
        config_dir = Path(self.hass.config.config_dir)
        if self._debug_attrs:
            path = config_dir / f"oupes_mega_ble_{safe_addr}_attrs.csv"
            is_new = not path.exists()
            self._attr_csv_path = path
            with path.open("a", newline="", encoding="utf-8") as f:
                if is_new:
                    csv.writer(f).writerow(
                        ["timestamp", "dir", "cmd", "pkt_idx", "last",
                         "attr", "attr_hex", "value", "known",
                         "slot", "soc", "grid_w", "note"]
                    )
            _LOGGER.info("OUPES debug: attr log → %s", path)
        if self._debug_raw:
            path = config_dir / f"oupes_mega_ble_{safe_addr}_raw.csv"
            is_new = not path.exists()
            self._raw_csv_path = path
            with path.open("a", newline="", encoding="utf-8") as f:
                if is_new:
                    csv.writer(f).writerow(
                        ["timestamp", "dir", "cmd", "pkt_idx", "last", "hex"]
                    )
            _LOGGER.info("OUPES debug: raw log → %s", path)

    @staticmethod
    def _classify_packet(data: bytearray) -> tuple[str, int, bool]:
        """Return (cmd_label, pkt_index, is_last) for a BLE packet."""
        if len(data) < 3:
            return ("unknown", 0, True)
        pkt_sn = data[1]
        pkt_index = pkt_sn & 0x7F
        is_last = bool(pkt_sn & 0x80)
        if pkt_index == 0:
            echo = data[2]
            if echo == 0x02:
                return ("cmd2_resp", pkt_index, is_last)
            if echo == 0x03:
                return ("cmd3_resp", pkt_index, is_last)
            return ("cmd1", pkt_index, is_last)
        return ("continuation", pkt_index, is_last)

    def _debug_log_tx(self, pkt: bytes, label: str = "") -> None:
        """Log an outgoing (TX) packet to the raw CSV."""
        if not self._debug_raw or not self._raw_csv_path:
            return
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        cmd = label or "tx"
        try:
            with self._raw_csv_path.open("a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow([ts, "TX", cmd, "", "", pkt.hex()])
        except OSError:
            pass

    def _debug_log_packet(
        self,
        ts: str,
        raw: bytearray,
        parsed: dict[int, int],
        slot: int,
        soc: int,
        grid_w: int,
        *,
        raw_logged: bool = False,
    ) -> None:
        """Write debug rows for one received notification packet."""
        cmd, pkt_index, is_last = self._classify_packet(raw)

        if not raw_logged and self._debug_raw and self._raw_csv_path:
            try:
                with self._raw_csv_path.open("a", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow([
                        ts, "RX", cmd, pkt_index,
                        "Y" if is_last else "N", raw.hex(),
                    ])
            except OSError as exc:
                _LOGGER.debug("OUPES raw CSV write error: %s", exc)

        if self._debug_attrs and self._attr_csv_path:
            rows = []
            for attr, val in parsed.items():
                known = attr in self._KNOWN_ATTRS
                note = ""
                if attr == 78:
                    if val <= self._runtime_max_minutes:
                        note = f"attr78_runtime {val}min"
                    else:
                        note = f"attr78_capped {val}min → {self._runtime_max_minutes}min"
                elif not known:
                    note = "unknown_attr"
                rows.append([
                    ts, "RX", cmd, pkt_index,
                    "Y" if is_last else "N",
                    attr, f"0x{attr:02x}", val,
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

            # Get device object from HA's BT scanner cache (required by
            # bleak-retry-connector's establish_connection).
            ble_device = async_ble_device_from_address(
                self.hass, self.address, connectable=True
            )
            if ble_device is None:
                _LOGGER.debug(
                    "BLE scanner hasn't seen %s recently — skipping "
                    "attempt %d/%d",
                    self.address, attempt + 1, MAX_ATTEMPTS,
                )
                last_exc = UpdateFailed(
                    f"Device {self.address} not seen by BLE scanner"
                )
                continue

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
                name=f"oupes_mega_ble_continuous_{self.address}",
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
        attrs: dict[int, int] = {105: 1}  # Charge Mode default = Fast (factory default)
        ext_batteries: dict[int, dict[int, int]] = {1: {}}  # pre-seed slot 1 to match current_slot default
        current_slot = 1
        disconnected_event = asyncio.Event()

        def on_disconnect(_client: BleakClient) -> None:
            _LOGGER.debug("Continuous connection: %s disconnected", self.address)
            disconnected_event.set()

        c_rolling_soc: list[int] = [-1]
        c_rolling_grid: list[int] = [0]
        c_pkt_buf: list[bytearray] = []

        def _apply_parsed(parsed: dict[int, int]) -> None:
            """Merge parsed attribute values into running data dicts."""
            nonlocal current_slot
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
                    if attr == 78:
                        ext_batteries[current_slot]["last_runtime_min"] = min(val, self._runtime_max_minutes)
                elif attr != 101:
                    attrs[attr] = val
                    if attr == 30:
                        attrs["last_runtime_min"] = min(val, self._runtime_max_minutes)

        def _flush_pkt_buf() -> None:
            """Parse buffered packet sequence and apply results."""
            if not c_pkt_buf:
                return
            parsed = parse_packet_sequence(c_pkt_buf)
            if parsed:
                _apply_parsed(parsed)
                if self._debug_attrs:
                    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                    self._debug_log_packet(
                        ts, c_pkt_buf[0], parsed,
                        current_slot, c_rolling_soc[0], c_rolling_grid[0],
                        raw_logged=True,
                    )
            c_pkt_buf.clear()

        def notification_handler(_sender: int, raw: bytearray) -> None:
            if len(raw) < 2:
                return
            pkt_sn = raw[1]
            pkt_index = pkt_sn & 0x7F
            is_last = bool(pkt_sn & 0x80)

            # Log raw packet immediately (before buffering)
            if self._debug_raw and self._raw_csv_path:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                cmd, pi, il = self._classify_packet(raw)
                try:
                    with self._raw_csv_path.open(
                        "a", newline="", encoding="utf-8"
                    ) as f:
                        csv.writer(f).writerow([
                            ts, "RX", cmd, pi,
                            "Y" if il else "N", raw.hex(),
                        ])
                except OSError:
                    pass

            # Multi-packet reassembly: new idx-0 flushes any prior buffer
            if pkt_index == 0:
                _flush_pkt_buf()
            c_pkt_buf.append(bytearray(raw))
            if is_last:
                _flush_pkt_buf()

        ble_device = async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if ble_device is None:
            raise BleakError(
                f"Device {self.address} not seen by BLE scanner"
            )

        client = await establish_connection(
            client_class=BleakClient,
            device=ble_device,
            name=self.address,
            disconnected_callback=on_disconnect,
            max_attempts=2,
        )
        try:
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
                    self._debug_log_tx(cmd, "queued_cmd")
                    await asyncio.sleep(0.1)
                except BleakError as exc:
                    _LOGGER.warning(
                        "Queued command error (continuous) on %s: %s",
                        self.address, exc,
                    )

            # ── Send settings query (Cmd2) to read current setting values ──
            for qpkt in self._settings_query_pkts:
                if disconnected_event.is_set():
                    break
                try:
                    await client.write_gatt_char(
                        WRITE_CHAR_UUID, qpkt, response=False
                    )
                    self._debug_log_tx(qpkt, "cmd2_query")
                    await asyncio.sleep(0.15)
                except BleakError as exc:
                    _LOGGER.debug(
                        "Settings query error (continuous) on %s: %s",
                        self.address, exc,
                    )
                    break

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
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

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
        attrs: dict[int, int] = {105: 1}  # Charge Mode default = Fast (factory default)
        ext_batteries: dict[int, dict[int, int]] = {1: {}}  # pre-seed slot 1 to match current_slot default
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
        o_pkt_buf: list[bytearray] = []

        def _apply_parsed(parsed: dict[int, int]) -> None:
            """Merge parsed attribute values into running data dicts."""
            nonlocal got_data, current_slot
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
                    if attr == 78:
                        ext_batteries[current_slot]["last_runtime_min"] = min(val, self._runtime_max_minutes)
                elif attr != 101:
                    attrs[attr] = val
                    if attr == 30:
                        attrs["last_runtime_min"] = min(val, self._runtime_max_minutes)

        def _flush_pkt_buf() -> None:
            """Parse buffered packet sequence and apply results."""
            if not o_pkt_buf:
                return
            parsed = parse_packet_sequence(o_pkt_buf)
            if parsed:
                _apply_parsed(parsed)
                if self._debug_attrs:
                    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                    self._debug_log_packet(
                        ts, o_pkt_buf[0], parsed,
                        current_slot, rolling_soc[0], rolling_grid[0],
                        raw_logged=True,
                    )
            o_pkt_buf.clear()

        def notification_handler(_sender: int, raw: bytearray) -> None:
            if len(raw) < 2:
                return
            pkt_sn = raw[1]
            pkt_index = pkt_sn & 0x7F
            is_last = bool(pkt_sn & 0x80)

            # Log raw packet immediately (before buffering)
            if self._debug_raw and self._raw_csv_path:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                cmd, pi, il = self._classify_packet(raw)
                try:
                    with self._raw_csv_path.open(
                        "a", newline="", encoding="utf-8"
                    ) as f:
                        csv.writer(f).writerow([
                            ts, "RX", cmd, pi,
                            "Y" if il else "N", raw.hex(),
                        ])
                except OSError:
                    pass

            # Multi-packet reassembly: new idx-0 flushes any prior buffer
            if pkt_index == 0:
                _flush_pkt_buf()
            o_pkt_buf.append(bytearray(raw))
            if is_last:
                _flush_pkt_buf()

        try:
            client = await establish_connection(
                client_class=BleakClient,
                device=ble_device,
                name=self.address,
                disconnected_callback=on_disconnect,
                max_attempts=2,
            )
        except asyncio.CancelledError:
            raise  # let HA handle task cancellation
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("BLE error on %s: %s", self.address, exc)
            raise UpdateFailed(f"BLE connection failed for {self.address}: {exc}") from exc

        try:
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
                    self._debug_log_tx(cmd, "queued_cmd")
                    await asyncio.sleep(0.1)
                except BleakError as exc:
                    _LOGGER.warning(
                        "Failed to send queued command to %s: %s",
                        self.address, exc,
                    )

            # ── Step 3c: send settings query (Cmd2) ─────────────────────
            for qpkt in self._settings_query_pkts:
                if disconnected_event.is_set():
                    break
                try:
                    await client.write_gatt_char(
                        WRITE_CHAR_UUID, qpkt, response=False
                    )
                    self._debug_log_tx(qpkt, "cmd2_query")
                    await asyncio.sleep(0.15)
                except BleakError as exc:
                    _LOGGER.debug(
                        "Settings query error on %s: %s", self.address, exc,
                    )
                    break

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
        except asyncio.CancelledError:
            _LOGGER.debug("BLE connection to %s was cancelled", self.address)
            raise
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

        return False, data
