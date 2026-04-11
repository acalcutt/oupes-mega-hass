"""Config flow for OUPES Mega integration.

Supports two entry points:
  1. Automatic — HA's bluetooth scanner finds a 'TT' device and calls
     async_step_bluetooth(); the user confirms.
  2. Manual   — user picks 'Add Integration > OUPES Mega' and types the
     Bluetooth MAC address.

After identifying the device, both paths present a method-selection step
with three options for providing the device_key:
  A. Create new key  — factory-reset the device and pair over BLE
  B. Existing key    — enter a known key manually
  C. Cloud login     — fetch the key from the Cleanergy cloud
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.bluetooth import (
    BluetoothServiceInfo,
    async_discovered_service_info,
)
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .ble_pairing import PairingResult, async_pair_device
from .cloud_api import async_cloud_login, async_fetch_device_key
from .const import (
    CONF_ADDRESS,
    CONF_CONTINUOUS,
    CONF_DEBUG_ATTRS,
    CONF_DEBUG_RAW,
    CONF_DEVICE_ID,
    CONF_DEVICE_KEY,
    CONF_NAME,
    CONF_POLL_INTERVAL,
    CONF_PRODUCT_ID,
    CONF_STALE_TIMEOUT,
    DOMAIN,
    STALE_TIMEOUT,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

_HEX_CHARS = frozenset("0123456789abcdefABCDEF")

KEY_METHOD_CREATE = "create_new_key"
KEY_METHOD_EXISTING = "existing_key"
KEY_METHOD_CLOUD = "cloud_login"


def _valid_device_key(key: str) -> bool:
    return len(key) == 10 and all(c in _HEX_CHARS for c in key)


def _generate_device_key() -> str:
    """Generate a random 10-character hex device key."""
    return secrets.token_hex(5)


def _extract_device_id(manufacturer_data: dict[int, bytes]) -> str | None:
    """Extract the 20-char hex device_id from BLE manufacturer data."""
    for company_id, payload in manufacturer_data.items():
        if len(payload) < 21:
            continue
        flag = company_id & 0xFF
        if flag > 1:
            continue
        first_byte = (company_id >> 8) & 0xFF
        device_id = bytes([first_byte]) + payload[0:9]
        return device_id.hex()
    return None


def _extract_product_id(manufacturer_data: dict[int, bytes]) -> str | None:
    """Extract the 6-char ASCII product_id from BLE manufacturer data.

    The product_id occupies bytes 11-16 of the raw manufacturer AD payload,
    which maps to payload[9:15] after Bleak splits out the 2-byte company_id.
    """
    for company_id, payload in manufacturer_data.items():
        if len(payload) < 15:
            continue
        flag = company_id & 0xFF
        if flag > 1:
            continue
        try:
            pid = payload[9:15].decode("ascii")
            if pid.isprintable() and len(pid) == 6:
                return pid
        except (UnicodeDecodeError, ValueError):
            pass
    return None


def _extract_device_id_for_address(hass, address: str) -> str | None:
    """Search HA's known BLE advertisements for a device_id by MAC."""
    for svc in async_discovered_service_info(hass):
        if svc.address.upper() == address.upper():
            did = _extract_device_id(svc.manufacturer_data)
            if did:
                return did
    return None


def _extract_product_id_for_address(hass, address: str) -> str | None:
    """Search HA's known BLE advertisements for a product_id by MAC."""
    for svc in async_discovered_service_info(hass):
        if svc.address.upper() == address.upper():
            pid = _extract_product_id(svc.manufacturer_data)
            if pid:
                return pid
    return None


class OUPESMegaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for OUPES Mega."""

    VERSION = 1

    @staticmethod
    @config_entries.callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "OUPESMegaOptionsFlow":
        return OUPESMegaOptionsFlow(config_entry)

    def __init__(self) -> None:
        self._address: str | None = None
        self._name: str | None = None
        self._device_id: str | None = None
        self._product_id: str | None = None
        self._pairing_key: str | None = None
        self._pairing_task: asyncio.Task | None = None
        self._pairing_error: str | None = None

    # ── Automatic bluetooth discovery ─────────────────────────────────────

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfo
    ) -> FlowResult:
        """Invoked when HA discovers a 'TT' BLE advertisement."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        self._address = discovery_info.address
        self._name = discovery_info.name or "OUPES Mega"
        self._device_id = _extract_device_id(discovery_info.manufacturer_data)
        self._product_id = _extract_product_id(discovery_info.manufacturer_data)

        self.context["title_placeholders"] = {
            "name": self._name,
            "address": self._address,
        }
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm the auto-discovered device, then go to method selection."""
        if user_input is not None:
            return await self.async_step_choose_method()

        device_id_display = self._device_id or "not detected"
        return self.async_show_form(
            step_id="bluetooth_confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "name": self._name,
                "address": self._address,
                "device_id": device_id_display,
            },
        )

    # ── Manual setup ──────────────────────────────────────────────────────

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """User-initiated config flow (manual MAC entry)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            address = user_input[CONF_ADDRESS].upper().strip()
            name = (user_input.get(CONF_NAME) or "OUPES Mega").strip()

            parts = address.split(":")
            if len(parts) != 6 or not all(len(p) == 2 for p in parts):
                errors[CONF_ADDRESS] = "invalid_address"
            else:
                self._address = address
                self._name = name
                self._device_id = _extract_device_id_for_address(
                    self.hass, address
                )
                self._product_id = _extract_product_id_for_address(
                    self.hass, address
                )
                return await self.async_step_choose_method()

        discovered = [
            svc
            for svc in async_discovered_service_info(self.hass)
            if (svc.name or "").strip().upper() == "TT"
        ]
        suggested_address = discovered[0].address if discovered else ""

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS, default=suggested_address): str,
                    vol.Optional(CONF_NAME, default="OUPES Mega"): str,
                }
            ),
            errors=errors,
        )

    # ── Method selection ──────────────────────────────────────────────────

    async def async_step_choose_method(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Let the user pick how to provide the device key."""
        if user_input is not None:
            method = user_input["key_method"]
            if method == KEY_METHOD_CREATE:
                return await self.async_step_create_key()
            if method == KEY_METHOD_EXISTING:
                return await self.async_step_existing_key()
            if method == KEY_METHOD_CLOUD:
                return await self.async_step_cloud_login()

        return self.async_show_form(
            step_id="choose_method",
            data_schema=vol.Schema(
                {
                    vol.Required("key_method", default=KEY_METHOD_CREATE): vol.In(
                        {
                            KEY_METHOD_CREATE: "Create a new device key (requires factory reset)",
                            KEY_METHOD_EXISTING: "Enter an existing device key",
                            KEY_METHOD_CLOUD: "Fetch key from Cleanergy cloud account",
                        }
                    ),
                }
            ),
            description_placeholders={
                "name": self._name,
                "address": self._address,
            },
        )

    # ── Method A: Create new key via BLE pairing ─────────────────────────

    async def async_step_create_key(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Collect the device key, then start pairing via progress step."""
        errors: dict[str, str] = {}

        # If we're returning here after a failed pairing, show the error.
        if self._pairing_error:
            errors["base"] = self._pairing_error
            self._pairing_error = None

        if user_input is not None:
            new_key = user_input.get(CONF_DEVICE_KEY, "").strip().lower()
            if not new_key:
                new_key = _generate_device_key()
            elif not _valid_device_key(new_key):
                errors[CONF_DEVICE_KEY] = "invalid_device_key"

            if not errors:
                self._pairing_key = new_key
                return await self.async_step_pairing()

        suggested_key = self._pairing_key or _generate_device_key()
        return self.async_show_form(
            step_id="create_key",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_DEVICE_KEY, default=suggested_key): str,
                }
            ),
            description_placeholders={
                "name": self._name,
                "address": self._address,
            },
            errors=errors,
        )

    async def async_step_pairing(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Run BLE pairing in the background while showing a progress spinner."""
        if self._pairing_task is None:
            self._pairing_task = self.hass.async_create_task(
                async_pair_device(
                    self.hass, self._address, self._pairing_key
                ),
            )

        if not self._pairing_task.done():
            return self.async_show_progress(
                step_id="pairing",
                progress_action="pairing",
                progress_task=self._pairing_task,
                description_placeholders={
                    "name": self._name,
                    "address": self._address,
                },
            )

        try:
            result = self._pairing_task.result()
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "Unexpected error during BLE pairing with %s",
                self._address,
            )
            result = PairingResult.CONNECTION_FAILED
        finally:
            self._pairing_task = None

        if result == PairingResult.SUCCESS:
            return self.async_show_progress_done(
                next_step_id="pairing_complete"
            )

        # Map failure to error key and go back to create_key form
        if result == PairingResult.DEVICE_NOT_FOUND:
            self._pairing_error = "pairing_device_not_found"
        elif result == PairingResult.CONNECTION_FAILED:
            self._pairing_error = "pairing_connection_failed"
        else:
            self._pairing_error = "pairing_timeout"

        return self.async_show_progress_done(
            next_step_id="create_key"
        )

    async def async_step_pairing_complete(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Pairing succeeded — proceed to connection settings."""
        return await self.async_step_connection_settings()

    # ── Method B: Enter existing key ─────────────────────────────────────

    async def async_step_existing_key(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manual entry of an already-known device key."""
        errors: dict[str, str] = {}

        if user_input is not None:
            raw_key = user_input.get(CONF_DEVICE_KEY, "").strip().lower()
            if not _valid_device_key(raw_key):
                errors[CONF_DEVICE_KEY] = "invalid_device_key"
            else:
                self._pairing_key = raw_key
                return await self.async_step_connection_settings()

        return self.async_show_form(
            step_id="existing_key",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DEVICE_KEY): str,
                }
            ),
            description_placeholders={
                "name": self._name,
                "address": self._address,
            },
            errors=errors,
        )

    # ── Method C: Cloud login ────────────────────────────────────────────

    async def async_step_cloud_login(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Fetch the device key from the Cleanergy cloud API."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input.get("cloud_email", "").strip()
            password = user_input.get("cloud_password", "").strip()

            if not email or not password:
                errors["cloud_email"] = "cloud_login_failed"
            else:
                key = await _cloud_fetch_key(
                    self.hass,
                    email,
                    password,
                    self._device_id,
                    self._address,
                )
                if key:
                    self._pairing_key = key
                    return await self.async_step_connection_settings()
                errors["cloud_email"] = "cloud_login_failed"

        return self.async_show_form(
            step_id="cloud_login",
            data_schema=vol.Schema(
                {
                    vol.Required("cloud_email"): str,
                    vol.Required("cloud_password"): str,
                }
            ),
            description_placeholders={
                "name": self._name,
                "address": self._address,
            },
            errors=errors,
        )


    # ── Connection settings (final step before entry creation) ──────────

    async def async_step_connection_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Let the user choose connection mode before creating the entry."""
        if user_input is not None:
            continuous = user_input.get(CONF_CONTINUOUS, True)
            poll_interval = int(user_input.get(
                CONF_POLL_INTERVAL, UPDATE_INTERVAL.total_seconds()
            ))
            stale_timeout = int(user_input.get(
                CONF_STALE_TIMEOUT, STALE_TIMEOUT.total_seconds() // 60
            ))
            debug_attrs = user_input.get(CONF_DEBUG_ATTRS, False)
            debug_raw = user_input.get(CONF_DEBUG_RAW, False)
            await self.async_set_unique_id(self._address)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=self._name,
                data={
                    CONF_ADDRESS: self._address,
                    CONF_NAME: self._name,
                    CONF_DEVICE_KEY: self._pairing_key,
                    CONF_DEVICE_ID: self._device_id or "",
                    CONF_PRODUCT_ID: self._product_id or "",
                },
                options={
                    CONF_CONTINUOUS: continuous,
                    CONF_POLL_INTERVAL: poll_interval,
                    CONF_STALE_TIMEOUT: stale_timeout,
                    CONF_DEBUG_ATTRS: debug_attrs,
                    CONF_DEBUG_RAW: debug_raw,
                },
            )

        default_poll = int(UPDATE_INTERVAL.total_seconds())
        default_stale = int(STALE_TIMEOUT.total_seconds() // 60)

        return self.async_show_form(
            step_id="connection_settings",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_CONTINUOUS, default=True): bool,
                    vol.Optional(
                        CONF_POLL_INTERVAL, default=default_poll,
                    ): NumberSelector(NumberSelectorConfig(
                        min=10, max=600, step=1, mode=NumberSelectorMode.BOX,
                        unit_of_measurement="seconds",
                    )),
                    vol.Optional(
                        CONF_STALE_TIMEOUT, default=default_stale,
                    ): NumberSelector(NumberSelectorConfig(
                        min=1, max=120, step=1, mode=NumberSelectorMode.BOX,
                        unit_of_measurement="minutes",
                    )),
                    vol.Required(CONF_DEBUG_ATTRS, default=False): bool,
                    vol.Required(CONF_DEBUG_RAW, default=False): bool,
                }
            ),
            description_placeholders={
                "name": self._name,
                "address": self._address,
            },
        )


# ── Helper ────────────────────────────────────────────────────────────────────

async def _cloud_fetch_key(
    hass,
    email: str,
    password: str,
    device_id: str | None,
    mac_address: str | None,
) -> str | None:
    """Log in to the OUPES cloud and fetch the device_key in one shot."""
    try:
        token = await async_cloud_login(hass, email, password)
        if not token:
            return None
        return await async_fetch_device_key(
            hass,
            device_id=device_id,
            mac_address=mac_address,
            token=token,
        )
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Cloud key fetch failed", exc_info=True)
        return None


# ── Options flow (unchanged) ──────────────────────────────────────────────────

class OUPESMegaOptionsFlow(config_entries.OptionsFlow):
    """Options flow — lets the user toggle continuous BLE connection mode."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            raw_key = user_input.get(CONF_DEVICE_KEY, "").strip()

            if raw_key and not _valid_device_key(raw_key):
                errors[CONF_DEVICE_KEY] = "invalid_device_key"

            if not errors:
                opts = dict(user_input)
                if raw_key:
                    opts[CONF_DEVICE_KEY] = raw_key
                return self.async_create_entry(title="", data=opts)

        current_key = (
            self._entry.options.get(CONF_DEVICE_KEY)
            or self._entry.data.get(CONF_DEVICE_KEY)
            or ""
        )

        default_poll = int(UPDATE_INTERVAL.total_seconds())
        default_stale = int(STALE_TIMEOUT.total_seconds() // 60)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_DEVICE_KEY, default=current_key): str,
                    vol.Required(
                        CONF_CONTINUOUS,
                        default=self._entry.options.get(CONF_CONTINUOUS, True),
                    ): bool,
                    vol.Optional(
                        CONF_POLL_INTERVAL,
                        default=self._entry.options.get(CONF_POLL_INTERVAL, default_poll),
                    ): NumberSelector(NumberSelectorConfig(
                        min=10, max=600, step=1, mode=NumberSelectorMode.BOX,
                        unit_of_measurement="seconds",
                    )),
                    vol.Optional(
                        CONF_STALE_TIMEOUT,
                        default=self._entry.options.get(CONF_STALE_TIMEOUT, default_stale),
                    ): NumberSelector(NumberSelectorConfig(
                        min=1, max=120, step=1, mode=NumberSelectorMode.BOX,
                        unit_of_measurement="minutes",
                    )),
                    vol.Required(
                        CONF_DEBUG_ATTRS,
                        default=self._entry.options.get(CONF_DEBUG_ATTRS, False),
                    ): bool,
                    vol.Required(
                        CONF_DEBUG_RAW,
                        default=self._entry.options.get(CONF_DEBUG_RAW, False),
                    ): bool,
                }
            ),
            errors=errors,
        )
