"""Coordinate additional hot-water charging through an existing protected gateway."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import math
from typing import Any

from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.event import async_track_state_change_event

from .price_charging import ChargeWindow, PriceChargingManager, PriceSlot, average_price
from .thermal_planning import (
    UsageProfile, async_temperature_history, estimate_heat_cost, infer_usage_profile,
)

_LOGGER = logging.getLogger(__name__)


class HotWaterManager(PriceChargingManager):
    """Schedule an optional one-hour boost without replacing native water heating."""

    def __init__(self, hass, config: dict[str, Any], notify=lambda: None) -> None:
        super().__init__(hass, config, notify)
        self.mode = config.get("mode", "monitor")
        self._prices = PriceChargingManager(hass, {"mode": "preview"}, lambda: None)
        self._owned = False
        self._reason = "monitor"
        self._requested_at = None
        self._surplus_since = None
        self._import_since = None
        self._remove_state_listener = None
        self._smart = config.get("smart_charging")
        self._usage = UsageProfile()
        self._usage_updated: datetime | None = None
        self._history_attempt: datetime | None = None
        self._usage_error = None
        self._active_target: float | None = None

    @property
    def _time_zone(self) -> str:
        return getattr(getattr(self._hass, "config", None), "time_zone", "UTC")

    async def _refresh_usage(self, now: datetime) -> None:
        """Learn recurring draws at most every six hours, retrying failures hourly."""
        if self._smart is None:
            return
        if self._usage_updated is not None and now - self._usage_updated < timedelta(hours=6):
            return
        if self._history_attempt is not None and now - self._history_attempt < timedelta(hours=1):
            return
        self._history_attempt = now
        try:
            samples = await async_temperature_history(
                self._hass, self._config["temperature_sensor"], now,
                self._smart.get("history_days", 7),
            )
            self._usage = infer_usage_profile(samples, self._time_zone,
                min_days=self._smart.get("min_days", 3))
            self._usage_updated = now
            self._usage_error = None
        except Exception as err:
            self._usage = UsageProfile()
            self._usage_error = "usage_history_unavailable"
            _LOGGER.warning("Hot-water usage history unavailable (%s)", type(err).__name__)

    def _smart_window(self, slots: list[PriceSlot], now: datetime) -> ChargeWindow | None:
        """Plan limited extra heat before a learned draw only when modeled useful heat is cheaper."""
        best = None
        normal = self._smart.get("normal_temperature", 50.0)
        maximum = self._smart.get("max_temperature", 55.0)
        for use_time, period in self._usage.upcoming(now, self._time_zone):
            normal_price = average_price(slots, use_time, use_time + timedelta(hours=1))
            if normal_price is None:
                continue
            for start in [now, *(s.start for s in slots if now < s.start < use_time)]:
                end = start + timedelta(hours=1)
                if end > use_time - timedelta(minutes=15):
                    continue
                if use_time - end > timedelta(hours=self._smart.get("max_storage_hours", 12)):
                    continue
                price = average_price(slots, start, end)
                if price is None or normal_price - price <= self._smart.get("min_price_spread", 0.03):
                    continue
                hours = (use_time - (start + timedelta(minutes=30))).total_seconds() / 3600
                cost = estimate_heat_cost(price, normal_price, hours, self._smart["thermal_model"])
                if cost is None:
                    continue
                extra = min(maximum - normal, period.drop_c) * cost.fraction
                target = min(maximum, normal + round(extra * 2) / 2)
                if target < normal + self._smart.get("min_boost_c", 2.0):
                    continue
                candidate = ChargeWindow(start, end, price, normal_price, cost.fraction,
                    cost.charge, cost.normal, cost.saving_percent, use_time, target)
                if best is None or cost.normal - cost.charge > best.normal_heat_cost - best.charge_heat_cost:
                    best = candidate
        return best

    @property
    def blocks_space_heating(self) -> bool:
        """Keep the shared heat pump available to requested or native water heating."""
        demand = self._state("demand_entity")
        if self._owned or demand is None or demand.state != "off":
            return True
        for entity_id in self._config.get("active_sensors", []):
            state = self._hass.states.get(entity_id)
            if state is None or state.state not in {"off", "0", "0.0"}:
                return True
        return False

    def reserved_windows(self):
        """Reserve the boiler block with allowance for the minimum runtime."""
        if self.mode == "auto" and self.window is not None:
            return [(self.window.start, self.window.end + timedelta(minutes=15))]
        return []

    async def async_start(self) -> None:
        """React immediately to native heat-pump and request state changes."""
        self._remove_state_listener = async_track_state_change_event(
            self._hass,
            [self._config["demand_entity"], self._config["temperature_sensor"], *self._config.get("active_sensors", [])],
            self._async_demand_changed,
        )
        await super().async_start()

    async def _async_demand_changed(self, *_):
        self._notify()
        await self.async_refresh()

    def _state(self, key: str):
        return self._hass.states.get(self._config[key])

    def _number(self, key: str) -> float | None:
        state = self._state(key)
        try:
            value = float(state.state)
            return value if math.isfinite(value) else None
        except (AttributeError, ValueError, TypeError):
            return None

    def _elapsed(self, key: str, now: datetime) -> float | None:
        state = self._state(key)
        try:
            timestamp = float(state.attributes["timestamp"])
            return now.timestamp() - timestamp if math.isfinite(timestamp) else None
        except (AttributeError, KeyError, TypeError, ValueError):
            return None

    @property
    def state(self) -> str:
        return self._reason

    @property
    def attributes(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "temperature": self._number("temperature_sensor"),
            "soc": self._number("soc_sensor"),
            "request_owned": self._owned,
            "space_heating_blocked": self.blocks_space_heating,
            "charge_start": None if self.window is None else self.window.start.isoformat(),
            "charge_end": None if self.window is None else self.window.end.isoformat(),
            "average_price": None if self.window is None else self.window.price,
            "target_temperature": self._active_target if self._owned else (
                None if self.window is None else self.window.target_temperature
            ),
            "expected_use": None if self.window is None or self.window.use_time is None else self.window.use_time.isoformat(),
            "usage_periods": [{"time": f"{p.minute // 60:02}:{p.minute % 60:02}",
                               "temperature_drop_c": round(p.drop_c, 1), "days": p.days}
                              for p in self._usage.periods],
            "usage_observed_days": self._usage.observed_days,
            "usage_draws": self._usage.draws,
            "usage_updated_at": None if self._usage_updated is None else self._usage_updated.isoformat(),
            "usage_error": self._usage_error,
            "thermal_model": None if self._smart is None else self._smart["thermal_model"],
            "modeled_saving_percent": None if self.window is None else self.window.modeled_saving_percent,
            "estimated_charge_heat_cost": None if self.window is None else self.window.charge_heat_cost,
            "estimated_normal_heat_cost": None if self.window is None else self.window.normal_heat_cost,
            "error": self.error,
        }

    async def _gateway(self, start: bool) -> None:
        prefix = "start" if start else "stop"
        domain, service = self._config[f"{prefix}_service"].split(".", 1)
        response = await asyncio.wait_for(self._hass.services.async_call(
            domain, service, self._config[f"{prefix}_data"],
            blocking=True, **({"return_response": True} if domain == "rest_command" else {}),
        ), timeout=20)
        if isinstance(response, dict) and not 200 <= int(response.get("status", 200)) < 300:
            raise RuntimeError("Hot-water gateway rejected request")

    async def _release(self) -> None:
        if not self._owned:
            return
        await self._gateway(False)
        await self._hass.services.async_call(
            "input_select", "select_option",
            {"entity_id": self._config["source_entity"], "option": "none"},
            blocking=True,
        )
        self._owned = False
        self._active_target = None

    async def async_set_mode(self, mode: str) -> None:
        if mode not in {"off", "monitor", "auto"}:
            raise ValueError(f"Invalid hot-water mode: {mode}")
        if mode != "auto":
            await self._release()
        self.mode = mode
        await self.async_refresh()

    async def async_stop(self) -> None:
        if self._remove_state_listener is not None:
            self._remove_state_listener()
            self._remove_state_listener = None
        await self._release()
        await super().async_stop()
        self._reason = "off"

    async def async_refresh(self, *_: Any) -> None:
        async with self._lock:
            if self._stopped:
                return
            now = datetime.now(timezone.utc)
            try:
                if self.mode == "off":
                    self._reason = "off"
                    self._notify()
                    return
                await self._prices.async_refresh()
                slots = self._prices._slots
                await self._refresh_usage(now)
                if self._stopped or self.mode == "off":
                    self._reason = "off"
                    self._notify()
                    return
                if self._smart is not None and not self._owned:
                    self.window = self._smart_window(slots, now)
                elif not self._owned and (self.window is None or now >= self.window.end):
                    candidates = []
                    for start in [now, *(s.start for s in slots if now < s.start < now + timedelta(days=1))]:
                        end = start + timedelta(hours=1)
                        price = average_price(slots, start, end)
                        if price is not None:
                            candidates.append(ChargeWindow(start, end, price, price))
                    self.window = min(candidates, key=lambda w: w.price, default=None)
                temperature = self._number("temperature_sensor")
                soc = self._number("soc_sensor")
                demand = self._state("demand_entity")
                source = self._state("source_entity")
                valid = temperature is not None and (
                    self._smart is not None or (soc is not None and 0 <= soc <= 100)
                )
                in_window = self.window is not None and self.window.start <= now < self.window.end
                runtime = self._elapsed("started_at_entity", now)
                if (self.mode == "auto" and source is not None
                        and source.state in {"price", "solar"}
                        and demand is not None and demand.state == "on"):
                    self._owned = True
                    if self._smart is not None and self._active_target is None:
                        state = self._hass.states.get(self._smart["target_entity"])
                        try:
                            self._active_target = float(state.state)
                        except (AttributeError, ValueError, TypeError):
                            self._active_target = None
                if self._requested_at is not None and (
                    runtime is None or runtime > (now-self._requested_at).total_seconds()+60
                ):
                    runtime = (now-self._requested_at).total_seconds()
                grid = None
                if "grid_sensor" in self._config:
                    grid = self._number("grid_sensor")
                if grid is not None and grid < -2500:
                    self._surplus_since = self._surplus_since or now
                else:
                    self._surplus_since = None
                if grid is None or grid > 500:
                    self._import_since = self._import_since or now
                else:
                    self._import_since = None
                solar = self._surplus_since is not None and (now-self._surplus_since).total_seconds() >= 300
                if self._owned:
                    is_solar = source is not None and source.state == "solar"
                    expired = not in_window or not slots
                    if is_solar:
                        expired = self._import_since is not None and (now-self._import_since).total_seconds() >= (60 if grid is None else 300)
                    target = self._active_target if self._smart is not None else 65
                    target_valid = target is not None and math.isfinite(target) and (
                        self._smart is None or self._smart.get("normal_temperature", 50) <= target <= self._smart.get("max_temperature", 55)
                    )
                    stop = (demand is None or demand.state != "on" or not valid or not target_valid or (soc is not None and soc >= 99) or (temperature is not None and target_valid and temperature >= target) or
                            expired or
                            runtime is None or runtime >= 5400)
                    if stop:
                        await self._release()
                    self._reason = "heating" if self._owned else "idle"
                elif self.mode == "monitor":
                    self._reason = "monitor"
                elif not valid or not slots:
                    self._reason = "unavailable"
                elif source is None or source.state != "none" or demand is None or demand.state != "off":
                    self._reason = "external_control"
                else:
                    elapsed = self._elapsed("last_off_entity", now)
                    target = None if self.window is None else self.window.target_temperature
                    if self._smart is not None:
                        if solar and self._usage.periods and self._usage.upcoming(now, self._time_zone):
                            next_use, period = self._usage.upcoming(now, self._time_zone)[0]
                            if next_use - now <= timedelta(hours=self._smart.get("max_storage_hours", 12)):
                                target = min(self._smart.get("max_temperature", 55),
                                    self._smart.get("normal_temperature", 50) + period.drop_c)
                        native_idle = all(
                            (state := self._hass.states.get(e)) is not None and state.state in {"off", "0", "0.0"}
                            for e in self._config.get("active_sensors", [])
                        )
                        should_start = native_idle and target is not None and temperature < target - 0.5 and (solar or in_window)
                    else:
                        should_start = (solar and soc < 99) or (in_window and soc < 40)
                    if should_start and elapsed is not None and elapsed > 14400:
                        if self._smart is not None:
                            entity = self._smart["target_entity"]
                            state = self._hass.states.get(entity)
                            if state is None or state.state in {"unknown", "unavailable"}:
                                self._reason = "unavailable"
                                self._notify()
                                return
                            await self._hass.services.async_call(entity.split(".")[0], "set_value",
                                {"entity_id": entity, "value": target}, blocking=True)
                            self._active_target = target
                        await self._hass.services.async_call(
                            "input_select", "select_option",
                            {"entity_id": self._config["source_entity"], "option": "solar" if solar else "price"}, blocking=True,
                        )
                        self._owned = True
                        self._requested_at = now
                        await self._gateway(True)
                        self._reason = "requested"
                    else:
                        self._reason = "scheduled" if self.window is not None else (
                            "learning_usage" if self._smart is not None and not self._usage.periods else
                            "no_economic_window" if self._smart is not None else "unavailable"
                        )
                self.error = self._prices.error
            except Exception as err:
                self.error = "gateway_or_data_error"
                self._reason = "error"
                _LOGGER.error("Hot-water coordination failed (%s)", type(err).__name__)
                if self._owned:
                    try:
                        await self._release()
                    except Exception as release_error:
                        _LOGGER.error("Hot-water request release failed (%s)", type(release_error).__name__)
            self._notify()
