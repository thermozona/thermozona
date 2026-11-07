"""Plan bounded house preheating from published electricity prices."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import math
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

from . import (
    CONF_PRICE_BOOST,
    CONF_PRICE_CHARGE_HOURS,
    CONF_PRICE_COAST_HOURS,
    CONF_PRICE_HOME,
    CONF_PRICE_MIN_SPREAD,
    CONF_PRICE_MODE,
    DEFAULT_PRICE_BOOST,
    DEFAULT_PRICE_CHARGE_HOURS,
    DEFAULT_PRICE_COAST_HOURS,
    DEFAULT_PRICE_MIN_SPREAD,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PriceSlot:
    """An electricity price over an explicit UTC interval."""

    start: datetime
    end: datetime
    price: float


@dataclass(frozen=True)
class ChargeWindow:
    """A contiguous charging block followed by a costlier coast period."""

    start: datetime
    end: datetime
    price: float
    following_price: float


def parse_prices(response: dict[str, Any], home: str | None = None) -> list[PriceSlot]:
    """Normalize Tibber response intervals, rejecting invalid or ambiguous data."""
    homes = response.get("prices", {})
    if not isinstance(homes, dict) or not homes:
        raise ValueError("No home prices available")
    if home is None:
        if len(homes) != 1:
            raise ValueError("Set price_charging.home when multiple homes are returned")
        home = next(iter(homes))
    rows = homes.get(home)
    if not isinstance(rows, list) or len(rows) < 2:
        raise ValueError("Insufficient price intervals for the configured home")
    entries: list[tuple[datetime, float]] = []
    for row in rows:
        start = datetime.fromisoformat(str(row["start_time"]))
        price = float(row["price"])
        if start.tzinfo is None or not math.isfinite(price):
            raise ValueError("Prices require finite values and timezone-aware timestamps")
        entries.append((start.astimezone(timezone.utc), price))
    entries.sort()
    durations = [right[0] - left[0] for left, right in zip(entries, entries[1:])]
    cadence = min(durations)
    if not timedelta(0) < cadence <= timedelta(hours=1) or any(
        duration != cadence for duration in durations
    ):
        raise ValueError("Duplicate timestamps or missing price intervals")
    slots = []
    for start, price in entries:
        slots.append(PriceSlot(start, start + cadence, price))
    return slots


def average_price(slots: list[PriceSlot], start: datetime, end: datetime) -> float | None:
    """Return the duration-weighted price only when the whole period is covered."""
    cursor = start
    total = 0.0
    for slot in slots:
        if slot.end <= cursor:
            continue
        if slot.start > cursor:
            return None
        until = min(end, slot.end)
        total += (until - cursor).total_seconds() * slot.price
        cursor = until
        if cursor >= end:
            return total / (end - start).total_seconds()
    return None


def choose_charge_window(
    slots: list[PriceSlot],
    now: datetime,
    *,
    charge_hours: float = DEFAULT_PRICE_CHARGE_HOURS,
    coast_hours: float = DEFAULT_PRICE_COAST_HOURS,
    min_spread: float = DEFAULT_PRICE_MIN_SPREAD,
    excluded_windows: list[tuple[datetime, datetime]] | None = None,
) -> ChargeWindow | None:
    """Choose the cheapest contiguous block ahead of a meaningfully costlier period."""
    horizon = now + timedelta(hours=24)
    candidates = sorted({now, *(slot.start for slot in slots if now < slot.start < horizon)})
    best = None
    for start in candidates:
        end = start + timedelta(hours=charge_hours)
        if any(start < blocked_end and end > blocked_start
               for blocked_start, blocked_end in (excluded_windows or [])):
            continue
        price = average_price(slots, start, end)
        following = average_price(slots, end, end + timedelta(hours=coast_hours))
        if price is None or following is None or following - price <= min_spread:
            continue
        if best is None or price < best.price:
            best = ChargeWindow(start, end, price, following)
    return best


class PriceChargingManager:
    """Fetch prices, retain the active plan, and publish temporary charging headroom."""

    def __init__(
        self, hass: HomeAssistant, config: dict[str, Any], notify: Callable[[], None]
    ) -> None:
        self._hass = hass
        self._config = config
        self.mode = config.get(CONF_PRICE_MODE, "off")
        self._notify_control = notify
        self._listeners: set[Callable[[], None]] = set()
        self._slots: list[PriceSlot] = []
        self.window: ChargeWindow | None = None
        self.error: str | None = None
        self._last_fetch: datetime | None = None
        self._last_attempt: datetime | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._lock = asyncio.Lock()
        self._stopped = False
        self.excluded_windows = lambda: []

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Subscribe an entity to updates until its removal."""
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()
        self._notify_control()

    @property
    def coast_hours(self) -> float:
        """Return the period that charging is intended to carry the house through."""
        return float(self._config.get(CONF_PRICE_COAST_HOURS, DEFAULT_PRICE_COAST_HOURS))

    def planned_state(self, now: datetime) -> str:
        """Return the planned phase without allowing missing prices to charge."""
        if not self._slots or average_price(
            self._slots, now, now + timedelta(seconds=1)
        ) is None:
            return "unavailable"
        if self.window is not None:
            if self.window.start <= now < self.window.end:
                return "charging"
            if self.window.end <= now < self.window.end + timedelta(hours=self.coast_hours):
                return "coasting"
        return "normal"

    @property
    def state(self) -> str:
        """Return the user-facing mode/phase."""
        if self.mode == "off" or self._stopped:
            return "off"
        phase = self.planned_state(datetime.now(timezone.utc))
        return "preview" if self.mode == "preview" and phase != "unavailable" else phase

    @property
    def boost(self) -> float:
        """Return headroom only during a currently priced, enabled charging window."""
        if self.mode != "auto" or self._stopped:
            return 0.0
        if self.planned_state(datetime.now(timezone.utc)) != "charging":
            return 0.0
        return float(self._config.get(CONF_PRICE_BOOST, DEFAULT_PRICE_BOOST))

    @property
    def attributes(self) -> dict[str, Any]:
        """Expose the plan and prices for inspection without promising savings."""
        window = self.window
        return {
            "mode": self.mode,
            "planned_state": self.planned_state(datetime.now(timezone.utc)),
            "boost_c": self.boost,
            "configured_boost_c": self._config.get(CONF_PRICE_BOOST, DEFAULT_PRICE_BOOST),
            "charge_start": None if window is None else window.start.isoformat(),
            "charge_end": None if window is None else window.end.isoformat(),
            "average_charge_price": None if window is None else round(window.price, 6),
            "average_following_price": (
                None if window is None else round(window.following_price, 6)
            ),
            "price_intervals": len(self._slots),
            "prices_updated_at": (
                None if self._last_fetch is None else self._last_fetch.isoformat()
            ),
            "error": self.error,
        }

    async def async_start(self) -> None:
        """Start periodic planning after entity platforms have loaded."""
        self._stopped = False
        if self._unsubscribe is None:
            self._unsubscribe = async_track_time_interval(
                self._hass, self.async_refresh, timedelta(minutes=1)
            )
        await self.async_refresh()

    async def async_stop(self) -> None:
        """Remove the timer and expire all temporary headroom on unload."""
        self._stopped = True
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self._slots = []
        self.window = None
        self._notify()

    async def async_set_mode(self, mode: str) -> None:
        """Apply a user selection immediately and obtain fresh prices when enabled."""
        if mode not in {"off", "preview", "auto"}:
            raise ValueError(f"Invalid price charging mode: {mode}")
        self.mode = mode
        self.window = None
        self._slots = []
        self._last_fetch = None
        self._last_attempt = None
        self._notify()
        await self.async_refresh()

    async def async_refresh(self, *_: Any) -> None:
        """Refresh hourly price data and re-evaluate the phase every minute."""
        async with self._lock:
            if self.mode == "off" or self._stopped:
                return
            now = datetime.now(timezone.utc)
            fetch_due = self._last_fetch is None or now - self._last_fetch >= timedelta(hours=1)
            retry_due = (
                self._last_attempt is None
                or now - self._last_attempt >= timedelta(minutes=5)
            )
            if fetch_due and retry_due:
                self._last_attempt = now
                try:
                    response = await asyncio.wait_for(
                        self._hass.services.async_call(
                            "tibber", "get_prices",
                            {
                                "start": (now - timedelta(hours=1)).isoformat(),
                                "end": (now + timedelta(days=2)).isoformat(),
                            },
                            blocking=True,
                            return_response=True,
                        ),
                        timeout=20,
                    )
                    slots = parse_prices(response, self._config.get(CONF_PRICE_HOME))
                    if self._stopped or self.mode == "off":
                        return
                    self._slots = slots
                    self._last_fetch = now
                    self.error = None
                except (ValueError, TypeError, KeyError) as err:
                    self._slots = []
                    self.window = None
                    self.error = "invalid_prices"
                    _LOGGER.warning("Invalid price charging data: %s", err)
                except Exception as err:
                    self._slots = []
                    self.window = None
                    self.error = "price_fetch_failed"
                    _LOGGER.warning("Price charging fetch failed (%s)", type(err).__name__)
            if self._slots:
                retain = (
                    self.window is not None
                    and self.window.start <= now
                    < self.window.end + timedelta(hours=self.coast_hours)
                )
                exclusions = self.excluded_windows()
                if self.window is not None and any(
                    self.window.start < end and self.window.end > start
                    for start, end in exclusions
                ):
                    retain = False
                if not retain:
                    self.window = choose_charge_window(
                        self._slots, now,
                        charge_hours=float(self._config.get(
                            CONF_PRICE_CHARGE_HOURS, DEFAULT_PRICE_CHARGE_HOURS
                        )),
                        coast_hours=self.coast_hours,
                        excluded_windows=exclusions,
                        min_spread=float(self._config.get(
                            CONF_PRICE_MIN_SPREAD, DEFAULT_PRICE_MIN_SPREAD
                        )),
                    )
            self._notify()
