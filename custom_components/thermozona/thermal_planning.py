"""Conservative heat-cost estimates and temperature-based hot-water usage inference."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import partial
import math
from statistics import median
from typing import Any, TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


@dataclass(frozen=True)
class HeatCost:
    """Estimated costs per useful kWh of heat, rather than measured savings."""

    charge: float
    normal: float
    saving_percent: float
    fraction: float
    retention: float


def estimate_heat_cost(
    price: float, normal_price: float, storage_hours: float, config: dict[str, Any]
) -> HeatCost | None:
    """Reject charging that fails the configured efficiency/loss/saving margin."""
    charge_cop = float(config["charge_cop"])
    normal_cop = float(config["normal_cop"])
    loss = float(config.get("loss_percent_per_hour", 1.0)) / 100
    minimum = float(config.get("min_saving_percent", 5.0))
    full = float(config.get("full_charge_saving_percent", 25.0))
    if not all(math.isfinite(v) for v in [price, normal_price, storage_hours, charge_cop, normal_cop, loss, minimum, full]):
        return None
    if charge_cop <= 0 or normal_cop <= 0 or not 0 <= loss < 1 or storage_hours < 0:
        return None
    retention = (1 - loss) ** storage_hours
    if retention <= 0:
        return None
    cost = price / charge_cop
    # Penalize loss even at negative prices; wasting heat must not improve the score.
    cost += abs(cost) * (1 / retention - 1)
    normal = normal_price / normal_cop
    saving = (normal - cost) / max(abs(normal), 0.01) * 100
    if normal <= cost or saving < minimum:
        return None
    return HeatCost(cost, normal, saving, min(1.0, saving / max(full, minimum, 0.1)), retention)


@dataclass(frozen=True)
class UsagePeriod:
    """A recurring draw inferred from tank temperature, not a volume measurement."""

    minute: int
    drop_c: float
    days: int


@dataclass(frozen=True)
class UsageProfile:
    """Evidence supporting daily hot-water timing estimates."""

    periods: tuple[UsagePeriod, ...] = ()
    observed_days: int = 0
    draws: int = 0

    def upcoming(self, now: datetime, time_zone: str) -> list[tuple[datetime, UsagePeriod]]:
        """Return real UTC use times, including both folds at the autumn clock change."""
        zone = ZoneInfo(time_zone)
        events = {}
        for offset in range(3):
            day = now.astimezone(zone).date() + timedelta(days=offset)
            for period in self.periods:
                naive = datetime.combine(day, datetime.min.time()) + timedelta(minutes=period.minute)
                for fold in [0, 1]:
                    local = naive.replace(tzinfo=zone, fold=fold)
                    utc = local.astimezone(timezone.utc)
                    if utc.astimezone(zone).replace(tzinfo=None) != naive:
                        continue
                    if now < utc <= now + timedelta(hours=24):
                        events[utc] = period
        return sorted(events.items())


def infer_usage_profile(
    samples: list[tuple[datetime, float | None]],
    time_zone: str,
    *,
    min_days: int = 3,
    min_drop_c: float = 2.0,
) -> UsageProfile:
    """Identify repeated rapid cooling episodes while rejecting gaps and slow losses."""
    zone = ZoneInfo(time_zone)
    samples = sorted(((t.astimezone(timezone.utc), value) for t, value in samples if t.tzinfo is not None), key=lambda row: row[0])
    days = set()
    events = []
    previous = None
    episode = None

    def finish() -> None:
        nonlocal episode
        if episode is not None and episode[2] >= min_drop_c and episode[3] >= 2:
            events.append((episode[0].astimezone(zone), episode[2]))
        episode = None

    for timestamp, value in samples:
        if value is None or not math.isfinite(value) or not 10 <= value <= 90:
            finish()
            previous = None
            continue
        days.add(timestamp.astimezone(zone).date())
        if previous is not None:
            dt = (timestamp - previous[0]).total_seconds()
            drop = previous[1] - value
            if dt <= 0 or dt > 1200:
                finish()
            elif drop >= 0.05 and drop / dt * 3600 >= 2:
                if episode is not None and (timestamp - episode[1]).total_seconds() > 1200:
                    finish()
                if episode is None:
                    episode = [previous[0], timestamp, 0.0, 0]
                episode[1] = timestamp
                episode[2] += drop
                episode[3] += 1
            elif drop < -0.5 or (episode is not None and (timestamp - episode[1]).total_seconds() > 1200):
                finish()
        previous = (timestamp, value)
    finish()
    # A rolling week touches eight calendar dates; its two partial days form one day.
    exposure_days = max(1, math.ceil((samples[-1][0] - samples[0][0]).total_seconds() / 86400)) if samples else 0
    observed_days = min(len(days), exposure_days)
    if observed_days < min_days:
        return UsageProfile(observed_days=observed_days, draws=len(events))

    required = max(min_days, math.ceil(observed_days * 0.4))
    candidates = []
    for hour in range(24):
        anchor = hour * 60
        near = [(t, drop, (t.hour * 60 + t.minute - anchor + 720) % 1440 - 720)
                for t, drop in events
                if abs((t.hour * 60 + t.minute - anchor + 720) % 1440 - 720) <= 60]
        distinct = {t.date() for t, _, _ in near}
        if len(distinct) < required:
            continue
        minute = round(anchor + median(offset for _, _, offset in near)) % 1440
        by_day = [max(drop for t, drop, _ in near if t.date() == day) for day in distinct]
        candidates.append(UsagePeriod(minute, median(by_day), min(len(distinct), observed_days)))
    periods = []
    for candidate in sorted(candidates, key=lambda p: (-p.days, -p.drop_c, p.minute)):
        if all(abs((candidate.minute - p.minute + 720) % 1440 - 720) >= 120 for p in periods):
            periods.append(candidate)
        if len(periods) == 3:
            break
    return UsageProfile(tuple(sorted(periods, key=lambda p: p.minute)), observed_days, len(events))


async def async_temperature_history(
    hass: HomeAssistant, entity_id: str, now: datetime, days: int
) -> list[tuple[datetime, float | None]]:
    """Read history through recorder's supported executor/API, without direct SQL."""
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.history import get_significant_states

    history = await get_instance(hass).async_add_executor_job(partial(
        get_significant_states, hass, now - timedelta(days=days), now,
        entity_ids=[entity_id], no_attributes=True,
    ))
    samples = []
    for state in history.get(entity_id, []):
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            value = None
        samples.append((state.last_updated, value))
    return samples
