"""Switches for locally connected (BLE) EcoFlow devices.

Every switch comes from a `@controls.toggle` (or `switch`/`outlet`) declared on the
vendored device class, so a model only gets the toggles its firmware really has.
"""

from __future__ import annotations

from typing import Any, Final

from homeassistant.components.switch import (
    SwitchDeviceClass,
    SwitchEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from ..eflib import controls, get_controls
from . import EcoFlowBleConfigEntry
from .coordinator import EcoFlowBleCoordinator
from .entity import EcoFlowBleControlEntity

# Toggles that switch a physical output rather than a device feature. eflib models
# both with the generic `switch` control, which would otherwise lose the distinction
# HA draws between an outlet and a feature switch.
#
# These are also created enabled, overriding the vendored River 3 module, which ships
# its AC output switch registry-disabled. The reason it gives is that the state is
# derived from `flow_info_ac_out` rather than a config readback - but decoding the
# real River 3 UPS capture in tests/test_ble_eflib.py shows the firmware never sends
# the `ac_out_open` field that pr705 defines, so that flow bitmask is the only signal
# the device offers and it is the same check the vendor app makes. Hiding the main
# output switch of a power station by default is the worse trade.
_OUTLETS: Final[frozenset[str]] = frozenset({"ac_ports", "dc_12v_port"})


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EcoFlowBleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up switches for a locally connected EcoFlow device."""
    coordinator = entry.runtime_data
    async_add_entities(
        EcoFlowBleSwitch(coordinator, control)
        for control in get_controls(coordinator.device, controls.toggle)
    )


class EcoFlowBleSwitch(EcoFlowBleControlEntity, SwitchEntity):
    """A device toggle driven over the local link."""

    def __init__(
        self,
        coordinator: EcoFlowBleCoordinator,
        control: controls.toggle,
    ) -> None:
        """Build the switch from its control descriptor."""
        super().__init__(coordinator, control)
        self._enable = control.enable_func
        if isinstance(control, controls.outlet) or control.key in _OUTLETS:
            self._attr_device_class = SwitchDeviceClass.OUTLET
            self._attr_entity_registry_enabled_default = True
        else:
            self._attr_device_class = SwitchDeviceClass.SWITCH

    @property
    def is_on(self) -> bool | None:
        """Reported state, or None while the device has not sent one."""
        return self._value(self._key)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the toggle on and wait for the device to accept the write."""
        await self._async_call(self._enable, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the toggle off and wait for the device to accept the write."""
        await self._async_call(self._enable, False)
