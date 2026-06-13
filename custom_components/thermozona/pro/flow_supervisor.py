# SPDX-FileCopyrightText: 2026 Jaap van der Meer
# SPDX-License-Identifier: MIT
"""Demand-weighted flow-temperature supervisor for heating mode."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, NamedTuple

from .. import ZONE_RESPONSE_FAST, ZONE_RESPONSE_SLOW


class _ZoneDemand(NamedTuple):
    """Normalized per-zone demand contribution."""

    zone_name: str
    target: float
    error: float
    duty: float
    response: str
    weight: float
    solar_weight: float
    score: float


class ProFlowSupervisor:
    """Compute a stable, demand-weighted heating flow setpoint."""

    def __init__(self) -> None:
        self._ema_duty: dict[str, float] = {}
        self._integral = 0.0
        self._last_eval_time: datetime | None = None
        self._last_flow = 30.0

    def reset(self) -> None:
        """Reset adaptive state after reload/reconfiguration."""
        self._ema_duty.clear()
        self._integral = 0.0
        self._last_eval_time = None
        self._last_flow = 30.0

    def compute_heating_flow(
        self,
        *,
        zone_status: dict[str, dict[str, Any]],
        outside_temp: float | None,
        forecast_outside_temp: float | None,
        forecast_solar_irradiance: float | None,
        base_offset: float,
        weather_slope: float,
        flow_curve_offset: float,
        config: dict[str, Any],
    ) -> float:
        """Return supervised heating flow command in degrees Celsius."""
        flow, _ = self.compute_heating_flow_with_breakdown(
            zone_status=zone_status,
            outside_temp=outside_temp,
            forecast_outside_temp=forecast_outside_temp,
            forecast_solar_irradiance=forecast_solar_irradiance,
            base_offset=base_offset,
            weather_slope=weather_slope,
            flow_curve_offset=flow_curve_offset,
            config=config,
        )
        return flow

    def compute_heating_flow_with_breakdown(
        self,
        *,
        zone_status: dict[str, dict[str, Any]],
        outside_temp: float | None,
        forecast_outside_temp: float | None,
        forecast_solar_irradiance: float | None,
        base_offset: float,
        weather_slope: float,
        flow_curve_offset: float,
        config: dict[str, Any],
    ) -> tuple[float, dict[str, Any]]:
        """Return (flow, breakdown) for observability attributes."""
        now = datetime.now(timezone.utc)
        dt_minutes = self._get_dt_minutes(now)

        demand_entries = self._build_zone_demands(zone_status, dt_minutes, config)
        if not demand_entries:
            self._last_eval_time = now
            breakdown = {
                "target_ref_c": None,
                "di_slow": 0.0,
                "di_fast": 0.0,
                "slow_mix_weight": 0.0,
                "fast_mix_weight": 0.0,
                "demand_index": 0.0,
                "kp": 0.0,
                "trim_p_c": 0.0,
                "integral_enabled": False,
                "integral_c": 0.0,
                "fast_boost_c": 0.0,
                "preheat_boost_c": 0.0,
                "flow_temp_unclamped_c": 30.0,
                "flow_temp_smoothed_c": 30.0,
                "flow_temp_c": 30.0,
                "clamp_min_c": 15.0,
                "clamp_max_c": 35.0,
            }
            return 30.0, breakdown

        slow_entries = [
            entry for entry in demand_entries if entry.response == ZONE_RESPONSE_SLOW
        ]
        fast_entries = [
            entry for entry in demand_entries if entry.response == ZONE_RESPONSE_FAST
        ]
        if not slow_entries:
            slow_entries = demand_entries

        target_ref = max(entry.target for entry in slow_entries)

        di_slow = self._weighted_average(
            [(entry.score, entry.weight) for entry in slow_entries]
        )
        di_fast = self._weighted_average(
            [(entry.score, entry.weight) for entry in fast_entries]
        )
        if not fast_entries:
            di_fast = di_slow

        slow_mix = max(0.0, float(config.get("slow_mix_weight", 0.8)))
        fast_mix = max(0.0, float(config.get("fast_mix_weight", 0.2)))
        mix_total = slow_mix + fast_mix
        if mix_total <= 0:
            slow_mix = 1.0
            fast_mix = 0.0
            mix_total = 1.0
        slow_mix /= mix_total
        fast_mix /= mix_total
        demand_index = (slow_mix * di_slow) + (fast_mix * di_fast)

        weather_term = base_offset + flow_curve_offset
        if outside_temp is not None:
            weather_term += max(0.0, 15.0 - outside_temp) * max(0.0, weather_slope)
        flow = target_ref + weather_term

        kp = max(0.0, float(config.get("kp", 1.0)))
        trim_p = kp * demand_index
        trim = trim_p

        use_integral = bool(config.get("use_integral", False))
        if use_integral:
            ti_minutes = max(1.0, float(config.get("ti_minutes", 180)))
            i_max = max(0.0, float(config.get("i_max", 1.5)))
            self._integral += demand_index * (dt_minutes / ti_minutes)
            self._integral = self._clamp(self._integral, 0.0, i_max)
            trim += self._integral
        else:
            self._integral = 0.0

        fast_boost = self._compute_fast_zone_boost(
            fast_entries=fast_entries,
            error_deadband=float(config.get("fast_error_deadband_c", 0.4)),
            gain=float(config.get("fast_boost_gain", 1.2)),
            cap=float(config.get("fast_boost_cap_c", 1.2)),
        )

        preheat_boost = self._compute_preheat_boost(
            enabled=bool(config.get("preheat_enabled", False)),
            outside_temp=outside_temp,
            forecast_outside_temp=forecast_outside_temp,
            forecast_solar_irradiance=forecast_solar_irradiance,
            slow_entries=slow_entries,
            slow_di=di_slow,
            gain=float(config.get("preheat_gain", 0.35)),
            solar_gain_per_w_m2=float(config.get("preheat_solar_gain_per_w_m2", 0.0)),
            cap=float(config.get("preheat_cap_c", 1.2)),
            min_slow_di=float(config.get("preheat_min_slow_di", 0.25)),
        )

        raw_flow = flow + trim + fast_boost + preheat_boost
        smoothed_flow = self._apply_slew_rate(raw_flow=raw_flow, now=now, config=config)
        self._last_eval_time = now
        self._last_flow = smoothed_flow

        clamped_flow = self._clamp(smoothed_flow, 15.0, 35.0)
        breakdown = {
            "target_ref_c": round(float(target_ref), 3),
            "di_slow": round(float(di_slow), 6),
            "di_fast": round(float(di_fast), 6),
            "slow_mix_weight": round(float(slow_mix), 6),
            "fast_mix_weight": round(float(fast_mix), 6),
            "demand_index": round(float(demand_index), 6),
            "weather_term_c": round(float(weather_term), 3),
            "kp": round(float(kp), 6),
            "trim_p_c": round(float(trim_p), 3),
            "integral_enabled": bool(use_integral),
            "integral_c": round(float(self._integral if use_integral else 0.0), 6),
            "fast_boost_c": round(float(fast_boost), 3),
            "preheat_boost_c": round(float(preheat_boost), 3),
            "flow_temp_unclamped_c": round(float(raw_flow), 3),
            "flow_temp_smoothed_c": round(float(smoothed_flow), 3),
            "flow_temp_c": round(float(clamped_flow), 1),
            "clamp_min_c": 15.0,
            "clamp_max_c": 35.0,
        }
        return clamped_flow, breakdown

    def _build_zone_demands(
        self,
        zone_status: dict[str, dict[str, Any]],
        dt_minutes: float,
        config: dict[str, Any],
    ) -> list[_ZoneDemand]:
        error_norm_max = max(0.1, float(config.get("error_norm_max", 2.0)))
        duty_ema_minutes = max(1.0, float(config.get("duty_ema_minutes", 20)))

        error_weight = max(0.0, float(config.get("error_weight", 0.6)))
        duty_weight = max(0.0, float(config.get("duty_weight", 0.4)))
        weight_total = error_weight + duty_weight
        if weight_total <= 0:
            error_weight = 1.0
            duty_weight = 0.0
            weight_total = 1.0
        error_weight /= weight_total
        duty_weight /= weight_total

        entries: list[_ZoneDemand] = []
        for zone_name, status in zone_status.items():
            target = status.get("target")
            current = status.get("current")
            if target is None or current is None:
                continue

            target_value = float(target)
            current_value = float(current)
            error = max(0.0, target_value - current_value)
            normalized_error = self._clamp(error / error_norm_max, 0.0, 1.0)

            raw_duty = self._clamp(float(status.get("duty_cycle", 0.0)), 0.0, 100.0)
            filtered_duty = self._update_duty_ema(
                zone_name=zone_name,
                raw_duty=raw_duty,
                dt_minutes=dt_minutes,
                tau_minutes=duty_ema_minutes,
            )
            duty_fraction = filtered_duty / 100.0

            response = str(status.get("zone_response", ZONE_RESPONSE_SLOW)).lower()
            if response not in {ZONE_RESPONSE_SLOW, ZONE_RESPONSE_FAST}:
                response = ZONE_RESPONSE_SLOW

            zone_weight = max(0.0, float(status.get("zone_flow_weight", 1.0)))
            zone_solar_weight = max(0.0, float(status.get("zone_solar_weight", 1.0)))
            score = (error_weight * normalized_error) + (duty_weight * duty_fraction)
            entries.append(
                _ZoneDemand(
                    zone_name=zone_name,
                    target=target_value,
                    error=error,
                    duty=duty_fraction,
                    response=response,
                    weight=zone_weight,
                    solar_weight=zone_solar_weight,
                    score=score,
                )
            )

        return entries

    def _update_duty_ema(
        self,
        *,
        zone_name: str,
        raw_duty: float,
        dt_minutes: float,
        tau_minutes: float,
    ) -> float:
        previous = self._ema_duty.get(zone_name)
        if previous is None:
            self._ema_duty[zone_name] = raw_duty
            return raw_duty

        alpha = 1.0 - math.exp(-dt_minutes / max(tau_minutes, 1e-6))
        filtered = previous + alpha * (raw_duty - previous)
        self._ema_duty[zone_name] = filtered
        return filtered

    def _compute_fast_zone_boost(
        self,
        *,
        fast_entries: list[_ZoneDemand],
        error_deadband: float,
        gain: float,
        cap: float,
    ) -> float:
        if not fast_entries:
            return 0.0

        excess_values = []
        deadband = max(0.0, error_deadband)
        for entry in fast_entries:
            excess_error = max(0.0, entry.error - deadband)
            excess_values.append(excess_error * entry.duty * entry.weight)

        if not excess_values:
            return 0.0
        raw_boost = max(excess_values) * max(0.0, gain)
        return self._clamp(raw_boost, 0.0, max(0.0, cap))

    def _compute_preheat_boost(
        self,
        *,
        enabled: bool,
        outside_temp: float | None,
        forecast_outside_temp: float | None,
        forecast_solar_irradiance: float | None,
        slow_entries: list[_ZoneDemand],
        slow_di: float,
        gain: float,
        solar_gain_per_w_m2: float,
        cap: float,
        min_slow_di: float,
    ) -> float:
        if not enabled:
            return 0.0
        if outside_temp is None or forecast_outside_temp is None:
            return 0.0
        if slow_di < max(0.0, min_slow_di):
            return 0.0

        cold_drop = max(0.0, outside_temp - forecast_outside_temp)
        cold_preheat = cold_drop * max(0.0, gain)

        solar_softening = 0.0
        if forecast_solar_irradiance is not None:
            zone_solar_factor = self._weighted_average(
                [
                    (
                        entry.solar_weight,
                        max(0.0, entry.weight * max(entry.score, 0.05)),
                    )
                    for entry in slow_entries
                ]
            )
            solar_softening = max(0.0, forecast_solar_irradiance) * max(
                0.0, solar_gain_per_w_m2
            ) * zone_solar_factor

        preheat = cold_preheat - solar_softening
        return self._clamp(preheat, 0.0, max(0.0, cap))

    def _apply_slew_rate(
        self,
        *,
        raw_flow: float,
        now: datetime,
        config: dict[str, Any],
    ) -> float:
        if self._last_eval_time is None:
            return raw_flow

        dt_seconds = max(1.0, (now - self._last_eval_time).total_seconds())
        slew_up_per_5m = max(0.0, float(config.get("slew_up_c_per_5m", 0.3)))
        slew_down_per_5m = max(0.0, float(config.get("slew_down_c_per_5m", 0.2)))

        max_up = (slew_up_per_5m / 300.0) * dt_seconds
        max_down = (slew_down_per_5m / 300.0) * dt_seconds

        lower = self._last_flow - max_down
        upper = self._last_flow + max_up
        return self._clamp(raw_flow, lower, upper)

    def _get_dt_minutes(self, now: datetime) -> float:
        if self._last_eval_time is None:
            return 1.0
        dt_seconds = max((now - self._last_eval_time).total_seconds(), 1.0)
        return dt_seconds / 60.0

    @staticmethod
    def _weighted_average(values: list[tuple[float, float]]) -> float:
        weighted_sum = 0.0
        total_weight = 0.0
        for value, weight in values:
            if weight <= 0:
                continue
            weighted_sum += value * weight
            total_weight += weight
        if total_weight <= 0:
            return 0.0
        return weighted_sum / total_weight

    @staticmethod
    def _clamp(value: float, minimum: float, maximum: float) -> float:
        return max(minimum, min(maximum, value))
