"""Button platform for EcoFlow IoT.

Buttons exist only on the local Bluetooth transport, where a device offers
one-shot actions with no state to read back. Home Assistant discovers platforms
at the integration root, which is why this dispatcher exists at all rather than
living under ``ble/``.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EcoFlowConfigEntry, is_ble_entry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EcoFlowConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EcoFlow buttons from a config entry."""
    if not is_ble_entry(entry):
        return

    from .ble.button import async_setup_entry as async_setup_ble_entry

    await async_setup_ble_entry(hass, entry, async_add_entities)
