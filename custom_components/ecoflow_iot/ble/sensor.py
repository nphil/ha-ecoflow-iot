"""Sensors for locally connected (BLE) EcoFlow devices.

Rows are keyed by the property name the vendored `eflib` device class exposes, so a
row only becomes an entity on models that actually declare that field. A field the
device never reports stays `None` and the entity reads as unknown - it is never
faked as zero.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Final, TypedDict, Unpack

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from ..eflib import DeviceBase
from ..eflib.devices import wave3
from ..eflib.props.enums import IntFieldValue
from . import EcoFlowBleConfigEntry
from .coordinator import EcoFlowBleCoordinator
from .entity import (
    EcoFlowBleEntity,
    device_temperature_unit,
    enum_option,
    enum_options,
)


@dataclass(frozen=True, kw_only=True)
class EcoFlowBleSensorEntityDescription(SensorEntityDescription):
    """Sensor description bound to an eflib device property."""

    unit_from_device: Callable[[DeviceBase], str] | None = None
    state_attribute_props: tuple[str, ...] = ()


class _Kwargs(TypedDict, total=False):
    entity_category: EntityCategory
    state_attribute_props: tuple[str, ...]


def _battery(*, enabled: bool = True, **kwargs: Unpack[_Kwargs]):
    return EcoFlowBleSensorEntityDescription(
        key="",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=enabled,
        **kwargs,
    )


def _power(*, precision: int = 0, enabled: bool = True, **kwargs: Unpack[_Kwargs]):
    return EcoFlowBleSensorEntityDescription(
        key="",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=precision,
        entity_registry_enabled_default=enabled,
        **kwargs,
    )


def _energy(*, enabled: bool = True, **kwargs: Unpack[_Kwargs]):
    """Cumulative counter reported by the device in whole watt-hours."""
    return EcoFlowBleSensorEntityDescription(
        key="",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_registry_enabled_default=enabled,
        **kwargs,
    )


def _temperature(*, enabled: bool = True, **kwargs: Unpack[_Kwargs]):
    return EcoFlowBleSensorEntityDescription(
        key="",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=enabled,
        **kwargs,
    )


def _device_temperature(*, enabled: bool = True, **kwargs: Unpack[_Kwargs]):
    """Temperature the device reports in whatever unit it is itself set to.

    The native unit follows `user_temp_unit`, which the user can flip. That is safe
    for history: for a temperature device class HA converts the native reading into
    the entity's registry unit, which is fixed when the entity is first added, so the
    recorded unit never changes and no value is rounded through an intermediate unit.
    """
    return EcoFlowBleSensorEntityDescription(
        key="",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        unit_from_device=device_temperature_unit,
        suggested_display_precision=1,
        entity_registry_enabled_default=enabled,
        **kwargs,
    )


def _humidity(*, enabled: bool = True, **kwargs: Unpack[_Kwargs]):
    return EcoFlowBleSensorEntityDescription(
        key="",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=enabled,
        **kwargs,
    )


def _percentage(*, enabled: bool = True, **kwargs: Unpack[_Kwargs]):
    return EcoFlowBleSensorEntityDescription(
        key="",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=enabled,
        **kwargs,
    )


def _duration(*, enabled: bool = True, **kwargs: Unpack[_Kwargs]):
    return EcoFlowBleSensorEntityDescription(
        key="",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_unit_of_measurement=UnitOfTime.HOURS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=enabled,
        **kwargs,
    )


def _enum(
    options: type[IntFieldValue], *, enabled: bool = True, **kwargs: Unpack[_Kwargs]
):
    return EcoFlowBleSensorEntityDescription(
        key="",
        device_class=SensorDeviceClass.ENUM,
        options=enum_options(options),
        entity_registry_enabled_default=enabled,
        **kwargs,
    )


def _raw(
    *,
    enabled: bool = True,
    state_class: SensorStateClass | None = None,
    **kwargs: Unpack[_Kwargs],
):
    return EcoFlowBleSensorEntityDescription(
        key="",
        state_class=state_class,
        entity_registry_enabled_default=enabled,
        **kwargs,
    )


_DIAGNOSTIC: Final = EntityCategory.DIAGNOSTIC

# Keyed by eflib device property name. Writable fields (charge limits, charging
# speed, ports, climate) belong to the control platforms and are absent here.
_SENSORS: Final[dict[str, EcoFlowBleSensorEntityDescription]] = {
    # Shared
    "battery_level": _battery(),
    "cell_temperature": _temperature(enabled=False, entity_category=_DIAGNOSTIC),
    "input_power": _power(),
    "output_power": _power(),
    "ac_input_power": _power(precision=1),
    # Wave 3
    "ambient_temperature": _device_temperature(),
    "ambient_humidity": _humidity(),
    "temp_indoor_supply_air": _device_temperature(),
    "temp_indoor_return_air": _device_temperature(),
    "temp_outdoor_ambient": _device_temperature(),
    "temp_condenser": _device_temperature(enabled=False, entity_category=_DIAGNOSTIC),
    "temp_evaporator": _device_temperature(enabled=False, entity_category=_DIAGNOSTIC),
    "temp_compressor_discharge": _device_temperature(
        enabled=False, entity_category=_DIAGNOSTIC
    ),
    "condensate_water_level": _percentage(),
    "battery_power": _power(precision=1),
    "pcs_fan_level": _raw(
        enabled=False,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=_DIAGNOSTIC,
    ),
    "drainage_mode": _raw(enabled=False, entity_category=_DIAGNOSTIC),
    "sleep_state": _enum(wave3.SleepState, enabled=False, entity_category=_DIAGNOSTIC),
    # temp_unit is writable, so it lives on the select platform, not here
    # River 3 family
    "battery_level_main": _battery(),
    "ac_output_power": _power(precision=1),
    "dc_input_power": _power(precision=1),
    "dc12v_output_power": _power(precision=1),
    "usbc_output_power": _power(),
    "usba_output_power": _power(),
    "battery_input_power": _power(enabled=False),
    "battery_output_power": _power(enabled=False),
    "max_ac_charging_power": _power(enabled=False, entity_category=_DIAGNOSTIC),
    "remaining_time_charging": _duration(enabled=False),
    "remaining_time_discharging": _duration(enabled=False),
    "input_energy": _energy(),
    "output_energy": _energy(),
    "ac_input_energy": _energy(),
    "ac_output_energy": _energy(),
    "dc_input_energy": _energy(),
    "dc12v_output_energy": _energy(),
    "usbc_output_energy": _energy(),
    "usba_output_energy": _energy(),
    # River 2 (Pro): DC port input power split by charging source. Mutually
    # exclusive with each other (whichever source is inactive reports 0, not
    # unknown) - the raw pre-split `dc_port_input_power` feeding both is internal
    # and not surfaced, to avoid a third sensor describing the same physical input.
    "solar_input_power": _power(),
    "car_input_power": _power(),
    # River 3 Plus / Pro add-on battery
    "battery_1_battery_level": _battery(enabled=False),
    "battery_1_cell_temperature": _temperature(
        enabled=False, entity_category=_DIAGNOSTIC
    ),
    "battery_1_sn": _raw(enabled=False, entity_category=_DIAGNOSTIC),
}

# Ports only some SKUs in a family populate. River 3 Plus and Pro share a device
# class, but the second USB-A/USB-C, the second solar input and the 24V output are
# only ever reported by hardware that has them - a device that never sends the
# field gets no entity rather than one stuck at unknown.
_OPTIONAL_SENSORS: Final[dict[str, EcoFlowBleSensorEntityDescription]] = {
    "usbc2_output_power": _power(),
    "usba2_output_power": _power(),
    "dc2_input_power": _power(precision=1),
    "dc24v_output_power": _power(precision=1),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EcoFlowBleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors for a locally connected EcoFlow device."""
    coordinator = entry.runtime_data
    device = coordinator.device

    entities: list[SensorEntity] = [EcoFlowBleLinkSensor(coordinator)]
    entities.extend(
        EcoFlowBleSensor(coordinator, key, description)
        for key, description in _SENSORS.items()
        if hasattr(device, key)
    )
    async_add_entities(entities)

    pending = {
        key: description
        for key, description in _OPTIONAL_SENSORS.items()
        if hasattr(device, key)
    }
    if not pending:
        return

    remove_listener: Callable[[], None] | None = None

    @callback
    def _stop_watching() -> None:
        """Drop the listener once, whether we finished or the entry unloaded."""
        nonlocal remove_listener
        if remove_listener is not None:
            remove_listener()
            remove_listener = None

    @callback
    def _add_reported() -> None:
        """Add optional port sensors once the device first reports them."""
        reported = [key for key in pending if getattr(device, key, None) is not None]
        if not reported:
            return
        # claim the keys before handing the entities over: the platform may add them
        # asynchronously, and a second frame in between would duplicate them
        added = [
            EcoFlowBleSensor(coordinator, key, pending.pop(key)) for key in reported
        ]
        async_add_entities(added)
        if not pending:
            _stop_watching()

    remove_listener = coordinator.async_add_listener(_add_reported)
    entry.async_on_unload(_stop_watching)
    _add_reported()


class EcoFlowBleSensor(EcoFlowBleEntity, SensorEntity):
    """A value the device reports over its local link."""

    entity_description: EcoFlowBleSensorEntityDescription

    def __init__(
        self,
        coordinator: EcoFlowBleCoordinator,
        key: str,
        description: EcoFlowBleSensorEntityDescription,
    ) -> None:
        """Bind the sensor to the device property named by `key`."""
        super().__init__(coordinator, key)
        self.entity_description = replace(description, key=key)
        self._attr_translation_key = key

    @property
    def native_value(self) -> Any:
        """Latest reported value, or None while the device has not sent one."""
        return enum_option(self._value(self._key))

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Unit, honouring the device's own temperature-unit setting."""
        if (unit_of := self.entity_description.unit_from_device) is not None:
            return unit_of(self._device)
        return self.entity_description.native_unit_of_measurement

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Supporting values the sensor carries alongside its state."""
        props = self.entity_description.state_attribute_props
        if not props:
            return None
        return {prop: self._value(prop) for prop in props}


class EcoFlowBleLinkSensor(EcoFlowBleEntity, SensorEntity):
    """State of the BLE link itself: which proxy holds it, or `disconnected`.

    Stays available while the device is unreachable - the whole point of the entity
    is to report that, and the household heal script keys off it.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "ble_connection"

    def __init__(self, coordinator: EcoFlowBleCoordinator) -> None:
        """Create the link sensor for this device's config entry."""
        super().__init__(coordinator, "connection")

    @property
    def available(self) -> bool:
        """Always available; the state carries the connection status."""
        return True

    @property
    def native_value(self) -> str:
        """Name of the proxy holding the link, or `disconnected`."""
        return self.coordinator.link_state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Link health detail: hold, drop history and reconnect progress."""
        return self.coordinator.link_attributes
