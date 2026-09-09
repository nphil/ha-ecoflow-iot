"""Holds one persistent Bluetooth link to an EcoFlow device.

Everything about the link lives here: a single supervisor task owns connecting,
authenticating, holding the link idle while it is healthy, and reconnecting for
as long as the config entry is loaded. Entities never connect, never reconnect
and never see a disconnected device object - they read pushed values and take
their availability from :attr:`EcoFlowBleCoordinator.connected`.

Deliberate non-behaviours, each of which cost this house time before:

* No scanner is ever pinned. Every attempt hands Home Assistant a freshly
  resolved ``BLEDevice`` and lets ``habluetooth`` score the proxies, so a device
  roams because nothing chose for it, not because something forced a hop.
* A healthy link is never torn down and re-established - not for a command, not
  for a refresh, not on a timer. Teardowns are where ghost ACLs come from.
* A failed attempt never escalates into a config entry reload. The supervisor
  backs off and retries forever; Home Assistant is only told the entry needs
  attention when the *credentials* are wrong, which retrying cannot fix.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from ..const import (
    BLE_BACKOFF_JITTER,
    BLE_BACKOFF_SECONDS,
    BLE_COMMAND_TIMEOUT,
    BLE_CONNECT_ATTEMPTS,
    BLE_CONNECT_TIMEOUT,
    BLE_DISCONNECT_TIMEOUT,
    BLE_DROP_WINDOW_SECONDS,
    BLE_READY_TIMEOUT,
    BLE_SETUP_READY_WAIT,
    DEFAULT_UPDATE_PERIOD,
    DOMAIN,
    MANUFACTURER,
)
from ..eflib import DeviceBase
from ..eflib.connection import Connection
from ..eflib.exceptions import AuthErrors

_LOGGER = logging.getLogger(__name__)

# State reported by the connection diagnostic while no link is held. The house
# automations match on this exact string, so it is not a translated value.
LINK_DISCONNECTED = "disconnected"

# Authentication failures retrying cannot fix: the device is telling us the
# account behind the user ID is wrong, stale or not entitled to this unit. Every
# other `BaseAuthException` (an internal device error, a malformed exchange) is
# a fault of the moment and is retried like any other failure - treating one of
# those as fatal would leave a healthy device dead until someone noticed.
_CREDENTIAL_ERRORS: tuple[type[Exception], ...] = (
    AuthErrors.WrongKey,
    AuthErrors.NeedRefreshToken,
    AuthErrors.DeviceAlreadyBound,
    AuthErrors.NeedBindInstallFirst,
    AuthErrors.MaximumDevicesError,
)


class DeviceNotSeen(Exception):
    """No usable advertisement for the device, so there is nothing to connect to."""


class LinkNotReady(Exception):
    """The connection settled in a state that is not an authenticated link."""


class EcoFlowBleCoordinator(DataUpdateCoordinator[None]):
    """Owns the BLE link for one device and publishes its pushed values."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device: DeviceBase,
        *,
        serial: str,
        address: str,
        model: str,
        local_name: str,
        device_name: str,
        user_id: str | None,
    ) -> None:
        """Prepare the link without touching Bluetooth; see :meth:`async_start`."""
        super().__init__(
            hass,
            _LOGGER,
            name=device_name or local_name or serial,
            config_entry=entry,
            # Values arrive as the device pushes them; there is nothing to poll.
            update_interval=None,
        )
        self.device = device
        self.serial = serial
        self.address = address
        self.model_name = model
        # What the user called the device, chosen before any entity existed;
        # the advertised name is only the fallback for entries predating that.
        self.device_name = device_name or local_name or serial
        self.local_name = local_name
        self._user_id = user_id
        self._update_period = DEFAULT_UPDATE_PERIOD

        self._command_lock = asyncio.Lock()
        self._supervisor: asyncio.Task[None] | None = None
        self._settled = asyncio.Event()
        self._auth_error: Exception | None = None
        self._hold = False
        self._connected = False
        self._notify_scheduled = False
        self._reconnect_attempt = 0
        self._scanner_source: str | None = None
        self._remove_frame_listener: Callable[[], None] | None = None
        self._last_frame: float | None = None
        # Monotonic for the rolling window, wall clock for what is reported: a
        # monotonic reading is meaningless as a timestamp to anything reading it.
        self._drops: deque[float] = deque()
        self._last_drop: datetime | None = None

    # -- state read by entities -------------------------------------------------

    @property
    def connected(self) -> bool:
        """Whether an authenticated link is up right now."""
        return self._connected

    @property
    def device_info(self) -> DeviceInfo:
        """Registry entry shared by every entity of this device.

        Keyed on the serial so a Bluetooth device and the same unit seen through
        the cloud account collapse into one Home Assistant device.
        """
        return DeviceInfo(
            identifiers={(DOMAIN, self.serial)},
            connections={(CONNECTION_BLUETOOTH, self.address)},
            manufacturer=MANUFACTURER,
            model=self.model_name,
            name=self.device_name,
            serial_number=self.serial,
        )

    @property
    def link_state(self) -> str:
        """Name of the proxy holding the link, or ``"disconnected"``."""
        if not self._connected:
            return LINK_DISCONNECTED
        return self._holding_scanner() or "connected"

    @property
    def link_attributes(self) -> dict[str, Any]:
        """Diagnostics the household automations read off the link sensor."""
        self._prune_drops()
        last_frame = self._last_frame
        return {
            "hold": self._hold,
            "drops_1h": len(self._drops),
            "last_drop": self._last_drop,
            "reconnect_attempt": self._reconnect_attempt,
            "scanner_source": self._scanner_source,
            "seconds_since_last_frame": (
                None
                if last_frame is None
                else round(time.monotonic() - last_frame, 1)
            ),
        }

    # -- lifecycle --------------------------------------------------------------

    async def async_start(self) -> Exception | None:
        """Start holding the link and wait a bounded time for it to come up.

        Returns the authentication error if the device rejected our credentials,
        in which case the supervisor has already stopped and the caller should
        fail setup with :class:`ConfigEntryAuthFailed`. Any other outcome -
        including the device simply being out of range - returns ``None``: the
        entry loads and the supervisor keeps trying.
        """
        self._hold = True
        self.device.register_callback(self._handle_device_update)
        # Frame age is measured from receipt, not from a value changing: a device
        # repeating an identical frame is still answering, and a diagnostic that
        # said otherwise would report a healthy link as silent.
        self._remove_frame_listener = self.device.on_packet_parsed(self._handle_frame)
        self._supervisor = self.config_entry.async_create_background_task(
            self.hass,
            self._supervise(),
            f"{DOMAIN}-ble-{self.serial}",
            eager_start=True,
        )

        try:
            async with asyncio.timeout(BLE_SETUP_READY_WAIT):
                await self._settled.wait()
        except TimeoutError:
            _LOGGER.debug(
                "%s: no link within %ss of setup; entities start unavailable",
                self.device_name,
                BLE_SETUP_READY_WAIT,
            )
        return self._auth_error

    async def async_stop(self) -> None:
        """Release the link and stop supervising it.

        Cancellation comes first so the supervisor cannot observe the release as
        a drop and reconnect behind the unload.
        """
        self._hold = False
        if (supervisor := self._supervisor) is not None:
            self._supervisor = None
            supervisor.cancel()
            try:
                await supervisor
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - unload must complete regardless
                _LOGGER.exception(
                    "%s: link supervisor failed; releasing anyway", self.device_name
                )

        self.device.remove_callback(self._handle_device_update)
        if self._remove_frame_listener is not None:
            self._remove_frame_listener()
            self._remove_frame_listener = None
        await self._safe_disconnect()
        self._set_connected(False)

    async def _supervise(self) -> None:
        """Connect, hold, reconnect - for as long as the entry is loaded."""
        attempt = 0
        while True:
            if attempt:
                self._reconnect_attempt = attempt
                self.async_update_listeners()
                await asyncio.sleep(_backoff(attempt))

            try:
                await self._connect_once()
            except _CREDENTIAL_ERRORS as err:
                # Retrying cannot fix a rejected user ID, so stop and ask.
                self._auth_error = err
                self._settled.set()
                _LOGGER.error(
                    "%s: authentication rejected by the device: %s",
                    self.device_name,
                    err,
                )
                if self.config_entry.state is ConfigEntryState.LOADED:
                    self.config_entry.async_start_reauth(self.hass)
                return
            except DeviceNotSeen:
                attempt += 1
                _LOGGER.debug(
                    "%s: no advertisement seen yet; retrying", self.device_name
                )
                continue
            except Exception as err:  # noqa: BLE001 - every failure just retries
                attempt += 1
                _LOGGER.log(
                    logging.INFO if attempt < len(BLE_BACKOFF_SECONDS) else logging.DEBUG,
                    "%s: connect attempt %s failed: %s",
                    self.device_name,
                    attempt,
                    err,
                )
                continue

            attempt = 0
            self._reconnect_attempt = 0
            self._set_connected(True)
            self._settled.set()
            _LOGGER.info("%s: connected via %s", self.device_name, self.link_state)

            # Healthy and idle: nothing to do until the device goes away.
            await self.device.wait_disconnected()

            self._record_drop()
            self._set_connected(False)
            _LOGGER.warning("%s: link dropped; reconnecting", self.device_name)
            await self._safe_disconnect()
            attempt = 1

    async def _connect_once(self) -> None:
        """One bounded attempt at an authenticated link."""
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if ble_device is None:
            raise DeviceNotSeen(self.address)

        self.device.update_ble_device(ble_device)
        if service_info := bluetooth.async_last_service_info(
            self.hass, self.address, connectable=True
        ):
            self._scanner_source = service_info.source

        try:
            async with asyncio.timeout(BLE_READY_TIMEOUT):
                await self.device.connect(
                    user_id=self._user_id, max_attempts=BLE_CONNECT_ATTEMPTS
                )
                state = await self.device.wait_until_authenticated_or_error(
                    raise_on_error=True
                )
            if not state.authenticated:
                # A terminal state that is not an error and not authenticated -
                # the device answered and then went away mid-handshake.
                raise LinkNotReady(str(state))
        except BaseException:
            # Leave nothing half-open: the next attempt builds a fresh connection
            # so its own attempt counter and assemblers start clean.
            await self._safe_disconnect()
            raise

    async def _safe_disconnect(self) -> None:
        """Drop the link, bounded, without letting a teardown failure escape."""
        try:
            async with asyncio.timeout(BLE_DISCONNECT_TIMEOUT):
                await self.device.disconnect()
        except Exception:  # noqa: BLE001 - teardown must not break the caller
            _LOGGER.debug(
                "%s: error while disconnecting, continuing",
                self.device_name,
                exc_info=True,
            )

    def configure(self, *, update_period: int) -> None:
        """Apply entry options to the device without disturbing a live link."""
        self._update_period = update_period
        (
            self.device.with_update_period(update_period)
            .with_disabled_reconnect(True)
            .with_connection_options(Connection.Options(timeout=BLE_CONNECT_TIMEOUT))
        )

    # -- commands ---------------------------------------------------------------

    async def async_command(
        self, func: Callable[[], Awaitable[Any]], *, name: str
    ) -> None:
        """Run one device command, serialised against every other command.

        The device is a single BLE link with no request identifiers, so two
        commands in flight can have their replies attributed to each other.
        Nothing is reported back optimistically: the caller's state only changes
        when the device pushes the new value.
        """
        executing = False
        if not self._connected:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="not_connected",
                translation_placeholders={"name": name, "device": self.device_name},
            )

        try:
            # The deadline covers the queue wait too: a backlog of commands must
            # not multiply the time a user waits for the one they just asked for.
            async with asyncio.timeout(BLE_COMMAND_TIMEOUT):
                async with self._command_lock:
                    if not self._connected:
                        raise HomeAssistantError(
                            translation_domain=DOMAIN,
                            translation_key="not_connected",
                            translation_placeholders={
                                "name": name,
                                "device": self.device_name,
                            },
                        )
                    executing = True
                    await func()
        except HomeAssistantError:
            # The caller already framed this for the user (e.g. the device
            # refused the value); wrapping it would bury that message. Nothing
            # is wrong with the link, so it stays up.
            raise
        except TimeoutError as err:
            if executing:
                # The device stopped answering mid-command: the link is wedged,
                # not merely busy, and only a fresh one will recover it.
                await self._async_drop_wedged_link(name)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_timeout",
                translation_placeholders={"name": name},
            ) from err
        except Exception as err:
            # A transport error leaves the link in an unknown state; hand it back
            # to the supervisor rather than keeping a connection we cannot use.
            await self._async_drop_wedged_link(name)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"name": name, "error": str(err)},
            ) from err

    async def _async_drop_wedged_link(self, name: str) -> None:
        """Release a link a command could not use, so the supervisor rebuilds it.

        Entities go unavailable immediately rather than showing values from a
        connection that is no longer answering, and the supervisor's own wait
        ends as the device disconnects, taking it through its normal backoff.
        """
        _LOGGER.warning(
            "%s: %s left the link unusable; releasing it to reconnect",
            self.device_name,
            name,
        )
        self._set_connected(False)
        await self._safe_disconnect()

    # -- internals --------------------------------------------------------------

    async def _async_update_data(self) -> None:
        """Nothing is fetched: values are pushed over the held link."""
        return None

    @callback
    def _handle_frame(self, packet: Any) -> None:
        """Timestamp a packet the device sent, whether or not it changed a value."""
        self._last_frame = time.monotonic()

    @callback
    def _handle_device_update(self) -> None:
        """A device property changed; publish the frame once, not per property."""
        if self._notify_scheduled:
            return
        self._notify_scheduled = True
        self.hass.loop.call_soon(self._publish_frame)

    @callback
    def _publish_frame(self) -> None:
        self._notify_scheduled = False
        self.async_set_updated_data(None)

    @callback
    def _set_connected(self, connected: bool) -> None:
        if self._connected == connected:
            return
        self._connected = connected
        # Availability follows the real link, so a coordinator that has never
        # had a successful frame still reports unavailable rather than empty.
        self.last_update_success = connected
        self.async_update_listeners()

    def _record_drop(self) -> None:
        self._drops.append(time.monotonic())
        self._last_drop = dt_util.utcnow()
        self._prune_drops()

    def _prune_drops(self) -> None:
        cutoff = time.monotonic() - BLE_DROP_WINDOW_SECONDS
        while self._drops and self._drops[0] < cutoff:
            self._drops.popleft()

    def _holding_scanner(self) -> str | None:
        """Name the adapter or proxy currently holding this device's link."""
        address = self.address.upper()
        for scanner in bluetooth.async_current_scanners(self.hass):
            allocations = scanner.get_allocations()
            if allocations is None:
                continue
            if any(held.upper() == address for held in allocations.allocated):
                return f"{scanner.name or scanner.source} ({scanner.source})"
        return None


def _backoff(attempt: int) -> float:
    """Delay before attempt ``attempt`` (1-based), jittered and capped."""
    step = BLE_BACKOFF_SECONDS[min(attempt, len(BLE_BACKOFF_SECONDS)) - 1]
    return step * (1.0 + random.uniform(-BLE_BACKOFF_JITTER, BLE_BACKOFF_JITTER))
