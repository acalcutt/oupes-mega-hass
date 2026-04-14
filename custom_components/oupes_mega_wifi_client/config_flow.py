"""Config flow for the OUPES Mega WiFi Client integration.

Step 1 (user):  Enter proxy host/port + email/password.
Step 2 (select_device):  Authenticate via the proxy's HTTP API, fetch the
        device list, and let the user pick which device to monitor.
Each config entry = one device on the proxy.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_KEY,
    CONF_DEVICE_NAME,
    CONF_EMAIL,
    CONF_HOST,
    CONF_HTTP_PORT,
    CONF_PASSWORD,
    CONF_PRODUCT_ID,
    CONF_TCP_PORT,
    CONF_TOKEN,
    DEFAULT_HOST,
    DEFAULT_HTTP_PORT,
    DEFAULT_TCP_PORT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class OUPESWiFiClientConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for OUPES Mega WiFi Client."""

    VERSION = 1

    def __init__(self) -> None:
        self._host: str = DEFAULT_HOST
        self._tcp_port: int = DEFAULT_TCP_PORT
        self._http_port: int = DEFAULT_HTTP_PORT
        self._email: str = ""
        self._password: str = ""
        self._token: str = ""
        self._devices: list[dict] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            self._host = user_input[CONF_HOST].strip()
            self._tcp_port = int(user_input[CONF_TCP_PORT])
            self._http_port = int(user_input[CONF_HTTP_PORT])
            self._email = user_input[CONF_EMAIL].strip().lower()
            self._password = user_input[CONF_PASSWORD]

            # Try to authenticate and fetch devices
            try:
                self._token, self._devices = await self._login_and_list_devices()
            except aiohttp.ClientError as exc:
                _LOGGER.error("Connection error: %s", exc)
                errors["base"] = "cannot_connect"
            except AuthError as exc:
                _LOGGER.error("Auth error: %s", exc)
                errors["base"] = "invalid_auth"
            except Exception as exc:  # noqa: BLE001
                _LOGGER.exception("Unexpected error: %s", exc)
                errors["base"] = "unknown"

            if not errors:
                if self._devices:
                    return await self.async_step_select_device()
                errors["base"] = "no_devices"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=self._host): TextSelector(),
                    vol.Required(CONF_TCP_PORT, default=self._tcp_port): NumberSelector(
                        NumberSelectorConfig(
                            min=1, max=65535, step=1, mode=NumberSelectorMode.BOX
                        )
                    ),
                    vol.Required(CONF_HTTP_PORT, default=self._http_port): NumberSelector(
                        NumberSelectorConfig(
                            min=1, max=65535, step=1, mode=NumberSelectorMode.BOX
                        )
                    ),
                    vol.Required(CONF_EMAIL, default=self._email): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.EMAIL)
                    ),
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_select_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            device_id = user_input["device"]
            # Find the selected device
            dev = next(
                (d for d in self._devices if d.get("device_id") == device_id),
                None,
            )
            if dev is None:
                return self.async_abort(reason="device_not_found")

            # Ensure unique per device_id
            await self.async_set_unique_id(f"{DOMAIN}_{device_id}")
            self._abort_if_unique_id_configured()

            device_name = dev.get("name") or dev.get("device_name") or "OUPES"
            return self.async_create_entry(
                title=f"{device_name} (WiFi)",
                data={
                    CONF_HOST: self._host,
                    CONF_TCP_PORT: self._tcp_port,
                    CONF_HTTP_PORT: self._http_port,
                    CONF_EMAIL: self._email,
                    CONF_PASSWORD: self._password,
                    CONF_TOKEN: self._token,
                    CONF_DEVICE_ID: device_id,
                    CONF_DEVICE_KEY: dev.get("device_key", ""),
                    CONF_DEVICE_NAME: device_name,
                    CONF_PRODUCT_ID: dev.get("device_product_id", ""),
                },
            )

        # Build option list
        options = []
        for dev in self._devices:
            did = dev.get("device_id", "unknown")
            name = dev.get("name") or dev.get("device_name") or "OUPES"
            label = f"{name}  ({did})"
            options.append({"value": did, "label": label})

        return self.async_show_form(
            step_id="select_device",
            data_schema=vol.Schema(
                {
                    vol.Required("device"): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
        )

    async def _login_and_list_devices(self) -> tuple[str, list[dict]]:
        """Authenticate with the proxy HTTP API and return (token, device_list)."""
        base_url = f"http://{self._host}:{self._http_port}"
        async with aiohttp.ClientSession() as session:
            # Login
            async with session.post(
                f"{base_url}/api/app/user/login",
                json={
                    "mail": self._email,
                    "passwd": self._password,
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json()
                if data.get("ret") != 1:
                    raise AuthError(data.get("desc", "Login failed"))
                info = data.get("info", {})
                token = info.get("token", "")
                if not token:
                    raise AuthError("No token in login response")

            # Fetch device list
            async with session.get(
                f"{base_url}/api/app/device/list",
                params={"token": token},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json()
                info = data.get("info", {})
                devices = info.get("bind", [])

        return token, devices


class AuthError(Exception):
    """Authentication failed."""
