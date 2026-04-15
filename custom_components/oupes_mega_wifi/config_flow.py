"""Config flow for OUPES Mega WiFi."""
from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigSubentryFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import device_registry as dr
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
    ATTR78_RUNTIME_MAX,
    CONF_DEBUG_HTTP,
    CONF_DEBUG_RAW_LINES,
    CONF_DEBUG_TELEMETRY,
    CONF_DEVICE_ID,
    CONF_DEVICE_KEY,
    CONF_DEVICE_NAME,
    CONF_MAC_ADDRESS,
    CONF_HTTP_PORT,
    CONF_MAIL,
    CONF_BROKER_UID,
    CONF_MODEL_OVERRIDE,
    CONF_PASSWD,
    CONF_PORT,
    CONF_RUNTIME_MAX,
    CONF_SIBO_PORT,
    CONF_UID,
    CONF_VALIDATION_MODE,
    DEFAULT_HTTP_PORT,
    DEFAULT_PORT,
    DEFAULT_SIBO_PORT,
    DEFAULT_VALIDATION_MODE,
    DOMAIN,
    MODEL_CATALOG,
    VALIDATION_ACCEPT_ALL,
    VALIDATION_ACCEPT_REGISTERED,
    VALIDATION_LOG_ONLY,
)


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

class OUPESMegaWiFiConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for OUPES Mega WiFi Proxy."""

    VERSION = 1

    @staticmethod
    @config_entries.callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "OUPESMegaWiFiOptionsFlow":
        return OUPESMegaWiFiOptionsFlow(config_entry)

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: config_entries.ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return sub-entry types supported by this integration."""
        return {"device": OUPESDeviceSubentryFlow}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Create a user account and specify proxy ports."""
        existing_entries = self.hass.config_entries.async_entries(DOMAIN)
        is_secondary_user = len(existing_entries) > 0

        if user_input is not None:
            mail = user_input[CONF_MAIL].strip().lower()
            passwd_hash = hashlib.sha256(
                user_input[CONF_PASSWD].encode()
            ).hexdigest()

            await self.async_set_unique_id(mail)
            self._abort_if_unique_id_configured()

            # Hash the email to create stable numeric UIDs for proxy tracking.
            # CONF_UID   = HTTP API user ID   (info.uid in login response)
            # CONF_BROKER_UID = Broker/mark UID (mark.uid in login response, used
            #                   for device key generation: MD5(str(uid))[:10])
            # Both default to the same derived value; user can override via Reconfigure.
            uid_str = str(int(hashlib.md5(mail.encode()).hexdigest(), 16) % 100000000)

            entry_data = {
                CONF_MAIL: mail,
                CONF_PASSWD: passwd_hash,
                CONF_UID: uid_str,
                CONF_BROKER_UID: uid_str,
            }
            if not is_secondary_user:
                entry_data.update({
                    CONF_PORT: int(user_input.get(CONF_PORT, DEFAULT_PORT)),
                    CONF_HTTP_PORT: int(user_input.get(CONF_HTTP_PORT, DEFAULT_HTTP_PORT)),
                    CONF_SIBO_PORT: int(user_input.get(CONF_SIBO_PORT, DEFAULT_SIBO_PORT)),
                })

            return self.async_create_entry(
                title=mail,
                data=entry_data,
            )

        schema = {
            vol.Required(CONF_MAIL): TextSelector(
                TextSelectorConfig(type=TextSelectorType.EMAIL)
            ),
            vol.Required(CONF_PASSWD): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
        }
        
        if not is_secondary_user:
            schema.update({
                vol.Optional(CONF_PORT, default=DEFAULT_PORT): NumberSelector(
                    NumberSelectorConfig(
                        min=1024, max=65535, step=1, mode=NumberSelectorMode.BOX
                    )
                ),
                vol.Optional(CONF_HTTP_PORT, default=DEFAULT_HTTP_PORT): NumberSelector(
                    NumberSelectorConfig(
                        min=1024, max=65535, step=1, mode=NumberSelectorMode.BOX
                    )
                ),
                vol.Optional(CONF_SIBO_PORT, default=DEFAULT_SIBO_PORT): NumberSelector(
                    NumberSelectorConfig(
                        min=1024, max=65535, step=1, mode=NumberSelectorMode.BOX
                    )
                ),
            })

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(schema),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Allow the user to view and change the UIDs for this account."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            raw_uid = user_input.get(CONF_UID, "").strip()
            raw_broker_uid = user_input.get(CONF_BROKER_UID, "").strip()

            if not raw_uid.isdigit():
                errors[CONF_UID] = "uid_not_numeric"
            if not raw_broker_uid.isdigit():
                errors[CONF_BROKER_UID] = "uid_not_numeric"

            if not errors:
                # Check uniqueness of broker_uid across other entries (it's the key
                # that actually matters for device key generation).
                for other in self.hass.config_entries.async_entries(DOMAIN):
                    if other.entry_id == entry.entry_id:
                        continue
                    other_broker = str(
                        other.data.get(CONF_BROKER_UID)
                        or other.data.get(CONF_UID, "")
                    )
                    if other_broker == raw_broker_uid:
                        errors[CONF_BROKER_UID] = "uid_already_used"
                        break

            if not errors:
                return self.async_update_and_abort(
                    entry,
                    data={**entry.data, CONF_UID: raw_uid, CONF_BROKER_UID: raw_broker_uid},
                )

        current_uid = str(entry.data.get(CONF_UID, ""))
        current_broker_uid = str(
            entry.data.get(CONF_BROKER_UID) or entry.data.get(CONF_UID, "")
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_BROKER_UID, default=current_broker_uid): TextSelector(
                        TextSelectorConfig(autocomplete="off")
                    ),
                    vol.Required(CONF_UID, default=current_uid): TextSelector(
                        TextSelectorConfig(autocomplete="off")
                    ),
                }
            ),
            description_placeholders={"mail": entry.data.get(CONF_MAIL, "")},
            errors=errors,
        )


class OUPESMegaWiFiOptionsFlow(config_entries.OptionsFlow):
    """Options flow � change port or validation modes."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_port = self._entry.options.get(
            CONF_PORT, self._entry.data.get(CONF_PORT, DEFAULT_PORT)
        )
        current_http_port = int(
            self._entry.options.get(CONF_HTTP_PORT, DEFAULT_HTTP_PORT)
        )
        current_sibo_port = int(
            self._entry.options.get(CONF_SIBO_PORT, DEFAULT_SIBO_PORT)
        )
        current_mode = self._entry.options.get(
            CONF_VALIDATION_MODE, DEFAULT_VALIDATION_MODE
        )
        current_debug_raw = self._entry.options.get(CONF_DEBUG_RAW_LINES, False)
        current_debug_tel = self._entry.options.get(CONF_DEBUG_TELEMETRY, False)
        current_debug_http = self._entry.options.get(CONF_DEBUG_HTTP, False)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_PORT, default=current_port): NumberSelector(
                        NumberSelectorConfig(
                            min=1024, max=65535, step=1, mode=NumberSelectorMode.BOX
                        )
                    ),
                    vol.Optional(CONF_HTTP_PORT, default=current_http_port): NumberSelector(
                        NumberSelectorConfig(
                            min=1024, max=65535, step=1, mode=NumberSelectorMode.BOX
                        )
                    ),
                    vol.Optional(CONF_SIBO_PORT, default=current_sibo_port): NumberSelector(
                        NumberSelectorConfig(
                            min=1024, max=65535, step=1, mode=NumberSelectorMode.BOX
                        )
                    ),
                    vol.Optional(
                        CONF_VALIDATION_MODE, default=current_mode
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                VALIDATION_ACCEPT_ALL,
                                VALIDATION_LOG_ONLY,
                                VALIDATION_ACCEPT_REGISTERED,
                            ],
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                    vol.Optional(
                        CONF_DEBUG_RAW_LINES, default=current_debug_raw
                    ): bool,
                    vol.Optional(
                        CONF_DEBUG_TELEMETRY, default=current_debug_tel
                    ): bool,
                    vol.Optional(
                        CONF_DEBUG_HTTP, default=current_debug_http
                    ): bool,
                }
            ),
        )


_OUPES_MEGA_DOMAIN = "oupes_mega_ble"
_SOURCE_GENERATE = "__generate__"
_SOURCE_MANUAL = "__manual__"

class OUPESDeviceSubentryFlow(ConfigSubentryFlow):
    """Sub-entry flow to add a new device to the user account."""

    def __init__(self) -> None:
        self._prefill_device_id: str = ""
        self._prefill_device_key: str = ""
        self._prefill_device_name: str = ""
        self._prefill_mac_address: str = ""
        self._available_ble_entries: list = []
        self._source_method: str = ""
        self._pairing_key: str = ""
        self._pairing_address: str = ""
        self._device_id_save: str = ""
        self._device_name_save: str = ""
        self._pairing_task: asyncio.Task | None = None
        self._pairing_error: str | None = None
        self._wifi_ssid: str = ""
        self._wifi_psk: str = ""
        self._runtime_max: int = ATTR78_RUNTIME_MAX
        self._model_override: str = ""
        self._reconfigure_task: asyncio.Task | None = None
        self._reconfigure_new_data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Entry point � check for existing oupes_mega BLE entries first."""
        ble_entries = self.hass.config_entries.async_entries(_OUPES_MEGA_DOMAIN)
        device_registry = dr.async_get(self.hass)
        
        # Filter out BLE devices we've already imported
        try:
            parent_entry_id = self.handler[0]
            parent_entry = self.hass.config_entries.async_get_entry(parent_entry_id)
        except (AttributeError, IndexError, TypeError):
            parent_entry = None
            
        already_added_ids = []
        if parent_entry and hasattr(parent_entry, "subentries"):
            for sub in parent_entry.subentries.values():
                device_id = sub.data.get(CONF_DEVICE_ID)
                if device_id:
                    already_added_ids.append(device_id)

        self._available_ble_entries = []
        for entry in ble_entries:
            did = entry.data.get(CONF_DEVICE_ID, "")
            if not did or did not in already_added_ids:
                self._available_ble_entries.append(entry)

        if user_input is not None:
            source = user_input.get("device_source", _SOURCE_MANUAL)
            self._source_method = source
            
            if source == _SOURCE_GENERATE or source == _SOURCE_MANUAL:
                if source == _SOURCE_MANUAL:
                    self._force_manual_empty_key = True
                return await self.async_step_discover_ble()
            else:
                # Find the chosen entry and carry over its device_id / device_key.
                for entry in self._available_ble_entries:
                    if entry.entry_id == source:
                        self._prefill_device_id = entry.data.get(CONF_DEVICE_ID, "")
                        self._prefill_device_key = entry.data.get(CONF_DEVICE_KEY, "")
                        devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
                        dev_name = (devices[0].name_by_user or devices[0].name) if devices else ""
                        self._prefill_device_name = dev_name or entry.title or entry.data.get("name", "")
                        
                        # We know oupes_mega BLE component stores the MAC exactly in CONF_ADDRESS
                        self._prefill_mac_address = entry.data.get("address", "")
                        break
                return await self.async_step_credentials()

        # Always show the selection form � HA frontend requires the first step
        # to return a form (direct redirects cause a blank dialog).
        options: list[dict] = [
            {"value": _SOURCE_GENERATE, "label": "Generate new device key (based on User ID)"},
            {"value": _SOURCE_MANUAL, "label": "Enter existing device key / Enter manually"},
        ]
        for entry in self._available_ble_entries:
            devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
            dev_name = (devices[0].name_by_user or devices[0].name) if devices else ""
            name = dev_name or entry.title or entry.data.get("name", entry.entry_id)
            did = entry.data.get(CONF_DEVICE_ID, "")
            label = f"Import BLE Device: {name} ({did})" if did else f"Import BLE Device: {name}"
            options.append({"value": entry.entry_id, "label": label})

        default_val = _SOURCE_GENERATE

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("device_source", default=default_val): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
            description_placeholders={
                "count": str(len(self._available_ble_entries)),
            },
        )

    async def async_step_discover_ble(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Discover nearby BLE devices to optionally pick from."""
        try:
            from homeassistant.components.bluetooth import async_discovered_service_info
        except ImportError:
            # Bluetooth integration not available � skip discovery
            return await self.async_step_credentials()
        
        if user_input is not None:
            choice = user_input.get("discovered_device", _SOURCE_MANUAL)
            if choice != _SOURCE_MANUAL:
                # User chose a discovered device; extract the info
                parts = choice.split("||")
                if len(parts) == 3:
                    self._prefill_mac_address = parts[0]
                    self._prefill_device_name = parts[1]
                    self._prefill_device_id = parts[2]
            
            return await self.async_step_credentials()

        discovered = []
        for svc in async_discovered_service_info(self.hass):
            if (svc.name or "").strip().upper() == "TT":
                did = _extract_device_id(svc.manufacturer_data)
                name = "OUPES Mega"
                addr = svc.address
                if did:
                    val = f"{addr}||{name}||{did}"
                    discovered.append({"value": val, "label": f"{name} ({addr}) - {did}"})

        if not discovered:
            # Skip if none found
            return await self.async_step_credentials()

        options = discovered + [{"value": _SOURCE_MANUAL, "label": "Skip / Enter manually"}]
        return self.async_show_form(
            step_id="discover_ble",
            data_schema=vol.Schema(
                {
                    vol.Optional("discovered_device", default=options[0]["value"]): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Collect device credentials."""
        errors: dict[str, str] = {}
        if self._pairing_error:
            errors["base"] = self._pairing_error
            self._pairing_error = None

        # Calculate the default key based on the broker UID (mirrors official app:
        # createDeviceKey(userId) = MD5(str(broker_uid))[:10])
        # Use CONF_BROKER_UID if set; fall back to CONF_UID for backwards compatibility.
        try:
            parent_entry = self.hass.config_entries.async_get_entry(self.handler[0])
            broker_uid_str = (
                parent_entry.data.get(CONF_BROKER_UID, "")
                or parent_entry.data.get(CONF_UID, "")
            )
            if not broker_uid_str:
                mail = parent_entry.data.get(CONF_MAIL, "unknown")
                broker_uid_str = str(int(hashlib.md5(mail.encode()).hexdigest(), 16) % 100000000)
            default_key = hashlib.md5(str(broker_uid_str).encode()).hexdigest()[:10]
        except (AttributeError, IndexError, TypeError):
            default_key = ""

        if getattr(self, "_force_manual_empty_key", False):
            default_key = ""

        if user_input is not None:
            device_id = user_input.get(CONF_DEVICE_ID, "").strip()
            device_name = user_input.get(CONF_DEVICE_NAME, "").strip()
            title = device_name if device_name else device_id
            mac_address = user_input.get(CONF_MAC_ADDRESS, "").strip()
            device_key = user_input.get(CONF_DEVICE_KEY, "").strip()

            self._runtime_max = int(user_input.get(CONF_RUNTIME_MAX, ATTR78_RUNTIME_MAX))
            self._model_override = user_input.get(CONF_MODEL_OVERRIDE, "")

            if self._source_method == _SOURCE_GENERATE:
                if not mac_address:
                    errors["base"] = "missing_mac_for_pairing"
                else:
                    self._pairing_key = device_key
                    self._pairing_address = mac_address
                    self._device_id_save = device_id
                    self._device_name_save = device_name
                    self._wifi_ssid = user_input.get("wifi_ssid", "").strip()
                    self._wifi_psk = user_input.get("wifi_psk", "").strip()
                    return await self.async_step_prepare_pairing()

            if not errors:
                return self.async_create_entry(
                    title=title,
                    data={
                        CONF_DEVICE_ID: device_id,
                        CONF_DEVICE_KEY: device_key,
                        CONF_DEVICE_NAME: device_name,
                        CONF_MAC_ADDRESS: mac_address,
                        CONF_RUNTIME_MAX: self._runtime_max,
                        CONF_MODEL_OVERRIDE: self._model_override,
                    },
                )

        show_wifi = self._source_method == _SOURCE_GENERATE
        schema_fields = {
            vol.Required(CONF_DEVICE_NAME, default=self._prefill_device_name): TextSelector(
                TextSelectorConfig(autocomplete="off")
            ),
            vol.Required(CONF_DEVICE_ID, default=self._prefill_device_id): TextSelector(
                TextSelectorConfig(autocomplete="off")
            ),
            vol.Required(
                CONF_DEVICE_KEY, default=self._prefill_device_key or default_key
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="new-password")),
            vol.Optional(CONF_MAC_ADDRESS, default=self._prefill_mac_address): TextSelector(
                TextSelectorConfig(autocomplete="off")
            ),
            vol.Optional(
                CONF_RUNTIME_MAX, default=self._runtime_max
            ): NumberSelector(NumberSelectorConfig(
                min=100, max=50000, step=60, mode=NumberSelectorMode.BOX,
                unit_of_measurement="minutes",
            )),
            vol.Optional(CONF_MODEL_OVERRIDE, default=""): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        {"value": "", "label": "Auto-detect from device"},
                        *[
                            {"value": pid, "label": name}
                            for pid, (name, _) in sorted(
                                MODEL_CATALOG.items(), key=lambda x: x[1][0]
                            )
                        ],
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
        }
        if show_wifi:
            # WiFi credentials are required for generate mode (the device needs
            # them to connect to the broker after pairing).
            schema_fields[vol.Required("wifi_ssid", default=self._wifi_ssid)] = TextSelector(
                TextSelectorConfig(autocomplete="off")
            )
            schema_fields[vol.Required("wifi_psk", default=self._wifi_psk)] = TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="new-password")
            )
        schema = vol.Schema(schema_fields)

        return self.async_show_form(
            step_id="credentials",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_prepare_pairing(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show factory reset instructions before starting BLE pairing."""
        if user_input is not None:
            return await self.async_step_pairing()

        return self.async_show_form(
            step_id="prepare_pairing",
            data_schema=vol.Schema({}),
        )

    async def async_step_pairing(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Run BLE pairing in the background while showing a progress spinner."""
        if self._pairing_task is None:
            try:
                from custom_components.oupes_mega_ble.ble_pairing import async_pair_device
            except ImportError:
                self._pairing_error = "The 'oupes_mega_ble' integration is not installed. BLE pairing is unavailable."
                return self.async_show_progress_done(next_step_id="credentials")

            # Check if async_pair_device accepts ssid/psk by inspecting signature or just passing as kwargs
            # since we will update ble_pairing.py to accept them.
            kwargs = {
                "hass": self.hass,
                "address": self._pairing_address,
                "device_key": self._pairing_key,
            }
            if self._wifi_ssid:
                kwargs["ssid"] = self._wifi_ssid
                kwargs["psk"] = self._wifi_psk

            self._pairing_task = self.hass.async_create_task(
                async_pair_device(**kwargs)
            )

        if not self._pairing_task.done():
            return self.async_show_progress(
                step_id="pairing",
                progress_action="pairing",
                progress_task=self._pairing_task,
            )

        try:
            result = self._pairing_task.result()
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "Unexpected error during BLE pairing with %s",
                self._pairing_address,
            )
            # CONNECTION_FAILED fallback
            try:
                from custom_components.oupes_mega_ble.ble_pairing import PairingResult
                result = PairingResult.CONNECTION_FAILED
            except ImportError:
                result = None
        finally:
            self._pairing_task = None

        if result is None:
            self._pairing_error = "The 'oupes_mega_ble' integration is not installed."
            return self.async_show_progress_done(next_step_id="credentials")

        try:
            from custom_components.oupes_mega_ble.ble_pairing import PairingResult
            if result == PairingResult.SUCCESS:
                # Store that pairing was a success and proceed
                return self.async_show_progress_done(next_step_id="pairing_complete")

            # Map failure to error key and go back to credentials form
            if result == PairingResult.DEVICE_NOT_FOUND:
                self._pairing_error = "pairing_device_not_found"
            else:
                self._pairing_error = "pairing_failed"
        except ImportError:
            self._pairing_error = "Pairing dependencies missing."
            
        return self.async_show_progress_done(next_step_id="credentials")

    async def async_step_pairing_complete(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Pairing succeeded � save the entry."""
        title = self._device_name_save if self._device_name_save else self._device_id_save
        return self.async_create_entry(
            title=title,
            data={
                CONF_DEVICE_ID: self._device_id_save,
                CONF_DEVICE_KEY: self._pairing_key,
                CONF_DEVICE_NAME: self._device_name_save,
                CONF_MAC_ADDRESS: self._pairing_address,
                CONF_RUNTIME_MAX: self._runtime_max,
                CONF_MODEL_OVERRIDE: self._model_override,
            },
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Allow viewing/editing device info including the device key."""
        config_entry = self._get_entry()
        config_subentry = self._get_reconfigure_subentry()
        errors: dict[str, str] = {}

        # Surface errors from the background WiFi provisioning step.
        if self._pairing_error:
            errors["base"] = self._pairing_error
            self._pairing_error = None

        if user_input is not None:
            new_ssid = user_input.get("wifi_ssid", "").strip()
            new_psk = user_input.get("wifi_psk", "").strip()
            mac = user_input.get(CONF_MAC_ADDRESS, "").strip()
            device_key = user_input.get(CONF_DEVICE_KEY, "").strip()

            if new_ssid:
                if not mac:
                    errors[CONF_MAC_ADDRESS] = "missing_mac_for_pairing"
                else:
                    # Store everything and run provisioning in a background task
                    # (direct await times out in the HA frontend).
                    self._reconfigure_new_data = {
                        CONF_DEVICE_ID: user_input.get(CONF_DEVICE_ID, "").strip(),
                        CONF_DEVICE_KEY: device_key,
                        CONF_DEVICE_NAME: user_input.get(CONF_DEVICE_NAME, "").strip(),
                        CONF_MAC_ADDRESS: mac,
                        CONF_RUNTIME_MAX: int(user_input.get(CONF_RUNTIME_MAX, ATTR78_RUNTIME_MAX)),
                        CONF_MODEL_OVERRIDE: user_input.get(CONF_MODEL_OVERRIDE, ""),
                    }
                    self._wifi_ssid = new_ssid
                    self._wifi_psk = new_psk
                    self._pairing_address = mac
                    return await self.async_step_reconfigure_wifi()

            if not errors:
                return self.async_update_and_abort(
                    config_entry,
                    config_subentry,
                    data={
                        CONF_DEVICE_ID: user_input.get(CONF_DEVICE_ID, "").strip(),
                        CONF_DEVICE_KEY: device_key,
                        CONF_DEVICE_NAME: user_input.get(CONF_DEVICE_NAME, "").strip(),
                        CONF_MAC_ADDRESS: mac,
                        CONF_RUNTIME_MAX: int(user_input.get(CONF_RUNTIME_MAX, ATTR78_RUNTIME_MAX)),
                        CONF_MODEL_OVERRIDE: user_input.get(CONF_MODEL_OVERRIDE, ""),
                    },
                    title=user_input.get(CONF_DEVICE_NAME, "").strip() or config_subentry.title,
                )

        current = config_subentry.data
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DEVICE_NAME,
                        default=current.get(CONF_DEVICE_NAME, ""),
                    ): TextSelector(TextSelectorConfig(autocomplete="off")),
                    vol.Required(
                        CONF_DEVICE_ID,
                        default=current.get(CONF_DEVICE_ID, ""),
                    ): TextSelector(TextSelectorConfig(autocomplete="off")),
                    vol.Required(
                        CONF_DEVICE_KEY,
                        default=current.get(CONF_DEVICE_KEY, ""),
                    ): TextSelector(TextSelectorConfig(autocomplete="off")),
                    vol.Optional(
                        CONF_MAC_ADDRESS,
                        default=current.get(CONF_MAC_ADDRESS, ""),
                    ): TextSelector(TextSelectorConfig(autocomplete="off")),
                    vol.Optional(
                        CONF_RUNTIME_MAX,
                        default=current.get(CONF_RUNTIME_MAX, ATTR78_RUNTIME_MAX),
                    ): NumberSelector(NumberSelectorConfig(
                        min=100, max=50000, step=60, mode=NumberSelectorMode.BOX,
                        unit_of_measurement="minutes",
                    )),
                    vol.Optional(
                        CONF_MODEL_OVERRIDE,
                        default=current.get(CONF_MODEL_OVERRIDE, ""),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                {"value": "", "label": "Auto-detect from device"},
                                *[
                                    {"value": pid, "label": name}
                                    for pid, (name, _) in sorted(
                                        MODEL_CATALOG.items(), key=lambda x: x[1][0]
                                    )
                                ],
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Optional("wifi_ssid", default=""): TextSelector(
                        TextSelectorConfig(autocomplete="off")
                    ),
                    vol.Optional("wifi_psk", default=""): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="new-password")
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure_wifi(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Run WiFi re-provisioning in the background while showing a progress spinner."""
        if self._reconfigure_task is None:
            try:
                from custom_components.oupes_mega_ble.ble_pairing import async_provision_wifi
            except ImportError:
                self._pairing_error = "wifi_ble_not_installed"
                return self.async_show_progress_done(next_step_id="reconfigure")

            self._reconfigure_task = self.hass.async_create_task(
                async_provision_wifi(
                    hass=self.hass,
                    address=self._pairing_address,
                    device_key=self._reconfigure_new_data.get(CONF_DEVICE_KEY, ""),
                    ssid=self._wifi_ssid,
                    psk=self._wifi_psk,
                )
            )

        if not self._reconfigure_task.done():
            return self.async_show_progress(
                step_id="reconfigure_wifi",
                progress_action="provisioning_wifi",
                progress_task=self._reconfigure_task,
            )

        try:
            result = self._reconfigure_task.result()
        except Exception:  # noqa: BLE001
            result = None
        finally:
            self._reconfigure_task = None

        try:
            from custom_components.oupes_mega_ble.ble_pairing import PairingResult
            if result == PairingResult.SUCCESS:
                return self.async_show_progress_done(next_step_id="reconfigure_save")
            if result == PairingResult.DEVICE_NOT_FOUND:
                self._pairing_error = "wifi_device_not_found"
            else:
                self._pairing_error = "wifi_provision_failed"
        except ImportError:
            self._pairing_error = "wifi_ble_not_installed"

        return self.async_show_progress_done(next_step_id="reconfigure")

    async def async_step_reconfigure_save(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Save reconfigured device data after successful WiFi provisioning."""
        config_entry = self._get_entry()
        config_subentry = self._get_reconfigure_subentry()
        data = self._reconfigure_new_data
        return self.async_update_and_abort(
            config_entry,
            config_subentry,
            data=data,
            title=data.get(CONF_DEVICE_NAME, "") or config_subentry.title,
        )

