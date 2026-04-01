"""Sensor entities for the OUPES Mega integration."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricPotential,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OUPESMegaCoordinator
from .protocol import CHARGE_MODES


# ── Entity description ────────────────────────────────────────────────────────

@dataclass(frozen=True, kw_only=True)
class OUPESSensorDescription(SensorEntityDescription):
    """Extends SensorEntityDescription with OUPES-specific fields."""

    # BLE attribute number for this sensor
    attr: int = 0
    # None → main device; 1 or 2 → external battery slot
    slot: int | None = None
    # Optional transform from raw integer to the value exposed to HA
    value_fn: Callable[[int], float | str] | None = None


# ── Main device sensors ───────────────────────────────────────────────────────

SENSOR_DESCRIPTIONS: tuple[OUPESSensorDescription, ...] = (
    OUPESSensorDescription(
        key="battery_pct",
        attr=3,
        name="Battery",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
    ),
    OUPESSensorDescription(
        key="ac_output_power",
        attr=4,
        name="AC Output Power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    OUPESSensorDescription(
        key="ac_input_power",
        attr=5,
        name="AC Input Power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    OUPESSensorDescription(
        key="dc_charger_input",
        attr=6,
        name="DC Car Charger Input",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
    ),
    OUPESSensorDescription(
        key="solar_input",
        attr=7,
        name="Solar Input",
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
        key="remaining_runtime",
        attr=30,
        name="Remaining Runtime",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        icon="mdi:timer-outline",
    ),
    OUPESSensorDescription(
        key="battery_voltage",
        attr=32,
        name="Battery Pack Voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        value_fn=lambda v: round(v / 10, 1),
    ),
    OUPESSensorDescription(
        key="charge_mode",
        attr=51,
        name="Charge Mode",
        icon="mdi:battery-charging",
        value_fn=lambda v: CHARGE_MODES.get(v, f"Unknown ({v})"),
    ),
)

# ── External battery sensors (slots 1–6, covering Mega 1/2/3 max) ───────────
# Slots with no data are automatically marked unavailable by the entity.

MAX_EXT_BATTERY_SLOTS = 6  # Mega 3: up to 6 × B2 batteries


def _make_ext_battery_descriptions() -> list[OUPESSensorDescription]:
    descs: list[OUPESSensorDescription] = []
    for slot in range(1, MAX_EXT_BATTERY_SLOTS + 1):
        descs.extend(
            [
                OUPESSensorDescription(
                    key=f"ext_battery_{slot}_pct",
                    attr=79,
                    slot=slot,
                    name=f"Ext Battery {slot} Charge",
                    device_class=SensorDeviceClass.BATTERY,
                    state_class=SensorStateClass.MEASUREMENT,
                    native_unit_of_measurement=PERCENTAGE,
                ),
                OUPESSensorDescription(
                    key=f"ext_battery_{slot}_runtime",
                    attr=78,
                    slot=slot,
                    name=f"Ext Battery {slot} Runtime",
                    device_class=SensorDeviceClass.DURATION,
                    state_class=SensorStateClass.MEASUREMENT,
                    native_unit_of_measurement=UnitOfTime.MINUTES,
                    icon="mdi:timer-outline",
                ),
                OUPESSensorDescription(
                    key=f"ext_battery_{slot}_temp",
                    attr=80,
                    slot=slot,
                    name=f"Ext Battery {slot} Temperature",
                    device_class=SensorDeviceClass.TEMPERATURE,
                    state_class=SensorStateClass.MEASUREMENT,
                    native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
                    value_fn=lambda v: round(v / 10, 1),
                ),
            ]
        )
    return descs


EXT_BATTERY_DESCRIPTIONS: list[OUPESSensorDescription] = (
    _make_ext_battery_descriptions()
)


# ── Entity class ──────────────────────────────────────────────────────────────

class OUPESMegaSensor(CoordinatorEntity[OUPESMegaCoordinator], SensorEntity):
    """A single numeric sensor reading from an OUPES Mega device."""

    entity_description: OUPESSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OUPESMegaCoordinator,
        description: OUPESSensorDescription,
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
        """Mark ext battery sensors unavailable when that slot has no data."""
        if not super().available or self.coordinator.data is None:
            return False
        desc = self.entity_description
        if desc.slot is not None:
            return bool(self.coordinator.data["ext_batteries"].get(desc.slot))
        return True

    @property
    def native_value(self) -> float | str | None:
        if self.coordinator.data is None:
            return None
        desc = self.entity_description
        if desc.slot is not None:
            raw = self.coordinator.data["ext_batteries"].get(desc.slot, {}).get(
                desc.attr
            )
        else:
            raw = self.coordinator.data["attrs"].get(desc.attr)

        if raw is None:
            return None
        return desc.value_fn(raw) if desc.value_fn is not None else raw


# ── Platform setup ────────────────────────────────────────────────────────────

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OUPES Mega sensor entities."""
    coordinator: OUPESMegaCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        OUPESMegaSensor(coordinator, desc, entry)
        for desc in [*SENSOR_DESCRIPTIONS, *EXT_BATTERY_DESCRIPTIONS]
    )
