"""Constants for the OUPES Mega integration."""
from datetime import timedelta

DOMAIN = "oupes_mega"

# Config entry data keys
CONF_ADDRESS = "address"
CONF_NAME = "name"

# How often to reconnect and pull a fresh telemetry snapshot
UPDATE_INTERVAL = timedelta(minutes=1)

# How long to keep showing the last known values after a failed poll before
# marking entities as unavailable. Covers transient BLE connection failures
# without hiding genuine device-off situations.
STALE_TIMEOUT = timedelta(minutes=10)

# How many seconds to hold the BLE connection collecting notifications per poll
SCAN_DURATION = 15.0

# Max cold-probe retries per coordinator update cycle
MAX_ATTEMPTS = 5
