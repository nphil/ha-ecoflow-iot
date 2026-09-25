"""Cloud/BLE dual-ownership tests under a minimal Home Assistant stub.

Both transports identify a device the same way - device identifiers
``{(DOMAIN, sn)}`` and unique_id ``f"{sn}_{key}"`` - so a serial served by
both fights itself for the same Home Assistant device and entity ids (seen
live: ``ID R621FAB1XFAQ0047_connection already exists``). The rule: a serial
with a configured, *enabled* BLE entry is owned by BLE. The cloud coordinator
must skip that serial entirely - no device, no entities, no HTTP polling, no
MQTT subscription - clean up whatever it registered before the BLE entry
existed, and pick the serial back up (via a reload) once that BLE entry is
removed or disabled.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import types
from pathlib import Path


def install_ha_stub() -> None:
    """Install only the Home Assistant modules coordinator.py imports."""
    ha = types.ModuleType("homeassistant")
    ha.__path__ = []
    sys.modules["homeassistant"] = ha

    config_entries = types.ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = object
    config_entries.ConfigEntryChange = types.SimpleNamespace(
        ADDED="added", REMOVED="removed", UPDATED="updated"
    )
    config_entries.SIGNAL_CONFIG_ENTRY_CHANGED = "config_entry_changed"
    sys.modules["homeassistant.config_entries"] = config_entries

    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    core.callback = lambda fn: fn
    sys.modules["homeassistant.core"] = core

    exceptions = types.ModuleType("homeassistant.exceptions")
    exceptions.HomeAssistantError = Exception
    sys.modules["homeassistant.exceptions"] = exceptions

    helpers = types.ModuleType("homeassistant.helpers")
    helpers.__path__ = []
    sys.modules["homeassistant.helpers"] = helpers

    event = types.ModuleType("homeassistant.helpers.event")
    event.async_track_time_interval = lambda *args, **kwargs: (lambda: None)
    sys.modules["homeassistant.helpers.event"] = event

    dispatcher = types.ModuleType("homeassistant.helpers.dispatcher")
    dispatcher.async_dispatcher_connect = lambda *args, **kwargs: (lambda: None)
    sys.modules["homeassistant.helpers.dispatcher"] = dispatcher

    entity_registry_mod = types.ModuleType("homeassistant.helpers.entity_registry")
    entity_registry_mod.async_get = lambda hass: hass.entity_registry
    entity_registry_mod.async_entries_for_config_entry = (
        lambda registry, entry_id: registry.entries_for_config_entry(entry_id)
    )
    sys.modules["homeassistant.helpers.entity_registry"] = entity_registry_mod

    device_registry_mod = types.ModuleType("homeassistant.helpers.device_registry")
    device_registry_mod.async_get = lambda hass: hass.device_registry
    device_registry_mod.async_entries_for_config_entry = (
        lambda registry, entry_id: registry.entries_for_config_entry(entry_id)
    )
    sys.modules["homeassistant.helpers.device_registry"] = device_registry_mod

    issue_registry = types.ModuleType("homeassistant.helpers.issue_registry")
    issue_registry.IssueSeverity = types.SimpleNamespace(WARNING="warning")
    issue_registry.async_create_issue = lambda *args, **kwargs: None
    issue_registry.async_delete_issue = lambda *args, **kwargs: None
    issue_registry.async_get = lambda hass: types.SimpleNamespace(issues={})
    sys.modules["homeassistant.helpers.issue_registry"] = issue_registry

    update_coordinator = types.ModuleType("homeassistant.helpers.update_coordinator")

    class DataUpdateCoordinator:
        def __class_getitem__(cls, item):
            return cls

        def __init__(self, *args, **kwargs):
            self.config_entry = kwargs.get("config_entry")

        def async_set_updated_data(self, data):
            self.data = data

        def async_update_listeners(self):
            return None

        async def async_shutdown(self):
            return None

    update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
    update_coordinator.UpdateFailed = Exception
    sys.modules["homeassistant.helpers.update_coordinator"] = update_coordinator


install_ha_stub()

root = Path(__file__).resolve().parents[1] / "custom_components"
sys.path.insert(0, str(root))

pkg = types.ModuleType("ecoflow_iot")
pkg.__path__ = [str(root / "ecoflow_iot")]
sys.modules["ecoflow_iot"] = pkg

api = types.ModuleType("ecoflow_iot.api")


class EcoFlowError(Exception):
    """Stub API error."""


class EcoFlowApiError(EcoFlowError):
    """Stub business-code error, mirroring the real ``code``/``message`` pair."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"EcoFlow API error {code}: {message}")


api.EcoFlowError = EcoFlowError
api.EcoFlowApiError = EcoFlowApiError
api.EcoFlowHttpClient = object
api.EcoFlowMqttClient = object
sys.modules["ecoflow_iot.api"] = api

devices = types.ModuleType("ecoflow_iot.devices")


class FakeSupportedDevice:
    """Stands in for a real `EcoFlowDevice` the cloud path would create."""

    def has_http_only_entities(self) -> bool:
        return False


RESOLVABLE_SN = "R621FAB1XFAQ0047"

resolve_calls: list[str] = []


def _tracking_resolve_device(sn, quota):
    resolve_calls.append(sn)
    return FakeSupportedDevice() if sn == RESOLVABLE_SN else None


devices.EcoFlowDevice = object
devices.resolve_device = _tracking_resolve_device
devices.is_silenced = lambda sn: False
sys.modules["ecoflow_iot.devices"] = devices

from ecoflow_iot.const import (  # noqa: E402
    CONF_SERIAL,
    CONF_TRANSPORT,
    DOMAIN,
    TRANSPORT_BLE,
)
from ecoflow_iot.coordinator import EcoFlowCoordinator  # noqa: E402


class FakeConfigEntry:
    """Just enough of `ConfigEntry` for `is_ble_entry`/`_is_ble_owned`."""

    def __init__(self, entry_id, domain, data, *, disabled_by=None):
        self.entry_id = entry_id
        self.domain = domain
        self.data = data
        self.disabled_by = disabled_by
        self.unload_callbacks: list = []

    def async_on_unload(self, func):
        self.unload_callbacks.append(func)


def ble_entry(sn: str, *, entry_id: str = "ble1", disabled: bool = False) -> FakeConfigEntry:
    return FakeConfigEntry(
        entry_id,
        DOMAIN,
        {CONF_TRANSPORT: TRANSPORT_BLE, CONF_SERIAL: sn},
        disabled_by="user" if disabled else None,
    )


class FakeConfigEntries:
    def __init__(self, entries: list[FakeConfigEntry]) -> None:
        self._entries = list(entries)
        self.reloaded: list[str] = []

    def async_entries(self, domain=None):
        return [e for e in self._entries if domain is None or e.domain == domain]

    def async_schedule_reload(self, entry_id: str) -> None:
        self.reloaded.append(entry_id)


class FakeEntityEntry:
    def __init__(self, entity_id: str, unique_id: str, config_entry_id: str) -> None:
        self.entity_id = entity_id
        self.unique_id = unique_id
        self.config_entry_id = config_entry_id


class FakeEntityRegistry:
    def __init__(self, entries: list[FakeEntityEntry]) -> None:
        self.entries = list(entries)
        self.removed: list[str] = []

    def entries_for_config_entry(self, config_entry_id: str) -> list[FakeEntityEntry]:
        return [e for e in self.entries if e.config_entry_id == config_entry_id]

    def async_remove(self, entity_id: str) -> None:
        self.removed.append(entity_id)
        self.entries = [e for e in self.entries if e.entity_id != entity_id]


class FakeDeviceEntry:
    def __init__(self, device_id: str, identifiers: set, config_entry_id: str) -> None:
        self.id = device_id
        self.identifiers = identifiers
        self.config_entry_id = config_entry_id


class FakeDeviceRegistry:
    def __init__(self, entries: list[FakeDeviceEntry]) -> None:
        self.entries = list(entries)
        self.removed: list[str] = []

    def entries_for_config_entry(self, config_entry_id: str) -> list[FakeDeviceEntry]:
        return [e for e in self.entries if e.config_entry_id == config_entry_id]

    def async_remove_device(self, device_id: str) -> None:
        self.removed.append(device_id)
        self.entries = [e for e in self.entries if e.id != device_id]


class FakeHass:
    def __init__(
        self,
        entries: list[FakeConfigEntry],
        *,
        entity_entries: list[FakeEntityEntry] | None = None,
        device_entries: list[FakeDeviceEntry] | None = None,
    ) -> None:
        self.config_entries = FakeConfigEntries(entries)
        self.entity_registry = FakeEntityRegistry(entity_entries or [])
        self.device_registry = FakeDeviceRegistry(device_entries or [])


class FakeHttp:
    def __init__(self, listed: list[dict], quotas: dict[str, dict] | None = None) -> None:
        self._listed = listed
        self._quotas = quotas or {}
        self.quota_calls: list[str] = []

    async def device_list(self):
        return self._listed

    async def get_all_quota(self, sn: str):
        self.quota_calls.append(sn)
        return self._quotas.get(sn, {})

    async def get_certification(self):
        return {}


def make_coordinator(
    *,
    other_entries: list[FakeConfigEntry],
    listed: list[dict],
    entity_entries: list[FakeEntityEntry] | None = None,
    device_entries: list[FakeDeviceEntry] | None = None,
) -> tuple[EcoFlowCoordinator, FakeHass, FakeHttp]:
    coord = EcoFlowCoordinator.__new__(EcoFlowCoordinator)
    cloud_entry = FakeConfigEntry("cloud1", DOMAIN, {})
    hass = FakeHass(
        [cloud_entry, *other_entries],
        entity_entries=entity_entries,
        device_entries=device_entries,
    )
    http = FakeHttp(listed)

    coord.hass = hass
    coord.config_entry = cloud_entry
    coord._http = http
    coord._enable_mqtt = False  # exercised separately; irrelevant to this fix
    coord.devices = {}
    coord.unmapped = {}
    coord.unsupported_prefixes = set()
    coord._http_only_sns = set()
    coord.data = {}
    return coord, hass, http


def test_cloud_skips_a_ble_owned_serial_and_cleans_up_stale_registrations(caplog):
    """A serial with a configured, enabled BLE entry gets no device, no
    entities, no polling - and whatever this cloud entry registered for it
    before the BLE entry existed (the live collision's root cause) is removed.
    """
    resolve_calls.clear()
    stale_entities = [
        FakeEntityEntry(
            "sensor.camping_power_station_connection",
            f"{RESOLVABLE_SN}_connection",
            "cloud1",
        ),
        FakeEntityEntry(
            "sensor.camping_power_station_battery_level",
            f"{RESOLVABLE_SN}_battery_level",
            "cloud1",
        ),
    ]
    stale_device = FakeDeviceEntry("dev1", {(DOMAIN, RESOLVABLE_SN)}, "cloud1")
    coord, hass, http = make_coordinator(
        other_entries=[ble_entry(RESOLVABLE_SN)],
        listed=[{"sn": RESOLVABLE_SN, "online": 1}],
        entity_entries=stale_entities,
        device_entries=[stale_device],
    )

    with caplog.at_level(logging.INFO):
        asyncio.run(coord.async_setup())

    assert RESOLVABLE_SN not in coord.devices
    assert RESOLVABLE_SN in coord.unmapped
    assert http.quota_calls == [], "a BLE-owned serial must not be polled at all"
    assert resolve_calls == [], "a BLE-owned serial must never reach resolve_device"

    # The stale cloud-side registrations that caused the live collision are gone.
    assert sorted(hass.entity_registry.removed) == [
        "sensor.camping_power_station_battery_level",
        "sensor.camping_power_station_connection",
    ]
    assert hass.device_registry.removed == ["dev1"]

    messages = [r.message for r in caplog.records]
    assert any(
        "local Bluetooth" in m and "R621" in m and "2 stale cloud" in m
        for m in messages
    ), f"expected a one-time INFO log naming the prefix and the cleanup count: {messages}"
    # Never the full serial - it stays redacted like every other log line here.
    assert not any(RESOLVABLE_SN in m for m in messages)


def test_cloud_skip_log_omits_cleanup_note_when_nothing_was_stale(caplog):
    """A serial that was BLE-owned from the start has nothing to clean up."""
    coord, _hass, _http = make_coordinator(
        other_entries=[ble_entry(RESOLVABLE_SN)],
        listed=[{"sn": RESOLVABLE_SN, "online": 1}],
    )

    with caplog.at_level(logging.INFO):
        asyncio.run(coord.async_setup())

    messages = [r.message for r in caplog.records if "local Bluetooth" in r.message]
    assert messages and "stale" not in messages[0]


def test_cleanup_never_touches_other_entries_or_serials():
    """Only this cloud entry's own entities for THIS serial are removed -
    a sibling account, or an unrelated serial on the same account, is safe."""
    other_account_entity = FakeEntityEntry(
        "sensor.another_account_device",
        f"{RESOLVABLE_SN}_battery_level",
        "cloud2",  # a different cloud config entry
    )
    different_serial_entity = FakeEntityEntry(
        "sensor.other_device_battery_level",
        "R655OTHER00001234_battery_level",
        "cloud1",
    )
    matching_entity = FakeEntityEntry(
        "sensor.camping_power_station_connection",
        f"{RESOLVABLE_SN}_connection",
        "cloud1",
    )
    coord, hass, _http = make_coordinator(
        other_entries=[ble_entry(RESOLVABLE_SN)],
        listed=[{"sn": RESOLVABLE_SN, "online": 1}],
        entity_entries=[other_account_entity, different_serial_entity, matching_entity],
    )

    asyncio.run(coord.async_setup())

    assert hass.entity_registry.removed == ["sensor.camping_power_station_connection"]
    remaining_ids = {e.entity_id for e in hass.entity_registry.entries}
    assert remaining_ids == {
        "sensor.another_account_device",
        "sensor.other_device_battery_level",
    }


def test_cloud_serves_the_serial_when_no_ble_entry_exists():
    """The same serial, with no BLE entry at all, is served normally."""
    resolve_calls.clear()
    coord, _hass, http = make_coordinator(
        other_entries=[],
        listed=[{"sn": RESOLVABLE_SN, "online": 1}],
    )

    asyncio.run(coord.async_setup())

    assert RESOLVABLE_SN in coord.devices
    assert RESOLVABLE_SN not in coord.unmapped
    assert http.quota_calls == [RESOLVABLE_SN]
    assert resolve_calls == [RESOLVABLE_SN]


def test_cloud_serves_the_serial_when_its_ble_entry_is_disabled():
    """'Configured (not disabled)' - a disabled BLE entry does not claim it."""
    resolve_calls.clear()
    coord, _hass, http = make_coordinator(
        other_entries=[ble_entry(RESOLVABLE_SN, disabled=True)],
        listed=[{"sn": RESOLVABLE_SN, "online": 1}],
    )

    asyncio.run(coord.async_setup())

    assert RESOLVABLE_SN in coord.devices
    assert http.quota_calls == [RESOLVABLE_SN]


def test_cloud_serves_the_serial_when_a_ble_entry_owns_a_different_one():
    """A BLE entry for another device must not shadow this one."""
    resolve_calls.clear()
    coord, _hass, http = make_coordinator(
        other_entries=[ble_entry("R655OTHER00001234")],
        listed=[{"sn": RESOLVABLE_SN, "online": 1}],
    )

    asyncio.run(coord.async_setup())

    assert RESOLVABLE_SN in coord.devices
    assert http.quota_calls == [RESOLVABLE_SN]


def test_ble_entry_removed_schedules_a_cloud_reload():
    """Removing (or disabling) the BLE entry must hand the serial back - a
    reload is how `_is_ble_owned` gets asked again."""
    coord, hass, _http = make_coordinator(other_entries=[], listed=[])

    coord._on_ble_entry_changed("removed", ble_entry(RESOLVABLE_SN))

    assert hass.config_entries.reloaded == ["cloud1"]


def test_ble_entry_added_schedules_a_cloud_reload():
    coord, hass, _http = make_coordinator(other_entries=[], listed=[])

    coord._on_ble_entry_changed("added", ble_entry(RESOLVABLE_SN))

    assert hass.config_entries.reloaded == ["cloud1"]


def test_unrelated_entry_change_does_not_reload():
    """A cloud entry (this domain, but not BLE) or a foreign domain must not
    trigger a reload - only a BLE entry in this domain can change ownership."""
    coord, hass, _http = make_coordinator(other_entries=[], listed=[])

    coord._on_ble_entry_changed("updated", FakeConfigEntry("other_cloud", DOMAIN, {}))
    coord._on_ble_entry_changed(
        "updated", FakeConfigEntry("foreign", "some_other_domain", {})
    )

    assert hass.config_entries.reloaded == []


def test_watch_ble_ownership_registers_for_cleanup():
    """The dispatcher subscription must be tied to the entry's own unload."""
    coord, _hass, _http = make_coordinator(other_entries=[], listed=[])

    coord._watch_ble_ownership()

    assert coord.config_entry.unload_callbacks, "listener was never registered"
