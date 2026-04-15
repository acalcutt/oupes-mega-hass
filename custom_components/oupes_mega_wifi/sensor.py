"""Sensor entities for the OUPES Mega WiFi integration."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, model_name_from_product_id, series_from_product_id
from .coordinator import OUPESWiFiCoordinator


def _device_info(coordinator: OUPESWiFiCoordinator) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.device_id)},
        name=coordinator.device_name,
        manufacturer="OUPES",
        model=model_name_from_product_id(coordinator.product_id),
    )


@dataclass(frozen=True, kw_only=True)
class OUPESSensorDescription(SensorEntityDescription):
    attr: int = 0
    slot: int | None = None
    value_fn: Callable[[int], float | str] | None = None
    data_key: str | int | None = None


SENSOR_DESCRIPTIONS: tuple[OUPESSensorDescription, ...] = (
    OUPESSensorDescription(
        key="battery_pct",
        attr=3,
        name="Battery Charge",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
    ),
    OUPESSensorDescription(
        key="total_output_power",
        attr=4,
        name="Total Output Power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    OUPESSensorDescription(
        key="ac_output_power",
        attr=5,
        name="AC Output Power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    OUPESSensorDescription(
        key="dc_12v_output",
        attr=6,
        name="DC 12V Output",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    OUPESSensorDescription(
        key="usb_c_output",
        attr=7,
        name="USB-C Output",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    OUPESSensorDescription(
        key="usb_a_output",
        attr=8,
        name="USB-A Output",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    OUPESSensorDescription(
        key="total_input_power",
        attr=21,
        name="Total Input Power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    OUPESSensorDescription(
        key="grid_input_power",
        attr=22,
        name="Grid Input Power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    OUPESSensorDescription(
        key="solar_input_power",
        attr=23,
        name="Solar Input Power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    OUPESSensorDescription(
        key="remaining_runtime",
        attr=30,
        data_key="last_runtime_min",
        name="Remaining Runtime",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        icon="mdi:timer-outline",
    ),
    OUPESSensorDescription(
        key="main_unit_temp",
        attr=32,
        name="Main Unit Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        value_fn=lambda v: round(v / 10, 1),
    ),
    OUPESSensorDescription(
        key="expansion_battery_count",
        attr=51,
        name="Expansion Battery Count",
        icon="mdi:battery-plus",
        state_class=SensorStateClass.MEASUREMENT,
    ),
)

_CAR_PORT_POWER_NAMES: dict[str, str] = {
    "mega_1":   "Car Port Power",
    "mega":     "Car & 12V Power",
    "guardian": "Car & 12V Power",
}


def _slot_descriptions(slot: int) -> list[OUPESSensorDescription]:
    display_name = f"External Battery {slot}"
    return [
        OUPESSensorDescription(
            key=f"ext_battery_{slot}_pct",
            attr=79,
            slot=slot,
            name=f"{display_name} Charge",
            device_class=SensorDeviceClass.BATTERY,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=PERCENTAGE,
        ),
        OUPESSensorDescription(
            key=f"ext_battery_{slot}_runtime",
            attr=78,
            data_key="last_runtime_min",
            slot=slot,
            name=f"{display_name} Runtime",
            device_class=SensorDeviceClass.DURATION,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfTime.MINUTES,
            icon="mdi:timer-outline",
        ),
        OUPESSensorDescription(
            key=f"ext_battery_{slot}_temp",
            attr=80,
            slot=slot,
            name=f"{display_name} Temperature",
            device_class=SensorDeviceClass.TEMPERATURE,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
            value_fn=lambda v: round(v / 10, 1),
        ),
        OUPESSensorDescription(
            key=f"ext_battery_{slot}_output_power",
            attr=54,
            slot=slot,
            name=f"{display_name} Output Power",
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfPower.WATT,
        ),
        OUPESSensorDescription(
            key=f"ext_battery_{slot}_input_power",
            attr=53,
            slot=slot,
            name=f"{display_name} Input Power",
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfPower.WATT,
        ),
    ]


class OUPESWiFiSensor(
    CoordinatorEntity[OUPESWiFiCoordinator], SensorEntity
):
    entity_description: OUPESSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OUPESWiFiCoordinator,
        description: OUPESSensorDescription,
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
        if datetime.now() - last > self.coordinator.stale_timeout:
            return False
        desc = self.entity_description
        if desc.slot is not None:
            slot_data = (self.coordinator.data or {}).get("ext_batteries", {}).get(desc.slot)
            if not slot_data:
                return False
        return True

    @property
    def native_value(self) -> float | str | None:
        if self.coordinator.data is None:
            return None
        desc = self.entity_description
        lookup = desc.data_key if desc.data_key is not None else desc.attr
        if desc.slot is not None:
            raw = (
                self.coordinator.data.get("ext_batteries", {})
                .get(desc.slot, {})
                .get(lookup)
            )
        else:
            raw = self.coordinator.data.get("attrs", {}).get(lookup)
        if raw is None:
            return None
        return desc.value_fn(raw) if desc.value_fn is not None else raw


def _add_entities_for_device(
    coordinator: OUPESWiFiCoordinator,
    subentry_id: str,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create and register sensor entities for one device coordinator."""
    series = series_from_product_id(coordinator.product_id)

    def _resolve(desc: OUPESSensorDescription) -> OUPESSensorDescription:
        if desc.key == "dc_12v_output" and series in _CAR_PORT_POWER_NAMES:
            return replace(desc, name=_CAR_PORT_POWER_NAMES[series])
        return desc

    async_add_entities(
        (
            OUPESWiFiSensor(coordinator, _resolve(desc), subentry_id)
            for desc in SENSOR_DESCRIPTIONS
        ),
        config_subentry_id=subentry_id,
    )

    # Dynamic expansion-battery slot detection
    seen_slots: set[int] = set()

    def _add_new_slots() -> None:
        if not coordinator.data:
            return
        new_slots = set(coordinator.data.get("ext_batteries", {}).keys()) - seen_slots
        if not new_slots:
            return
        new_entities = [
            OUPESWiFiSensor(coordinator, desc, subentry_id)
            for slot in sorted(new_slots)
            for desc in _slot_descriptions(slot)
        ]
        seen_slots.update(new_slots)
        async_add_entities(new_entities, config_subentry_id=subentry_id)

    _add_new_slots()
    coordinator.async_add_listener(_add_new_slots)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    entry_data = hass.data[DOMAIN][entry.entry_id]

    # Add entities for all currently-known coordinators
    for subentry_id, coordinator in entry_data["coordinators"].items():
        _add_entities_for_device(coordinator, subentry_id, async_add_entities)

    # Register callback so async_setup_subentry can add entities for future devices
    def _add_for_new_device(coordinator: OUPESWiFiCoordinator, subentry: Any) -> None:
        _add_entities_for_device(coordinator, subentry.subentry_id, async_add_entities)

    entry_data["add_device_fns"]["sensor"] = _add_for_new_device
