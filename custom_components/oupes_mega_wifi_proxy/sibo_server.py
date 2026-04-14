"""OUPES Mega WiFi — SiBo (wp-cn.doiting.com) HTTPS Mock Server.

The Cleanergy / OUPES app calls a secondary IoT cloud ("SiBo", at
wp-cn.doiting.com / 8.135.109.78:443) via HTTPS.  When those calls fail —
either because the device IMEI isn't registered with SiBo or because
configNetToken is stale — the app's ResponseParse fires skipToLogin(true)
and shows a "token error" toast, causing an infinite login loop.

This server stubs the SiBo HTTPS endpoints so the app always gets a valid
ret:1 response and never tries to re-login.

Deployment requires your firewall/router to:
  1. Intercept TLS for wp-cn.doiting.com using an SSL-inspecting proxy (e.g. Squid).
  2. Redirect the decrypted HTTP traffic to this server on port 8898.

See the README for full Squid + Android CA setup instructions.

SiBo endpoints handled:
  POST /api/app/temp_user/login          → fake configNetToken
  GET  /api/v2/app/device_with_group/list → empty groups list (ret:1)
  POST /api/app/device/info              → empty device info (ret:1)
  POST /api/app/device/bind / unbind     → ok (ret:1)
  POST /api/app/temp_user/logout         → ok (ret:1)
  *    (everything else)                 → {"ret":"1","info":{},"desc":"ok"}
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import ssl
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp import web

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# TLS helpers
# ---------------------------------------------------------------------------

def _generate_self_signed_cert() -> tuple[str, str]:
    """Generate a self-signed TLS certificate for wp-cn.doiting.com.

    Returns (cert_pem_path, key_pem_path) as temporary files.
    These are written to a temp dir that persists for the process lifetime.
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError as exc:
        raise RuntimeError(
            "The 'cryptography' package is required for the SiBo HTTPS mock server. "
            "It should be available in Home Assistant."
        ) from exc

    # Generate RSA private key
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    # Build certificate
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "wp-cn.doiting.com"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "OUPES HA Mock"),
    ])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("wp-cn.doiting.com"),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    # Write to temp files
    tmp = tempfile.mkdtemp(prefix="oupes_sibo_")
    cert_path = os.path.join(tmp, "sibo.crt")
    key_path  = os.path.join(tmp, "sibo.key")

    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))

    _LOGGER.info(
        "SiBo mock: generated self-signed TLS cert for wp-cn.doiting.com at %s", tmp
    )
    return cert_path, key_path


def _build_ssl_context(
    cert_path: str | None = None,
    key_path: str  | None = None,
) -> ssl.SSLContext:
    """Build an SSL context.

    If cert_path/key_path are provided, load that keypair.
    Otherwise generate a self-signed cert at runtime.
    """
    if not cert_path or not key_path:
        cert_path, key_path = _generate_self_signed_cert()

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
    return ctx


# ---------------------------------------------------------------------------
# Response helpers (SiBo uses ret:"1" as strings, not int)
# ---------------------------------------------------------------------------

def _sibo_ok(info: object = None) -> dict:
    return {"ret": "1", "desc": "success", "info": info if info is not None else {}}


def _sibo_json(data: dict) -> web.Response:
    # Compact JSON (no spaces) to match real SiBo server format.
    return web.Response(
        content_type="application/json",
        text=json.dumps(data, separators=(",", ":")),
    )


def _parse_body(body: bytes) -> dict:
    try:
        return json.loads(body) if body else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class SiBoClouServerStub:
    """HTTPS stub server that impersonates wp-cn.doiting.com.

    Returns ret:1 for all SiBo requests so the app never triggers
    the "token error" / skipToLogin loop.

    Args:
        port:      TCP port to listen on (default 8898).
        cert_path: Path to a TLS certificate PEM file.  If None, a
                   self-signed cert is generated at startup.
        key_path:  Path to the corresponding private key PEM file.
    """

    def __init__(
        self,
        port: int = 8898,
        cert_path: str | None = None,
        key_path: str | None = None,
    ) -> None:
        self._port = int(port)
        self._cert_path = cert_path
        self._key_path  = key_path
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        ssl_ctx = await asyncio.to_thread(
            _build_ssl_context, self._cert_path, self._key_path
        )

        app = web.Application()
        app.router.add_route("*", "/{path_info:.*}", self._handle)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self._port, ssl_context=ssl_ctx)
        await site.start()
        _LOGGER.info(
            "SiBo HTTPS mock server listening on port %d (TLS)", self._port
        )

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            _LOGGER.debug("SiBo HTTPS mock server stopped")

    # ------------------------------------------------------------------
    # Request handler
    # ------------------------------------------------------------------

    async def _handle(self, request: web.Request) -> web.Response:
        body = await request.read()
        path = request.path
        method = request.method
        _LOGGER.debug("SiBo mock %s %s (%d B)", method, path, len(body))
        return self._dispatch(path, method, body)

    def _dispatch(self, path: str, method: str, body: bytes) -> web.Response:
        # SiBo temp-user login — return a fake configNetToken
        if path == "/api/app/temp_user/login" and method == "POST":
            return _sibo_json(_sibo_ok({
                "token":             "oupes_ha_stub_token",
                "uid":               1,
                "nickname":          "",
                "avatar":            "",
                "tcp_host":          "",
                "tcp_port":          "",
                "is_third_party":    0,
                "is_update":         0,
                "country_number_code": "840",
            }))

        # Device + group list — empty lists mean no SiBo devices; no ret:9
        if path in (
            "/api/v2/app/device_with_group/list",
            "/api/app/device_with_group/list",
        ):
            return _sibo_json(_sibo_ok({
                "bindDevices":  [],
                "shareDevices": [],
            }))

        # Device info
        if path == "/api/app/device/info":
            return _sibo_json(_sibo_ok({}))

        # Bind / rebind
        if path in ("/api/app/device/bind", "/api/app/device/rebind"):
            return _sibo_json(_sibo_ok({}))

        # Unbind
        if path in ("/api/app/device/unbind", "/app/device/unbind"):
            return _sibo_json(_sibo_ok({}))

        # Logout
        if path in ("/api/app/temp_user/logout", "/api/app/log_out"):
            return _sibo_json(_sibo_ok({}))

        # Upload SiBo token (profile/upload goes to OUPES server, not here — but stub anyway)
        if "/profile/upload" in path:
            return _sibo_json(_sibo_ok({}))

        # Any other SiBo path — return success so ResponseParse doesn't fire skipToLogin
        _LOGGER.debug("SiBo mock: unhandled %s %s — returning stub ok", method, path)
        return _sibo_json(_sibo_ok({}))
