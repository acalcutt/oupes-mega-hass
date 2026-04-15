"""Number entities for writable device settings on the OUPES Mega WiFi integration."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, SERIES_SETTINGS, series_from_product_id
from .coordinator import OUPESWiFiCoordinator
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
    CoordinatorEntity[OUPESWiFiCoordinator], NumberEntity, RestoreEntity
):
    entity_description: OUPESNumberDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OUPESWiFiCoordinator,
        description: OUPESNumberDescription,
        subentry_id: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{subentry_id}_{description.key}"
        self._attr_device_info = _device_info(coordinator)
        self._optimistic_value: float | None = None

    async def async_added_to_hass(self) -> None:
        """Restore last known setting value on startup.

        Setting DPIDs (41, 45, 46, 47, 49, …) are never echoed in WiFi
        telemetry, so the only way to persist them across HA restarts is to
        restore from the HA state DB.
        """
        await super().async_added_to_hass()
        if self._optimistic_value is None:
            last_state = await self.async_get_last_state()
            if last_state is not None and last_state.state not in (
                "unknown", "unavailable",
            ):
                try:
                    self._optimistic_value = float(last_state.state)
                except (ValueError, TypeError):
                    pass

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
        # Only clear optimistic value if the device actually echoed this
        # DPID back in telemetry.  Setting DPIDs are never included in WiFi
        # cmd=10 responses, so _optimistic_value persists across updates.
        if (
            self.coordinator.data is not None
            and self.entity_description.dpid
            in self.coordinator.data.get("attrs", {})
        ):
            self._optimistic_value = None
        super()._handle_coordinator_update()


def _add_entities_for_device(
    coordinator: OUPESWiFiCoordinator,
    subentry_id: str,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create and register number entities for one device coordinator."""
    series = series_from_product_id(coordinator.product_id)
    supported_dpids = SERIES_SETTINGS.get(series, SERIES_SETTINGS["unknown"])

    async_add_entities(
        (
            OUPESWiFiNumber(coordinator, desc, subentry_id)
            for desc in NUMBER_DESCRIPTIONS
            if desc.dpid in supported_dpids
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

    entry_data["add_device_fns"]["number"] = _add_for_new_device
