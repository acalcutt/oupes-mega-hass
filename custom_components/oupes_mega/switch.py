"""Switch entities for the controllable outputs on the OUPES Mega."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, STALE_TIMEOUT
from .coordinator import OUPESMegaCoordinator
from .protocol import (
    OUTPUT_AC_BIT,
    OUTPUT_DC12V_BIT,
    OUTPUT_USB_BIT,
    build_output_command,
)

# Attr 1 holds the live output-enable bitmask (bit0=AC, bit1=DC12V, bit2=USB).
_ATTR_OUTPUT_BITMASK = 1


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
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            name=coordinator.device_name,
            manufacturer="OUPES",
            model="Mega 1",
        )

    # ── Availability ──────────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        last = self.coordinator.last_successful_poll
        if last is None:
            return False
        return datetime.now() - last <= STALE_TIMEOUT

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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OUPES Mega switch entities."""
    coordinator: OUPESMegaCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        OUPESMegaSwitch(coordinator, desc, entry)
        for desc in SWITCH_DESCRIPTIONS
    )
