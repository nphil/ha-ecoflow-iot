"""Repair-issue reconciliation and the Bluetooth recovery wizard, under a HA stub.

What is pinned here is the behaviour a user sees: which issues the registry ends up
holding, which rungs the fix flow offers, and what it leaves in the entry's options.
Nothing asserts translated wording or the order in which internals were called.

Three regressions in particular must stay fixed:

* An issue is *never* deleted on the strength of remembered state. Both reconciles
  run unconditionally at setup, so a repair cannot outlive the fault it describes
  across an entry reload - which is what a remembered "we raised it" flag cannot
  survive, and the unsupported-device repair had no delete path at all.
* A device that is already unreachable when its entry loads must still raise the
  repair, even though no health *transition* ever happens - the setup-time
  reconcile is the only thing that arms the countdown for it.
* A device that never advertises at all fails setup before a coordinator exists,
  so the countdown belongs to the config entry: the most complete outage there is
  must not be the one nothing reports.

Run: ``python3 -m pytest tests/test_repairs.py``
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import re
import sys
import types
from pathlib import Path
from typing import Any

import pytest

# --- Home Assistant stub -----------------------------------------------------
# Only what the modules under test import, and total rather than conditional: the
# reconciles are asserted through a recording issue registry and a controllable
# `async_call_later`, so a real Home Assistant install would not serve them.

FLOW_MENU = "menu"
FLOW_FORM = "form"
FLOW_CREATE_ENTRY = "create_entry"
FLOW_ABORT = "abort"

# Timers armed through the stubbed `async_call_later`: [delay, action, cancelled].
TIMERS: list[list[Any]] = []


def fire_timers() -> None:
    """Run every armed timer, as Home Assistant would once its delay elapsed."""
    for slot in list(TIMERS):
        if not slot[2]:
            slot[2] = True
            slot[1](None)


def armed_delays() -> list[float]:
    """Delays of the timers still waiting to fire."""
    return [slot[0] for slot in TIMERS if not slot[2]]


def _translated_error_init(self: Any, *args: Any, **kwargs: Any) -> None:
    """Home Assistant's setup exceptions carry translation kwargs, not a message."""
    Exception.__init__(self, *args)
    self.translation_key = kwargs.get("translation_key")


class StubIssueRegistry:
    """The slice of ``IssueRegistry`` the reconciles read."""

    def __init__(self) -> None:
        self.issues: dict[tuple[str, str], dict[str, Any]] = {}

    def async_get_issue(self, domain: str, issue_id: str) -> dict[str, Any] | None:
        return self.issues.get((domain, issue_id))


def _registry(hass: Any) -> StubIssueRegistry:
    return hass.data.setdefault("issue_registry", StubIssueRegistry())


def _create_issue(
    hass: Any,
    domain: str,
    issue_id: str,
    *,
    is_fixable: bool,
    severity: Any,
    translation_key: str,
    translation_placeholders: dict[str, str] | None = None,
    **_kwargs: Any,
) -> None:
    _registry(hass).issues[(domain, issue_id)] = {
        "is_fixable": is_fixable,
        "severity": severity,
        "translation_key": translation_key,
        "translation_placeholders": translation_placeholders or {},
    }


def _delete_issue(hass: Any, domain: str, issue_id: str) -> None:
    _registry(hass).issues.pop((domain, issue_id), None)


class StubRepairsFlow:
    """The slice of ``RepairsFlow`` the wizard uses, returning HA's result shapes."""

    hass: Any = None

    def async_show_menu(
        self,
        *,
        step_id: str,
        menu_options: list[str],
        description_placeholders: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return {
            "type": FLOW_MENU,
            "step_id": step_id,
            "menu_options": menu_options,
            "description_placeholders": description_placeholders,
        }

    def async_show_form(
        self,
        *,
        step_id: str,
        data_schema: Any = None,
        description_placeholders: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return {
            "type": FLOW_FORM,
            "step_id": step_id,
            "data_schema": data_schema,
            "description_placeholders": description_placeholders,
        }

    def async_create_entry(
        self, *, data: dict[str, Any], **_kwargs: Any
    ) -> dict[str, Any]:
        return {"type": FLOW_CREATE_ENTRY, "data": data}

    def async_abort(self, *, reason: str) -> dict[str, Any]:
        return {"type": FLOW_ABORT, "reason": reason}


def install_ha_stub() -> None:
    """Install the Home Assistant modules the code under test imports."""

    def module(name: str, *, package: bool = False) -> types.ModuleType:
        mod = types.ModuleType(name)
        if package:
            mod.__path__ = []
        sys.modules[name] = mod
        parent, _, child = name.rpartition(".")
        if parent:
            setattr(sys.modules[parent], child, mod)
        return mod

    module("homeassistant", package=True)

    data_entry_flow = module("homeassistant.data_entry_flow")
    data_entry_flow.FlowResult = dict

    components = module("homeassistant.components", package=True)
    bluetooth = module("homeassistant.components.bluetooth")
    bluetooth.async_current_scanners = lambda hass: hass.data.get("scanners", [])
    bluetooth.async_ble_device_from_address = lambda hass, address, connectable: None
    bluetooth.async_last_service_info = lambda hass, address, connectable: None
    bluetooth.async_register_callback = lambda *args, **kwargs: (lambda: None)
    bluetooth.BluetoothCallbackMatcher = dict
    bluetooth.BluetoothChange = object
    bluetooth.BluetoothServiceInfoBleak = object
    bluetooth.BluetoothScanningMode = types.SimpleNamespace(PASSIVE="passive")
    components.bluetooth = bluetooth
    repairs = module("homeassistant.components.repairs")
    repairs.RepairsFlow = StubRepairsFlow

    config_entries = module("homeassistant.config_entries")
    config_entries.ConfigEntry = object
    config_entries.ConfigEntryState = ConfigEntryState

    const = module("homeassistant.const")
    const.ATTR_ENTITY_ID = "entity_id"
    const.SERVICE_TURN_OFF = "turn_off"
    const.SERVICE_TURN_ON = "turn_on"
    const.Platform = types.SimpleNamespace(
        BINARY_SENSOR="binary_sensor",
        BUTTON="button",
        CLIMATE="climate",
        LIGHT="light",
        NUMBER="number",
        SELECT="select",
        SENSOR="sensor",
        SWITCH="switch",
    )

    core = module("homeassistant.core")
    core.HomeAssistant = object
    core.callback = lambda fn: fn

    exceptions = module("homeassistant.exceptions")
    exceptions.HomeAssistantError = type("HomeAssistantError", (Exception,), {})
    # Home Assistant's setup exceptions carry translation kwargs.
    for name in ("ConfigEntryAuthFailed", "ConfigEntryError", "ConfigEntryNotReady"):
        setattr(
            exceptions,
            name,
            type(name, (Exception,), {"__init__": _translated_error_init}),
        )

    module("homeassistant.helpers", package=True)

    importlib_helper = module("homeassistant.helpers.importlib")

    async def async_import_module(hass: Any, name: str) -> Any:
        """Whatever was stubbed under that name; nothing here imports for real."""
        return sys.modules[name]

    importlib_helper.async_import_module = async_import_module

    device_registry = module("homeassistant.helpers.device_registry")
    device_registry.CONNECTION_BLUETOOTH = "bluetooth"
    device_registry.DeviceInfo = dict

    event = module("homeassistant.helpers.event")
    event.async_track_time_interval = lambda *args, **kwargs: (lambda: None)

    def async_call_later(hass: Any, delay: float, action: Any) -> Any:
        slot: list[Any] = [delay, action, False]
        TIMERS.append(slot)

        def cancel() -> None:
            slot[2] = True

        return cancel

    event.async_call_later = async_call_later

    issue_registry = module("homeassistant.helpers.issue_registry")
    issue_registry.IssueSeverity = types.SimpleNamespace(WARNING="warning")
    issue_registry.async_get = _registry
    issue_registry.async_create_issue = _create_issue
    issue_registry.async_delete_issue = _delete_issue

    selector = module("homeassistant.helpers.selector")
    selector.EntitySelector = lambda config: {"entity": config}
    selector.EntitySelectorConfig = dict

    update_coordinator = module("homeassistant.helpers.update_coordinator")
    update_coordinator.DataUpdateCoordinator = StubDataUpdateCoordinator
    update_coordinator.UpdateFailed = type("UpdateFailed", (Exception,), {})

    util = module("homeassistant.util", package=True)
    util.slugify = lambda text: re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    dt_module = module("homeassistant.util.dt")
    dt_module.utcnow = lambda: None
    util.dt = dt_module


class ConfigEntryState(enum.Enum):
    """The entry states the wizard distinguishes."""

    LOADED = "loaded"
    SETUP_RETRY = "setup_retry"
    SETUP_ERROR = "setup_error"
    NOT_LOADED = "not_loaded"


class StubDataUpdateCoordinator:
    """The slice of ``DataUpdateCoordinator`` both coordinators build on."""

    def __class_getitem__(cls, item: Any) -> Any:
        return cls

    def __init__(self, hass: Any, logger: Any, **kwargs: Any) -> None:
        self.hass = hass
        self.logger = logger
        self.name = kwargs.get("name")
        self.config_entry = kwargs.get("config_entry")
        self.update_interval = kwargs.get("update_interval")
        self.last_update_success = True
        self.data: Any = None
        self.notified = 0

    def async_update_listeners(self) -> None:
        self.notified += 1

    def async_set_updated_data(self, data: Any) -> None:
        self.data = data

    async def async_shutdown(self) -> None:
        return None


install_ha_stub()

_ROOT = Path(__file__).resolve().parents[1] / "custom_components"
sys.path.insert(0, str(_ROOT))

# A sibling stub-world test module may already have imported these against *its*
# stubs, in which case `import` would hand back modules bound to a no-op issue
# registry and the assertions here would silently pass over nothing. Dropping
# them forces a fresh import against the stubs above; whatever the other module
# already holds in its own globals keeps working, since it holds the objects.
for _name in [name for name in sys.modules if name.split(".")[0] == "ecoflow_iot"]:
    del sys.modules[_name]

# The package roots are faked so their `__init__` (which imports aiohttp, protobuf
# and the crypto stack) never runs; every module under test is imported through
# them.
_pkg = types.ModuleType("ecoflow_iot")
_pkg.__path__ = [str(_ROOT / "ecoflow_iot")]
sys.modules["ecoflow_iot"] = _pkg

_api = types.ModuleType("ecoflow_iot.api")


class EcoFlowError(Exception):
    """Stub API error."""


class EcoFlowApiError(EcoFlowError):
    """Stub business-code error, mirroring the real ``code``/``message`` pair."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"EcoFlow API error {code}: {message}")


_api.EcoFlowError = EcoFlowError
_api.EcoFlowApiError = EcoFlowApiError
_api.EcoFlowHttpClient = object
_api.EcoFlowMqttClient = object
sys.modules["ecoflow_iot.api"] = _api

_devices = types.ModuleType("ecoflow_iot.devices")
_devices.EcoFlowDevice = object
_devices.resolve_device = lambda sn, quota: None
_devices.is_silenced = lambda sn: False
sys.modules["ecoflow_iot.devices"] = _devices

# `eflib.exceptions` is pure Python and is imported for real (the coordinator's
# fatal-credential tuple is built from it); the rest of eflib needs bleak.
_eflib = types.ModuleType("ecoflow_iot.eflib")
_eflib.__path__ = [str(_ROOT / "ecoflow_iot" / "eflib")]
_eflib.DeviceBase = object
sys.modules["ecoflow_iot.eflib"] = _eflib
_connection = types.ModuleType("ecoflow_iot.eflib.connection")
_connection.Connection = type("Connection", (), {"Options": dict})
sys.modules["ecoflow_iot.eflib.connection"] = _connection

# The `ble` package __init__ is imported for real: its setup/unload/remove paths
# are where the countdown is armed for a device that never advertises, and where
# it is cancelled and the issue retired. Only `eflib` under it is stubbed.
from ecoflow_iot import repairs  # noqa: E402
from ecoflow_iot import ble  # noqa: E402
from ecoflow_iot.ble import unreachable  # noqa: E402
from ecoflow_iot.ble.coordinator import EcoFlowBleCoordinator  # noqa: E402
from ecoflow_iot.const import (  # noqa: E402
    BLE_UNREACHABLE_SECONDS,
    CONF_ADDRESS,
    CONF_DEVICE_NAME,
    CONF_LAST_HOLDING_PROXY,
    CONF_RECOVERY_OUTLET,
    CONF_SERIAL,
    CONF_TRANSPORT,
    CONF_UPDATE_PERIOD,
    DOMAIN,
    TRANSPORT_BLE,
    TRANSPORT_CLOUD,
    unreachable_issue_id,
)
import ecoflow_iot.coordinator as cloud_module  # noqa: E402
from ecoflow_iot.coordinator import EcoFlowCoordinator  # noqa: E402

# Provided by the stub `homeassistant.exceptions` module installed above, not by
# a real Home Assistant: the not-ready path is what arms the countdown for a
# device that never advertises, so the tests need the type to assert against.
from homeassistant.exceptions import ConfigEntryNotReady  # noqa: E402

ADDRESS = "AA:BB:CC:DD:EE:01"
SERIAL = "R351ZTEST0000001"
ISSUE_ID = unreachable_issue_id(ADDRESS)
# The shape habluetooth actually reports: `scanner.adapter` is the ESPHome node
# name (bleak_esphome passes `device_info.name`), and `scanner.name` is
# `adapter_human_name(adapter, source)` == "<node> (<MAC>)". The action the
# esphome integration registers for that node is `name.replace("-", "_")` plus
# the action's own name.
PROXY_MAC = "E8:9F:6D:11:22:33"
PROXY_NODE = "plant-room-bluetooth-proxy"
PROXY_DISPLAY = f"{PROXY_NODE} ({PROXY_MAC})"
PROXY_ACTION = "plant_room_bluetooth_proxy_restart_proxy"


# --- fakes -------------------------------------------------------------------


class FakeServices:
    def __init__(self) -> None:
        self._registered: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def register(self, domain: str, service: str) -> None:
        self._registered.setdefault(domain, {})[service] = object()

    def has_service(self, domain: str, service: str) -> bool:
        return service in self._registered.get(domain, {})

    async def async_call(
        self,
        domain: Any,
        service: str,
        data: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> None:
        self.calls.append((str(domain), service, dict(data or {})))


class FakeConfigEntries:
    def __init__(self) -> None:
        self.entries: dict[str, FakeConfigEntry] = {}
        self.reloads: list[str] = []
        self.updates = 0

    def add(self, entry: FakeConfigEntry) -> FakeConfigEntry:
        self.entries[entry.entry_id] = entry
        return entry

    def async_entries(self, domain: str | None = None) -> list[FakeConfigEntry]:
        return list(self.entries.values())

    def async_get_entry(self, entry_id: str) -> FakeConfigEntry | None:
        return self.entries.get(entry_id)

    def async_update_entry(self, entry: FakeConfigEntry, **changes: Any) -> bool:
        self.updates += 1
        if "options" in changes:
            entry.options = changes["options"]
        return True

    async def async_reload(self, entry_id: str) -> bool:
        self.reloads.append(entry_id)
        return True

    async def async_unload_platforms(
        self, entry: FakeConfigEntry, platforms: list[str]
    ) -> bool:
        return True


class FakeConfigEntry:
    """The slice of ``ConfigEntry`` the code under test touches."""

    def __init__(
        self,
        entry_id: str,
        *,
        data: dict[str, Any],
        options: dict[str, Any] | None = None,
        title: str = "River 3 Pro",
        state: ConfigEntryState = ConfigEntryState.LOADED,
    ) -> None:
        self.entry_id = entry_id
        self.data = data
        self.options = options or {}
        self.title = title
        self.state = state

    def async_create_background_task(
        self, hass: Any, coro: Any, name: str, eager_start: bool = False
    ) -> None:
        # Nothing supervises the link in a stub: the tests drive the health
        # transitions themselves, which is the only part the repair reacts to.
        coro.close()
        return None


class FakeHass:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.config_entries = FakeConfigEntries()
        self.services = FakeServices()
        self.loop = types.SimpleNamespace(time=lambda: 0.0)


class FakeDevice:
    """The slice of an ``eflib`` device the coordinator touches while holding."""

    def on_packet_parsed(self, callback: Any) -> Any:
        return lambda: None

    def register_callback(self, callback: Any) -> None:
        return None

    def remove_callback(self, callback: Any) -> None:
        return None

    def with_update_period(self, period: int) -> FakeDevice:
        return self

    def with_disabled_reconnect(self, disabled: bool) -> FakeDevice:
        return self

    def with_connection_options(self, options: Any) -> FakeDevice:
        return self

    async def disconnect(self) -> None:
        return None


class FakeScanner:
    """The slice of a habluetooth scanner the coordinator reads."""

    def __init__(self, node: str, allocated: tuple[str, ...]) -> None:
        self.adapter = node
        self.source = PROXY_MAC
        self.name = f"{node} ({PROXY_MAC})"
        self._allocated = allocated

    def get_allocations(self) -> Any:
        return types.SimpleNamespace(allocated=self._allocated)


class StubBleCoordinator:
    """The slice of ``EcoFlowBleCoordinator`` the fix flow contracts on."""

    def __init__(self, *, connected: bool = False, node: str | None = None) -> None:
        self.connected = connected
        self.holding_proxy_name = node if connected else None
        if not connected:
            self.link_state = "disconnected"
        else:
            self.link_state = f"{node} ({PROXY_MAC})" if node else "connected"
        self.reconciles = 0

    def reconcile_unreachable_issue(self) -> None:
        self.reconciles += 1


# --- helpers -----------------------------------------------------------------


def ble_entry(**kwargs: Any) -> FakeConfigEntry:
    return FakeConfigEntry(
        "ble-entry",
        data={
            CONF_TRANSPORT: TRANSPORT_BLE,
            CONF_ADDRESS: ADDRESS,
            CONF_SERIAL: SERIAL,
            CONF_DEVICE_NAME: "River 3 Pro",
        },
        **kwargs,
    )


def cloud_entry() -> FakeConfigEntry:
    return FakeConfigEntry(
        "cloud-entry",
        data={CONF_TRANSPORT: TRANSPORT_CLOUD, "access_key": "k", "secret_key": "s"},
        title="EcoFlow account",
    )


def start_ble(
    hass: FakeHass, entry: FakeConfigEntry, *, connected: bool = False
) -> EcoFlowBleCoordinator:
    """Load a BLE entry the way ``ble.async_setup_entry`` does.

    ``connected`` says the link is already up as setup runs. It is set as state
    rather than driven through a transition on purpose: a transition deletes the
    issue by itself, so it would never exercise the setup-time reconcile.
    """
    coordinator = EcoFlowBleCoordinator(
        hass,
        entry,
        FakeDevice(),
        serial=SERIAL,
        address=ADDRESS,
        model="River 3 Pro",
        local_name="EF-R3PR0001",
        device_name=entry.data.get(CONF_DEVICE_NAME, ""),
        user_id="1234567890123456789",
    )
    coordinator._connected = connected
    # Setup does not wait for a link the stub will not deliver.
    coordinator._settled.set()
    assert asyncio.run(coordinator.async_start()) is None
    entry.runtime_data = coordinator
    return coordinator


def issue_ids(hass: FakeHass) -> set[str]:
    return {issue_id for _, issue_id in _registry(hass).issues}


def new_hass() -> FakeHass:
    TIMERS.clear()
    return FakeHass()


# --- the auto-clear rule -----------------------------------------------------


def test_setup_deletes_the_repair_of_a_device_that_is_healthy():
    """The orphan bug: a stale issue must not survive a reload of a healthy entry.

    Deletion is gated on nothing but the live link - not on this process having
    been the one that raised it, which is the memory a reload throws away.
    """
    hass = new_hass()
    entry = hass.config_entries.add(ble_entry())
    _create_issue(
        hass,
        DOMAIN,
        ISSUE_ID,
        is_fixable=True,
        severity="warning",
        translation_key="device_unreachable",
    )

    start_ble(hass, entry, connected=True)

    assert issue_ids(hass) == set()


def test_setup_arms_the_countdown_for_a_device_that_is_already_down():
    """A device down before the entry loaded gets no transition; setup must still act."""
    hass = new_hass()
    entry = hass.config_entries.add(ble_entry())

    start_ble(hass, entry)

    assert armed_delays() == [BLE_UNREACHABLE_SECONDS]
    assert issue_ids(hass) == set()

    fire_timers()

    issue = _registry(hass).issues[(DOMAIN, ISSUE_ID)]
    assert issue["is_fixable"] is True
    assert issue["translation_key"] == "device_unreachable"
    assert issue["translation_placeholders"]["device"] == "River 3 Pro"


def test_a_link_inside_the_threshold_raises_nothing():
    """Reconnecting before the threshold disarms the countdown entirely."""
    hass = new_hass()
    entry = hass.config_entries.add(ble_entry())
    coordinator = start_ble(hass, entry)

    coordinator._set_connected(True)
    fire_timers()

    assert issue_ids(hass) == set()


def test_reconnecting_deletes_the_repair():
    hass = new_hass()
    entry = hass.config_entries.add(ble_entry())
    coordinator = start_ble(hass, entry)
    fire_timers()
    assert issue_ids(hass) == {ISSUE_ID}

    coordinator._set_connected(True)

    assert issue_ids(hass) == set()


def test_a_device_that_never_advertises_still_raises_the_repair():
    """The real setup path, which fails before a coordinator can exist.

    A device already gone when Home Assistant starts is the most complete outage
    there is, and it is the one a supervisor-owned countdown never reports - the
    supervisor is never built, because setup raises ConfigEntryNotReady first.
    """
    hass = new_hass()
    entry = hass.config_entries.add(ble_entry())

    with pytest.raises(ConfigEntryNotReady):
        asyncio.run(ble.async_setup_entry(hass, entry))

    assert armed_delays() == [BLE_UNREACHABLE_SECONDS]
    fire_timers()
    assert issue_ids(hass) == {ISSUE_ID}


def test_setup_retries_do_not_push_the_deadline_out():
    """Home Assistant re-enters setup on every retry; the device has been gone
    since the first one, which is the moment the operator cares about."""
    hass = new_hass()
    entry = hass.config_entries.add(ble_entry())

    for _ in range(4):
        with pytest.raises(ConfigEntryNotReady):
            asyncio.run(ble.async_setup_entry(hass, entry))

    assert armed_delays() == [BLE_UNREACHABLE_SECONDS]


def test_unloading_leaves_no_countdown_behind():
    """A timer outliving the entry would raise a repair for a device nobody holds.

    An entry the user disables is unloaded exactly like one being reloaded, so
    the cancel has to be on the unload path itself.
    """
    hass = new_hass()
    entry = hass.config_entries.add(ble_entry())
    coordinator = start_ble(hass, entry)
    entry.runtime_data = coordinator

    assert asyncio.run(ble.async_unload_entry(hass, entry)) is True
    fire_timers()

    assert issue_ids(hass) == set()


def test_removing_the_entry_retires_its_repair():
    """Nothing else deletes an issue naming a device that is no longer configured."""
    hass = new_hass()
    entry = hass.config_entries.add(ble_entry())
    start_ble(hass, entry)
    fire_timers()
    assert issue_ids(hass) == {ISSUE_ID}

    asyncio.run(ble.async_remove_entry(hass, entry))

    assert issue_ids(hass) == set()


def test_holding_proxy_is_recorded_as_a_node_name_once_per_change():
    """The wizard's only proxy candidate, written without churning the entry.

    The node name is what gets stored, not the display name: the display name
    carries the proxy's address (which diagnostics must not export) and is not
    what the node's own actions are named after.
    """
    hass = new_hass()
    entry = hass.config_entries.add(ble_entry(options={CONF_UPDATE_PERIOD: 20}))
    hass.data["scanners"] = [FakeScanner(PROXY_NODE, (ADDRESS,))]
    coordinator = start_ble(hass, entry)

    coordinator._set_connected(True)
    assert entry.options == {
        CONF_UPDATE_PERIOD: 20,
        CONF_LAST_HOLDING_PROXY: PROXY_NODE,
    }
    # The link sensor the household automations read still names the address.
    assert coordinator.link_state == PROXY_DISPLAY
    writes = hass.config_entries.updates

    coordinator._set_connected(False)
    coordinator._set_connected(True)

    assert hass.config_entries.updates == writes, "unchanged proxy must not rewrite"


# --- the cloud path ----------------------------------------------------------


def cloud_coordinator(hass: FakeHass, entry: FakeConfigEntry, serials: list[str]):
    """A cloud coordinator whose account holds ``serials``, none of them mappable."""

    class Http:
        async def device_list(self) -> list[dict[str, Any]]:
            return [{"sn": sn, "online": 1} for sn in serials]

        async def get_all_quota(self, sn: str) -> dict[str, Any]:
            return {"sn": sn}

    coordinator = EcoFlowCoordinator(hass, entry, Http(), enable_mqtt=False)
    return coordinator


def test_cloud_entry_raises_no_unreachable_repair():
    """The unreachable repair belongs to the Bluetooth transport alone."""
    hass = new_hass()
    entry = hass.config_entries.add(cloud_entry())

    asyncio.run(cloud_coordinator(hass, entry, [SERIAL]).async_setup())

    assert issue_ids(hass) == {"unsupported_device_R351"}
    assert armed_delays() == []


def test_setup_deletes_the_repair_of_a_device_that_is_now_supported(monkeypatch):
    """The same orphan bug on the cloud path: this one had no delete path at all."""
    hass = new_hass()
    entry = hass.config_entries.add(cloud_entry())
    _create_issue(
        hass,
        DOMAIN,
        "unsupported_device_R351",
        is_fixable=False,
        severity="warning",
        translation_key="unsupported_device",
    )
    # The device the last release could not map now resolves to a class.
    device = types.SimpleNamespace(has_http_only_entities=lambda: False)
    monkeypatch.setattr(cloud_module, "resolve_device", lambda sn, quota: device)

    asyncio.run(cloud_coordinator(hass, entry, [SERIAL]).async_setup())

    assert issue_ids(hass) == set()


def test_another_accounts_unsupported_device_is_not_swept_away(monkeypatch):
    """Two accounts can hold the same unsupported model; one loading is not a verdict."""
    hass = new_hass()
    other = hass.config_entries.add(
        FakeConfigEntry("cloud-2", data={CONF_TRANSPORT: TRANSPORT_CLOUD})
    )
    other.runtime_data = cloud_coordinator(hass, other, [])
    other.runtime_data.unsupported_prefixes = {"R351"}
    _create_issue(
        hass,
        DOMAIN,
        "unsupported_device_R351",
        is_fixable=False,
        severity="warning",
        translation_key="unsupported_device",
    )
    entry = hass.config_entries.add(cloud_entry())
    device = types.SimpleNamespace(has_http_only_entities=lambda: False)
    monkeypatch.setattr(cloud_module, "resolve_device", lambda sn, quota: device)

    asyncio.run(cloud_coordinator(hass, entry, ["R601ZTEST0000002"]).async_setup())

    assert issue_ids(hass) == {"unsupported_device_R351"}


# --- the wizard --------------------------------------------------------------


def flow(hass: FakeHass, issue_id: str = ISSUE_ID) -> repairs.BleRecoveryFixFlow:
    handler = asyncio.run(repairs.async_create_fix_flow(hass, issue_id, None))
    handler.hass = hass
    return handler


def test_the_flow_finds_its_entry_by_address_and_ignores_cloud_entries():
    hass = new_hass()
    hass.config_entries.add(cloud_entry())
    entry = hass.config_entries.add(ble_entry())
    entry.runtime_data = StubBleCoordinator()

    assert asyncio.run(flow(hass).async_step_init())["type"] == FLOW_MENU

    # An id no configured address produces has nothing to act on.
    aborted = asyncio.run(
        flow(hass, unreachable_issue_id("00:11:22:33:44:55")).async_step_init()
    )
    assert aborted == {"type": FLOW_ABORT, "reason": "entry_gone"}


def test_restart_proxy_is_offered_only_with_a_proxy_and_an_action():
    """A rung that cannot do anything is worse than a rung that is not there."""
    hass = new_hass()
    entry = hass.config_entries.add(ble_entry())
    entry.runtime_data = StubBleCoordinator()

    unknown = asyncio.run(flow(hass).async_step_menu())
    assert unknown["menu_options"] == ["recheck", "reload", "power_cycle"]

    # A proxy is known, but nothing exposes a restart action for it.
    entry.options = {CONF_LAST_HOLDING_PROXY: PROXY_NODE}
    assert asyncio.run(flow(hass).async_step_menu())["menu_options"] == [
        "recheck",
        "reload",
        "power_cycle",
    ]

    # The display name is not the action's name: a rung derived from it would
    # look up an action nobody registered and vanish.
    hass.services.register("esphome", PROXY_ACTION)
    entry.options = {CONF_LAST_HOLDING_PROXY: PROXY_DISPLAY}
    assert "restart_proxy" not in (
        asyncio.run(flow(hass).async_step_menu())["menu_options"]
    )

    # The node exposes its action: now the rung can act.
    entry.options = {CONF_LAST_HOLDING_PROXY: PROXY_NODE}
    assert asyncio.run(flow(hass).async_step_menu())["menu_options"] == [
        "recheck",
        "reload",
        "restart_proxy",
        "power_cycle",
    ]

    # The live holding proxy stands in for the remembered one.
    entry.options = {}
    entry.runtime_data = StubBleCoordinator(connected=True, node=PROXY_NODE)
    assert "restart_proxy" in asyncio.run(flow(hass).async_step_menu())["menu_options"]


def test_the_menu_reports_state_without_inventing_a_last_result():
    hass = new_hass()
    entry = hass.config_entries.add(
        ble_entry(options={CONF_LAST_HOLDING_PROXY: PROXY_NODE})
    )
    entry.runtime_data = StubBleCoordinator()

    placeholders = asyncio.run(flow(hass).async_step_menu())["description_placeholders"]

    assert placeholders["device"] == "River 3 Pro"
    assert placeholders["link"] == "disconnected"
    assert placeholders["proxy"] == PROXY_NODE
    assert placeholders["last_result"] == ""


def test_an_unactionable_entry_aborts():
    """A serial mismatch or a disabled entry needs a decision, not a power cycle."""
    hass = new_hass()
    hass.config_entries.add(ble_entry(state=ConfigEntryState.SETUP_ERROR))

    assert asyncio.run(flow(hass).async_step_menu()) == {
        "type": FLOW_ABORT,
        "reason": "not_loaded",
    }


def test_a_device_out_of_range_at_startup_can_still_be_recovered():
    """Setup fails when the device is away, which is exactly when the flow is opened."""
    hass = new_hass()
    hass.config_entries.add(ble_entry(state=ConfigEntryState.SETUP_RETRY))

    assert asyncio.run(flow(hass).async_step_menu())["type"] == FLOW_MENU


def test_reload_goes_through_the_config_entry(monkeypatch):
    hass = new_hass()
    entry = hass.config_entries.add(ble_entry())
    entry.runtime_data = StubBleCoordinator()
    monkeypatch.setattr(repairs, "_SETTLE_TIMEOUT", 0.0)

    result = asyncio.run(flow(hass).async_step_reload())

    assert hass.config_entries.reloads == ["ble-entry"]
    # Still down, so the ladder is offered again rather than declaring success.
    assert result["type"] == FLOW_MENU
    assert "Reloaded the integration" in result["description_placeholders"]["last_result"]


def test_restart_proxy_calls_the_esphome_action_of_the_remembered_node(monkeypatch):
    hass = new_hass()
    entry = hass.config_entries.add(
        ble_entry(options={CONF_LAST_HOLDING_PROXY: PROXY_NODE})
    )
    entry.runtime_data = StubBleCoordinator()
    hass.services.register("esphome", PROXY_ACTION)
    monkeypatch.setattr(repairs, "_SETTLE_TIMEOUT", 0.0)

    result = asyncio.run(flow(hass).async_step_restart_proxy())

    assert hass.services.calls == [("esphome", PROXY_ACTION, {})]
    assert result["type"] == FLOW_MENU


def test_power_cycle_remembers_the_outlet_and_switches_it(monkeypatch):
    """The outlet is asked for once; the update period must survive the write."""
    hass = new_hass()
    entry = hass.config_entries.add(ble_entry(options={CONF_UPDATE_PERIOD: 20}))
    entry.runtime_data = StubBleCoordinator()
    monkeypatch.setattr(repairs, "_POWER_OFF_SECONDS", 0.0)
    monkeypatch.setattr(repairs, "_POWER_CYCLE_TIMEOUT", 0.0)

    form = asyncio.run(flow(hass).async_step_power_cycle())
    assert form["type"] == FLOW_FORM and form["step_id"] == "power_cycle"

    result = asyncio.run(
        flow(hass).async_step_power_cycle({CONF_RECOVERY_OUTLET: "switch.river_outlet"})
    )

    assert entry.options == {
        CONF_UPDATE_PERIOD: 20,
        CONF_RECOVERY_OUTLET: "switch.river_outlet",
    }
    assert hass.services.calls == [
        ("switch", "turn_off", {"entity_id": "switch.river_outlet"}),
        ("switch", "turn_on", {"entity_id": "switch.river_outlet"}),
    ]
    assert result["type"] == FLOW_MENU

    # Asked again, the form comes back prefilled with what was stored.
    reopened = asyncio.run(flow(hass).async_step_power_cycle())
    keys = list(reopened["data_schema"].schema)
    assert keys[0].default() == "switch.river_outlet"


def test_closing_the_dialog_mid_cut_still_restores_the_mains(monkeypatch):
    """Cancelling the flow during the 10 s cut must not leave the device dark."""
    hass = new_hass()
    entry = hass.config_entries.add(ble_entry())
    entry.runtime_data = StubBleCoordinator()
    monkeypatch.setattr(repairs, "_POWER_OFF_SECONDS", 0.2)

    async def scenario() -> None:
        handler = await repairs.async_create_fix_flow(hass, ISSUE_ID, None)
        handler.hass = hass
        cycling = asyncio.ensure_future(
            handler.async_step_power_cycle({CONF_RECOVERY_OUTLET: "switch.river_outlet"})
        )
        await asyncio.sleep(0.05)  # inside the cut
        cycling.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cycling
        # The restore is shielded, so it outlives the cancellation: let it land.
        await asyncio.sleep(0.05)

    asyncio.run(scenario())

    assert [service for _, service, _ in hass.services.calls] == ["turn_off", "turn_on"]


def test_a_link_that_comes_back_completes_the_flow_and_clears_the_repair():
    """The real reconcile runs too, so the issue is gone either way."""
    hass = new_hass()
    entry = hass.config_entries.add(ble_entry())
    start_ble(hass, entry, connected=True)
    _create_issue(
        hass,
        DOMAIN,
        ISSUE_ID,
        is_fixable=True,
        severity="warning",
        translation_key="device_unreachable",
    )

    result = asyncio.run(flow(hass).async_step_recheck())

    assert result == {"type": FLOW_CREATE_ENTRY, "data": {}}
    assert issue_ids(hass) == set()
