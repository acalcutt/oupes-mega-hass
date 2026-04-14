"""Number entities for writable device settings on the OUPES WiFi Client."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, SERIES_SETTINGS, series_from_product_id
from .coordinator import OUPESWiFiClientCoordinator
from .sensor import _device_info


@dataclass(frozen=True, kw_only=True)
class OUPESNumberDescription(NumberEntityDescription):
    dpid: int = 0


NUMBER_DESCRIPTIONS: tuple[OUPESNumberDescription, ...] = (
    OUPESNumberDescription(
        key="screen_timeout",
        dpid=41,
        name="Screen Timeout",
        icon="mdi:monitor-off",
        device_class=NumberDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        mode=NumberMode.BOX,
        native_min_value=0,
        native_max_value=3600,
        native_step=30,
    ),
    OUPESNumberDescription(
        key="machine_standby",
        dpid=45,
        name="Machine Standby Timeout",
        icon="mdi:timer-off-outline",
        device_class=NumberDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        mode=NumberMode.BOX,
        native_min_value=0,
        native_max_value=43200,
        native_step=600,
    ),
    OUPESNumberDescription(
        key="wifi_standby",
        dpid=46,
        name="WiFi Standby Timeout",
        icon="mdi:wifi-off",
        device_class=NumberDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        mode=NumberMode.BOX,
        native_min_value=0,
        native_max_value=86400,
        native_step=3600,
    ),
    OUPESNumberDescription(
        key="usb_car_standby",
        dpid=47,
        name="USB/Car Port Standby Timeout",
        icon="mdi:usb",
        device_class=NumberDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        mode=NumberMode.BOX,
        native_min_value=0,
        native_max_value=21600,
        native_step=600,
    ),
    OUPESNumberDescription(
        key="xt90_standby",
        dpid=48,
        name="XT90 Standby Timeout",
        icon="mdi:power-plug-outline",
        device_class=NumberDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        mode=NumberMode.BOX,
        native_min_value=0,
        native_max_value=21600,
        native_step=600,
    ),
    OUPESNumberDescription(
        key="ac_standby",
        dpid=49,
        name="AC Output Standby Timeout",
        icon="mdi:power-socket",
        device_class=NumberDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        mode=NumberMode.BOX,
        native_min_value=0,
        native_max_value=21600,
        native_step=600,
    ),
    OUPESNumberDescription(
        key="ac_eco_threshold",
        dpid=111,
        name="AC ECO Threshold",
        icon="mdi:leaf",
        device_class=NumberDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        mode=NumberMode.BOX,
        native_min_value=0,
        native_max_value=100,
        native_step=5,
    ),
    OUPESNumberDescription(
        key="dc_eco_threshold",
        dpid=113,
        name="DC ECO Threshold",
        icon="mdi:leaf",
        device_class=NumberDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        mode=NumberMode.BOX,
        native_min_value=0,
        native_max_value=100,
        native_step=5,
    ),
)


class OUPESWiFiNumber(
    CoordinatorEntity[OUPESWiFiClientCoordinator], NumberEntity
):
    entity_description: OUPESNumberDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OUPESWiFiClientCoordinator,
        description: OUPESNumberDescription,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = _device_info(coordinator)
        self._optimistic_value: float | None = None

    @property
    def available(self) -> bool:
        last = self.coordinator.last_successful_update
        if last is None:
            return False
        return datetime.now() - last <= self.coordinator.stale_timeout

    @property
    def native_value(self) -> float | None:
        if self._optimistic_value is not None:
            return self._optimistic_value
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("attrs", {}).get(self.entity_description.dpid)

    async def async_set_native_value(self, value: float) -> None:
        int_value = int(value)
        self._optimistic_value = float(int_value)
        self.async_write_ha_state()
        self.coordinator.send_setting_command(self.entity_description.dpid, int_value)

    def _handle_coordinator_update(self) -> None:
        self._optimistic_value = None
        super()._handle_coordinator_update()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: OUPESWiFiClientCoordinator = hass.data[DOMAIN][entry.entry_id]
    series = series_from_product_id(coordinator.product_id)
    supported_dpids = SERIES_SETTINGS.get(series, SERIES_SETTINGS["unknown"])

    async_add_entities(
        OUPESWiFiNumber(coordinator, desc, entry)
        for desc in NUMBER_DESCRIPTIONS
        if desc.dpid in supported_dpids
    )
