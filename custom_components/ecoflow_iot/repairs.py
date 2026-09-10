"""Fix flow for a Bluetooth device that stopped coming back.

The supervisor in ``ble/coordinator.py`` retries forever and the house has its
own healing (the hourly re-home, ``script.ble_heal_device``), so by the time the
unreachable repair exists all of that has already failed. What is left is the
ladder someone would climb by hand, in the order they would climb it: look
again, reload the entry, restart the proxy in front of the device, and only then
cut its mains. Each rung performs its action, waits for the link using the same
predicate the entities take their availability from, and drops back to the menu
saying what happened - the flow only completes when a link is genuinely up.

Nothing here imports the Bluetooth stack. This module is loaded for every
install that has the integration, cloud-only ones included, and the whole point
of ``ble`` being imported through the executor is that those installs never pay
for protobuf and the crypto stack. The coordinator is reached through the three
attributes the ladder needs (``connected``, ``holding_scanner``, ``link_state``)
and the entry's own options, which costs no import at all.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import EntitySelector, EntitySelectorConfig
from homeassistant.util import slugify

from .const import (
    CONF_ADDRESS,
    CONF_LAST_HOLDING_PROXY,
    CONF_RECOVERY_OUTLET,
    DOMAIN,
    LINK_DISCONNECTED,
    is_ble_entry,
    unreachable_issue_id,
)

if TYPE_CHECKING:
    from .ble.coordinator import EcoFlowBleCoordinator

_LOGGER = logging.getLogger(__name__)

# ESPHome exposes one restart button per Bluetooth-proxy node, as
# `esphome.<node name slug>_restart_proxy`. Nothing can be asked which node a
# scanner belongs to, so the action id is derived from the scanner's own name
# and the rung is only offered when a service by that exact name is registered.
_ESPHOME_DOMAIN = "esphome"
_RESTART_PROXY_SUFFIX = "_restart_proxy"

# How long a rung waits for the link after acting. Generous enough to cover a
# proxy rebooting and the device advertising again plus a supervisor backoff
# step, short enough that the operator is not left staring at a spinner.
_SETTLE_TIMEOUT = 45.0
# A power-cycled device has to boot before it advertises at all, so its wait is
# longer, and the cut itself has to outlast the unit's own hold-up capacitors.
_POWER_OFF_SECONDS = 10.0
_POWER_CYCLE_TIMEOUT = 60.0
# Polling step while waiting: small enough that a link coming up is noticed
# almost at once, and awaited rather than slept through so nothing blocks.
_POLL_STEP = 1.0

_STEP_RECHECK = "recheck"
_STEP_RELOAD = "reload"
_STEP_RESTART_PROXY = "restart_proxy"
_STEP_POWER_CYCLE = "power_cycle"

# Entry states the ladder can still act on. SETUP_RETRY belongs here: a device
# out of range when Home Assistant started fails setup exactly that way, which
# is precisely when someone reaches for this flow, and both reload and power
# cycle work on an entry that never loaded. Anything else - disabled, a setup
# error such as a serial mismatch, a failed unload - needs a decision from the
# user that no amount of restarting proxies will supply.
_ACTIONABLE_STATES = (ConfigEntryState.LOADED, ConfigEntryState.SETUP_RETRY)


class BleRecoveryFixFlow(RepairsFlow):
    """Walk the recovery ladder for one Bluetooth entry, cheapest rung first."""

    def __init__(self, entry_id: str) -> None:
        """Remember which entry this repair belongs to, never its objects.

        Everything else is looked up per step: a reload puts a new coordinator
        on the entry, so a handle taken now would be reporting on a link that
        nobody holds any more.
        """
        self._entry_id = entry_id
        # What the previous rung did, reported on the menu. Empty on first
        # entry: a literal "None" mid-sentence is worse than a gap.
        self._last_result = ""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Repairs always enters at ``init``; the ladder lives on the menu."""
        return await self.async_step_menu()

    async def async_step_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Offer the rungs that can actually be climbed right now."""
        entry = self._entry()
        if entry is None:
            return self.async_abort(reason="entry_gone")
        if entry.state not in _ACTIONABLE_STATES:
            return self.async_abort(reason="not_loaded")

        options = [_STEP_RECHECK, _STEP_RELOAD]
        if _proxy_action(self.hass, _known_proxy(entry)) is not None:
            # Withheld when no proxy is known, or the known one has no restart
            # action (a built-in adapter, or an ESPHome node without the
            # button): the rung would only lead to a step that can do nothing.
            options.append(_STEP_RESTART_PROXY)
        options.append(_STEP_POWER_CYCLE)

        return self.async_show_menu(
            step_id="menu",
            menu_options=options,
            description_placeholders=self._placeholders(entry),
        )

    async def async_step_recheck(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Change nothing and look again; the supervisor may be mid-attempt."""
        if self._entry() is None:
            return self.async_abort(reason="entry_gone")
        return await self._async_settle(
            _SETTLE_TIMEOUT, "Waited for the supervisor's own reconnect"
        )

    async def async_step_reload(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Rebuild the entry, which rebuilds the link from nothing."""
        entry = self._entry()
        if entry is None:
            return self.async_abort(reason="entry_gone")

        # Through the config entry rather than at the supervisor: a reload
        # releases the link, drops the device object and starts a fresh
        # supervisor, where poking the running one would only queue an attempt
        # it was about to make anyway.
        await self.hass.config_entries.async_reload(entry.entry_id)
        return await self._async_settle(_SETTLE_TIMEOUT, "Reloaded the integration")

    async def async_step_restart_proxy(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Ask the ESPHome node that holds (or last held) the link to restart."""
        entry = self._entry()
        if entry is None:
            return self.async_abort(reason="entry_gone")

        proxy = _known_proxy(entry)
        action = _proxy_action(self.hass, proxy)
        if action is None:
            # The action went away between the menu being drawn and this step:
            # the node was removed, renamed, or its own entry unloaded.
            self._last_result = "The proxy's restart action is no longer available"
            return await self.async_step_menu()

        await self.hass.services.async_call(_ESPHOME_DOMAIN, action, blocking=True)
        # Deliberately not phrased as a reboot. The proxy firmware refuses to
        # restart inside its first 20 minutes of uptime ("refused: up only N
        # s") and the action reports success either way, so nothing here can
        # tell a reboot from a refusal - claiming one would be a guess.
        return await self._async_settle(
            _SETTLE_TIMEOUT,
            f"Asked {proxy} to restart (it may have refused if it booted recently)",
        )

    async def async_step_power_cycle(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Cut and restore the device's mains supply: the last rung."""
        entry = self._entry()
        if entry is None:
            return self.async_abort(reason="entry_gone")

        if user_input is None:
            stored = entry.options.get(CONF_RECOVERY_OUTLET)
            outlet_key = (
                vol.Required(CONF_RECOVERY_OUTLET, default=stored)
                if stored
                else vol.Required(CONF_RECOVERY_OUTLET)
            )
            return self.async_show_form(
                step_id=_STEP_POWER_CYCLE,
                data_schema=vol.Schema(
                    {
                        outlet_key: EntitySelector(
                            EntitySelectorConfig(domain=Platform.SWITCH)
                        )
                    }
                ),
                description_placeholders=self._placeholders(entry),
            )

        outlet: str = user_input[CONF_RECOVERY_OUTLET]
        if entry.options.get(CONF_RECOVERY_OUTLET) != outlet:
            # Remembered so the next escalation prefills it. Merged, never
            # replaced: options also carry the update period and the proxy the
            # coordinator recorded.
            self.hass.config_entries.async_update_entry(
                entry, options={**entry.options, CONF_RECOVERY_OUTLET: outlet}
            )

        _LOGGER.warning(
            "Power-cycling %s through %s to recover its Bluetooth link",
            entry.title,
            outlet,
        )
        # The turn-off is outside the guard on purpose: if it fails, nothing was
        # cut and there is nothing to restore.
        await self._async_switch(outlet, SERVICE_TURN_OFF)
        try:
            await asyncio.sleep(_POWER_OFF_SECONDS)
        finally:
            # The mains comes back even if this flow does not. Closing the
            # repair dialog cancels this task mid-cut, and a power station left
            # dark because a browser tab was shut is far worse than the fault
            # being unfixed - so the restore is shielded and survives the very
            # cancellation that reached this line.
            await asyncio.shield(self._async_switch(outlet, SERVICE_TURN_ON))
        return await self._async_settle(
            _POWER_CYCLE_TIMEOUT, f"Power-cycled the device through {outlet}"
        )

    # -- internals --------------------------------------------------------------

    async def _async_switch(self, outlet: str, service: str) -> None:
        await self.hass.services.async_call(
            Platform.SWITCH, service, {ATTR_ENTITY_ID: outlet}, blocking=True
        )

    async def _async_settle(
        self, timeout: float, tried: str
    ) -> data_entry_flow.FlowResult:
        """Wait out ``timeout`` for a link, then finish or fall back to the menu."""
        if await self._async_wait_for_link(timeout):
            # Home Assistant deletes the issue when a fix flow completes, and
            # the coordinator's own reconciliation deletes it the moment a link
            # comes up. Both run; neither depends on the other having run.
            if (coordinator := self._coordinator()) is not None:
                coordinator.reconcile_unreachable_issue()
            return self.async_create_entry(data={})

        self._last_result = f"{tried}: still no link after {int(timeout)}s"
        return await self.async_step_menu()

    async def _async_wait_for_link(self, timeout: float) -> bool:
        """Poll the link for up to ``timeout`` seconds, yielding between checks."""
        deadline = self.hass.loop.time() + timeout
        while True:
            if self._connected():
                return True
            if self.hass.loop.time() >= deadline:
                return False
            await asyncio.sleep(_POLL_STEP)

    def _entry(self) -> ConfigEntry | None:
        return self.hass.config_entries.async_get_entry(self._entry_id)

    def _coordinator(self) -> EcoFlowBleCoordinator | None:
        """The live coordinator, re-resolved every time it is asked for.

        A reload leaves the entry in place and puts a *new* coordinator on it,
        so anything cached across a rung would answer for a link that no longer
        exists. An entry that has not loaded has no coordinator at all.
        """
        entry = self._entry()
        return None if entry is None else getattr(entry, "runtime_data", None)

    def _connected(self) -> bool:
        """The integration's own notion of an established link, and only that."""
        coordinator = self._coordinator()
        return coordinator is not None and coordinator.connected

    def _placeholders(self, entry: ConfigEntry) -> dict[str, str]:
        """What the menu and the power-cycle form report back to the operator.

        The unknown proxy is a dash rather than an empty string: an em dash
        needs no translating and reads as an answer, where a gap reads as a
        rendering bug. ``last_result`` is genuinely empty first time round -
        nothing has been tried yet, and the sentence "None" would be a lie.
        """
        coordinator = self._coordinator()
        return {
            "device": entry.title,
            "link": (
                coordinator.link_state if coordinator is not None else LINK_DISCONNECTED
            ),
            "proxy": _known_proxy(entry) or "—",
            "last_result": self._last_result,
        }


def _known_proxy(entry: ConfigEntry) -> str | None:
    """The proxy to act on: the one holding the link, else the last one that did.

    An unreachable device has no holding scanner - which is exactly when this is
    asked - so the remembered name is normally the only answer there is. The
    live one still wins when it exists, because a link that came back through a
    different proxy makes the remembered name stale.
    """
    coordinator: EcoFlowBleCoordinator | None = getattr(entry, "runtime_data", None)
    if coordinator is not None and (live := coordinator.holding_scanner):
        return live
    return entry.options.get(CONF_LAST_HOLDING_PROXY)


def _proxy_action(hass: HomeAssistant, proxy: str | None) -> str | None:
    """The ESPHome restart action for a scanner name, if that node exposes one.

    ``habluetooth`` names an ESPHome scanner "<node name> (<MAC>)" and only the
    node name is part of its action ids, so the address is dropped before
    slugifying: "plant-room-bluetooth-proxy (E8:...)" becomes
    ``esphome.plant_room_bluetooth_proxy_restart_proxy``.
    """
    if not proxy:
        return None
    action = f"{slugify(proxy.split(' (')[0])}{_RESTART_PROXY_SUFFIX}"
    # `has_service` rather than `async_services()`: the latter deep-copies every
    # service in the instance, which its own docstring says to avoid when one
    # exact name is all that is being asked about - and this runs per menu draw.
    return action if hass.services.has_service(_ESPHOME_DOMAIN, action) else None


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None
) -> RepairsFlow:
    """Build the recovery flow for the Bluetooth entry the issue id names.

    The entry is found by address rather than by anything carried in the issue,
    so a repair raised before the entry was removed and added back still
    resolves - and a cloud entry can never match, because only a Bluetooth entry
    has an address. An id that matches nothing still has to return a flow, and
    that one aborts on its first step rather than pretending to have an entry.
    """
    for entry in hass.config_entries.async_entries(DOMAIN):
        if not is_ble_entry(entry):
            continue
        if unreachable_issue_id(entry.data[CONF_ADDRESS]) == issue_id:
            return BleRecoveryFixFlow(entry.entry_id)
    return BleRecoveryFixFlow("")
