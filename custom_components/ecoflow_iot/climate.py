"""Climate platform for EcoFlow IoT.

Only the local Bluetooth transport has climate devices (the Wave air
conditioner); the cloud Open API exposes no thermostat, so a cloud entry adds
nothing here. Home Assistant discovers platforms at the integration root, which
is why this dispatcher exists at all rather than living under ``ble/``.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.importlib import async_import_module

from . import EcoFlowConfigEntry
from .const import is_ble_entry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EcoFlowConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EcoFlow climate entities from a config entry."""
    if not is_ble_entry(entry):
        return

    module = await async_import_module(hass, f"{__package__}.ble.climate")

    await module.async_setup_entry(hass, entry, async_add_entities)
