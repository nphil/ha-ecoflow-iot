"""Whether the operator has been told this device is unreachable, and when to be.

Owned by the config *entry* rather than by the link supervisor, because the worst
outage there is - a device already gone by the time Home Assistant starts - never
produces a supervisor at all: setup fails with ``ConfigEntryNotReady`` before a
coordinator exists and the entry sits retrying behind an advertisement watch. A
countdown living on the coordinator would never run for exactly the case that
most needs reporting, so it lives here and both paths call the same reconcile.

The rule every caller obeys:

    Never gate the delete on remembered state. Reality is the live link and the
    issue registry; nothing here consults what a previous instance believed.
    ``async_create_issue`` and ``async_delete_issue`` are both idempotent, so
    reconciling unconditionally is safe - and it is the only form that survives
    an entry reload, which takes any in-memory flag with it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later

from ..const import (
    BLE_UNREACHABLE_SECONDS,
    CONF_ADDRESS,
    CONF_DEVICE_NAME,
    DOMAIN,
    unreachable_issue_id,
)

_LOGGER = logging.getLogger(__name__)

# Armed countdowns by entry id: one entry, at most one timer.
_TIMERS_KEY = f"{DOMAIN}_ble_unreachable"


@callback
def async_reconcile(
    hass: HomeAssistant, entry: ConfigEntry, *, connected: bool
) -> None:
    """Make the unreachable repair match the link as it is right now.

    Called on every health transition, once during entry setup, and once on the
    setup path that fails because the device is not being heard at all.
    """
    issue_id = unreachable_issue_id(entry.data[CONF_ADDRESS])

    if connected:
        async_cancel(hass, entry)
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return

    if ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None:
        # A reload does not heal a link. An issue already on the registry is the
        # durable record that this device passed the threshold, so it is
        # re-asserted rather than re-timed - restarting the clock would grant
        # the fault another quiet quarter of an hour per reload, and a device
        # down for a day would never manage to raise one. Re-asserting also
        # upgrades an issue raised by an older release whose severity or Fix
        # button differed; an identical one is a no-op inside the registry.
        _async_raise(hass, entry, issue_id)
        return

    _async_arm(hass, entry, issue_id)


@callback
def async_cancel(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Disarm the countdown; safe to call more than once.

    Called when the link comes up and when the entry unloads - a countdown that
    outlived its entry would report on a device nobody is holding, including one
    the user has just disabled.
    """
    if cancel := hass.data.get(_TIMERS_KEY, {}).pop(entry.entry_id, None):
        cancel()


@callback
def async_clear(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Forget this device's repair entirely: the entry itself is going away.

    Nothing else would ever delete an issue whose device has been removed, and
    it names a device the user can no longer act on.
    """
    async_cancel(hass, entry)
    ir.async_delete_issue(hass, DOMAIN, unreachable_issue_id(entry.data[CONF_ADDRESS]))


@callback
def _async_arm(hass: HomeAssistant, entry: ConfigEntry, issue_id: str) -> None:
    """Start the countdown, unless one is already running for this entry."""
    timers: dict[str, Callable[[], None]] = hass.data.setdefault(_TIMERS_KEY, {})
    if entry.entry_id in timers:
        # Setup re-runs on every retry of an entry whose device is away, and
        # none of those may push the deadline out: the device has been gone
        # since the first one, which is the moment the operator cares about.
        return

    @callback
    def _elapsed(_now: datetime) -> None:
        """The link never came back inside the threshold; tell the operator.

        By now the household's own healing has had its turn - the hourly re-home
        and ``script.ble_heal_device`` both act well inside this window - so the
        repair only ever appears once that has failed too. No re-check of the
        link is needed here: a link coming up cancels this timer.
        """
        hass.data.get(_TIMERS_KEY, {}).pop(entry.entry_id, None)
        _LOGGER.warning(
            "%s: no Bluetooth link for %s minutes; raising a repair issue",
            _device_name(entry),
            int(BLE_UNREACHABLE_SECONDS // 60),
        )
        _async_raise(hass, entry, issue_id)

    timers[entry.entry_id] = async_call_later(hass, BLE_UNREACHABLE_SECONDS, _elapsed)


@callback
def _async_raise(hass: HomeAssistant, entry: ConfigEntry, issue_id: str) -> None:
    """Raise (or refresh) the repair, with a Fix button onto the recovery flow."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="device_unreachable",
        translation_placeholders={
            "device": _device_name(entry),
            "address": entry.data[CONF_ADDRESS],
            "minutes": str(int(BLE_UNREACHABLE_SECONDS // 60)),
        },
    )


def _device_name(entry: ConfigEntry) -> str:
    """What the user calls this device.

    Read off the entry, never off a coordinator, so the repair reads the same
    whether or not the entry got far enough to build one. The title comes first
    because it is what Home Assistant shows everywhere else and the only one of
    the two that follows a rename in the UI; the name chosen at config time is
    the fallback for an entry left with no title at all.
    """
    return entry.title or entry.data.get(CONF_DEVICE_NAME, "")
