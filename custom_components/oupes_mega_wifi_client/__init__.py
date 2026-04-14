"""OUPES Mega WiFi Client — Home Assistant custom integration.

Connects to a running OUPES WiFi proxy (oupes_mega_wifi_proxy) as a TCP
broker client, using the same protocol the real Android app uses. Creates
HA entities (sensors, switches, numbers) from the telemetry stream.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_KEY,
    CONF_DEVICE_NAME,
    CONF_HOST,
    CONF_PRODUCT_ID,
    CONF_TCP_PORT,
    CONF_TOKEN,
    DEFAULT_TCP_PORT,
    DOMAIN,
)
from .coordinator import OUPESWiFiClientCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up an OUPES WiFi Client device from a config entry."""
    host = entry.data[CONF_HOST]
    tcp_port = int(entry.data.get(CONF_TCP_PORT, DEFAULT_TCP_PORT))
    device_id = entry.data[CONF_DEVICE_ID]
    device_key = entry.data.get(CONF_DEVICE_KEY, "")
    device_name = entry.data.get(CONF_DEVICE_NAME, "OUPES")
    product_id = entry.data.get(CONF_PRODUCT_ID, "")
    token = entry.data.get(CONF_TOKEN, "")

    coordinator = OUPESWiFiClientCoordinator(
        hass,
        host=host,
        tcp_port=tcp_port,
        device_id=device_id,
        device_key=device_key,
        device_name=device_name,
        product_id=product_id,
        token=token,
    )

    # Start the persistent TCP connection
    coordinator.start()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(lambda: coordinator.stop())

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id, None)
        if coordinator:
            coordinator.stop()
    return unload_ok
