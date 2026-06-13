"""Heat pump controller for the Thermozona integration."""
from __future__ import annotations

import logging
import weakref
from datetime import datetime, timedelta, timezone
from typing import Any, TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.components.climate import HVACMode

from . import (
    CONTROL_MODE_PWM,
    CONF_FLOW_CURVE_OFFSET,
    CONF_FLOW_TEMP_SENSOR,
    CONF_HEAT_PUMP_MODE,
    CONF_FLOW,
    CONF_COOLING_BASE_OFFSET,
    CONF_HEATING_BASE_OFFSET,
    CONF_PRO_PREHEAT_ENABLED,
    CONF_PRO_PREHEAT_FORECAST_SENSOR,
    CONF_PRO_PREHEAT_SOLAR_SENSOR,
    CONF_OUTSIDE_TEMP_SENSOR,
    CONF_WEATHER_SLOPE_COOL,
    CONF_WEATHER_SLOPE_HEAT,
    CONF_WRITE_DEADBAND_C,
    CONF_WRITE_MIN_INTERVAL_MINUTES,
    CONF_ZONES,
    DEFAULT_COOLING_BASE_OFFSET,
    DEFAULT_FLOW_CURVE_OFFSET,
    DEFAULT_HEATING_BASE_OFFSET,
    DEFAULT_PRO_WRITE_DEADBAND_C,
    DEFAULT_PRO_WRITE_MIN_INTERVAL_MINUTES,
    DEFAULT_WEATHER_SLOPE_COOL,
    DEFAULT_WEATHER_SLOPE_HEAT,
    DOMAIN,
)
from .helpers import resolve_circuits
from .pro.flow_curve import FlowCurveRuntimeManager
from .pro.flow_supervisor import ProFlowSupervisor

if TYPE_CHECKING:
    from .thermostat import ThermozonaThermostat
    from .number import (
        ThermozonaFlowTemperatureNumber,
    )
    from .pro.number import ThermozonaFlowCurveOffsetNumber
    from .select import ThermozonaHeatPumpModeSelect
    from .sensor import ThermozonaFlowTemperatureSensor, ThermozonaHeatPumpStatusSensor

_LOGGER = logging.getLogger(__name__)


class HeatPumpController:
    """Coordinate state updates for the shared heat pump."""

    def __init__(self, hass: HomeAssistant, entry_config: dict[str, Any]) -> None:
        self._hass = hass
        self._entry_config = entry_config
        self._zone_status: dict[str, dict[str, Any]] = {}
        self._last_auto_mode: HVACMode = HVACMode.HEAT
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
        self._last_flow_factors: dict[str, Any] = {}
        self._last_flow_write_temp: float | None = None
        self._last_flow_write_time: datetime | None = None
        self._mode_select: weakref.ReferenceType[
            ThermozonaHeatPumpModeSelect
        ] | None = None
        self._mode_value: str = "auto"
        self._mode_entity_id: str | None = None
        self._pump_state: str = "idle"
        self._last_any_circuit_on: datetime | None = None
        self._demand_off_delay = timedelta(minutes=5)
        self._flow_curve_runtime = FlowCurveRuntimeManager(
            get_yaml_value=self._get_yaml_flow_curve_offset,
            notify_thermostats=lambda: self._notify_thermostats(),
        )
        self._flow_supervisor = ProFlowSupervisor()

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
        if self._last_flow_temp is not None:
            entity.set_calculated_value(self._last_flow_temp)

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

    def register_mode_select(
        self, entity: ThermozonaHeatPumpModeSelect
    ) -> None:
        """Register internal select entity to control heat pump mode."""
        self._mode_select = weakref.ref(entity)
        self._mode_entity_id = entity.entity_id
        entity.update_current_option(self._mode_value)
        for thermostat in list(self._thermostats):
            self._hass.async_create_task(thermostat.async_update_mode_listener())

    def unregister_mode_select(
        self, entity: ThermozonaHeatPumpModeSelect
    ) -> None:
        """Unregister the internal select entity when removed."""
        if self._mode_select is not None and self._mode_select() is entity:
            self._mode_select = None
            self._mode_entity_id = None
            for thermostat in list(self._thermostats):
                self._hass.async_create_task(thermostat.async_update_mode_listener())

    def set_mode_value(self, value: str) -> None:
        """Store a new mode value coming from the select entity."""
        normalized = value.lower()
        if normalized not in {"auto", "heat", "cool", "off"}:
            _LOGGER.warning("%s: Invalid mode '%s', defaulting to auto", DOMAIN, value)
            normalized = "auto"

        previous = self._mode_value

        if normalized == "heat":
            self._last_auto_mode = HVACMode.HEAT
        elif normalized == "cool":
            self._last_auto_mode = HVACMode.COOL

        if normalized == previous:
            if (select := self._mode_select_entity()) is not None:
                select.update_current_option(normalized)
            return

        self._mode_value = normalized

        if (select := self._mode_select_entity()) is not None:
            select.update_current_option(normalized)

        _LOGGER.debug("%s: Heat pump mode set to %s", DOMAIN, normalized)
        self._notify_thermostats()

    def get_all_circuit_entities(self) -> list[str]:
        """Return all circuit entities across the configured zones."""
        circuits: list[str] = []
        for zone_config in self._entry_config.get(CONF_ZONES, {}).values():
            circuits.extend(resolve_circuits(zone_config))
        return circuits

    def get_operation_mode(self) -> str:
        """Return the current heat pump operation mode (heat/cool/auto)."""
        if self._mode_select_entity() is not None:
            return self._mode_value

        mode_entity = self._entry_config.get(CONF_HEAT_PUMP_MODE)
        if not mode_entity:
            return "auto"

        state = self._hass.states.get(mode_entity)
        if not state:
            _LOGGER.warning("%s: Heat pump mode entity %s not found", DOMAIN, mode_entity)
            return "auto"

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
            "%s: Heat pump mode %s unknown (state=%s), defaulting to auto",
            DOMAIN,
            mode_entity,
            state.state,
        )
        return "auto"

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
        zone_solar_weight: float | None = None,
        source: ThermozonaThermostat | None = None,
    ) -> None:
        """Store latest temperature/target info for the zone."""
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
            if zone_solar_weight is not None:
                entry["zone_solar_weight"] = max(0.0, float(zone_solar_weight))

        if active is not None and zone_name in self._zone_status:
            self._zone_status[zone_name]["active"] = bool(active)

        if self.get_operation_mode() == "auto":
            previous_mode = self._last_auto_mode
            new_mode = self.determine_auto_mode()
            if new_mode != previous_mode:
                _LOGGER.debug(
                    "%s: Auto mode changed from %s to %s after %s update",
                    DOMAIN,
                    previous_mode,
                    new_mode,
                    zone_name,
                )
                self._notify_thermostats(skip=source)

    def determine_auto_mode(self) -> HVACMode:
        """Decide between heating or cooling when pump is in auto mode."""
        if not self._zone_status:
            self._last_auto_mode = HVACMode.HEAT
            return self._last_auto_mode

        deltas: list[float] = []
        for status in self._zone_status.values():
            deltas.append(status["current"] - status["target"])

        if not deltas:
            self._last_auto_mode = HVACMode.HEAT
            return self._last_auto_mode

        avg_delta = sum(deltas) / len(deltas)
        # Small deadband to avoid rapid toggling around the setpoint
        if avg_delta > 0.2:
            self._last_auto_mode = HVACMode.COOL
        elif avg_delta < -0.2:
            self._last_auto_mode = HVACMode.HEAT

        return self._last_auto_mode

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

        forecast_state = self._hass.states.get(forecast_sensor)
        if not forecast_state:
            _LOGGER.debug(
                "%s: Forecast sensor %s not found, skipping preheat",
                DOMAIN,
                forecast_sensor,
            )
            return None

        try:
            return float(forecast_state.state)
        except (TypeError, ValueError):
            _LOGGER.warning(
                "%s: Invalid forecast temperature value from %s: %s",
                DOMAIN,
                forecast_sensor,
                forecast_state.state,
            )
            return None

    def _forecast_solar_irradiance(self) -> float | None:
        """Return forecast solar irradiance used to soften preheat before sun gains."""
        flow_config = self._flow_config()
        if not bool(flow_config.get(CONF_PRO_PREHEAT_ENABLED, False)):
            return None

        solar_sensor = flow_config.get(CONF_PRO_PREHEAT_SOLAR_SENSOR)
        if not solar_sensor:
            return None

        solar_state = self._hass.states.get(solar_sensor)
        if not solar_state:
            _LOGGER.debug(
                "%s: Solar forecast sensor %s not found, skipping solar adjustment",
                DOMAIN,
                solar_sensor,
            )
            return None

        try:
            return max(0.0, float(solar_state.state))
        except (TypeError, ValueError):
            _LOGGER.warning(
                "%s: Invalid solar irradiance value from %s: %s",
                DOMAIN,
                solar_sensor,
                solar_state.state,
            )
            return None

    def _determine_cooling_flow_temperature_with_factors(
        self,
        *,
        outside_temp: float | None,
        statuses: list[dict[str, Any]],
    ) -> tuple[float, dict[str, Any]]:
        """Return (flow_temp, breakdown) for the cooling curve."""

        flow_curve_offset = self.get_flow_curve_offset()
        clamp_min = 15.0
        clamp_max = 25.0

        min_target = min(float(status["target"]) for status in statuses)

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
        effective_base_offset = base_offset + weather_comp
        unclamped = min_target - effective_base_offset - flow_curve_offset
        flow = max(clamp_min, min(clamp_max, unclamped))
        return (
            flow,
            {
                "target_ref_c": round(min_target, 3),
                "base_offset_c": round(base_offset, 3),
                "weather_slope": round(slope, 6),
                "weather_comp_c": round(weather_comp, 3),
                "effective_base_offset_c": round(effective_base_offset, 3),
                "flow_curve_offset_c": round(flow_curve_offset, 3),
                "flow_temp_unclamped_c": round(unclamped, 3),
                "flow_temp_c": round(flow, 1),
                "clamp_min_c": clamp_min,
                "clamp_max_c": clamp_max,
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
            clamp_min = 15.0
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
        for thermostat in list(self._thermostats):
            if thermostat is skip:
                continue
            thermostat.async_schedule_control()

    async def async_update_heat_pump_state(self) -> None:
        """Update the heat pump switch and flow temperature based on circuit state."""
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
                self._last_any_circuit_on = now
                effective_mode = await self._async_set_flow_temperature()
                self._update_pump_status(True, effective_mode)
            elif (
                self._last_any_circuit_on is not None
                and now - self._last_any_circuit_on < self._demand_off_delay
            ):
                _LOGGER.debug(
                    "%s: All circuits off, but within %s delay — keeping demand",
                    DOMAIN,
                    self._demand_off_delay,
                )
            else:
                self._update_pump_status(False, None)
        except Exception as exc:  # pragma: no cover - defensive logging
            _LOGGER.error("%s: Error updating heat pump state: %s", DOMAIN, exc)

    async def _async_set_flow_temperature(self) -> HVACMode | None:
        """Calculate and set the flow temperature using the weather-compensation curve."""
        outside_sensor = self._outside_temp_sensor()
        has_target_entity = self._flow_temp_entity() or self._flow_temp_number()

        if not outside_sensor or not has_target_entity:
            _LOGGER.debug(
                "%s: Flow temperature update skipped, missing config", DOMAIN
            )
            return None

        outside_temp_state = self._hass.states.get(outside_sensor)
        if not outside_temp_state:
            _LOGGER.error("Outside temperature sensor not found: %s", outside_sensor)
            return None

        try:
            outside_temp = float(outside_temp_state.state)
        except (TypeError, ValueError):
            _LOGGER.error(
                "Invalid outside temperature value from %s: %s",
                outside_sensor,
                outside_temp_state.state,
            )
            outside_temp = None

        operation = self.get_operation_mode()
        if operation == "off":
            _LOGGER.debug("%s: Heat pump mode off, skipping flow update", DOMAIN)
            return None

        if operation == "cool":
            effective_mode = HVACMode.COOL
        elif operation == "heat":
            effective_mode = HVACMode.HEAT
        else:
            effective_mode = self.determine_auto_mode()

        flow_temp, flow_factors = self.determine_flow_temperature_with_factors(
            effective_mode,
            outside_temp,
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
        now = datetime.now(timezone.utc)

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

    def refresh_entry_config(self, entry_config: dict[str, Any]) -> None:
        """Update internal reference to the config entry (for reload scenarios)."""
        self._entry_config = entry_config
        self._flow_supervisor.reset()
        self._last_flow_write_temp = None
        self._last_flow_write_time = None
        self.reset_flow_curve_offset()
