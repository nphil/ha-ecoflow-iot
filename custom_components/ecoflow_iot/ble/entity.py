"""Base entity and shared helpers for the EcoFlow BLE platforms.

Every BLE entity is a `CoordinatorEntity` over `EcoFlowBleCoordinator`: values are
read straight off the vendored `eflib` device object, which the coordinator keeps
current from the device's own push notifications. Nothing here polls, and nothing
here writes optimistic state - a command is only reflected once the device reports
the new value back.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Final

from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..const import DOMAIN
from ..eflib import DeviceBase, units
from ..eflib.devices import wave3
from ..eflib.entity import DynamicValue, EntityType
from ..eflib.props import Field
from ..eflib.props.enums import IntFieldValue
from .coordinator import EcoFlowBleCoordinator

_UNIT_CONVERSION: dict[str, str] = {
    units.Power.WATT: UnitOfPower.WATT,
    units.Temperature.C: UnitOfTemperature.CELSIUS,
    units.Temperature.F: UnitOfTemperature.FAHRENHEIT,
    units.Current.AMPERE: UnitOfElectricCurrent.AMPERE,
    units.Voltage.VOLT: UnitOfElectricPotential.VOLT,
}


def hass_unit(unit: str | units.Unit | None) -> str | None:
    """Translate an eflib unit into the matching Home Assistant unit."""
    if unit is None:
        return None
    return _UNIT_CONVERSION.get(unit, unit)


def prop_name(value: Any) -> str | None:
    """Resolve a control's field reference to the device property name."""
    if isinstance(value, Field):
        return value.public_name
    if isinstance(value, DynamicValue):
        return value.field.public_name
    if isinstance(value, str):
        return value
    return None


def enum_option(value: Any) -> Any:
    """Render an eflib enum value as a HA option string.

    A firmware value eflib could not map comes back as `UNKNOWN`; that is reported
    as no state at all rather than as a bogus option.
    """
    if isinstance(value, IntFieldValue):
        if value.name == "UNKNOWN":
            return None
        return value.state_name
    return value


def enum_options(enum_type: type[IntFieldValue]) -> list[str]:
    """Option strings for an eflib enum, without the `UNKNOWN` placeholder."""
    return enum_type.options(include_unknown=False)


def device_temperature_unit(device: DeviceBase) -> str:
    """Unit the device reports its temperatures in.

    Wave 3 reports `user_temp_unit` and formats every temperature field (and the
    values it accepts back) in that unit, so assuming Celsius mis-scales the whole
    climate entity on a device set to Fahrenheit.
    """
    if getattr(device, "temp_unit", None) is wave3.TemperatureUnit.FAHRENHEIT:
        return UnitOfTemperature.FAHRENHEIT
    return UnitOfTemperature.CELSIUS


class EcoFlowBleEntity(CoordinatorEntity[EcoFlowBleCoordinator]):
    """Common base for entities backed by a local BLE link."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EcoFlowBleCoordinator,
        key: str,
        *,
        availability_prop: str | None = None,
    ) -> None:
        """Bind the entity to a device property key."""
        super().__init__(coordinator)
        self._key = key
        self._availability_prop = availability_prop
        self._attr_unique_id = f"{coordinator.serial}_{key}"
        self._attr_device_info = coordinator.device_info

    @property
    def _device(self) -> DeviceBase:
        return self.coordinator.device

    def _value(self, prop: str | None) -> Any:
        """Current value of a device property, or None when absent/unreported."""
        if prop is None:
            return None
        return getattr(self.coordinator.device, prop, None)

    @property
    def available(self) -> bool:
        """Available while the link is up and any gating field allows it."""
        if not self.coordinator.connected:
            return False
        if self._availability_prop is None:
            return True
        return bool(self._value(self._availability_prop))

    async def _async_call(
        self, func: Callable[..., Awaitable[Any]], *args: Any
    ) -> None:
        """Run a device setter through the coordinator's command path.

        The setters return False when the device refuses the value (an out-of-range
        SOC limit, say); that is surfaced as an error instead of being swallowed and
        leaving the UI showing a value the device never accepted.
        """

        async def _call() -> None:
            if await func(self._device, *args) is False:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="command_rejected",
                    translation_placeholders={"name": self._key},
                )

        await self.coordinator.async_command(_call, name=self._key)


# Controls that configure how the device behaves rather than operating it. The house
# rule is that a config-category entity is not created enabled: these stay out of the
# registry until someone asks for them, leaving the dashboard to the actual controls.
CONFIG_CONTROLS: Final[frozenset[str]] = frozenset(
    {
        "battery_charge_limit_min",
        "battery_charge_limit_max",
        "energy_backup_battery_level",
        "ac_charging_speed",
        "dc_charging_max_amps",
        "dc_charging_type",
        "dc_mode",
        "led_mode",
        "temp_unit",
        "lcd_show_temp_type",
    }
)


class EcoFlowBleControlEntity(EcoFlowBleEntity):
    """Entity described by an eflib control descriptor.

    The control already carries the device knowledge - which field it reads, what
    gates it, whether it is worth enabling - so it doubles as the entity
    description and nothing is restated here.
    """

    def __init__(
        self,
        coordinator: EcoFlowBleCoordinator,
        control: EntityType,
    ) -> None:
        """Build the entity from a control descriptor."""
        super().__init__(
            coordinator,
            control.key,
            availability_prop=prop_name(control.availability),
        )
        self._control = control
        self._attr_translation_key = control.translation_key or control.key
        if control.translation_placeholders is not None:
            self._attr_translation_placeholders = dict(control.translation_placeholders)

        enabled = control.enabled
        if control.key in CONFIG_CONTROLS:
            self._attr_entity_category = EntityCategory.CONFIG
            enabled = False
        self._attr_entity_registry_enabled_default = enabled
