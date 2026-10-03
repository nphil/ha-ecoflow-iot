"""Startup tests for the Bluetooth entries: no radio work may slow Home Assistant.

Home Assistant reports itself started only once every integration's setup has
returned, so a setup that waits on a radio is on the critical path of every
restart. Measured 2026-10-02: ecoflow_iot took 20.8 s, because a River 2 Pro
connected through its preferred proxy, then sat 20 s in the notification
subscribe before GATT error 133, and setup waited for all of it. What is proven
here, with a real `HomeAssistant`, the real `EcoFlowBleCoordinator` and real
`eflib` devices, and only the radio faked:

* setup returns inside its budget however the device fails to answer, and the
  link keeps coming up in the background;
* the entities - unavailable until then, never fabricated - fill in when the
  link arrives late, and nothing is actuated by that;
* after an attempt stalls or fails through one proxy, the next attempt (the
  retry inside the same connect pass included) goes through a different one.

Run: ``python -m pytest tests/test_ble_startup.py``
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("homeassistant.components.bluetooth")

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import bleak_retry_connector as brc  # noqa: E402
from bleak.backends.device import BLEDevice  # noqa: E402
from bleak.backends.scanner import AdvertisementData  # noqa: E402
from homeassistant.components import bluetooth  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.exceptions import ConfigEntryNotReady  # noqa: E402

from custom_components.ecoflow_iot import ble  # noqa: E402
from custom_components.ecoflow_iot.ble import (  # noqa: E402
    binary_sensor as ble_binary_sensor,
    coordinator as coordinator_module,
    number as ble_number,
    select as ble_select,
    sensor as ble_sensor,
    switch as ble_switch,
    unreachable,
)
from custom_components.ecoflow_iot.const import (  # noqa: E402
    BLE_SETUP_READY_WAIT,
    CONF_ADDRESS,
    CONF_PREFERRED_PROXY,
    CONF_SERIAL,
    CONF_USER_ID,
)
from custom_components.ecoflow_iot.eflib.devicebase import DeviceBase  # noqa: E402
from custom_components.ecoflow_iot.eflib.devices import river3_plus  # noqa: E402
from custom_components.ecoflow_iot.eflib.packet import Packet  # noqa: E402
from custom_components.ecoflow_iot.eflib.pb import pr705_pb2  # noqa: E402

ADDRESS = "AA:BB:CC:DD:EE:FF"
# What the setup path is given: a River 3 UPS advertisement carrying its own serial.
SERIAL = "R655TEST00001234"
SETUP_NAME = "EF-R31234"
# The real River 3 Plus object the entity tests drive.
RIVER_SERIAL = "R635ZTEST0000002"
RIVER_NAME = "EF-R3PR0002"


class FakeEntry:
    """The slice of ConfigEntry the BLE setup and coordinator use."""

    def __init__(self, entry_id: str = "entry1") -> None:
        self.entry_id = entry_id
        self.title = "River 3 Pro"
        self.data = {CONF_ADDRESS: ADDRESS, CONF_SERIAL: SERIAL, CONF_USER_ID: "123"}
        self.options: dict = {}
        self.state = None
        self.unload_callbacks: list = []

    def async_on_unload(self, func):
        self.unload_callbacks.append(func)

    def add_update_listener(self, _listener):
        return lambda: None

    def async_create_background_task(self, _hass, coro, name, eager_start=False):
        return asyncio.create_task(coro, name=name)

    async def run_unload_callbacks(self) -> None:
        while self.unload_callbacks:
            if asyncio.iscoroutine(result := self.unload_callbacks.pop()()):
                await result


@pytest.fixture
def stack(monkeypatch, tmp_path):
    """Quiet the pieces that need a fully started Home Assistant."""
    monkeypatch.setattr(
        unreachable, "async_reconcile", lambda _hass, _entry, *, connected: None
    )
    monkeypatch.setattr(unreachable, "async_cancel", lambda _hass, _entry: None)
    monkeypatch.setattr(bluetooth, "async_current_scanners", lambda _hass: [])
    monkeypatch.setattr(
        bluetooth, "async_ble_device_from_address", lambda *_a, **_k: Mock()
    )
    monkeypatch.setattr(bluetooth, "async_last_service_info", lambda *_a, **_k: None)
    monkeypatch.setattr(coordinator_module, "BLE_SETUP_READY_WAIT", 1.0)
    return SimpleNamespace(tmp_path=tmp_path)


def _make_hass(stack) -> HomeAssistant:
    hass = HomeAssistant(str(stack.tmp_path))
    # Before bootstrap `hass.config_entries` is None; give it the calls used here.
    hass.config_entries = SimpleNamespace(
        async_forward_entry_setups=AsyncMock(),
        async_unload_platforms=AsyncMock(return_value=True),
        async_update_entry=Mock(),
        async_schedule_reload=Mock(),
        async_get_entry=Mock(),
    )
    return hass


def _advertisement(local_name: str = RIVER_NAME) -> AdvertisementData:
    payload = bytes([0x13]) + local_name.encode()[:16].ljust(16, b"\x00") + bytes(4)
    return AdvertisementData(
        local_name=local_name,
        manufacturer_data={46517: payload},
        service_data={},
        service_uuids=[],
        tx_power=None,
        rssi=-60,
        platform_data=(),
    )


def _setup_advertisement() -> AdvertisementData:
    """An EcoFlow advertisement that carries its serial, which is how `eflib` picks a model."""
    payload = bytearray(b"\x13")
    payload += SERIAL.encode("ASCII").ljust(16, b"\x00")
    payload += bytes([0x80, 0x01, 0x00, 0x00, 0x00, 0b0111000])
    return AdvertisementData(
        local_name=SETUP_NAME,
        manufacturer_data={0xB5B5: bytes(payload)},
        service_data={},
        service_uuids=[],
        tx_power=None,
        rssi=-60,
        platform_data=(),
    )


def _river_device() -> river3_plus.Device:
    """A real River 3 Plus object; what a setter would write is captured, not sent."""
    device = river3_plus.Device(
        BLEDevice(ADDRESS, RIVER_NAME, {}), _advertisement(), RIVER_SERIAL
    )
    device.sent = []

    async def _send_config_packet(message) -> None:
        device.sent.append(message)

    device._send_config_packet = _send_config_packet
    return device


def _coordinator(hass, entry, device) -> coordinator_module.EcoFlowBleCoordinator:
    coordinator = coordinator_module.EcoFlowBleCoordinator(
        hass,
        entry,
        device,
        serial=RIVER_SERIAL,
        address=ADDRESS,
        model="River 3 Plus",
        local_name=RIVER_NAME,
        device_name="River",
        user_id="123",
    )
    coordinator.configure(update_period=10)
    return coordinator


async def _setup(module, coordinator) -> dict:
    """Run one platform's setup against `coordinator`; return its entities by key."""
    collected: dict = {}

    def add(entities, update_before_add=False):
        for entity in entities:
            collected[entity._key] = entity

    entry = SimpleNamespace(
        runtime_data=coordinator, async_on_unload=lambda unsubscribe: None
    )
    await module.async_setup_entry(None, entry, add)
    return collected


async def _feed(device, message, src: int) -> None:
    await device.data_parse(Packet(src, 0x20, 0xFE, 0x15, message.SerializeToString()))


async def _until(predicate, timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def _never(*_args, **_kwargs) -> None:
    await asyncio.Event().wait()


# ---------------------------------------------------------------- S1 / S2 ---


def test_setup_budget_is_the_contracts_five_seconds_or_less() -> None:
    assert BLE_SETUP_READY_WAIT <= 5.0


def _advertise(monkeypatch, *, device_in_range: bool) -> None:
    """The device has been heard at least once; whether HA can reach it is `device_in_range`."""
    ble_device = Mock(address=ADDRESS)
    ble_device.name = SETUP_NAME
    monkeypatch.setattr(
        bluetooth,
        "async_last_service_info",
        lambda *_a, **_k: SimpleNamespace(
            device=ble_device, advertisement=_setup_advertisement(), source="proxy-1"
        ),
    )
    monkeypatch.setattr(
        bluetooth,
        "async_ble_device_from_address",
        lambda *_a, **_k: Mock() if device_in_range else None,
    )


@pytest.mark.parametrize("stall", ["connect", "authentication", "out_of_range"])
def test_setup_returns_within_the_budget_when_the_device_never_answers(
    monkeypatch, stack, stall
) -> None:
    """Whatever the radio does - a step that hangs for ever, a handshake that is
    slow, a device nobody can reach - setup returns when its budget is spent, the
    entry loads, and the supervisor is still trying in the background."""

    async def scenario() -> None:
        hass = _make_hass(stack)
        monkeypatch.setattr(coordinator_module, "BLE_SETUP_READY_WAIT", 0.3)
        _advertise(monkeypatch, device_in_range=stall != "out_of_range")
        entered = asyncio.Event()

        async def hang(self, *_args, **_kwargs):
            entered.set()
            await asyncio.Event().wait()

        async def connected(self, **_kwargs) -> None:
            return None

        if stall == "connect":
            monkeypatch.setattr(DeviceBase, "connect", hang)
        elif stall == "authentication":
            monkeypatch.setattr(DeviceBase, "connect", connected)
            monkeypatch.setattr(DeviceBase, "wait_until_authenticated_or_error", hang)

        entry = FakeEntry()
        started = time.monotonic()
        assert await ble.async_setup_entry(hass, entry) is True
        elapsed = time.monotonic() - started

        assert 0.3 <= elapsed < 1.5
        coordinator = entry.runtime_data
        assert not coordinator.connected
        # The entities were created (unavailable), not withheld until the link.
        hass.config_entries.async_forward_entry_setups.assert_awaited_once()
        if stall != "out_of_range":
            await asyncio.wait_for(entered.wait(), timeout=1.0)
        assert not coordinator._supervisor.done(), "the link keeps coming up"

        await entry.run_unload_callbacks()
        await coordinator.async_stop()

    asyncio.run(scenario())


def test_setup_time_already_spent_comes_out_of_the_budget(monkeypatch, stack) -> None:
    """The budget is counted from when setup began, so the executor import of the
    Bluetooth stack that precedes the wait is not added on top of it."""

    async def scenario() -> None:
        hass = _make_hass(stack)
        monkeypatch.setattr(coordinator_module, "BLE_SETUP_READY_WAIT", 0.3)
        _advertise(monkeypatch, device_in_range=True)
        monkeypatch.setattr(DeviceBase, "connect", _never)

        entry = FakeEntry()
        began = asyncio.get_running_loop().time() - 0.25  # 0.25 s already gone
        started = time.monotonic()
        assert await ble.async_setup_entry(hass, entry, started=began)

        assert time.monotonic() - started < 0.2
        await entry.run_unload_callbacks()
        await entry.runtime_data.async_stop()

    asyncio.run(scenario())


def test_a_device_nobody_has_heard_is_refused_at_once_not_after_the_budget(
    monkeypatch, stack
) -> None:
    async def scenario() -> None:
        hass = _make_hass(stack)
        monkeypatch.setattr(coordinator_module, "BLE_SETUP_READY_WAIT", 0.5)
        registered: list = []
        monkeypatch.setattr(
            bluetooth,
            "async_register_callback",
            lambda _hass, callback, *_a: registered.append(callback) or (lambda: None),
        )

        started = time.monotonic()
        with pytest.raises(ConfigEntryNotReady):
            await ble.async_setup_entry(hass, FakeEntry())

        assert time.monotonic() - started < 0.25
        assert len(registered) == 1, "retried on the device's next advertisement"

    asyncio.run(scenario())


# ------------------------------------------------------------- S3 / S6 b, c ---


class LateLink:
    """Makes one real device's link come up only when the test says so."""

    def __init__(self, device) -> None:
        self.arrive = asyncio.Event()
        self.dropped = asyncio.Event()
        self.connects = 0

        async def connect(**_kwargs) -> None:
            self.connects += 1
            await self.arrive.wait()

        async def authenticated(raise_on_error=False):
            return SimpleNamespace(authenticated=True)

        async def wait_disconnected() -> None:
            await self.dropped.wait()

        async def disconnect() -> None:
            return None

        device.connect = connect
        device.wait_until_authenticated_or_error = authenticated
        device.wait_disconnected = wait_disconnected
        device.disconnect = disconnect


def test_entities_fill_in_when_the_link_arrives_after_setup_returned(
    monkeypatch, stack
) -> None:
    async def scenario() -> None:
        hass = _make_hass(stack)
        monkeypatch.setattr(coordinator_module, "BLE_SETUP_READY_WAIT", 0.1)
        device = _river_device()
        link = LateLink(device)
        coordinator = _coordinator(hass, FakeEntry(), device)

        started = time.monotonic()
        assert await coordinator.async_start() is None
        assert time.monotonic() - started < 0.5, "setup waited for a silent device"

        sensors = await _setup(ble_sensor, coordinator)
        assert not coordinator.connected
        assert sensors["battery_level"].available is False
        assert sensors["battery_level"].native_value is None, "nothing is made up"
        assert "usbc2_output_power" not in sensors

        link.arrive.set()  # the proxy finally answers
        await _until(lambda: coordinator.connected)
        assert sensors["battery_level"].available is True
        assert sensors["battery_level"].native_value is None, "linked, but unheard"

        await _feed(
            device,
            pr705_pb2.DisplayPropertyUpload(cms_batt_soc=55, pow_get_typec2=-18.5),
            0x02,
        )
        assert sensors["battery_level"].native_value == 55
        # An entity that only exists once its value has been reported is created
        # then - late, not by holding setup back to learn about it.
        assert sensors["usbc2_output_power"].native_value == 18.5

        await coordinator.async_stop()

    asyncio.run(scenario())


def test_a_link_coming_up_actuates_nothing(monkeypatch, stack) -> None:
    """Coming back from unavailable must not write to the device (house rule 3)."""

    async def scenario() -> None:
        hass = _make_hass(stack)
        monkeypatch.setattr(coordinator_module, "BLE_SETUP_READY_WAIT", 0.1)
        device = _river_device()
        link = LateLink(device)
        coordinator = _coordinator(hass, FakeEntry(), device)
        commands: list[str] = []
        real_command = coordinator.async_command

        async def spy(func, *, name: str) -> None:
            commands.append(name)
            await real_command(func, name=name)

        coordinator.async_command = spy

        assert await coordinator.async_start() is None
        platforms = {
            module.__name__.rpartition(".")[2]: await _setup(module, coordinator)
            for module in (
                ble_sensor,
                ble_binary_sensor,
                ble_switch,
                ble_number,
                ble_select,
            )
        }
        assert platforms["switch"]["ac_ports"].available is False

        link.arrive.set()
        await _until(lambda: coordinator.connected)
        await _feed(
            device,
            pr705_pb2.DisplayPropertyUpload(
                cms_batt_soc=55, flow_info_ac_out=2, led_mode=1, cms_max_chg_soc=100
            ),
            0x02,
        )

        # The controls are live and readable again ...
        assert platforms["switch"]["ac_ports"].available is True
        # ... and nothing - no restored setpoint, no re-applied mode - was sent.
        assert commands == []
        assert device.sent == []

        await coordinator.async_stop()

    asyncio.run(scenario())


# ------------------------------------------------------------------- S4 ---
#
# After an attempt stalls or fails through one proxy, the next attempt - the
# retry inside the same connect pass included - must go through a different one.
# The real affinity wrapper and the real `establish_connection` do the routing;
# the fakes are the two proxies and habluetooth's client wrapper they route.


class _Scanner:
    """The slice of an ESPHome proxy's scanner that routing and the coordinator read."""

    def __init__(self, adapter: str, rssi: int) -> None:
        self.adapter = adapter
        self.source = f"{adapter}-source"
        self.name = f"{adapter} ({self.source})"
        self.rssi = rssi
        self.connector = SimpleNamespace(can_connect=lambda: True)
        self.holding = False

    def connection_failures(self, _address: str) -> int:
        return 0

    def get_allocations(self):
        return SimpleNamespace(allocated=[ADDRESS] if self.holding else [])


class _Rig:
    """Proxies, a stand-in for habluetooth's client wrapper, and what each connect does."""

    def __init__(self, monkeypatch, scanners: list[_Scanner], connect_script) -> None:
        self.scanners = scanners
        self.chosen: list[_Scanner] = []
        self.attempts = 0
        rig = self

        class FakeWrapper:
            def __init__(self, _device, disconnected_callback=None, **_kwargs) -> None:
                self._HaBleakClientWrapper__address = ADDRESS

            def _async_get_best_available_backend_and_device(self, manager):
                best = max(
                    manager.async_scanner_devices_by_address(ADDRESS, True),
                    key=lambda device: device.advertisement.rssi,
                )
                return SimpleNamespace(scanner=best.scanner, ble_device=best.ble_device)

            def _async_get_backend_for_ble_device(self, manager, scanner, ble_device):
                return SimpleNamespace(scanner=scanner, ble_device=ble_device)

            async def connect(self, **_kwargs) -> None:
                backend = self._async_get_best_available_backend_and_device(
                    rig.manager
                )
                rig.attempts += 1
                rig.chosen.append(backend.scanner)
                await connect_script(rig.attempts)
                for scanner in rig.scanners:
                    scanner.holding = scanner is backend.scanner

        self.manager = SimpleNamespace(
            async_scanner_devices_by_address=lambda _address, _connectable: [
                SimpleNamespace(
                    scanner=scanner,
                    ble_device=SimpleNamespace(address=ADDRESS),
                    advertisement=SimpleNamespace(rssi=scanner.rssi),
                )
                for scanner in scanners
            ]
        )
        monkeypatch.setattr(brc, "BleakClient", FakeWrapper)
        monkeypatch.setattr(bluetooth, "async_current_scanners", lambda _hass: scanners)

    @property
    def routes(self) -> list[str]:
        return [scanner.adapter for scanner in self.chosen]


class _LinkDevice:
    """Stands in for `DeviceBase` so the coordinator's own client class does the routing."""

    current_connection = None

    def __init__(self, *, authenticated: list[bool], establish: bool = False) -> None:
        self._authenticated = iter(authenticated)
        self._establish = establish
        self._dropped = asyncio.Event()
        self.connect_calls = 0

    def on_packet_parsed(self, _listener):
        return lambda: None

    def register_callback(self, _callback) -> None:
        return None

    def remove_callback(self, _callback) -> None:
        return None

    def update_ble_device(self, _device) -> None:
        return None

    async def connect(self, *, user_id, max_attempts, client_class) -> None:
        self.connect_calls += 1
        self._dropped.clear()
        ble_device = BLEDevice(ADDRESS, "river", {})
        if self._establish:  # the real retry loop makes the attempts
            await brc.establish_connection(
                client_class, ble_device, "river", max_attempts=max_attempts
            )
        else:  # one attempt per pass
            await client_class(ble_device).connect(timeout=20.0)

    async def wait_until_authenticated_or_error(self, raise_on_error=False):
        return SimpleNamespace(authenticated=next(self._authenticated))

    async def wait_disconnected(self) -> None:
        await self._dropped.wait()

    async def disconnect(self) -> None:
        self._dropped.set()


async def _instantly(_attempt: int) -> None:
    return None


def _routed_coordinator(stack, device: _LinkDevice, *, preferred: str | None):
    hass = _make_hass(stack)
    entry = FakeEntry()
    entry.options = {CONF_PREFERRED_PROXY: preferred} if preferred else {}
    return coordinator_module.EcoFlowBleCoordinator(
        hass,
        entry,
        device,
        serial=RIVER_SERIAL,
        address=ADDRESS,
        model="River 3 Plus",
        local_name=RIVER_NAME,
        device_name="River",
        user_id="123",
    )


def test_after_a_stall_through_the_preferred_proxy_the_next_pass_uses_another(
    monkeypatch, stack, caplog
) -> None:
    """Live 2026-10-02: connected through the preferred proxy, then stalled."""

    async def scenario() -> None:
        near, far = _Scanner("near-proxy", -45), _Scanner("far-proxy", -70)
        rig = _Rig(monkeypatch, [near, far], _instantly)
        monkeypatch.setattr(coordinator_module, "_backoff", lambda _attempt: 0.01)
        # The first link connects and then never authenticates (a stalled
        # subscribe ends up here); the second one does.
        device = _LinkDevice(authenticated=[False, True])
        coordinator = _routed_coordinator(stack, device, preferred="near-proxy")

        with caplog.at_level(logging.INFO):
            assert await coordinator.async_start() is None
        assert coordinator.connected

        assert rig.routes == ["near-proxy", "far-proxy"]
        attributes = coordinator.link_attributes
        assert attributes["avoided_proxies"] == [near.source]
        assert attributes["preferred_proxy_suspended"] is True
        assert coordinator.link_state == far.name
        assert any(
            "near-proxy" in record.getMessage()
            and "stalled or failed its last connection attempt" in record.getMessage()
            for record in caplog.records
        ), "the log must say why the preferred proxy was skipped"

        await coordinator.async_stop()

    asyncio.run(scenario())


def test_a_connect_that_stalls_through_the_preferred_proxy_is_retried_through_another(
    monkeypatch, stack
) -> None:
    """The retry inside one connect pass is a genuine second path, not the same proxy."""

    async def scenario() -> None:
        async def first_attempt_never_answers(attempt: int) -> None:
            if attempt == 1:
                await asyncio.Event().wait()

        near, far = _Scanner("near-proxy", -45), _Scanner("far-proxy", -70)
        rig = _Rig(monkeypatch, [near, far], first_attempt_never_answers)
        monkeypatch.setattr(coordinator_module, "BLE_CONNECT_TIMEOUT", 0.2)
        monkeypatch.setattr(coordinator_module, "BLE_SETUP_READY_WAIT", 3.0)
        device = _LinkDevice(authenticated=[True], establish=True)
        coordinator = _routed_coordinator(stack, device, preferred="near-proxy")

        assert await coordinator.async_start() is None
        assert coordinator.connected

        assert rig.routes == ["near-proxy", "far-proxy"]
        assert device.connect_calls == 1, "the retry happened inside the one pass"
        assert coordinator.link_state == far.name

        await coordinator.async_stop()

    asyncio.run(scenario())


def test_a_lone_proxy_is_still_used_after_it_stalled_and_forgiven_once_it_works(
    monkeypatch, stack
) -> None:
    """Steering away must never strand a device that has only one way out."""

    async def scenario() -> None:
        only = _Scanner("only-proxy", -50)
        rig = _Rig(monkeypatch, [only], _instantly)
        monkeypatch.setattr(coordinator_module, "_backoff", lambda _attempt: 0.01)
        device = _LinkDevice(authenticated=[False, True])
        coordinator = _routed_coordinator(stack, device, preferred=None)

        assert await coordinator.async_start() is None
        assert coordinator.connected

        assert rig.routes == ["only-proxy", "only-proxy"]
        # A link that came up through it proves it works: not skipped any more.
        assert coordinator.link_attributes["avoided_proxies"] == []

        await coordinator.async_stop()

    asyncio.run(scenario())
