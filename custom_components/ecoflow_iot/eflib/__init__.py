"""
Local BLE protocol library for EcoFlow devices

Vendored subset of `rabits/ha-ef-ble` (Apache-2.0) — see the `NOTICE` file next to
this module for provenance and the list of modifications. Only the Wave 3 and River 3
family closure is vendored; unrelated device modules are deliberately absent.

MODIFICATION vs upstream `eflib/__init__.py`: upstream discovers device classes by
globbing `eflib/devices/*.py` and importing every module at package import time, then
falls back to an `UnsupportedDevice` placeholder for any unknown serial. Both are
replaced here by an explicit registry: the supported device classes are listed in
`SUPPORTED_DEVICE_CLASSES`, an unknown serial resolves to `None`, and advertisement
parsing is exposed as a pure, connection-free `identify()` so the config flow can
classify a discovery without opening a link.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from .devicebase import DeviceBase, ScanRecord
from .devices import river3, river3_plus, wave3
from .entity import controls as controls
from .entity import units as units
from .props.updatable_props import UpdatableProps

if TYPE_CHECKING:
    from .props import ProtobufProps

#: Device classes this integration speaks BLE to, most specific first.
#:
#: Order matters: `river3_plus.Device` subclasses `river3.Device`, and both accept a
#: disjoint set of serial prefixes, but listing the subclass first keeps the resolution
#: correct if a future prefix is ever accepted by both.
SUPPORTED_DEVICE_CLASSES: Final[tuple[type[DeviceBase], ...]] = (
    wave3.Device,
    river3_plus.Device,
    river3.Device,
)

#: Length of the serial number inside the 0xB5B5 manufacturer payload.
_SN_SLICE = slice(1, 17)

#: Session-key derivation schemes seen on EcoFlow hardware. The account id is needed
#: for the auth secret in EVERY case (see `encrypt_type` below), so this is not a
#: credential-requirement switch.
SESSION_KEY_PASSTHROUGH = 0
SESSION_KEY_FROM_SERIAL = 1
SESSION_KEY_FROM_SEED = 7


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    """
    What a BLE advertisement tells us about a supported device, without connecting

    `model` is the display name for the exact SKU; it is resolved from the serial
    prefix and — where a prefix is shared between SKUs — from the advertised local
    name. `advertised_name` is the raw local name from the advertisement, which
    EcoFlow firmware may change (e.g. to "Ecoflow-dev" while provisioning), so it is
    never used for matching.
    """

    serial: str
    model: str
    device_class: type[DeviceBase]
    advertised_name: str | None
    scan_record: ScanRecord

    @property
    def encrypt_type(self) -> int:
        """
        Session-key derivation the device advertises, from the capability flags

        This selects the transport-level session key only: `0` passthrough, `1` derives
        AES key/IV from MD5 of the serial and its reverse, `7` derives one from the
        device's seed/sRand against the static key table.

        It does NOT decide whether EcoFlow account credentials are needed.
        `Connection._auto_authentication` runs for every type and always sends
        MD5(user_id + serial), so a `user_id` is required regardless — passing None
        raises `TypeError` mid-auth. Do not gate the credential step on this.
        """
        return self.scan_record.encrypt_type


def sn_from_advertisement(adv_data: AdvertisementData) -> str | None:
    """
    Serial number carried in the EcoFlow manufacturer data, or None

    MODIFICATION vs upstream: returns `str` instead of `bytes`. Every caller decoded it
    immediately, and a non-ASCII payload is not a serial number we can match on.
    """
    manufacturer_data = getattr(adv_data, "manufacturer_data", None)
    if not manufacturer_data:
        return None

    payload = manufacturer_data.get(DeviceBase.MANUFACTURER_KEY)
    if not payload:
        return None

    try:
        return payload[_SN_SLICE].strip(b"\x00").decode("ASCII")
    except UnicodeDecodeError:
        return None


def device_class_for_serial(serial: str) -> type[DeviceBase] | None:
    """Supported device class whose serial prefix matches `serial`, or None"""
    try:
        serial_bytes = serial.encode("ASCII")
    except UnicodeEncodeError:
        # Not a serial number any EcoFlow prefix can match; callers get None rather
        # than an exception, since this is reachable from a discovery callback.
        return None

    for device_class in SUPPORTED_DEVICE_CLASSES:
        if device_class.check(serial_bytes):
            return device_class
    return None


def identify(adv_data: AdvertisementData) -> DeviceIdentity | None:
    """
    Classify an advertisement without connecting; None when we do not support it

    Used by config flow and discovery. Matching is on the serial prefix from the
    manufacturer payload only — never on the advertised local name.
    """
    if (serial := sn_from_advertisement(adv_data)) is None:
        return None

    if (device_class := device_class_for_serial(serial)) is None:
        return None

    local_name = getattr(adv_data, "local_name", None)
    advertised_name = local_name if isinstance(local_name, str) else None
    return DeviceIdentity(
        serial=serial,
        model=device_class.model_name(serial, advertised_name),
        device_class=device_class,
        advertised_name=advertised_name,
        scan_record=ScanRecord.from_manufacturer_data(
            adv_data.manufacturer_data[DeviceBase.MANUFACTURER_KEY]
        ),
    )


def is_supported(adv_data: AdvertisementData) -> bool:
    """True when this advertisement belongs to a device we speak BLE to"""
    return identify(adv_data) is not None


def new_device(ble_dev: BLEDevice, adv_data: AdvertisementData) -> DeviceBase | None:
    """
    Build the device for this advertisement, or None when it is not supported

    MODIFICATION vs upstream `NewDevice`: unsupported serials return None instead of
    an `UnsupportedDevice` placeholder — this integration already resolves unknown
    EcoFlow hardware through its cloud path, so a BLE placeholder would duplicate it.
    """
    if (identity := identify(adv_data)) is None:
        return None

    return identity.device_class(ble_dev, adv_data, identity.serial)


def get_protobuf_device(device: DeviceBase | None) -> "ProtobufProps | None":
    from .props import ProtobufProps  # noqa: PLC0415

    return device if isinstance(device, ProtobufProps) else None


def get_updatable_prop_device(device: DeviceBase) -> UpdatableProps:
    if not isinstance(device, UpdatableProps):
        raise TypeError("Device has to be subclass of UpdatableProps")

    return device


def get_controls[E: controls.ControlType](
    device: DeviceBase, control_type: type[E]
) -> list[E]:
    """Declared controls of `control_type` on this device, bound to the instance"""
    if not isinstance(device, UpdatableProps):
        return []

    return device.get_controls(control_type)


__all__ = [
    "SUPPORTED_DEVICE_CLASSES",
    "SESSION_KEY_FROM_SEED",
    "SESSION_KEY_FROM_SERIAL",
    "SESSION_KEY_PASSTHROUGH",
    "DeviceBase",
    "DeviceIdentity",
    "ScanRecord",
    "controls",
    "device_class_for_serial",
    "get_controls",
    "get_protobuf_device",
    "get_updatable_prop_device",
    "identify",
    "is_supported",
    "new_device",
    "sn_from_advertisement",
    "units",
]
