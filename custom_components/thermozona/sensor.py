"""Sensor platform for Thermozona helper entities."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import UnitOfTemperature
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DOMAIN
from .heat_pump import HeatPumpController

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Thermozona sensor entities."""
    domain_data = hass.data[DOMAIN]
    entry_config = domain_data[config_entry.entry_id]

    controller = domain_data["controllers"][config_entry.entry_id]

    async_add_entities(
        [
            ThermozonaHeatPumpStatusSensor(config_entry.entry_id, controller),
            ThermozonaFlowTemperatureSensor(config_entry.entry_id, controller),
            ThermozonaPriceChargingSensor(config_entry.entry_id, controller),
        ]
    )


class ThermozonaHeatPumpStatusSensor(SensorEntity):
    """Expose the current heat-pump direction (heat/cool/idle)."""

    _attr_has_entity_name = True
    _attr_name = "Heat Pump Status"
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:hvac"

    def __init__(
        self,
        entry_id: str,
        controller: HeatPumpController,
    ) -> None:
        self._controller = controller
        self._attr_unique_id = f"{entry_id}_heat_pump_status"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry_id)},
            "name": "Thermozona",
        }
        self._attr_native_value: str = "idle"

    async def async_added_to_hass(self) -> None:
        """Register the sensor with the heat pump controller."""
        await super().async_added_to_hass()
        self.async_write_ha_state()
        self._controller.register_pump_sensor(self)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister the sensor when removed."""
        self._controller.unregister_pump_sensor(self)
        await super().async_will_remove_from_hass()

    def update_state(self, state: str) -> None:
        """Push the latest pump state into Home Assistant."""
        if state not in {"heat", "cool", "idle"}:
            state = "idle"
        self._attr_native_value = state
        self.async_write_ha_state()


class ThermozonaFlowTemperatureSensor(SensorEntity):
    """Expose calculated flow temperature as a long-term statistics sensor."""

    _attr_has_entity_name = True
    _attr_name = "Flow Temperature"
    _attr_should_poll = False
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:thermometer"

    def __init__(
        self,
        entry_id: str,
        controller: HeatPumpController,
    ) -> None:
        self._controller = controller
        self._attr_unique_id = f"{entry_id}_flow_temperature_sensor"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry_id)},
            "name": "Thermozona",
        }
        self._attr_native_value: float | None = None

    async def async_added_to_hass(self) -> None:
        """Register with the heat pump controller."""
        await super().async_added_to_hass()
        self.async_write_ha_state()
        self._controller.register_flow_temperature_sensor(self)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister when the entity is removed."""
        self._controller.unregister_flow_temperature_sensor(self)
        await super().async_will_remove_from_hass()

    def set_calculated_value(self, value: float) -> None:
        """Update with the latest calculated flow temperature."""
        self._attr_native_value = round(value, 1)
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return breakdown attributes for the computed flow temperature."""
        return self._controller.get_flow_temperature_factors()


class ThermozonaPriceChargingSensor(SensorEntity):
    """Expose the current charging phase and the next planned window."""

    _attr_has_entity_name = True
    _attr_name = "Price Charging"
    _attr_should_poll = False
    _attr_icon = "mdi:home-battery"

    def __init__(self, entry_id: str, controller: HeatPumpController) -> None:
        self._controller = controller
        self._attr_unique_id = f"{entry_id}_price_charging_status"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry_id)}, "name": "Thermozona"
        }
        self._unsubscribe = None

    @property
    def native_value(self) -> str:
        """Return off, preview, normal, charging, coasting, or unavailable."""
        return self._controller.price_charging.state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the current plan and price diagnostics."""
        return self._controller.price_charging.attributes

    async def async_added_to_hass(self) -> None:
        """Subscribe to plan updates."""
        await super().async_added_to_hass()
        self._unsubscribe = self._controller.price_charging.subscribe(
            self.async_write_ha_state
        )
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Remove the subscription on unload."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        await super().async_will_remove_from_hass()
