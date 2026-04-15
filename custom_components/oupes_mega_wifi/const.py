"""Constants for the OUPES Mega WiFi integration."""
from __future__ import annotations

DOMAIN = "oupes_mega_wifi"

# ── Proxy / server constants ──────────────────────────────────────────────────

# Config entry keys
CONF_PORT = "port"

# Default TCP port — matches the real cloud broker (47.252.10.9:8896).
# DNS-redirect the device's broker hostname to this HA instance so the device
# connects here instead.
DEFAULT_PORT = 8896

# Sub-entry / user registry keys
CONF_MAIL = "mail"
CONF_PASSWD = "passwd"          # stored as SHA-256 hex digest
CONF_UID = "uid"                # numeric HTTP API user ID (info.uid in login response)
CONF_BROKER_UID = "broker_uid"  # numeric broker/mark UID (mark.uid in login response)
                                # used by the app for device key generation:
                                # device_key = MD5(str(broker_uid))[:10]
CONF_DEVICE_ID = "device_id"
CONF_DEVICE_KEY = "device_key"
CONF_DEVICE_NAME = "device_name"
CONF_MAC_ADDRESS = "mac_address"
CONF_PRODUCT_ID = "product_id"   # populated from device bind; persisted in subentry
CONF_MODEL_OVERRIDE = "model_override"  # "" = auto-detect from device; product_id = manual override

# Validation mode (option on the main config entry)
CONF_VALIDATION_MODE = "validation_mode"
VALIDATION_ACCEPT_ALL = "accept_all"
VALIDATION_LOG_ONLY = "log_only"          # validate + warn, but stay connected
VALIDATION_ACCEPT_REGISTERED = "accept_registered"
DEFAULT_VALIDATION_MODE = VALIDATION_ACCEPT_ALL

# HTTP intercept server (options)
CONF_HTTP_PORT = "http_port"
DEFAULT_HTTP_PORT = 8897

# SiBo (wp-cn.doiting.com) HTTPS mock server (options)
# Intercepts the secondary SiBo cloud calls that would otherwise cause
# "token error" login loops in the Cleanergy app.
CONF_SIBO_PORT = "sibo_https_port"
DEFAULT_SIBO_PORT = 8898

# Debug file logging (options)
CONF_DEBUG_RAW_LINES = "debug_raw_lines"   # log every raw RX/TX protocol line
CONF_DEBUG_TELEMETRY = "debug_telemetry"   # log parsed cmd=10 telemetry objects
CONF_DEBUG_HTTP = "debug_http"             # log every intercepted HTTP request

# ── Coordinator / entity constants ────────────────────────────────────────────

# Config entry option key — user-settable upper bound (minutes) for runtime attrs
# (attr 30 and attr 78). Values above this are filtered as firmware noise.
CONF_RUNTIME_MAX = "runtime_max_minutes"

# Attr-78 / attr-30 runtime cap (minutes). Values above this are firmware
# noise emitted during charging/idle.
ATTR78_RUNTIME_MAX = 5940  # 99 h

# Telemetry attribute set for expansion-battery slot data.
EXT_BATTERY_ATTRS: frozenset[int] = frozenset({53, 54, 78, 79, 80})

# ── Product model catalog ─────────────────────────────────────────────────────

MODEL_CATALOG: dict[str, tuple[str, str]] = {
    "O44A5o": ("Mega 1",        "mega_1"),
    "YRWj81": ("Mega 2",        "mega"),
    "EFDayi": ("Mega 3",        "mega"),
    "JTEnK3": ("Mega 5",        "mega"),
    "Hr9Uhd": ("Exodus 1200",   "exodus"),
    "pba1j6": ("Exodus 1500",   "exodus"),
    "IDaSL8": ("Exodus 2400",   "exodus"),
    "gF7XRS": ("S024 Lite",     "exodus"),
    "H99Evi": ("S1 Lite",       "exodus"),
    "oB6OKs": ("Guardian 6000", "guardian"),
    "LtQmdj": ("HP2500",        "guardian"),
    "95haDY": ("D5 V2",         "guardian"),
    "xLtGhT": ("S2 V2",         "other"),
    "5cY3Mf": ("DC 800",        "other"),
    "zcWgyE": ("LP350",         "lp"),
    "fckIgv": ("LP700",         "lp"),
    "ZlD25j": ("PB300",         "portable"),
    "uAsyax": ("UPS 1200",      "ups"),
    "QWlryl": ("UPS 1800",      "ups"),
}


def model_name_from_product_id(product_id: str | None) -> str:
    """Return the model display name for a product_id, or 'Power Station'."""
    if product_id and product_id in MODEL_CATALOG:
        return MODEL_CATALOG[product_id][0]
    return "Power Station"


def series_from_product_id(product_id: str | None) -> str:
    """Return the model series key, or 'unknown' if not in catalog."""
    if product_id and product_id in MODEL_CATALOG:
        return MODEL_CATALOG[product_id][1]
    return "unknown"


# ── Per-series supported settings ─────────────────────────────────────────────
# Maps series key → set of setting DPID numbers the series is known to support.
# Source: StandByTimeoutFragment, ECOFragment, S2_V2SettingFragment, etc.
# in the decompiled Cleanergy APK.
#
# DPID numbers are from the *settings* namespace (distinct from telemetry attrs):
#   41 = screen timeout, 45-49 = standby timeouts, 58 = breath light,
#   63 = silent mode, 110-113 = ECO mode (Exodus only).
# "unknown" series gets a conservative safe set.

# Mega series (1/2/3/5): settings exposed by S2_V2SettingFragment.  No ECO mode.
_MEGA_SETTINGS: frozenset[int] = frozenset({
    41,   # screen/display timeout
    45,   # machine standby timeout
    46,   # WiFi standby timeout
    47,   # USB/car standby timeout
    49,   # AC standby timeout
    58,   # breath light
    63,   # silent mode
    105,  # charge mode (fast/slow)
})

# Guardian series: same as Mega but also has a physical XT90 DC output port.
_GUARDIAN_SETTINGS: frozenset[int] = _MEGA_SETTINGS | frozenset({
    48,   # XT90 standby timeout (Guardian has 12V/24V XT90 output; Mega does not)
})

# Exodus series: ECO mode visible in DeviceSettingFragment for Hr9Uhd/pba1j6/IDaSL8.
_EXODUS_SETTINGS: frozenset[int] = frozenset({
    41,   # screen/display timeout
    58,   # breath light
    63,   # silent mode
    105,  # charge mode (fast/slow)
    110,  # AC ECO switch
    111,  # AC ECO threshold
    112,  # DC ECO switch
    113,  # DC ECO threshold
})

SERIES_SETTINGS: dict[str, frozenset[int]] = {
    # mega_1 uses the same settings as mega but is a separate key so that
    # binary_sensor.py / switch.py can give bit2 a different name:
    #   mega_1 → "USB Output"  (bit2 is USB-A/C only, no Anderson port)
    #   mega   → "Anderson & USB Output"  (bit2 controls Anderson+USB together)
    "mega_1":   _MEGA_SETTINGS,
    "mega":     _MEGA_SETTINGS,
    "exodus":   _EXODUS_SETTINGS,
    "guardian": _GUARDIAN_SETTINGS,
    "lp":       frozenset({41, 45, 47, 49, 58, 63}),  # no XT90, no ECO
    "portable": frozenset({41, 45, 47, 49, 58, 63}),
    "ups":      frozenset({41, 45, 49, 58, 63}),
    "other":    frozenset({41, 45, 49, 58, 63}),
    "unknown":  frozenset({41, 45, 49, 58, 63}),  # safe minimal set
}
