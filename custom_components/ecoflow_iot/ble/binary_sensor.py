"""Binary sensors for locally connected (BLE) EcoFlow devices."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Final

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EcoFlowBleConfigEntry
from .coordinator import EcoFlowBleCoordinator
from .entity import EcoFlowBleEntity


@dataclass(frozen=True, kw_only=True)
class EcoFlowBleBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Binary sensor description bound to a boolean eflib device property."""

    state_attribute_props: tuple[str, ...] = ()


_DIAGNOSTIC: Final = EntityCategory.DIAGNOSTIC

_BINARY_SENSORS: Final[dict[str, EcoFlowBleBinarySensorEntityDescription]] = {
    # Wave 3
    "pet_care_warning": EcoFlowBleBinarySensorEntityDescription(
        key="",
        device_class=BinarySensorDeviceClass.PROBLEM,
    ),
    "in_drainage": EcoFlowBleBinarySensorEntityDescription(
        key="",
        device_class=BinarySensorDeviceClass.RUNNING,
    ),
    # River 3 family
    "plugged_in_ac": EcoFlowBleBinarySensorEntityDescription(
        key="",
        device_class=BinarySensorDeviceClass.PLUG,
    ),
    "error_occurred": EcoFlowBleBinarySensorEntityDescription(
        key="",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=_DIAGNOSTIC,
        state_attribute_props=("error_code",),
    ),
    "fan_running": EcoFlowBleBinarySensorEntityDescription(
        key="",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=_DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    # River 3 Plus / Pro add-on battery
    "battery_1_enabled": EcoFlowBleBinarySensorEntityDescription(
        key="",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=_DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EcoFlowBleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up binary sensors for a locally connected EcoFlow device."""
    coordinator = entry.runtime_data
    device = coordinator.device

    async_add_entities(
        EcoFlowBleBinarySensor(coordinator, key, description)
        for key, description in _BINARY_SENSORS.items()
        if hasattr(device, key)
    )


class EcoFlowBleBinarySensor(EcoFlowBleEntity, BinarySensorEntity):
    """A boolean state the device reports over its local link."""

    entity_description: EcoFlowBleBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: EcoFlowBleCoordinator,
        key: str,
        description: EcoFlowBleBinarySensorEntityDescription,
    ) -> None:
        """Bind the binary sensor to the device property named by `key`."""
        super().__init__(coordinator, key)
        self.entity_description = replace(description, key=key)
        self._attr_translation_key = key

    @property
    def is_on(self) -> bool | None:
        """Reported state, or None while the device has not sent one."""
        return self._value(self._key)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Supporting values, such as the error code behind a problem."""
        props = self.entity_description.state_attribute_props
        if not props:
            return None
        return {prop: self._value(prop) for prop in props}
