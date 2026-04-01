"""Constants for the OUPES Mega integration."""
from datetime import timedelta

DOMAIN = "oupes_mega"

# Config entry data keys
CONF_ADDRESS = "address"
CONF_NAME = "name"

# How often to reconnect and pull a fresh telemetry snapshot
UPDATE_INTERVAL = timedelta(minutes=1)

# How many seconds to hold the BLE connection collecting notifications per poll
SCAN_DURATION = 15.0

# Max cold-probe retries per coordinator update cycle
MAX_ATTEMPTS = 5
