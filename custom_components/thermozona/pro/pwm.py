# SPDX-FileCopyrightText: 2026 Jaap van der Meer
# SPDX-License-Identifier: MIT
"""PWM/PI helpers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from homeassistant.components.climate import HVACMode


def get_aligned_pwm_cycle_start(
    *,
    now: datetime,
    cycle_time_minutes: int,
    zone_index: int,
    zone_count: int,
) -> datetime:
    """Return cycle start aligned to fixed wall-clock PWM intervals."""
    cycle_seconds = max(60, cycle_time_minutes * 60)
    timestamp = int(now.timestamp())
    aligned_timestamp = timestamp - (timestamp % cycle_seconds)

    if zone_count > 1:
        offset_seconds = int(zone_index * cycle_seconds / zone_count)
        aligned_timestamp += offset_seconds
        if aligned_timestamp > timestamp:
            aligned_timestamp -= cycle_seconds

    return datetime.fromtimestamp(aligned_timestamp, tz=timezone.utc)


def calculate_pwm_duty(
    *,
    target_temperature: float,
    current_temp: float,
    effective_mode: HVACMode,
    now: datetime,
    last_control_time: datetime | None,
    pwm_integral: float,
    pwm_kp: float,
    pwm_ki: float,
) -> tuple[float, float, datetime]:
    """Calculate PI duty cycle percentage and return updated state."""
    error = target_temperature - current_temp
    if effective_mode == HVACMode.COOL:
        error = -error

    if last_control_time is None:
        dt_minutes = 1.0
    else:
        dt_minutes = max((now - last_control_time).total_seconds() / 60, 1.0)

    updated_integral = pwm_integral + (error * dt_minutes)
    if pwm_ki != 0:
        limit = 100 / abs(pwm_ki)
        updated_integral = max(-limit, min(limit, updated_integral))

    p_output = pwm_kp * error
    i_output = pwm_ki * updated_integral
    duty_cycle = max(0.0, min(100.0, p_output + i_output))
    return duty_cycle, updated_integral, now


def calculate_on_time_minutes(
    *,
    duty_cycle: float,
    cycle_time_minutes: int,
    min_on_time_minutes: int,
    min_off_time_minutes: int,
    actuator_delay_minutes: int,
    was_active: bool,
) -> float:
    """Calculate PWM on-time for the cycle based on PI output and limits."""
    on_minutes = cycle_time_minutes * duty_cycle / 100
    off_minutes = cycle_time_minutes - on_minutes

    if 0 < on_minutes < min_on_time_minutes:
        on_minutes = 0 if duty_cycle < 5 else float(min_on_time_minutes)
        off_minutes = cycle_time_minutes - on_minutes

    if 0 < off_minutes < min_off_time_minutes:
        off_minutes = 0 if duty_cycle > 95 else float(min_off_time_minutes)
        on_minutes = cycle_time_minutes - off_minutes

    if was_active and 0 < off_minutes < min_off_time_minutes:
        on_minutes = float(cycle_time_minutes)

    on_minutes = max(0.0, min(float(cycle_time_minutes), on_minutes))
    if on_minutes > 0:
        on_minutes += actuator_delay_minutes
        on_minutes = min(on_minutes, float(cycle_time_minutes))

    return on_minutes


def should_circuits_be_on(
    *,
    now: datetime,
    cycle_start: datetime | None,
    on_time: timedelta,
) -> bool:
    """Return whether circuits should be active in current PWM slice."""
    if cycle_start is None:
        return False
    return now < (cycle_start + on_time)
