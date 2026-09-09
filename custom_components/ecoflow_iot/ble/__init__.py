"""Local Bluetooth transport for EcoFlow devices.

A config entry with ``transport == "ble"`` describes exactly one device and is
served entirely from here; cloud entries never enter this package. Platform
modules live in this package too, but Home Assistant only discovers platforms at
the integration root, so the root modules dispatch here for BLE entries.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.importlib import async_import_module
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)

from ..const import (
    CONF_ADDRESS,
    CONF_DEVICE_NAME,
    CONF_LOCAL_NAME,
    CONF_MODEL,
    CONF_SERIAL,
    CONF_UPDATE_PERIOD,
    CONF_USER_ID,
    DEFAULT_UPDATE_PERIOD,
    DOMAIN,
)
from .coordinator import EcoFlowBleCoordinator

_LOGGER = logging.getLogger(__name__)

type EcoFlowBleConfigEntry = ConfigEntry[EcoFlowBleCoordinator]

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

_REAPPEAR_KEY = f"{DOMAIN}_ble_reappear"


async def async_setup_entry(hass: HomeAssistant, entry: EcoFlowBleConfigEntry) -> bool:
    """Set up one EcoFlow device over Bluetooth."""
    # Imported through the executor: a cloud-only installation never pays for
    # protobuf and the crypto stack, and the crypto import's ctypes/libgmp
    # probing never lands on the event loop.
    eflib = await async_import_module(hass, f"{__package__.rpartition('.')[0]}.eflib")

    address: str = entry.data[CONF_ADDRESS]
    serial: str = entry.data[CONF_SERIAL]

    service_info = bluetooth.async_last_service_info(hass, address, connectable=True)
    if service_info is None:
        # Out of range, or simply not advertising yet - a River 3 Pro emits only
        # two or three advertisements a minute and can go a minute with none. So
        # rather than poll, watch for the next advertisement and retry then;
        # Home Assistant's own setup backoff is the fallback.
        _register_reappear_callback(hass, entry, address)
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="device_not_present",
            translation_placeholders={"address": address},
        )

    _cancel_reappear_callback(hass, entry)

    if not entry.data.get(CONF_USER_ID):
        # Every session authenticates with MD5(user_id + serial), whatever the
        # encryption type, so a link cannot even be attempted without one. Ask
        # rather than retry: no amount of reconnecting produces an account id.
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN,
            translation_key="user_id_missing",
        )

    device = eflib.new_device(service_info.device, service_info.advertisement)
    if device is None:
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="not_supported",
            translation_placeholders={"serial": serial},
        )

    if device.serial_number != serial:
        # A Bluetooth address can end up on a different unit (a replacement, or
        # a reused private address). Adopting it would hang another device's
        # values off this one's entity ids, so refuse and let the user re-add.
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="serial_mismatch",
            translation_placeholders={
                "address": address,
                "expected": serial,
                "found": device.serial_number,
            },
        )

    coordinator = EcoFlowBleCoordinator(
        hass,
        entry,
        device,
        serial=serial,
        address=address,
        model=entry.data.get(CONF_MODEL) or device.device,
        local_name=entry.data.get(CONF_LOCAL_NAME) or device.name,
        device_name=entry.data.get(CONF_DEVICE_NAME) or "",
        user_id=entry.data.get(CONF_USER_ID),
    )
    coordinator.configure(update_period=_update_period(entry))

    if (auth_error := await coordinator.async_start()) is not None:
        await coordinator.async_stop()
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN,
            translation_key="authentication_failed",
            translation_placeholders={"error": str(auth_error)},
        )

    entry.runtime_data = coordinator
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        # The supervisor is already holding a link; a half-set-up entry must not
        # leave it, and its background task, running unowned.
        await coordinator.async_stop()
        raise
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EcoFlowBleConfigEntry) -> bool:
    """Tear the entry down, releasing the link whether or not platforms unload."""
    _cancel_reappear_callback(hass, entry)
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    # An entry torn down before it finished setting up has no coordinator, and
    # an unload that raised there would leave the entry stuck in FAILED_UNLOAD.
    if (coordinator := getattr(entry, "runtime_data", None)) is not None:
        await coordinator.async_stop()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: EcoFlowBleConfigEntry) -> None:
    """Drop the advertisement watch a never-loaded entry may have left behind."""
    _cancel_reappear_callback(hass, entry)


async def _async_options_updated(
    hass: HomeAssistant, entry: EcoFlowBleConfigEntry
) -> None:
    """Apply changed options in place; reloading would drop a healthy link."""
    entry.runtime_data.configure(update_period=_update_period(entry))


def _update_period(entry: EcoFlowBleConfigEntry) -> int:
    return int(entry.options.get(CONF_UPDATE_PERIOD, DEFAULT_UPDATE_PERIOD))


def _register_reappear_callback(
    hass: HomeAssistant, entry: ConfigEntry, address: str
) -> None:
    """Retry setup as soon as the device is heard from again."""
    watches: dict[str, Callable[[], None]] = hass.data.setdefault(_REAPPEAR_KEY, {})
    if entry.entry_id in watches:
        return

    @callback
    def _on_reappear(
        service_info: BluetoothServiceInfoBleak, change: BluetoothChange
    ) -> None:
        _cancel_reappear_callback(hass, entry)
        # The entry may have been removed or reloaded while we were waiting; only
        # a still-retrying entry has anything to gain from being retried now.
        current = hass.config_entries.async_get_entry(entry.entry_id)
        if current is None or current.state is not ConfigEntryState.SETUP_RETRY:
            return
        _LOGGER.debug("%s advertised again; retrying setup", address)
        hass.config_entries.async_schedule_reload(entry.entry_id)

    watches[entry.entry_id] = bluetooth.async_register_callback(
        hass,
        _on_reappear,
        BluetoothCallbackMatcher(address=address, connectable=True),
        BluetoothScanningMode.PASSIVE,
    )


def _cancel_reappear_callback(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Release the watch if one is held; safe to call more than once."""
    if cancel := hass.data.get(_REAPPEAR_KEY, {}).pop(entry.entry_id, None):
        cancel()
