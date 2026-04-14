"""Switch entities for the OUPES Mega WiFi Client.

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

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, series_from_product_id
from .coordinator import OUPESWiFiClientCoordinator
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
    "mega_1":   ("USB Output",           "mdi:usb"),
    "mega":     ("Anderson & USB Output", "mdi:power-plug"),
    "guardian": ("XT90 Output",           "mdi:ev-plug-chademo"),
}


class OUPESWiFiSwitch(
    CoordinatorEntity[OUPESWiFiClientCoordinator], SwitchEntity
):
    entity_description: OUPESSwitchDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OUPESWiFiClientCoordinator,
        description: OUPESSwitchDescription,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
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

    async def async_turn_on(self, **kwargs) -> None:
        new_bitmask = self._current_bitmask() | self.entity_description.bit
        self._apply_and_send(new_bitmask)

    async def async_turn_off(self, **kwargs) -> None:
        new_bitmask = self._current_bitmask() & ~self.entity_description.bit
        self._apply_and_send(new_bitmask)

    def _apply_and_send(self, new_bitmask: int) -> None:
        if self.coordinator.data is not None:
            self.coordinator.data.setdefault("attrs", {})[_ATTR_OUTPUT_BITMASK] = new_bitmask & 0xFF
        self.async_write_ha_state()
        self.coordinator.send_output_command(new_bitmask)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: OUPESWiFiClientCoordinator = hass.data[DOMAIN][entry.entry_id]
    series = series_from_product_id(coordinator.product_id)

    def _resolve(desc: OUPESSwitchDescription) -> OUPESSwitchDescription:
        if desc.key == "dc12v_output_switch" and series in _DC_OUTPUT_NAMES:
            return replace(desc, name=_DC_OUTPUT_NAMES[series])
        if desc.key == "usb_output_switch" and series in _USB_OUTPUT_NAMES:
            name, icon = _USB_OUTPUT_NAMES[series]
            return replace(desc, name=name, icon=icon)
        return desc

    async_add_entities(
        OUPESWiFiSwitch(coordinator, _resolve(desc), entry)
        for desc in SWITCH_DESCRIPTIONS
    )
