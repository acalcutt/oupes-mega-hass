"""Sensor entities for the OUPES Mega integration."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
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
from .protocol import ATTR_MAP
from .const import series_from_product_id


def _device_info(coordinator: OUPESMegaCoordinator) -> DeviceInfo:
    """Build a DeviceInfo dict with dynamic model name from product_id."""
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.address)},
        name=coordinator.device_name,
        manufacturer="OUPES",
        model=coordinator.model_name,
        connections={("bluetooth", coordinator.address)},
    )


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
    # Override the dict key used to look up the value in coordinator.data.
    # When set, this is used instead of `attr` for the data lookup.
    data_key: str | int | None = None


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

# ── Per-series display names for attr-6 (car port power) ────────────────────
# Mirrors the naming used by the car port output switch — same port, same label.
_CAR_PORT_POWER_NAMES: dict[str, str] = {
    "mega_1":   "Car Port Power",
    "mega":     "Car & 12V Power",
    "guardian": "Car & 12V Power",
}


# ── Battery module sensors (created dynamically per slot) ────────────────────
# Attr 101 carries the slot index; attrs 78/79/80/53/54 carry the per-slot
# values. Entities are added the first time each slot number appears in
# coordinator data, so only the slots that actually exist on this unit are
# ever created in HA. Additional slots appear automatically if more batteries
# are connected later.


def _slot_descriptions(slot: int) -> list[OUPESSensorDescription]:
    """Return the six sensor descriptions for one ext-battery slot."""
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
            # Raw value is direct battery % (0–100).
            # Confirmed: raw 15 = 15% after a few hours of charging.
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
            # Only stores real runtime values; the 99h sentinel (5940 min) that
            # the firmware emits when charging/idle is filtered out in the
            # coordinator, so this sensor goes Unknown when not discharging.
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
            # Confirmed: attr 54 = B2 "OUTPUT W" in the app — total power
            # leaving the B2 (chain cable discharge to Mega + USB ports).
        ),
        OUPESSensorDescription(
            key=f"ext_battery_{slot}_input_power",
            attr=53,
            slot=slot,
            name=f"{display_name} Input Power",
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfPower.WATT,
            # Confirmed: attr 53 = B2 "INPUT W" in the app — power entering
            # the B2 via its secondary MPPT/DC port (solar panel or DC source).
        ),
    ]


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
        self._attr_device_info = _device_info(coordinator)

    @property
    def available(self) -> bool:
        """Stay available for STALE_TIMEOUT after the last successful poll.

        This prevents flickering during transient BLE failures while still
        correctly marking entities unavailable if the device is off long-term.
        Ext battery slots go unavailable only if that slot has never had data.
        Voltage entities go unavailable if the firmware has never emitted a
        voltage reading for that slot (e.g. slot 1 on current Mega 1 firmware).
        """
        last = self.coordinator.last_successful_poll
        if last is None:
            return False  # never had a successful poll yet
        if datetime.now() - last > self.coordinator.stale_timeout:
            return False  # data is too old to be trustworthy
        desc = self.entity_description
        if desc.slot is not None:
            slot_data = self.coordinator.data["ext_batteries"].get(desc.slot)
            if not slot_data:
                return False  # slot never seen
            return True
        return True

    @property
    def native_value(self) -> float | str | None:
        if self.coordinator.data is None:
            return None
        desc = self.entity_description
        if desc.slot is not None:
            lookup = desc.data_key if desc.data_key is not None else desc.attr
            raw = self.coordinator.data["ext_batteries"].get(desc.slot, {}).get(
                lookup
            )
        else:
            lookup = desc.data_key if desc.data_key is not None else desc.attr
            raw = self.coordinator.data["attrs"].get(lookup)

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
    series = series_from_product_id(coordinator.product_id)

    def _resolve(desc: OUPESSensorDescription) -> OUPESSensorDescription:
        if desc.key == "dc_12v_output" and series in _CAR_PORT_POWER_NAMES:
            return replace(desc, name=_CAR_PORT_POWER_NAMES[series])
        return desc

    # Add main device sensors immediately.
    async_add_entities(
        OUPESMegaSensor(coordinator, _resolve(desc), entry)
        for desc in SENSOR_DESCRIPTIONS
    )

    # Ext-battery entities are created on-demand the first time each slot
    # number appears in coordinator data.  This way only batteries that are
    # actually connected to THIS unit are ever registered in HA.
    seen_slots: set[int] = set()

    def _add_new_slots() -> None:
        if not coordinator.data:
            return
        new_slots = set(coordinator.data["ext_batteries"].keys()) - seen_slots
        if not new_slots:
            return
        new_entities = [
            OUPESMegaSensor(coordinator, desc, entry)
            for slot in sorted(new_slots)
            for desc in _slot_descriptions(slot)
        ]
        seen_slots.update(new_slots)
        async_add_entities(new_entities)

    # Run once immediately (first refresh already completed by this point)
    # then re-run on every subsequent coordinator update.
    _add_new_slots()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_slots))
