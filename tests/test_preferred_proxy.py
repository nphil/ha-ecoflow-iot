"""Plain-script proof of the EcoFlow preferred-proxy affinity contract."""

import logging
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys

_ROOT = Path(__file__).resolve().parents[1]

# Import only the vendored module, not the integration package initializer or HA.
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


from custom_components.ecoflow_iot.ble import link_health  # noqa: E402
from custom_components.ecoflow_iot.ble_affinity import (  # noqa: E402
    DEFAULT_HALF_OPEN_COOLDOWN,
    make_affinity_client_class,
)
from custom_components.ecoflow_iot.const import CONF_PREFERRED_PROXY  # noqa: E402

ADDRESS = "AA:BB:CC:DD:EE:FF"
PREFERRED = "plant-room-bluetooth-proxy"
DEFAULT_BACKEND = SimpleNamespace(scanner=SimpleNamespace(name="default-scanner"))


class FakeConnector:
    def __init__(self, connectable: bool = True) -> None:
        self._connectable = connectable

    def can_connect(self) -> bool:
        return self._connectable


class FakeScanner:
    def __init__(
        self,
        adapter: str,
        *,
        failures: int = 0,
        connectable: bool = True,
    ) -> None:
        self.adapter = adapter
        self.source = f"{adapter}-source"
        self.name = f"{adapter} (source)"
        self.connectable = connectable
        self.connector = FakeConnector(connectable)
        self._failures = failures

    def connection_failures(self, address: str) -> int:
        assert address == ADDRESS
        return self._failures


class FakeScannerDevice:
    def __init__(self, scanner: FakeScanner) -> None:
        self.scanner = scanner
        self.ble_device = SimpleNamespace(address=ADDRESS)
        self.advertisement = SimpleNamespace(rssi=-42)


class FakeManager:
    def __init__(self, scanner_devices: list[FakeScannerDevice]) -> None:
        self._scanner_devices = scanner_devices

    def async_scanner_devices_by_address(
        self, address: str, connectable: bool
    ) -> list[FakeScannerDevice]:
        assert address == ADDRESS
        assert connectable is True
        return self._scanner_devices


class FakeBase:
    def __init__(self) -> None:
        # This is the private attribute used by HA's wrapper before connect().
        self._HaBleakClientWrapper__address = ADDRESS

    def _async_get_best_available_backend_and_device(
        self, _manager: FakeManager
    ) -> SimpleNamespace:
        return DEFAULT_BACKEND

    def _async_get_backend_for_ble_device(
        self,
        _manager: FakeManager,
        scanner: FakeScanner,
        ble_device: SimpleNamespace,
    ) -> SimpleNamespace:
        return SimpleNamespace(scanner=scanner, ble_device=ble_device)


class DefaultRoutingBase:
    """Fakes habluetooth's own default scorer: best RSSI wins, no memory of
    which scanner previously dropped this same link seconds after accepting
    it - that memory is exactly what `is_penalised` adds back.
    """

    def __init__(self) -> None:
        self._HaBleakClientWrapper__address = ADDRESS

    def _async_get_best_available_backend_and_device(
        self, manager: FakeManager
    ) -> SimpleNamespace:
        devices = sorted(
            manager.async_scanner_devices_by_address(ADDRESS, True),
            key=lambda device: device.advertisement.rssi,
            reverse=True,
        )
        best = devices[0]
        return SimpleNamespace(scanner=best.scanner, ble_device=best.ble_device)

    def _async_get_backend_for_ble_device(
        self,
        _manager: FakeManager,
        scanner: FakeScanner,
        ble_device: SimpleNamespace,
    ) -> SimpleNamespace:
        return SimpleNamespace(scanner=scanner, ble_device=ble_device)


def select(
    preferred: str, scanner_devices: list[FakeScannerDevice]
) -> tuple[SimpleNamespace, list[tuple[str, bool]]]:
    entry = SimpleNamespace(options={CONF_PREFERRED_PROXY: preferred})
    choices: list[tuple[str, bool]] = []
    client_class = make_affinity_client_class(
        FakeBase,
        lambda: entry.options.get(CONF_PREFERRED_PROXY) or None,
        on_choice=lambda name, used: choices.append((name, used)),
    )
    backend = client_class()._async_get_best_available_backend_and_device(
        FakeManager(scanner_devices)
    )
    return backend, choices


def test_preferred_scanner_present_and_connectable_is_chosen() -> None:
    preferred_scanner = FakeScanner(PREFERRED)
    backend, choices = select(
        PREFERRED,
        [
            FakeScannerDevice(FakeScanner("master-bedroom-bluetooth-proxy")),
            FakeScannerDevice(preferred_scanner),
        ],
    )
    assert backend.scanner is preferred_scanner
    assert choices == [(preferred_scanner.name, True)]


def test_absent_preferred_scanner_uses_default_selection() -> None:
    backend, choices = select(
        PREFERRED,
        [FakeScannerDevice(FakeScanner("master-bedroom-bluetooth-proxy"))],
    )
    assert backend is DEFAULT_BACKEND
    assert choices == [("default-scanner", False)]


def test_preferred_scanner_after_three_failures_uses_default_selection() -> None:
    backend, choices = select(
        PREFERRED,
        [FakeScannerDevice(FakeScanner(PREFERRED, failures=3))],
    )
    assert backend is DEFAULT_BACKEND
    assert choices == [("default-scanner", False)]


def test_short_lived_links_steer_default_routing_away_from_the_bad_proxy() -> None:
    """habluetooth clears a scanner's failure count on every successful
    connect, so a proxy that authenticates and then drops the link seconds
    later keeps winning on RSSI alone forever - live evidence: PoE proxy
    chosen on 5 consecutive attempts, each ended by a supervision timeout
    ~5s after auth. Once a scanner has done that twice in a row, default
    routing must avoid it; a stable link through it clears the penalty.
    """
    close = FakeScanner("close-bluetooth-proxy")
    far = FakeScanner("far-bluetooth-proxy")
    close_device = FakeScannerDevice(close)
    close_device.advertisement = SimpleNamespace(rssi=-50)
    far_device = FakeScannerDevice(far)
    far_device.advertisement = SimpleNamespace(rssi=-70)

    store: dict = {}
    now = [1000.0]

    def is_penalised(scanner: FakeScanner) -> bool:
        return link_health.is_penalised(store, ADDRESS, scanner.source, now=now[0])

    client_class = make_affinity_client_class(
        DefaultRoutingBase, lambda: None, is_penalised=is_penalised
    )

    def select_once() -> SimpleNamespace:
        return client_class()._async_get_best_available_backend_and_device(
            FakeManager([close_device, far_device])
        )

    chosen: list[str] = []
    for _ in range(5):
        backend = select_once()
        chosen.append(backend.scanner.name)
        if backend.scanner is close:
            link_health.record_short_link(store, ADDRESS, close.source, now=now[0])
        else:
            link_health.record_stable_link(store, ADDRESS, far.source)
        now[0] += 10.0

    assert chosen == [close.name, close.name, far.name, far.name, far.name]

    # A stable link through the previously-bad proxy clears its penalty.
    link_health.record_stable_link(store, ADDRESS, close.source)
    assert select_once().scanner is close


def test_preferred_proxy_gets_one_half_open_trial_after_its_cooldown() -> None:
    """A preferred proxy stuck at max_failures must not be skipped forever.

    habluetooth's failure counter never decays and is not reset by a reload,
    so without a half-open retry a proxy that failed 3 times stays skipped
    until a connect happens to route through it again by chance - which the
    affinity wrapper alone never causes.
    """
    scanner = FakeScanner(PREFERRED, failures=3)
    now = [1000.0]
    entry = SimpleNamespace(options={CONF_PREFERRED_PROXY: PREFERRED})
    suspensions: list[tuple[str, bool]] = []
    client_class = make_affinity_client_class(
        FakeBase,
        lambda: entry.options.get(CONF_PREFERRED_PROXY) or None,
        now=lambda: now[0],
        on_suspension_change=lambda name, suspended: suspensions.append(
            (name, suspended)
        ),
    )

    def select_once() -> SimpleNamespace:
        return client_class()._async_get_best_available_backend_and_device(
            FakeManager([FakeScannerDevice(scanner)])
        )

    # Newly discovered as stuck at max_failures: no immediate retry.
    assert select_once() is DEFAULT_BACKEND
    assert suspensions == [(scanner.name, True)]

    # Still inside the cooldown: keep using the default.
    now[0] += DEFAULT_HALF_OPEN_COOLDOWN - 1
    assert select_once() is DEFAULT_BACKEND

    # Cooldown elapsed: exactly one attempt through the preferred proxy.
    now[0] += 2
    backend = select_once()
    assert backend.scanner is scanner

    # Immediately after that trial, back to the default until the next cooldown.
    assert select_once() is DEFAULT_BACKEND
    assert suspensions == [(scanner.name, True)]

    # The trial succeeded (habluetooth clears the count): preferred again, and
    # the suspension is reported over.
    scanner._failures = 0
    now[0] += 5
    assert select_once().scanner is scanner
    assert suspensions[-1] == (scanner.name, False)

    # Much later it fails 3 times again. Its cooldown must start afresh - no
    # immediate trial carried over from the old, already-doubled schedule.
    scanner._failures = 3
    now[0] += 10 * DEFAULT_HALF_OPEN_COOLDOWN
    assert select_once() is DEFAULT_BACKEND
    now[0] += DEFAULT_HALF_OPEN_COOLDOWN + 1
    assert select_once().scanner is scanner


def _two_proxies() -> tuple[FakeScanner, FakeScanner, list[FakeScannerDevice]]:
    near = FakeScanner(PREFERRED)
    far = FakeScanner("master-bedroom-bluetooth-proxy")
    near_device, far_device = FakeScannerDevice(near), FakeScannerDevice(far)
    near_device.advertisement = SimpleNamespace(rssi=-45)
    far_device.advertisement = SimpleNamespace(rssi=-70)
    return near, far, [near_device, far_device]


def test_a_failed_attempt_steers_the_next_one_to_another_proxy_for_a_while() -> None:
    """An attempt that connected and then stalled (a notification subscribe that
    never answered) is a *success* to habluetooth, which clears the scanner's
    failure count at once - so the preferred proxy, healthy by its books, would
    be chosen again straight away. The caller's own record of the failed
    attempt is what sends the next one elsewhere, and what forgives it again.
    """
    near, far, devices = _two_proxies()
    store: dict = {}
    now = [1000.0]

    def penalty(scanner: FakeScanner) -> str | bool:
        return link_health.penalty(store, ADDRESS, scanner.source, now=now[0]) or False

    client_class = make_affinity_client_class(
        DefaultRoutingBase, lambda: PREFERRED, is_penalised=penalty
    )

    def select_once(scanner_devices=devices) -> SimpleNamespace:
        return client_class()._async_get_best_available_backend_and_device(
            FakeManager(scanner_devices)
        )

    assert select_once().scanner is near

    link_health.record_failed_attempt(store, ADDRESS, near.source, now=now[0])
    assert select_once().scanner is far, "preferred, free and healthy - but it just stalled"
    assert link_health.penalised_sources(store, ADDRESS, now=now[0]) == [near.source]

    # A moment later it is eligible again: the penalty is for the next
    # attempts, not a verdict on the proxy beside the device.
    now[0] += link_health.FAILED_ATTEMPT_AVOID_SECONDS + 1
    assert select_once().scanner is near
    assert link_health.penalised_sources(store, ADDRESS, now=now[0]) == []

    # A link that came up through it clears the record at once.
    link_health.record_failed_attempt(store, ADDRESS, near.source, now=now[0])
    link_health.clear_failed_attempt(store, ADDRESS, near.source)
    assert select_once().scanner is near

    # With nowhere else to go it is still used, never skipped into a dead end.
    link_health.record_failed_attempt(store, ADDRESS, near.source, now=now[0])
    assert select_once([devices[0]]).scanner is near


def test_the_skip_is_logged_with_its_real_reason() -> None:
    near, _far, devices = _two_proxies()
    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    handler = Capture(level=logging.INFO)
    logger = logging.getLogger("custom_components.ecoflow_iot.ble_affinity")
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.INFO)
    try:
        for reason, wording in (
            (link_health.FAILED_ATTEMPT_REASON, "stalled or failed its last"),
            (True, "has dropped this link too soon too often recently"),
        ):
            records.clear()
            client_class = make_affinity_client_class(
                DefaultRoutingBase, lambda: PREFERRED, is_penalised=lambda s, r=reason: r if s is near else False
            )
            client_class()._async_get_best_available_backend_and_device(
                FakeManager(devices)
            )
            assert any(
                "preferred proxy" in message and wording in message
                for message in records
            ), (reason, records)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


# ------------------------------------------------------------ local adapter slots ---
#
# habluetooth reserves a connection slot on a *local* adapter the moment it builds a
# backend for it - long before anything connects - and takes it back only when that
# backend's connect fails (`HaBleakClientWrapper.connect`) or when the BlueZ device
# disconnects (`BleakSlotManager`'s watcher). A backend that is built and then
# discarded gets neither. These fakes keep that accounting; the same scenarios were
# also run against the installed habluetooth 6.26.11 wrapper and the real
# `BleakSlotManager` when this was fixed.


class SlotAwareRouting:
    """habluetooth's default routing as it treats local adapters versus proxies.

    Mirrors `HaBleakClientWrapper._async_get_backend_for_ble_device` and
    `_async_get_best_available_backend_and_device`: a BLEDevice without a
    ``source`` belongs to a local adapter, and building its backend reserves a slot
    through `manager.async_allocate_connection_slot` (``None`` when that adapter is
    full); one with a ``source`` belongs to a proxy, whose backend is built only if
    its connector can take a connection and reserves nothing. Best RSSI wins.
    """

    def __init__(self, address: str = ADDRESS) -> None:
        self._HaBleakClientWrapper__address = address

    def _async_get_backend_for_ble_device(self, manager, scanner, ble_device):
        source = ble_device.details.get("source")
        if not source:
            if not manager.async_allocate_connection_slot(ble_device):
                return None
        elif not scanner.connector.can_connect():
            return None
        return SimpleNamespace(device=ble_device, scanner=scanner, source=source)

    def _async_get_best_available_backend_and_device(self, manager):
        address = self._HaBleakClientWrapper__address
        for entry in sorted(
            manager.async_scanner_devices_by_address(address, True),
            key=lambda entry: entry.advertisement.rssi,
            reverse=True,
        ):
            if backend := self._async_get_backend_for_ble_device(
                manager, entry.scanner, entry.ble_device
            ):
                return backend
        raise RuntimeError("no backend with a free connection slot")


class SlotManager:
    """The scanner devices per address, plus `BleakSlotManager`'s allocate/release."""

    def __init__(self, devices_for, capacity: dict[str, int]) -> None:
        self._devices_for = devices_for
        self.capacity = capacity
        self.reserved: dict[str, set[str]] = {adapter: set() for adapter in capacity}

    def async_scanner_devices_by_address(self, address, connectable):
        return self._devices_for(address)

    def async_allocate_connection_slot(self, device) -> bool:
        adapter = device.details["adapter"]
        if device.address in self.reserved[adapter]:
            return True
        if len(self.reserved[adapter]) >= self.capacity[adapter]:
            return False
        self.reserved[adapter].add(device.address)
        return True

    def async_release_connection_slot(self, device) -> None:
        self.reserved[device.details["adapter"]].discard(device.address)


def seen_by(
    scanner: FakeScanner, rssi: int, *, address: str = ADDRESS, adapter: str | None = None
) -> FakeScannerDevice:
    """How `scanner` sees the device: a local adapter's BLEDevice carries no source, a proxy's does."""
    entry = FakeScannerDevice(scanner)
    entry.ble_device = SimpleNamespace(
        address=address,
        details={"adapter": adapter} if adapter else {"source": scanner.source},
    )
    entry.advertisement = SimpleNamespace(rssi=rssi)
    return entry


def failed_last_attempt(*scanners: FakeScanner):
    """An `is_penalised` hook that reports these scanners as having just failed."""
    return lambda scanner: (
        link_health.FAILED_ATTEMPT_REASON if scanner in scanners else False
    )


def local_and_proxy(local_slots: int, **proxy_options):
    local = FakeScanner("hci0")
    proxy = FakeScanner("office-proxy", **proxy_options)
    manager = SlotManager(
        lambda address: [
            seen_by(local, -40, address=address, adapter="hci0"),
            seen_by(proxy, -60, address=address),
        ],
        {"hci0": local_slots},
    )
    return local, proxy, manager


def test_replacing_a_local_default_backend_gives_its_slot_back() -> None:
    """The local adapter is the default pick, but its last attempt failed, so the
    proxy is used instead.

    The adapter's slot was reserved when the discarded backend was built. Nothing
    will ever connect with that backend, and the adapter's BlueZ watcher only acts
    when a *connected* device disconnects, so unless it is handed back the adapter
    stays one slot short for every device it serves - and its allocation list, which
    the Connection sensor reads, names it as holding this device while the link is
    really on the proxy.
    """
    local, proxy, manager = local_and_proxy(local_slots=3)
    client_class = make_affinity_client_class(
        SlotAwareRouting, lambda: None, is_penalised=failed_last_attempt(local)
    )

    backend = client_class()._async_get_best_available_backend_and_device(manager)

    assert backend.scanner is proxy
    assert manager.reserved["hci0"] == set()


def test_a_device_retried_away_from_the_adapter_does_not_use_up_its_last_slot() -> None:
    """One slot, one device that had to move to a proxy: the next device that is
    perfectly happy on the adapter must still be able to have it."""
    local, proxy, manager = local_and_proxy(local_slots=1)
    retried = make_affinity_client_class(
        SlotAwareRouting, lambda: None, is_penalised=failed_last_attempt(local)
    )
    healthy = make_affinity_client_class(SlotAwareRouting, lambda: None)

    first = retried("AA:BB:CC:DD:EE:01")
    assert first._async_get_best_available_backend_and_device(manager).scanner is proxy
    second = healthy("AA:BB:CC:DD:EE:02")
    assert second._async_get_best_available_backend_and_device(manager).scanner is local


def test_a_penalised_default_with_nothing_to_replace_it_keeps_its_slot() -> None:
    """Steering away must never strand a device: when no other path can take it, the
    penalised local default is used after all, and the slot it holds is its own."""
    local, _proxy, manager = local_and_proxy(local_slots=3, connectable=False)
    client_class = make_affinity_client_class(
        SlotAwareRouting, lambda: None, is_penalised=failed_last_attempt(local)
    )

    backend = client_class()._async_get_best_available_backend_and_device(manager)

    assert backend.scanner is local
    assert manager.reserved["hci0"] == {ADDRESS}


def test_a_replacement_that_cannot_be_built_leaves_the_default_its_slot() -> None:
    """The replacement looked usable, but its own adapter turns out to be full: the
    default stays in use, so the slot reserved for it must not have been given away."""
    first, second = FakeScanner("hci0"), FakeScanner("hci1")
    manager = SlotManager(
        lambda address: [
            seen_by(first, -40, address=address, adapter="hci0"),
            seen_by(second, -60, address=address, adapter="hci1"),
        ],
        {"hci0": 3, "hci1": 0},
    )
    client_class = make_affinity_client_class(
        SlotAwareRouting, lambda: None, is_penalised=failed_last_attempt(first)
    )

    backend = client_class()._async_get_best_available_backend_and_device(manager)

    assert backend.scanner is first
    assert manager.reserved["hci0"] == {ADDRESS}


def test_routing_survives_a_slot_release_that_cannot_be_done() -> None:
    """Handing a slot back is housekeeping: whatever the manager does, the connect
    still goes through the replacement."""

    class Raises(SlotManager):
        def async_release_connection_slot(self, device) -> None:
            raise RuntimeError("the slot manager is gone")

    class Lacks(SlotManager):
        async_release_connection_slot = None  # a habluetooth without the method

    for manager_class in (Raises, Lacks):
        local = FakeScanner("hci0")
        proxy = FakeScanner("office-proxy")
        manager = manager_class(
            lambda address: [
                seen_by(local, -40, address=address, adapter="hci0"),
                seen_by(proxy, -60, address=address),
            ],
            {"hci0": 3},
        )
        client_class = make_affinity_client_class(
            SlotAwareRouting, lambda: None, is_penalised=failed_last_attempt(local)
        )

        backend = client_class()._async_get_best_available_backend_and_device(manager)

        assert backend.scanner is proxy, manager_class.__name__


def main() -> None:
    tests = (
        test_preferred_scanner_present_and_connectable_is_chosen,
        test_absent_preferred_scanner_uses_default_selection,
        test_preferred_scanner_after_three_failures_uses_default_selection,
        test_short_lived_links_steer_default_routing_away_from_the_bad_proxy,
        test_preferred_proxy_gets_one_half_open_trial_after_its_cooldown,
        test_a_failed_attempt_steers_the_next_one_to_another_proxy_for_a_while,
        test_the_skip_is_logged_with_its_real_reason,
        test_replacing_a_local_default_backend_gives_its_slot_back,
        test_a_device_retried_away_from_the_adapter_does_not_use_up_its_last_slot,
        test_a_penalised_default_with_nothing_to_replace_it_keeps_its_slot,
        test_a_replacement_that_cannot_be_built_leaves_the_default_its_slot,
        test_routing_survives_a_slot_release_that_cannot_be_done,
    )
    for test in tests:
        test()
    print(f"{len(tests)}/{len(tests)} preferred-proxy tests passed")


if __name__ == "__main__":
    main()
