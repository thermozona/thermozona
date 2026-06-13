# SPDX-FileCopyrightText: 2026 Jaap van der Meer
# SPDX-License-Identifier: MIT
"""Number entities for advanced Thermozona controls."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity
from homeassistant.const import UnitOfTemperature
from homeassistant.helpers.entity import EntityCategory

from .. import DOMAIN


class ThermozonaFlowCurveOffsetNumber(NumberEntity):
    """Expose runtime flow-curve offset control as a Home Assistant number."""

    _attr_has_entity_name = True
    _attr_name = "Flow Curve Offset"
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_min_value = -10.0
    _attr_native_max_value = 10.0
    _attr_native_step = 0.5
    _attr_icon = "mdi:tune-variant"
    _attr_object_id = "thermozona_flow_curve_offset"
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        entry_id: str,
        controller,
    ) -> None:
        self._controller = controller
        self._attr_unique_id = f"{entry_id}_flow_curve_offset"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry_id)},
            "name": "Thermozona",
        }
        self._attr_native_value = controller.get_flow_curve_offset()

    async def async_added_to_hass(self) -> None:
        """Register with heat pump controller."""
        await super().async_added_to_hass()
        self._controller.register_flow_curve_offset_number(self)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister when the entity is removed."""
        self._controller.unregister_flow_curve_offset_number(self)
        await super().async_will_remove_from_hass()

    @property
    def native_value(self) -> float | None:
        """Return current active offset value."""
        return self._attr_native_value

    async def async_set_native_value(self, value: float) -> None:
        """Set runtime flow-curve offset override."""
        self._controller.set_flow_curve_offset(value)

    def set_current_value(self, value: float) -> None:
        """Update state from controller (used on reset/reload)."""
        self._attr_native_value = round(value, 1)
        self.async_write_ha_state()
