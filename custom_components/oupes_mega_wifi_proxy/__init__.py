"""OUPES Mega WiFi Proxy — Home Assistant custom integration.

Starts a TCP server on the configured port (default 8896) that speaks the
OUPES cloud broker protocol.  Point your router/DNS to redirect the device's
broker hostname to this HA instance to intercept WiFi telemetry.

Current v0.1 scope:
  - Accept device connections
  - Maintain the broker protocol (auth / subscribe / ping-pong / keep)
  - Periodically poll attribute groups and log cmd=10 telemetry responses
  - Log everything for further protocol analysis

Future scope:
  - Expose telemetry as HA sensor entities
  - Send control commands (cmd=3 write) from HA
"""
from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.config_entries import ConfigEntry, ConfigEntryChange, SIGNAL_CONFIG_ENTRY_CHANGED
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import (
    CONF_DEBUG_HTTP,
    CONF_DEBUG_RAW_LINES,
    CONF_DEBUG_TELEMETRY,
    CONF_DEVICE_ID,
    CONF_DEVICE_KEY,
    CONF_DEVICE_NAME,
    CONF_MAC_ADDRESS,
    CONF_HTTP_PORT,
    CONF_MAIL,
    CONF_PASSWD,
    CONF_UID,
    CONF_PORT,
    CONF_SIBO_PORT,
    CONF_VALIDATION_MODE,
    DEFAULT_HTTP_PORT,
    DEFAULT_PORT,
    DEFAULT_SIBO_PORT,
    DEFAULT_VALIDATION_MODE,
    DOMAIN,
)
from .http_server import OUPESHttpInterceptServer
from .server import OUPESWiFiProxyServer
from .sibo_server import SiBoClouServerStub

_LOGGER = logging.getLogger(__name__)


def _build_device_registry(hass: HomeAssistant) -> dict[str, str]:
    """Build {device_id: device_key} for the TCP broker from all User Entries."""
    registry: dict[str, str] = {}
    if DOMAIN not in hass.data:
        return registry
        
    for entry in hass.config_entries.async_entries(DOMAIN):
        for subentry in getattr(entry, "subentries", {}).values():
            device_id = subentry.data.get(CONF_DEVICE_ID, "").strip()
            device_key = subentry.data.get(CONF_DEVICE_KEY, "").strip()
            if device_id and device_key:
                registry[device_id] = device_key
    return registry


def _build_user_registry(hass: HomeAssistant) -> dict[str, dict]:
    """Build {email: {passwd, devices: [...]}} for the HTTP intercept server from all entries."""
    registry: dict[str, dict] = {}
    if DOMAIN not in hass.data:
        return registry
        
    for entry in hass.config_entries.async_entries(DOMAIN):
        email = entry.data.get(CONF_MAIL, "").strip().lower()
        if not email:
            continue
        if email not in registry:
            registry[email] = {
                "passwd": entry.data.get(CONF_PASSWD, ""),
                "uid": entry.data.get(CONF_UID),
                "devices": [],
            }
            
        for subentry in getattr(entry, "subentries", {}).values():
            device_id = subentry.data.get(CONF_DEVICE_ID, "").strip()
            device_key = subentry.data.get(CONF_DEVICE_KEY, "").strip()
            device_name = getattr(subentry, "title", "").strip() or subentry.data.get(CONF_DEVICE_NAME, "").strip() or "OUPES"
            mac_address = subentry.data.get(CONF_MAC_ADDRESS, "").strip()
            if device_id and device_key:
                registry[email]["devices"].append(
                    {
                        "device_id": device_id,
                        "device_key": device_key,
                        "device_name": device_name,
                        "mac_address": mac_address,
                    }
                )
    return registry


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Start the servers for this config entry or share the existing ones."""
    hass.data.setdefault(DOMAIN, {})

    # If the proxy servers are already started by another entry, just update the registries and return
    if "proxy" in hass.data[DOMAIN]:
        await _async_update_registries(hass)
        entry.async_on_unload(entry.add_update_listener(_async_options_updated))
        return True

    port: int = int(entry.options.get(CONF_PORT, entry.data.get(CONF_PORT, DEFAULT_PORT)))
    http_port: int = int(entry.options.get(CONF_HTTP_PORT, entry.data.get(CONF_HTTP_PORT, DEFAULT_HTTP_PORT)))
    sibo_port: int = int(entry.options.get(CONF_SIBO_PORT, entry.data.get(CONF_SIBO_PORT, DEFAULT_SIBO_PORT)))
    validation_mode: str = entry.options.get(CONF_VALIDATION_MODE, DEFAULT_VALIDATION_MODE)
    debug_raw_lines: bool = entry.options.get(CONF_DEBUG_RAW_LINES, False)
    debug_telemetry: bool = entry.options.get(CONF_DEBUG_TELEMETRY, False)
    debug_http: bool = entry.options.get(CONF_DEBUG_HTTP, False)
    
    device_registry = _build_device_registry(hass)
    user_registry = _build_user_registry(hass)

    debug_file: Path | None = None
    if debug_raw_lines or debug_telemetry or debug_http:
        debug_file = Path(hass.config.config_dir) / "oupes_mega_wifi_proxy_debug.jsonl"
        _LOGGER.info("OUPES WiFi proxy: debug log ? %s", debug_file)

    server = OUPESWiFiProxyServer(
        port=port,
        device_registry=device_registry,
        validation_mode=validation_mode,
        debug_file=debug_file,
        debug_raw_lines=debug_raw_lines,
        debug_telemetry=debug_telemetry,
    )
    try:
        await server.start()
    except OSError as exc:
        _LOGGER.error(
            "OUPES WiFi proxy: could not bind to port %d: %s", port, exc
        )
        return False

    http_server = OUPESHttpInterceptServer(
        port=http_port,
        tcp_port=port,
        user_registry=user_registry,
        validation_mode=validation_mode,
        debug_file=debug_file,
        debug_http=debug_http,
        tcp_server=server,
    )
    try:
        await http_server.start()
    except OSError as exc:
        _LOGGER.error(
            "OUPES WiFi proxy: could not bind HTTP intercept to port %d: %s",
            http_port,
            exc,
        )
        await server.stop()
        return False

    sibo_server = SiBoClouServerStub(port=sibo_port)
    try:
        await sibo_server.start()
    except OSError as exc:
        _LOGGER.error(
            "OUPES WiFi proxy: could not bind SiBo HTTPS mock to port %d: %s",
            sibo_port,
            exc,
        )
        # Non-fatal — log but continue without SiBo interception.
        _LOGGER.warning(
            "SiBo mock disabled — token-error login loop may occur without Squid (or equivalent) SSL inspection setup."
        )
        sibo_server = None

    hass.data[DOMAIN]["proxy"] = {
        "tcp": server,
        "http": http_server,
        "sibo": sibo_server,
        "primary_entry_id": entry.entry_id,
    }

    # Now that the domain data proxy is created, update registries (it uses hass.data[DOMAIN])
    await _async_update_registries(hass)

    # Subscribe to config-entry changes so that adding/removing a device subentry
    # immediately updates the HTTP server's user-registry without requiring a
    # full proxy reload.  Only the primary entry registers this listener to avoid
    # redundant updates when multiple user-account entries are present.
    @callback
    def _async_entry_changed(
        change_type: ConfigEntryChange, changed_entry: ConfigEntry
    ) -> None:
        if change_type == ConfigEntryChange.UPDATED and changed_entry.domain == DOMAIN:
            hass.async_create_task(_async_update_registries(hass))

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_CONFIG_ENTRY_CHANGED, _async_entry_changed)
    )
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Stop the TCP proxy and HTTP intercept servers."""
    proxy = hass.data.get(DOMAIN, {}).get("proxy")
    
    # Only the entry that created the proxy server can tear it down.
    if proxy and proxy.get("primary_entry_id") == entry.entry_id:
        tcp_server: OUPESWiFiProxyServer | None = proxy.get("tcp")
        http_server: OUPESHttpInterceptServer | None = proxy.get("http")
        sibo_server: SiBoClouServerStub | None = proxy.get("sibo")
        if tcp_server:
            await tcp_server.stop()
        if http_server:
            await http_server.stop()
        if sibo_server:
            await sibo_server.stop()
        hass.data[DOMAIN].pop("proxy", None)
    else:
        # Otherwise, just rebuilding the registries since this user account goes away
        await _async_update_registries(hass)

    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Restart server when options (e.g. port) change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_update_registries(hass: HomeAssistant):
    """Update device and user registries on the running proxy server."""
    servers = hass.data.get(DOMAIN, {}).get("proxy", {})
    http_server: OUPESHttpInterceptServer | None = servers.get("http")
    if http_server:
        http_server.update_user_registry(_build_user_registry(hass))
        
    tcp_server: OUPESWiFiProxyServer | None = servers.get("tcp")
    if tcp_server:
        tcp_server.update_device_registry(_build_device_registry(hass))


async def async_setup_subentry(
    hass: HomeAssistant, entry: ConfigEntry, subentry
) -> bool:
    """Called when a sub-entry (device) is added — refresh the registries."""
    await _async_update_registries(hass)
    return True


async def async_unload_subentry(
    hass: HomeAssistant, entry: ConfigEntry, subentry
) -> bool:
    """Called when a sub-entry (device) is removed — refresh the registries."""
    await _async_update_registries(hass)
    return True

