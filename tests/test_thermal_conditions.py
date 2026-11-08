"""Exercise bounded feedforward independently of valve timing and physical devices."""
from datetime import datetime, timedelta, timezone

import pytest
import voluptuous as vol

from custom_components.thermozona import CONFIG_SCHEMA
from custom_components.thermozona.heat_pump import HeatPumpController
from custom_components.thermozona.thermal_conditions import (
    ThermalReference, compensation, compensate_duty,
)


REFERENCE = ThermalReference(25, 10, 20)
NOW = datetime(2026, 11, 8, 6, tzinfo=timezone.utc)


def factor(supply=25, outside=10, **changes):
    """Evaluate the same room under different measured water and weather."""
    options = dict(supply_c=supply, outside_c=outside, room_c=20,
                   target_c=20, reference=REFERENCE)
    options.update(changes)
    return compensation(**options)


def test_water_and_weather_affect_demand_in_opposite_directions():
    assert factor().factor == 1
    assert factor(supply=26).factor < 1
    assert factor(supply=24).factor > 1
    assert factor(outside=8).factor > 1
    assert factor(outside=12).factor < 1
    # Do not add a second independent weather boost on top of hotter water.
    assert factor(supply=26, outside=8).factor == pytest.approx(1)


@pytest.mark.parametrize("supply,outside", [(21, -20), (40, 18)])
def test_extreme_conditions_remain_within_conservative_bounds(supply, outside):
    assert .75 <= factor(supply, outside).factor <= 1.25


@pytest.mark.parametrize("changes", [
    {"supply": None}, {"outside": None}, {"supply": float("nan")},
    {"outside": float("inf")}, {"supply": 55}, {"outside": -50},
    {"supply": 20}, {"room_c": None},
])
def test_bad_or_unusable_conditions_keep_original_control(changes):
    result = factor(**changes)
    assert result.factor == 1 and result.reason != "active"


def test_partial_demand_is_trimmed_and_saturated_requests_are_preserved():
    assert compensate_duty(50, .75) == 43.75
    assert compensate_duty(50, 1.25) == 56.25
    for multiplier in [.75, 1, 1.25]:
        assert compensate_duty(0, multiplier) == 0
        assert compensate_duty(120, multiplier) == 100
        assert compensate_duty(-10, multiplier) == 0
        duties = [compensate_duty(duty, multiplier) for duty in range(101)]
        assert duties == sorted(duties)


def controller(fake_hass, **thermal):
    config = {"enabled": True, "supply_sensor": "sensor.water", **thermal}
    return HeatPumpController(fake_hass, {
        "outside_temp_sensor": "sensor.outside", "thermal_compensation": config,
        "zones": {},
    })


def sensor(fake_hass, entity, value, *, age=0, unit="°C"):
    fake_hass.states.set(entity, value)
    state = fake_hass.states.get(entity)
    state.last_updated = NOW - timedelta(minutes=age)
    state.attributes = {"unit_of_measurement": unit}
    return state


def test_controller_uses_report_timestamp_not_age_of_last_value_change(fake_hass):
    ctrl = controller(fake_hass)
    water = sensor(fake_hass, "sensor.water", "26", age=120)
    water.last_reported = NOW
    sensor(fake_hass, "sensor.outside", "10")
    result = ctrl.thermal_compensation(room_c=20, target_c=20, now=NOW)
    assert result.reason == "active" and result.supply_c == 26
    assert result.factor < 1


@pytest.mark.parametrize("failure", ["stale", "future", "unavailable", "units", "no_timestamp"])
def test_controller_falls_back_when_any_sensor_is_untrustworthy(fake_hass, failure):
    ctrl = controller(fake_hass)
    water = sensor(fake_hass, "sensor.water", "26")
    sensor(fake_hass, "sensor.outside", "10")
    if failure == "stale": water.last_updated = NOW - timedelta(minutes=31)
    if failure == "future": water.last_updated = NOW + timedelta(minutes=2)
    if failure == "unavailable": water.state = "unavailable"
    if failure == "units": water.attributes["unit_of_measurement"] = "°F"
    if failure == "no_timestamp": del water.last_updated
    result = ctrl.thermal_compensation(room_c=20, target_c=20, now=NOW)
    assert result.factor == 1 and result.reason != "active"


def test_configured_water_sensor_is_required_and_defaults_keep_legacy_behavior(fake_hass):
    ctrl = controller(fake_hass, enabled=False)
    assert ctrl.thermal_compensation(room_c=20, target_c=20, now=NOW).reason == "disabled"
    config = {"outside_temp_sensor": "sensor.outside", "flow_temp_sensor": "input_number.flow",
              "zones": {"room": {"circuits": ["switch.room"], "temp_sensor": "sensor.room"}}}
    for invalid in [{"enabled": True}, {"reference_supply_c": "nan"},
                    {"reference_outside_c": "inf"}, {"max_sensor_age_minutes": 0}]:
        with pytest.raises(vol.Invalid):
            CONFIG_SCHEMA({"thermozona": {**config, "thermal_compensation": invalid}})


@pytest.mark.parametrize("value,age,unit", [("unavailable", 0, "W/m2"),
    ("500", 181, "W/m2"), ("5000", 0, "W/m2"), ("500", 0, "W")])
def test_bad_solar_forecast_does_not_reduce_heat(fake_hass, value, age, unit):
    ctrl = controller(fake_hass, solar_forecast_sensor="sensor.solar")
    sensor(fake_hass, "sensor.water", "25")
    sensor(fake_hass, "sensor.outside", "10")
    sensor(fake_hass, "sensor.solar", value, age=age, unit=unit)
    result = ctrl.thermal_compensation(room_c=20, target_c=20, now=NOW, solar_weight=2)
    assert result.factor == 1 and result.solar_equivalent_c == 0
