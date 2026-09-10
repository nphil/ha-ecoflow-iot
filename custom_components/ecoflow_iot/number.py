"""Number platform for EcoFlow IoT."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.importlib import async_import_module

from . import EcoFlowConfigEntry
from .const import is_ble_entry
from .devices.base import EcoFlowNumberEntityDescription
from .entity import EcoFlowEntity

_PLATFORM = Platform.NUMBER


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EcoFlowConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EcoFlow numbers from a config entry."""
    if is_ble_entry(entry):
        module = await async_import_module(hass, f"{__package__}.ble.number")

        await module.async_setup_entry(hass, entry, async_add_entities)
        return

    coordinator = entry.runtime_data
    entities: list[NumberEntity] = []
    for sn, device in coordinator.devices.items():
        for description in device.entity_descriptions(_PLATFORM):
            entities.append(EcoFlowNumber(coordinator, sn, description))
    async_add_entities(entities)

    # Some numbers are discovered from live quota that only arrives over MQTT
    # after setup (e.g. the Stream base-load schedule). Add them as they appear,
    # which also picks up periods added in the app later without a reload.
    added: dict[str, set[str]] = {}

    @callback
    def _add_dynamic() -> None:
        new_entities: list[NumberEntity] = []
        for sn, device in coordinator.devices.items():
            state = coordinator.data.get(sn)
            if state is None:
                continue
            seen = added.setdefault(sn, set())
            for description in device.dynamic_entity_descriptions(_PLATFORM, state.quota):
                if description.key in seen:
                    continue
                seen.add(description.key)
                new_entities.append(EcoFlowNumber(coordinator, sn, description))
        if new_entities:
            async_add_entities(new_entities)

    _add_dynamic()
    entry.async_on_unload(coordinator.async_add_listener(_add_dynamic))


class EcoFlowNumber(EcoFlowEntity, NumberEntity):
    """A controllable numeric setpoint on an EcoFlow device."""

    entity_description: EcoFlowNumberEntityDescription

    @property
    def native_value(self) -> float | None:
        """Return the current setpoint."""
        value = self._raw_value()
        return None if value is None else float(value)

    async def async_set_native_value(self, value: float) -> None:
        """Send a new setpoint to the device."""
        params = self.entity_description.command_fn(value, self._quota)
        await self.coordinator.async_send_command(self._sn, params)
