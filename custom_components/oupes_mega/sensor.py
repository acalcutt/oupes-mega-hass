"""Sensor entities for the OUPES Mega integration."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
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

from .const import DOMAIN, STALE_TIMEOUT
from .coordinator import OUPESMegaCoordinator
from .protocol import ATTR_MAP


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
        name="Battery Charge",
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
        key="unknown_attr51",
        attr=51,
        name="Unknown (attr 51)",
        icon="mdi:help-circle-outline",
    ),
)

# ── Battery module sensors (slots 1–N) ──────────────────────────────────────
# Attr 101 carries the slot index; attrs 78/79/80 carry the per-slot values.
#
# On Mega 1: slots 1–2 = two INTERNAL battery modules (always present).
# External OUPES B2 Expansion Batteries appear as additional slots.
# Known B2 external-battery maximums per model:
#   Mega 1 → up to 2 B2 batteries
#   Mega 2 → up to 4 B2 batteries
#   Mega 3 → up to 6 B2 batteries
# Total slot count for Mega 2/3 (internal + external) is unconfirmed;
# 6 covers the Mega 3 external-only maximum and is a safe ceiling for now.
# Slots with no data are automatically marked unavailable by the entity.

MAX_EXT_BATTERY_SLOTS = 6  # conservative ceiling; increase if Mega 2/3 exceed this


def _make_ext_battery_descriptions() -> list[OUPESSensorDescription]:
    descs: list[OUPESSensorDescription] = []
    for slot in range(1, MAX_EXT_BATTERY_SLOTS + 1):
        descs.extend(
            [
                OUPESSensorDescription(
                    key=f"ext_battery_{slot}_pct",
                    attr=79,
                    slot=slot,
                    name=f"External Battery {slot} Charge",
                    device_class=SensorDeviceClass.BATTERY,
                    state_class=SensorStateClass.MEASUREMENT,
                    native_unit_of_measurement=PERCENTAGE,
                    # Raw value is direct battery % (0–100).
                    # Confirmed: raw 15 = 15% after a few hours of charging.
                ),
                OUPESSensorDescription(
                    key=f"ext_battery_{slot}_runtime",
                    attr=78,
                    slot=slot,
                    name=f"External Battery {slot} Runtime",
                    device_class=SensorDeviceClass.DURATION,
                    state_class=SensorStateClass.MEASUREMENT,
                    native_unit_of_measurement=UnitOfTime.MINUTES,
                    icon="mdi:timer-outline",
                ),
                OUPESSensorDescription(
                    key=f"ext_battery_{slot}_temp",
                    attr=80,
                    slot=slot,
                    name=f"External Battery {slot} Temperature",
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
        """Stay available for STALE_TIMEOUT after the last successful poll.

        This prevents flickering during transient BLE failures while still
        correctly marking entities unavailable if the device is off long-term.
        Ext battery slots go unavailable only if that slot has never had data.
        """
        last = self.coordinator.last_successful_poll
        if last is None:
            return False  # never had a successful poll yet
        if datetime.now() - last > STALE_TIMEOUT:
            return False  # data is too old to be trustworthy
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
