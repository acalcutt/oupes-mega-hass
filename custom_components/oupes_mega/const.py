"""Constants for the OUPES Mega integration."""
from __future__ import annotations

from datetime import timedelta

DOMAIN = "oupes_mega"

# Config entry data keys
CONF_ADDRESS = "address"
CONF_NAME = "name"
CONF_DEVICE_ID = "device_id"
CONF_PRODUCT_ID = "product_id"

# How often to reconnect and pull a fresh telemetry snapshot
UPDATE_INTERVAL = timedelta(seconds=30)

# How long to keep showing the last known values after a failed poll before
# marking entities as unavailable. Covers transient BLE connection failures
# without hiding genuine device-off situations.
STALE_TIMEOUT = timedelta(minutes=15)

# How many seconds to hold the BLE connection collecting notifications per poll
SCAN_DURATION = 15.0

# Max cold-probe retries per coordinator update cycle
MAX_ATTEMPTS = 5

# Config entry data key — the per-device 10-character hex init token.
# Found at bytes 4–13 of BLE init packet 6 (from a btsnoop/PCAPdroid capture).
# Per-device; obtained from the cloud API (auto) or a packet capture (manual).
CONF_DEVICE_KEY = "device_key"

# Config entry options key — whether to hold the BLE connection open permanently
# instead of polling every UPDATE_INTERVAL seconds.
CONF_CONTINUOUS = "continuous_connection"

# Config entry options — user-tuneable polling parameters (non-continuous mode).
CONF_POLL_INTERVAL = "poll_interval"        # seconds between polls (default 30)
CONF_STALE_TIMEOUT = "stale_timeout"        # minutes before marking unavailable (default 15)

# Debug: log unknown/interesting attr values to a CSV in the HA config dir.
# Also emits HA warnings for attr-78 middle-range mystery values.
CONF_DEBUG_ATTRS = "debug_attr_logging"

# Debug: log every raw BLE notification payload as hex to a separate CSV.
CONF_DEBUG_RAW = "debug_raw_logging"

# Attr-78 range boundaries (used in coordinator debug logging)
ATTR78_RUNTIME_MAX  = 6000    # ≤ this → runtime in minutes
ATTR78_MV_MIN       = 44000   # ≥ this (and ≤ MV_MAX) → voltage in mV
ATTR78_MV_MAX       = 58500


# ── Product model catalog ─────────────────────────────────────────────────────
# Maps the 6-char ASCII product_id from BLE advertising to (model_name, series).
# Source: AppParams.java in Cleanergy APK v1.4.1.

MODEL_CATALOG: dict[str, tuple[str, str]] = {
    "O44A5o": ("Mega 1", "mega"),
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
    """Return human-readable model name, or 'Power Station' if unknown."""
    if product_id and product_id in MODEL_CATALOG:
        return MODEL_CATALOG[product_id][0]
    return "Power Station"


def series_from_product_id(product_id: str | None) -> str:
    """Return the model series key, or 'unknown' if not in catalog."""
    if product_id and product_id in MODEL_CATALOG:
        return MODEL_CATALOG[product_id][1]
    return "unknown"


# ── Per-series feature flags ──────────────────────────────────────────────────
# Maps series key → set of setting DPID numbers the series is known to support.
# Source: StandByTimeoutFragment, ECOFragment, S2_V2SettingFragment, etc.
# in the decompiled Cleanergy APK.
#
# DPID numbers are from the *settings* namespace (distinct from telemetry attrs):
#   41 = screen timeout, 45-49 = standby timeouts, 58 = breath light,
#   63 = silent mode, 110-113 = ECO mode (Exodus only).
# "unknown" series gets a conservative safe set.

# Mega series: settings exposed by S2_V2SettingFragment.  No ECO mode.
_MEGA_SETTINGS: frozenset[int] = frozenset({
    41,   # screen/display timeout
    45,   # machine standby timeout
    46,   # WiFi standby timeout
    47,   # USB/car standby timeout
    48,   # XT90 standby timeout
    49,   # AC standby timeout
    58,   # breath light
    63,   # silent mode
    105,  # charge mode (fast/slow)
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
    "mega":     _MEGA_SETTINGS,
    "exodus":   _EXODUS_SETTINGS,
    "guardian":  _MEGA_SETTINGS,  # assumed same as mega until tested
    "lp":       frozenset({41, 45, 47, 49, 58, 63}),  # no XT90, no ECO
    "portable": frozenset({41, 45, 47, 49, 58, 63}),
    "ups":      frozenset({41, 45, 49, 58, 63}),
    "other":    frozenset({41, 45, 49, 58, 63}),
    "unknown":  frozenset({41, 45, 49, 58, 63}),  # safe minimal set
}
