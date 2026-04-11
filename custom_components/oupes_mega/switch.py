"""Switch entities for the OUPES power station.

Two kinds of switch live here:
  1. **Output switches** — toggle physical outputs (AC, DC 12V, USB) via the
     attr-1 bitmask and ``build_output_command()``.
  2. **Setting switches** — toggle boolean device settings (ECO mode, silent,
     breath light) via individual DPIDs and ``build_setting_command()``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, series_from_product_id, SERIES_SETTINGS
from .coordinator import OUPESMegaCoordinator
from .protocol import (
    OUTPUT_AC_BIT,
    OUTPUT_DC12V_BIT,
    OUTPUT_USB_BIT,
    build_output_command,
    build_setting_command,
)
from .sensor import _device_info

# Attr 1 holds the live output-enable bitmask (bit0=AC, bit1=DC12V, bit2=USB).
_ATTR_OUTPUT_BITMASK = 1
_ATTR_GRID_INPUT_POWER = 22
_DPID_CHARGE_MODE = 105


@dataclass(frozen=True, kw_only=True)
class OUPESSwitchDescription(SwitchEntityDescription):
    """Extends SwitchEntityDescription with the output-bitmask bit to control."""

    bit: int = 0


SWITCH_DESCRIPTIONS: tuple[OUPESSwitchDescription, ...] = (
    OUPESSwitchDescription(
        key="ac_output_switch",
        bit=OUTPUT_AC_BIT,
        name="AC Output",
        icon="mdi:power-socket",
    ),
    OUPESSwitchDescription(
        key="dc12v_output_switch",
        bit=OUTPUT_DC12V_BIT,
        name="DC 12V Output",
        icon="mdi:car-electric",
    ),
    OUPESSwitchDescription(
        key="usb_output_switch",
        bit=OUTPUT_USB_BIT,
        name="USB Output",
        icon="mdi:usb",
    ),
)


class OUPESMegaSwitch(CoordinatorEntity[OUPESMegaCoordinator], SwitchEntity):
    """A writable switch that controls one output on the OUPES Mega.

    Reads live state from attr 1 (bitmask).  When toggled, computes the new
    bitmask, queues the BLE write command on the coordinator, applies an
    optimistic state update immediately, then requests a coordinator refresh
    so the device's confirmed response is reflected without waiting for the
    normal polling interval.
    """

    entity_description: OUPESSwitchDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OUPESMegaCoordinator,
        description: OUPESSwitchDescription,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = _device_info(coordinator)

    # ── Availability ──────────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        last = self.coordinator.last_successful_poll
        if last is None:
            return False
        return datetime.now() - last <= self.coordinator.stale_timeout

    # ── State ─────────────────────────────────────────────────────────────────

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        raw = self.coordinator.data["attrs"].get(_ATTR_OUTPUT_BITMASK)
        if raw is None:
            return None
        return bool(raw & self.entity_description.bit)

    # ── Control ───────────────────────────────────────────────────────────────

    def _current_bitmask(self) -> int:
        if self.coordinator.data is None:
            return 0
        return self.coordinator.data["attrs"].get(_ATTR_OUTPUT_BITMASK, 0)

    async def async_turn_on(self, **kwargs) -> None:
        new_bitmask = self._current_bitmask() | self.entity_description.bit
        self._apply_and_send(new_bitmask)

    async def async_turn_off(self, **kwargs) -> None:
        new_bitmask = self._current_bitmask() & ~self.entity_description.bit
        self._apply_and_send(new_bitmask)

    def _apply_and_send(self, new_bitmask: int) -> None:
        """Apply optimistic state update and queue the BLE write command."""
        # Optimistic update — makes the UI feel instant.
        if self.coordinator.data is not None:
            self.coordinator.data["attrs"][_ATTR_OUTPUT_BITMASK] = new_bitmask & 0xFF
        self.async_write_ha_state()

        # Queue command; the coordinator will send it on the next connection.
        self.coordinator.queue_command(build_output_command(new_bitmask))
        self.hass.async_create_task(self.coordinator.async_request_refresh())


# ── Setting switches (Cmd3 DPID boolean toggles) ─────────────────────────────

@dataclass(frozen=True, kw_only=True)
class OUPESSettingSwitchDescription(SwitchEntityDescription):
    """Extends SwitchEntityDescription with the BLE DPID to write."""

    dpid: int = 0


SETTING_SWITCH_DESCRIPTIONS: tuple[OUPESSettingSwitchDescription, ...] = (
    OUPESSettingSwitchDescription(
        key="silent_mode",
        dpid=63,
        name="Silent Mode",
        icon="mdi:volume-off",
    ),
    OUPESSettingSwitchDescription(
        key="ac_eco_mode",
        dpid=110,
        name="AC ECO Mode",
        icon="mdi:leaf",
    ),
    OUPESSettingSwitchDescription(
        key="dc_eco_mode",
        dpid=112,
        name="DC ECO Mode",
        icon="mdi:leaf",
    ),
    OUPESSettingSwitchDescription(
        key="breath_light",
        dpid=58,
        name="Breath Light",
        icon="mdi:lightbulb-outline",
    ),
    OUPESSettingSwitchDescription(
        key="charge_mode",
        dpid=105,
        name="Fast Charge",
        icon="mdi:lightning-bolt",
    ),
)


class OUPESSettingSwitch(CoordinatorEntity[OUPESMegaCoordinator], SwitchEntity):
    """A boolean device setting controlled via Cmd3 DPID write."""

    entity_description: OUPESSettingSwitchDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OUPESMegaCoordinator,
        description: OUPESSettingSwitchDescription,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = _device_info(coordinator)
        self._optimistic_state: bool | None = None

    @property
    def available(self) -> bool:
        last = self.coordinator.last_successful_poll
        if last is None:
            return False
        return datetime.now() - last <= self.coordinator.stale_timeout

    @property
    def is_on(self) -> bool | None:
        if self._optimistic_state is not None:
            return self._optimistic_state
        if self.coordinator.data is None:
            return None
        raw = self.coordinator.data["attrs"].get(self.entity_description.dpid)
        if raw is None:
            return None
        return bool(raw)

    async def async_turn_on(self, **kwargs) -> None:
        self._check_charge_mode_preconditions()
        self._optimistic_state = True
        self.async_write_ha_state()
        self.coordinator.queue_command(
            build_setting_command(self.entity_description.dpid, 1)
        )
        self.hass.async_create_task(self.coordinator.async_request_refresh())

    async def async_turn_off(self, **kwargs) -> None:
        self._check_charge_mode_preconditions()
        self._optimistic_state = False
        self.async_write_ha_state()
        self.coordinator.queue_command(
            build_setting_command(self.entity_description.dpid, 0)
        )
        self.hass.async_create_task(self.coordinator.async_request_refresh())

    def _check_charge_mode_preconditions(self) -> None:
        """Block charge-mode toggle when AC output is on or AC input is active."""
        if self.entity_description.dpid != _DPID_CHARGE_MODE:
            return
        attrs = (self.coordinator.data or {}).get("attrs", {})
        if attrs.get(_ATTR_OUTPUT_BITMASK, 0) & OUTPUT_AC_BIT:
            raise HomeAssistantError(
                "Turn off AC output before switching charge mode"
            )
        if attrs.get(_ATTR_GRID_INPUT_POWER, 0) > 0:
            raise HomeAssistantError(
                "Disconnect AC charging input before switching charge mode"
            )

    def _handle_coordinator_update(self) -> None:
        """Clear the optimistic state once the coordinator has fresh data."""
        self._optimistic_state = None
        super()._handle_coordinator_update()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OUPES Mega switch entities (output + setting switches)."""
    coordinator: OUPESMegaCoordinator = hass.data[DOMAIN][entry.entry_id]

    # Output switches are always available on all models.
    entities: list[SwitchEntity] = [
        OUPESMegaSwitch(coordinator, desc, entry)
        for desc in SWITCH_DESCRIPTIONS
    ]

    # Setting switches are filtered by the device's model series.
    series = series_from_product_id(coordinator.product_id)
    supported_dpids = SERIES_SETTINGS.get(series, SERIES_SETTINGS["unknown"])
    entities.extend(
        OUPESSettingSwitch(coordinator, desc, entry)
        for desc in SETTING_SWITCH_DESCRIPTIONS
        if desc.dpid in supported_dpids
    )

    async_add_entities(entities)
