"""Plain-script proof of the EcoFlow preferred-proxy affinity contract."""

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


def main() -> None:
    tests = (
        test_preferred_scanner_present_and_connectable_is_chosen,
        test_absent_preferred_scanner_uses_default_selection,
        test_preferred_scanner_after_three_failures_uses_default_selection,
        test_short_lived_links_steer_default_routing_away_from_the_bad_proxy,
        test_preferred_proxy_gets_one_half_open_trial_after_its_cooldown,
    )
    for test in tests:
        test()
    print(f"{len(tests)}/{len(tests)} preferred-proxy tests passed")


if __name__ == "__main__":
    main()
