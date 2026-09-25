"""Preferred-proxy affinity for Home Assistant Bluetooth connections.

Vendored verbatim into each nateshome BLE integration (ac_infinity, bedjet,
fluvalble, ecoflow_iot). Change it here first
(`/data/home/tmp/ble_affinity.py` is the source of truth) and copy to every
integration in the same pass.

Why this exists
---------------
`HaBleakClientWrapper.connect()` (habluetooth `wrappers.py`) ignores the
`BLEDevice` an integration hands it and re-picks the connection path on
every connect: sorted by advertisement RSSI, then
`BaseHaScanner._score_connection_paths` (penalties for connections in
progress, prior failures and a last free slot). An integration therefore
has NO supported way to say "connect through the proxy in this room";
habluetooth #602 asks for one and is still open.

The distance the RSSI sort ignores is what matters here. Every ghost link
seen on this network (2026-09-09 .. 2026-09-17) formed on a marginal link
to a *distant* proxy: the disconnect handshake failed to complete over the
weak path and left the peripheral holding a connection the proxy had
forgotten. A device that is always carried by the proxy sitting next to it
does not get into that state.

How
---
`make_affinity_client_class(base, ...)` returns a subclass of whatever
client class `bleak_retry_connector.establish_connection` would otherwise
use and overrides ONE method, `_async_get_best_available_backend_and_device`.
When the preferred scanner currently advertises the address, can accept a
connection, has not failed this address `max_failures` times in a row, and
is not `is_penalised` (see below), it is chosen; in every other case the
default selection runs, with one exception (also below). All of the
wrapper's bookkeeping (`_add_connecting`, `_track`, slot release,
abort-on-unregister) still runs because `connect()` is untouched.

Two failure modes the caller's own failure counter cannot see:

* habluetooth clears a scanner's connect-failure count on every *successful*
  connect (`BaseHaScanner._finished_connecting`), so a proxy that accepts the
  connection and then loses it seconds later - a marginal RF path, not a
  connect failure - always shows `failures == 0` and keeps winning forever,
  whether it is the preferred proxy or just the strongest RSSI. `is_penalised`
  is an injected predicate over a scanner; the caller is expected to back it
  with its own per-(address, scanner source) short-link history (see
  `ble/link_health.py` in this integration) and clear it on a link that
  actually holds. A penalised scanner is skipped exactly like one whose
  failure count is at `max_failures`, including as the *default* pick: if
  habluetooth's own scorer would have chosen a penalised scanner, the best
  non-penalised connectable alternative is used instead, and default routing
  only runs unmodified when none exists.
* habluetooth's failure counter never decays and is not reset by a reload, so
  a preferred proxy that failed `max_failures` times stays skipped forever
  once the fault that caused it clears up - reloading the entry or reselecting
  the same proxy in options does not help. Once skipped, one half-open trial
  is allowed through it after a cooldown (`half_open_cooldown`, doubling on
  each failed trial up to `half_open_max_cooldown`); a connect that succeeds
  clears habluetooth's own counter and ends the suspension, a failed trial
  restarts the (now longer) cooldown.

Fallback is otherwise bounded by habluetooth's own failure counter, which
resets on the next successful connect through that scanner:
`establish_connection` retries, the first `max_failures` attempts go to the
preferred proxy, then the default path takes over for the rest.

The preferred scanner is named by its ESPHome node name (`scanner.adapter`,
what `bleak_esphome` registers from `device_info.name`), the same identity
the integrations already store in `last_holding_proxy`. Source MAC and the
full `scanner.name` are accepted too.

Private-API note: the overridden method and `_async_get_backend_for_ble_device`
are habluetooth internals (present in 6.26.x). `affinity_supported()` checks
for them; when absent the factory returns `base` unchanged and logs once, so
an upgrade degrades to default routing rather than breaking connections.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
import time
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Preferred scanner gives up after this many consecutive failures for one
# address (habluetooth resets the counter on success). Three is the point at
# which the default scorer would itself have ranked the scanner below a
# healthy alternative.
DEFAULT_MAX_FAILURES = 3

# Once a scanner is skipped (failures or a short-link penalty), how long to
# wait before routing one attempt through it again to see if it has healed.
# Doubles on each failed trial, up to the cap - long enough that a genuinely
# broken proxy is not retried every few minutes, short enough that a proxy
# whose fault cleared up is not skipped for the life of the config entry.
DEFAULT_HALF_OPEN_COOLDOWN = 600.0  # 10 minutes
DEFAULT_HALF_OPEN_MAX_COOLDOWN = 3600.0  # 1 hour

_SELECT = "_async_get_best_available_backend_and_device"
_BACKEND_FOR = "_async_get_backend_for_ble_device"
# HaBleakClientWrapper skips BleakClient.__init__ and keeps the address in a
# name-mangled attribute; there is no public accessor before connect().
_WRAPPER_ADDRESS = "_HaBleakClientWrapper__address"
_warned_unsupported = False


def scanner_matches(scanner: Any, preferred: str) -> bool:
    """Return True if ``scanner`` is the one the operator named."""
    if not preferred:
        return False
    wanted = preferred.strip().lower()
    for attr in ("adapter", "source", "name"):
        value = getattr(scanner, attr, None)
        if isinstance(value, str) and value.strip().lower() == wanted:
            return True
    return False


def affinity_supported(base: type) -> bool:
    """Return True if ``base`` exposes the habluetooth hooks this relies on."""
    return callable(getattr(base, _SELECT, None)) and callable(
        getattr(base, _BACKEND_FOR, None)
    )


def _client_address(client: Any) -> str:
    """Return the target address of a not-yet-connected wrapper client."""
    address = getattr(client, _WRAPPER_ADDRESS, None)
    if isinstance(address, str):
        return address
    return str(getattr(client, "address", ""))


def _is_connectable(scanner: Any) -> bool:
    connector = getattr(scanner, "connector", None)
    return connector is not None and connector.can_connect()


def _best_available(scanner_devices: Any, is_penalised: Callable[[Any], bool]) -> Any:
    """Best-RSSI connectable, non-penalised scanner device, or ``None``."""
    candidates = [
        device
        for device in scanner_devices
        if _is_connectable(device.scanner) and not is_penalised(device.scanner)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda device: device.advertisement.rssi)


def make_affinity_client_class(
    base: type,
    preferred_getter: Callable[[], str | None],
    *,
    max_failures: int = DEFAULT_MAX_FAILURES,
    on_choice: Callable[[str, bool], None] | None = None,
    is_penalised: Callable[[Any], bool] | None = None,
    on_suspension_change: Callable[[str, bool], None] | None = None,
    half_open_cooldown: float = DEFAULT_HALF_OPEN_COOLDOWN,
    half_open_max_cooldown: float = DEFAULT_HALF_OPEN_MAX_COOLDOWN,
    now: Callable[[], float] | None = None,
) -> type:
    """Return ``base`` specialised to prefer one scanner.

    ``preferred_getter`` is called at each connect so an options change
    takes effect on the next reconnect without rebuilding the client.
    ``on_choice(scanner_name, preferred_used)`` is invoked after every
    selection so the caller can surface which path was taken.
    ``is_penalised(scanner)`` reports whether a scanner has recently accepted
    connections it could not hold; it defaults to "never" when omitted.
    ``on_suspension_change(scanner_name, suspended)`` fires once on each
    transition into or out of skipping the preferred scanner (for either
    reason), so the caller can surface it as a diagnostic without polling.
    ``now`` defaults to `time.monotonic` and exists so tests can drive the
    half-open cooldown without a real clock.
    """
    global _warned_unsupported
    if not affinity_supported(base):
        if not _warned_unsupported:
            _LOGGER.warning(
                "Bluetooth client %s has no backend-selection hook; "
                "preferred-proxy affinity is disabled and habluetooth's "
                "default routing applies",
                getattr(base, "__name__", base),
            )
            _warned_unsupported = True
        return base

    default_select = getattr(base, _SELECT)
    is_penalised = is_penalised or (lambda scanner: False)
    now = now or time.monotonic

    # (address, scanner.source) -> (monotonic time of the last trial, seconds
    # to wait before the next one). Entries exist only while a scanner is
    # being skipped; a recovered scanner is dropped from this dict entirely.
    half_open: dict[tuple[str, str], tuple[float, float]] = {}
    # (address, scanner.source) currently believed skipped, purely so
    # suspension/resumption logs at WARNING once per transition instead of
    # once per connect attempt while nothing has changed.
    suspended: set[tuple[str, str]] = set()

    def _suspend(key: tuple[str, str], scanner: Any, reason: str) -> None:
        if key in suspended:
            return
        suspended.add(key)
        _LOGGER.warning(
            "%s: preferred proxy %s %s; falling back to default routing "
            "until it recovers",
            key[0],
            scanner.name,
            reason,
        )
        if on_suspension_change is not None:
            on_suspension_change(scanner.name, True)

    def _resume(key: tuple[str, str], scanner: Any) -> None:
        if key not in suspended:
            return
        suspended.discard(key)
        _LOGGER.warning(
            "%s: preferred proxy %s recovered; ending its suspension",
            key[0],
            scanner.name,
        )
        if on_suspension_change is not None:
            on_suspension_change(scanner.name, False)

    def _half_open_due(key: tuple[str, str], moment: float) -> bool:
        """Whether a single trial through a skipped scanner is due now."""
        state = half_open.get(key)
        if state is None:
            # First time this scanner has been seen skipped: start the
            # cooldown clock now rather than trialling it immediately - the
            # failures that got it here just happened moments ago.
            half_open[key] = (moment, half_open_cooldown)
            return False
        last_trial, cooldown = state
        if moment - last_trial < cooldown:
            return False
        half_open[key] = (moment, min(cooldown * 2, half_open_max_cooldown))
        return True

    def _select(self: Any, manager: Any) -> Any:
        address = _client_address(self)
        scanner_devices = list(manager.async_scanner_devices_by_address(address, True))
        preferred = preferred_getter()

        if preferred:
            for scanner_device in scanner_devices:
                scanner = scanner_device.scanner
                if not scanner_matches(scanner, preferred):
                    continue
                connector = getattr(scanner, "connector", None)
                if connector is None or not connector.can_connect():
                    _LOGGER.debug(
                        "%s: preferred proxy %s has no free connection slot; "
                        "falling back to default routing",
                        address,
                        scanner.name,
                    )
                    break

                key = (address, scanner.source)
                if is_penalised(scanner):
                    _suspend(
                        key,
                        scanner,
                        "has dropped this link too soon too often recently",
                    )
                    break

                failures = scanner.connection_failures(address)
                if failures >= max_failures:
                    if not _half_open_due(key, now()):
                        _suspend(key, scanner, f"has failed {failures} times in a row")
                        break
                    _LOGGER.info(
                        "%s: preferred proxy %s cooldown elapsed; trying it "
                        "once (%d failures so far)",
                        address,
                        scanner.name,
                        failures,
                    )
                else:
                    half_open.pop(key, None)
                    _resume(key, scanner)

                backend = getattr(self, _BACKEND_FOR)(
                    manager, scanner, scanner_device.ble_device
                )
                if backend is None:
                    break
                _LOGGER.info(
                    "%s: connecting via preferred proxy %s (RSSI %s)",
                    address,
                    scanner.name,
                    scanner_device.advertisement.rssi,
                )
                if on_choice is not None:
                    on_choice(scanner.name, True)
                return backend
            else:
                _LOGGER.debug(
                    "%s: preferred proxy %s does not currently see this device; "
                    "falling back to default routing",
                    address,
                    preferred,
                )

        default_backend = default_select(self, manager)
        default_scanner = getattr(default_backend, "scanner", None)
        if default_scanner is not None and is_penalised(default_scanner):
            # habluetooth's own scorer has no memory of short links, so left
            # unmodified it re-picks the same bad proxy every time.
            alt = _best_available(scanner_devices, is_penalised)
            if alt is not None:
                backend = getattr(self, _BACKEND_FOR)(
                    manager, alt.scanner, alt.ble_device
                )
                if backend is not None:
                    _LOGGER.info(
                        "%s: default routing chose %s, which has dropped this "
                        "link too soon too often recently; using %s instead",
                        address,
                        getattr(default_scanner, "name", "?"),
                        alt.scanner.name,
                    )
                    if on_choice is not None:
                        on_choice(alt.scanner.name, False)
                    return backend
        if on_choice is not None:
            on_choice(getattr(default_scanner, "name", "?"), False)
        return default_backend

    return type(
        f"Affinity{base.__name__}",
        (base,),
        {_SELECT: _select, "__module__": __name__},
    )
