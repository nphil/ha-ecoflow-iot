"""Constants for the EcoFlow IoT integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

DOMAIN: Final = "ecoflow_iot"

# Config entry keys
CONF_ACCESS_KEY: Final = "access_key"
CONF_SECRET_KEY: Final = "secret_key"
CONF_REGION: Final = "region"

# Transport selector. Absent means the entry is a cloud (Open API) account entry;
# the BLE transport is chosen per device at config time and never mixed into one
# entry, so an entry either talks to the cloud or to one Bluetooth device.
CONF_TRANSPORT: Final = "transport"
TRANSPORT_CLOUD: Final = "cloud"
TRANSPORT_BLE: Final = "ble"


def is_ble_entry(entry: ConfigEntry) -> bool:
    """Whether this entry is served by the local Bluetooth transport.

    Entries created before local Bluetooth existed carry no transport at all,
    which is why the cloud path is the absence of a marker rather than its own.

    Lives here rather than beside the setup it guards so that asking the
    question costs nothing: ``repairs.py`` has to know which transport an entry
    uses, and importing the package root for it would pull in the cloud
    coordinator and the HTTP stack on a Bluetooth-only install.
    """
    return entry.data.get(CONF_TRANSPORT) == TRANSPORT_BLE


# BLE entry keys (transport == TRANSPORT_BLE)
CONF_ADDRESS: Final = "address"
CONF_SERIAL: Final = "serial"
CONF_LOCAL_NAME: Final = "local_name"
CONF_MODEL: Final = "model"
# Name chosen before any entity exists. Naming a device after its entities are
# created leaves their ids built from the old name, so it is asked for up front.
CONF_DEVICE_NAME: Final = "device_name"
# EcoFlow account user ID. Only devices advertising encryption type 7 need it;
# it is the sole thing kept from an optional e-mail/password login.
CONF_USER_ID: Final = "user_id"
CONF_EMAIL: Final = "email"
CONF_PASSWORD: Final = "password"
CONF_LOGIN_REGION: Final = "login_region"
# Seconds between the periodic data requests the device answers over BLE.
CONF_UPDATE_PERIOD: Final = "update_period"
DEFAULT_UPDATE_PERIOD: Final = 10
# Cap on a single BLE connect attempt. Kept short on purpose: habluetooth's
# connect-failure penalty is sticky, so a long blind attempt through the nearest
# proxy is worse than failing fast and letting the next attempt be re-scored.
BLE_CONNECT_TIMEOUT: Final = 10.0
# BLE attempts inside one supervisor pass. Home Assistant re-picks the proxy for
# each of them, so a second attempt is a genuine second path, not a repeat.
BLE_CONNECT_ATTEMPTS: Final = 2
# Outer bound on one supervisor pass: link established *and* authenticated.
BLE_READY_TIMEOUT: Final = 45.0
# How long entry setup waits for the first authenticated link before returning.
# It never fails the entry - the supervisor keeps trying - it only decides how
# long Home Assistant's startup is held for entities that would come up populated.
BLE_SETUP_READY_WAIT: Final = 20.0
# Reconnect backoff, in seconds, held at the last step forever. Retrying is never
# abandoned: a power station out of range for a day must come back on its own.
BLE_BACKOFF_SECONDS: Final = (1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
# Fraction of jitter applied to each backoff step, so several devices that dropped
# together do not line their retries up on the same proxy.
BLE_BACKOFF_JITTER: Final = 0.2
# Window over which link drops are counted for the connection diagnostic.
BLE_DROP_WINDOW_SECONDS: Final = 3600.0
# Total budget for one command: the queue wait and the round trip together, so a
# backlog cannot multiply the wait a user is made to sit through.
BLE_COMMAND_TIMEOUT: Final = 15.0
# Outer bound on releasing a link. Bleak's own disconnect is already capped, but
# a wedged transport can stall inside it and unload must never hang on that.
BLE_DISCONNECT_TIMEOUT: Final = 8.0
# How long the link must be continuously down before the operator is told about
# it with a repair issue. Long enough that the house's own healing has had its
# turn - the hourly re-home and script.ble_heal_device both act well inside it -
# so the repair only ever appears once that machinery has already failed.
BLE_UNREACHABLE_SECONDS: Final = 900.0
# State reported by the connection diagnostic while no link is held. The house
# automations match on this exact string, so it is not a translated value.
LINK_DISCONNECTED: Final = "disconnected"

# Options keys
CONF_POLL_INTERVAL: Final = "poll_interval"
CONF_MQTT_STALE_SECONDS: Final = "mqtt_stale_seconds"
CONF_MQTT_REFRESH_INTERVAL: Final = "mqtt_refresh_interval"
CONF_ENABLE_MQTT: Final = "enable_mqtt"
CONF_MQTT_INSECURE_TLS: Final = "mqtt_insecure_tls"
CONF_INVERT_GRID_SIGN: Final = "invert_grid_sign"
# Transient options-flow checkbox (never persisted): queues a one-shot reset of
# the grid energy totals, applied when the entities are recreated on reload.
CONF_RESET_GRID_ENERGY: Final = "reset_grid_energy"
# Integral-energy sensor keys the reset checkbox zeroes.
RESET_ENERGY_KEYS: Final = ("grid_import_energy", "grid_export_energy")
# hass.data slot holding the set of entity unique_ids queued for reset.
DATA_RESET_ENERGY_IDS: Final = "reset_energy_ids"
# Name of the scanner that last held the BLE link. An unreachable device has no
# holding scanner to discover, so the proxy that was holding it is remembered
# while the link is up - it is the only candidate the recovery flow has left.
CONF_LAST_HOLDING_PROXY: Final = "last_holding_proxy"
# Switch entity the recovery flow power-cycles, remembered from the last run so
# the operator picks their outlet once rather than on every escalation.
CONF_RECOVERY_OUTLET: Final = "recovery_outlet"

# Regions -> REST base URL.
REGION_EU: Final = "eu"
REGION_GLOBAL: Final = "global"

REGION_BASE_URLS: Final[dict[str, str]] = {
    REGION_EU: "https://api-e.ecoflow.com",
    REGION_GLOBAL: "https://api.ecoflow.com",
}
DEFAULT_REGION: Final = REGION_EU

# REST API paths (all under the signed open platform).
PATH_DEVICE_LIST: Final = "/iot-open/sign/device/list"
PATH_QUOTA_ALL: Final = "/iot-open/sign/device/quota/all"
PATH_QUOTA: Final = "/iot-open/sign/device/quota"
PATH_CERTIFICATION: Final = "/iot-open/sign/certification"

# MQTT topic suffixes: /open/{certificateAccount}/{sn}/<suffix>
TOPIC_PREFIX: Final = "/open"
TOPIC_QUOTA: Final = "quota"
TOPIC_STATUS: Final = "status"
TOPIC_SET: Final = "set"
TOPIC_SET_REPLY: Final = "set_reply"
TOPIC_GET: Final = "get"
TOPIC_GET_REPLY: Final = "get_reply"

# operateType that asks a device to report its full latest quota snapshot. This
# is the refresh message the EcoFlow app publishes to the device's get topic
# (reverse-engineered from the Android app's MqttManager.fetchAllDeviceData).
OPERATE_LATEST_QUOTAS: Final = "latestQuotas"

# Defaults / tuning.
DEFAULT_POLL_INTERVAL: Final = 60  # seconds
DEFAULT_MQTT_STALE_SECONDS: Final = 120  # consider MQTT stale after this many seconds
# How often to actively pull fresh data over MQTT by publishing a "latestQuotas"
# get request (0 disables). The official app does this rather than relying purely
# on the broker pushing — devices throttle their push cadence when idle, so a
# passive subscriber sees data go stale even while the connection stays up.
DEFAULT_MQTT_REFRESH_INTERVAL: Final = 20  # seconds
DEFAULT_ENABLE_MQTT: Final = True
DEFAULT_MQTT_INSECURE_TLS: Final = False
# Consecutive poll ticks with the MQTT connection nominally CONNECTED but every
# device stale before the coordinator force-reconnects the broker session. A
# connection can claim to be up while delivering nothing (broker-side
# subscription loss, half-open socket); only a reconnect restores live data.
MQTT_WATCHDOG_TICKS: Final = 3
# Stream firmware reports gridConnectionPower with the opposite sign to Home
# Assistant's grid convention (it reports feeding the grid as POSITIVE, despite
# the docs claiming feed-in is negative). Default to normalising it so that
# import is positive / export is negative; users whose unit already matches HA
# can turn this off.
DEFAULT_INVERT_GRID_SIGN: Final = True
SET_ACK_TIMEOUT: Final = 8.0  # seconds to await an MQTT set_reply before HTTP fallback
# How many leading SN characters identify a device type. Shown to the user for
# unsupported devices (the full serial is never surfaced).
SN_PREFIX_LEN: Final = 4


def redact_sn(sn: str) -> str:
    """Serial as shown in logs and error messages: type prefix + last 3 chars."""
    if len(sn) <= SN_PREFIX_LEN + 3:
        return sn[:SN_PREFIX_LEN] + "…"
    return f"{sn[:SN_PREFIX_LEN]}…{sn[-3:]}"


# Repair issue ids, derived and never remembered: code that has to decide
# whether an issue should exist can name it from the thing it describes, which
# is what makes an unconditional create/delete reconciliation possible.
UNSUPPORTED_ISSUE_PREFIX: Final = "unsupported_device_"


def unreachable_issue_id(address: str) -> str:
    """Issue id for one Bluetooth device's unreachable repair.

    Keyed on the address rather than the entry id so the repair a user is
    looking at survives the entry being removed and added back, and so the fix
    flow can find its entry from the issue id alone.
    """
    return f"{address.replace(':', '').upper()}_unreachable"

# quota/all business code for devices the open API refuses to serve at all
# ("current device is not allowed to get device info"), e.g. Delta Mini, River 2.
API_CODE_DEVICE_NOT_ALLOWED: Final = "1006"

MANUFACTURER: Final = "EcoFlow"

# --- Bundled Lovelace card ---------------------------------------------------
# The integration ships an "EcoFlow Energy" card under ``www/`` and serves that
# whole folder over HTTP at ``/ecoflow_iot`` (the card JS plus device images).
# The card JS is auto-registered as a Lovelace resource in storage mode, so most
# users never have to add it by hand.
CARD_ASSET_BASE: Final = f"/{DOMAIN}"  # serves custom_components/ecoflow_iot/www
CARD_FILENAME: Final = "ecoflow-energy-card.js"
CARD_URL: Final = f"{CARD_ASSET_BASE}/{CARD_FILENAME}"
