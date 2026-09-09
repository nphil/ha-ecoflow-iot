"""Numbers for locally connected (BLE) EcoFlow devices.

Rows come straight from the `@controls.<number>` descriptors on the vendored device
classes, which already carry the unit, the step and the (often dynamic) bounds.
"""

from __future__ import annotations

from typing import Final

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
)
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from ..eflib import controls, get_controls
from ..eflib.entity import DynamicValue
from . import EcoFlowBleConfigEntry
from .coordinator import EcoFlowBleCoordinator
from .entity import EcoFlowBleControlEntity, hass_unit

_DEVICE_CLASSES: Final[dict[type[controls.NumberType], NumberDeviceClass]] = {
    controls.power: NumberDeviceClass.POWER,
    controls.battery: NumberDeviceClass.BATTERY,
    controls.current: NumberDeviceClass.CURRENT,
    controls.temperature: NumberDeviceClass.TEMPERATURE,
}

# Used only while a dynamic bound has not been reported yet; the entity keeps its
# own state inside the range so HA does not render an out-of-range slider.
_DEFAULT_MIN: Final = 0.0
_DEFAULT_MAX: Final = 100.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EcoFlowBleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up numbers for a locally connected EcoFlow device."""
    coordinator = entry.runtime_data
    async_add_entities(
        EcoFlowBleNumber(coordinator, control)
        for control in get_controls(coordinator.device, controls.NumberType)
    )


class EcoFlowBleNumber(EcoFlowBleControlEntity, NumberEntity):
    """A numeric device setting written over the local link."""

    def __init__(
        self,
        coordinator: EcoFlowBleCoordinator,
        control: controls.NumberType,
    ) -> None:
        """Build the number from its control descriptor."""
        super().__init__(coordinator, control)
        self._control: controls.NumberType = control
        self._set_value = control.set_value_func
        self._attr_device_class = _DEVICE_CLASSES.get(type(control))
        self._attr_native_step = control.step
        self._attr_native_unit_of_measurement = (
            PERCENTAGE
            if isinstance(control, controls.battery)
            else hass_unit(getattr(control, "unit", None))
        )

    def _resolve(self, bound: float | DynamicValue | None) -> float | None:
        """Resolve a bound that may track another device field."""
        if isinstance(bound, DynamicValue):
            resolved = bound.resolve(self._device)
            return None if resolved is None else float(resolved)
        return None if bound is None else float(bound)

    @property
    def native_value(self) -> float | None:
        """Current setting, or None while the device has not reported it."""
        value = self._value(self._key)
        return None if value is None else float(value)

    @property
    def native_min_value(self) -> float:
        """Lower bound, tracking the sibling field when the model ties them."""
        resolved = self._resolve(self._control.min)
        if resolved is not None:
            return resolved
        current = self.native_value
        return min(current, _DEFAULT_MIN) if current is not None else _DEFAULT_MIN

    @property
    def native_max_value(self) -> float:
        """Upper bound, tracking the device-reported maximum when it has one."""
        resolved = self._resolve(self._control.max)
        if resolved is not None:
            return resolved
        current = self.native_value
        return max(current, _DEFAULT_MAX) if current is not None else _DEFAULT_MAX

    async def async_set_native_value(self, value: float) -> None:
        """Write the value and wait for the device to accept it."""
        await self._async_call(self._set_value, value)
