"""Regression tests for the BLE Connection/DeviceBase lifecycle.

Covers findings from the 2026-09-24 BLE audit that live in
`eflib.connection.Connection`, `eflib.devicebase.DeviceBase` and
`eflib.commands.TimeCommands`. None of those modules import Home Assistant,
so - like `test_ble_eflib.py` - only `bleak`, `bleak_retry_connector`,
`ecdsa`, `pycryptodome` and `protobuf` need to be installed; every test here
runs a real event loop via `asyncio.run` rather than needing `pytest-asyncio`.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError

_ROOT = Path(__file__).resolve().parents[1]
if "custom_components" not in sys.modules:
    _cc = ModuleType("custom_components")
    _cc.__path__ = [str(_ROOT / "custom_components")]
    sys.modules["custom_components"] = _cc
    _pkg = ModuleType("custom_components.ecoflow_iot")
    _pkg.__path__ = [str(_ROOT / "custom_components" / "ecoflow_iot")]
    sys.modules["custom_components.ecoflow_iot"] = _pkg
    setattr(_cc, "ecoflow_iot", _pkg)
if "custom_components.ecoflow_iot.ble" not in sys.modules:
    # Only the Home Assistant-free modules of the `ble` package are used here, so
    # its (Home Assistant importing) initialiser is stood in for, as in
    # `test_preferred_proxy.py`.
    _ble = ModuleType("custom_components.ecoflow_iot.ble")
    _ble.__path__ = [str(_ROOT / "custom_components" / "ecoflow_iot" / "ble")]
    sys.modules["custom_components.ecoflow_iot.ble"] = _ble

from custom_components.ecoflow_iot import eflib  # noqa: E402
from custom_components.ecoflow_iot.eflib import commands as commands_module  # noqa: E402
from custom_components.ecoflow_iot.eflib import connection as connection_module  # noqa: E402
from custom_components.ecoflow_iot.eflib.connection import (  # noqa: E402
    Connection,
    ConnectionState,
)
from custom_components.ecoflow_iot.ble.bounded_client import (  # noqa: E402
    make_bounded_client_class,
)
from custom_components.ecoflow_iot.const import BLE_CONNECT_TIMEOUT  # noqa: E402
from custom_components.ecoflow_iot.eflib.encryption import Type7Encryption  # noqa: E402
from custom_components.ecoflow_iot.eflib.exceptions import (  # noqa: E402
    ConnectionTimeout,
    LinkClosed,
    NotConnectedError,
)

ADDRESS = "AA:BB:CC:DD:EE:FF"
ENCRYPT_TYPE_USER_ID = 0b0111000


def _advertisement(serial: str, local_name: str) -> AdvertisementData:
    """EcoFlow 0xB5B5 advertisement, same shape as `test_ble_eflib.py`'s."""
    payload = bytearray(b"\x13")
    payload += serial.encode("ASCII").ljust(16, b"\x00")
    payload += bytes([0x80, 0x01, 0x00, 0x00, 0x00, ENCRYPT_TYPE_USER_ID])
    return AdvertisementData(
        local_name=local_name,
        manufacturer_data={0xB5B5: bytes(payload)},
        service_uuids=[],
        service_data={},
        tx_power=None,
        rssi=-60,
        platform_data=(),
    )


def _ble_device() -> Mock:
    device = Mock()
    device.address = ADDRESS
    device.name = "EF-R31234"
    return device


def _make_device():
    """A real River 3 `DeviceBase`, unconnected (`_conn is None`)."""
    device = eflib.new_device(_ble_device(), _advertisement("R655TEST00001234", "EF-R31234"))
    assert device is not None
    return device


def _make_connection(**overrides) -> Connection:
    async def data_parse(_packet):
        return False

    async def packet_parse(_data):
        return None

    return Connection(
        ble_dev=_ble_device(),
        dev_sn="R655TEST00001234",
        user_id="1234567890",
        data_parse=data_parse,
        packet_parse=packet_parse,
        **overrides,
    )


async def _noop(*_args, **_kwargs) -> None:
    return None


# ------------------------------------------------------------- finding 3 ---


def test_connect_gives_up_within_the_configured_timeout_budget(monkeypatch) -> None:
    """BLE_CONNECT_TIMEOUT must bound the connect step itself.

    Before the fix, `Options.timeout` reached `establish_connection` only as
    a *client-constructor* kwarg; bleak_retry_connector's own retry loop
    calls the real `client.connect()` with its own hardcoded 20s timeout
    regardless (`bleak_retry_connector.BLEAK_TIMEOUT`), so a stuck attempt
    ran for however long the transport let it, not `Options.timeout`. Here
    the fake attempt is made deliberately much longer than the configured
    budget, so a small, fast, deterministic budget is enough to tell the two
    behaviours apart without a multi-second real sleep.
    """

    async def scenario() -> None:
        async def fake_establish_connection(_client_class, _ble_device, _name, **_kw):
            await asyncio.sleep(1.0)
            raise connection_module.BleakError("simulated: proxy never answered")

        monkeypatch.setattr(
            connection_module, "establish_connection", fake_establish_connection
        )
        monkeypatch.setattr(
            connection_module, "close_stale_connections_by_address", _noop
        )

        conn = _make_connection()
        conn.with_options(Connection.Options(timeout=0.15))

        started = time.monotonic()
        await conn.connect(max_attempts=2)
        elapsed = time.monotonic() - started

        assert elapsed < 0.7, (
            "the connect step must give up long before the simulated attempt "
            f"finishes (took {elapsed:.2f}s)"
        )
        assert elapsed >= 0.25, (
            "the full timeout*attempts budget must be granted, not cut short "
            f"to a single timeout (took {elapsed:.2f}s)"
        )
        assert isinstance(conn._last_exception, ConnectionTimeout)

    asyncio.run(scenario())


# ------------------------------------------------------------- finding 4 ---


class _FakeCharacteristic:
    pass


class _FakeServices:
    def get_characteristic(self, _uuid: str) -> _FakeCharacteristic:
        return _FakeCharacteristic()


class _FakeBleakClient:
    def __init__(self) -> None:
        self.is_connected = True
        self.disconnect_calls = 0
        self.services = _FakeServices()

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.is_connected = False

    async def start_notify(self, _characteristic, _callback, **_kwargs) -> None:
        return None


def test_stale_command_disconnect_does_not_orphan_a_still_establishing_link(
    monkeypatch,
) -> None:
    """A disconnect() racing a still-establishing connect must not leave a
    ghost link.

    2026-09-24 live mechanism: a command's retried send outlived the drop
    that started it; by the time it gave up and told the device to
    disconnect, the supervisor had already started building a new
    `Connection`. The stale disconnect() call cleared `device._conn`
    unconditionally but could not touch the connection still establishing,
    which then went on to authenticate - and hold a proxy's connection slot -
    with nothing left pointing at it. Reproduced here with the real
    `DeviceBase` and `Connection` and a gated fake `establish_connection`.
    """

    async def scenario() -> None:
        device = _make_device()
        established = asyncio.Event()
        gate = asyncio.Event()
        fake_client = _FakeBleakClient()

        async def fake_establish_connection(_client_class, _ble_device, _name, **_kw):
            established.set()
            await gate.wait()
            return fake_client

        monkeypatch.setattr(
            connection_module, "establish_connection", fake_establish_connection
        )
        monkeypatch.setattr(
            connection_module, "close_stale_connections_by_address", _noop
        )

        connect_task = asyncio.create_task(device.connect(user_id="1234567890"))
        try:
            await asyncio.wait_for(established.wait(), timeout=1.0)

            conn = device.current_connection
            assert conn is not None

            # A stale command's teardown fires while that connect is still
            # establishing - exactly the race that used to orphan the link.
            await device.disconnect()

            gate.set()
            await asyncio.wait_for(connect_task, timeout=1.0)

            # Either outcome is healthy: the reference survived because it was
            # still current when the disconnect ran, or the connection that
            # came up after the disconnect was requested was torn down. The
            # bug produced neither - `device._conn` was `None` while
            # `conn.is_connected` stayed `True`, a ghost link.
            assert device.current_connection is conn or not conn.is_connected
            assert not conn.is_connected
            assert fake_client.disconnect_calls >= 1
        finally:
            if not connect_task.done():
                connect_task.cancel()

    asyncio.run(scenario())


# ------------------------------------------------------------- finding 6 ---


class _FakeCommandsConnection:
    def __init__(self) -> None:
        self.scheduled: list = []

    def _add_task(self, coro):
        # `async_send_all` only cares that a task was *scheduled*; nothing
        # here needs to run it, and leaving it un-awaited would warn.
        self.scheduled.append(coro)
        coro.close()


class _FakeTimeCommandsDevice:
    def __init__(self) -> None:
        self.address = ADDRESS
        self._conn = _FakeCommandsConnection()
        self._listeners: list = []

    def on_connection_state_change(self, listener):
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def fire(self, state: ConnectionState) -> None:
        for listener in list(self._listeners):
            listener(state)


def test_time_sync_throttle_resets_when_a_new_connection_authenticates(
    monkeypatch,
) -> None:
    """A time request on a fresh link must not be swallowed by the old
    link's throttle.

    2026-09-24 live: 'throttling repeated time-sync request (last sent 19.2s
    ago)' fired on the very link that had just re-authenticated, because
    `TimeCommands._last_sent` survives a reconnect - and `river3.py`
    withholds predictions and config data until this request's reply
    arrives, so the throttled request left a freshly reconnected device
    quietly missing them.
    """
    device = _FakeTimeCommandsDevice()
    time_commands = commands_module.TimeCommands(device)

    clock = [0.0]
    monkeypatch.setattr(commands_module.time, "monotonic", lambda: clock[0])

    time_commands.async_send_all()
    assert len(device._conn.scheduled) == 3

    clock[0] = 5.0  # well inside the 30s resend window
    device.fire(ConnectionState.AUTHENTICATED)

    time_commands.async_send_all()
    assert len(device._conn.scheduled) == 6


# ------------------------------------------------------------- finding 7 ---


def test_disconnect_on_an_already_disconnected_device_does_not_log_an_error(
    caplog,
) -> None:
    """`async_stop` calling `_safe_disconnect` a second time - after a
    cancelled connect has already torn itself down - must not surface as an
    ERROR.

    2026-09-24 live: 20:42:07.622 and .624 both logged 'Device has no
    connection' at ERROR during a normal `release_link` unload, polluting
    the very logs used to triage real drops.
    """
    device = _make_device()
    with caplog.at_level(logging.DEBUG):
        asyncio.run(device.disconnect())  # never connected: the double-disconnect case
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)


# ------------------------------------------------- close() (shutdown latch) ---


def _patch_establish(monkeypatch, calls: list, client=None):
    async def fake_establish_connection(_client_class, _ble_device, _name, **_kw):
        calls.append(1)
        return client or _FakeBleakClient()

    monkeypatch.setattr(
        connection_module, "establish_connection", fake_establish_connection
    )
    monkeypatch.setattr(connection_module, "close_stale_connections_by_address", _noop)


def test_closed_device_refuses_every_later_connect(monkeypatch) -> None:
    """After `close()` nothing - supervisor, retry, stray caller - can reconnect.

    `disconnect()` stays reversible on purpose (the supervisor reconnects after
    it); only `close()` is final, and it is what Home Assistant's shutdown job
    uses so that no connect can slip in while the Bluetooth stack goes away.
    """

    async def scenario() -> None:
        attempts: list = []
        _patch_establish(monkeypatch, attempts)

        device = _make_device()
        await device.close()  # never connected: the latch alone must hold
        with pytest.raises(LinkClosed):
            await device.connect(user_id="1234567890")
        assert attempts == []

        live = _make_device()
        await live.connect(user_id="1234567890")
        assert len(attempts) == 1
        conn = live.current_connection
        await live.close()
        assert live.current_connection is None and not conn.is_connected
        with pytest.raises(LinkClosed):
            await live.connect(user_id="1234567890")
        assert len(attempts) == 1

    asyncio.run(scenario())


def test_close_during_a_connect_still_establishing_drops_that_link(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        established = asyncio.Event()
        gate = asyncio.Event()
        client = _FakeBleakClient()

        async def fake_establish_connection(_client_class, _ble_device, _name, **_kw):
            established.set()
            await gate.wait()
            return client

        monkeypatch.setattr(
            connection_module, "establish_connection", fake_establish_connection
        )
        monkeypatch.setattr(
            connection_module, "close_stale_connections_by_address", _noop
        )

        device = _make_device()
        task = asyncio.create_task(device.connect(user_id="1234567890"))
        await asyncio.wait_for(established.wait(), timeout=1.0)
        await device.close()
        gate.set()
        await asyncio.wait_for(task, timeout=1.0)

        assert client.disconnect_calls >= 1 and not client.is_connected
        assert device.is_closing

    asyncio.run(scenario())


def test_a_closed_connection_never_reconnects_or_writes(monkeypatch) -> None:
    """A late disconnect callback or a send retry must not reopen/touch the link."""

    async def scenario() -> None:
        attempts: list = []
        _patch_establish(monkeypatch, attempts)

        conn = _make_connection()
        conn._reconnect = True
        conn._retry_on_disconnect_delay = 0.01
        conn._reconnect_attempt = 1  # keeps the delay chosen above
        await conn.close()

        # Even if something re-arms the retry flag after the close, the
        # reconnect task bails out instead of connecting.
        conn._retry_on_disconnect = True
        await conn.reconnect()
        assert attempts == []

        writes: list = []
        conn._client = Mock(is_connected=True)
        conn._client.write_gatt_char = lambda *a, **k: writes.append(a)
        with pytest.raises(NotConnectedError):
            await conn.send_request(b"payload", raise_on_failure=True)
        await conn.send_request(b"payload")
        assert writes == []

    asyncio.run(scenario())


def test_close_stops_a_send_retry_that_is_sleeping(monkeypatch) -> None:
    async def scenario() -> None:
        conn = _make_connection()
        attempts = {"n": 0}

        async def failing_send(_data, *, raise_on_failure=False):
            attempts["n"] += 1
            raise OSError("write failed")

        conn._send_request = failing_send
        conn._client = Mock(is_connected=True)
        sender = asyncio.create_task(
            conn.send_request(b"payload", raise_on_failure=True)
        )
        await asyncio.sleep(0.05)  # first attempt failed; now sleeping before retry 2
        assert attempts["n"] == 1
        conn._closing = True
        with pytest.raises(NotConnectedError):
            await asyncio.wait_for(sender, timeout=3.0)
        assert attempts["n"] == 1

    asyncio.run(scenario())


# --------------------------------------- startup contract S4: step bounds ---
#
# No single connect / subscribe / handshake step may hang longer than
# `Connection.Options.timeout`. The stack's own timeouts are far longer than a
# healthy link needs - live 2026-10-02 a River 2 Pro connected through its
# preferred ESPHome proxy and then sat 20 s in the notification subscribe before
# GATT error 133 - and whatever a step does not bound, the whole link waits out
# before its retry even starts. Every stall below is a fake that never answers,
# against a deliberately small `Options.timeout`.

_STEP = 0.2


async def _never(*_args, **_kwargs) -> None:
    await asyncio.Event().wait()


def _as_the_coordinator_runs_it(conn: Connection, *, timeout: float) -> Connection:
    """The options `EcoFlowBleCoordinator.configure` applies to every connection.

    Above all that reconnecting is the supervisor's job, not the connection's.
    """
    return conn.with_disabled_reconnect(True).with_options(
        Connection.Options(timeout=timeout)
    )


def test_a_subscribe_that_hangs_fails_fast_and_releases_the_link(
    monkeypatch, caplog
) -> None:
    async def scenario() -> None:
        client = _FakeBleakClient()
        client.start_notify = _never
        _patch_establish(monkeypatch, [], client)
        conn = _make_connection()
        _as_the_coordinator_runs_it(conn, timeout=_STEP)

        started = time.monotonic()
        with caplog.at_level(logging.WARNING):
            await asyncio.wait_for(conn.connect(max_attempts=2), timeout=3.0)
        elapsed = time.monotonic() - started

        assert _STEP <= elapsed < 1.0
        # Released, not left half-open for the next attempt to trip over.
        assert client.disconnect_calls == 1 and not client.is_connected
        assert conn._state is ConnectionState.DISCONNECTED
        assert conn._auth_task is None, "no handshake on a link that cannot notify"
        assert any(
            record.levelno == logging.WARNING
            and "subscribe" in record.getMessage()
            and f"{_STEP:g}s" in record.getMessage()
            for record in caplog.records
        ), "the stall must say what stalled and for how long"

    asyncio.run(scenario())


@pytest.mark.parametrize("encrypt_type", [0, 1, 7])
def test_a_handshake_write_that_never_returns_ends_the_handshake_within_the_step_budget(
    monkeypatch, encrypt_type
) -> None:
    """The reply reads were always bounded; the writes in front of them were not."""

    async def scenario() -> None:
        client = _FakeBleakClient()
        client.write_gatt_char = _never
        _patch_establish(monkeypatch, [], client)
        conn = _make_connection(encrypt_type=encrypt_type)
        _as_the_coordinator_runs_it(conn, timeout=_STEP)
        await conn.connect(max_attempts=2)

        started = time.monotonic()
        state = await asyncio.wait_for(
            conn.wait_until_authenticated_or_error(), timeout=3.0
        )

        assert state is ConnectionState.ERROR_TIMEOUT
        assert time.monotonic() - started < 1.0
        assert client.disconnect_calls == 1

    asyncio.run(scenario())


def test_the_stage_that_sends_the_credentials_is_bounded_too(monkeypatch) -> None:
    async def scenario() -> None:
        client = _FakeBleakClient()
        client.write_gatt_char = _never
        _patch_establish(monkeypatch, [], client)
        conn = _make_connection()
        _as_the_coordinator_runs_it(conn, timeout=_STEP)
        # Skip the three key-exchange stages (the test above covers a stall in
        # them); what is under test is the write that carries the credentials.
        conn._encryption = Type7Encryption(bytes(16), bytes(16))
        conn._init_ble_session_key = _noop
        await conn.connect(max_attempts=2)

        started = time.monotonic()
        state = await asyncio.wait_for(
            conn.wait_until_authenticated_or_error(), timeout=3.0
        )

        assert state is ConnectionState.ERROR_TIMEOUT
        assert time.monotonic() - started < 1.0
        assert client.disconnect_calls == 1

    asyncio.run(scenario())


def test_a_stale_connection_cleanup_that_hangs_does_not_hold_up_the_connect(
    monkeypatch, caplog
) -> None:
    async def scenario() -> None:
        client = _FakeBleakClient()
        client.write_gatt_char = _never

        async def fake_establish_connection(_client_class, _ble_device, _name, **_kw):
            return client

        monkeypatch.setattr(
            connection_module, "establish_connection", fake_establish_connection
        )
        monkeypatch.setattr(
            connection_module, "close_stale_connections_by_address", _never
        )
        conn = _make_connection()
        _as_the_coordinator_runs_it(conn, timeout=_STEP)

        started = time.monotonic()
        with caplog.at_level(logging.WARNING):
            await asyncio.wait_for(conn.connect(max_attempts=2), timeout=3.0)
        elapsed = time.monotonic() - started

        assert _STEP <= elapsed < 1.0
        assert conn.is_connected, "best-effort housekeeping, so it connects anyway"
        assert any("stale" in record.getMessage() for record in caplog.records)
        await conn.disconnect()

    asyncio.run(scenario())


def test_a_gatt_cache_clear_that_hangs_does_not_hold_up_the_failed_connect(
    monkeypatch, caplog
) -> None:
    """An empty service table makes a connect wipe the cache - one more round trip
    to the proxy, on the very path that exists to unblock the retry."""

    async def scenario() -> None:
        client = _FakeBleakClient()
        client.services = SimpleNamespace(
            get_characteristic=lambda _uuid: None, characteristics={}
        )
        client.clear_cache = _never
        _patch_establish(monkeypatch, [], client)
        conn = _make_connection()
        _as_the_coordinator_runs_it(conn, timeout=_STEP)

        started = time.monotonic()
        with caplog.at_level(logging.WARNING):
            await asyncio.wait_for(conn.connect(max_attempts=2), timeout=3.0)
        elapsed = time.monotonic() - started

        assert _STEP <= elapsed < 1.0
        assert conn._state is ConnectionState.DISCONNECTED
        assert any("GATT cache" in record.getMessage() for record in caplog.records)

    asyncio.run(scenario())


def _stalling_client_class(
    calls: list[int], timeouts: list[float | None] | None = None
) -> type:
    """What `establish_connection` instantiates; its first `connect()` never answers."""

    class FirstConnectStalls(_FakeBleakClient):
        def __init__(self, _device, disconnected_callback=None, **_kwargs) -> None:
            super().__init__()
            self.is_connected = False

        async def connect(self, **kwargs) -> None:
            calls.append(len(calls) + 1)
            if timeouts is not None:
                timeouts.append(kwargs.get("timeout"))
            if len(calls) == 1:
                await asyncio.Event().wait()
            self.is_connected = True

        write_gatt_char = _never

    return FirstConnectStalls


def test_a_stalled_connect_attempt_leaves_room_for_the_retry_that_follows(
    monkeypatch,
) -> None:
    """The real `establish_connection`, a fake client whose first attempt hangs.

    `establish_connection` calls `client.connect()` with its own hardcoded 20 s
    timeout per attempt, so capping only the whole call at `Options.timeout *
    attempts` lets a stalled first attempt eat all of it: the retry - meant to be
    a second path - never starts. The per-attempt bound is what lets it.
    """

    async def scenario() -> None:
        calls: list[int] = []
        timeouts: list[float | None] = []
        failures: list[BaseException] = []
        client_class = make_bounded_client_class(
            _stalling_client_class(calls, timeouts),
            timeout=0.3,
            backend_timeout=0.24,
            on_failure=failures.append,
        )
        monkeypatch.setattr(
            connection_module, "close_stale_connections_by_address", _noop
        )
        conn = _real_connection(client_class)
        _as_the_coordinator_runs_it(conn, timeout=1.0)  # whole call: 2 x 1.0 s

        started = time.monotonic()
        await asyncio.wait_for(conn.connect(max_attempts=2), timeout=5.0)
        elapsed = time.monotonic() - started

        assert calls == [1, 2], "the retry must actually run"
        # `establish_connection` asks for its own 20 s every time; each attempt
        # is handed the shorter backend timeout instead.
        assert timeouts == [0.24, 0.24]
        assert [type(failure) for failure in failures] == [TimeoutError]
        assert conn.is_connected
        assert elapsed < 1.5, "not the 2 s whole-call cap the stalled attempt burned"
        await conn.disconnect()

    asyncio.run(scenario())


def _real_connection(client_class: type) -> Connection:
    """A `Connection` over a real `BLEDevice`, so the real `establish_connection` runs."""

    async def data_parse(_packet):
        return False

    async def packet_parse(_data):
        return None

    return Connection(
        ble_dev=BLEDevice(ADDRESS, "EF-R31234", {}),
        dev_sn="R655TEST00001234",
        user_id="1234567890",
        data_parse=data_parse,
        packet_parse=packet_parse,
        client_class=client_class,
    )


def test_a_cancelled_attempt_is_not_reported_as_a_failed_one() -> None:
    """Being let go of (an unload, the pass's own cap) is not the proxy's fault."""

    class Hangs:
        connect = _never

    async def scenario() -> None:
        failures: list[BaseException] = []
        client = make_bounded_client_class(
            Hangs, timeout=5.0, backend_timeout=4.0, on_failure=failures.append
        )()
        attempt = asyncio.create_task(client.connect())
        await asyncio.sleep(0.05)
        attempt.cancel()
        with pytest.raises(asyncio.CancelledError):
            await attempt
        assert failures == []

    asyncio.run(scenario())


def test_a_failing_report_never_masks_the_connect_error() -> None:
    class Refused:
        async def connect(self, **_kwargs) -> None:
            raise BleakError("no free slot")

    def broken_report(_error: BaseException) -> None:
        raise RuntimeError("bookkeeping bug")

    client = make_bounded_client_class(
        Refused, timeout=1.0, backend_timeout=0.8, on_failure=broken_report
    )()
    with pytest.raises(BleakError, match="no free slot"):
        asyncio.run(client.connect())


# --------------- startup contract S8: the backend's own timeout fires first ---
#
# aioesphomeapi 46.2.0's `bluetooth_gatt_start_notify` (under bleak-esphome 4.0.0)
# registers the notification handler BEFORE the proxy acknowledges and removes it only
# on `except Exception`, never on cancellation. A guard that cancels a stalled
# subscribe from outside therefore leaves a handler registered on the proxy's
# connection, delivering notifications to stale objects until it resets. The fix is to
# hand the backend a `timeout` of its own, shorter than the guard, so it raises and
# cleans up first; the guard stays as the safety net for backends that ignore it.

_GUARD = 1.0  # the step guard used by the behavioural tests below


class _ProxyClient(_FakeBleakClient):
    """How an ESPHome proxy backend treats a notification subscribe it is never answered.

    Mirrors `bluetooth_gatt_start_notify` as bleak-esphome calls it: the handler is
    registered before the proxy acknowledges, the wait is bounded by the ``timeout``
    kwarg (bleak-esphome defaults to 30 s when none is passed), and the handler is
    removed on an Exception - not on a cancellation.
    """

    def __init__(self) -> None:
        super().__init__()
        self.handlers: list = []
        self.timeouts: list[float] = []

    async def start_notify(self, _characteristic, callback, **kwargs) -> None:
        timeout = kwargs.get("timeout", 30.0)
        self.timeouts.append(timeout)
        self.handlers.append(callback)
        try:
            async with asyncio.timeout(timeout):
                await asyncio.Event().wait()  # the proxy never acknowledges
        except Exception:
            self.handlers.remove(callback)
            raise


def test_a_subscribe_never_acknowledged_is_handed_a_backend_timeout_inside_its_guard(
    monkeypatch,
) -> None:
    """With the production guard: the backend gets 4 s, so the subscribe's two proxy
    round trips (subscribe, then the CCCD write) fit inside the 10 s guard."""

    async def scenario() -> None:
        client = _ProxyClient()
        _patch_establish(monkeypatch, [], client)
        conn = _make_connection()
        _as_the_coordinator_runs_it(conn, timeout=BLE_CONNECT_TIMEOUT)
        attempt = asyncio.create_task(conn.connect(max_attempts=2))
        try:
            async with asyncio.timeout(2.0):
                while not client.timeouts:
                    await asyncio.sleep(0.01)
            (backend_timeout,) = client.timeouts
            assert backend_timeout == 4.0
            assert 2 * backend_timeout < BLE_CONNECT_TIMEOUT
        finally:
            attempt.cancel()
            try:
                await attempt
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())


def test_a_stalled_proxy_subscribe_ends_by_the_backends_timeout_leaving_no_handler(
    monkeypatch, caplog
) -> None:
    async def scenario() -> None:
        client = _ProxyClient()
        _patch_establish(monkeypatch, [], client)
        conn = _make_connection()
        _as_the_coordinator_runs_it(conn, timeout=_GUARD)

        started = time.monotonic()
        with caplog.at_level(logging.WARNING):
            await asyncio.wait_for(conn.connect(max_attempts=2), timeout=5.0)
        elapsed = time.monotonic() - started

        assert elapsed < _GUARD * 0.9, "the backend's own timeout must end it, not the guard"
        assert client.handlers == [], "a handler was left registered on the proxy"
        assert client.disconnect_calls == 1 and not client.is_connected
        assert any(
            record.levelno == logging.WARNING and "subscribe" in record.getMessage()
            for record in caplog.records
        )

    asyncio.run(scenario())


def test_each_connect_attempt_is_handed_a_backend_timeout_shorter_than_its_guard() -> None:
    seen: list[dict] = []

    class Backend:
        async def connect(self, **kwargs) -> None:
            seen.append(kwargs)

    client = make_bounded_client_class(
        Backend, timeout=10.0, backend_timeout=8.0, on_failure=lambda _error: None
    )()

    async def scenario() -> None:
        # What `establish_connection` passes: its own 20 s, and a cache hint.
        await client.connect(timeout=20.0, dangerous_use_bleak_cache=True)
        await client.connect()
        await client.connect(timeout=3.0)  # a caller that wants less keeps it

    asyncio.run(scenario())

    assert seen == [
        {"timeout": 8.0, "dangerous_use_bleak_cache": True},
        {"timeout": 8.0},
        {"timeout": 3.0},
    ]

    for bad in (10.0, 12.0, 0.0):
        with pytest.raises(ValueError):
            make_bounded_client_class(
                Backend, timeout=10.0, backend_timeout=bad, on_failure=lambda _e: None
            )


def test_a_stalled_connect_ends_by_the_backends_timeout_so_it_can_clean_up() -> None:
    """Mirrors aioesphomeapi's connect: bounded by ``timeout``, and on that timeout it
    tells the proxy to drop the half-made connection and waits for the slot before
    raising - a path a cancellation does not take."""

    class Backend:
        def __init__(self) -> None:
            self.cleaned_up = False

        async def connect(self, **kwargs) -> None:
            try:
                async with asyncio.timeout(kwargs.get("timeout", 30.0)):
                    await asyncio.Event().wait()
            except Exception:
                self.cleaned_up = True
                raise

    async def scenario() -> None:
        failures: list[BaseException] = []
        client = make_bounded_client_class(
            Backend, timeout=_GUARD, backend_timeout=_GUARD * 0.4, on_failure=failures.append
        )()
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            await client.connect(timeout=20.0)
        assert time.monotonic() - started < _GUARD * 0.9
        assert client.cleaned_up
        assert [type(failure) for failure in failures] == [TimeoutError]

    asyncio.run(scenario())
