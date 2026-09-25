"""Reconnect pacing for the BLE link supervisor.

Home Assistant-free on purpose, so the one decision that decides whether a bad
link degrades gracefully or turns into a connect storm is unit-testable alone.
"""

from __future__ import annotations

import random

from ..const import BLE_BACKOFF_JITTER, BLE_BACKOFF_SECONDS, BLE_STABLE_LINK_SECONDS


def attempt_after_drop(short_streak: int, lived: float) -> tuple[int, int]:
    """Return ``(short_streak, attempt)`` for the reconnect after a dropped link.

    A link that survived ``BLE_STABLE_LINK_SECONDS`` genuinely worked: reconnect
    promptly (attempt 1) and forget any earlier streak. One that dropped sooner
    is a failed attempt dressed as a success - resetting the backoff on it is how
    a proxy that kills the link seconds after every connect turns into a
    connect storm - so each consecutive short link climbs one backoff step.
    """
    if lived >= BLE_STABLE_LINK_SECONDS:
        return 0, 1
    short_streak += 1
    return short_streak, short_streak + 1


def backoff(attempt: int) -> float:
    """Delay before attempt ``attempt`` (1-based), jittered and capped."""
    step = BLE_BACKOFF_SECONDS[min(attempt, len(BLE_BACKOFF_SECONDS)) - 1]
    return step * (1.0 + random.uniform(-BLE_BACKOFF_JITTER, BLE_BACKOFF_JITTER))
