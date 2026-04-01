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

from .const import CONF_ADDRESS, CONF_NAME, DOMAIN

_LOGGER = logging.getLogger(__name__)


class OUPESMegaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for OUPES Mega."""

    VERSION = 1

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
        if user_input is not None:
            return self.async_create_entry(
                title=self._discovered_name,
                data={
                    CONF_ADDRESS: self._discovered_address,
                    CONF_NAME: self._discovered_name,
                },
            )

        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={
                "name": self._discovered_name,
                "address": self._discovered_address,
            },
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

            # Basic MAC address format validation
            parts = address.split(":")
            if len(parts) != 6 or not all(len(p) == 2 for p in parts):
                errors[CONF_ADDRESS] = "invalid_address"
            else:
                await self.async_set_unique_id(address)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=name,
                    data={CONF_ADDRESS: address, CONF_NAME: name},
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
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )
