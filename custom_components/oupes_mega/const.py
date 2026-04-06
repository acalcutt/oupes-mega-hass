"""Constants for the OUPES Mega integration."""
from datetime import timedelta

DOMAIN = "oupes_mega"

# Config entry data keys
CONF_ADDRESS = "address"
CONF_NAME = "name"

# How often to reconnect and pull a fresh telemetry snapshot
UPDATE_INTERVAL = timedelta(seconds=30)

# How long to keep showing the last known values after a failed poll before
# marking entities as unavailable. Covers transient BLE connection failures
# without hiding genuine device-off situations.
STALE_TIMEOUT = timedelta(minutes=10)

# How many seconds to hold the BLE connection collecting notifications per poll
SCAN_DURATION = 15.0

# Max cold-probe retries per coordinator update cycle
MAX_ATTEMPTS = 5

# Config entry options key — whether to hold the BLE connection open permanently
# instead of polling every UPDATE_INTERVAL seconds.
CONF_CONTINUOUS = "continuous_connection"

# Debug: log unknown/interesting attr values to a CSV in the HA config dir.
# Also emits HA warnings for attr-78 middle-range mystery values.
CONF_DEBUG_ATTRS = "debug_attr_logging"

# Debug: log every raw BLE notification payload as hex to a separate CSV.
CONF_DEBUG_RAW = "debug_raw_logging"

# Attr-78 range boundaries (used in coordinator debug logging)
ATTR78_RUNTIME_MAX  = 6000    # ≤ this → runtime in minutes
ATTR78_MV_MIN       = 44000   # ≥ this (and ≤ MV_MAX) → voltage in mV
ATTR78_MV_MAX       = 58500
