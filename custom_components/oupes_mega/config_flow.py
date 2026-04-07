"""Config flow for OUPES Mega integration.

Supports two paths:
  1. Automatic — HA's bluetooth scanner finds a 'TT' device and calls
     async_step_bluetooth(); the user just confirms.
  2. Manual   — user picks 'Add Integration > OUPES Mega' and types the
     Bluetooth MAC address.

Both paths offer to log in to the OUPES (Cleanergy) cloud to automatically
fetch the per-device ``device_key``.  The user can also enter a manually
captured key instead.  Credentials are used only for the one-time key fetch
and are **not** stored in the config entry.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.bluetooth import (
    BluetoothServiceInfo,
    async_discovered_service_info,
)
from homeassistant.data_entry_flow import FlowResult

from .cloud_api import async_cloud_login, async_fetch_device_key
from .const import (
    CONF_ADDRESS,
    CONF_CONTINUOUS,
    CONF_DEBUG_ATTRS,
    CONF_DEBUG_RAW,
    CONF_DEVICE_ID,
    CONF_DEVICE_KEY,
    CONF_NAME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

_HEX_CHARS = frozenset("0123456789abcdefABCDEF")


def _valid_device_key(key: str) -> bool:
    return len(key) == 10 and all(c in _HEX_CHARS for c in key)


def _extract_device_id(manufacturer_data: dict[int, bytes]) -> str | None:
    """Extract the 20-char hex device_id from BLE manufacturer data.

    The OUPES device uses a pseudo BLE company_id where the high byte is
    actually the first byte of ``device_id`` and the low byte toggles
    between 0x00 and 0x01 (pairing flag).  The payload after the company_id
    starts at device_id byte 2:

        payload[0:9]   → device_id bytes 2-10
        payload[9:15]  → device_product_id (6 ASCII bytes)
        payload[15:21] → reversed MAC
    """
    for company_id, payload in manufacturer_data.items():
        if len(payload) < 21:
            continue
        # Low byte of company_id is a flag (0 or 1); high byte is device_id[0]
        flag = company_id & 0xFF
        if flag > 1:
            continue
        first_byte = (company_id >> 8) & 0xFF
        device_id = bytes([first_byte]) + payload[0:9]
        return device_id.hex()
    return None


def _extract_device_id_for_address(
    hass, address: str
) -> str | None:
    """Search HA's known BLE advertisements for a device_id by MAC."""
    for svc in async_discovered_service_info(hass):
        if svc.address.upper() == address.upper():
            did = _extract_device_id(svc.manufacturer_data)
            if did:
                return did
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
        self._discovered_address: str | None = None
        self._discovered_name: str | None = None
        self._discovered_device_id: str | None = None
        self._auto_fetched_key: str | None = None

    # ── Automatic bluetooth discovery ─────────────────────────────────────────

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfo
    ) -> FlowResult:
        """Invoked automatically when HA discovers a 'TT' BLE advertisement."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        self._discovered_address = discovery_info.address
        self._discovered_name = discovery_info.name or "OUPES Mega"
        self._discovered_device_id = _extract_device_id(
            discovery_info.manufacturer_data
        )

        # Show device details and ask for confirmation before adding
        self.context["title_placeholders"] = {
            "name": self._discovered_name,
            "address": self._discovered_address,
        }
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm adding the auto-discovered device."""
        errors: dict[str, str] = {}
        if user_input is not None:
            raw_key = user_input.get(CONF_DEVICE_KEY, "").strip()
            cloud_email = user_input.get("cloud_email", "").strip()
            cloud_password = user_input.get("cloud_password", "").strip()

            if raw_key and not _valid_device_key(raw_key):
                errors[CONF_DEVICE_KEY] = "invalid_device_key"

            # If no manual key provided, try cloud login to fetch it
            if not raw_key and not errors and cloud_email and cloud_password:
                raw_key = await _cloud_fetch_key(
                    self.hass,
                    cloud_email,
                    cloud_password,
                    self._discovered_device_id,
                    self._discovered_address,
                )
                if not raw_key:
                    errors["cloud_email"] = "cloud_login_failed"

            if not errors:
                final_key = raw_key or self._auto_fetched_key or ""
                if not final_key:
                    errors["base"] = "no_device_key"
                else:
                    return self.async_create_entry(
                        title=self._discovered_name,
                        data={
                            CONF_ADDRESS: self._discovered_address,
                            CONF_NAME: self._discovered_name,
                            CONF_DEVICE_KEY: final_key,
                            CONF_DEVICE_ID: self._discovered_device_id or "",
                        },
                    )

        device_id_display = self._discovered_device_id or "not detected"
        prefilled_key = self._auto_fetched_key or ""
        return self.async_show_form(
            step_id="bluetooth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Optional("cloud_email", default=""): str,
                    vol.Optional("cloud_password", default=""): str,
                    vol.Optional(CONF_DEVICE_KEY, default=prefilled_key): str,
                }
            ),
            description_placeholders={
                "name": self._discovered_name,
                "address": self._discovered_address,
                "device_id": device_id_display,
            },
            errors=errors,
        )

    # ── Manual setup ─────────────────────────────────────────────────────────

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle a user-initiated config flow (manual MAC entry)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            address = user_input[CONF_ADDRESS].upper().strip()
            name = (user_input.get(CONF_NAME) or "OUPES Mega").strip()
            raw_key = user_input.get(CONF_DEVICE_KEY, "").strip()
            cloud_email = user_input.get("cloud_email", "").strip()
            cloud_password = user_input.get("cloud_password", "").strip()

            # Basic MAC address format validation
            parts = address.split(":")
            if len(parts) != 6 or not all(len(p) == 2 for p in parts):
                errors[CONF_ADDRESS] = "invalid_address"
            elif raw_key and not _valid_device_key(raw_key):
                errors[CONF_DEVICE_KEY] = "invalid_device_key"
            else:
                # Try to extract device_id from known BLE advertisements
                device_id = _extract_device_id_for_address(self.hass, address)

                # If no manual key, try cloud login
                if not raw_key and cloud_email and cloud_password:
                    raw_key = await _cloud_fetch_key(
                        self.hass,
                        cloud_email,
                        cloud_password,
                        device_id,
                        address,
                    )
                    if not raw_key:
                        errors["cloud_email"] = "cloud_login_failed"

                if not errors:
                    if not raw_key:
                        errors["base"] = "no_device_key"
                    else:
                        await self.async_set_unique_id(address)
                        self._abort_if_unique_id_configured()
                        return self.async_create_entry(
                            title=name,
                            data={
                                CONF_ADDRESS: address,
                                CONF_NAME: name,
                                CONF_DEVICE_KEY: raw_key,
                                CONF_DEVICE_ID: device_id or "",
                            },
                        )

        # Pre-fill with any 'TT' device already seen by HA's bluetooth scanner
        discovered = [
            svc
            for svc in async_discovered_service_info(self.hass)
            if (svc.name or "").strip().upper() == "TT"
        ]
        suggested_address = discovered[0].address if discovered else ""

        schema = vol.Schema(
            {
                vol.Required(CONF_ADDRESS, default=suggested_address): str,
                vol.Optional(CONF_NAME, default="OUPES Mega"): str,
                vol.Optional("cloud_email", default=""): str,
                vol.Optional("cloud_password", default=""): str,
                vol.Optional(CONF_DEVICE_KEY, default=""): str,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )


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
            cloud_email = user_input.get("cloud_email", "").strip()
            cloud_password = user_input.get("cloud_password", "").strip()

            if raw_key and not _valid_device_key(raw_key):
                errors[CONF_DEVICE_KEY] = "invalid_device_key"

            # If no manual key and credentials supplied, try cloud fetch
            if not raw_key and not errors and cloud_email and cloud_password:
                raw_key = await _cloud_fetch_key(
                    self.hass,
                    cloud_email,
                    cloud_password,
                    self._entry.data.get(CONF_DEVICE_ID),
                    self._entry.data.get(CONF_ADDRESS),
                )
                if raw_key:
                    _LOGGER.info(
                        "Fetched device_key via cloud login for %s",
                        self._entry.data.get(CONF_ADDRESS),
                    )
                else:
                    errors["cloud_email"] = "cloud_login_failed"

            if not errors:
                # Don't store cloud credentials — only the fetched key
                opts = {
                    k: v
                    for k, v in user_input.items()
                    if k not in ("cloud_email", "cloud_password")
                }
                if raw_key:
                    opts[CONF_DEVICE_KEY] = raw_key
                return self.async_create_entry(title="", data=opts)

        current_key = (
            self._entry.options.get(CONF_DEVICE_KEY)
            or self._entry.data.get(CONF_DEVICE_KEY)
            or ""
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional("cloud_email", default=""): str,
                    vol.Optional("cloud_password", default=""): str,
                    vol.Optional(CONF_DEVICE_KEY, default=current_key): str,
                    vol.Required(
                        CONF_CONTINUOUS,
                        default=self._entry.options.get(CONF_CONTINUOUS, False),
                    ): bool,
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
