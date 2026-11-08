"""Regression coverage for learned room recovery without physical device calls."""
from datetime import datetime, timedelta, timezone
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import voluptuous as vol

from custom_components.thermozona import CONFIG_SCHEMA
from custom_components.thermozona import room_recovery as module
from custom_components.thermozona.price_charging import PriceSlot
from custom_components.thermozona.room_recovery import (
    HeatingRate, RoomRecovery, learn_heating_rate, overlap_seconds,
)
from custom_components.thermozona.thermal_conditions import ThermalReference

UTC = timezone.utc
END = datetime(2026, 11, 9, 17, tzinfo=UTC)


def plan(manager=None, **changes):
    """Plan a one-degree recovery ending at 17:00 UTC."""
    options = dict(now=END - timedelta(hours=4), schedule_on=True, ready_at=END,
                   current=19, target=20, setback=1, enabled=True)
    options.update(changes)
    return (manager or RoomRecovery({})).plan(**options)


def observations(days=2, rate=.4):
    """Provide separate heating episodes with frequent temperature reports."""
    rows, intervals = [], []
    for day in range(days):
        start = END - timedelta(days=day + 1, hours=4)
        intervals.append((start, start + timedelta(hours=3)))
        rows.extend((start + timedelta(minutes=10 * i), 18 + rate * i / 6)
                    for i in range(19))
    return rows, intervals


def test_rate_requires_heating_and_separate_days():
    rows, intervals = observations()
    learned = learn_heating_rate(rows, intervals)
    assert learned.rate == pytest.approx(.4)
    assert learned.days == 2
    assert learn_heating_rate(rows, []).rate is None
    assert learn_heating_rate(*observations(days=1)).rate is None
    midnight = datetime(2026, 11, 9, 23, tzinfo=UTC)
    rows = [(midnight + timedelta(minutes=10*i), 18 + i/10) for i in range(19)]
    assert learn_heating_rate(rows, [(midnight, midnight+timedelta(hours=3))]).rate is None


def test_rate_rejects_gaps_and_invalid_readings():
    rows, intervals = observations()
    assert learn_heating_rate([(t, None) for t, _ in rows], intervals).rate is None
    assert learn_heating_rate([(t, float("nan")) for t, _ in rows], intervals).rate is None
    sparse = [(t, v) for i, (t, v) in enumerate(rows) if i % 6 == 0]
    assert learn_heating_rate(sparse, intervals).rate is None


def test_rate_uses_conservative_lower_quartile():
    rows, intervals = observations(rate=.2)
    faster, active = observations(rate=1)
    faster = [(t-timedelta(days=3), v) for t, v in faster]
    active = [(a-timedelta(days=3), b-timedelta(days=3)) for a, b in active]
    assert learn_heating_rate(rows+faster, intervals+active).rate == pytest.approx(.2)


@pytest.mark.asyncio
async def test_recorder_adapter_combines_any_open_circuit_with_heating(monkeypatch):
    rows, intervals = observations()
    history = {"sensor.room": [SimpleNamespace(last_updated=t, state=str(v)) for t, v in rows],
               "sensor.status": [], "switch.first": [], "switch.second": []}
    for start, end in intervals:
        history["sensor.status"].extend([SimpleNamespace(last_updated=start, state="heat"),
                                         SimpleNamespace(last_updated=end, state="idle")])
        history["switch.first"].append(SimpleNamespace(last_updated=start, state="off"))
        history["switch.second"].extend([SimpleNamespace(last_updated=start, state="on"),
                                         SimpleNamespace(last_updated=end, state="off")])
    recorder = ModuleType("homeassistant.components.recorder")
    history_module = ModuleType("homeassistant.components.recorder.history")
    calls = []

    def read(_hass, start, end, **kwargs):
        calls.append(kwargs)
        return history

    async def executor(job):
        return job()

    recorder.get_instance = lambda _hass: SimpleNamespace(async_add_executor_job=executor)
    history_module.get_significant_states = read
    monkeypatch.setitem(sys.modules, recorder.__name__, recorder)
    monkeypatch.setitem(sys.modules, history_module.__name__, history_module)
    result = await module.async_heating_rate(None, "sensor.room", ["switch.first", "switch.second"],
                                             "sensor.status", END, 7)
    assert result.rate == pytest.approx(.4)
    assert calls[0]["no_attributes"]
    assert set(calls[0]["entity_ids"]) == set(history)


def test_overlap_does_not_double_count_or_count_outside_window():
    assert overlap_seconds(END-timedelta(hours=1), END, [
        (END-timedelta(hours=2), END-timedelta(minutes=20)),
        (END-timedelta(minutes=30), END+timedelta(hours=1)),
    ]) == 3600


def test_fallback_and_learned_rate_have_hard_bounds():
    assert plan().minutes == 135
    manager = RoomRecovery({})
    manager.profile = HeatingRate(.5, 10, 3)
    assert plan(manager).minutes == 135
    assert plan(manager, current=19.5).minutes == 75
    manager.profile = HeatingRate(.1, 10, 3)
    limited = plan(manager)
    assert limited.minutes == 180 and limited.capped
    assert plan(current=20).minutes == 15


def test_pwm_and_boiler_time_are_allowed_within_cap():
    aligned = plan(cycle_minutes=30)
    assert aligned.start == END-timedelta(minutes=150)
    blocked = [(END-timedelta(hours=1), END-timedelta(minutes=30))]
    assert plan(blocked_windows=blocked).minutes == 165
    bounded = plan(blocked_windows=[(END-timedelta(hours=4), END)])
    assert bounded.minutes == 180 and bounded.capped


def test_price_shift_is_bounded_and_requires_complete_prices():
    slots = [PriceSlot(END-timedelta(minutes=165), END-timedelta(minutes=135), .05),
             PriceSlot(END-timedelta(minutes=135), END, .4)]
    result = plan(prices=slots)
    assert result.minutes == 165
    assert result.start >= END-timedelta(hours=3)
    assert plan(prices=slots[:1]).minutes == 135


def test_active_plan_is_latched_until_schedule_end():
    manager = RoomRecovery({})
    active = plan(manager, now=END-timedelta(hours=2))
    assert active.active
    assert plan(manager, now=END-timedelta(hours=1), current=19.9) == active
    assert not plan(manager, schedule_on=False).active
    assert plan(manager, current=19.9).minutes == pytest.approx(27)


@pytest.mark.parametrize("changes", [
    {"current": None}, {"current": float("nan")}, {"enabled": False},
    {"schedule_on": False}, {"ready_at": None}, {"ready_at": "bad"},
    {"ready_at": datetime(2026, 11, 9)}, {"now": END},
])
def test_invalid_or_disabled_schedule_has_no_recovery(changes):
    assert plan(**changes).start is None


def test_offset_deadline_uses_absolute_time_across_dst():
    deadline = "2026-10-25T02:30:00+01:00"
    result = plan(ready_at=deadline, now=datetime(2026, 10, 24, 20, tzinfo=UTC))
    assert result.ready_at == datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
    assert result.start == result.ready_at-timedelta(minutes=135)


def test_recovery_adjusts_for_delivery_and_loss_without_exceeding_hard_limit():
    assert plan(thermal_factor=.75).minutes == 105
    assert plan(thermal_factor=1.25).minutes == 165
    assert plan(thermal_factor=1.25, current=18).minutes == 180


def test_learning_keeps_conditions_of_the_selected_observed_rate():
    rows, intervals = observations()
    water = [(t, 26) for t, _ in rows]
    outside = [(t, 8) for t, _ in rows]
    result = learn_heating_rate(rows, intervals, water, outside)
    assert result.rate == pytest.approx(.4)
    assert result.reference.supply_c == 26
    assert result.reference.outside_c == 8
    assert 18 <= result.reference.room_c <= 20
    missing = learn_heating_rate(rows, intervals, [(t, None) for t, _ in water], outside)
    assert missing.rate == pytest.approx(.4) and missing.reference is None


def test_condition_history_is_time_weighted_and_rejects_stale_initial_values():
    start = END-timedelta(hours=1)
    rows = [(start, 24), (start+timedelta(minutes=15), 28)]
    assert module._average_condition(rows, [t for t, _ in rows], start, END) == 27
    stale = [(start-timedelta(hours=3), 24)]
    assert module._average_condition(stale, [t for t, _ in stale], start, END) is None


def test_sunlit_windows_are_not_learned_as_heater_only_warming():
    rows, intervals = observations()
    solar = [(t, 500) for t, _ in rows]
    assert learn_heating_rate(rows, intervals, solar_forecasts=solar, solar_weight=1).rate is None
    assert learn_heating_rate(rows, intervals, solar_forecasts=solar, solar_weight=0).rate == pytest.approx(.4)
    assert learn_heating_rate(rows, intervals, solar_forecasts=[], solar_weight=1).rate is None


@pytest.mark.asyncio
async def test_learning_failure_retries_and_recovers_without_device_calls(monkeypatch):
    manager = RoomRecovery({})
    reader = AsyncMock(side_effect=RuntimeError("offline"))
    monkeypatch.setattr(module, "async_heating_rate", reader)
    await manager.async_learn(None, "sensor.room", [], "sensor.status", END)
    assert manager.error == "history_unavailable"
    assert not manager.learning_due(END+timedelta(minutes=14))
    assert manager.learning_due(END+timedelta(minutes=15))
    reader.side_effect = None
    reader.return_value = HeatingRate(.4, 8, 3)
    await manager.async_learn(None, "sensor.room", [], "sensor.status", END+timedelta(minutes=15))
    assert manager.error is None
    assert manager.profile.rate == .4
    assert not manager.learning_due(END+timedelta(hours=5))


@pytest.mark.parametrize("config", [
    {"fallback_minutes": 200, "max_minutes": 180},
    {"buffer_minutes": 61}, {"max_minutes": 400}, {"history_days": 1},
])
def test_schema_rejects_unbounded_or_inconsistent_recovery(config):
    with pytest.raises(vol.Invalid):
        CONFIG_SCHEMA({"thermozona": {
            "outside_temp_sensor": "sensor.outside", "flow_temp_sensor": "input_number.flow",
            "zones": {"room": {"circuits": ["switch.room"], "temp_sensor": "sensor.room",
                               "schedule_recovery": config}},
        }})
