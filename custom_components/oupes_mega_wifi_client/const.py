"""Constants for the OUPES Mega WiFi Client integration."""
from __future__ import annotations

from datetime import timedelta

DOMAIN = "oupes_mega_wifi_client"

# Config entry data keys
CONF_HOST = "host"
CONF_TCP_PORT = "tcp_port"
CONF_HTTP_PORT = "http_port"
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_DEVICE_ID = "device_id"
CONF_DEVICE_KEY = "device_key"
CONF_DEVICE_NAME = "device_name"
CONF_PRODUCT_ID = "product_id"
CONF_TOKEN = "token"

# Config entry option key — user-settable upper bound (minutes) for runtime attrs
# (attr 30 and attr 78). Values above this are filtered as firmware noise.
# Default: ATTR78_RUNTIME_MAX.
CONF_RUNTIME_MAX = "runtime_max_minutes"
# Defaults — match the proxy's default ports
DEFAULT_HOST = "127.0.0.1"
DEFAULT_TCP_PORT = 8896
DEFAULT_HTTP_PORT = 8897

# How long entities stay "available" after the last telemetry update before
# going unavailable. Covers momentary TCP reconnects.
STALE_TIMEOUT = timedelta(minutes=5)

# Attr-78 / attr-30 runtime cap (minutes). Values above this are firmware
# noise emitted during charging/idle.
ATTR78_RUNTIME_MAX = 5940  # 99 h

# ── Product model catalog (shared with oupes_mega) ───────────────────────────

MODEL_CATALOG: dict[str, tuple[str, str]] = {
    "O44A5o": ("Mega 1", "mega_1"),
    "YRWj81": ("Mega 2", "mega"),
    "EFDayi": ("Mega 3", "mega"),
    "JTEnK3": ("Mega 5", "mega"),
    "Hr9Uhd": ("Exodus 1200", "exodus"),
    "pba1j6": ("Exodus 1500", "exodus"),
    "IDaSL8": ("Exodus 2400", "exodus"),
    "gF7XRS": ("S024 Lite", "exodus"),
    "H99Evi": ("S1 Lite", "exodus"),
    "oB6OKs": ("Guardian 6000", "guardian"),
    "LtQmdj": ("HP2500", "guardian"),
    "95haDY": ("D5 V2", "guardian"),
    "xLtGhT": ("S2 V2", "other"),
    "5cY3Mf": ("DC 800", "other"),
    "zcWgyE": ("LP350", "lp"),
    "fckIgv": ("LP700", "lp"),
    "ZlD25j": ("PB300", "portable"),
    "uAsyax": ("UPS 1200", "ups"),
    "QWlryl": ("UPS 1800", "ups"),
}


def model_name_from_product_id(product_id: str | None) -> str:
    if product_id and product_id in MODEL_CATALOG:
        return MODEL_CATALOG[product_id][0]
    return "Power Station"


def series_from_product_id(product_id: str | None) -> str:
    if product_id and product_id in MODEL_CATALOG:
        return MODEL_CATALOG[product_id][1]
    return "unknown"


# ── Telemetry attribute sets (for ext-battery detection) ─────────────────────

EXT_BATTERY_ATTRS: frozenset[int] = frozenset({53, 54, 78, 79, 80})
