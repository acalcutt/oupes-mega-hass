"""Binary sensor entities for boolean attributes on the OUPES Mega."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, STALE_TIMEOUT
from .coordinator import OUPESMegaCoordinator


@dataclass(frozen=True, kw_only=True)
class OUPESBinarySensorDescription(BinarySensorEntityDescription):
    """Extends BinarySensorEntityDescription with the BLE attr number."""

    attr: int = 0


BINARY_SENSOR_DESCRIPTIONS: tuple[OUPESBinarySensorDescription, ...] = (
    OUPESBinarySensorDescription(
        key="ac_output",
        attr=1,
        name="AC Output",
        icon="mdi:power-socket",
    ),
    OUPESBinarySensorDescription(
        key="dc_output",
        attr=2,
        name="DC Output",
        icon="mdi:car-electric",
    ),
    OUPESBinarySensorDescription(
        key="ac_input_connected",
        attr=23,
        name="AC Input Connected",
        icon="mdi:transmission-tower",
    ),
    OUPESBinarySensorDescription(
        key="ac_output_control",
        attr=84,
        name="AC Output Control",
        icon="mdi:toggle-switch",
    ),
)


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
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            name=coordinator.device_name,
            manufacturer="OUPES",
            model="Mega 1",
        )

    @property
    def available(self) -> bool:
        """Stay available for STALE_TIMEOUT after the last successful poll.

        Prevents flickering during transient BLE failures while still correctly
        marking unavailable if the device has been off long-term.
        """
        last = self.coordinator.last_successful_poll
        if last is None:
            return False
        return datetime.now() - last <= STALE_TIMEOUT

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        raw = self.coordinator.data["attrs"].get(self.entity_description.attr)
        return bool(raw) if raw is not None else None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OUPES Mega binary sensor entities."""
    coordinator: OUPESMegaCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        OUPESMegaBinarySensor(coordinator, desc, entry)
        for desc in BINARY_SENSOR_DESCRIPTIONS
    )
