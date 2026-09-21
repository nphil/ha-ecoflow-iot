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

from custom_components.ecoflow_iot.ble_affinity import (  # noqa: E402
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


def main() -> None:
    tests = (
        test_preferred_scanner_present_and_connectable_is_chosen,
        test_absent_preferred_scanner_uses_default_selection,
        test_preferred_scanner_after_three_failures_uses_default_selection,
    )
    for test in tests:
        test()
    print(f"{len(tests)}/{len(tests)} preferred-proxy tests passed")


if __name__ == "__main__":
    main()
