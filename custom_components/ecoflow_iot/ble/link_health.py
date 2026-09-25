"""Per-address BLE link health that must outlive a config-entry reload.

Home Assistant-free on purpose, like :mod:`.backoff`: every function here takes
the backing ``dict`` explicitly (the caller passes ``hass.data[STORE_KEY]``)
rather than a `HomeAssistant` instance, so the decisions that decide whether a
bad proxy keeps winning are unit-testable without Home Assistant installed.

Two related but distinct records live here, both keyed by Bluetooth address so
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


def is_penalised(
    store: dict[str, AddressLinkHealth], address: str, source: str, *, now: float
) -> bool:
    """Whether `source` has dropped `address`'s link too soon, too often, recently."""
    record = store.get(address)
    if record is None:
        return False
    count, last = record.penalties.get(source, (0, 0.0))
    return (
        count >= SHORT_LINK_PENALTY_THRESHOLD
        and (now - last) <= SHORT_LINK_DECAY_SECONDS
    )


def penalised_sources(
    store: dict[str, AddressLinkHealth], address: str, *, now: float
) -> list[str]:
    """Sources currently penalised for `address`, for a diagnostic attribute."""
    record = store.get(address)
    if record is None:
        return []
    return sorted(
        source
        for source, (count, last) in record.penalties.items()
        if count >= SHORT_LINK_PENALTY_THRESHOLD and (now - last) <= SHORT_LINK_DECAY_SECONDS
    )
