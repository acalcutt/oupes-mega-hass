"""Switch entities for the OUPES Mega WiFi integration.

Output switches toggle physical outputs via the attr-1 bitmask.
Commands are sent through the TCP broker using the same cmd=3 protocol
the real Android app uses.

Note: Setting switches (Silent Mode, Breath Light, Fast Charge, etc.) are
not included because the device firmware does not echo setting DPIDs over
WiFi, making their state unreadable.  Use the BLE integration instead.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, series_from_product_id
from .coordinator import OUPESWiFiCoordinator
from .sensor import _device_info

_ATTR_OUTPUT_BITMASK = 1

OUTPUT_AC_BIT = 0x01
OUTPUT_DC12V_BIT = 0x02
OUTPUT_USB_BIT = 0x04


@dataclass(frozen=True, kw_only=True)
class OUPESSwitchDescription(SwitchEntityDescription):
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
        name="Car Port",
        icon="mdi:car-electric",
    ),
    OUPESSwitchDescription(
        key="usb_output_switch",
        bit=OUTPUT_USB_BIT,
        name="USB Output",
        icon="mdi:usb",
    ),
)

_DC_OUTPUT_NAMES: dict[str, str] = {
    "mega_1":   "Car Port",
    "mega":     "Car & 12V Output",
    "guardian": "Car & 12V Output",
}

_USB_OUTPUT_NAMES: dict[str, tuple[str, str]] = {
    "mega_1":   ("USB Output",            "mdi:usb"),
    "mega":     ("Anderson & USB Output", "mdi:power-plug"),
    "guardian": ("XT90 Output",           "mdi:ev-plug-chademo"),
}


class OUPESWiFiSwitch(
    CoordinatorEntity[OUPESWiFiCoordinator], SwitchEntity
):
    entity_description: OUPESSwitchDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OUPESWiFiCoordinator,
        description: OUPESSwitchDescription,
        subentry_id: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{subentry_id}_{description.key}"
        self._attr_device_info = _device_info(coordinator)

    @property
    def available(self) -> bool:
        last = self.coordinator.last_successful_update
        if last is None:
            return False
        return datetime.now() - last <= self.coordinator.stale_timeout

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        raw = self.coordinator.data.get("attrs", {}).get(_ATTR_OUTPUT_BITMASK)
        if raw is None:
            return None
        return bool(raw & self.entity_description.bit)

    def _current_bitmask(self) -> int:
        if self.coordinator.data is None:
            return 0
        return self.coordinator.data.get("attrs", {}).get(_ATTR_OUTPUT_BITMASK, 0)

    async def async_turn_on(self, **kwargs: Any) -> None:
        new_bitmask = self._current_bitmask() | self.entity_description.bit
        self._apply_and_send(new_bitmask)

    async def async_turn_off(self, **kwargs: Any) -> None:
        new_bitmask = self._current_bitmask() & ~self.entity_description.bit
        self._apply_and_send(new_bitmask)

    def _apply_and_send(self, new_bitmask: int) -> None:
        # Update the coordinator's source-of-truth _attrs so the next
        # telemetry push (async_set_updated_data) doesn't revert the state.
        self.coordinator.optimistic_set_attr(_ATTR_OUTPUT_BITMASK, new_bitmask & 0xFF)
        # Also patch the live snapshot so async_write_ha_state() sees the
        # new value immediately (coordinator.data is replaced each telemetry
        # cycle; mutating it here is safe until then).
        if self.coordinator.data is not None:
            self.coordinator.data.setdefault("attrs", {})[_ATTR_OUTPUT_BITMASK] = new_bitmask & 0xFF
        self.async_write_ha_state()
        self.coordinator.send_output_command(new_bitmask)


def _add_entities_for_device(
    coordinator: OUPESWiFiCoordinator,
    subentry_id: str,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create and register switch entities for one device coordinator."""
    series = series_from_product_id(coordinator.product_id)

    def _resolve(desc: OUPESSwitchDescription) -> OUPESSwitchDescription:
        if desc.key == "dc12v_output_switch" and series in _DC_OUTPUT_NAMES:
            return replace(desc, name=_DC_OUTPUT_NAMES[series])
        if desc.key == "usb_output_switch" and series in _USB_OUTPUT_NAMES:
            name, icon = _USB_OUTPUT_NAMES[series]
            return replace(desc, name=name, icon=icon)
        return desc

    async_add_entities(
        (
            OUPESWiFiSwitch(coordinator, _resolve(desc), subentry_id)
            for desc in SWITCH_DESCRIPTIONS
        ),
        config_subentry_id=subentry_id,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    entry_data = hass.data[DOMAIN][entry.entry_id]

    for subentry_id, coordinator in entry_data["coordinators"].items():
        _add_entities_for_device(coordinator, subentry_id, async_add_entities)

    def _add_for_new_device(coordinator: OUPESWiFiCoordinator, subentry: Any) -> None:
        _add_entities_for_device(coordinator, subentry.subentry_id, async_add_entities)

    entry_data["add_device_fns"]["switch"] = _add_for_new_device
