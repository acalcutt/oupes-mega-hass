"""OUPES Mega WiFi — Home Assistant custom integration.

Combines the proxy broker servers (TCP/HTTP/SiBo) with per-device HA entities
(sensors, switches, numbers).

Architecture:
  - One config entry per user account (email/password/ports).
  - One config subentry per device (device_id, device_key, device_name, mac).
  - Proxy servers (TCP broker, HTTP intercept, SiBo stub) are started once for
    the first/primary config entry.
  - Each device subentry gets its own OUPESWiFiCoordinator that connects as an
    internal TCP client to the local broker, then populates HA entities.

Adding a new device:
  1. User adds a subentry via the config flow.
  2. HA calls async_setup_subentry() which creates a coordinator and calls the
     per-platform add_device_fn callbacks to register new entities immediately.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryChange, SIGNAL_CONFIG_ENTRY_CHANGED
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import (
    ATTR78_RUNTIME_MAX,
    CONF_DEBUG_HTTP,
    CONF_DEBUG_RAW_LINES,
    CONF_DEBUG_TELEMETRY,
    CONF_DEVICE_ID,
    CONF_DEVICE_KEY,
    CONF_DEVICE_NAME,
    CONF_HTTP_PORT,
    CONF_MAC_ADDRESS,
    CONF_MAIL,
    CONF_PASSWD,
    CONF_PORT,
    CONF_PRODUCT_ID,
    CONF_RUNTIME_MAX,
    CONF_SIBO_PORT,
    CONF_UID,
    CONF_VALIDATION_MODE,
    DEFAULT_HTTP_PORT,
    DEFAULT_PORT,
    DEFAULT_SIBO_PORT,
    DEFAULT_VALIDATION_MODE,
    DOMAIN,
)
from .coordinator import OUPESWiFiCoordinator
from .http_server import OUPESHttpInterceptServer
from .server import OUPESWiFiProxyServer
from .sibo_server import SiBoClouServerStub

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
]


# ── Registry helpers (same as proxy) ──────────────────────────────────────────


def _build_device_registry(hass: HomeAssistant) -> dict[str, str]:
    """Build {device_id: device_key} for the TCP broker from all config entries."""
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
    """Build {email: {passwd, uid, devices}} for the HTTP intercept server."""
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
            device_name = (
                getattr(subentry, "title", "").strip()
                or subentry.data.get(CONF_DEVICE_NAME, "").strip()
                or "OUPES"
            )
            mac_address = subentry.data.get(CONF_MAC_ADDRESS, "").strip()
            if device_id and device_key:
                registry[email]["devices"].append({
                    "device_id": device_id,
                    "device_key": device_key,
                    "device_name": device_name,
                    "mac_address": mac_address,
                })
    return registry


async def _async_update_registries(hass: HomeAssistant) -> None:
    """Push updated device/user registries to the running proxy servers."""
    servers = hass.data.get(DOMAIN, {}).get("proxy", {})
    http_server: OUPESHttpInterceptServer | None = servers.get("http")
    if http_server:
        http_server.update_user_registry(_build_user_registry(hass))
    tcp_server: OUPESWiFiProxyServer | None = servers.get("tcp")
    if tcp_server:
        tcp_server.update_device_registry(_build_device_registry(hass))


# ── Coordinator factory ───────────────────────────────────────────────────────


def _coordinator_for_subentry(
    hass: HomeAssistant, entry: ConfigEntry, subentry: Any
) -> OUPESWiFiCoordinator:
    """Create (but don't start) a coordinator for a device subentry."""
    port: int = int(
        entry.options.get(CONF_PORT, entry.data.get(CONF_PORT, DEFAULT_PORT))
    )
    device_id = subentry.data.get(CONF_DEVICE_ID, "")
    device_key = subentry.data.get(CONF_DEVICE_KEY, "")
    device_name = (
        getattr(subentry, "title", "").strip()
        or subentry.data.get(CONF_DEVICE_NAME, "").strip()
        or "OUPES"
    )
    runtime_max = subentry.data.get(CONF_RUNTIME_MAX, ATTR78_RUNTIME_MAX)

    # Prefer product_id persisted in the subentry (survives HA restarts).
    # Fall back to the HTTP server's live device cache if present.
    product_id: str = subentry.data.get(CONF_PRODUCT_ID, "")
    if not product_id:
        http_server: OUPESHttpInterceptServer | None = (
            hass.data.get(DOMAIN, {}).get("proxy", {}).get("http")
        )
        if http_server is not None:
            cached = getattr(http_server, "_device_cache", {}).get(device_id, {})
            product_id = cached.get("device_product_id", "")

    return OUPESWiFiCoordinator(
        hass,
        host="127.0.0.1",
        tcp_port=port,
        device_id=device_id,
        device_key=device_key,
        device_name=device_name,
        product_id=product_id,
        token="internal",
        runtime_max_minutes=runtime_max,
    )


# ── Entry lifecycle ───────────────────────────────────────────────────────────


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Start proxy servers (primary entry) and coordinator for each subentry."""
    hass.data.setdefault(DOMAIN, {})

    # ── 1. Start proxy servers (only for the first / primary entry) ────────
    if "proxy" not in hass.data[DOMAIN]:
        port: int = int(
            entry.options.get(CONF_PORT, entry.data.get(CONF_PORT, DEFAULT_PORT))
        )
        http_port: int = int(
            entry.options.get(CONF_HTTP_PORT, entry.data.get(CONF_HTTP_PORT, DEFAULT_HTTP_PORT))
        )
        sibo_port: int = int(
            entry.options.get(CONF_SIBO_PORT, entry.data.get(CONF_SIBO_PORT, DEFAULT_SIBO_PORT))
        )
        validation_mode: str = entry.options.get(CONF_VALIDATION_MODE, DEFAULT_VALIDATION_MODE)
        debug_raw_lines: bool = entry.options.get(CONF_DEBUG_RAW_LINES, False)
        debug_telemetry: bool = entry.options.get(CONF_DEBUG_TELEMETRY, False)
        debug_http: bool = entry.options.get(CONF_DEBUG_HTTP, False)

        debug_file: Path | None = None
        if debug_raw_lines or debug_telemetry or debug_http:
            debug_file = Path(hass.config.config_dir) / "oupes_mega_wifi_debug.jsonl"
            _LOGGER.info("OUPES Mega WiFi: debug log → %s", debug_file)

        tcp_server = OUPESWiFiProxyServer(
            port=port,
            device_registry=_build_device_registry(hass),
            validation_mode=validation_mode,
            debug_file=debug_file,
            debug_raw_lines=debug_raw_lines,
            debug_telemetry=debug_telemetry,
        )
        try:
            await tcp_server.start()
        except OSError as exc:
            _LOGGER.error("OUPES Mega WiFi: could not bind TCP port %d: %s", port, exc)
            return False

        http_server = OUPESHttpInterceptServer(
            port=http_port,
            tcp_port=port,
            user_registry=_build_user_registry(hass),
            validation_mode=validation_mode,
            debug_file=debug_file,
            debug_http=debug_http,
            tcp_server=tcp_server,
        )
        try:
            await http_server.start()
        except OSError as exc:
            _LOGGER.error(
                "OUPES Mega WiFi: could not bind HTTP port %d: %s", http_port, exc
            )
            await tcp_server.stop()
            return False

        sibo_server: SiBoClouServerStub | None
        try:
            sibo_server = SiBoClouServerStub(port=sibo_port)
            await sibo_server.start()
        except OSError as exc:
            _LOGGER.warning(
                "OUPES Mega WiFi: could not bind SiBo HTTPS port %d: %s — "
                "SiBo mock disabled",
                sibo_port, exc,
            )
            sibo_server = None

        hass.data[DOMAIN]["proxy"] = {
            "tcp": tcp_server,
            "http": http_server,
            "sibo": sibo_server,
            "primary_entry_id": entry.entry_id,
        }

        # ── Register bind callback — fires when a device calls /api/device/bind
        #    and tells us its product_id.  We update the matching coordinator
        #    so model-specific entity names take effect without a restart, and
        #    persist the product_id to the subentry so it survives HA restarts.
        def _on_device_bind(device_id: str, product_id: str) -> None:
            for cfg_entry in hass.config_entries.async_entries(DOMAIN):
                for subentry_id, sub in getattr(cfg_entry, "subentries", {}).items():
                    if sub.data.get(CONF_DEVICE_ID) != device_id:
                        continue
                    if sub.data.get(CONF_PRODUCT_ID) == product_id:
                        return  # already up to date

                    async def _persist_and_reload(
                        _entry: ConfigEntry = cfg_entry,
                        _sub_id: str = subentry_id,
                        _sub: Any = sub,
                        _pid: str = product_id,
                    ) -> None:
                        # Persist product_id into the subentry.
                        await hass.config_entries.async_update_subentry(
                            _entry,
                            _sub,
                            data={**_sub.data, CONF_PRODUCT_ID: _pid},
                        )
                        # Reload the subentry so entities are recreated with the
                        # correct model-specific names (entity_description.name is
                        # baked at registration time; updating coord.product_id
                        # alone does not re-derive the names).
                        fresh = _entry.subentries.get(_sub_id)
                        if fresh is None:
                            return
                        _LOGGER.info(
                            "OUPES WiFi: product_id for %s changed → %s; reloading subentry",
                            device_id, _pid,
                        )
                        await hass.config_entries.async_unload_subentry(_entry, fresh)
                        fresh2 = _entry.subentries.get(_sub_id)
                        if fresh2:
                            await hass.config_entries.async_setup_subentry(_entry, fresh2)

                    hass.async_create_task(_persist_and_reload())
                    return

        http_server.on_device_bind = _on_device_bind

    await _async_update_registries(hass)

    # ── 2. Init per-entry data storage ────────────────────────────────────
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinators": {},   # subentry_id → OUPESWiFiCoordinator
        "add_device_fns": {}, # platform_key → Callable[[coordinator, subentry], None]
        "_cached_options": dict(entry.options),  # snapshot to detect real options changes
    }

    # ── 3. Create coordinators for already-configured subentries ──────────
    for subentry in getattr(entry, "subentries", {}).values():
        coordinator = _coordinator_for_subentry(hass, entry, subentry)
        hass.data[DOMAIN][entry.entry_id]["coordinators"][subentry.subentry_id] = coordinator
        coordinator.start()

    # ── 4. Forward platform setups (platforms register their add_device_fns) ─
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # ── 5. Subscribe to config-entry changes to keep proxy registries fresh ──
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
    """Stop coordinators, unload platforms, and stop proxy if primary."""
    # Stop all coordinators for this entry
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    for coordinator in entry_data.get("coordinators", {}).values():
        coordinator.stop()

    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    hass.data[DOMAIN].pop(entry.entry_id, None)

    # Tear down proxy only if this is the primary entry
    proxy = hass.data.get(DOMAIN, {}).get("proxy", {})
    if proxy.get("primary_entry_id") == entry.entry_id:
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
        await _async_update_registries(hass)

    return unload_ok


async def async_setup_subentry(
    hass: HomeAssistant, entry: ConfigEntry, subentry: Any
) -> bool:
    """Create coordinator and entities for a newly-added device subentry."""
    await _async_update_registries(hass)

    # Create and start tracker for this subentry
    coordinator = _coordinator_for_subentry(hass, entry, subentry)
    hass.data[DOMAIN][entry.entry_id]["coordinators"][subentry.subentry_id] = coordinator
    coordinator.start()

    # Notify each platform to add entities for the new device
    for fn in hass.data[DOMAIN][entry.entry_id].get("add_device_fns", {}).values():
        fn(coordinator, subentry)

    return True


async def async_unload_subentry(
    hass: HomeAssistant, entry: ConfigEntry, subentry: Any
) -> bool:
    """Stop coordinator when a device subentry is removed."""
    await _async_update_registries(hass)

    coordinator: OUPESWiFiCoordinator | None = (
        hass.data[DOMAIN][entry.entry_id]["coordinators"].pop(subentry.subentry_id, None)
    )
    if coordinator:
        coordinator.stop()

    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """React to config entry changes (options updates, subentry add/remove).

    NOTE: add_update_listener fires for ANY config entry mutation, including
    subentry additions/removals — not just options changes.  We handle each
    case differently to avoid a full reload when only subentries changed.
    """
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if entry_data is None:
        return

    # If options genuinely changed (e.g. port number) do a full reload.
    if dict(entry.options) != entry_data.get("_cached_options", {}):
        await hass.config_entries.async_reload(entry.entry_id)
        return

    # Options are unchanged — handle subentry additions/removals incrementally
    # so that existing coordinators are not disrupted.
    await _async_update_registries(hass)

    known_ids: set[str] = set(entry_data["coordinators"].keys())
    current_ids: set[str] = set(entry.subentries.keys())

    # Tear down coordinators for removed subentries (entity registry is
    # already cleared by HA's async_remove_subentry).
    for subentry_id in known_ids - current_ids:
        coordinator: OUPESWiFiCoordinator | None = entry_data["coordinators"].pop(
            subentry_id, None
        )
        if coordinator is not None:
            coordinator.stop()

    # Create coordinators and register entities for newly-added subentries.
    for subentry_id in current_ids - known_ids:
        subentry = entry.subentries[subentry_id]
        coordinator = _coordinator_for_subentry(hass, entry, subentry)
        entry_data["coordinators"][subentry_id] = coordinator
        coordinator.start()
        for fn in entry_data.get("add_device_fns", {}).values():
            fn(coordinator, subentry)

    # Apply in-place updates for subentries whose data changed but were not
    # added or removed (e.g. runtime_max changed via Reconfigure).
    for subentry_id in known_ids & current_ids:
        coord = entry_data["coordinators"].get(subentry_id)
        if coord is None:
            continue
        subentry = entry.subentries[subentry_id]
        new_runtime_max = subentry.data.get(CONF_RUNTIME_MAX, ATTR78_RUNTIME_MAX)
        if coord._runtime_max != new_runtime_max:
            coord._runtime_max = new_runtime_max
            _LOGGER.debug(
                "OUPES WiFi: updated runtime_max for %s → %d min",
                coord.device_id,
                new_runtime_max,
            )
