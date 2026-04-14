"""Binary sensor entities for boolean attributes on the OUPES Mega."""
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
from .coordinator import OUPESMegaCoordinator
from .sensor import _device_info


@dataclass(frozen=True, kw_only=True)
class OUPESBinarySensorDescription(BinarySensorEntityDescription):
    """Extends BinarySensorEntityDescription with the BLE attr number."""

    attr: int = 0
    bit_mask: int = 0  # if non-zero, check this specific bit of the attr value


# attr 1 switchValue bitmask layout (from APK dcXt90Switch / dcUsbCarSwitch / acSwitch):
#   bit0 (0x01) = AC output (all series)
#   bit1 (0x02) = Car/DC output  ← Mega 1: car port only; Mega 2/3/5 + Guardian: car + 12V barrel jacks
#   bit2 (0x04) = USB/Anderson/XT90  ← Mega 1: USB only; Mega 2/3/5: Anderson+USB grouped; Guardian: XT90
# Names are resolved per-series in async_setup_entry via _DC_OUTPUT_NAMES / _USB_OUTPUT_NAMES.
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
        name="Car Port",        # overridden per series below
        icon="mdi:car-electric",
    ),
    OUPESBinarySensorDescription(
        key="usb_output",
        attr=1,
        bit_mask=0x04,
        name="USB Output",      # overridden per series below
        icon="mdi:usb",
    ),
)

# Per-series display names for bit1 (Car/DC output group).
_DC_OUTPUT_NAMES: dict[str, str] = {
    "mega_1":   "Car Port",
    "mega":     "Car & 12V Output",
    "guardian": "Car & 12V Output",
}

# Per-series display names + icons for bit2 (USB / Anderson / XT90 output group).
_USB_OUTPUT_NAMES: dict[str, tuple[str, str]] = {
    "mega_1":   ("USB Output",           "mdi:usb"),
    "mega":     ("Anderson & USB Output", "mdi:power-plug"),
    "guardian": ("XT90 Output",           "mdi:ev-plug-chademo"),
}


class OUPESMegaBinarySensor(
    CoordinatorEntity[OUPESMegaCoordinator], BinarySensorEntity
):
    """A boolean sensor reading from an OUPES Mega device."""

    entity_description: OUPESBinarySensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OUPESMegaCoordinator,
        description: OUPESBinarySensorDescription,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = _device_info(coordinator)

    @property
    def available(self) -> bool:
        """Stay available for STALE_TIMEOUT after the last successful poll.

        Prevents flickering during transient BLE failures while still correctly
        marking unavailable if the device has been off long-term.
        """
        last = self.coordinator.last_successful_poll
        if last is None:
            return False
        return datetime.now() - last <= self.coordinator.stale_timeout

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        raw = self.coordinator.data["attrs"].get(self.entity_description.attr)
        if raw is None:
            return None
        mask = self.entity_description.bit_mask
        return bool(raw & mask) if mask else bool(raw)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OUPES Mega binary sensor entities."""
    coordinator: OUPESMegaCoordinator = hass.data[DOMAIN][entry.entry_id]
    series = series_from_product_id(coordinator.product_id)

    def _resolve(desc: OUPESBinarySensorDescription) -> OUPESBinarySensorDescription:
        if desc.key == "dc_output" and series in _DC_OUTPUT_NAMES:
            return replace(desc, name=_DC_OUTPUT_NAMES[series])
        if desc.key == "usb_output" and series in _USB_OUTPUT_NAMES:
            name, icon = _USB_OUTPUT_NAMES[series]
            return replace(desc, name=name, icon=icon)
        return desc

    async_add_entities(
        OUPESMegaBinarySensor(coordinator, _resolve(desc), entry)
        for desc in BINARY_SENSOR_DESCRIPTIONS
    )
