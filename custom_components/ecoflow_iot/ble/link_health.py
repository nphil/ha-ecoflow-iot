"""Per-address BLE link health that must outlive a config-entry reload.

Home Assistant-free on purpose, like :mod:`.backoff`: every function here takes
the backing ``dict`` explicitly (the caller passes ``hass.data[STORE_KEY]``)
rather than a `HomeAssistant` instance, so the decisions that decide whether a
bad proxy keeps winning are unit-testable without Home Assistant installed.

Three related but distinct records live here, all keyed by Bluetooth address so
a reload - manual, options-triggered, or the household autoheal's five-minute
sweep - never resets them:

* The reconnect streak and drop history the supervisor's backoff and the
  Connection sensor's ``drops_1h`` read (see ``ble/backoff.py`` and
  ``EcoFlowBleCoordinator.link_attributes``). Modeled on `.unreachable`'s
  ``DOWN_SINCE_KEY``: without this, every reload restarted the backoff ladder
  at its 2s floor, which is most of what the ladder exists to prevent.
* Per-(address, scanner source) short-link penalties that steer
  ``ble_affinity`` away from a proxy that accepts a connection and then loses
  it before ``BLE_STABLE_LINK_SECONDS`` - the failure mode habluetooth's own
  connect-failure counter cannot see, because it clears that counter on every
  *successful* connect regardless of how long the link then lasted.
* Per-(address, scanner source) failed attempts: a connect, subscribe or
  handshake through that scanner that stalled or failed before any
  authenticated link existed. They steer the *next* attempt - the second try
  inside the same connect pass included - to a different scanner, briefly
  (``FAILED_ATTEMPT_AVOID_SECONDS``). habluetooth cannot see these either: it
  clears a scanner's failure count the moment a connect succeeds, so a link
  that connected and then stalled in the notification subscribe looks like a
  success to it, and the preferred-proxy affinity keeps routing straight back.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from ..const import DOMAIN

# A scanner that accepted 2 or more consecutive short-lived connections is
# treated as though it had failed habluetooth's own connect-failure counter,
# which a proxy that authenticates and then drops the link never trips (see
# `ble_affinity.py`'s module docstring).
SHORT_LINK_PENALTY_THRESHOLD = 2
# The penalty is not permanent: whatever made a proxy flaky for a few minutes
# may well have cleared up, and nothing else here ever un-sticks it other than
# a stable link through that same source.
SHORT_LINK_DECAY_SECONDS = 1800.0
# An attempt that stalled or failed through a scanner before any authenticated
# link existed (a connect that never answered, a notification subscribe that
# hung, a handshake stage that timed out) keeps that scanner out of the routing
# for the next attempts for this long. Deliberately short: a stall is as likely a
# busy proxy as a broken one, and the proxy beside the device is where this house
# wants it to live, so it must be eligible again soon. The long penalty above is
# for a scanner that has proved itself bad twice over.
FAILED_ATTEMPT_AVOID_SECONDS = 120.0

# Why a scanner is being skipped, worded to complete "preferred proxy <name> ..."
# in the affinity wrapper's log lines.
SHORT_LINK_REASON = "has dropped this link too soon too often recently"
FAILED_ATTEMPT_REASON = "stalled or failed its last connection attempt for this device"

# hass.data key for the address -> AddressLinkHealth store.
STORE_KEY = f"{DOMAIN}_ble_link_health"


@dataclass
class AddressLinkHealth:
    """One Bluetooth address's reconnect streak, drops and proxy penalties."""

    #: Consecutive links that dropped before BLE_STABLE_LINK_SECONDS; see
    #: `ble/backoff.py`'s `attempt_after_drop` for why this must not reset on
    #: every reload.
    streak: int = 0
    #: Monotonic drop timestamps within the rolling diagnostic window.
    drops: deque[float] = field(default_factory=deque)
    #: Wall-clock time of the most recent drop, for display.
    last_drop: datetime | None = None
    #: scanner source -> (consecutive short-link count, monotonic time of the
    #: most recent one).
    penalties: dict[str, tuple[int, float]] = field(default_factory=dict)
    #: scanner source -> monotonic time of the most recent attempt through it that
    #: stalled or failed before an authenticated link existed.
    failed_attempts: dict[str, float] = field(default_factory=dict)


def get(store: dict[str, AddressLinkHealth], address: str) -> AddressLinkHealth:
    """Return `address`'s record, creating an empty one on first use."""
    record = store.get(address)
    if record is None:
        record = AddressLinkHealth()
        store[address] = record
    return record


def clear(store: dict[str, AddressLinkHealth], address: str) -> None:
    """Forget everything about `address`; the device is being removed."""
    store.pop(address, None)


# -- reconnect streak and drop history (backoff pacing, drops_1h) -----------


def record_drop(
    store: dict[str, AddressLinkHealth],
    address: str,
    *,
    when: float,
    wall_clock: datetime,
) -> None:
    """Record a drop's monotonic and wall-clock time for `address`."""
    record = get(store, address)
    record.drops.append(when)
    record.last_drop = wall_clock


def prune_drops(
    store: dict[str, AddressLinkHealth], address: str, *, cutoff: float
) -> None:
    """Discard drop timestamps older than `cutoff` (a monotonic time)."""
    record = store.get(address)
    if record is None:
        return
    while record.drops and record.drops[0] < cutoff:
        record.drops.popleft()


# -- per-source short-link penalties (proxy avoidance) -----------------------


def record_short_link(
    store: dict[str, AddressLinkHealth], address: str, source: str, *, now: float
) -> None:
    """Count one more short-lived link through `source` for `address`."""
    record = get(store, address)
    count, _ = record.penalties.get(source, (0, 0.0))
    record.penalties[source] = (count + 1, now)


def record_stable_link(
    store: dict[str, AddressLinkHealth], address: str, source: str
) -> None:
    """A link through `source` survived - clear any penalty it was carrying."""
    record = store.get(address)
    if record is None:
        return
    record.penalties.pop(source, None)


# -- per-source failed attempts (next-attempt proxy avoidance) ---------------


def record_failed_attempt(
    store: dict[str, AddressLinkHealth], address: str, source: str, *, now: float
) -> None:
    """An attempt for `address` through `source` stalled or failed."""
    get(store, address).failed_attempts[source] = now


def clear_failed_attempt(
    store: dict[str, AddressLinkHealth], address: str, source: str
) -> None:
    """An authenticated link came up through `source` - it works, stop skipping it."""
    record = store.get(address)
    if record is None:
        return
    record.failed_attempts.pop(source, None)


# -- the routing verdict -----------------------------------------------------


def penalty(
    store: dict[str, AddressLinkHealth], address: str, source: str, *, now: float
) -> str | None:
    """Why routing should skip `source` for `address` right now, or ``None``.

    The wording completes "preferred proxy <name> ..." in `ble_affinity`'s logs.
    """
    record = store.get(address)
    if record is None:
        return None
    count, last = record.penalties.get(source, (0, 0.0))
    if (
        count >= SHORT_LINK_PENALTY_THRESHOLD
        and (now - last) <= SHORT_LINK_DECAY_SECONDS
    ):
        return SHORT_LINK_REASON
    failed_at = record.failed_attempts.get(source)
    if failed_at is not None and (now - failed_at) <= FAILED_ATTEMPT_AVOID_SECONDS:
        return FAILED_ATTEMPT_REASON
    return None


def is_penalised(
    store: dict[str, AddressLinkHealth], address: str, source: str, *, now: float
) -> bool:
    """Whether routing should skip `source` for `address` right now.

    True if it dropped this link too soon, too often, recently - or if its last
    attempt for this device stalled or failed a moment ago.
    """
    return penalty(store, address, source, now=now) is not None


def penalised_sources(
    store: dict[str, AddressLinkHealth], address: str, *, now: float
) -> list[str]:
    """Sources routing is currently skipping for `address`, for a diagnostic attribute."""
    record = store.get(address)
    if record is None:
        return []
    return sorted(
        {
            source
            for source, (count, last) in record.penalties.items()
            if count >= SHORT_LINK_PENALTY_THRESHOLD
            and (now - last) <= SHORT_LINK_DECAY_SECONDS
        }
        | {
            source
            for source, failed_at in record.failed_attempts.items()
            if (now - failed_at) <= FAILED_ATTEMPT_AVOID_SECONDS
        }
    )
