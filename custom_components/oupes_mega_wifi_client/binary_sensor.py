"""Binary sensor entities for the OUPES Mega WiFi Client."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, series_from_product_id
from .coordinator import OUPESWiFiClientCoordinator
from .sensor import _device_info


@dataclass(frozen=True, kw_only=True)
class OUPESBinarySensorDescription(BinarySensorEntityDescription):
    attr: int = 0
    bit_mask: int = 0


BINARY_SENSOR_DESCRIPTIONS: tuple[OUPESBinarySensorDescription, ...] = (
    OUPESBinarySensorDescription(
        key="ac_output",
        attr=1,
        bit_mask=0x01,
        name="AC Output",
        icon="mdi:power-socket",
    ),
    OUPESBinarySensorDescription(
        key="dc_output",
        attr=1,
        bit_mask=0x02,
        name="Car Port",
        icon="mdi:car-electric",
    ),
    OUPESBinarySensorDescription(
        key="usb_output",
        attr=1,
        bit_mask=0x04,
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


class OUPESWiFiBinarySensor(
    CoordinatorEntity[OUPESWiFiClientCoordinator], BinarySensorEntity
):
    entity_description: OUPESBinarySensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OUPESWiFiClientCoordinator,
        description: OUPESBinarySensorDescription,
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
        raw = self.coordinator.data.get("attrs", {}).get(self.entity_description.attr)
        if raw is None:
            return None
        mask = self.entity_description.bit_mask
        return bool(raw & mask) if mask else bool(raw)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: OUPESWiFiClientCoordinator = hass.data[DOMAIN][entry.entry_id]
    series = series_from_product_id(coordinator.product_id)

    def _resolve(desc: OUPESBinarySensorDescription) -> OUPESBinarySensorDescription:
        if desc.key == "dc_output" and series in _DC_OUTPUT_NAMES:
            return replace(desc, name=_DC_OUTPUT_NAMES[series])
        if desc.key == "usb_output" and series in _USB_OUTPUT_NAMES:
            name, icon = _USB_OUTPUT_NAMES[series]
            return replace(desc, name=name, icon=icon)
        return desc

    async_add_entities(
        OUPESWiFiBinarySensor(coordinator, _resolve(desc), entry)
        for desc in BINARY_SENSOR_DESCRIPTIONS
    )
