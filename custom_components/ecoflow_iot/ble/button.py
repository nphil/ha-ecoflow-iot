"""Buttons for locally connected (BLE) EcoFlow devices."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from ..eflib import controls, get_controls
from . import EcoFlowBleConfigEntry
from .coordinator import EcoFlowBleCoordinator
from .entity import EcoFlowBleControlEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EcoFlowBleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up buttons for a locally connected EcoFlow device."""
    coordinator = entry.runtime_data
    async_add_entities(
        EcoFlowBleButton(coordinator, control)
        for control in get_controls(coordinator.device, controls.button)
    )


class EcoFlowBleButton(EcoFlowBleControlEntity, ButtonEntity):
    """A stateless device action triggered over the local link."""

    def __init__(
        self,
        coordinator: EcoFlowBleCoordinator,
        control: controls.button,
    ) -> None:
        """Build the button from its control descriptor."""
        super().__init__(coordinator, control)
        self._press = control.press_func

    async def async_press(self) -> None:
        """Run the action and wait for the device to accept it."""
        await self._async_call(self._press)
