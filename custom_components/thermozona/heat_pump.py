"""Heat pump controller for the Thermozona integration."""
from __future__ import annotations

import asyncio
import logging
import math
import weakref
from datetime import datetime, timedelta, timezone
from typing import Any, TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.components.climate import HVACMode

from . import (
    CONTROL_MODE_PWM,
    CONF_AUTO_COOL_LOCKOUT_BELOW_C,
    CONF_AUTO_HEAT_LOCKOUT_ABOVE_C,
    CONF_AUTO_MIN_CHANGEOVER_MINUTES,
    CONF_COOLING_AIR_COIL_ONLY_OFFSET,
    CONF_COOLING_DEHUMIDIFICATION_MIN_FLOW,
    CONF_COOLING_DEWPOINT_MARGIN,
    CONF_COOLING_DEWPOINT_SENSOR,
    CONF_COOLING_FLOOR_DEWPOINT_MARGIN,
    CONF_FLOW_CURVE_OFFSET,
    CONF_FLOW_TEMP_SENSOR,
    CONF_HEAT_PUMP_MODE,
    CONF_FLOW,
    CONF_COOLING_BASE_OFFSET,
    CONF_FLOOR_MATERIAL,
    CONF_HEATING_BASE_OFFSET,
    CONF_PRO_PREHEAT_ENABLED,
    CONF_PRO_PREHEAT_FORECAST_SENSOR,
    CONF_PRO_PREHEAT_SOLAR_SENSOR,
    CONF_OUTSIDE_TEMP_SENSOR,
    CONF_PRICE_CHARGING,
    CONF_WEATHER_SLOPE_COOL,
    CONF_WEATHER_SLOPE_HEAT,
    CONF_WRITE_DEADBAND_C,
    CONF_WRITE_MIN_INTERVAL_MINUTES,
    CONF_ZONE_TYPE,
    CONF_ZONES,
    DEFAULT_COOLING_BASE_OFFSET,
    DEFAULT_COOLING_AIR_COIL_ONLY_OFFSET,
    DEFAULT_COOLING_DEHUMIDIFICATION_MIN_FLOW,
    DEFAULT_COOLING_FLOOR_DEWPOINT_MARGIN,
    DEFAULT_AUTO_COOL_LOCKOUT_BELOW_C,
    DEFAULT_AUTO_HEAT_LOCKOUT_ABOVE_C,
    DEFAULT_AUTO_MIN_CHANGEOVER_MINUTES,
    DEFAULT_FLOW_CURVE_OFFSET,
    DEFAULT_HEATING_BASE_OFFSET,
    DEFAULT_PRO_WRITE_DEADBAND_C,
    DEFAULT_PRO_WRITE_MIN_INTERVAL_MINUTES,
    DEFAULT_WEATHER_SLOPE_COOL,
    DEFAULT_WEATHER_SLOPE_HEAT,
    DOMAIN,
    FLOOR_MATERIAL_WOOD,
    ZONE_TYPE_AIR_COIL,
)
from .helpers import resolve_circuits
from .price_charging import PriceChargingManager
from .pro.flow_curve import FlowCurveRuntimeManager
from .pro.flow_supervisor import ProFlowSupervisor

if TYPE_CHECKING:
    from .thermostat import ThermozonaThermostat
    from .number import (
        ThermozonaFlowTemperatureNumber,
    )
    from .pro.number import ThermozonaFlowCurveOffsetNumber
    from .select import ThermozonaHeatPumpModeSelect
    from .select import ThermozonaZoneStrategySelect
    from .sensor import ThermozonaFlowTemperatureSensor, ThermozonaHeatPumpStatusSensor

_LOGGER = logging.getLogger(__name__)


class HeatPumpController:
    """Coordinate state updates for the shared heat pump."""

    def __init__(self, hass: HomeAssistant, entry_config: dict[str, Any]) -> None:
        self._hass = hass
        self._entry_config = entry_config
        self._zone_status: dict[str, dict[str, Any]] = {}
        self._last_auto_mode: HVACMode = HVACMode.HEAT
        self._last_auto_effective_mode: HVACMode = HVACMode.HEAT
        self._last_auto_mode_change: datetime | None = None
        self._thermostats: weakref.WeakSet[ThermozonaThermostat] = weakref.WeakSet()
        self._pwm_zone_indices: weakref.WeakKeyDictionary[ThermozonaThermostat, int] = weakref.WeakKeyDictionary()
        self._next_pwm_zone_index = 0
        self._flow_number: weakref.ReferenceType[
            ThermozonaFlowTemperatureNumber
        ] | None = None
        self._flow_sensor: weakref.ReferenceType[
            ThermozonaFlowTemperatureSensor
        ] | None = None
        self._pump_sensor: weakref.ReferenceType[
            ThermozonaHeatPumpStatusSensor
        ] | None = None
        self._last_flow_temp: float | None = None
        self._last_flow_factors: dict[str, Any] = {
            "command_active": False,
            "command_updated_at": datetime.now(timezone.utc).isoformat(),
            "active_zones": [],
        }
        self._last_flow_write_temp: float | None = None
        self._last_flow_write_time: datetime | None = None
        self._mode_select: weakref.ReferenceType[
            ThermozonaHeatPumpModeSelect
        ] | None = None
        self._zone_strategy_select: weakref.ReferenceType[
            ThermozonaZoneStrategySelect
        ] | None = None
        self._mode_value: str = "auto"
        self._zone_strategy_value: str = "auto"
        self._mode_entity_id: str | None = None
        self._pump_state: str = "idle"
        self._last_any_circuit_on: datetime | None = None
        self._demand_off_delay = timedelta(minutes=5)
        self._demand_off_task: asyncio.Task | None = None
        self._shutting_down = False
        self._startup_guard_enabled = False
        self._mode_ready = True
        self._flow_output_ready = True
        self._flow_curve_runtime = FlowCurveRuntimeManager(
            get_yaml_value=self._get_yaml_flow_curve_offset,
            notify_thermostats=lambda: self._notify_thermostats(),
        )
        self._flow_supervisor = ProFlowSupervisor()
        self.price_charging = PriceChargingManager(
            hass, entry_config.get(CONF_PRICE_CHARGING, {}), self._notify_thermostats
        )

    def get_price_charging_boost(self) -> float:
        """Allow price headroom only while heating is an available operation."""
        mode = self.get_operation_mode()
        if self._shutting_down or mode not in {"heat", "auto"}:
            return 0.0
        if mode == "auto":
            outside = self._read_entity_float(self._outside_temp_sensor())
            maximum = self._entry_config.get(
                CONF_AUTO_HEAT_LOCKOUT_ABOVE_C, DEFAULT_AUTO_HEAT_LOCKOUT_ABOVE_C
            )
            if outside is None or outside > maximum:
                return 0.0
        return self.price_charging.boost

    def begin_platform_setup(self) -> None:
        """Hold control idle until mode and flow output platforms are ready."""
        self._startup_guard_enabled = True
        self._mode_ready = bool(self._entry_config.get(CONF_HEAT_PUMP_MODE))
        self._flow_output_ready = False

    def _get_flow_write_settings(self) -> tuple[float, timedelta]:
        """Return deadband and minimum write interval for flow commands."""
        config = self._flow_config()
        deadband_default = DEFAULT_PRO_WRITE_DEADBAND_C
        min_interval_default = DEFAULT_PRO_WRITE_MIN_INTERVAL_MINUTES

        deadband = max(
            0.0,
            float(config.get(CONF_WRITE_DEADBAND_C, deadband_default)),
        )
        min_interval_minutes = max(
            1,
            int(config.get(CONF_WRITE_MIN_INTERVAL_MINUTES, min_interval_default)),
        )
        return deadband, timedelta(minutes=min_interval_minutes)

    def _should_dispatch_flow_temperature(
        self,
        *,
        flow_temp: float,
        now: datetime,
    ) -> bool:
        """Return True when a new flow command should be sent to output."""
        if self._last_flow_write_temp is None or self._last_flow_write_time is None:
            return True

        deadband, min_interval = self._get_flow_write_settings()
        if abs(flow_temp - self._last_flow_write_temp) >= deadband:
            return True

        if now - self._last_flow_write_time >= min_interval:
            return True

        return False

    def _flow_config(self) -> dict[str, Any]:
        return self._entry_config.get(CONF_FLOW, {})

    def _outside_temp_sensor(self) -> str | None:
        return self._entry_config.get(CONF_OUTSIDE_TEMP_SENSOR)

    def _flow_temp_entity(self) -> str | None:
        return self._entry_config.get(CONF_FLOW_TEMP_SENSOR)

    def _flow_temp_number(self) -> ThermozonaFlowTemperatureNumber | None:
        if self._flow_number is None:
            return None
        entity = self._flow_number()
        if entity is None:
            self._flow_number = None
        return entity

    def _mode_select_entity(self) -> ThermozonaHeatPumpModeSelect | None:
        if self._mode_select is None:
            return None
        entity = self._mode_select()
        if entity is None:
            self._mode_select = None
        return entity

    def _zone_strategy_select_entity(self) -> ThermozonaZoneStrategySelect | None:
        if self._zone_strategy_select is None:
            return None
        entity = self._zone_strategy_select()
        if entity is None:
            self._zone_strategy_select = None
        return entity

    def _flow_temp_sensor(self) -> ThermozonaFlowTemperatureSensor | None:
        if self._flow_sensor is None:
            return None
        entity = self._flow_sensor()
        if entity is None:
            self._flow_sensor = None
        return entity

    def _pump_sensor_entity(self) -> ThermozonaHeatPumpStatusSensor | None:
        if self._pump_sensor is None:
            return None
        entity = self._pump_sensor()
        if entity is None:
            self._pump_sensor = None
        return entity

    def register_flow_temperature_number(
        self, entity: ThermozonaFlowTemperatureNumber
    ) -> None:
        """Register internal number entity to publish flow temperature."""
        self._flow_number = weakref.ref(entity)
        self._flow_output_ready = True
        if self._last_flow_temp is not None:
            entity.set_calculated_value(self._last_flow_temp)
        if self._startup_guard_enabled and self._mode_ready:
            self._notify_thermostats()

    def unregister_flow_temperature_number(
        self, entity: ThermozonaFlowTemperatureNumber
    ) -> None:
        """Unregister the internal number entity when it is removed."""
        if self._flow_number is not None and self._flow_number() is entity:
            self._flow_number = None

    def register_flow_curve_offset_number(
        self, entity: ThermozonaFlowCurveOffsetNumber
    ) -> None:
        """Register helper number for runtime flow-curve offset tuning."""
        self._flow_curve_runtime.register_entity(entity)

    def unregister_flow_curve_offset_number(
        self, entity: ThermozonaFlowCurveOffsetNumber
    ) -> None:
        """Unregister runtime flow-curve offset helper number."""
        self._flow_curve_runtime.unregister_entity(entity)

    def _get_yaml_flow_curve_offset(self) -> float:
        """Return flow-curve offset from YAML config."""
        return float(
            self._entry_config.get(CONF_FLOW_CURVE_OFFSET, DEFAULT_FLOW_CURVE_OFFSET)
        )

    def get_flow_curve_offset(self) -> float:
        """Return active flow-curve offset (UI override or YAML value)."""
        return self._flow_curve_runtime.get_value()

    def set_flow_curve_offset(self, value: float) -> None:
        """Set runtime flow-curve offset override from the UI helper number."""
        self._flow_curve_runtime.set_override(value)

    def reset_flow_curve_offset(self) -> None:
        """Clear runtime override so YAML-configured offset becomes active again."""
        self._flow_curve_runtime.reset_override()

    def register_flow_temperature_sensor(
        self, entity: ThermozonaFlowTemperatureSensor
    ) -> None:
        """Register internal sensor entity to persist flow-temperature history."""
        self._flow_sensor = weakref.ref(entity)
        if self._last_flow_temp is not None:
            entity.set_calculated_value(self._last_flow_temp)

    def get_flow_temperature_factors(self) -> dict[str, Any]:
        """Return the latest flow-temperature breakdown attributes."""
        return dict(self._last_flow_factors) if self._last_flow_factors else {}

    def unregister_flow_temperature_sensor(
        self, entity: ThermozonaFlowTemperatureSensor
    ) -> None:
        """Unregister the internal flow-temperature sensor entity."""
        if self._flow_sensor is not None and self._flow_sensor() is entity:
            self._flow_sensor = None

    def register_pump_sensor(
        self, entity: ThermozonaHeatPumpStatusSensor
    ) -> None:
        """Register internal sensor for heat pump status."""
        self._pump_sensor = weakref.ref(entity)
        entity.update_state(self._pump_state)

    def unregister_pump_sensor(
        self, entity: ThermozonaHeatPumpStatusSensor
    ) -> None:
        """Unregister the internal sensor when removed."""
        if self._pump_sensor is not None and self._pump_sensor() is entity:
            self._pump_sensor = None

    def _update_pump_status(
        self, demand: bool, mode: HVACMode | None
    ) -> None:
        """Update cached pump status and expose it via helper entities."""
        if not demand or mode is None:
            state = "idle"
        elif mode == HVACMode.COOL:
            state = "cool"
        else:
            state = "heat"

        self._pump_state = state
        if (sensor := self._pump_sensor_entity()) is not None:
            sensor.update_state(state)

    def _mark_flow_command_inactive(self) -> None:
        """Publish that the cached flow value is not an active command."""
        if self._last_flow_factors.get("command_active") is False:
            return

        self._last_flow_factors.update(
            {
                "effective_mode": "idle",
                "command_active": False,
                "command_updated_at": datetime.now(timezone.utc).isoformat(),
                "active_zones": [],
                "cooling_active_floor_zones": [],
                "cooling_active_air_coil_zones": [],
                "cooling_demand_zone_count": 0,
            }
        )
        if (sensor := self._flow_temp_sensor()) is not None:
            sensor.async_write_ha_state()

    def register_mode_select(
        self, entity: ThermozonaHeatPumpModeSelect
    ) -> None:
        """Register internal select entity to control heat pump mode."""
        self._mode_select = weakref.ref(entity)
        self._mode_entity_id = entity.entity_id
        self._mode_ready = True
        entity.update_current_option(self._mode_value)
        for thermostat in list(self._thermostats):
            self._hass.async_create_task(thermostat.async_update_mode_listener())
        if self._startup_guard_enabled and self._flow_output_ready:
            self._notify_thermostats()

    def register_zone_strategy_select(
        self, entity: ThermozonaZoneStrategySelect
    ) -> None:
        """Register internal select entity to control zone strategy."""
        self._zone_strategy_select = weakref.ref(entity)
        entity.update_current_option(self._zone_strategy_value)
        self._notify_thermostats()

    def unregister_mode_select(
        self, entity: ThermozonaHeatPumpModeSelect
    ) -> None:
        """Unregister the internal select entity when removed."""
        if self._mode_select is not None and self._mode_select() is entity:
            self._mode_select = None
            self._mode_entity_id = None
            for thermostat in list(self._thermostats):
                self._hass.async_create_task(thermostat.async_update_mode_listener())

    def unregister_zone_strategy_select(
        self, entity: ThermozonaZoneStrategySelect
    ) -> None:
        """Unregister zone strategy select when removed."""
        if (
            self._zone_strategy_select is not None
            and self._zone_strategy_select() is entity
        ):
            self._zone_strategy_select = None

    def set_mode_value(self, value: str) -> None:
        """Store a new mode value coming from the select entity."""
        normalized = value.lower()
        if normalized not in {"auto", "heat", "cool", "off"}:
            _LOGGER.warning("%s: Invalid mode '%s', defaulting to auto", DOMAIN, value)
            normalized = "auto"

        previous = self._mode_value

        if normalized == "heat":
            self._last_auto_mode = HVACMode.HEAT
            self._last_auto_mode_change = datetime.now(timezone.utc)
        elif normalized == "cool":
            self._last_auto_mode = HVACMode.COOL
            self._last_auto_mode_change = datetime.now(timezone.utc)

        if normalized == previous:
            if (select := self._mode_select_entity()) is not None:
                select.update_current_option(normalized)
            return

        self._mode_value = normalized

        if (select := self._mode_select_entity()) is not None:
            select.update_current_option(normalized)

        _LOGGER.debug("%s: Heat pump mode set to %s", DOMAIN, normalized)
        self._notify_thermostats()

    def set_zone_strategy_value(self, value: str) -> None:
        """Store a new cooling strategy coming from the select entity."""
        normalized = value.lower()
        if normalized not in {"auto", "air_coil_only"}:
            _LOGGER.warning(
                "%s: Invalid cooling strategy '%s', defaulting to auto",
                DOMAIN,
                value,
            )
            normalized = "auto"

        if normalized == self._zone_strategy_value:
            if (select := self._zone_strategy_select_entity()) is not None:
                select.update_current_option(normalized)
            return

        self._zone_strategy_value = normalized
        if (select := self._zone_strategy_select_entity()) is not None:
            select.update_current_option(normalized)

        _LOGGER.debug("%s: Cooling strategy set to %s", DOMAIN, normalized)
        self._last_flow_write_temp = None
        self._last_flow_write_time = None
        self._notify_thermostats()
        self._hass.async_create_task(self._async_refresh_after_strategy_change())

    async def _async_refresh_after_strategy_change(self) -> None:
        """Refresh flow after strategy changes and thermostat tasks settle."""
        await self.async_update_heat_pump_state()
        await asyncio.sleep(2)
        await self.async_update_heat_pump_state()
        await asyncio.sleep(8)
        await self.async_update_heat_pump_state()

    def get_all_circuit_entities(self) -> list[str]:
        """Return all circuit entities across the configured zones."""
        circuits: list[str] = []
        for zone_config in self._entry_config.get(CONF_ZONES, {}).values():
            circuits.extend(resolve_circuits(zone_config))
        return circuits

    def get_operation_mode(self) -> str:
        """Return the current heat pump operation mode (heat/cool/auto)."""
        if self._startup_guard_enabled and (
            not self._mode_ready or not self._flow_output_ready
        ):
            return "off"

        if self._mode_select_entity() is not None:
            return self._mode_value

        mode_entity = self._entry_config.get(CONF_HEAT_PUMP_MODE)
        if not mode_entity:
            return "auto"

        state = self._hass.states.get(mode_entity)
        if not state:
            _LOGGER.warning("%s: Heat pump mode entity %s not found", DOMAIN, mode_entity)
            return "off"

        value = state.state.lower()
        if value in {"off", "idle"}:
            return "off"
        if value in {"heat", "heating"}:
            return "heat"
        if value in {"cool", "cooling"}:
            return "cool"
        if value in {"auto", "automatic"}:
            return "auto"

        _LOGGER.debug(
            "%s: Heat pump mode %s unknown (state=%s), defaulting to off",
            DOMAIN,
            mode_entity,
            state.state,
        )
        return "off"

    @property
    def mode_entity(self) -> str | None:
        """Return the configured heat pump mode entity, if any."""
        if self._mode_entity_id is not None:
            return self._mode_entity_id
        return self._entry_config.get(CONF_HEAT_PUMP_MODE)

    def update_zone_status(
        self,
        zone_name: str,
        *,
        target: float | None,
        current: float | None,
        active: bool | None = None,
        duty_cycle: float | None = None,
        zone_response: str | None = None,
        zone_flow_weight: float | None = None,
        heating_min_flow_c: float | None = None,
        zone_solar_weight: float | None = None,
        zone_type: str | None = None,
        floor_material: str | None = None,
        cooling_dewpoint_margin: float | None = None,
        cooling_dewpoint_protection: bool | None = None,
        cooling_suppressed: bool | None = None,
        source: ThermozonaThermostat | None = None,
    ) -> None:
        """Store latest temperature/target info for the zone."""
        previous = self._zone_status.get(zone_name)
        previous_cooling_state = None
        if previous is not None:
            previous_cooling_state = (
                bool(previous.get("active")),
                float(previous.get("current", 0.0))
                > float(previous.get("target", 0.0)),
                previous.get("target"),
                previous.get("zone_type"),
                previous.get("cooling_dewpoint_margin"),
                previous.get("cooling_dewpoint_protection", True),
                bool(previous.get("cooling_suppressed")),
            )

        if target is None or current is None:
            self._zone_status.pop(zone_name, None)
        else:
            entry = self._zone_status.setdefault(zone_name, {})
            entry["target"] = float(target)
            entry["current"] = float(current)
            if active is not None:
                entry["active"] = bool(active)
            if duty_cycle is not None:
                entry["duty_cycle"] = max(0.0, min(100.0, float(duty_cycle)))
            elif active is not None and "duty_cycle" not in entry:
                entry["duty_cycle"] = 100.0 if active else 0.0
            if zone_response is not None:
                entry["zone_response"] = str(zone_response).lower()
            if zone_flow_weight is not None:
                entry["zone_flow_weight"] = max(0.0, float(zone_flow_weight))
            if heating_min_flow_c is not None:
                entry["heating_min_flow_c"] = max(15.0, min(35.0, float(heating_min_flow_c)))
            if zone_solar_weight is not None:
                entry["zone_solar_weight"] = max(0.0, float(zone_solar_weight))
            if zone_type is not None:
                entry["zone_type"] = str(zone_type).lower()
            elif "zone_type" not in entry:
                entry["zone_type"] = "floor"
            if floor_material is not None:
                entry["floor_material"] = str(floor_material).lower() or None
            if cooling_dewpoint_margin is not None:
                entry["cooling_dewpoint_margin"] = max(
                    0.0,
                    float(cooling_dewpoint_margin),
                )
            if cooling_dewpoint_protection is not None:
                entry["cooling_dewpoint_protection"] = bool(
                    cooling_dewpoint_protection
                )
            if cooling_suppressed is not None:
                entry["cooling_suppressed"] = bool(cooling_suppressed)

        if active is not None and zone_name in self._zone_status:
            self._zone_status[zone_name]["active"] = bool(active)

        operation_mode = self.get_operation_mode()
        mode_changed = False
        if operation_mode == "auto":
            previous_mode = self._last_auto_effective_mode
            new_mode = self.determine_auto_mode()
            if new_mode != previous_mode:
                mode_changed = True
                _LOGGER.debug(
                    "%s: Auto mode changed from %s to %s after %s update",
                    DOMAIN,
                    previous_mode,
                    new_mode,
                    zone_name,
                )
                self._notify_thermostats(skip=source)

        current_status = self._zone_status.get(zone_name)
        current_cooling_state = None
        if current_status is not None:
            current_cooling_state = (
                bool(current_status.get("active")),
                float(current_status.get("current", 0.0))
                > float(current_status.get("target", 0.0)),
                current_status.get("target"),
                current_status.get("zone_type"),
                current_status.get("cooling_dewpoint_margin"),
                current_status.get("cooling_dewpoint_protection", True),
                bool(current_status.get("cooling_suppressed")),
            )
        if (
            not mode_changed
            and operation_mode in {"auto", "cool"}
            and current_cooling_state != previous_cooling_state
        ):
            self._notify_thermostats(skip=source)

    def determine_auto_mode(self) -> HVACMode:
        """Decide between heating or cooling when pump is in auto mode."""
        if not self._zone_status:
            self._last_auto_effective_mode = HVACMode.OFF
            return self._last_auto_effective_mode

        weighted_deltas: list[tuple[float, float]] = []
        for status in self._zone_status.values():
            delta = float(status["current"]) - float(status["target"])
            if abs(delta) <= 0.2:
                continue
            weight = max(0.1, float(status.get("zone_flow_weight", 1.0)))
            weighted_deltas.append((delta, weight))

        if not weighted_deltas:
            self._last_auto_effective_mode = HVACMode.OFF
            return self._last_auto_effective_mode

        total_weight = sum(weight for _, weight in weighted_deltas)
        avg_delta = (
            sum(delta * weight for delta, weight in weighted_deltas) / total_weight
        )
        candidate = HVACMode.OFF
        if avg_delta > 0.2:
            candidate = HVACMode.COOL
        elif avg_delta < -0.2:
            candidate = HVACMode.HEAT

        outside_temp = self._read_entity_float(self._outside_temp_sensor())
        if self._outside_temp_sensor() is not None and outside_temp is None:
            self._last_auto_effective_mode = HVACMode.OFF
            return self._last_auto_effective_mode
        heat_locked = (
            outside_temp is not None
            and outside_temp
            > float(
                self._entry_config.get(
                    CONF_AUTO_HEAT_LOCKOUT_ABOVE_C,
                    DEFAULT_AUTO_HEAT_LOCKOUT_ABOVE_C,
                )
            )
        )
        cool_locked = (
            outside_temp is not None
            and outside_temp
            < float(
                self._entry_config.get(
                    CONF_AUTO_COOL_LOCKOUT_BELOW_C,
                    DEFAULT_AUTO_COOL_LOCKOUT_BELOW_C,
                )
            )
        )

        if candidate == HVACMode.OFF:
            self._last_auto_effective_mode = HVACMode.OFF
            return self._last_auto_effective_mode

        if (candidate == HVACMode.HEAT and heat_locked) or (
            candidate == HVACMode.COOL and cool_locked
        ):
            self._last_auto_effective_mode = HVACMode.OFF
            return self._last_auto_effective_mode

        if candidate == self._last_auto_mode:
            self._last_auto_effective_mode = self._last_auto_mode
            return self._last_auto_effective_mode

        current_mode_locked = (
            self._last_auto_mode == HVACMode.HEAT and heat_locked
        ) or (
            self._last_auto_mode == HVACMode.COOL and cool_locked
        )
        now = datetime.now(timezone.utc)
        minimum_changeover = timedelta(
            minutes=max(
                0,
                int(
                    self._entry_config.get(
                        CONF_AUTO_MIN_CHANGEOVER_MINUTES,
                        DEFAULT_AUTO_MIN_CHANGEOVER_MINUTES,
                    )
                ),
            )
        )
        if (
            not current_mode_locked
            and self._last_auto_mode_change is not None
            and now - self._last_auto_mode_change < minimum_changeover
        ):
            # Wait idle instead of continuing in the direction opposite to demand.
            self._last_auto_effective_mode = HVACMode.OFF
            return self._last_auto_effective_mode

        self._last_auto_mode = candidate
        self._last_auto_effective_mode = candidate
        self._last_auto_mode_change = now

        return self._last_auto_effective_mode

    def _relevant_statuses(self) -> list[dict[str, Any]]:
        """Return active zone statuses, or all zones when none are active."""
        active_statuses = [
            status for status in self._zone_status.values() if status.get("active")
        ]
        return active_statuses or list(self._zone_status.values())

    def _forecast_outside_temp(self) -> float | None:
        """Return forecasted outside temperature used by optional preheat boost."""
        flow_config = self._flow_config()
        if not bool(flow_config.get(CONF_PRO_PREHEAT_ENABLED, False)):
            return None

        forecast_sensor = flow_config.get(CONF_PRO_PREHEAT_FORECAST_SENSOR)
        if not forecast_sensor:
            return None

        return self._read_entity_float(forecast_sensor)

    def _forecast_solar_irradiance(self) -> float | None:
        """Return forecast solar irradiance used to soften preheat before sun gains."""
        flow_config = self._flow_config()
        if not bool(flow_config.get(CONF_PRO_PREHEAT_ENABLED, False)):
            return None

        solar_sensor = flow_config.get(CONF_PRO_PREHEAT_SOLAR_SENSOR)
        if not solar_sensor:
            return None

        value = self._read_entity_float(solar_sensor)
        return None if value is None else max(0.0, value)

    def _read_entity_float(self, entity_id: str | None) -> float | None:
        """Return a float state value for an entity, if available."""
        if not entity_id:
            return None

        states = getattr(self._hass, "states", None)
        if states is None:
            return None
        state = states.get(entity_id)
        if not state:
            return None

        try:
            value = float(state.state)
            if not math.isfinite(value):
                raise ValueError("value is not finite")
            return value
        except (TypeError, ValueError):
            _LOGGER.warning(
                "%s: Invalid numeric value from %s: %s",
                DOMAIN,
                entity_id,
                state.state,
            )
            return None

    def _cooling_dewpoint(self) -> tuple[str | None, float | None]:
        """Return configured indoor dewpoint sensor and value."""
        sensor = self._flow_config().get(CONF_COOLING_DEWPOINT_SENSOR)
        return sensor, self._read_entity_float(sensor)

    def is_air_coil_only_override_enabled(self) -> bool:
        """Return True when manual air-coil-only override is enabled."""
        return self._zone_strategy_value == "air_coil_only"

    def _cooling_weather_comp(self, outside_temp: float | None) -> tuple[float, float, float]:
        """Return base offset, weather slope and weather compensation for cooling."""
        base_offset = float(
            self._entry_config.get(
                CONF_COOLING_BASE_OFFSET,
                DEFAULT_COOLING_BASE_OFFSET,
            )
        )
        slope = float(
            self._entry_config.get(
                CONF_WEATHER_SLOPE_COOL,
                DEFAULT_WEATHER_SLOPE_COOL,
            )
        )
        weather_comp = 0.0
        if outside_temp is not None:
            weather_comp = max(0.0, outside_temp - 24.0) * max(0.0, slope)
        return base_offset, slope, weather_comp

    def _floor_cooling_margin(
        self,
        status: dict[str, Any],
        default_floor_margin: float,
    ) -> float:
        """Return dewpoint margin for a floor zone."""
        margin = status.get("cooling_dewpoint_margin")
        if margin is None:
            margin = default_floor_margin
            if status.get("floor_material") == FLOOR_MATERIAL_WOOD:
                margin += 1.0
        return float(margin)

    def _cooling_unprotected_flow_for_target(
        self,
        target: float,
        outside_temp: float | None,
    ) -> float:
        """Return cooling flow before any floor dewpoint protection."""
        base_offset, _, weather_comp = self._cooling_weather_comp(outside_temp)
        return target - (base_offset + weather_comp) - self.get_flow_curve_offset()

    def _cooling_candidate_flow_with_floor(
        self,
        *,
        candidate_status: dict[str, Any] | None = None,
        outside_temp: float | None = None,
    ) -> float | None:
        """Return flow if floor demand participates, without air-coil-only boost."""
        demand_statuses = [
            status
            for status in self._zone_status.values()
            if bool(status.get("active"))
            or float(status.get("current", 0.0)) > float(status.get("target", 0.0))
        ]
        if candidate_status is not None:
            demand_statuses.append(candidate_status)

        if not demand_statuses:
            return None

        base_offset, _, weather_comp = self._cooling_weather_comp(outside_temp)
        min_target = min(float(status["target"]) for status in demand_statuses)
        unclamped = min_target - (base_offset + weather_comp) - self.get_flow_curve_offset()
        air_coil_min_flow = float(
            self._flow_config().get(
                CONF_COOLING_DEHUMIDIFICATION_MIN_FLOW,
                DEFAULT_COOLING_DEHUMIDIFICATION_MIN_FLOW,
            )
        )
        return max(air_coil_min_flow, min(25.0, unclamped))

    def _air_coil_cooling_statuses(self) -> list[dict[str, Any]]:
        """Return air-coil statuses that currently ask for cooling."""
        return [
            status
            for status in self._zone_status.values()
            if status.get("zone_type") == ZONE_TYPE_AIR_COIL
            and (
                bool(status.get("active"))
                or float(status.get("current", 0.0)) > float(status.get("target", 0.0))
            )
        ]

    def _air_coil_cooling_flow(self, outside_temp: float | None) -> float | None:
        """Return flow target for current air-coil cooling demand."""
        statuses = self._air_coil_cooling_statuses()
        if not statuses:
            return None

        flow_config = self._flow_config()
        base_offset, _, weather_comp = self._cooling_weather_comp(outside_temp)
        air_coil_only_offset = float(
            flow_config.get(
                CONF_COOLING_AIR_COIL_ONLY_OFFSET,
                DEFAULT_COOLING_AIR_COIL_ONLY_OFFSET,
            )
        )
        air_coil_min_flow = float(
            flow_config.get(
                CONF_COOLING_DEHUMIDIFICATION_MIN_FLOW,
                DEFAULT_COOLING_DEHUMIDIFICATION_MIN_FLOW,
            )
        )
        min_target = min(float(status["target"]) for status in statuses)
        unclamped = min_target - (base_offset + weather_comp + air_coil_only_offset) - self.get_flow_curve_offset()
        return max(air_coil_min_flow, min(25.0, unclamped))

    def should_suppress_floor_cooling(
        self,
        *,
        zone_type: str,
        target: float,
        current: float,
        floor_material: str | None,
        cooling_dewpoint_margin: float | None,
        cooling_dewpoint_protection: bool = True,
    ) -> bool:
        """Return True when a floor zone should stay closed during cooling."""
        if zone_type == ZONE_TYPE_AIR_COIL:
            return False
        if self.is_air_coil_only_override_enabled():
            return True
        if not cooling_dewpoint_protection:
            return False

        dewpoint_sensor, dewpoint = self._cooling_dewpoint()
        if dewpoint is None:
            return dewpoint_sensor is not None

        outside_temp = self._read_entity_float(self._outside_temp_sensor())
        air_coil_flow = self._air_coil_cooling_flow(outside_temp)
        default_margin = float(
            self._flow_config().get(
                CONF_COOLING_FLOOR_DEWPOINT_MARGIN,
                DEFAULT_COOLING_FLOOR_DEWPOINT_MARGIN,
            )
        )
        floor_statuses = [
            status
            for status in self._zone_status.values()
            if status.get("zone_type") != ZONE_TYPE_AIR_COIL
            and bool(status.get("cooling_dewpoint_protection", True))
            and not bool(status.get("cooling_suppressed"))
            and (
                bool(status.get("active"))
                or float(status.get("current", 0.0))
                > float(status.get("target", 0.0))
            )
        ]
        if current > target:
            floor_statuses.append(
                {
                    "floor_material": floor_material,
                    "cooling_dewpoint_margin": cooling_dewpoint_margin,
                }
            )
        floor_margin = max(
            (
                self._floor_cooling_margin(status, default_margin)
                for status in floor_statuses
            ),
            default=default_margin,
        )
        floor_constraint = dewpoint + floor_margin
        if floor_constraint > 25.0:
            return True
        return air_coil_flow is not None and air_coil_flow < floor_constraint

    def _determine_cooling_flow_temperature_with_factors(
        self,
        *,
        outside_temp: float | None,
        statuses: list[dict[str, Any]],
    ) -> tuple[float, dict[str, Any]]:
        """Return (flow_temp, breakdown) for the cooling curve."""

        flow_curve_offset = self.get_flow_curve_offset()
        flow_config = self._flow_config()
        air_coil_min_flow = float(
            flow_config.get(
                CONF_COOLING_DEHUMIDIFICATION_MIN_FLOW,
                DEFAULT_COOLING_DEHUMIDIFICATION_MIN_FLOW,
            )
        )
        clamp_min = air_coil_min_flow
        clamp_max = 25.0

        active_demand_statuses = [
            status
            for status in statuses
            if not bool(status.get("cooling_suppressed"))
            and (
                bool(status.get("active"))
                or float(status.get("current", 0.0)) > float(status.get("target", 0.0))
            )
        ]
        reference_statuses = active_demand_statuses or statuses

        floor_statuses = [
            status
            for status in active_demand_statuses
            if status.get("zone_type") != ZONE_TYPE_AIR_COIL
        ]
        protected_floor_statuses = [
            status
            for status in floor_statuses
            if bool(status.get("cooling_dewpoint_protection", True))
        ]
        air_coil_statuses = [
            status
            for status in active_demand_statuses
            if status.get("zone_type") == ZONE_TYPE_AIR_COIL
        ]

        air_coil_only_override = self.is_air_coil_only_override_enabled()
        if air_coil_only_override and air_coil_statuses:
            floor_statuses = []
            active_demand_statuses = list(air_coil_statuses)
            reference_statuses = active_demand_statuses

        dewpoint_sensor, dewpoint = self._cooling_dewpoint()
        air_coil_only_offset = float(
            flow_config.get(
                CONF_COOLING_AIR_COIL_ONLY_OFFSET,
                DEFAULT_COOLING_AIR_COIL_ONLY_OFFSET,
            )
        )
        default_floor_margin = float(
            flow_config.get(
                CONF_COOLING_FLOOR_DEWPOINT_MARGIN,
                DEFAULT_COOLING_FLOOR_DEWPOINT_MARGIN,
            )
        )
        floor_margin = default_floor_margin
        floor_constraint = None
        suppressed_floor_zone_names = sorted(
            status_name
            for status_name, status in self._zone_status.items()
            if bool(status.get("cooling_suppressed"))
        )
        if protected_floor_statuses and dewpoint is not None:
            zone_margins = [
                self._floor_cooling_margin(status, default_floor_margin)
                for status in protected_floor_statuses
            ]
            if zone_margins:
                floor_margin = max(zone_margins)
                floor_constraint = dewpoint + floor_margin

        min_target = min(float(status["target"]) for status in reference_statuses)

        base_offset, slope, weather_comp = self._cooling_weather_comp(outside_temp)
        floor_candidate_flow = self._cooling_candidate_flow_with_floor(
            outside_temp=outside_temp,
        )
        floor_candidate_statuses = [
            status
            for status in statuses
            if status.get("zone_type") != ZONE_TYPE_AIR_COIL
            and bool(status.get("cooling_dewpoint_protection", True))
            and (
                bool(status.get("active"))
                or bool(status.get("cooling_suppressed"))
                or float(status.get("current", 0.0)) > float(status.get("target", 0.0))
            )
        ]
        floor_candidate_constraint = None
        if floor_candidate_statuses and dewpoint is not None:
            floor_candidate_constraint = dewpoint + max(
                self._floor_cooling_margin(status, default_floor_margin)
                for status in floor_candidate_statuses
            )
        effective_base_offset = base_offset + weather_comp
        air_coil_only_mode = bool(air_coil_statuses) and not bool(floor_statuses)
        if air_coil_only_mode:
            effective_base_offset += air_coil_only_offset
        unclamped = min_target - effective_base_offset - flow_curve_offset
        flow = max(clamp_min, min(clamp_max, unclamped))
        if floor_constraint is not None:
            flow = max(flow, floor_constraint)

        active_floor_zone_names = sorted(
            status_name
            for status_name, status in self._zone_status.items()
            if status in floor_statuses
        )
        active_air_coil_zone_names = sorted(
            status_name
            for status_name, status in self._zone_status.items()
            if status in air_coil_statuses
        )
        return (
            flow,
            {
                "target_ref_c": round(min_target, 3),
                "base_offset_c": round(base_offset, 3),
                "weather_slope": round(slope, 6),
                "weather_comp_c": round(weather_comp, 3),
                "effective_base_offset_c": round(effective_base_offset, 3),
                "cooling_air_coil_only_offset_c": round(air_coil_only_offset, 3),
                "cooling_air_coil_only_mode": air_coil_only_mode,
                "cooling_air_coil_only_override": air_coil_only_override,
                "flow_curve_offset_c": round(flow_curve_offset, 3),
                "flow_temp_unclamped_c": round(unclamped, 3),
                "flow_temp_c": round(flow, 1),
                "clamp_min_c": round(clamp_min, 3),
                "clamp_max_c": clamp_max,
                "cooling_dewpoint_sensor": dewpoint_sensor,
                "cooling_dewpoint_c": None if dewpoint is None else round(dewpoint, 3),
                "cooling_floor_margin_c": round(floor_margin, 3),
                "cooling_floor_constraint_c": (
                    None if floor_constraint is None else round(floor_constraint, 3)
                ),
                "cooling_floor_candidate_flow_c": (
                    None if floor_candidate_flow is None else round(floor_candidate_flow, 3)
                ),
                "cooling_floor_candidate_constraint_c": (
                    None
                    if floor_candidate_constraint is None
                    else round(floor_candidate_constraint, 3)
                ),
                "cooling_air_coil_min_flow_c": round(air_coil_min_flow, 3),
                "cooling_has_floor_demand": bool(floor_statuses),
                "cooling_has_air_coil_demand": bool(air_coil_statuses),
                "cooling_active_floor_zones": active_floor_zone_names,
                "cooling_active_air_coil_zones": active_air_coil_zone_names,
                "cooling_suppressed_floor_zones": suppressed_floor_zone_names,
                "cooling_demand_zone_count": len(active_demand_statuses),
            },
        )

    def _determine_supervised_heating_flow_temperature_with_factors(
        self,
        *,
        outside_temp: float | None,
    ) -> tuple[float, dict[str, Any]]:
        """Return (flow_temp, breakdown) for the supervisor heating strategy."""

        base_offset = float(
            self._entry_config.get(
                CONF_HEATING_BASE_OFFSET,
                DEFAULT_HEATING_BASE_OFFSET,
            )
        )
        weather_slope = float(
            self._entry_config.get(
                CONF_WEATHER_SLOPE_HEAT,
                DEFAULT_WEATHER_SLOPE_HEAT,
            )
        )
        flow_config = self._flow_config()
        forecast_outside_temp = self._forecast_outside_temp()
        forecast_solar_irradiance = self._forecast_solar_irradiance()

        flow, breakdown = self._flow_supervisor.compute_heating_flow_with_breakdown(
            zone_status=self._zone_status,
            outside_temp=outside_temp,
            forecast_outside_temp=forecast_outside_temp,
            forecast_solar_irradiance=forecast_solar_irradiance,
            base_offset=base_offset,
            weather_slope=weather_slope,
            flow_curve_offset=self.get_flow_curve_offset(),
            config=flow_config,
        )

        breakdown.update(
            {
                "base_offset_c": round(base_offset, 3),
                "weather_slope": round(weather_slope, 6),
                "forecast_outside_temp_c": (
                    None if forecast_outside_temp is None else round(forecast_outside_temp, 3)
                ),
                "forecast_solar_irradiance_w_m2": (
                    None
                    if forecast_solar_irradiance is None
                    else round(forecast_solar_irradiance, 3)
                ),
                "flow_curve_offset_c": round(self.get_flow_curve_offset(), 3),
            }
        )
        return flow, breakdown

    def determine_flow_temperature_with_factors(
        self, effective_mode: HVACMode, outside_temp: float | None
    ) -> tuple[float, dict[str, Any]]:
        """Return (desired flow temperature, breakdown attributes).

        This is used by the Flow Temperature sensor to surface explainable
        components of the computed supply temperature.
        """

        statuses = self._relevant_statuses()
        mode_value = effective_mode.value if hasattr(effective_mode, "value") else str(effective_mode)
        common: dict[str, Any] = {
            "effective_mode": str(mode_value),
            "outside_temp_c": None if outside_temp is None else round(float(outside_temp), 3),
        }

        if not statuses:
            default_flow = 30.0 if effective_mode != HVACMode.COOL else 20.0
            clamp_min = 14.0 if effective_mode == HVACMode.COOL else 15.0
            clamp_max = 25.0 if effective_mode == HVACMode.COOL else 35.0
            breakdown = {
                **common,
                "target_ref_c": None,
                "flow_curve_offset_c": round(self.get_flow_curve_offset(), 3),
                "flow_temp_unclamped_c": round(default_flow, 3),
                "flow_temp_c": round(default_flow, 1),
                "clamp_min_c": clamp_min,
                "clamp_max_c": clamp_max,
            }
            return default_flow, breakdown

        if effective_mode == HVACMode.COOL:
            flow, breakdown = self._determine_cooling_flow_temperature_with_factors(
                outside_temp=outside_temp,
                statuses=statuses,
            )
            return flow, {**common, **breakdown}

        flow, breakdown = self._determine_supervised_heating_flow_temperature_with_factors(
            outside_temp=outside_temp,
        )
        return flow, {**common, **breakdown}

    def determine_flow_temperature(
        self, effective_mode: HVACMode, outside_temp: float | None
    ) -> float:
        """Return desired flow temperature based on active strategy."""
        flow, _ = self.determine_flow_temperature_with_factors(
            effective_mode,
            outside_temp,
        )
        return flow

    def register_thermostat(self, thermostat: ThermozonaThermostat) -> None:
        """Register a thermostat for notifications."""
        self._thermostats.add(thermostat)
        if (
            getattr(thermostat, "control_mode", None) == CONTROL_MODE_PWM
            and thermostat not in self._pwm_zone_indices
        ):
            self._pwm_zone_indices[thermostat] = self._next_pwm_zone_index
            self._next_pwm_zone_index += 1
        self._hass.async_create_task(thermostat.async_update_mode_listener())

    def unregister_thermostat(self, thermostat: ThermozonaThermostat) -> None:
        """Unregister a thermostat."""
        self._thermostats.discard(thermostat)

    def get_pwm_zone_info(self, thermostat: ThermozonaThermostat) -> tuple[int, int]:
        """Return deterministic PWM staggering index and active PWM zone count."""
        if getattr(thermostat, "control_mode", None) != CONTROL_MODE_PWM:
            return (0, 0)

        zone_index = self._pwm_zone_indices.get(thermostat)
        if zone_index is None:
            return (0, 0)

        zone_count = sum(
            1
            for entity in self._thermostats
            if getattr(entity, "control_mode", None) == CONTROL_MODE_PWM
            and entity in self._pwm_zone_indices
        )
        return (zone_index, zone_count)

    def _notify_thermostats(
        self, *, skip: ThermozonaThermostat | None = None
    ) -> None:
        """Ask all thermostats (except the source) to re-evaluate control."""
        if self._shutting_down:
            return
        for thermostat in list(self._thermostats):
            if thermostat is skip:
                continue
            thermostat.async_schedule_control()

    async def async_update_heat_pump_state(self, *, force_idle: bool = False) -> None:
        """Update the heat pump switch and flow temperature based on circuit state."""
        if self._shutting_down:
            self._last_any_circuit_on = None
            self._update_pump_status(False, None)
            self._mark_flow_command_inactive()
            return
        try:
            circuits = self.get_all_circuit_entities()
            _LOGGER.debug("%s: Checking circuits for heat pump control: %s", DOMAIN, circuits)

            any_circuit_on = False
            for entity_id in circuits:
                state = self._hass.states.get(entity_id)
                if state and state.state == "on":
                    any_circuit_on = True
                    _LOGGER.debug("%s: Circuit %s is active", DOMAIN, entity_id)
                    break

            now = datetime.now(timezone.utc)

            if any_circuit_on:
                if self._demand_off_task is not None:
                    self._demand_off_task.cancel()
                    self._demand_off_task = None
                self._last_any_circuit_on = now
                effective_mode = await self._async_set_flow_temperature()
                self._update_pump_status(True, effective_mode)
                if effective_mode is None:
                    self._mark_flow_command_inactive()
            elif (
                not force_idle
                and self.get_operation_mode() != "off"
                and self._last_any_circuit_on is not None
                and now - self._last_any_circuit_on < self._demand_off_delay
            ):
                _LOGGER.debug(
                    "%s: All circuits off, but within %s delay — keeping demand",
                    DOMAIN,
                    self._demand_off_delay,
                )
                self._schedule_demand_off_recheck(now)
            else:
                if self._demand_off_task is not None:
                    self._demand_off_task.cancel()
                    self._demand_off_task = None
                self._last_any_circuit_on = None
                self._update_pump_status(False, None)
                self._mark_flow_command_inactive()
        except Exception as exc:  # pragma: no cover - defensive logging
            _LOGGER.error("%s: Error updating heat pump state: %s", DOMAIN, exc)
            self._last_any_circuit_on = None
            self._update_pump_status(False, None)
            self._mark_flow_command_inactive()

    def _schedule_demand_off_recheck(self, now: datetime) -> None:
        """Re-evaluate when the demand off-delay expires."""
        if self._last_any_circuit_on is None or self._demand_off_task is not None:
            return

        remaining = self._demand_off_delay - (now - self._last_any_circuit_on)
        delay_seconds = max(1.0, remaining.total_seconds())

        async def _recheck() -> None:
            try:
                await asyncio.sleep(delay_seconds)
                self._demand_off_task = None
                await self.async_update_heat_pump_state()
            except asyncio.CancelledError:
                pass

        self._demand_off_task = self._hass.async_create_task(_recheck())

    async def _async_set_flow_temperature(self) -> HVACMode | None:
        """Calculate and set the flow temperature using the weather-compensation curve."""
        outside_sensor = self._outside_temp_sensor()
        has_target_entity = self._flow_temp_entity() or self._flow_temp_number()

        if not outside_sensor or not has_target_entity:
            _LOGGER.debug(
                "%s: Flow temperature update skipped, missing config", DOMAIN
            )
            return None

        outside_temp = self._read_entity_float(outside_sensor)
        if outside_temp is None:
            _LOGGER.error(
                "Outside temperature sensor %s has no valid finite value",
                outside_sensor,
            )

        operation = self.get_operation_mode()
        if operation == "off":
            _LOGGER.debug("%s: Heat pump mode off, skipping flow update", DOMAIN)
            self._mark_flow_command_inactive()
            return None

        if operation == "cool":
            effective_mode = HVACMode.COOL
        elif operation == "heat":
            effective_mode = HVACMode.HEAT
        else:
            effective_mode = self.determine_auto_mode()

        if effective_mode == HVACMode.OFF:
            self._mark_flow_command_inactive()
            return None

        if effective_mode == HVACMode.COOL:
            dewpoint_sensor, dewpoint = self._cooling_dewpoint()
            active_floor = any(
                status.get("zone_type") != ZONE_TYPE_AIR_COIL
                and bool(status.get("cooling_dewpoint_protection", True))
                and bool(status.get("active"))
                and not bool(status.get("cooling_suppressed"))
                for status in self._zone_status.values()
            )
            if dewpoint_sensor is not None and dewpoint is None and active_floor:
                _LOGGER.warning(
                    "%s: Cooling flow command blocked because dewpoint sensor %s "
                    "is unavailable while a floor zone is active",
                    DOMAIN,
                    dewpoint_sensor,
                )
                self._mark_flow_command_inactive()
                return None

        flow_temp, flow_factors = self.determine_flow_temperature_with_factors(
            effective_mode,
            outside_temp,
        )
        now = datetime.now(timezone.utc)
        flow_factors.update(
            {
                "command_active": True,
                "command_updated_at": now.isoformat(),
                "active_zones": sorted(
                    zone_name
                    for zone_name, status in self._zone_status.items()
                    if bool(status.get("active"))
                    and not bool(status.get("cooling_suppressed"))
                ),
            }
        )

        _LOGGER.debug(
            "%s: Setting flow temperature to %.1f°C (mode=%s, outside=%s)",
            DOMAIN,
            flow_temp,
            effective_mode,
            outside_temp,
        )

        self._last_flow_temp = flow_temp
        self._last_flow_factors = flow_factors
        if (sensor_entity := self._flow_temp_sensor()) is not None:
            sensor_entity.set_calculated_value(flow_temp)

        if not self._should_dispatch_flow_temperature(flow_temp=flow_temp, now=now):
            _LOGGER.debug(
                "%s: Suppressing flow setpoint write %.2f°C due to deadband/interval policy",
                DOMAIN,
                flow_temp,
            )
            return effective_mode

        if (number_entity := self._flow_temp_number()) is not None:
            number_entity.set_calculated_value(flow_temp)
            self._last_flow_write_temp = flow_temp
            self._last_flow_write_time = now
            return effective_mode

        if flow_temp_entity := self._flow_temp_entity():
            await self._hass.services.async_call(
                "input_number",
                "set_value",
                {"entity_id": flow_temp_entity, "value": flow_temp},
                blocking=True,
            )
            self._last_flow_write_temp = flow_temp
            self._last_flow_write_time = now
        return effective_mode

    async def async_shutdown(self) -> None:
        """Close all circuits and clear demand before unload or reload."""
        self._shutting_down = True
        await self.price_charging.async_stop()
        if self._demand_off_task is not None:
            self._demand_off_task.cancel()
            self._demand_off_task = None

        for thermostat in list(self._thermostats):
            await thermostat.async_prepare_shutdown()

        for entity_id in self.get_all_circuit_entities():
            domain, _, _ = entity_id.partition(".")
            if domain not in {"input_boolean", "switch"}:
                continue
            try:
                await self._hass.services.async_call(
                    domain,
                    "turn_off",
                    {"entity_id": entity_id},
                    blocking=True,
                )
            except Exception as exc:  # pragma: no cover - defensive logging
                _LOGGER.error(
                    "%s: Failed to close circuit %s during unload: %s",
                    DOMAIN,
                    entity_id,
                    exc,
                )

        self._zone_status.clear()
        self._last_any_circuit_on = None
        self._update_pump_status(False, None)
        self._mark_flow_command_inactive()

    def refresh_entry_config(self, entry_config: dict[str, Any]) -> None:
        """Update internal reference to the config entry (for reload scenarios)."""
        self._entry_config = entry_config
        self.price_charging = PriceChargingManager(
            self._hass, entry_config.get(CONF_PRICE_CHARGING, {}), self._notify_thermostats
        )
        self._flow_supervisor.reset()
        self._last_flow_write_temp = None
        self._last_flow_write_time = None
        self.reset_flow_curve_offset()
