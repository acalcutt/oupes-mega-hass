"""Constants for the OUPES Mega WiFi Proxy integration."""
from __future__ import annotations

DOMAIN = "oupes_mega_wifi_proxy"

# Config entry keys
CONF_PORT = "port"

# Default TCP port — matches the real cloud broker (47.252.10.9:8896).
# DNS-redirect the device's broker hostname to this HA instance so the device
# connects here instead.
DEFAULT_PORT = 8896

# Sub-entry / user registry keys
CONF_MAIL = "mail"
CONF_PASSWD = "passwd"          # stored as SHA-256 hex digest
CONF_UID = "uid"                # numeric user ID generated for the proxy
CONF_DEVICE_ID = "device_id"
CONF_DEVICE_KEY = "device_key"
CONF_DEVICE_NAME = "device_name"
CONF_MAC_ADDRESS = "mac_address"

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
