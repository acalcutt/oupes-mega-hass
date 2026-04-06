"""Config flow for OUPES Mega integration.

Supports two paths:
  1. Automatic — HA's bluetooth scanner finds a 'TT' device and calls
     async_step_bluetooth(); the user just confirms.
  2. Manual   — user picks 'Add Integration > OUPES Mega' and types the
     Bluetooth MAC address.
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

from .const import CONF_ADDRESS, CONF_CONTINUOUS, CONF_DEBUG_ATTRS, CONF_DEBUG_RAW, CONF_DEVICE_KEY, CONF_NAME, DOMAIN

_LOGGER = logging.getLogger(__name__)

_HEX_CHARS = frozenset("0123456789abcdefABCDEF")


def _valid_device_key(key: str) -> bool:
    return len(key) == 10 and all(c in _HEX_CHARS for c in key)


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

    # ── Automatic bluetooth discovery ─────────────────────────────────────────

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfo
    ) -> FlowResult:
        """Invoked automatically when HA discovers a 'TT' BLE advertisement."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        self._discovered_address = discovery_info.address
        self._discovered_name = discovery_info.name or "OUPES Mega"

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
            if raw_key and not _valid_device_key(raw_key):
                errors[CONF_DEVICE_KEY] = "invalid_device_key"
            else:
                return self.async_create_entry(
                    title=self._discovered_name,
                    data={
                        CONF_ADDRESS: self._discovered_address,
                        CONF_NAME: self._discovered_name,
                        CONF_DEVICE_KEY: raw_key,
                    },
                )

        return self.async_show_form(
            step_id="bluetooth_confirm",
            data_schema=vol.Schema(
                {vol.Optional(CONF_DEVICE_KEY, default=""): str}
            ),
            description_placeholders={
                "name": self._discovered_name,
                "address": self._discovered_address,
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

            # Basic MAC address format validation
            parts = address.split(":")
            if len(parts) != 6 or not all(len(p) == 2 for p in parts):
                errors[CONF_ADDRESS] = "invalid_address"
            elif raw_key and not _valid_device_key(raw_key):
                errors[CONF_DEVICE_KEY] = "invalid_device_key"
            else:
                await self.async_set_unique_id(address)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=name,
                    data={CONF_ADDRESS: address, CONF_NAME: name, CONF_DEVICE_KEY: raw_key},
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
                vol.Optional(CONF_DEVICE_KEY, default=""): str,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )


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
            else:
                return self.async_create_entry(title="", data=user_input)

        # Default: prefer existing options value, fall back to value stored at setup time
        current_key = (
            self._entry.options.get(CONF_DEVICE_KEY)
            or self._entry.data.get(CONF_DEVICE_KEY)
            or ""
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
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
