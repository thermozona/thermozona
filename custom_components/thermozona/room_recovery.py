"""Learn room warming rates and plan bounded recovery before an absence ends."""
from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import math
from typing import Any

from .price_charging import PriceSlot, average_price

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class HeatingRate:
    """Conservative observed warming during circuit and shared heating demand."""

    rate: float | None = None
    samples: int = 0
    days: int = 0


def overlap_seconds(
    start: datetime, end: datetime, intervals: Sequence[tuple[datetime, datetime]]
) -> float:
    """Measure the union of interval overlap without counting overlaps twice."""
    cursor = start
    total = 0.0
    for left, right in sorted(intervals):
        left = max(cursor, left)
        right = min(end, right)
        if right > left:
            total += (right - left).total_seconds()
            cursor = right
    return total


def learn_heating_rate(
    temperatures: Sequence[tuple[datetime, float | None]],
    heating_intervals: Sequence[tuple[datetime, datetime]],
) -> HeatingRate:
    """Use lower-quartile hourly rises with >=20 minutes of heating on >=2 days."""
    rows = sorted(temperatures, key=lambda row: row[0])
    if len(rows) < 2:
        return HeatingRate()
    times = [t for t, _ in rows]
    cursor = rows[0][0]
    rates = []
    days = set()
    observed = []
    while cursor + timedelta(hours=1) <= rows[-1][0]:
        end = cursor + timedelta(hours=1)
        a = bisect_right(times, cursor) - 1
        b = bisect_right(times, end) - 1
        values = [value for _, value in rows[a:b + 1]]
        fresh = (a >= 0 and b > a
                 and cursor - rows[a][0] <= timedelta(minutes=15)
                 and end - rows[b][0] <= timedelta(minutes=15)
                 and all(right[0] - left[0] <= timedelta(minutes=30)
                         for left, right in zip(rows[a:b], rows[a + 1:b + 1])))
        if fresh and values and all(
            value is not None and math.isfinite(value) and -10 <= value <= 50
            for value in values
        ) and overlap_seconds(cursor, end, heating_intervals) >= 1200:
            hours = (rows[b][0] - rows[a][0]).total_seconds() / 3600
            rise = (rows[b][1] - rows[a][1]) / hours
            if 0.05 <= rise <= 2.0:
                rates.append(rise)
                days.add(cursor.date())
                observed.append(cursor)
        cursor += timedelta(minutes=30)
    if (len(rates) < 3 or len(days) < 2
            or observed[-1] - observed[0] < timedelta(hours=12)):
        return HeatingRate(samples=len(rates), days=len(days))
    rates.sort()
    return HeatingRate(rates[int((len(rates) - 1) * 0.25)], len(rates), len(days))


async def async_heating_rate(
    hass, temperature_sensor: str, circuits: list[str], status_sensor: str,
    now: datetime, days: int,
) -> HeatingRate:
    """Read and process recorder history in its executor, without direct database access."""
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.history import get_significant_states

    def read_and_learn() -> HeatingRate:
        start = now - timedelta(days=days)
        history = get_significant_states(
            hass, start, now, entity_ids=[temperature_sensor, *circuits, status_sensor],
            no_attributes=True,
        )
        temperatures = []
        for state in history.get(temperature_sensor, []):
            try:
                value = float(state.state)
            except (ValueError, TypeError):
                value = None
            temperatures.append((max(start, state.last_updated), value))
        events = sorted(
            (max(start, state.last_updated), entity, state.state)
            for entity in [*circuits, status_sensor] for state in history.get(entity, [])
        )
        states = {}
        intervals = []
        previous = start
        for timestamp, entity, state in [*events, (now, "", "")]:
            if states.get(status_sensor) == "heat" and any(states.get(c) == "on" for c in circuits):
                if timestamp > previous:
                    intervals.append((previous, timestamp))
            states[entity] = state
            previous = timestamp
        return learn_heating_rate(temperatures, intervals)

    return await get_instance(hass).async_add_executor_job(read_and_learn)


@dataclass(frozen=True)
class RecoveryPlan:
    """An estimate of when normal comfort must resume, never a forced heat command."""

    active: bool = False
    start: datetime | None = None
    ready_at: datetime | None = None
    minutes: float = 0
    capped: bool = False


class RoomRecovery:
    """Keep a recovery latched until its schedule ends, respecting hard bounds."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.profile = HeatingRate()
        self.last_learning: datetime | None = None
        self.last_attempt: datetime | None = None
        self.error: str | None = None
        self._latched: RecoveryPlan | None = None

    def learning_due(self, now: datetime) -> bool:
        """Refresh six-hourly, with a bounded retry after history failures."""
        return not (
            self.last_learning is not None and now-self.last_learning < timedelta(hours=6)
        ) and not (
            self.last_attempt is not None and now-self.last_attempt < timedelta(minutes=15)
        )

    async def async_learn(self, hass, sensor, circuits, status, now: datetime) -> None:
        """Only learn through the recorder API; normal control continues meanwhile."""
        self.last_attempt = now
        try:
            self.profile = await async_heating_rate(
                hass, sensor, circuits, status, now, self.config.get("history_days", 7)
            )
            self.last_learning = now
            self.error = None
        except Exception as err:
            self.error = "history_unavailable"
            _LOGGER.warning(
                "Room warming history unavailable for %s (%s)", sensor, type(err).__name__
            )

    def plan(self, *, now: datetime, schedule_on: bool, ready_at: Any,
             current: float | None, target: float, setback: float, enabled: bool,
             cycle_minutes: int = 0, prices: list[PriceSlot] | None = None,
             blocked_windows: Sequence[tuple[datetime, datetime]] | None = None) -> RecoveryPlan:
        """Restore comfort early, allowing boiler time and modest cheaper-hour shifts."""
        try:
            if isinstance(ready_at, str):
                ready_at = datetime.fromisoformat(ready_at)
            if not isinstance(ready_at, datetime) or ready_at.tzinfo is None:
                ready_at = None
            else:
                ready_at = ready_at.astimezone(timezone.utc)
        except (ValueError, TypeError):
            ready_at = None
        if (not enabled or not schedule_on or ready_at is None or ready_at <= now
                or current is None or not math.isfinite(current)):
            self._latched = None
            return RecoveryPlan()
        if self._latched is not None and self._latched.ready_at == ready_at:
            return self._latched
        self._latched = None
        deficit = max(0.0, target-current)
        rate = self.profile.rate
        base_minutes = (
            deficit / rate * 60 if rate else
            self.config.get("fallback_minutes", 120) * deficit / max(setback, 0.1)
        ) + self.config.get("buffer_minutes", 15)
        maximum = self.config.get("max_minutes", 180)
        capped = base_minutes > maximum
        minutes = min(maximum, base_minutes)
        blocked = blocked_windows or []
        for _ in range(4):
            delay = overlap_seconds(
                ready_at - timedelta(minutes=minutes), ready_at, blocked
            ) / 60
            adjusted = min(maximum, base_minutes + delay)
            if adjusted == minutes:
                break
            capped = capped or adjusted >= maximum
            minutes = adjusted
        latest = ready_at - timedelta(minutes=minutes)
        hard_start = ready_at - timedelta(minutes=maximum)
        if cycle_minutes:
            seconds = cycle_minutes*60
            aligned = datetime.fromtimestamp(int(latest.timestamp())//seconds*seconds, timezone.utc)
            latest = max(hard_start, aligned)
        start = latest
        baseline_price = average_price(prices or [], latest, ready_at)
        if baseline_price is not None:
            earliest = max(hard_start, latest-timedelta(minutes=30))
            for candidate in [s.start for s in prices if earliest <= s.start < latest]:
                price = average_price(prices, candidate, ready_at)
                if price is not None and baseline_price-price >= 0.03:
                    start = candidate
                    baseline_price = price
        plan = RecoveryPlan(now >= start, start, ready_at,
                            (ready_at-start).total_seconds()/60, capped)
        if plan.active:
            self._latched = plan
        return plan
