"""Coordinate additional hot-water charging through an existing protected gateway."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import math
from typing import Any

from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.event import async_track_state_change_event

from .price_charging import ChargeWindow, PriceChargingManager, average_price

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
            [self._config["demand_entity"], *self._config.get("active_sensors", [])],
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
                if not self._owned and (self.window is None or now >= self.window.end):
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
                valid = temperature is not None and soc is not None and 0 <= soc <= 100
                in_window = self.window is not None and self.window.start <= now < self.window.end
                runtime = self._elapsed("started_at_entity", now)
                if (self.mode == "auto" and source is not None
                        and source.state in {"price", "solar"}
                        and demand is not None and demand.state == "on"):
                    self._owned = True
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
                    stop = (demand is None or demand.state != "on" or not valid or soc >= 99 or temperature >= 65 or
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
                    if ((solar and soc < 99) or (in_window and soc < 40)) and elapsed is not None and elapsed > 14400:
                        await self._hass.services.async_call(
                            "input_select", "select_option",
                            {"entity_id": self._config["source_entity"], "option": "solar" if solar else "price"}, blocking=True,
                        )
                        self._owned = True
                        self._requested_at = now
                        await self._gateway(True)
                        self._reason = "requested"
                    else:
                        self._reason = "scheduled" if self.window is not None else "unavailable"
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
