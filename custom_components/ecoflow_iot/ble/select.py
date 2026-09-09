"""Selects for locally connected (BLE) EcoFlow devices."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from ..eflib import controls, get_controls
from . import EcoFlowBleConfigEntry
from .climate import CLIMATE_PRESET_KEYS
from .coordinator import EcoFlowBleCoordinator
from .entity import EcoFlowBleControlEntity, enum_option


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EcoFlowBleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up selects for a locally connected EcoFlow device."""
    coordinator = entry.runtime_data
    async_add_entities(
        EcoFlowBleSelect(coordinator, control)
        for control in get_controls(coordinator.device, controls.select)
        if control.key not in CLIMATE_PRESET_KEYS
    )


class EcoFlowBleSelect(EcoFlowBleControlEntity, SelectEntity):
    """A device mode chosen over the local link."""

    def __init__(
        self,
        coordinator: EcoFlowBleCoordinator,
        control: controls.select,
    ) -> None:
        """Build the select from its control descriptor."""
        super().__init__(coordinator, control)
        self._set_option = control.set_value_func
        self._attr_options = control.options_str

    @property
    def current_option(self) -> str | None:
        """Selected option, or None for an unreported or unmapped value."""
        option = enum_option(self._value(self._key))
        return option if option in self._attr_options else None

    async def async_select_option(self, option: str) -> None:
        """Select the option and wait for the device to accept it."""
        await self._async_call(self._set_option, option)
