"""Tests for releasing BLE links through Home Assistant's shutdown jobs.

Home Assistant runs shutdown jobs (stage 1 of `async_stop`) before it stops the
Bluetooth stack, so that is where a held link has to be dropped. These tests
use a real `HomeAssistant` object, the real `EcoFlowBleCoordinator` and the
real shutdown-job registry; only the Bluetooth device itself is faked (the
eflib-level latch is covered in `test_ble_connection_lifecycle.py`).

Run: ``python -m pytest tests/test_ble_shutdown.py``
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

from bleak.backends.scanner import AdvertisementData  # noqa: E402
from homeassistant.components import bluetooth  # noqa: E402
from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.core import HassJobType, HomeAssistant  # noqa: E402
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError  # noqa: E402

import custom_components.ecoflow_iot as integration  # noqa: E402
from custom_components.ecoflow_iot import ble  # noqa: E402
from custom_components.ecoflow_iot.ble import (  # noqa: E402
    coordinator as coordinator_module,
    link_health,
    unreachable,
)
from custom_components.ecoflow_iot.const import (  # noqa: E402
    CONF_ADDRESS,
    CONF_SERIAL,
    CONF_USER_ID,
    DOMAIN,
    SHUTDOWN_LATCH_KEY,
)
from custom_components.ecoflow_iot.eflib.exceptions import LinkClosed  # noqa: E402

ADDRESS = "AA:BB:CC:DD:EE:FF"
SERIAL = "R655TEST00001234"


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


class FakeDevice:
    """A link that comes up on connect and drops when released."""

    def __init__(self, *, close_behaviour: str = "ok") -> None:
        self.close_behaviour = close_behaviour
        self.connects = 0
        self.closed = False
        self.disconnects = 0
        self.current_connection = None
        self._dropped = asyncio.Event()
        self.order: list[str] = []

    def on_packet_parsed(self, _listener):
        return lambda: None

    def register_callback(self, _callback) -> None:
        return None

    def remove_callback(self, _callback) -> None:
        return None

    def update_ble_device(self, _device) -> None:
        return None

    async def connect(self, **_kwargs) -> None:
        if self.closed:
            raise LinkClosed("closed")
        self.connects += 1
        self._dropped.clear()

    async def wait_until_authenticated_or_error(self, raise_on_error=False):
        return SimpleNamespace(authenticated=True)

    async def wait_disconnected(self) -> None:
        await self._dropped.wait()

    async def disconnect(self) -> None:
        self.disconnects += 1
        self._dropped.set()

    async def close(self) -> None:
        self.order.append("close")
        self.closed = True
        if self.close_behaviour == "hang":
            await asyncio.Event().wait()
        if self.close_behaviour == "raise":
            raise RuntimeError("proxy went away")
        self._dropped.set()


@pytest.fixture
def stack(monkeypatch, tmp_path):
    """Quiet the pieces that need a fully started Home Assistant."""
    calls: list[tuple] = []
    monkeypatch.setattr(
        unreachable,
        "async_reconcile",
        lambda _hass, _entry, *, connected: calls.append(("reconcile", connected)),
    )
    monkeypatch.setattr(
        unreachable, "async_cancel", lambda _hass, _entry: calls.append(("cancel",))
    )
    monkeypatch.setattr(bluetooth, "async_current_scanners", lambda _hass: [])
    monkeypatch.setattr(
        bluetooth, "async_ble_device_from_address", lambda *_a, **_k: Mock()
    )
    monkeypatch.setattr(bluetooth, "async_last_service_info", lambda *_a, **_k: None)
    monkeypatch.setattr(coordinator_module, "BLE_SETUP_READY_WAIT", 1.0)
    return SimpleNamespace(calls=calls, tmp_path=tmp_path)


def _new_coordinator(hass, entry, device) -> coordinator_module.EcoFlowBleCoordinator:
    return coordinator_module.EcoFlowBleCoordinator(
        hass,
        entry,
        device,
        serial=SERIAL,
        address=ADDRESS,
        model="River 3 Pro",
        local_name="EF-R31234",
        device_name="River 3 Pro",
        user_id="123",
    )


def _make_hass(stack) -> HomeAssistant:
    hass = HomeAssistant(str(stack.tmp_path))
    # Before bootstrap `hass.config_entries` is None; give it the calls used here.
    hass.config_entries = SimpleNamespace(
        async_forward_entry_setups=AsyncMock(),
        async_unload_platforms=AsyncMock(return_value=True),
        async_unload=AsyncMock(),
        async_schedule_reload=Mock(),
        async_get_entry=Mock(),
    )
    return hass


def _registered_jobs(hass: HomeAssistant) -> list:
    return list(hass._shutdown_jobs)


async def _connected(hass, stack, device=None):
    """A coordinator holding a (fake) authenticated link, plus its job."""
    entry = FakeEntry()
    device = device or FakeDevice()
    coordinator = _new_coordinator(hass, entry, device)
    assert await coordinator.async_start() is None
    assert coordinator.connected
    stack.calls.clear()
    return entry, device, coordinator


def test_setup_registers_one_removable_job_per_entry(monkeypatch, stack) -> None:
    async def scenario() -> None:
        hass = _make_hass(stack)
        adv = AdvertisementData(
            local_name="EF-R31234",
            manufacturer_data={0xB5B5: _advert_payload()},
            service_uuids=[],
            service_data={},
            tx_power=None,
            rssi=-60,
            platform_data=(),
        )
        ble_device = Mock(address=ADDRESS, name="EF-R31234")
        ble_device.name = "EF-R31234"
        monkeypatch.setattr(
            bluetooth,
            "async_last_service_info",
            lambda *_a, **_k: SimpleNamespace(device=ble_device, advertisement=adv),
        )
        monkeypatch.setattr(
            bluetooth, "async_ble_device_from_address", lambda *_a, **_k: None
        )
        monkeypatch.setattr(coordinator_module, "BLE_SETUP_READY_WAIT", 0.05)

        entries = [FakeEntry("a"), FakeEntry("b")]
        for entry in entries:
            assert await ble.async_setup_entry(hass, entry)

        jobs = _registered_jobs(hass)
        assert len(jobs) == 2, "one shutdown job per BLE entry"
        for job in jobs:
            # A plain function would be run in the executor and never awaited.
            assert job.job.job_type is HassJobType.Coroutinefunction
        assert {job.job.name for job in jobs} == {
            f"{DOMAIN} release BLE link River 3 Pro"
        }

        for entry in entries:
            await entry.run_unload_callbacks()
            await entry.runtime_data.async_stop()
        assert _registered_jobs(hass) == [], "unloading an entry removes its job"

    asyncio.run(scenario())


def _advert_payload() -> bytes:
    payload = bytearray(b"\x13")
    payload += SERIAL.encode("ASCII").ljust(16, b"\x00")
    payload += bytes([0x80, 0x01, 0x00, 0x00, 0x00, 0b0111000])
    return bytes(payload)


def test_job_releases_the_link_and_latches_without_unloading(stack) -> None:
    async def scenario() -> None:
        hass = _make_hass(stack)
        entry, device, coordinator = await _connected(hass, stack)
        assert device.connects == 1
        supervisor = coordinator._supervisor
        link_health_store = hass.data.setdefault(link_health.STORE_KEY, {})

        await ble._async_release_at_shutdown(hass, entry, coordinator)

        assert device.closed and coordinator.closing
        assert supervisor.done(), "the supervisor must not outlive the release"
        # Quiet: the watchers were stopped, nothing was reconciled as an outage
        # and the deliberate disconnect was not recorded as a drop.
        assert ("cancel",) in stack.calls
        assert ("reconcile", False) not in stack.calls
        record = link_health.get(link_health_store, ADDRESS)
        assert not record.drops and not record.streak
        # Release only: no unload, and entities keep their values (no wave of
        # `unavailable` states).
        hass.config_entries.async_unload.assert_not_called()
        assert coordinator.connected

        # Latched for good: nothing can open a link again in this process.
        with pytest.raises(LinkClosed):
            await coordinator._connect_once()
        await asyncio.sleep(0.05)
        assert device.connects == 1
        with pytest.raises(HomeAssistantError):
            await coordinator.async_command(AsyncMock(), name="turn on")
        assert hass.data[SHUTDOWN_LATCH_KEY] is True

    asyncio.run(scenario())


def test_latch_precedes_the_disconnect_and_blocks_the_supervisor(stack) -> None:
    """The window between 'disconnecting' and 'done' must not allow a reconnect."""

    async def scenario() -> None:
        hass = _make_hass(stack)
        entry, device, coordinator = await _connected(hass, stack)
        seen: dict[str, bool] = {}
        original_close = device.close

        async def close_and_check() -> None:
            seen["latched_before_disconnect"] = coordinator.closing
            seen["watchers_quiet_before_disconnect"] = ("cancel",) in stack.calls
            await original_close()

        device.close = close_and_check
        await ble._async_release_at_shutdown(hass, entry, coordinator)
        assert seen == {
            "latched_before_disconnect": True,
            "watchers_quiet_before_disconnect": True,
        }

    asyncio.run(scenario())


def test_supervisor_does_not_reconnect_after_a_drop_once_latched(stack) -> None:
    async def scenario() -> None:
        hass = _make_hass(stack)
        entry, device, coordinator = await _connected(hass, stack)
        # Latch only (supervisor still running), then let the link drop: the
        # supervisor would normally back off and reconnect.
        coordinator.close_for_shutdown()
        device._dropped.set()
        await asyncio.wait_for(asyncio.shield(coordinator._supervisor), timeout=5)
        assert device.connects == 1

    asyncio.run(scenario())


def test_hanging_disconnect_is_bounded_and_does_not_raise(
    monkeypatch, stack, caplog
) -> None:
    async def scenario() -> None:
        hass = _make_hass(stack)
        entry, device, coordinator = await _connected(
            hass, stack, FakeDevice(close_behaviour="hang")
        )
        monkeypatch.setattr(ble, "BLE_SHUTDOWN_TIMEOUT", 0.2)

        started = time.monotonic()
        with caplog.at_level(logging.INFO):
            await ble._async_release_at_shutdown(hass, entry, coordinator)
        assert time.monotonic() - started < 2
        assert coordinator.closing
        assert any(
            r.levelno == logging.WARNING and "did not finish" in r.getMessage()
            for r in caplog.records
        )

    asyncio.run(scenario())


def test_failing_disconnect_is_swallowed_with_one_warning(stack, caplog) -> None:
    async def scenario() -> None:
        hass = _make_hass(stack)
        entry, device, coordinator = await _connected(
            hass, stack, FakeDevice(close_behaviour="raise")
        )
        with caplog.at_level(logging.INFO):
            await ble._async_release_at_shutdown(hass, entry, coordinator)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1 and "proxy went away" in warnings[0].getMessage()

    asyncio.run(scenario())


def test_success_logs_one_info_line(stack, caplog) -> None:
    async def scenario() -> None:
        hass = _make_hass(stack)
        entry, _device, coordinator = await _connected(hass, stack)
        with caplog.at_level(logging.INFO):
            await ble._async_release_at_shutdown(hass, entry, coordinator)
        infos = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.INFO and "at shutdown" in r.getMessage()
        ]
        assert len(infos) == 1
        assert infos[0].startswith("Released BLE link to River 3 Pro at shutdown in ")

    asyncio.run(scenario())


def test_entry_without_a_link_yet_cannot_be_set_up_after_shutdown(
    monkeypatch, stack
) -> None:
    """An entry still waiting for its device must not connect after the jobs ran."""

    async def scenario() -> None:
        hass = _make_hass(stack)
        entry, _device, coordinator = await _connected(hass, stack)
        await ble._async_release_at_shutdown(hass, entry, coordinator)

        with pytest.raises(ConfigEntryNotReady):
            await ble.async_setup_entry(hass, FakeEntry("late"))

        # And a late advertisement does not schedule a reload of a waiting entry.
        waiting = FakeEntry("waiting")
        captured = {}
        monkeypatch.setattr(
            bluetooth,
            "async_register_callback",
            lambda _hass, callback, *_a: captured.setdefault("cb", callback)
            and (lambda: None),
        )
        ble._register_reappear_callback(hass, waiting, ADDRESS)
        captured["cb"](Mock(), Mock())
        hass.config_entries.async_schedule_reload.assert_not_called()

    asyncio.run(scenario())


def test_release_link_action_still_unloads_ble_entries_only(monkeypatch) -> None:
    """`ecoflow_iot.release_link` keeps its semantics (and its imports resolve).

    A NameError in this path once aborted a whole restart, so it is exercised
    for real rather than assumed.
    """

    async def scenario() -> None:

        ble_entry = SimpleNamespace(
            entry_id="ble",
            title="River 3 Pro",
            state=ConfigEntryState.LOADED,
            data={"transport": "ble"},
            unique_id="ble",
        )
        cloud_entry = SimpleNamespace(
            entry_id="cloud",
            title="Cloud",
            state=ConfigEntryState.LOADED,
            data={},
            unique_id="cloud",
        )
        unloaded: list[str] = []

        async def async_unload(entry_id: str) -> bool:
            unloaded.append(entry_id)
            return True

        hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_entries=lambda _domain: [ble_entry, cloud_entry],
                async_unload=async_unload,
            )
        )
        monkeypatch.setattr(integration, "is_ble_entry", lambda e: e is ble_entry)
        await integration._async_release_links(hass, 0)
        assert unloaded == ["ble"]

    asyncio.run(scenario())


# ---------------------------------------------------------------- addendum ---


def _run_shutdown_jobs_like_home_assistant(hass: HomeAssistant) -> list:
    """Stage 1 of `HomeAssistant.async_stop`: the list is enumerated once, here."""
    tasks = []
    for job in list(hass._shutdown_jobs):
        task_or_none = hass.async_run_hass_job(job.job, *job.args)
        if task_or_none:
            tasks.append(task_or_none)
    return tasks


def _loaded_ble_entry(entry_id: str):
    return SimpleNamespace(
        entry_id=entry_id,
        title="River 3 Pro",
        state=ConfigEntryState.LOADED,
        data={},
        unique_id=entry_id,
    )


def _release_link_hass(stack, entries):
    hass = _make_hass(stack)
    setups: list[str] = []

    async def async_unload(entry_id: str) -> bool:
        for entry in entries:
            if entry.entry_id == entry_id:
                entry.state = ConfigEntryState.NOT_LOADED
        return True

    async def async_setup(entry_id: str) -> bool:
        setups.append(entry_id)
        return True

    hass.config_entries = SimpleNamespace(
        async_entries=lambda _domain: entries,
        async_unload=async_unload,
        async_setup=async_setup,
    )
    return hass, setups


def test_release_link_resume_fires_when_nothing_shuts_down(monkeypatch, stack) -> None:
    """Control for the next test: the resume timer does bring the entries back."""

    async def scenario() -> None:
        entries = [_loaded_ble_entry("a"), _loaded_ble_entry("b")]
        monkeypatch.setattr(integration, "is_ble_entry", lambda _e: True)
        hass, setups = _release_link_hass(stack, entries)
        await integration.async_setup(hass, {})
        await integration._async_release_links(hass, 0.1)
        await asyncio.sleep(0.4)
        assert setups == ["a", "b"]

    asyncio.run(scenario())


def test_latch_job_outlives_release_link_and_cancels_pending_resumes(
    monkeypatch, stack
) -> None:
    """`release_link(resume_after=N)` then a shutdown must not reconnect anything.

    `release_link` unloads both entries and so removes their own shutdown jobs;
    the domain-lifetime job is what still sets the flag and cancels the timer.
    """

    async def scenario() -> None:
        entries = [_loaded_ble_entry("a"), _loaded_ble_entry("b")]
        monkeypatch.setattr(integration, "is_ble_entry", lambda _e: True)
        hass, setups = _release_link_hass(stack, entries)
        assert await integration.async_setup(hass, {})
        assert len(_registered_jobs(hass)) == 1, "one domain job, no entry jobs"

        await integration._async_release_links(hass, 0.2)
        assert not hass.data.get(SHUTDOWN_LATCH_KEY)

        # The latch job is a callback: it has finished by the time the loop
        # that enumerates the jobs has, ahead of any entry job's coroutine.
        assert _run_shutdown_jobs_like_home_assistant(hass) == []
        assert hass.data[SHUTDOWN_LATCH_KEY] is True

        await asyncio.sleep(0.5)
        assert setups == [], "a cancelled resume must not set the entries up"

        # And a resume that was already due is refused by the flag too.
        await integration._async_release_links(hass, 0.05)
        await asyncio.sleep(0.3)
        assert setups == []

    asyncio.run(scenario())


def test_setup_refuses_when_latched_at_entry_and_after_each_await(
    monkeypatch, stack
) -> None:
    async def scenario() -> None:
        # (1) Already latched when setup starts: nothing is even imported.
        hass = _make_hass(stack)
        hass.data[SHUTDOWN_LATCH_KEY] = True
        with pytest.raises(ConfigEntryNotReady):
            await ble.async_setup_entry(hass, FakeEntry())
        assert _registered_jobs(hass) == []

        # (2) Latched while the link is being established (inside async_start):
        # whatever setup already started is torn down and setup refuses.
        hass = _make_hass(stack)
        adv = AdvertisementData(
            local_name="EF-R31234",
            manufacturer_data={0xB5B5: _advert_payload()},
            service_uuids=[],
            service_data={},
            tx_power=None,
            rssi=-60,
            platform_data=(),
        )
        ble_device = Mock(address=ADDRESS)
        ble_device.name = "EF-R31234"
        monkeypatch.setattr(
            bluetooth,
            "async_last_service_info",
            lambda *_a, **_k: SimpleNamespace(device=ble_device, advertisement=adv),
        )
        monkeypatch.setattr(
            bluetooth, "async_ble_device_from_address", lambda *_a, **_k: None
        )
        monkeypatch.setattr(coordinator_module, "BLE_SETUP_READY_WAIT", 0.05)
        jobs_at_start: list[int] = []
        original_start = coordinator_module.EcoFlowBleCoordinator.async_start

        async def start_then_latch(self):
            # The per-entry job must already exist before the first await that
            # can open a link, or a shutdown landing in it would miss the link.
            jobs_at_start.append(len(_registered_jobs(hass)))
            result = await original_start(self)
            hass.data[SHUTDOWN_LATCH_KEY] = True
            return result

        monkeypatch.setattr(
            coordinator_module.EcoFlowBleCoordinator, "async_start", start_then_latch
        )
        entry = FakeEntry()
        with pytest.raises(ConfigEntryNotReady):
            await ble.async_setup_entry(hass, entry)
        assert jobs_at_start == [1]
        assert entry.runtime_data.closing is False  # stopped, not latched-by-job
        assert entry.runtime_data._supervisor is None, "the supervisor was stopped"
        hass.config_entries.async_forward_entry_setups.assert_not_called()

    asyncio.run(scenario())
