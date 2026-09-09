"""Climate entity for locally connected (BLE) EcoFlow devices.

Built from the `@controls.climate` descriptor on the vendored device class: the
model declares which protobuf fields carry power, mode, setpoints and fan speed,
and which of those apply in which mode. Wave 3 reports every temperature in the
unit the unit itself is set to, so the entity follows `user_temp_unit` instead of
assuming Celsius (the mismatch behind upstream issue #422).
"""

from __future__ import annotations

from typing import Any, Final

from homeassistant.components.climate import (
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util.unit_conversion import TemperatureConverter

from ..eflib import DeviceBase, controls, get_controls
from . import EcoFlowBleConfigEntry
from .coordinator import EcoFlowBleCoordinator
from .entity import (
    EcoFlowBleControlEntity,
    device_temperature_unit,
    enum_option,
    hass_unit,
    prop_name,
)

# Device fields the climate entity presents as presets. The select platform skips
# them so a single control does not appear twice.
CLIMATE_PRESET_KEYS: Final[frozenset[str]] = frozenset({"operating_submode"})

# The climate control descriptor names a current-temperature field but has no slot
# for measured humidity; Wave 3 reports it under this name.
_CURRENT_HUMIDITY_FIELD: Final = "ambient_humidity"


def _to_hvac_modes(modes: frozenset[str] | None) -> frozenset[HVACMode] | None:
    """Translate the model's mode names into HA's own enum."""
    if modes is None:
        return None
    return frozenset(HVACMode(mode) for mode in modes)


def _preset_control(device: DeviceBase) -> controls.select | None:
    """The select control the climate entity presents as its preset list."""
    for control in get_controls(device, controls.select):
        if control.key in CLIMATE_PRESET_KEYS:
            return control
    return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EcoFlowBleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the climate entity for a locally connected EcoFlow device."""
    coordinator = entry.runtime_data
    device = coordinator.device
    preset = _preset_control(device)

    async_add_entities(
        EcoFlowBleClimate(coordinator, control, preset)
        for control in get_controls(device, controls.climate)
    )


class EcoFlowBleClimate(EcoFlowBleControlEntity, ClimateEntity):
    """An air conditioner driven over the local link."""

    _enable_turn_on_off_backwards_compat = False

    def __init__(
        self,
        coordinator: EcoFlowBleCoordinator,
        control: controls.climate,
        preset: controls.select | None,
    ) -> None:
        """Build the climate entity from its control descriptor."""
        super().__init__(coordinator, control)
        self._control: controls.climate = control
        self._preset = preset
        self._preset_availability_prop = (
            prop_name(preset.availability) if preset is not None else None
        )

        self._power_prop = prop_name(control.power_field)
        self._current_temp_prop = prop_name(control.current_temperature_field)
        self._target_temp_prop = prop_name(control.target_temperature_field)
        self._target_low_prop = prop_name(control.target_temperature_low_field)
        self._target_high_prop = prop_name(control.target_temperature_high_field)
        self._humidity_prop = prop_name(control.target_humidity_field)
        self._fan_prop = prop_name(control.fan_speed_field)

        self._hvac_to_mode: dict[HVACMode, Any] = {
            HVACMode(mode): value for mode, value in control.hvac_modes.items()
        }
        self._mode_to_hvac = {v: k for k, v in self._hvac_to_mode.items()}
        self._fan_to_speed = dict(control.fan_modes)
        self._speed_to_fan = {v: k for k, v in self._fan_to_speed.items()}
        self._range_modes = _to_hvac_modes(control.target_temperature_range_hvac_modes)
        self._humidity_modes = _to_hvac_modes(control.target_humidity_hvac_modes)
        self._target_temp_modes = _to_hvac_modes(control.target_temperature_hvac_modes)
        self._fan_speed_modes = _to_hvac_modes(control.fan_speed_hvac_modes)

        self._declared_unit = hass_unit(control.temperature_unit)
        self._device_reports_unit = hasattr(coordinator.device, "temp_unit")

        self._attr_hvac_modes = [HVACMode.OFF, *self._hvac_to_mode]
        self._attr_fan_modes = list(self._fan_to_speed)
        if preset is not None:
            self._attr_preset_modes = preset.options_str
        if control.min_humidity is not None:
            self._attr_min_humidity = control.min_humidity
        if control.max_humidity is not None:
            self._attr_max_humidity = control.max_humidity

    @property
    def temperature_unit(self) -> str:
        """Unit the device itself works in."""
        if self._device_reports_unit:
            return device_temperature_unit(self._device)
        return self._declared_unit

    def _in_device_unit(self, value: float | None) -> float | None:
        """Convert a bound declared by the model into the device's unit."""
        if value is None:
            return None
        unit = self.temperature_unit
        if unit == self._declared_unit:
            return value
        return round(
            TemperatureConverter.convert(value, self._declared_unit, unit)
        )

    def _temperature_step(self, step: float) -> float:
        """Step in the device's unit; a half-degree C is a whole degree F."""
        if self.temperature_unit == self._declared_unit:
            return step
        return max(step, 1.0)

    def _mode_allowed(self, modes: frozenset[HVACMode] | None) -> bool:
        return modes is None or self.hvac_mode in modes

    @property
    def _uses_range(self) -> bool:
        """Whether the active mode sets a low/high pair instead of one setpoint."""
        return self._control.set_target_temperature_range is not None and (
            self._mode_allowed(self._range_modes)
        )

    @property
    def _uses_humidity(self) -> bool:
        return self._control.set_target_humidity is not None and self._mode_allowed(
            self._humidity_modes
        )

    @property
    def _uses_target_temp(self) -> bool:
        return self._control.set_target_temperature is not None and self._mode_allowed(
            self._target_temp_modes
        )

    @property
    def _presets_available(self) -> bool:
        if self._preset is None:
            return False
        if self._preset_availability_prop is None:
            return True
        return bool(self._value(self._preset_availability_prop))

    @property
    def supported_features(self) -> ClimateEntityFeature:
        """Features of the mode the device is actually in."""
        features = ClimateEntityFeature(0)
        if self._control.set_power is not None:
            features |= ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF

        mode = self.hvac_mode
        if mode is None or mode == HVACMode.OFF:
            return features

        if self._control.set_fan_speed is not None and self._mode_allowed(
            self._fan_speed_modes
        ):
            features |= ClimateEntityFeature.FAN_MODE

        if self._uses_range:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        elif self._uses_humidity:
            features |= ClimateEntityFeature.TARGET_HUMIDITY
        elif self._uses_target_temp:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE

        if self._presets_available:
            features |= ClimateEntityFeature.PRESET_MODE

        return features

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Active mode, or OFF while the device is powered down."""
        if self._power_prop is not None and self._value(self._power_prop) is not True:
            return HVACMode.OFF
        return self._mode_to_hvac.get(self._value(self._key))

    @property
    def current_temperature(self) -> float | None:
        """Temperature the device measures."""
        return self._value(self._current_temp_prop)

    @property
    def target_temperature(self) -> float | None:
        """Single setpoint of the active mode."""
        return self._value(self._target_temp_prop)

    @property
    def target_temperature_low(self) -> float | None:
        """Lower setpoint of the thermostatic mode."""
        return self._value(self._target_low_prop)

    @property
    def target_temperature_high(self) -> float | None:
        """Upper setpoint of the thermostatic mode."""
        return self._value(self._target_high_prop)

    @property
    def target_humidity(self) -> int | None:
        """Humidity setpoint of the dehumidifying mode."""
        value = self._value(self._humidity_prop)
        return None if value is None else int(value)

    @property
    def current_humidity(self) -> int | None:
        """Humidity the device measures."""
        value = self._value(_CURRENT_HUMIDITY_FIELD)
        return None if value is None else int(value)

    @property
    def fan_mode(self) -> str | None:
        """Named fan speed, or None for a speed the model does not name."""
        return self._speed_to_fan.get(self._value(self._fan_prop))

    @property
    def preset_mode(self) -> str | None:
        """Active submode, or None when unreported or not applicable."""
        if self._preset is None:
            return None
        option = enum_option(self._value(self._preset.key))
        return option if option in self._preset.options_str else None

    @property
    def min_temp(self) -> float:
        """Lowest setpoint the model accepts, in the device's unit."""
        control = self._control
        bound = control.min_range_temp if self._uses_range else control.min_temp
        converted = self._in_device_unit(bound)
        return super().min_temp if converted is None else converted

    @property
    def max_temp(self) -> float:
        """Highest setpoint the model accepts, in the device's unit."""
        control = self._control
        bound = control.max_range_temp if self._uses_range else control.max_temp
        converted = self._in_device_unit(bound)
        return super().max_temp if converted is None else converted

    @property
    def target_temperature_step(self) -> float:
        """Setpoint granularity in the device's unit."""
        control = self._control
        step = (
            control.target_temperature_range_step
            if self._uses_range
            else control.target_temperature_step
        )
        return self._temperature_step(step)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Switch the device off, or on and into the requested mode."""
        set_power = self._control.set_power
        if hvac_mode == HVACMode.OFF:
            if set_power is not None:
                await self._async_call(set_power, False)
            return

        if (
            set_power is not None
            and self._power_prop is not None
            and self._value(self._power_prop) is not True
        ):
            await self._async_call(set_power, True)

        mode = self._hvac_to_mode.get(hvac_mode)
        if mode is not None and self._control.set_operating_mode is not None:
            await self._async_call(self._control.set_operating_mode, mode)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Write the setpoint(s) for the active mode."""
        control = self._control
        low = kwargs.get(ATTR_TARGET_TEMP_LOW)
        high = kwargs.get(ATTR_TARGET_TEMP_HIGH)
        if control.set_target_temperature_range is not None and None not in (low, high):
            step = self.target_temperature_step
            await self._async_call(
                control.set_target_temperature_range,
                _snap(float(low), step),
                _snap(float(high), step),
            )
            return

        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is not None and control.set_target_temperature is not None:
            await self._async_call(
                control.set_target_temperature,
                _snap(float(temperature), self.target_temperature_step),
            )

    async def async_set_humidity(self, humidity: int) -> None:
        """Write the humidity setpoint."""
        if self._control.set_target_humidity is not None:
            await self._async_call(self._control.set_target_humidity, int(humidity))

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Write the fan speed."""
        speed = self._fan_to_speed.get(fan_mode)
        if speed is not None and self._control.set_fan_speed is not None:
            await self._async_call(self._control.set_fan_speed, speed)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Write the submode behind the preset."""
        if self._preset is not None:
            await self._async_call(self._preset.set_value_func, preset_mode)

    async def async_turn_on(self) -> None:
        """Power the device on."""
        if self._control.set_power is not None:
            await self._async_call(self._control.set_power, True)

    async def async_turn_off(self) -> None:
        """Power the device off."""
        if self._control.set_power is not None:
            await self._async_call(self._control.set_power, False)


def _snap(value: float, step: float) -> float:
    """Round a requested temperature onto the step the device accepts."""
    if step <= 0:
        return value
    return round(value / step) * step
