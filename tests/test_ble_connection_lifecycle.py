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
from types import ModuleType
from unittest.mock import Mock

from bleak.backends.scanner import AdvertisementData

_ROOT = Path(__file__).resolve().parents[1]
if "custom_components" not in sys.modules:
    _cc = ModuleType("custom_components")
    _cc.__path__ = [str(_ROOT / "custom_components")]
    sys.modules["custom_components"] = _cc
    _pkg = ModuleType("custom_components.ecoflow_iot")
    _pkg.__path__ = [str(_ROOT / "custom_components" / "ecoflow_iot")]
    sys.modules["custom_components.ecoflow_iot"] = _pkg
    setattr(_cc, "ecoflow_iot", _pkg)

from custom_components.ecoflow_iot import eflib  # noqa: E402
from custom_components.ecoflow_iot.eflib import commands as commands_module  # noqa: E402
from custom_components.ecoflow_iot.eflib import connection as connection_module  # noqa: E402
from custom_components.ecoflow_iot.eflib.connection import (  # noqa: E402
    Connection,
    ConnectionState,
)
from custom_components.ecoflow_iot.eflib.exceptions import ConnectionTimeout  # noqa: E402

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
