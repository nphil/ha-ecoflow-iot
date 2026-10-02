"""Local Bluetooth transport for EcoFlow devices.

A config entry with ``transport == "ble"`` describes exactly one device and is
served entirely from here; cloud entries never enter this package. Platform
modules live in this package too, but Home Assistant only discovers platforms at
the integration root, so the root modules dispatch here for BLE entries.
"""

from __future__ import annotations

import asyncio
import logging
import time
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
from homeassistant.core import HassJob, HomeAssistant, callback
from homeassistant.helpers.importlib import async_import_module
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)

from ..const import (
    BLE_SHUTDOWN_TIMEOUT,
    CONF_ADDRESS,
    CONF_DEVICE_NAME,
    CONF_LOCAL_NAME,
    CONF_MODEL,
    CONF_PREFERRED_PROXY,
    CONF_SERIAL,
    CONF_UPDATE_PERIOD,
    CONF_USER_ID,
    DEFAULT_PREFERRED_PROXY,
    DEFAULT_UPDATE_PERIOD,
    DOMAIN,
)
from . import link_health, unreachable
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
# Set (in `hass.data`, so for the life of the process) by the first shutdown job
# to run. Entries that held no link when it ran - one still waiting for its
# device to advertise - would otherwise be set up by a late advertisement and
# open a link nobody will release.
_SHUTDOWN_KEY = f"{DOMAIN}_ble_shutdown"


async def async_setup_entry(hass: HomeAssistant, entry: EcoFlowBleConfigEntry) -> bool:
    """Set up one EcoFlow device over Bluetooth."""
    # Imported through the executor: a cloud-only installation never pays for
    # protobuf and the crypto stack, and the crypto import's ctypes/libgmp
    # probing never lands on the event loop.
    eflib = await async_import_module(hass, f"{__package__.rpartition('.')[0]}.eflib")
    if hass.data.get(_SHUTDOWN_KEY):
        raise ConfigEntryNotReady("Home Assistant is shutting down")

    address: str = entry.data[CONF_ADDRESS]
    serial: str = entry.data[CONF_SERIAL]

    service_info = bluetooth.async_last_service_info(hass, address, connectable=True)
    if service_info is None:
        # Out of range, or simply not advertising yet - a River 3 Pro emits only
        # two or three advertisements a minute and can go a minute with none. So
        # rather than poll, watch for the next advertisement and retry then;
        # Home Assistant's own setup backoff is the fallback.
        _register_reappear_callback(hass, entry, address)
        # No coordinator exists on this path and none will until the device is
        # heard again, so the countdown towards the unreachable repair is armed
        # from here. A device that was already gone when Home Assistant started
        # is the most complete outage there is, and it is the one a supervisor-
        # owned countdown would silently never report. Setup re-enters here on
        # every retry; the clock behind the countdown is keyed by address and
        # written once, so no retry - and no reload - pushes the deadline out.
        unreachable.async_reconcile(hass, entry, connected=False)
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
    # One job per entry, registered as soon as the object holding the link
    # exists, so a shutdown that lands mid-setup still releases it. HA runs the
    # jobs of all entries in parallel, before it stops the Bluetooth stack.
    # `async_on_unload` also removes the job when setup fails or the entry unloads.
    async def _release_at_shutdown() -> None:
        await _async_release_at_shutdown(hass, entry, coordinator)

    entry.async_on_unload(
        hass.async_add_shutdown_job(
            HassJob(
                _release_at_shutdown,
                f"{DOMAIN} release BLE link {entry.title}",
            )
        )
    )

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
    # Nothing may outlive the entry: a countdown left armed would raise a repair
    # about a device nobody is holding, including one just disabled by hand.
    # Only the timer goes; when the outage started is kept, so the setup a
    # reload runs next resumes the same window rather than starting a new one.
    unreachable.async_cancel(hass, entry)
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    # An entry torn down before it finished setting up has no coordinator, and
    # an unload that raised there would leave the entry stuck in FAILED_UNLOAD.
    if (coordinator := getattr(entry, "runtime_data", None)) is not None:
        await coordinator.async_stop()
    return unloaded


async def _async_release_at_shutdown(
    hass: HomeAssistant,
    entry: EcoFlowBleConfigEntry,
    coordinator: EcoFlowBleCoordinator,
) -> None:
    """Release one entry's Bluetooth link as Home Assistant begins to shut down.

    Home Assistant runs this (stage 1 of its stop sequence, all entries in
    parallel) while the `bluetooth` stack and the proxies' API connections are
    still alive. Waiting for the later stop event is too late: the stack goes
    down on that same event, and the links die uncompleted - ghost links.

    The order is the point. Latch first, so nothing can open a link behind the
    release; then silence the outage watchers, so the deliberate disconnect is
    never recorded as an outage and no repair is raised or deleted; only then
    drop the link. The entry is deliberately NOT unloaded - that would write a
    wave of `unavailable` states over the values restore-state is about to
    keep. Bounded, and it never raises: a failure here must not hold up the
    shutdown (a NameError in this path once aborted a whole restart).
    """
    hass.data[_SHUTDOWN_KEY] = True
    started = time.monotonic()
    try:
        coordinator.close_for_shutdown()
        _cancel_reappear_callback(hass, entry)
        unreachable.async_cancel(hass, entry)
        async with asyncio.timeout(BLE_SHUTDOWN_TIMEOUT):
            await coordinator.async_release_at_shutdown()
    except TimeoutError:
        _LOGGER.warning(
            "Releasing the BLE link to %s did not finish within %.0f s at shutdown",
            coordinator.device_name,
            BLE_SHUTDOWN_TIMEOUT,
        )
    except Exception as err:  # noqa: BLE001 - a shutdown job must never raise
        _LOGGER.warning(
            "Could not release the BLE link to %s at shutdown: %s",
            coordinator.device_name,
            err,
        )
    else:
        _LOGGER.info(
            "Released BLE link to %s at shutdown in %.2f s",
            coordinator.device_name,
            time.monotonic() - started,
        )


async def async_remove_entry(hass: HomeAssistant, entry: EcoFlowBleConfigEntry) -> None:
    """Drop what a removed entry would otherwise leave behind it.

    The advertisement watch a never-loaded entry may still hold, its
    unreachable repair along with any countdown towards one, and the
    short-link/backoff history kept in `hass.data` so a reload never lost it -
    which now outlives the entry it was tracking unless this clears it.
    """
    _cancel_reappear_callback(hass, entry)
    unreachable.async_clear(hass, entry)
    link_health.clear(hass.data.setdefault(link_health.STORE_KEY, {}), entry.data[CONF_ADDRESS])


async def _async_options_updated(
    hass: HomeAssistant, entry: EcoFlowBleConfigEntry
) -> None:
    """Reload for proxy changes; apply the polling interval without a drop."""
    coordinator = entry.runtime_data
    preferred_proxy = (
        entry.options.get(CONF_PREFERRED_PROXY) or DEFAULT_PREFERRED_PROXY
    )
    if coordinator.preferred_proxy != preferred_proxy:
        await hass.config_entries.async_reload(entry.entry_id)
        return
    coordinator.configure(update_period=_update_period(entry))


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
        if hass.data.get(_SHUTDOWN_KEY):
            return  # shutting down: a new link now would never be released
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
