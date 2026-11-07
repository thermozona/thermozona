"""Heat-cost and usage inference regressions, without recorder or real-time waits."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.thermozona.price_charging import PriceSlot, choose_charge_window
from custom_components.thermozona.thermal_planning import (
    UsagePeriod, UsageProfile, estimate_heat_cost, infer_usage_profile,
)


MODEL = {"normal_cop": 3.0, "charge_cop": 2.5, "loss_percent_per_hour": 1.0,
         "min_saving_percent": 5, "full_charge_saving_percent": 25}


def usage_samples(days=7):
    """Several measured falling values represent a draw; a single jump does not."""
    zone = ZoneInfo("Europe/Amsterdam")
    rows = []
    for day in range(days):
        start = datetime(2026, 11, 1 + day, 7, 20, tzinfo=zone)
        for minute, value in [(0, 50), (1, 49), (2, 47), (3, 44), (4, 42), (45, 50)]:
            rows.append((start + timedelta(minutes=minute), value))
    return rows


def test_cheaper_electricity_can_be_more_expensive_after_cop_and_storage_loss():
    assert estimate_heat_cost(.30, .36, 6, MODEL) is None
    assert estimate_heat_cost(.20, .40, 6, MODEL) is not None


def test_small_advantage_bounds_charge_fraction_and_large_advantage_reaches_cap():
    small = estimate_heat_cost(.29, .40, 0, MODEL)
    large = estimate_heat_cost(.10, .40, 0, MODEL)
    assert 0 < small.fraction < 1
    assert large.fraction == 1
    assert small.charge < small.normal


def test_losses_do_not_improve_the_score_at_negative_prices():
    immediate = estimate_heat_cost(-.10, .30, 0, MODEL)
    stored = estimate_heat_cost(-.10, .30, 10, MODEL)
    assert stored.charge > immediate.charge
    assert stored.saving_percent < immediate.saving_percent


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_thermal_cost_rejects_nonfinite_inputs(bad):
    assert estimate_heat_cost(bad, .4, 2, MODEL) is None


def test_house_window_requires_economic_advantage_not_only_electricity_spread():
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    slots = [PriceSlot(now + timedelta(hours=i), now + timedelta(hours=i+1),
                       .30 if i < 3 else .36) for i in range(20)]
    assert choose_charge_window(slots, now) is not None
    assert choose_charge_window(slots, now, thermal_model=MODEL) is None


def test_repeated_temperature_draws_learn_local_use_time_and_amount_proxy():
    profile = infer_usage_profile(usage_samples(), "Europe/Amsterdam")
    assert profile.observed_days == 7
    assert profile.draws == 7
    assert len(profile.periods) == 1
    assert profile.periods[0] == UsagePeriod(7 * 60 + 20, 8, 7)
    assert profile.upcoming(datetime(2026, 11, 7, 20, tzinfo=timezone.utc), "Europe/Amsterdam")[0][0] == datetime(2026, 11, 8, 6, 20, tzinfo=timezone.utc)


def test_insufficient_history_does_not_invent_a_usage_schedule():
    assert not infer_usage_profile(usage_samples(2), "Europe/Amsterdam").periods


def test_rolling_seven_day_window_does_not_count_eight_full_observation_days():
    zone = ZoneInfo("Europe/Amsterdam")
    start = datetime(2026, 10, 31, 12, tzinfo=zone)
    rows = [(start + timedelta(days=d), 50) for d in range(7)]
    rows.append((start + timedelta(days=7, seconds=-1), 50))
    for day in [2, 4, 6]:
        for minute, value in enumerate([50, 49, 47, 44, 42]):
            rows.append((datetime(2026, 11, day, 7, 20, tzinfo=zone)+timedelta(minutes=minute), value))
    profile = infer_usage_profile(rows, "Europe/Amsterdam")
    assert profile.observed_days == 7
    assert len(profile.periods) == 1
    assert profile.periods[0].days == 3


def test_outages_single_sensor_jumps_and_slow_cooling_do_not_count_as_draws():
    rows = []
    for day in range(7):
        start = datetime(2026, 11, 1 + day, tzinfo=timezone.utc)
        rows.extend([(start, 50), (start + timedelta(hours=1), 49.9),
                     (start + timedelta(hours=3), None),
                     (start + timedelta(hours=4), 42),
                     (start + timedelta(hours=5), 50),
                     (start + timedelta(hours=5, minutes=1), 40)])
    assert not infer_usage_profile(rows, "UTC").periods


def test_only_recurring_draws_are_predicted():
    rows = usage_samples()
    start = datetime(2026, 11, 3, 14, tzinfo=timezone.utc)
    rows.extend((start+timedelta(minutes=i), value) for i, value in enumerate([50, 48, 46, 42]))
    profile = infer_usage_profile(rows, "Europe/Amsterdam")
    assert len(profile.periods) == 1
    assert profile.periods[0].minute == 440


def test_usage_forecast_handles_dst_fold_and_skips_nonexistent_local_times():
    profile = UsageProfile((UsagePeriod(150, 8, 7),), 7, 7)
    now = datetime(2026, 10, 25, 1, 15, tzinfo=timezone.utc)
    assert profile.upcoming(now, "Europe/Amsterdam")[0][0] == datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)
    spring = profile.upcoming(datetime(2026, 3, 28, 23, tzinfo=timezone.utc), "Europe/Amsterdam")
    assert spring == []
