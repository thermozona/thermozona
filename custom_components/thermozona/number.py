"""Number platform for Thermozona helper entities."""
from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.helpers.entity import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DOMAIN
from .heat_pump import HeatPumpController
from .pro.number import ThermozonaFlowCurveOffsetNumber

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Thermozona number entities."""
    domain_data = hass.data[DOMAIN]
    entry_config = domain_data[config_entry.entry_id]

    controllers = domain_data.setdefault("controllers", {})
    controller = controllers.get(config_entry.entry_id)
    if controller is None:
        controller = HeatPumpController(hass, entry_config)
    else:
        controller.refresh_entry_config(entry_config)
    controllers[config_entry.entry_id] = controller

    entities: list[NumberEntity] = [
        ThermozonaFlowTemperatureNumber(config_entry.entry_id, controller),
        ThermozonaFlowCurveOffsetNumber(config_entry.entry_id, controller),
    ]

    async_add_entities(entities)


class ThermozonaFlowTemperatureNumber(NumberEntity):
    """Expose the computed flow temperature as a Home Assistant number."""

    _attr_has_entity_name = True
    _attr_name = "Flow Temperature"
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_min_value = 10.0
    _attr_native_max_value = 45.0
    _attr_native_step = 0.5
    _attr_icon = "mdi:thermometer"
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        entry_id: str,
        controller: HeatPumpController,
    ) -> None:
        self._controller = controller
        self._attr_unique_id = f"{entry_id}_flow_temperature"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry_id)},
            "name": "Thermozona",
        }
        self._attr_native_value: float | None = None

    async def async_added_to_hass(self) -> None:
        """Register with the heat pump controller."""
        await super().async_added_to_hass()
        self.async_write_ha_state()
        self._controller.register_flow_temperature_number(self)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister when the entity is removed."""
        self._controller.unregister_flow_temperature_number(self)
        await super().async_will_remove_from_hass()

    @property
    def native_value(self) -> float | None:
        """Return the current flow temperature setpoint."""
        return self._attr_native_value

    def set_calculated_value(self, value: float) -> None:
        """Update the number with the latest calculated flow temperature."""
        self._attr_native_value = round(value, 1)
        self.async_write_ha_state()
