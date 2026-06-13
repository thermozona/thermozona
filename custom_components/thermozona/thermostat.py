"""Thermostat entity for the Thermozona integration."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity

from . import (
    CONTROL_MODE_BANG_BANG,
    CONTROL_MODE_PWM,
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
        self._manual_mode: HVACMode = HVACMode.AUTO
        self._effective_mode: HVACMode = HVACMode.AUTO
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
        self._zone_solar_weight = (
            float(zone_solar_weight)
            if zone_solar_weight is not None
            else DEFAULT_ZONE_SOLAR_WEIGHT
        )
        self._zone_solar_weight = max(0.0, self._zone_solar_weight)

        self._pwm_cycle_start: datetime | None = None
        self._pwm_on_time = timedelta()
        self._pwm_integral = 0.0
        self._pwm_duty_cycle = 0.0
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
                        last_state.attributes[ATTR_TEMPERATURE]
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

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity will be removed."""
        if self._remove_update_handler is not None:
            self._remove_update_handler()
        if self._remove_mode_listener is not None:
            self._remove_mode_listener()
        if self._remove_temp_sensor_listener is not None:
            self._remove_temp_sensor_listener()
        self._controller.update_zone_status(
            self._zone_name, target=None, current=None, source=self
        )
        self._controller.unregister_thermostat(self)

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature."""
        if not self._temp_sensor:
            _LOGGER.warning("%s: No temperature sensor configured", self._attr_name)
            return None
        temp_state = self.hass.states.get(self._temp_sensor)

        if temp_state is None:
            _LOGGER.warning(
                "%s: Temperature sensor %s not found",
                self._attr_name,
                self._temp_sensor,
            )
            return None

        try:
            return float(temp_state.state)
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
        return {
            "control_mode": self._control_mode,
            "pwm_duty_cycle": round(self._pwm_duty_cycle, 2),
            "pwm_on_time": round(self._pwm_on_time.total_seconds() / 60, 2),
            "pwm_cycle_time": self._pwm_cycle_time_minutes,
            "pwm_actuator_delay": self._pwm_actuator_delay_minutes,
            "pwm_zone_index": self._pwm_zone_index,
            "pwm_zone_count": self._pwm_zone_count,
            "zone_response": self._zone_response,
            "zone_flow_weight": round(self._zone_flow_weight, 2),
            "zone_solar_weight": round(self._zone_solar_weight, 2),
            PWM_INTEGRAL_KEY: round(self._pwm_integral, 4),
        }

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is None:
            _LOGGER.warning("No temperature provided in set_temperature call")
            return

        previous = self._attr_target_temperature
        self._attr_target_temperature = float(temperature)
        if abs(self._attr_target_temperature - previous) > PWM_TARGET_RESET_DELTA:
            self._reset_pwm_state(reset_integral=True)

        await self._control_heating()

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
        await self._control_heating()

    async def _control_heating(self) -> None:
        """Control the heating based on configured strategy."""
        if self._manual_mode == HVACMode.OFF:
            self._controller.update_zone_status(
                self._zone_name, target=None, current=None, source=self
            )
            await self._set_circuits_state(False)
            self._attr_hvac_action = HVACAction.OFF
            self._effective_mode = HVACMode.OFF
            self.async_write_ha_state()
            return

        current_temp = self.current_temperature
        if current_temp is None:
            self._controller.update_zone_status(
                self._zone_name, target=None, current=None, source=self
            )
            self.async_write_ha_state()
            return

        active_before = self._circuits_are_active()
        self._controller.update_zone_status(
            self._zone_name,
            target=self._attr_target_temperature,
            current=current_temp,
            active=active_before,
            duty_cycle=self._current_zone_duty_hint(active_before),
            zone_response=self._zone_response,
            zone_flow_weight=self._zone_flow_weight,
            zone_solar_weight=self._zone_solar_weight,
            source=self,
        )

        pump_mode = self._controller.get_operation_mode()
        effective_mode = self._resolve_effective_mode(pump_mode)

        if pump_mode == "off":
            await self._set_circuits_state(False)
            self._attr_hvac_action = HVACAction.OFF
            self._effective_mode = HVACMode.OFF
            self._reset_pwm_state(reset_integral=True)
            self._controller.update_zone_status(
                self._zone_name,
                target=self._attr_target_temperature,
                current=current_temp,
                active=False,
                duty_cycle=0.0,
                zone_response=self._zone_response,
                zone_flow_weight=self._zone_flow_weight,
                zone_solar_weight=self._zone_solar_weight,
                source=self,
            )
            self.async_write_ha_state()
            return

        if self._control_mode == CONTROL_MODE_PWM:
            await self._control_heating_pwm(current_temp, effective_mode)
        else:
            await self._control_heating_bang_bang(current_temp, effective_mode)

        self._effective_mode = effective_mode
        active_after = self._circuits_are_active()
        self._controller.update_zone_status(
            self._zone_name,
            target=self._attr_target_temperature,
            current=current_temp,
            active=active_after,
            duty_cycle=self._current_zone_duty_hint(active_after),
            zone_response=self._zone_response,
            zone_flow_weight=self._zone_flow_weight,
            zone_solar_weight=self._zone_solar_weight,
            source=self,
        )
        self.async_write_ha_state()

    def _current_zone_duty_hint(self, is_active: bool) -> float:
        """Return zone demand proxy for flow-supervisor calculations."""
        if self._control_mode == CONTROL_MODE_PWM:
            return max(0.0, min(100.0, float(self._pwm_duty_cycle)))
        return 100.0 if is_active else 0.0

    def _resolve_effective_mode(self, pump_mode: str) -> HVACMode:
        """Map pump mode to a climate mode."""
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
        target = self._attr_target_temperature

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
        now = datetime.now(timezone.utc)
        cycle_start = self._get_aligned_pwm_cycle_start(now)
        active_before = self._circuits_are_active()

        if self._pwm_cycle_start != cycle_start:
            self._start_new_pwm_cycle(
                current_temp=current_temp,
                effective_mode=effective_mode,
                now=now,
                cycle_start=cycle_start,
                was_active=active_before,
            )

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
        self._pwm_duty_cycle, self._pwm_integral, self._last_control_time = (
            calculate_pwm_duty(
                target_temperature=self._attr_target_temperature,
                current_temp=current_temp,
                effective_mode=effective_mode,
                now=now,
                last_control_time=self._last_control_time,
                pwm_integral=self._pwm_integral,
                pwm_kp=self._pwm_kp,
                pwm_ki=self._pwm_ki,
            )
        )
        return self._pwm_duty_cycle

    def _reset_pwm_state(self, reset_integral: bool) -> None:
        """Reset PWM cycle state."""
        self._pwm_cycle_start = None
        self._pwm_on_time = timedelta()
        self._pwm_duty_cycle = 0.0
        self._last_control_time = None
        if reset_integral:
            self._pwm_integral = 0.0

    async def _handle_pump_mode_change(self, event) -> None:
        """React to global heat pump mode changes."""
        self.async_schedule_control()

    async def _handle_temp_sensor_change(self, event) -> None:
        """Re-evaluate control whenever the room temperature sensor changes."""
        self.async_schedule_control()

    def async_schedule_control(self) -> None:
        """Schedule a control evaluation if one isn't already running."""
        if self._pending_control:
            self._reschedule_control = True
            return

        async def _run() -> None:
            try:
                await self._control_heating()
            finally:
                self._pending_control = False
                if self._reschedule_control:
                    self._reschedule_control = False
                    self.async_schedule_control()

        self._pending_control = True
        self._reschedule_control = False
        self.hass.async_create_task(_run())

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

        if self._temp_sensor is not None:
            self._remove_temp_sensor_listener = async_track_state_change_event(
                self.hass,
                self._temp_sensor,
                self._handle_temp_sensor_change,
            )

    async def _set_circuits_state(self, state: bool) -> None:
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

        await self._controller.async_update_heat_pump_state()

    def _circuits_are_active(self) -> bool:
        """Return True if any circuit is currently on."""
        for circuit_entity_id in self._circuits:
            state = self.hass.states.get(circuit_entity_id)
            if state and state.state == "on":
                return True
        return False

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
