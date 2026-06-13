from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.thermozona.helpers import resolve_circuits
from custom_components.thermozona.heat_pump import HeatPumpController
from custom_components.thermozona.sensor import ThermozonaFlowTemperatureSensor
from custom_components.thermozona.select import ThermozonaHeatPumpModeSelect
from custom_components.thermozona.thermostat import ThermozonaThermostat
from homeassistant.components.climate import HVACAction, HVACMode
from homeassistant.const import ATTR_TEMPERATURE


class DummyNumber:
    def __init__(self):
        self.values: list[float] = []

    def set_calculated_value(self, value: float) -> None:
        self.values.append(value)


class DummySensor:
    def __init__(self):
        self.states: list[str] = []

    def update_state(self, state: str) -> None:
        self.states.append(state)


class DummySelect:
    def __init__(self):
        self.entity_id = "select.mode"
        self.options: list[str] = []

    def update_current_option(self, option: str) -> None:
        self.options.append(option)


class DummyThermostat:
    def __init__(self):
        self.calls = 0

    def async_schedule_control(self) -> None:
        self.calls += 1

    async def async_update_mode_listener(self) -> None:
        return None


class RecordingThermostat(ThermozonaThermostat):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.write_calls = 0

    def async_write_ha_state(self) -> None:
        self.write_calls += 1


def _config(**overrides):
    base = {
        "outside_temp_sensor": "sensor.outside",
        "flow_temp_sensor": "input_number.flow",
        "zones": {
            "living_room": {
                "circuits": ["switch.zone_1"],
                "temp_sensor": "sensor.living",
            }
        },
    }

    if "flow" in overrides:
        base["flow"] = overrides.pop("flow")

    base.update(overrides)
    return base


def test_resolve_circuits_supports_new_and_legacy_keys():
    assert resolve_circuits({"circuits": ["switch.a"]}) == ["switch.a"]
    assert resolve_circuits({"groups": ["switch.b"]}) == ["switch.b"]
    assert resolve_circuits({}) == []


def test_auto_mode_and_flow_temperature_calculation_uses_zone_status():
    controller = HeatPumpController(SimpleNamespace(states=None), _config())
    controller.update_zone_status("living", target=21, current=19, active=True)
    assert controller.determine_auto_mode() == HVACMode.HEAT

    flow = controller.determine_flow_temperature(HVACMode.HEAT, outside_temp=5)
    assert flow > 24

    controller.update_zone_status("living", target=21, current=23, active=True)
    assert controller.determine_auto_mode() == HVACMode.COOL
    cool_flow = controller.determine_flow_temperature(HVACMode.COOL, outside_temp=30)
    assert 15 <= cool_flow <= 25


@pytest.mark.asyncio
async def test_flow_temperature_sensor_exposes_breakdown_attributes(fake_hass):
    controller = HeatPumpController(fake_hass, _config())
    sensor = ThermozonaFlowTemperatureSensor("entry-1", controller)
    controller.register_flow_temperature_sensor(sensor)

    fake_hass.states.set("sensor.outside", "10")
    controller.update_zone_status("living", target=21.0, current=20.0, active=True)

    await controller._async_set_flow_temperature()

    attrs = sensor.extra_state_attributes
    assert attrs["effective_mode"] == "heat"
    assert "target_ref_c" in attrs
    assert "weather_term_c" in attrs
    assert attrs["flow_temp_c"] == pytest.approx(sensor._attr_native_value, abs=0.01)


def test_supervised_heating_flow_is_available(fake_hass):
    controller = HeatPumpController(fake_hass, _config())
    controller.update_zone_status(
        "living",
        target=21,
        current=19,
        active=True,
        duty_cycle=100,
        zone_response="slow",
        zone_flow_weight=1.0,
    )

    flow = controller.determine_flow_temperature(HVACMode.HEAT, outside_temp=10)

    assert flow > 0


def test_flow_supervisor_prioritizes_slow_zones(fake_hass):
    flow_config = {
        "kp": 1.0,
        "use_integral": False,
        "fast_boost_gain": 0.0,
        "slew_up_c_per_5m": 20.0,
        "slew_down_c_per_5m": 20.0,
    }
    controller = HeatPumpController(fake_hass, _config(flow=flow_config))

    controller.update_zone_status(
        "slow_zone",
        target=21,
        current=20,
        active=True,
        duty_cycle=40,
        zone_response="slow",
        zone_flow_weight=1.0,
    )
    controller.update_zone_status(
        "fast_zone",
        target=23,
        current=20,
        active=True,
        duty_cycle=100,
        zone_response="fast",
        zone_flow_weight=0.2,
    )

    flow = controller.determine_flow_temperature(
        HVACMode.HEAT,
        outside_temp=15,
    )

    assert flow < 26.0
    assert flow >= 23.0


@pytest.mark.asyncio
async def test_flow_write_deadband_suppresses_small_changes(fake_hass):
    controller = HeatPumpController(
        fake_hass,
        _config(
            flow={
                "write_deadband_c": 0.5,
                "write_min_interval_minutes": 60,
            },
        ),
    )
    number = DummyNumber()
    controller.register_flow_temperature_number(number)

    fake_hass.states.set("sensor.outside", "12")
    controller.update_zone_status("living", target=21.0, current=20.0, active=True)

    await controller._async_set_flow_temperature()

    controller.update_zone_status("living", target=21.2, current=20.2, active=True)
    await controller._async_set_flow_temperature()

    assert len(number.values) == 1


@pytest.mark.asyncio
async def test_flow_write_interval_forces_periodic_write(fake_hass):
    controller = HeatPumpController(
        fake_hass,
        _config(
            flow={
                "write_deadband_c": 0.5,
                "write_min_interval_minutes": 10,
            },
        ),
    )
    number = DummyNumber()
    controller.register_flow_temperature_number(number)

    fake_hass.states.set("sensor.outside", "12")
    controller.update_zone_status("living", target=21.0, current=20.0, active=True)
    await controller._async_set_flow_temperature()

    controller._last_flow_write_time = datetime.now(timezone.utc) - timedelta(minutes=11)
    controller.update_zone_status("living", target=21.2, current=20.2, active=True)
    await controller._async_set_flow_temperature()

    assert len(number.values) == 2


def test_pro_preheat_boost_increases_flow_when_forecast_is_colder(fake_hass):
    preheat_off = HeatPumpController(
        fake_hass,
        _config(
            flow={
                "kp": 0.0,
                "use_integral": False,
                "fast_boost_gain": 0.0,
                "preheat_enabled": False,
                "slew_up_c_per_5m": 20.0,
                "slew_down_c_per_5m": 20.0,
            },
        ),
    )
    preheat_on = HeatPumpController(
        fake_hass,
        _config(
            flow={
                "kp": 0.0,
                "use_integral": False,
                "fast_boost_gain": 0.0,
                "preheat_enabled": True,
                "preheat_forecast_sensor": "sensor.outside_forecast",
                "preheat_gain": 0.35,
                "preheat_cap_c": 1.2,
                "preheat_min_slow_di": 0.0,
                "slew_up_c_per_5m": 20.0,
                "slew_down_c_per_5m": 20.0,
            },
        ),
    )

    fake_hass.states.set("sensor.outside", "8")
    fake_hass.states.set("sensor.outside_forecast", "2")

    for controller in (preheat_off, preheat_on):
        controller.update_zone_status(
            "living",
            target=21,
            current=20,
            active=True,
            duty_cycle=20,
            zone_response="slow",
            zone_flow_weight=1.0,
        )

    flow_without_preheat = preheat_off.determine_flow_temperature(
        HVACMode.HEAT,
        outside_temp=8,
    )
    flow_with_preheat = preheat_on.determine_flow_temperature(
        HVACMode.HEAT,
        outside_temp=8,
    )

    assert flow_with_preheat > flow_without_preheat


def test_pro_preheat_solar_forecast_softens_preheat_boost(fake_hass):
    no_solar = HeatPumpController(
        fake_hass,
        _config(
            flow={
                "kp": 0.0,
                "use_integral": False,
                "fast_boost_gain": 0.0,
                "preheat_enabled": True,
                "preheat_forecast_sensor": "sensor.outside_forecast",
                "preheat_gain": 0.35,
                "preheat_cap_c": 1.2,
                "preheat_min_slow_di": 0.0,
                "slew_up_c_per_5m": 20.0,
                "slew_down_c_per_5m": 20.0,
            },
        ),
    )
    with_solar = HeatPumpController(
        fake_hass,
        _config(
            flow={
                "kp": 0.0,
                "use_integral": False,
                "fast_boost_gain": 0.0,
                "preheat_enabled": True,
                "preheat_forecast_sensor": "sensor.outside_forecast",
                "preheat_solar_sensor": "sensor.solar_forecast",
                "preheat_gain": 0.35,
                "preheat_solar_gain_per_w_m2": 0.002,
                "preheat_cap_c": 1.2,
                "preheat_min_slow_di": 0.0,
                "slew_up_c_per_5m": 20.0,
                "slew_down_c_per_5m": 20.0,
            },
        ),
    )

    fake_hass.states.set("sensor.outside", "8")
    fake_hass.states.set("sensor.outside_forecast", "2")
    fake_hass.states.set("sensor.solar_forecast", "500")

    for controller in (no_solar, with_solar):
        controller.update_zone_status(
            "living",
            target=21,
            current=20,
            active=True,
            duty_cycle=20,
            zone_response="slow",
            zone_flow_weight=1.0,
        )

    flow_without_solar = no_solar.determine_flow_temperature(
        HVACMode.HEAT,
        outside_temp=8,
    )
    flow_with_solar = with_solar.determine_flow_temperature(
        HVACMode.HEAT,
        outside_temp=8,
    )

    assert flow_with_solar < flow_without_solar


def test_pro_preheat_solar_weight_is_applied_per_zone(fake_hass):
    controller = HeatPumpController(
        fake_hass,
        _config(
            flow={
                "kp": 0.0,
                "use_integral": False,
                "fast_boost_gain": 0.0,
                "preheat_enabled": True,
                "preheat_forecast_sensor": "sensor.outside_forecast",
                "preheat_solar_sensor": "sensor.solar_forecast",
                "preheat_gain": 0.35,
                "preheat_solar_gain_per_w_m2": 0.002,
                "preheat_cap_c": 1.2,
                "preheat_min_slow_di": 0.0,
                "slew_up_c_per_5m": 20.0,
                "slew_down_c_per_5m": 20.0,
            },
        ),
    )

    fake_hass.states.set("sensor.outside", "8")
    fake_hass.states.set("sensor.outside_forecast", "2")
    fake_hass.states.set("sensor.solar_forecast", "500")

    controller.update_zone_status(
        "kitchen",
        target=21,
        current=20,
        active=True,
        duty_cycle=20,
        zone_response="slow",
        zone_flow_weight=1.0,
        zone_solar_weight=2.0,
    )
    controller.update_zone_status(
        "bathroom",
        target=21,
        current=20,
        active=True,
        duty_cycle=20,
        zone_response="slow",
        zone_flow_weight=1.0,
        zone_solar_weight=0.0,
    )

    mixed_flow = controller.determine_flow_temperature(HVACMode.HEAT, outside_temp=8)

    controller.update_zone_status(
        "kitchen",
        target=21,
        current=20,
        active=True,
        duty_cycle=20,
        zone_response="slow",
        zone_flow_weight=1.0,
        zone_solar_weight=0.0,
    )
    controller.update_zone_status(
        "bathroom",
        target=21,
        current=20,
        active=True,
        duty_cycle=20,
        zone_response="slow",
        zone_flow_weight=1.0,
        zone_solar_weight=0.0,
    )

    no_solar_zone_weight_flow = controller.determine_flow_temperature(
        HVACMode.HEAT, outside_temp=8
    )

    assert mixed_flow < no_solar_zone_weight_flow


def test_pro_slew_rate_is_asymmetric(fake_hass):
    controller = HeatPumpController(
        fake_hass,
        _config(
            flow={
                "kp": 2.0,
                "use_integral": False,
                "fast_boost_gain": 0.0,
                "slew_up_c_per_5m": 0.3,
                "slew_down_c_per_5m": 0.2,
            },
        ),
    )
    controller.update_zone_status(
        "living",
        target=21,
        current=20.5,
        active=True,
        duty_cycle=30,
        zone_response="slow",
        zone_flow_weight=1.0,
    )
    first = controller.determine_flow_temperature(HVACMode.HEAT, outside_temp=10)

    controller._flow_supervisor._last_eval_time = (
        datetime.now(timezone.utc) - timedelta(minutes=5)
    )
    controller._flow_supervisor._last_flow = first
    controller.update_zone_status(
        "living",
        target=23,
        current=19,
        active=True,
        duty_cycle=100,
        zone_response="slow",
        zone_flow_weight=1.0,
    )
    increased = controller.determine_flow_temperature(HVACMode.HEAT, outside_temp=10)

    controller._flow_supervisor._last_eval_time = (
        datetime.now(timezone.utc) - timedelta(minutes=5)
    )
    controller._flow_supervisor._last_flow = increased
    controller.update_zone_status(
        "living",
        target=21,
        current=21,
        active=True,
        duty_cycle=0,
        zone_response="slow",
        zone_flow_weight=1.0,
    )
    decreased = controller.determine_flow_temperature(HVACMode.HEAT, outside_temp=10)

    assert round(increased - first, 3) <= 0.305
    assert round(increased - first, 3) >= 0.0
    assert round(increased - decreased, 3) <= 0.205
    assert round(increased - decreased, 3) >= 0.0


def test_get_operation_mode_maps_external_states(fake_hass):
    controller = HeatPumpController(fake_hass, _config(heat_pump_mode="sensor.mode"))

    fake_hass.states.set("sensor.mode", "heating")
    assert controller.get_operation_mode() == "heat"

    fake_hass.states.set("sensor.mode", "cooling")
    assert controller.get_operation_mode() == "cool"

    fake_hass.states.set("sensor.mode", "idle")
    assert controller.get_operation_mode() == "off"


@pytest.mark.asyncio
async def test_async_set_flow_temperature_updates_number_entity(fake_hass):
    controller = HeatPumpController(fake_hass, _config(flow_temp_sensor=None))
    number = DummyNumber()
    controller.register_flow_temperature_number(number)

    fake_hass.states.set("sensor.outside", "10")
    controller.update_zone_status("living", target=21, current=19, active=True)

    mode = await controller._async_set_flow_temperature()

    assert mode == HVACMode.HEAT
    assert number.values


@pytest.mark.asyncio
async def test_async_update_heat_pump_state_updates_status_sensor(fake_hass):
    controller = HeatPumpController(fake_hass, _config())
    sensor = DummySensor()
    controller.register_pump_sensor(sensor)

    fake_hass.states.set("switch.zone_1", "on")
    fake_hass.states.set("sensor.outside", "10")
    controller.update_zone_status("living", target=21, current=19, active=True)

    await controller.async_update_heat_pump_state()

    assert sensor.states[-1] in {"heat", "cool"}


@pytest.mark.asyncio
async def test_set_mode_value_normalizes_invalid_option_and_notifies_thermostats(fake_hass):
    controller = HeatPumpController(fake_hass, _config())
    select = DummySelect()
    thermostat = DummyThermostat()

    controller.register_thermostat(thermostat)
    controller.register_mode_select(select)
    controller.set_mode_value("INVALID")

    assert controller.get_operation_mode() == "auto"
    assert select.options[-1] == "auto"
    assert thermostat.calls == 0


@pytest.mark.asyncio
async def test_thermostat_controls_circuits_and_updates_hvac_action(fake_hass):
    controller = HeatPumpController(fake_hass, _config())
    thermostat = ThermozonaThermostat(
        fake_hass,
        "entry-1",
        "living-room",
        ["switch.zone_1"],
        "sensor.living",
        controller,
        hysteresis=0.2,
        control_mode=None,
        pwm_cycle_time=None,
        pwm_min_on_time=None,
        pwm_min_off_time=None,
        pwm_kp=None,
        pwm_ki=None,
        pwm_actuator_delay=None,
    )

    fake_hass.states.set("sensor.outside", "9")
    fake_hass.states.set("sensor.living", "19")
    fake_hass.states.set("switch.zone_1", "off")

    await thermostat.async_set_temperature(**{ATTR_TEMPERATURE: 21})

    assert thermostat.hvac_mode == HVACMode.AUTO
    assert thermostat._attr_hvac_action == HVACAction.HEATING
    assert fake_hass.states.get("switch.zone_1").state == "on"


@pytest.mark.asyncio
async def test_thermostat_turn_off_closes_circuits(fake_hass):
    controller = HeatPumpController(fake_hass, _config())
    thermostat = ThermozonaThermostat(
        fake_hass,
        "entry-1",
        "bedroom",
        ["switch.zone_2"],
        "sensor.bed",
        controller,
        hysteresis=None,
        control_mode=None,
        pwm_cycle_time=None,
        pwm_min_on_time=None,
        pwm_min_off_time=None,
        pwm_kp=None,
        pwm_ki=None,
        pwm_actuator_delay=None,
    )

    fake_hass.states.set("switch.zone_2", "on")
    fake_hass.states.set("sensor.bed", "20")

    await thermostat.async_turn_off()

    assert thermostat.hvac_mode == HVACMode.OFF
    assert fake_hass.states.get("switch.zone_2").state == "off"


def test_name_helpers_cover_slugify_and_prettify():
    assert ThermozonaThermostat._prettify("living_room-main") == "Living room main"
    assert ThermozonaThermostat._slugify("Living Room Main!") == "living_room_main"


def _create_pwm_thermostat(fake_hass, controller):
    return ThermozonaThermostat(
        fake_hass,
        "entry-1",
        "pwm-zone",
        ["switch.zone_pwm"],
        "sensor.zone_pwm",
        controller,
        hysteresis=0.2,
        control_mode="pwm",
        pwm_cycle_time=15,
        pwm_min_on_time=3,
        pwm_min_off_time=3,
        pwm_kp=30.0,
        pwm_ki=2.0,
        pwm_actuator_delay=None,
    )


def test_pwm_pi_output_is_clamped(fake_hass):
    controller = HeatPumpController(fake_hass, _config())
    thermostat = _create_pwm_thermostat(fake_hass, controller)

    thermostat._attr_target_temperature = 21
    duty = thermostat._calculate_pwm_duty(current_temp=10, effective_mode=HVACMode.HEAT, now=datetime.now(timezone.utc))

    assert duty == 100


def test_pwm_cycle_applies_minimum_times(fake_hass):
    controller = HeatPumpController(fake_hass, _config())
    thermostat = _create_pwm_thermostat(fake_hass, controller)

    thermostat._pwm_duty_cycle = 10
    thermostat._attr_target_temperature = 20

    now = datetime.now(timezone.utc)
    cycle_start = thermostat._get_aligned_pwm_cycle_start(now)
    thermostat._start_new_pwm_cycle(
        current_temp=19.7,
        effective_mode=HVACMode.HEAT,
        now=now,
        cycle_start=cycle_start,
        was_active=False,
    )

    assert thermostat._pwm_on_time.total_seconds() / 60 >= 3
    assert thermostat._pwm_cycle_start == cycle_start


def test_pwm_cycle_is_aligned_to_schedule(fake_hass):
    controller = HeatPumpController(fake_hass, _config())
    thermostat = _create_pwm_thermostat(fake_hass, controller)

    now = datetime(2024, 1, 1, 12, 7, 42, tzinfo=timezone.utc)
    aligned = thermostat._get_aligned_pwm_cycle_start(now)

    assert aligned == datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_pwm_cycle_alignment_handles_non_utc_timezones(fake_hass):
    controller = HeatPumpController(fake_hass, _config())
    thermostat = _create_pwm_thermostat(fake_hass, controller)

    cet = timezone(timedelta(hours=1))
    now = datetime(2024, 1, 1, 13, 7, 42, tzinfo=cet)
    aligned = thermostat._get_aligned_pwm_cycle_start(now)

    assert aligned == datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_pwm_mode_switches_circuit_within_cycle(fake_hass):
    controller = HeatPumpController(fake_hass, _config())
    thermostat = _create_pwm_thermostat(fake_hass, controller)

    fake_hass.states.set("sensor.outside", "9")
    fake_hass.states.set("sensor.zone_pwm", "19")
    fake_hass.states.set("switch.zone_pwm", "off")

    await thermostat.async_set_temperature(**{ATTR_TEMPERATURE: 21})

    assert thermostat._attr_hvac_action in {HVACAction.HEATING, HVACAction.IDLE}
    assert thermostat.extra_state_attributes["control_mode"] == "pwm"


@pytest.mark.asyncio
async def test_control_writes_state_when_temperature_is_unavailable(fake_hass):
    controller = HeatPumpController(fake_hass, _config())
    thermostat = RecordingThermostat(
        fake_hass,
        "entry-1",
        "living-room",
        ["switch.zone_1"],
        "sensor.living",
        controller,
        hysteresis=0.2,
        control_mode=None,
        pwm_cycle_time=None,
        pwm_min_on_time=None,
        pwm_min_off_time=None,
        pwm_kp=None,
        pwm_ki=None,
        pwm_actuator_delay=None,
    )

    await thermostat._control_heating()

    assert thermostat.write_calls == 1


@pytest.mark.asyncio
async def test_temp_sensor_listener_schedules_control(fake_hass):
    controller = HeatPumpController(fake_hass, _config())
    thermostat = ThermozonaThermostat(
        fake_hass,
        "entry-1",
        "living-room",
        ["switch.zone_1"],
        "sensor.living",
        controller,
        hysteresis=0.2,
        control_mode=None,
        pwm_cycle_time=None,
        pwm_min_on_time=None,
        pwm_min_off_time=None,
        pwm_kp=None,
        pwm_ki=None,
        pwm_actuator_delay=None,
    )
    calls = 0

    def _record_schedule_control() -> None:
        nonlocal calls
        calls += 1

    thermostat.async_schedule_control = _record_schedule_control

    await thermostat._handle_temp_sensor_change(None)

    assert calls == 1


def test_flow_curve_offset_override_and_reset(fake_hass):
    controller = HeatPumpController(fake_hass, _config(flow_curve_offset=1.5))

    assert controller.get_flow_curve_offset() == 1.5

    controller.set_flow_curve_offset(3.0)
    assert controller.get_flow_curve_offset() == 3.0

    controller.reset_flow_curve_offset()
    assert controller.get_flow_curve_offset() == 1.5


def test_flow_curve_offset_is_applied_in_heating_and_cooling(fake_hass):
    controller = HeatPumpController(fake_hass, _config(flow_curve_offset=2.0))
    controller.update_zone_status("living", target=21, current=19, active=True)

    heating = controller.determine_flow_temperature(HVACMode.HEAT, outside_temp=15)
    assert heating == 27.0

    controller.update_zone_status("living", target=21, current=23, active=True)
    cooling = controller.determine_flow_temperature(HVACMode.COOL, outside_temp=24)
    assert cooling == 16.5


def test_refresh_entry_config_resets_ui_override(fake_hass):
    controller = HeatPumpController(fake_hass, _config(flow_curve_offset=2.0))
    controller.set_flow_curve_offset(5.0)

    controller.refresh_entry_config(_config(flow_curve_offset=0.0))

    assert controller.get_flow_curve_offset() == 0.0


def test_runtime_flow_curve_override_is_available(fake_hass):
    controller = HeatPumpController(fake_hass, _config(flow_curve_offset=2.0))

    controller.set_flow_curve_offset(5.0)

    assert controller.get_flow_curve_offset() == 5.0


def test_controller_keeps_auto_mode(fake_hass):
    controller = HeatPumpController(fake_hass, _config())

    controller.set_mode_value("auto")

    assert controller.get_operation_mode() == "auto"


def test_pwm_control_mode_is_available(fake_hass):
    controller = HeatPumpController(fake_hass, _config())
    thermostat = ThermozonaThermostat(
        fake_hass,
        "entry-1",
        "pwm-zone",
        ["switch.zone_pwm"],
        "sensor.zone_pwm",
        controller,
        hysteresis=0.2,
        control_mode="pwm",
        pwm_cycle_time=15,
        pwm_min_on_time=3,
        pwm_min_off_time=3,
        pwm_kp=30.0,
        pwm_ki=2.0,
        pwm_actuator_delay=3,
    )

    assert thermostat.control_mode == "pwm"


def test_mode_select_options_are_stable(fake_hass):
    controller = HeatPumpController(fake_hass, _config())
    select = ThermozonaHeatPumpModeSelect(
        "entry-1",
        controller,
    )

    assert select._attr_options == ["auto", "heat", "cool", "off"]


@pytest.mark.asyncio
async def test_pwm_zones_get_different_stagger_offsets(fake_hass):
    controller = HeatPumpController(fake_hass, _config())

    zone_a = ThermozonaThermostat(
        fake_hass,
        "entry-1",
        "zone-a",
        ["switch.zone_a"],
        "sensor.zone_a",
        controller,
        hysteresis=0.2,
        control_mode="pwm",
        pwm_cycle_time=15,
        pwm_min_on_time=3,
        pwm_min_off_time=3,
        pwm_kp=30.0,
        pwm_ki=2.0,
        pwm_actuator_delay=3,
    )
    zone_b = ThermozonaThermostat(
        fake_hass,
        "entry-1",
        "zone-b",
        ["switch.zone_b"],
        "sensor.zone_b",
        controller,
        hysteresis=0.2,
        control_mode="pwm",
        pwm_cycle_time=15,
        pwm_min_on_time=3,
        pwm_min_off_time=3,
        pwm_kp=30.0,
        pwm_ki=2.0,
        pwm_actuator_delay=3,
    )
    zone_c = ThermozonaThermostat(
        fake_hass,
        "entry-1",
        "zone-c",
        ["switch.zone_c"],
        "sensor.zone_c",
        controller,
        hysteresis=0.2,
        control_mode="pwm",
        pwm_cycle_time=15,
        pwm_min_on_time=3,
        pwm_min_off_time=3,
        pwm_kp=30.0,
        pwm_ki=2.0,
        pwm_actuator_delay=3,
    )

    controller.register_thermostat(zone_a)
    controller.register_thermostat(zone_b)
    controller.register_thermostat(zone_c)

    assert controller.get_pwm_zone_info(zone_a) == (0, 3)
    assert controller.get_pwm_zone_info(zone_b) == (1, 3)
    assert controller.get_pwm_zone_info(zone_c) == (2, 3)


@pytest.mark.asyncio
async def test_pwm_stagger_offset_produces_different_cycle_starts(fake_hass):
    controller = HeatPumpController(fake_hass, _config())
    zone_a = _create_pwm_thermostat(fake_hass, controller)
    zone_b = ThermozonaThermostat(
        fake_hass,
        "entry-1",
        "pwm-zone-b",
        ["switch.zone_pwm_b"],
        "sensor.zone_pwm_b",
        controller,
        hysteresis=0.2,
        control_mode="pwm",
        pwm_cycle_time=15,
        pwm_min_on_time=3,
        pwm_min_off_time=3,
        pwm_kp=30.0,
        pwm_ki=2.0,
        pwm_actuator_delay=3,
    )

    controller.register_thermostat(zone_a)
    controller.register_thermostat(zone_b)
    zone_a._pwm_zone_index, zone_a._pwm_zone_count = controller.get_pwm_zone_info(zone_a)
    zone_b._pwm_zone_index, zone_b._pwm_zone_count = controller.get_pwm_zone_info(zone_b)

    now = datetime(2024, 1, 1, 12, 7, 42, tzinfo=timezone.utc)
    start_a = zone_a._get_aligned_pwm_cycle_start(now)
    start_b = zone_b._get_aligned_pwm_cycle_start(now)

    assert start_a != start_b


@pytest.mark.asyncio
async def test_pwm_stagger_covers_full_cycle(fake_hass):
    controller = HeatPumpController(fake_hass, _config())
    zones = []
    for idx in range(4):
        zone = ThermozonaThermostat(
            fake_hass,
            "entry-1",
            f"zone-{idx}",
            [f"switch.zone_{idx}"],
            f"sensor.zone_{idx}",
            controller,
            hysteresis=0.2,
            control_mode="pwm",
            pwm_cycle_time=16,
            pwm_min_on_time=3,
            pwm_min_off_time=3,
            pwm_kp=30.0,
            pwm_ki=2.0,
            pwm_actuator_delay=3,
        )
        zones.append(zone)
        controller.register_thermostat(zone)

    offsets = []
    for zone in zones:
        zone._pwm_zone_index, zone._pwm_zone_count = controller.get_pwm_zone_info(zone)
        offsets.append(int(zone._pwm_zone_index * 16 * 60 / zone._pwm_zone_count))

    offsets.sort()
    expected_spacing = 16 * 60 / len(zones)
    spacings = [offsets[i + 1] - offsets[i] for i in range(len(offsets) - 1)]

    assert all(abs(spacing - expected_spacing) <= 1 for spacing in spacings)


@pytest.mark.asyncio
async def test_bang_bang_zones_do_not_get_pwm_index(fake_hass):
    controller = HeatPumpController(fake_hass, _config())

    pwm_zone = _create_pwm_thermostat(fake_hass, controller)
    bang_zone = ThermozonaThermostat(
        fake_hass,
        "entry-1",
        "bang-zone",
        ["switch.zone_bang"],
        "sensor.zone_bang",
        controller,
        hysteresis=0.2,
        control_mode="bang_bang",
        pwm_cycle_time=15,
        pwm_min_on_time=3,
        pwm_min_off_time=3,
        pwm_kp=30.0,
        pwm_ki=2.0,
        pwm_actuator_delay=3,
    )

    controller.register_thermostat(pwm_zone)
    controller.register_thermostat(bang_zone)

    assert controller.get_pwm_zone_info(pwm_zone) == (0, 1)
    assert controller.get_pwm_zone_info(bang_zone) == (0, 0)


def test_pwm_actuator_delay_extends_on_time(fake_hass):
    controller = HeatPumpController(fake_hass, _config())
    thermostat = _create_pwm_thermostat(fake_hass, controller)
    thermostat._pwm_actuator_delay_minutes = 3
    thermostat._calculate_pwm_duty = lambda *args, **kwargs: 20.0

    now = datetime.now(timezone.utc)
    cycle_start = thermostat._get_aligned_pwm_cycle_start(now)
    thermostat._start_new_pwm_cycle(19.0, HVACMode.HEAT, now, cycle_start, False)

    assert thermostat._pwm_on_time == timedelta(minutes=6)


def test_pwm_actuator_delay_zero_duty_stays_zero(fake_hass):
    controller = HeatPumpController(fake_hass, _config())
    thermostat = _create_pwm_thermostat(fake_hass, controller)
    thermostat._pwm_actuator_delay_minutes = 3
    thermostat._calculate_pwm_duty = lambda *args, **kwargs: 0.0

    now = datetime.now(timezone.utc)
    cycle_start = thermostat._get_aligned_pwm_cycle_start(now)
    thermostat._start_new_pwm_cycle(25.0, HVACMode.HEAT, now, cycle_start, False)

    assert thermostat._pwm_on_time == timedelta()


def test_pwm_actuator_delay_clamped_to_cycle_time(fake_hass):
    controller = HeatPumpController(fake_hass, _config())
    thermostat = _create_pwm_thermostat(fake_hass, controller)
    thermostat._pwm_actuator_delay_minutes = 3
    thermostat._calculate_pwm_duty = lambda *args, **kwargs: 95.0

    now = datetime.now(timezone.utc)
    cycle_start = thermostat._get_aligned_pwm_cycle_start(now)
    thermostat._start_new_pwm_cycle(19.0, HVACMode.HEAT, now, cycle_start, False)

    assert thermostat._pwm_on_time == timedelta(minutes=thermostat._pwm_cycle_time_minutes)


def test_pwm_actuator_delay_default_value(fake_hass):
    controller = HeatPumpController(fake_hass, _config())
    thermostat = _create_pwm_thermostat(fake_hass, controller)

    assert thermostat._pwm_actuator_delay_minutes == 3
