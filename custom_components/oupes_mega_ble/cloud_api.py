"""Cloud API client for OUPES device key retrieval.

The OUPES cloud API at api.upspowerstation.top provides device metadata
including the per-device ``device_key`` needed for BLE init packet 6.

Authentication:
  1. ``POST /api/app/user/login`` with Cleanergy account email/password
     returns a session *token*.
  2. ``GET /api/app/device/list?token=...`` returns all bound devices
     with their ``device_key``.

The ``device_id`` is extracted from BLE advertising data during discovery.
Device matching falls back to MAC address when device_id is unavailable.

**Important:** This API uses unencrypted HTTP (not HTTPS).  This mirrors
the official Cleanergy app's own behaviour — OUPES does not offer an
HTTPS endpoint.
"""
from __future__ import annotations

import logging

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

API_BASE = "http://api.upspowerstation.top"
_COMMON_PARAMS = {"platform": "android", "lang": "en", "systemVersion": "36"}
_COMMON_HEADERS = {
    "versionname": "1.4.1",
    "lang": "en",
    "package": "com.cleanergy.app",
}
_TIMEOUT = aiohttp.ClientTimeout(total=15)


async def async_cloud_login(
    hass: HomeAssistant,
    email: str,
    password: str,
) -> str | None:
    """Log in to the OUPES cloud and return the session token.

    Calls ``POST /api/app/user/login`` with the user's Cleanergy account
    credentials.  Returns the token string on success, ``None`` on failure.
    """
    session = async_get_clientsession(hass)
    payload = {
        "mail": email,
        "passwd": password,
        "lang": "en",
        "platform": "android",
        "systemVersion": 36,
    }
    try:
        async with session.post(
            f"{API_BASE}/api/app/user/login",
            json=payload,
            headers=_COMMON_HEADERS,
            timeout=_TIMEOUT,
        ) as resp:
            if resp.status != 200:
                _LOGGER.debug("Cloud login returned HTTP %d", resp.status)
                return None
            body = await resp.json(content_type=None)
            if body.get("ret") != 1:
                _LOGGER.debug("Cloud login failed: %s", body.get("desc"))
                return None
            token = (body.get("info") or {}).get("token")
            if token:
                _LOGGER.debug("Cloud login successful")
                return token
            _LOGGER.debug("Cloud login response missing token")
            return None
    except Exception as exc:
        _LOGGER.debug("Cloud login request failed: %s", exc)
        return None


async def async_fetch_device_key(
    hass: HomeAssistant,
    *,
    device_id: str | None = None,
    mac_address: str | None = None,
    token: str = "",
) -> str | None:
    """Fetch the device_key for an OUPES device from the cloud API.

    Requires a valid *token* from :func:`async_cloud_login`.

    Tries strategies in order:
      1. ``/api/app/device/info`` with *device_id* and *token*.
      2. ``/api/app/device/list`` matched by *device_id* or *mac_address*.

    Returns the 10-char hex device_key, or ``None`` on failure.
    """
    if not token:
        _LOGGER.debug("No cloud token provided — cannot fetch device_key")
        return None

    session = async_get_clientsession(hass)

    # Strategy 1: direct device info lookup by device_id
    if device_id:
        key = await _fetch_by_device_id(session, device_id, token)
        if key:
            return key

    # Strategy 2: list all devices and match by device_id or MAC
    key = await _fetch_from_list(session, token, device_id, mac_address)
    if key:
        return key

    return None


async def _fetch_by_device_id(
    session: aiohttp.ClientSession,
    device_id: str,
    token: str,
) -> str | None:
    """GET /api/app/device/info?device_id=...&token=... → device_key."""
    params = {**_COMMON_PARAMS, "device_id": device_id, "token": token}
    try:
        async with session.get(
            f"{API_BASE}/api/app/device/info",
            params=params,
            headers=_COMMON_HEADERS,
            timeout=_TIMEOUT,
        ) as resp:
            if resp.status != 200:
                _LOGGER.debug(
                    "Cloud API /device/info returned HTTP %d", resp.status
                )
                return None
            body = await resp.json(content_type=None)
            if body.get("ret") != 1:
                _LOGGER.debug(
                    "Cloud API /device/info error: %s", body.get("desc")
                )
                return None
            return _extract_key(body.get("info"))
    except Exception as exc:
        _LOGGER.debug("Cloud API /device/info request failed: %s", exc)
        return None


async def _fetch_from_list(
    session: aiohttp.ClientSession,
    token: str,
    device_id: str | None,
    mac_address: str | None,
) -> str | None:
    """GET /api/app/device/list, find device by id or MAC, return its key."""
    params = {**_COMMON_PARAMS, "token": token}
    target_mac = mac_address.upper().replace("-", ":") if mac_address else None
    try:
        async with session.get(
            f"{API_BASE}/api/app/device/list",
            params=params,
            headers=_COMMON_HEADERS,
            timeout=_TIMEOUT,
        ) as resp:
            if resp.status != 200:
                _LOGGER.debug(
                    "Cloud API /device/list returned HTTP %d", resp.status
                )
                return None
            body = await resp.json(content_type=None)
            if body.get("ret") != 1:
                _LOGGER.debug(
                    "Cloud API /device/list error: %s", body.get("desc")
                )
                return None
            info = body.get("info") or {}
            devices = info.get("bind", [])
            for dev in devices:
                if device_id and dev.get("device_id") == device_id:
                    return _extract_key(dev)
                if target_mac:
                    dev_mac = (dev.get("mac_address") or "").upper().replace(
                        "-", ":"
                    )
                    if dev_mac == target_mac:
                        return _extract_key(dev)
    except Exception as exc:
        _LOGGER.debug("Cloud API /device/list request failed: %s", exc)
    return None


def _extract_key(device_data: dict | list | None) -> str | None:
    """Pull a valid 10-char hex device_key from a device info dict."""
    if isinstance(device_data, list):
        device_data = device_data[0] if device_data else None
    if not isinstance(device_data, dict):
        return None
    key = device_data.get("device_key", "")
    if isinstance(key, str) and len(key) == 10 and all(
        c in "0123456789abcdefABCDEF" for c in key
    ):
        return key
    _LOGGER.debug("Cloud API returned invalid or missing device_key: %r", key)
    return None
