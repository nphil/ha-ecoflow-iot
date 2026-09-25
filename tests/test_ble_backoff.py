"""The reconnect backoff must not collapse on links that die right after connecting.

2026-09-24: a River 3 Pro was dropped ~4.5 s after every authenticated connect.
Each drop reset the backoff to its 1 s floor, so the supervisor ran ~13 full
connect+auth cycles a minute and the load knocked three proxies off Home
Assistant's API repeatedly.
"""

from pathlib import Path
from types import ModuleType
import sys

_ROOT = Path(__file__).resolve().parents[1]
if "custom_components" not in sys.modules:
    components = ModuleType("custom_components")
    components.__path__ = [str(_ROOT / "custom_components")]
    sys.modules["custom_components"] = components
    package = ModuleType("custom_components.ecoflow_iot")
    package.__path__ = [str(_ROOT / "custom_components" / "ecoflow_iot")]
    sys.modules["custom_components.ecoflow_iot"] = package
    setattr(components, "ecoflow_iot", package)
if "custom_components.ecoflow_iot.ble" not in sys.modules:
    ble = ModuleType("custom_components.ecoflow_iot.ble")
    ble.__path__ = [str(_ROOT / "custom_components" / "ecoflow_iot" / "ble")]
    sys.modules["custom_components.ecoflow_iot.ble"] = ble

from custom_components.ecoflow_iot.ble.backoff import attempt_after_drop  # noqa: E402
from custom_components.ecoflow_iot.const import (  # noqa: E402
    BLE_BACKOFF_SECONDS,
    BLE_STABLE_LINK_SECONDS,
)


def _step(attempt: int) -> float:
    return BLE_BACKOFF_SECONDS[min(attempt, len(BLE_BACKOFF_SECONDS)) - 1]


def test_repeated_short_links_climb_to_the_backoff_ceiling() -> None:
    streak, delays = 0, []
    for _ in range(10):
        streak, attempt = attempt_after_drop(streak, lived=4.5)
        delays.append(_step(attempt))

    assert delays[0] > BLE_BACKOFF_SECONDS[0], "a 4.5 s link must not retry at the 1 s floor"
    assert delays == sorted(delays), "each consecutive short link must wait at least as long"
    assert delays[-1] == BLE_BACKOFF_SECONDS[-1]


def test_a_stable_link_drop_reconnects_promptly_and_clears_the_streak() -> None:
    streak, attempt = attempt_after_drop(5, lived=BLE_STABLE_LINK_SECONDS)
    assert (streak, attempt) == (0, 1)

    # The next short drop starts climbing from the bottom again.
    streak, attempt = attempt_after_drop(streak, lived=3.0)
    assert (streak, attempt) == (1, 2)
