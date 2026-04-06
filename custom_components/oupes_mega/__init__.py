"""OUPES Mega Power Station — Home Assistant custom integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import CONF_ADDRESS, CONF_CONTINUOUS, CONF_DEBUG_ATTRS, CONF_DEBUG_RAW, CONF_NAME, DOMAIN
from .coordinator import OUPESMegaCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up OUPES Mega from a config entry."""
    address: str = entry.data[CONF_ADDRESS]
    name: str = entry.data.get(CONF_NAME, "OUPES Mega")

    continuous: bool = entry.options.get(CONF_CONTINUOUS, False)
    debug_attrs: bool = entry.options.get(CONF_DEBUG_ATTRS, False)
    debug_raw: bool = entry.options.get(CONF_DEBUG_RAW, False)
    coordinator = OUPESMegaCoordinator(
        hass, address, name,
        continuous=continuous,
        debug_attrs=debug_attrs,
        debug_raw=debug_raw,
    )

    # Perform the first refresh; raises ConfigEntryNotReady if the device
    # is out of range so HA will retry setup automatically.
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as exc:
        raise ConfigEntryNotReady(
            f"Could not connect to OUPES Mega {address}: {exc}"
        ) from exc

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    if continuous:
        coordinator.start_continuous_connection()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    entry.async_on_unload(coordinator.stop_continuous_connection)

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle config entry options update (reload)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
