"""Thermostat entity for the Thermozona integration."""
from __future__ import annotations

import asyncio
import logging
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import (
    ATTR_TEMPERATURE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity

from . import (
    CONTROL_MODE_BANG_BANG,
    CONTROL_MODE_PWM,
    DEFAULT_AIR_COIL_MEASURED_WATER_SENSOR,
    DEFAULT_AIR_COIL_REQUESTED_WATER_SENSOR,
    DEFAULT_PWM_ACTUATOR_DELAY,
    DEFAULT_PWM_CYCLE_TIME,
    DEFAULT_PWM_KI,
    DEFAULT_PWM_KP,
    DEFAULT_PWM_MIN_OFF_TIME,
    DEFAULT_PWM_MIN_ON_TIME,
    DEFAULT_ZONE_FLOW_WEIGHT,
    DEFAULT_ZONE_RESPONSE,
    DEFAULT_ZONE_SOLAR_WEIGHT,
    DOMAIN,
)
from .heat_pump import HeatPumpController
from .helpers import air_coil_heating_suppressed
from .room_recovery import RecoveryPlan, RoomRecovery
from .thermal_conditions import ThermalCompensation
from .pro.pwm import (
    calculate_on_time_minutes,
    calculate_pwm_duty,
    get_aligned_pwm_cycle_start,
    should_circuits_be_on,
)

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=1)
DEFAULT_HYSTERESIS = 0.3
PWM_INTEGRAL_KEY = "pwm_integral"
PWM_TARGET_RESET_DELTA = 2.0


class ThermozonaThermostat(ClimateEntity, RestoreEntity):
    """Representation of a Thermozona thermostat."""

    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = 5
    _attr_max_temp = 30
    _attr_target_temperature_step = 0.5

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        zone_name: str,
        circuits: list[str],
        temp_sensor: str | None,
        controller: HeatPumpController,
        hysteresis: float | None,
        control_mode: str | None,
        pwm_cycle_time: int | None,
        pwm_min_on_time: int | None,
        pwm_min_off_time: int | None,
        pwm_kp: float | None,
        pwm_ki: float | None,
        pwm_actuator_delay: int | None,
        zone_response: str | None = None,
        zone_flow_weight: float | None = None,
        zone_solar_weight: float | None = None,
        zone_type: str | None = None,
        floor_material: str | None = None,
        cooling_dewpoint_margin: float | None = None,
        cooling_dewpoint_protection: bool | None = None,
        heating_min_flow_c: float | None = None,
        air_coil_requested_water_sensor: str | None = None,
        air_coil_measured_water_sensor: str | None = None,
        price_charging: bool = False,
        setback_schedule: str | None = None,
        setback_c: float = 1.0,
        schedule_recovery: dict[str, Any] | None = None,
    ) -> None:
        """Initialize the thermostat."""
        self.hass = hass
        human_name = self._prettify(zone_name)
        slug_name = self._slugify(zone_name)
        self._attr_name = f"Thermozona {human_name}"
        self._attr_unique_id = f"thermozona_{slug_name}"
        self._zone_name = slug_name
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry_id)},
            "name": "Thermozona",
        }
        self._circuits = circuits
        self._temp_sensor = temp_sensor
        self._attr_target_temperature = 20
        self._attr_hvac_action = HVACAction.OFF
        self._remove_update_handler = None
        self._remove_mode_listener = None
        self._remove_temp_sensor_listener = None
        self._controller = controller
        self._pending_control = False
        self._reschedule_control = False
        self._control_task: asyncio.Task | None = None
        self._is_removing = False
        self._temperature_seen = False
        self._manual_mode: HVACMode = HVACMode.AUTO
        self._effective_mode: HVACMode = HVACMode.AUTO
        self._air_coil_heat_suppressed = False
        self._mode_listener_entity: str | None = None
        self._hysteresis: float = (
            hysteresis if hysteresis is not None else DEFAULT_HYSTERESIS
        )

        self._control_mode = control_mode or CONTROL_MODE_BANG_BANG
        self._pwm_cycle_time_minutes = pwm_cycle_time or DEFAULT_PWM_CYCLE_TIME
        self._pwm_min_on_time_minutes = pwm_min_on_time or DEFAULT_PWM_MIN_ON_TIME
        self._pwm_min_off_time_minutes = pwm_min_off_time or DEFAULT_PWM_MIN_OFF_TIME
        self._pwm_kp = pwm_kp if pwm_kp is not None else DEFAULT_PWM_KP
        self._pwm_ki = pwm_ki if pwm_ki is not None else DEFAULT_PWM_KI
        self._pwm_actuator_delay_minutes = (
            pwm_actuator_delay
            if pwm_actuator_delay is not None
            else DEFAULT_PWM_ACTUATOR_DELAY
        )
        self._pwm_zone_index = 0
        self._pwm_zone_count = 0
        self._zone_response = (zone_response or DEFAULT_ZONE_RESPONSE).lower()
        self._zone_flow_weight = (
            float(zone_flow_weight)
            if zone_flow_weight is not None
            else DEFAULT_ZONE_FLOW_WEIGHT
        )
        self._zone_flow_weight = max(0.0, self._zone_flow_weight)
        self._heating_min_flow_c = heating_min_flow_c
        self._zone_solar_weight = (
            float(zone_solar_weight)
            if zone_solar_weight is not None
            else DEFAULT_ZONE_SOLAR_WEIGHT
        )
        self._zone_solar_weight = max(0.0, self._zone_solar_weight)
        self._zone_type = (zone_type or "floor").lower()
        self._price_charging_enabled = price_charging
        self._last_price_boost = 0.0
        self._setback_schedule = setback_schedule
        self._setback_c = setback_c
        self._last_setback = 0.0
        self._recovery = (RoomRecovery(schedule_recovery) if schedule_recovery
                          and schedule_recovery.get("enabled") and setback_schedule else None)
        self._recovery_task: asyncio.Task | None = None
        self._pwm_thermal = ThermalCompensation()
        self._air_coil_requested_water_sensor = (
            air_coil_requested_water_sensor or DEFAULT_AIR_COIL_REQUESTED_WATER_SENSOR
        )
        self._air_coil_measured_water_sensor = (
            air_coil_measured_water_sensor or DEFAULT_AIR_COIL_MEASURED_WATER_SENSOR
        )
        self._floor_material = (floor_material or "").lower() or None
        self._cooling_dewpoint_margin = (
            float(cooling_dewpoint_margin)
            if cooling_dewpoint_margin is not None
            else None
        )
        self._cooling_dewpoint_protection = (
            True
            if cooling_dewpoint_protection is None
            else bool(cooling_dewpoint_protection)
        )

        self._pwm_cycle_start: datetime | None = None
        self._pwm_on_time = timedelta()
        self._pwm_integral = 0.0
        self._pwm_integral_mode: HVACMode | None = None
        self._pwm_duty_cycle = 0.0
        self._pwm_comfort_limited = False
        self._pwm_comfort_hold_until: datetime | None = None
        self._last_control_time: datetime | None = None

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added."""
        await super().async_added_to_hass()

        if last_state := await self.async_get_last_state():
            _LOGGER.debug(
                "%s: Restoring state from %s", self._attr_name, last_state.state
            )
            if ATTR_TEMPERATURE in last_state.attributes:
                try:
                    self._attr_target_temperature = float(
                        last_state.attributes.get(
                            "comfort_temperature", last_state.attributes[ATTR_TEMPERATURE]
                        )
                    )
                except (TypeError, ValueError):
                    _LOGGER.warning(
                        "%s: Invalid stored temperature %s", self._attr_name, last_state
                    )
            if last_state.state in (HVACMode.AUTO, HVACMode.OFF):
                self._manual_mode = HVACMode(last_state.state)

            restored_integral = last_state.attributes.get(PWM_INTEGRAL_KEY)
            if restored_integral is not None:
                try:
                    self._pwm_integral = float(restored_integral)
                except (TypeError, ValueError):
                    _LOGGER.warning(
                        "%s: Invalid stored PWM integral: %s",
                        self._attr_name,
                        restored_integral,
                    )
            integral_mode = last_state.attributes.get("pwm_integral_mode")
            if integral_mode in {HVACMode.HEAT, HVACMode.COOL}:
                self._pwm_integral_mode = HVACMode(integral_mode)

        self._controller.register_thermostat(self)
        self._pwm_zone_index, self._pwm_zone_count = self._controller.get_pwm_zone_info(
            self
        )

        self._remove_update_handler = async_track_time_interval(
            self.hass,
            self._async_update_temp,
            SCAN_INTERVAL,
        )
        await self.async_update_mode_listener()
        await self.async_update_temp_sensor_listener()
        self.async_schedule_control()
        self._schedule_recovery_learning()

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity will be removed."""
        await self.async_prepare_shutdown()
        self._controller.update_zone_status(
            self._zone_name, target=None, current=None, source=self
        )
        self._controller.unregister_thermostat(self)

    async def async_prepare_shutdown(self) -> None:
        """Stop listeners and pending control work before circuits are closed."""
        self._is_removing = True
        self._reschedule_control = False
        if self._recovery_task is not None:
            self._recovery_task.cancel()
            try:
                await self._recovery_task
            except asyncio.CancelledError:
                pass
            self._recovery_task = None
        if self._remove_update_handler is not None:
            self._remove_update_handler()
            self._remove_update_handler = None
        if self._remove_mode_listener is not None:
            self._remove_mode_listener()
            self._remove_mode_listener = None
        if self._remove_temp_sensor_listener is not None:
            self._remove_temp_sensor_listener()
            self._remove_temp_sensor_listener = None

        control_task = self._control_task
        if control_task is not None and not control_task.done():
            control_task.cancel()
            try:
                await control_task
            except asyncio.CancelledError:
                pass
        self._control_task = None
        self._pending_control = False

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature."""
        if not self._temp_sensor:
            _LOGGER.warning("%s: No temperature sensor configured", self._attr_name)
            return None
        temp_state = self.hass.states.get(self._temp_sensor)

        if temp_state is None:
            _LOGGER.debug(
                "%s: Temperature sensor %s not found",
                self._attr_name,
                self._temp_sensor,
            )
            return None

        if temp_state.state in {STATE_UNKNOWN, STATE_UNAVAILABLE}:
            _LOGGER.debug(
                "%s: Temperature sensor %s is %s; keeping circuits fail-safe off",
                self._attr_name,
                self._temp_sensor,
                temp_state.state,
            )
            return None

        try:
            temperature = float(temp_state.state)
            if not math.isfinite(temperature):
                raise ValueError("temperature is not finite")
            return temperature
        except (ValueError, TypeError) as exc:
            _LOGGER.error(
                "%s: Could not convert temperature '%s' to float: %s",
                self._attr_name,
                temp_state.state,
                exc,
            )
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra diagnostic state attributes."""
        recovery = self.recovery_plan
        recovery_conditions = self._recovery_compensation()
        return {
            "control_mode": self._control_mode,
            "comfort_temperature": self._attr_target_temperature,
            "setback_schedule": self._setback_schedule,
            "setback_c": self._setback_c,
            "setback_active": self.schedule_setback > 0,
            "scheduled_target_temperature": self.target_temperature,
            "recovery_enabled": self._recovery is not None,
            "recovery_active": recovery.active,
            "recovery_start": None if recovery.start is None else recovery.start.isoformat(),
            "recovery_ready_at": None if recovery.ready_at is None else recovery.ready_at.isoformat(),
            "recovery_minutes": round(recovery.minutes),
            "recovery_capped": recovery.capped,
            "recovery_source": "learned" if self._recovery and self._recovery.profile.rate else "fallback",
            "heating_rate_c_per_hour": None if self._recovery is None else self._recovery.profile.rate,
            "heating_rate_samples": 0 if self._recovery is None else self._recovery.profile.samples,
            "heating_rate_days": 0 if self._recovery is None else self._recovery.profile.days,
            "recovery_thermal_factor": round(recovery_conditions.factor, 3),
            "recovery_thermal_compensation": recovery_conditions.reason,
            "heating_rate_reference_supply_c": (
                self._recovery.profile.reference.supply_c
                if self._recovery and self._recovery.profile.reference else None
            ),
            "heating_rate_reference_outside_c": (
                self._recovery.profile.reference.outside_c
                if self._recovery and self._recovery.profile.reference else None
            ),
            "recovery_waiting_for_boiler": recovery.active and self._controller.space_heating_suspended,
            "recovery_error": None if self._recovery is None else self._recovery.error,
            "pwm_duty_cycle": round(self._pwm_duty_cycle, 2),
            "pwm_comfort_limited": self._pwm_comfort_limited,
            "pwm_thermal_factor": round(self._pwm_thermal.factor, 3),
            "pwm_measured_supply_c": self._pwm_thermal.supply_c,
            "pwm_outside_c": self._pwm_thermal.outside_c,
            "pwm_thermal_compensation": self._pwm_thermal.reason,
            "pwm_solar_forecast_w_m2": self._pwm_thermal.solar_w_m2,
            "pwm_solar_correction_c": round(self._pwm_thermal.solar_equivalent_c, 3),
            "pwm_on_time": round(self._pwm_on_time.total_seconds() / 60, 2),
            "pwm_cycle_time": self._pwm_cycle_time_minutes,
            "pwm_actuator_delay": self._pwm_actuator_delay_minutes,
            "pwm_zone_index": self._pwm_zone_index,
            "pwm_zone_count": self._pwm_zone_count,
            "zone_response": self._zone_response,
            "zone_flow_weight": round(self._zone_flow_weight, 2),
            "heating_min_flow_c": self._heating_min_flow_c,
            "air_coil_heat_suppressed": (
                self._air_coil_heat_suppressed if self._zone_type == "air_coil" else None
            ),
            "zone_solar_weight": round(self._zone_solar_weight, 2),
            "zone_type": self._zone_type,
            "price_charging_enabled": self._price_charging_enabled,
            "price_charging_boost_c": self.price_charging_boost,
            "effective_target_temperature": self.effective_target_temperature,
            "floor_material": self._floor_material,
            "cooling_dewpoint_margin": self._cooling_dewpoint_margin,
            "cooling_dewpoint_protection": self._cooling_dewpoint_protection,
            PWM_INTEGRAL_KEY: round(self._pwm_integral, 4),
            "pwm_integral_mode": self._pwm_integral_mode,
        }

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is None:
            _LOGGER.warning("No temperature provided in set_temperature call")
            return

        previous = self._attr_target_temperature
        self._attr_target_temperature = min(
            self._attr_max_temp, float(temperature) + self.schedule_setback
        )
        if abs(self._attr_target_temperature - previous) > PWM_TARGET_RESET_DELTA:
            self._reset_pwm_state(reset_integral=True)

        await self._control_heating()

    @property
    def recovery_plan(self) -> RecoveryPlan:
        """Estimate recovery separately from demand so existing interlocks remain authoritative."""
        if self._recovery is None:
            return RecoveryPlan()
        schedule = self.hass.states.get(self._setback_schedule)
        mode = self._controller.get_operation_mode()
        heating = mode in {"heat", "auto"} and not (
            mode == "auto" and self._controller._last_auto_effective_mode == HVACMode.COOL
        )
        return self._recovery.plan(
            now=datetime.now(timezone.utc),
            schedule_on=schedule is not None and schedule.state == "on",
            ready_at=None if schedule is None else getattr(schedule, "attributes", {}).get("next_event"),
            current=self.current_temperature, target=self._attr_target_temperature,
            setback=self._setback_c, enabled=heating and self._manual_mode != HVACMode.OFF,
            cycle_minutes=self._pwm_cycle_time_minutes if self._control_mode == CONTROL_MODE_PWM else 0,
            prices=self._controller.price_charging._slots,
            blocked_windows=self._controller.hot_water.reserved_windows() if self._controller.hot_water else [],
            thermal_factor=self._recovery_compensation().factor,
        )

    def _recovery_compensation(self) -> ThermalCompensation:
        """Compare learned warming with the conditions it was actually observed in."""
        if self._recovery is None or self._zone_type == "air_coil":
            return ThermalCompensation()
        mode = self._controller.get_operation_mode()
        if (self._manual_mode == HVACMode.OFF or mode not in {"heat", "auto"}
                or (mode == "auto" and self._controller._last_auto_effective_mode == HVACMode.COOL)):
            return ThermalCompensation(reason="not_space_heating")
        profile = self._recovery.profile
        if profile.rate and profile.reference is None:
            return ThermalCompensation(reason="unreferenced_history")
        return self._controller.thermal_compensation(
            room_c=self.current_temperature, target_c=self._attr_target_temperature,
            now=datetime.now(timezone.utc), reference=profile.reference,
        )

    def _schedule_recovery_learning(self) -> None:
        """Run history processing outside the control path and cancel it on removal."""
        if (self._recovery is None or self._recovery_task is not None or self._is_removing
                or self._manual_mode == HVACMode.OFF or self._temp_sensor is None):
            return
        now = datetime.now(timezone.utc)
        status = self._controller.status_entity
        if status is None or not self._recovery.learning_due(now):
            return

        async def learn():
            try:
                config = self._controller.thermal_compensation_config
                await self._recovery.async_learn(
                    self.hass, self._temp_sensor, self._circuits, status, now,
                    config.get("supply_sensor") if config.get("enabled") else None,
                    self._controller._outside_temp_sensor() if config.get("enabled") else None,
                    config.get("solar_forecast_sensor") if config.get("enabled") else None,
                    self._zone_solar_weight,
                )
            finally:
                self._recovery_task = None
            if not self._is_removing:
                self.async_schedule_control()

        self._recovery_task = self.hass.async_create_task(learn())

    @property
    def schedule_setback(self) -> float:
        """Lower heating comfort only while the configured absence schedule is on."""
        if not self._setback_schedule or self._manual_mode == HVACMode.OFF:
            return 0.0
        mode = self._controller.get_operation_mode()
        if mode == "cool" or (
            mode == "auto" and self._controller._last_auto_effective_mode == HVACMode.COOL
        ):
            return 0.0
        schedule = self.hass.states.get(self._setback_schedule)
        return self._setback_c if schedule is not None and schedule.state == "on" and not self.recovery_plan.active else 0.0

    @property
    def target_temperature(self) -> float:
        """Display the scheduled comfort target, retaining the underlying setting."""
        return max(self._attr_min_temp, self._attr_target_temperature - self.schedule_setback)

    @property
    def price_charging_boost(self) -> float:
        """Return additional headroom for an opted-in, enabled floor zone."""
        schedule = self.hass.states.get(self._setback_schedule) if self._setback_schedule else None
        if (
            not self._price_charging_enabled
            or self._zone_type != "floor"
            or self._manual_mode == HVACMode.OFF
            or (schedule is not None and schedule.state == "on")
            or (self._setback_schedule is not None and (schedule is None or schedule.state not in {"on", "off"}))
        ):
            return 0.0
        boost = self._controller.get_price_charging_boost()
        return max(0.0, min(boost, self._attr_max_temp - self._attr_target_temperature))

    @property
    def effective_target_temperature(self) -> float:
        """Apply temporary headroom without changing the saved comfort target."""
        return self.target_temperature + self.price_charging_boost

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set HVAC mode."""
        if hvac_mode not in (HVACMode.AUTO, HVACMode.OFF):
            _LOGGER.warning(
                "%s: Unsupported hvac mode %s requested; only AUTO/OFF allowed",
                self._attr_name,
                hvac_mode,
            )
            return

        self._manual_mode = hvac_mode
        if hvac_mode == HVACMode.OFF:
            self._reset_pwm_state(reset_integral=True)
        await self._control_heating()

    async def async_turn_on(self) -> None:
        """Turn the thermostat on (enable circuits under pump control)."""
        await self.async_set_hvac_mode(HVACMode.AUTO)

    async def async_turn_off(self) -> None:
        """Turn the thermostat off (force circuits closed)."""
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def _async_update_temp(self, *_) -> None:
        """Update temperature and control heating periodically."""
        self._schedule_recovery_learning()
        await self._control_heating()

    async def _control_heating(self) -> None:
        """Control the heating based on configured strategy."""
        setback = self.schedule_setback
        if setback != self._last_setback:
            self._last_setback = setback
            self._reset_pwm_state(reset_integral=True)
        previous_boost = self._last_price_boost
        self._last_price_boost = self.price_charging_boost
        if self._last_price_boost != previous_boost:
            self._reset_pwm_state(reset_integral=True)
        if self._manual_mode == HVACMode.OFF:
            self._reset_pwm_state(reset_integral=True)
            self._air_coil_heat_suppressed = False
            self._controller.update_zone_status(
                self._zone_name, target=None, current=None, source=self
            )
            await self._set_circuits_state(False, force_idle=True)
            self._attr_hvac_action = HVACAction.OFF
            self._effective_mode = HVACMode.OFF
            self.async_write_ha_state()
            return

        current_temp = self.current_temperature
        if current_temp is None:
            circuits_closed = await self._set_circuits_state(False, force_idle=True)
            self._reset_pwm_state(reset_integral=(
                self._temperature_seen and not self._controller.platform_setup_pending
            ))
            self._attr_hvac_action = HVACAction.IDLE
            if circuits_closed:
                self._controller.update_zone_status(
                    self._zone_name, target=None, current=None, source=self
                )
            else:
                self._controller.update_zone_status(
                    self._zone_name,
                    target=self.effective_target_temperature,
                    cooling_target=max(self.effective_target_temperature, self._attr_target_temperature),
                    current=self._attr_target_temperature,
                    active=True,
                    duty_cycle=0.0,
                    zone_response=self._zone_response,
                    zone_flow_weight=self._zone_flow_weight,
                    heating_min_flow_c=self._heating_min_flow_c,
                    zone_solar_weight=self._zone_solar_weight,
                    zone_type=self._zone_type,
                    floor_material=self._floor_material,
                    cooling_dewpoint_margin=self._cooling_dewpoint_margin,
                    cooling_dewpoint_protection=self._cooling_dewpoint_protection,
                    cooling_suppressed=False,
                    source=self,
                )
                await self._controller.async_update_heat_pump_state()
            self.async_write_ha_state()
            return

        self._temperature_seen = True
        active_before = self._circuits_are_active()
        self._controller.update_zone_status(
            self._zone_name,
            target=self.effective_target_temperature,
            cooling_target=max(self.effective_target_temperature, self._attr_target_temperature),
            current=current_temp,
            active=active_before,
            duty_cycle=self._current_zone_duty_hint(active_before),
            zone_response=self._zone_response,
            zone_flow_weight=self._zone_flow_weight,
            heating_min_flow_c=self._heating_min_flow_c,
            zone_solar_weight=self._zone_solar_weight,
            zone_type=self._zone_type,
            floor_material=self._floor_material,
            cooling_dewpoint_margin=self._cooling_dewpoint_margin,
            cooling_dewpoint_protection=self._cooling_dewpoint_protection,
            source=self,
        )

        pump_mode = self._controller.get_operation_mode()
        effective_mode = self._resolve_effective_mode(pump_mode)

        if pump_mode == "off":
            self._air_coil_heat_suppressed = False
            await self._set_circuits_state(False)
            self._attr_hvac_action = HVACAction.OFF
            self._effective_mode = HVACMode.OFF
            self._reset_pwm_state(reset_integral=not self._controller.platform_setup_pending)
            self._controller.update_zone_status(
                self._zone_name,
                target=self.effective_target_temperature,
                cooling_target=max(self.effective_target_temperature, self._attr_target_temperature),
                current=current_temp,
                active=False,
                duty_cycle=0.0,
                zone_response=self._zone_response,
                zone_flow_weight=self._zone_flow_weight,
                heating_min_flow_c=self._heating_min_flow_c,
                zone_solar_weight=self._zone_solar_weight,
                zone_type=self._zone_type,
                floor_material=self._floor_material,
                cooling_dewpoint_margin=self._cooling_dewpoint_margin,
                cooling_dewpoint_protection=self._cooling_dewpoint_protection,
                cooling_suppressed=False,
                source=self,
            )
            self.async_write_ha_state()
            return

        if effective_mode == HVACMode.OFF:
            await self._set_circuits_state(False, force_idle=True)
            self._attr_hvac_action = HVACAction.IDLE
            self._effective_mode = HVACMode.OFF
            self._reset_pwm_state(reset_integral=not self._controller.space_heating_suspended)
            self._controller.update_zone_status(
                self._zone_name,
                target=self.effective_target_temperature,
                cooling_target=max(self.effective_target_temperature, self._attr_target_temperature),
                current=current_temp,
                active=False,
                duty_cycle=0.0,
                zone_response=self._zone_response,
                zone_flow_weight=self._zone_flow_weight,
                heating_min_flow_c=self._heating_min_flow_c,
                zone_solar_weight=self._zone_solar_weight,
                zone_type=self._zone_type,
                floor_material=self._floor_material,
                cooling_dewpoint_margin=self._cooling_dewpoint_margin,
                cooling_dewpoint_protection=self._cooling_dewpoint_protection,
                cooling_suppressed=False,
                source=self,
            )
            self.async_write_ha_state()
            return

        if (
            effective_mode == HVACMode.COOL
            and self._zone_type != "air_coil"
            and self._controller.should_suppress_floor_cooling(
                zone_type=self._zone_type,
                target=self.effective_target_temperature,
                current=current_temp,
                floor_material=self._floor_material,
                cooling_dewpoint_margin=self._cooling_dewpoint_margin,
                cooling_dewpoint_protection=self._cooling_dewpoint_protection,
            )
        ):
            circuits_closed = await self._set_circuits_state(False)
            self._attr_hvac_action = HVACAction.IDLE
            self._effective_mode = effective_mode
            self._reset_pwm_state(reset_integral=False)
            self._controller.update_zone_status(
                self._zone_name,
                target=self.effective_target_temperature,
                cooling_target=max(self.effective_target_temperature, self._attr_target_temperature),
                current=current_temp,
                active=not circuits_closed,
                duty_cycle=0.0 if circuits_closed else 100.0,
                zone_response=self._zone_response,
                zone_flow_weight=self._zone_flow_weight,
                heating_min_flow_c=self._heating_min_flow_c,
                zone_solar_weight=self._zone_solar_weight,
                zone_type=self._zone_type,
                floor_material=self._floor_material,
                cooling_dewpoint_margin=self._cooling_dewpoint_margin,
                cooling_dewpoint_protection=self._cooling_dewpoint_protection,
                cooling_suppressed=circuits_closed,
                source=self,
            )
            if not circuits_closed:
                await self._controller.async_update_heat_pump_state()
            self.async_write_ha_state()
            return

        if self._zone_type == "air_coil" and effective_mode == HVACMode.HEAT:
            requested = self._entity_temperature(self._air_coil_requested_water_sensor)
            measured = self._entity_temperature(self._air_coil_measured_water_sensor)
            suppressed = air_coil_heating_suppressed(
                requested, measured, self._air_coil_heat_suppressed
            )
            if suppressed != self._air_coil_heat_suppressed:
                _LOGGER.info(
                    "%s: Air-coil water interlock %s (requested=%s, measured=%s)",
                    self._attr_name,
                    "closed" if suppressed else "released",
                    requested,
                    measured,
                )
            self._air_coil_heat_suppressed = suppressed
        else:
            self._air_coil_heat_suppressed = False

        charge_cap_reached = (
            self._last_price_boost > 0
            and current_temp >= self.effective_target_temperature
        ) or (
            previous_boost > 0
            and self._last_price_boost == 0
            and current_temp >= self._attr_target_temperature
        )
        if self._air_coil_heat_suppressed or charge_cap_reached:
            await self._set_circuits_state(False)
            self._attr_hvac_action = HVACAction.IDLE
        elif self._control_mode == CONTROL_MODE_PWM:
            await self._control_heating_pwm(current_temp, effective_mode)
        else:
            await self._control_heating_bang_bang(current_temp, effective_mode)

        self._effective_mode = effective_mode
        active_after = self._circuits_are_active()
        self._controller.update_zone_status(
            self._zone_name,
            target=self.effective_target_temperature,
            cooling_target=max(self.effective_target_temperature, self._attr_target_temperature),
            current=current_temp,
            active=active_after,
            duty_cycle=self._current_zone_duty_hint(active_after),
            zone_response=self._zone_response,
            zone_flow_weight=self._zone_flow_weight,
            heating_min_flow_c=self._heating_min_flow_c,
            zone_solar_weight=self._zone_solar_weight,
            zone_type=self._zone_type,
            floor_material=self._floor_material,
            cooling_dewpoint_margin=self._cooling_dewpoint_margin,
            cooling_dewpoint_protection=self._cooling_dewpoint_protection,
            cooling_suppressed=False,
            source=self,
        )
        self.async_write_ha_state()

    def _entity_temperature(self, entity_id: str) -> float | None:
        """Read a water temperature, treating missing data as unsafe for the coil."""
        state = self.hass.states.get(entity_id)
        try:
            value = float(state.state) if state is not None else float("nan")
            return value if math.isfinite(value) else None
        except (TypeError, ValueError):
            return None

    def _current_zone_duty_hint(self, is_active: bool) -> float:
        """Return zone demand proxy for flow-supervisor calculations."""
        if self._control_mode == CONTROL_MODE_PWM:
            return max(0.0, min(100.0, float(self._pwm_duty_cycle)))
        return 100.0 if is_active else 0.0

    def _resolve_effective_mode(self, pump_mode: str) -> HVACMode:
        """Map pump mode to a climate mode."""
        if self._controller.space_heating_suspended:
            return HVACMode.OFF
        if pump_mode == "cool":
            return HVACMode.COOL
        if pump_mode == "heat":
            return HVACMode.HEAT
        return self._controller.determine_auto_mode()

    async def _control_heating_bang_bang(
        self,
        current_temp: float,
        effective_mode: HVACMode,
    ) -> None:
        """Original hysteresis based control."""
        hysteresis = self._hysteresis
        target = self.effective_target_temperature

        if effective_mode == HVACMode.HEAT:
            should_activate = current_temp < (target - hysteresis)
            should_deactivate = current_temp > (target + hysteresis)
            active_action = HVACAction.HEATING
        else:
            should_activate = current_temp > (target + hysteresis)
            should_deactivate = current_temp < (target - hysteresis)
            active_action = HVACAction.COOLING

        if should_activate:
            await self._set_circuits_state(True)
            self._attr_hvac_action = active_action
        elif should_deactivate:
            await self._set_circuits_state(False)
            self._attr_hvac_action = HVACAction.IDLE
        else:
            self._attr_hvac_action = (
                active_action if self._circuits_are_active() else HVACAction.IDLE
            )

    async def _control_heating_pwm(
        self,
        current_temp: float,
        effective_mode: HVACMode,
    ) -> None:
        """PI + PWM control with scheduled cycles."""
        if self._pwm_integral_mode is not None and self._pwm_integral_mode != effective_mode:
            self._reset_pwm_state(reset_integral=True)
        now = datetime.now(timezone.utc)
        cycle_start = self._get_aligned_pwm_cycle_start(now)
        active_before = self._circuits_are_active()

        error = self.effective_target_temperature - current_temp
        if effective_mode == HVACMode.COOL:
            error = -error
        if error < -self._hysteresis:
            self._reset_pwm_state(reset_integral=True)
            self._pwm_cycle_start = cycle_start
            self._pwm_comfort_limited = True
            self._pwm_comfort_hold_until = max(
                cycle_start + timedelta(minutes=self._pwm_cycle_time_minutes),
                now + timedelta(minutes=self._pwm_min_off_time_minutes),
            )

        if self._pwm_comfort_hold_until is not None and now < self._pwm_comfort_hold_until:
            await self._set_circuits_state(False)
            self._attr_hvac_action = HVACAction.IDLE
            return

        if self._pwm_cycle_start != cycle_start:
            resuming_after_limit = self._pwm_comfort_limited
            self._start_new_pwm_cycle(
                current_temp=current_temp,
                effective_mode=effective_mode,
                now=now,
                cycle_start=cycle_start,
                was_active=active_before,
            )
            remaining = (cycle_start + self._pwm_on_time - now).total_seconds()
            if resuming_after_limit and remaining < self._pwm_min_on_time_minutes * 60:
                self._pwm_on_time = timedelta()
                self._pwm_duty_cycle = 0.0

        if self._pwm_cycle_start is None:
            return

        should_be_on = should_circuits_be_on(
            now=now,
            cycle_start=self._pwm_cycle_start,
            on_time=self._pwm_on_time,
        )
        await self._set_circuits_state(should_be_on)

        if should_be_on:
            self._attr_hvac_action = (
                HVACAction.HEATING if effective_mode == HVACMode.HEAT else HVACAction.COOLING
            )
        else:
            self._attr_hvac_action = HVACAction.IDLE

    def _start_new_pwm_cycle(
        self,
        current_temp: float,
        effective_mode: HVACMode,
        now: datetime,
        cycle_start: datetime,
        was_active: bool,
    ) -> None:
        """Start a new PWM cycle and compute duty from PI terms."""
        self._pwm_comfort_limited = False
        self._pwm_comfort_hold_until = None
        duty = self._calculate_pwm_duty(current_temp, effective_mode, now)
        on_minutes = calculate_on_time_minutes(
            duty_cycle=duty,
            cycle_time_minutes=self._pwm_cycle_time_minutes,
            min_on_time_minutes=self._pwm_min_on_time_minutes,
            min_off_time_minutes=self._pwm_min_off_time_minutes,
            actuator_delay_minutes=self._pwm_actuator_delay_minutes,
            was_active=was_active,
        )

        self._pwm_cycle_start = cycle_start
        self._pwm_on_time = timedelta(minutes=on_minutes)

    def _get_aligned_pwm_cycle_start(self, now: datetime) -> datetime:
        """Return cycle start aligned to fixed wall-clock PWM intervals."""
        self._pwm_zone_index, self._pwm_zone_count = self._controller.get_pwm_zone_info(
            self
        )
        return get_aligned_pwm_cycle_start(
            now=now,
            cycle_time_minutes=self._pwm_cycle_time_minutes,
            zone_index=self._pwm_zone_index,
            zone_count=self._pwm_zone_count,
        )

    def _calculate_pwm_duty(
        self,
        current_temp: float,
        effective_mode: HVACMode,
        now: datetime,
    ) -> float:
        """Calculate PI duty cycle percentage."""
        if self._pwm_integral_mode is not None and self._pwm_integral_mode != effective_mode:
            self._reset_pwm_state(reset_integral=True)
        self._pwm_integral_mode = effective_mode
        self._pwm_thermal = (
            self._controller.thermal_compensation(
                room_c=current_temp, target_c=self.effective_target_temperature, now=now,
                solar_weight=(self._zone_solar_weight if
                    current_temp >= self.effective_target_temperature - 0.3 else 0.0),
            ) if effective_mode == HVACMode.HEAT and self._zone_type != "air_coil"
            else ThermalCompensation(reason="not_space_heating")
        )
        self._pwm_duty_cycle, self._pwm_integral, self._last_control_time = (
            calculate_pwm_duty(
                target_temperature=self.effective_target_temperature,
                current_temp=current_temp,
                effective_mode=effective_mode,
                now=now,
                last_control_time=self._last_control_time,
                pwm_integral=self._pwm_integral,
                pwm_kp=self._pwm_kp,
                pwm_ki=self._pwm_ki,
                thermal_factor=self._pwm_thermal.factor,
            )
        )
        return self._pwm_duty_cycle

    def _reset_pwm_state(self, reset_integral: bool) -> None:
        """Reset PWM cycle state."""
        self._pwm_cycle_start = None
        self._pwm_on_time = timedelta()
        self._pwm_duty_cycle = 0.0
        self._pwm_comfort_limited = False
        self._pwm_comfort_hold_until = None
        self._pwm_thermal = ThermalCompensation(reason="inactive")
        self._last_control_time = None
        if reset_integral:
            self._pwm_integral = 0.0
            self._pwm_integral_mode = None

    async def _handle_pump_mode_change(self, event) -> None:
        """React to global heat pump mode changes."""
        self.async_schedule_control()

    async def _handle_temp_sensor_change(self, event) -> None:
        """Re-evaluate control whenever the room temperature sensor changes."""
        self.async_schedule_control()

    def async_schedule_control(self) -> None:
        """Schedule a control evaluation if one isn't already running."""
        if self._is_removing:
            return
        if self._pending_control:
            self._reschedule_control = True
            return

        async def _run() -> None:
            try:
                await self._control_heating()
            finally:
                self._pending_control = False
                self._control_task = None
                if self._reschedule_control and not self._is_removing:
                    self._reschedule_control = False
                    self.async_schedule_control()

        self._pending_control = True
        self._reschedule_control = False
        self._control_task = self.hass.async_create_task(_run())

    async def async_update_mode_listener(self) -> None:
        """Subscribe to heat pump mode changes (internal or external)."""
        mode_entity = self._controller.mode_entity

        if self._mode_listener_entity == mode_entity:
            return

        if self._remove_mode_listener is not None:
            self._remove_mode_listener()
            self._remove_mode_listener = None

        self._mode_listener_entity = mode_entity

        if mode_entity is not None:
            self._remove_mode_listener = async_track_state_change_event(
                self.hass,
                mode_entity,
                self._handle_pump_mode_change,
            )

    async def async_update_temp_sensor_listener(self) -> None:
        """Subscribe to temperature sensor updates for quick post-startup recovery."""
        if self._remove_temp_sensor_listener is not None:
            self._remove_temp_sensor_listener()
            self._remove_temp_sensor_listener = None

        entities = [entity for entity in [self._temp_sensor, self._setback_schedule] if entity]
        if entities:
            self._remove_temp_sensor_listener = async_track_state_change_event(
                self.hass,
                entities[0] if len(entities) == 1 else entities,
                self._handle_temp_sensor_change,
            )

    async def _set_circuits_state(
        self, state: bool, *, force_idle: bool = False
    ) -> bool:
        """Set all circuits to the specified state."""
        for circuit_entity_id in self._circuits:
            try:
                domain, _, _ = circuit_entity_id.partition(".")
                if domain not in {"input_boolean", "switch"}:
                    _LOGGER.error(
                        "%s: Unsupported circuit entity %s (expected input_boolean.* or switch.*)",
                        self._attr_name,
                        circuit_entity_id,
                    )
                    continue

                desired_state = "on" if state else "off"
                circuit_state = self.hass.states.get(circuit_entity_id)
                if state and (
                    circuit_state is None
                    or circuit_state.state in {STATE_UNKNOWN, STATE_UNAVAILABLE}
                ):
                    _LOGGER.debug(
                        "%s: Circuit %s is not available; skipping %s during this cycle",
                        self._attr_name,
                        circuit_entity_id,
                        desired_state,
                    )
                    continue
                if circuit_state is not None and circuit_state.state == desired_state:
                    continue

                await self.hass.services.async_call(
                    domain,
                    "turn_on" if state else "turn_off",
                    {"entity_id": circuit_entity_id},
                    blocking=True,
                )
            except Exception as exc:  # pragma: no cover
                _LOGGER.error(
                    "%s: Error setting state for circuit %s: %s",
                    self._attr_name,
                    circuit_entity_id,
                    exc,
                )

        if not state and self._manual_mode == HVACMode.OFF:
            self._attr_hvac_action = HVACAction.OFF

        await self._controller.async_update_heat_pump_state(force_idle=force_idle)
        return self._circuits_are_active() if state else self._circuits_are_confirmed_off()

    def _circuits_are_active(self) -> bool:
        """Return True if any circuit is currently on."""
        for circuit_entity_id in self._circuits:
            state = self.hass.states.get(circuit_entity_id)
            if state and state.state == "on":
                return True
        return False

    def _circuits_are_confirmed_off(self) -> bool:
        """Return True only when every circuit explicitly reports off."""
        for circuit_entity_id in self._circuits:
            state = self.hass.states.get(circuit_entity_id)
            if state is None or state.state != "off":
                return False
        return True

    @staticmethod
    def _prettify(name: str) -> str:
        """Return a human readable name from a slug-like zone name."""
        cleaned = name.replace("_", " ").replace("-", " ").strip()
        return cleaned[:1].upper() + cleaned[1:]

    @staticmethod
    def _slugify(name: str) -> str:
        """Return a slug suitable for entity IDs."""
        value = name.lower()
        value = re.sub(r"[^a-z0-9_]+", "_", value)
        value = re.sub(r"__+", "_", value)
        return value.strip("_")

    @property
    def control_mode(self) -> str:
        """Return control mode for coordination logic."""
        return self._control_mode

    @property
    def hvac_mode(self) -> HVACMode:
        return HVACMode.OFF if self._manual_mode == HVACMode.OFF else HVACMode.AUTO

    @property
    def hvac_modes(self) -> list[HVACMode]:
        return [HVACMode.AUTO, HVACMode.OFF]
