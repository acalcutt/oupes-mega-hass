"""OUPES Mega WiFi — HTTP REST API Intercept Server.

Intercepts Cleanergy app REST API calls and returns responses that redirect
the app to our local HA TCP broker instead of the OUPES cloud.

Deployment — NAT port-forward (firewall LAN interface):
  Destination 47.251.27.175 port 80   →  <HA IP>:8897
  Destination 47.251.14.8  port 9504  →  <HA IP>:8896  (v2 broker, same server)
  Destination 8.135.109.78 port 80    →  <HA IP>:8897  (SiBo device bind, plain HTTP)

Key behaviours:
  - Login response redirects tcp_host / mark.tcpHost to this HA instance's
    local IP so the app connects to our TCP broker, not the cloud.
  - Device list is populated from registered sub-entry users.
  - Device/sync caches the app's full device JSON so later list/info
    responses have the correct name, MAC, firmware etc.
  - Unknown endpoints return {"ret":1,"info":{}} so the app does not crash.

Real API response shapes confirmed from PCAPdroid captures (Apr 2026).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

from .const import (
    VALIDATION_ACCEPT_ALL,
    VALIDATION_ACCEPT_REGISTERED,
    VALIDATION_LOG_ONLY,
)

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Static response data (captured from real API, Apr 2026)
# ---------------------------------------------------------------------------

_WEBURL_INFO: dict = {
    "manual":          "http://h5.upspowerstation.top/#/pages/product/product?token=",
    "warranty_policy": "http://h5.upspowerstation.top/#/pages/warranty/warranty?type=1&token=",
    "refund_policy":   "http://h5.upspowerstation.top/#/pages/warranty/warranty?type=2&token=",
    "cleanergy":       "http://h5.upspowerstation.top/#/pages/cleanergy/cleanergy?token=",
    "product_issue":   "http://h5.upspowerstation.top/#/pages/product_issue/product_issue?token=",
    "faq":             "http://h5.upspowerstation.top/#/pages/question/question?token=",
    "about":           "https://oupes.com/pages/about-us",
    "help":            "https://oupes.com/pages/oupes-help",
}

# Report as the current known version to suppress update nags.
_APP_VERSION_INFO: dict = {
    "version_code": "16",
    "download_url":  "1",
    "version":       "1.4.1",
    "desc":          "",
    "apk":           "",
    "zh_desc":       "",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok(info: object) -> dict:
    """Wrap a value in the standard API success envelope."""
    return {"ret": 1, "desc": "succes", "info": info}


def _err(desc: str = "error") -> dict:
    return {"ret": 0, "desc": desc, "info": ""}


def _sibo_ok(info: object = None) -> dict:
    """SiBo envelope — ret is string "1", not integer."""
    return {"ret": "1", "desc": "success", "info": info if info is not None else {}}


def _sibo_json_compact(data: dict) -> str:
    """Encode as compact JSON (no spaces) matching real SiBo server format."""
    return json.dumps(data, separators=(",", ":"))


def _parse_body(body: bytes) -> dict:
    try:
        return json.loads(body) if body else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class OUPESHttpInterceptServer:
    """Intercept HTTP REST API calls from the Cleanergy app.

    Args:
        port:            TCP port for this HTTP server (default 8897).
        tcp_port:        Port of the local TCP broker — returned to the app
                         in login responses so it connects here, not the cloud.
        user_registry:   email -> {passwd: sha256_hex,
                                   devices: [{device_id, device_key}, ...]}
                         Built from config sub-entries by __init__.py.
        validation_mode: accept_all | log_only | accept_registered
        debug_file:      Path to the shared JSONL debug log, or None.
        debug_http:      If True, write every request to debug_file.
    """

    def __init__(
        self,
        port: int,
        tcp_port: int,
        user_registry: dict[str, dict],
        validation_mode: str = VALIDATION_ACCEPT_ALL,
        debug_file: Path | None = None,
        debug_http: bool = False,
        tcp_server: object = None,
    ) -> None:
        self._port = int(port)
        self._tcp_port = int(tcp_port)
        self._user_registry = user_registry
        self._validation_mode = validation_mode
        self._debug_file = debug_file
        self._debug_http = debug_http
        self._tcp_server = tcp_server  # OUPESWiFiProxyServer, for live online status
        self._runner: web.AppRunner | None = None
        # token → {email, uid, nickname, mark_token}
        self._sessions: dict[str, dict] = {}
        # device_id → full device dict (populated from app's device/sync calls)
        self._device_cache: dict[str, dict] = {}
        self._next_uid = 90000
        # Stable uid per email — survives registry hot-swaps and re-logins
        self._uid_by_email: dict[str, int] = {}
        # Optional callback: called with (device_id, product_id) on device bind.
        # Set by __init__.py to propagate product_id to the matching coordinator.
        self.on_device_bind: object | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        app = web.Application()
        app.router.add_route("*", "/{path_info:.*}", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self._port)
        await site.start()
        _LOGGER.info("OUPES HTTP intercept server listening on port %d", self._port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            _LOGGER.debug("OUPES HTTP intercept server stopped")

    def update_user_registry(self, registry: dict[str, dict]) -> None:
        """Hot-swap the user registry (called when sub-entries are added/removed)."""
        self._user_registry = registry
        # Re-bind any stub sessions that were created when the registry was
        # empty (their email is "unknown_<token8>@local") to the sole registered
        # user, if there is exactly one now.  Without this, a session adopted
        # before sub-entries existed would never see devices.
        new_keys = list(registry.keys())
        if len(new_keys) == 1:
            sole_email = new_keys[0]
            for session in self._sessions.values():
                if (session.get("email", "").startswith("unknown_")
                        and session["email"].endswith("@local")):
                    _LOGGER.debug(
                        "HTTP: re-binding stub session %s → %s",
                        session["email"], sole_email,
                    )
                    session["email"] = sole_email
        _LOGGER.debug("OUPES HTTP: user registry updated (%d users)", len(registry))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _local_ip(self, request: web.Request) -> str:
        """Return the local interface IP this request arrived on.

        When the server listens on 0.0.0.0, the *accepted* socket's
        sockname reflects the specific interface (e.g. 192.168.1.5),
        which is what we need to return to the app as broker address.
        """
        sockname = request.transport.get_extra_info("sockname")  # type: ignore[union-attr]
        if sockname and sockname[0] not in ("0.0.0.0", "::"):
            return sockname[0]
        return "127.0.0.1"

    def _get_or_adopt_session(self, token: str, request: web.Request) -> dict | None:
        """Return an existing session or, in permissive modes, adopt an unknown token.

        When the app carries a token issued by the real cloud (before our
        interception began), we won't find it in _sessions.  In accept_all /
        log_only mode we create a stub session so profile/device-list can still
        return a valid mark with the HA broker IP.
        """
        session = self._sessions.get(token)
        if session:
            return session
        if self._validation_mode == VALIDATION_ACCEPT_REGISTERED:
            return None
        # Adopt the unknown token as a stub session so the broker redirect works.
        # If there is exactly one registered user, bind the stub to their email
        # so that device/list and device/sync return the correct devices.
        # (This covers the common case where the app has a cached real-cloud
        #  token and never re-logs-in through our server.)
        registered = list(self._user_registry.keys())
        adopted_email = registered[0] if len(registered) == 1 else f"unknown_{token[:8]}@local"
        _LOGGER.debug(
            "HTTP: adopting unknown token %s… as stub session for %s",
            token[:8], adopted_email,
        )
        stub: dict = {
            "email":      adopted_email,
            "uid":        self._uid_for(adopted_email),
            "nickname":   "oupes_user",
            "mark_token": secrets.token_urlsafe(22),
        }
        self._sessions[token] = stub
        return stub

    def _uid_for(self, email: str) -> int:
        """Return a stable uid for *email*, allocating one on first call."""
        uid = self._uid_by_email.get(email)
        if uid is None:
            user_data = self._user_registry.get(email) or {}
            
            # 1. Provide the UID saved in the native config entry if it exists
            stored_uidStr = user_data.get("uid")
            if stored_uidStr:
                try:
                    uid = int(stored_uidStr)
                except (ValueError, TypeError):
                    pass
            
            # 2. Backwards compatibility for older devices without CONF_UID
            if uid is None:
                devices = user_data.get("devices") or []
                if devices:
                    target_key = devices[0].get("device_key", "").lower()
                    if len(target_key) == 10:
                        _LOGGER.info("Reverse-engineering UID for device key: %s", target_key)
                        for i in range(1, 10000000):
                            if hashlib.md5(str(i).encode()).hexdigest()[:10] == target_key:
                                uid = i
                                _LOGGER.info("Computed matching UID: %d for key %s", uid, target_key)
                                break
            
            # 3. Last fallback
            if uid is None:
                uid = self._next_uid
                self._next_uid += 1
                
            self._uid_by_email[email] = uid
        return uid

    def _make_session(self, email: str) -> tuple[str, dict]:
        token = secrets.token_hex(16)
        session: dict = {
            "email":      email,
            "uid":        self._uid_for(email),
            "nickname":   secrets.token_hex(3),
            "mark_token": secrets.token_urlsafe(22),
        }
        self._sessions[token] = session
        return token, session

    def _json(self, data: dict) -> web.Response:
        return web.Response(content_type="application/json", text=json.dumps(data))

    def _resolve_email(self, email: str) -> str:
        """Resolve an email to a registry key.

        If the exact email is not in the registry but there is exactly one
        registered user, return that user's email.  Covers the case where a
        stub session was created before sub-entries were added (email is
        "unknown_<token8>@local") or a typo in the adopted email.
        """
        if email in self._user_registry:
            return email
        keys = list(self._user_registry.keys())
        if len(keys) == 1:
            _LOGGER.debug(
                "HTTP: resolving unregistered email '%s' → sole user '%s'",
                email, keys[0],
            )
            return keys[0]
        return email

    def _device_sync_list_for_email(self, email: str) -> list[dict]:
        """Build device list shaped as UserDevicesPo.DevicePo for device/sync responses."""
        email = self._resolve_email(email)
        user = self._user_registry.get(email, {})
        _LOGGER.debug(
            "HTTP device/sync: building list for '%s' — %d device(s) registered",
            email, len(user.get("devices", [])),
        )
        result = []
        for dev in user.get("devices", []):
            did = dev.get("device_id", "")
            if not did:
                continue
            cached = self._device_cache.get(did, {})
            result.append({
                "device_id":          did,
                "device_key":         dev.get("device_key", ""),
                "device_name":        dev.get("device_name") or cached.get("name", "OUPES"),
                "mac_address":        dev.get("mac_address") or cached.get("mac_address", ""),
                "device_product_id":  cached.get("device_product_id", "O44A5o"),
                "device_model_name":  cached.get("title", ""),
                "device_model_icon":  cached.get("pic", ""),
                "firmware_version":   cached.get("firmware_version", "1.2.0"),
                "is_online":          0,
                "bind_timestamp":     0,
                "status":             1,
                "network":            "",
                "mpass_url":          "",
            })
        return result

    def _device_list_for_email(self, email: str) -> list[dict]:
        """Build a device list for API responses from registry + sync cache."""
        email = self._resolve_email(email)
        user = self._user_registry.get(email, {})
        result = []
        for dev in user.get("devices", []):
            did = dev.get("device_id", "")
            if not did:
                continue
            cached = self._device_cache.get(did, {})
            result.append({
                "name":              dev.get("device_name") or cached.get("name", "OUPES"),
                "device_id":         did,
                "device_key":        dev.get("device_key", ""),
                "mac_address":       dev.get("mac_address") or cached.get("mac_address", ""),
                "device_product_id": cached.get("device_product_id", "O44A5o"),
                "firmware_version":  cached.get("firmware_version", "1.2.0"),
                "sn":                cached.get("sn", ""),
                "pic":               cached.get("pic", ""),
                "title":             cached.get("title", "Mega 1"),
                "home":              [],
                "system":            [],
            })
        return result

    # ------------------------------------------------------------------
    # Route handlers  (sync — body already read in _handle)
    # ------------------------------------------------------------------

    def _route_login(self, body: bytes, request: web.Request) -> web.Response:
        b = _parse_body(body)
        email = (b.get("mail") or b.get("email") or "").strip().lower()
        passwd_hash = hashlib.sha256(b.get("passwd", "").encode()).hexdigest()
        user = self._user_registry.get(email)

        if self._validation_mode == VALIDATION_ACCEPT_REGISTERED:
            if not user:
                _LOGGER.warning("HTTP login rejected: unknown user %s", email)
                return self._json(_err("User not found"))
            if user.get("passwd") and user["passwd"] != passwd_hash:
                _LOGGER.warning("HTTP login rejected: wrong password for %s", email)
                return self._json(_err("Wrong password"))
        elif self._validation_mode == VALIDATION_LOG_ONLY:
            if not user:
                _LOGGER.warning("HTTP login: unknown user %s (log_only — accepting)", email)
            elif user.get("passwd") and user["passwd"] != passwd_hash:
                _LOGGER.warning("HTTP login: wrong password for %s (log_only — accepting)", email)
        else:  # accept_all
            if not user:
                self._user_registry[email] = {"passwd": passwd_hash, "devices": []}

        token, session = self._make_session(email)
        ha_ip = self._local_ip(request)
        mark = json.dumps({
            "avatar":           "",
            "countryNumberCode": "840",
            "isTempLogin":      True,
            "isThirdParty":     0,
            "isUpdate":         0,
            "nickname":         session["nickname"],
            "tcpHost":          ha_ip,
            "tcpPort":          str(self._tcp_port),
            "token":            session["mark_token"],
            "uid":              session["uid"],
        }, separators=(",", ":"))
        _LOGGER.info("HTTP login: %s → session %s…", email, token[:8])
        return self._json(_ok({
            "uid":         session["uid"],
            "token":       token,
            "email":       email,
            "avatar":      "",
            "is_update":   "",
            "nickname":    session["nickname"],
            "mark":        mark,
            "tcp_host":    ha_ip,
            "tcp_port":    str(self._tcp_port),
            "api_host":    "api.upspowerstation.top",
            "tcp_host_v2": ha_ip,
            "tcp_port_v2": str(self._tcp_port),
            "udp_host":    ha_ip,
            "udp_port":    "9200",
        }))

    def _route_logout(self, body: bytes) -> web.Response:
        token = _parse_body(body).get("token", "")
        self._sessions.pop(token, None)
        return self._json(_ok(""))

    def _route_register_code(self, body: bytes) -> web.Response:
        # Pretend to send a verification email — always succeed.
        return self._json(_ok(""))

    def _route_register(self, body: bytes) -> web.Response:
        b = _parse_body(body)
        email = (b.get("mail") or b.get("email") or "").strip().lower()
        passwd_hash = hashlib.sha256(b.get("passwd", "").encode()).hexdigest()
        if email and email not in self._user_registry:
            self._user_registry[email] = {"passwd": passwd_hash, "devices": []}
        return self._json(_ok(""))

    def _route_profile(self, request: web.Request) -> web.Response:
        token = request.rel_url.query.get("token", "")
        session = self._get_or_adopt_session(token, request)
        if not session:
            return self._json(_ok({
                "id": 0, "avatar": "", "phone": "", "nickname": "",
                "mail": "", "lang": "en", "imei": "", "package_name": "",
                "user_term_version": "", "user_privacy_version": "", "mark": "",
            }))
        mark = json.dumps({
            "tcpHost": self._local_ip(request),
            "tcpPort": str(self._tcp_port),
            "token":   session["mark_token"],
            "uid":     session["uid"],
        }, separators=(",", ":"))
        return self._json(_ok({
            "id":                   session["uid"],
            "avatar":               "",
            "phone":                "",
            "nickname":             session["nickname"],
            "mail":                 session["email"],
            "lang":                 "en",
            "imei":                 "",
            "package_name":         "",
            "user_term_version":    "",
            "user_privacy_version": "",
            "mark":                 mark,
        }))

    def _route_device_list(self, request: web.Request) -> web.Response:
        token = request.rel_url.query.get("token", "")
        session = self._get_or_adopt_session(token, request)
        if not session:
            return self._json(_ok({"bind": [], "share": [], "third": []}))
        devices = self._device_list_for_email(session["email"])
        return self._json(_ok({"bind": devices, "share": [], "third": []}))

    def _route_device_info(self, request: web.Request) -> web.Response:
        did = request.rel_url.query.get("device_id", "")
        if not did:
            return self._json(_err("missing device_id"))
        cached = self._device_cache.get(did, {})
        device_key = cached.get("device_key", "")
        device_name = cached.get("name", "OUPES")
        mac_address = cached.get("mac_address", "")
        
        for user in self._user_registry.values():
            for dev in user.get("devices", []):
                if dev.get("device_id") == did:
                    device_key = dev.get("device_key", device_key)
                    if dev.get("device_name"):
                        device_name = dev.get("device_name")
                    if dev.get("mac_address"):
                        mac_address = dev.get("mac_address")
                    break

        online = 1 if (self._tcp_server and self._tcp_server.is_device_online(did)) else 0
        return self._json(_ok({
            "name":              device_name,
            "device_id":         did,
            "device_key":        device_key,
            "mac_address":       mac_address,
            "device_product_id": cached.get("device_product_id", "O44A5o"),
            "firmware_version":  cached.get("firmware_version", "1.2.0"),
            "online":            online,
            "sku":               0,
            "sn":                cached.get("sn", ""),
            "detail":            {"extreme_weather": 0, "config": ""},
            "state":             {"addtime": 0, "ip": "", "port": 0},
        }))

    def _route_device_sync(self, body: bytes, request: web.Request) -> web.Response:
        b = _parse_body(body)
        token = b.get("token", "")
        raw = b.get("device_list", "[]")
        try:
            devices = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            devices = []
        # Cache any devices the app pushed up.
        for dev in (devices if isinstance(devices, list) else []):
            did = dev.get("device_id", "")
            if did:
                self._device_cache[did] = dev
                _LOGGER.debug("HTTP sync: cached device %s (%s)", did, dev.get("name", "?"))
        # Respond with the canonical device list for this user so the app
        # discovers registered devices even when it never calls device/list.
        # Response is shaped as UserDevicesPo (sync POJO), not UserDevicesBean (list POJO).
        session = self._get_or_adopt_session(token, request)
        email = session["email"] if session else ""
        canonical = self._device_sync_list_for_email(email)
        return self._json(_ok({"bindDevices": canonical, "shareDevices": [], "groupDevices": [], "shareGroupDevices": []}))

    # ------------------------------------------------------------------
    # Main request dispatcher
    # ------------------------------------------------------------------

    async def _handle(self, request: web.Request) -> web.Response:
        body = await request.read()
        peer = request.remote or "unknown"
        path = request.path
        method = request.method

        _LOGGER.debug("HTTP %s %s from %s (%d B)", method, path, peer, len(body))

        if self._debug_http and self._debug_file:
            await asyncio.to_thread(
                self._debug_write_request, request, body, peer
            )

        resp = self._dispatch(request, body, path, method)

        if self._debug_http and self._debug_file:
            await asyncio.to_thread(
                self._debug_write_response, path, resp
            )

        return resp

    def _dispatch(self, request: web.Request, body: bytes, path: str, method: str) -> web.Response:
        """Route a request to the appropriate handler and return the response."""
        # Auth / user
        if path == "/api/app/user/login" and method == "POST":
            return self._route_login(body, request)
        if path == "/api/app/user/logout":
            return self._route_logout(body)
        if path == "/api/app/user/register/code" and method == "POST":
            return self._route_register_code(body)
        if path == "/api/app/user/register" and method == "POST":
            return self._route_register(body)
        if path == "/api/app/user/profile":
            return self._route_profile(request)

        # Device management
        if path == "/api/app/device/list":
            return self._route_device_list(request)
        if path == "/api/app/device/info":
            return self._route_device_info(request)
        if path == "/api/app/device/sync" and method == "POST":
            return self._route_device_sync(body, request)
        if path == "/api/app/device/model":
            return self._json(_ok({"count": 0, "list": []}))

        # Profile upload (nickname / avatar update)
        if path in ("/api/app/user/profile/upload", "api/app/user/profile/upload") and method == "POST":
            b = _parse_body(body)
            tok = b.get("token", "")
            session = self._sessions.get(tok)
            if session:
                if b.get("nickname"):
                    session["nickname"] = b["nickname"]
                if b.get("avatar"):
                    session["avatar"] = b["avatar"]
            return self._json(_ok(""))

        # Logoff (account deletion request) — treat same as logout
        if path == "/api/app/user/logoff" and method == "POST":
            return self._route_logout(body)

        # Config / misc
        if path == "/api/app/config/weburl":
            return self._json(_ok(_WEBURL_INFO))
        if path == "/api/app/config/app_version":
            return self._json(_ok(_APP_VERSION_INFO))
        if path == "/api/app/config/platfrom":
            # Community / platform info — return minimal stub.
            return self._json(_ok({"community": [], "platfrom": []}))
        if path == "/api/app/shop/list":
            return self._json(_ok({"banner": [], "goods": []}))

        # Token refresh — return the same token so the app doesn't crash
        if path == "/api/app/refresh/token":
            tok = request.rel_url.query.get("token", "")
            return self._json(_ok({"token": tok}))

        # --- SiBo device-firmware endpoints (8.135.109.78:80) ---
        # The device firmware POSTs to wp-cn.doiting.com (8.135.109.78) on
        # plain HTTP port 80 during boot.  NAT redirects that to
        # this same HTTP server on 8897.  The firmware uses paths WITHOUT
        # the "/app/" prefix and expects SiBo-style {"ret":"1",...} responses
        # (string, not integer).
        if path == "/api/device/unbind" and method == "POST":
            return self._route_sibo_device_unbind(body)
        if path == "/api/device/bind" and method == "POST":
            return self._route_sibo_device_bind(body, request)
        if path == "/api/temp_user/login" and method == "POST":
            return self._route_sibo_temp_login()
        if path.startswith("/api/") and "/app/" not in path:
            # Catch-all for any other SiBo firmware endpoint — return success
            _LOGGER.debug("SiBo device endpoint (catch-all): %s %s", method, path)
            return self._json_sibo(_sibo_ok({}))

        _LOGGER.debug("HTTP intercept: unhandled %s %s", method, path)
        return self._json(_ok({}))

    # ------------------------------------------------------------------
    # SiBo device-firmware handlers (plain HTTP, 8.135.109.78:80)
    # ------------------------------------------------------------------

    @staticmethod
    def _json_sibo(data: dict) -> web.Response:
        """Return a response using SiBo envelope (ret as string "1").

        Uses compact JSON (no spaces) to match the real SiBo server format,
        since the DoHome firmware parser may use simple string matching.
        """
        return web.Response(content_type="application/json", text=_sibo_json_compact(data))

    def _route_sibo_device_unbind(self, body: bytes) -> web.Response:
        """Handle POST /api/device/unbind from device firmware.

        The device sends unbind before bind on each boot cycle.
        The real cloud returns ret:"1" for success.
        """
        b = _parse_body(body)
        device_id = b.get("device_id", "")
        _LOGGER.info("SiBo device unbind: device_id=%s", device_id)
        return self._json_sibo(_sibo_ok({}))

    def _uid_for_device(self, device_id: str, device_key: str) -> str:
        """Find the matching user uid assigned to the given device."""
        for email, udata in self._user_registry.items():
            for d in udata.get("devices", []):
                if (device_id and d.get("device_id") == device_id) or \
                   (device_key and d.get("device_key") == device_key):
                    return str(self._uid_for(email))
        return "60859"

    def _route_sibo_device_bind(self, body: bytes, request: web.Request) -> web.Response:
        """Handle POST /api/device/bind from device firmware.

        The device sends: device_id, device_key, device_product_id,
        user_token, lat, lng, device_firmware_version, additional_detail.

        Real cloud response (confirmed via pfSense PCAP 2026-04-13):
          {"ret":"1","desc":"Success","info":{"uid":"60859",
           "tcp_ip":"47.252.10.9","tcp_port":8896,
           "timestamp":"1776055648","timezone_offset":0}}

        The info.tcp_ip and info.tcp_port tell the device WHERE to connect
        for the TCP broker.  Without these fields the device has no broker
        address and never connects.
        """
        b = _parse_body(body)
        device_id = b.get("device_id", "")
        device_key = b.get("device_key", "")
        product_id = b.get("device_product_id", "")
        _LOGGER.info(
            "SiBo device bind: device_id=%s key=%s product=%s fw=%s",
            device_id,
            device_key[:4] + "…" if device_key else "?",
            product_id or "?",
            b.get("device_firmware_version", b.get("firmware_version", "?")),
        )
        # Cache the product_id from the bind request so coordinators can pick
        # it up even if the app's device/sync call hasn't arrived yet.
        if device_id and product_id:
            self._device_cache.setdefault(device_id, {})["device_product_id"] = product_id
            if callable(self.on_device_bind):
                self.on_device_bind(device_id, product_id)
        ha_ip = self._local_ip(request)
        ts = str(int(time.time()))
        return self._json_sibo(_sibo_ok({
            "uid": self._uid_for_device(device_id, device_key),
            "tcp_ip": ha_ip,
            "tcp_port": self._tcp_port,
            "timestamp": ts,
            "timezone_offset": 0,
        }))

    def _route_sibo_temp_login(self) -> web.Response:
        """Handle POST /api/temp_user/login from device firmware."""
        return self._json_sibo(_sibo_ok({
            "token":              "oupes_ha_stub_token",
            "uid":                1,
            "nickname":           "",
            "avatar":             "",
            "tcp_host":           "",
            "tcp_port":           "",
            "is_third_party":     0,
            "is_update":          0,
            "country_number_code": "840",
        }))

    # ------------------------------------------------------------------
    # Debug logging
    # ------------------------------------------------------------------

    def _debug_write_request(self, request: web.Request, body: bytes, peer: str) -> None:
        if self._debug_file is None:
            return
        record = {
            "type":    "http_request",
            "ts":      datetime.now(timezone.utc).isoformat(),
            "peer":    peer,
            "method":  request.method,
            "path":    request.path,
            "query":   request.query_string,
            "headers": dict(request.headers),
            "body":    body.decode("utf-8", errors="replace"),
        }
        try:
            with self._debug_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            _LOGGER.warning("OUPES HTTP debug write failed: %s", exc)

    def _debug_write_response(self, path: str, resp: web.Response) -> None:
        if self._debug_file is None:
            return
        try:
            resp_body = resp.text or ""
        except Exception:
            resp_body = ""
        record = {
            "type":   "http_response",
            "ts":     datetime.now(timezone.utc).isoformat(),
            "path":   path,
            "status": resp.status,
            "body":   resp_body,
        }
        try:
            with self._debug_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            _LOGGER.warning("OUPES HTTP debug write failed: %s", exc)
