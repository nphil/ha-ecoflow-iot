"""Per-attempt bound for the Bluetooth client class a connect is made with.

`bleak_retry_connector.establish_connection` retries a failed connect itself, and
every retry calls the real `client.connect()` with its own hardcoded 20 s timeout
(`bleak_retry_connector.BLEAK_TIMEOUT`) whatever timeout the caller asked for.
Capping the *whole* call - as `eflib.connection.Connection.connect` does - bounds
the total, but a first attempt that stalls then eats all of it and the retry that
was meant to be a second path never starts.

`make_bounded_client_class` closes that gap from the one place every attempt goes
through: it returns the client class with `connect()` capped at ``timeout``
seconds, and it tells the caller about every attempt that stalled or failed *before*
the retry's proxy is chosen, so the retry can be steered to a different one (see
`.link_health` and `ble_affinity`'s ``is_penalised``).

Home Assistant-free on purpose, like :mod:`.backoff` and :mod:`.link_health`, so the
timing is unit-testable against the real `establish_connection` with a fake client.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

_LOGGER = logging.getLogger(__name__)


def make_bounded_client_class(
    base: type,
    *,
    timeout: float,
    on_failure: Callable[[Exception], None],
) -> type:
    """Return ``base`` whose ``connect()`` gives up after ``timeout`` seconds.

    Each call to ``connect()`` is one attempt - `establish_connection` makes one
    per retry. A stall raises `TimeoutError`, which `establish_connection` counts as
    a timed-out attempt and retries; any other failure propagates exactly as it did
    before. Either way ``on_failure(error)`` hears of it first. A cancellation is
    neither: it is somebody else ending the attempt (an unload, the pass's own cap),
    so it passes through unreported - a proxy is not at fault for being let go of.

    ``on_failure`` must not raise, and if it does that is logged and dropped: a
    bookkeeping slip must never turn a connect error into a different one.
    """

    class BoundedClient(base):  # type: ignore[misc, valid-type]
        async def connect(self, *args: Any, **kwargs: Any) -> Any:
            try:
                async with asyncio.timeout(timeout):
                    return await super().connect(*args, **kwargs)
            except Exception as err:  # noqa: BLE001 - reported, then re-raised as is
                try:
                    on_failure(err)
                except Exception:  # noqa: BLE001 - see the docstring
                    _LOGGER.exception("Reporting a failed connect attempt failed")
                raise

    BoundedClient.__name__ = BoundedClient.__qualname__ = f"Bounded{base.__name__}"
    return BoundedClient
