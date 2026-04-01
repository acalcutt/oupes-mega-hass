"""DataUpdateCoordinator that manages the BLE connection to an OUPES Mega."""
from __future__ import annotations

import asyncio
import logging
import time as _time
from datetime import datetime

from bleak import BleakClient
from bleak.exc import BleakError

from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    MAX_ATTEMPTS,
    SCAN_DURATION,
    STALE_TIMEOUT,
    UPDATE_INTERVAL,
)
from .protocol import (
    APP_INIT_SEQUENCE,
    EXT_BATTERY_ATTRS,
    KEEPALIVE_FIRST_DELAY,
    KEEPALIVE_INTERVAL,
    KEEPALIVE_PKT,
    NOTIFY_CHAR_UUID,
    WRITE_CHAR_UUID,
    parse_ble_packet,
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

    def __init__(self, hass: HomeAssistant, address: str, name: str) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{address}",
            update_interval=UPDATE_INTERVAL,
        )
        self.address = address
        self.device_name = name
        self.last_successful_poll: datetime | None = None

    # ── Public coordinator interface ──────────────────────────────────────────

    async def _async_update_data(self) -> OUPESData:
        """Called by the coordinator on each update interval."""
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

    # ── Internal BLE connection logic ─────────────────────────────────────────

    async def _connect_once(self, ble_device) -> tuple[bool, OUPESData]:
        """Single BLE connection attempt.

        Returns:
            (dropped_quickly, data)
            dropped_quickly — True if device disconnected in <2 s with no data
                              (caller should retry); False otherwise.
            data            — dict with keys 'attrs' and 'ext_batteries'.
        """
        attrs: dict[int, int] = {}
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
            for attr, val in parsed.items():
                if attr in EXT_BATTERY_ATTRS:
                    ext_batteries[current_slot][attr] = val
                elif attr != 101:
                    attrs[attr] = val

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
                for i, pkt in enumerate(APP_INIT_SEQUENCE):
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
