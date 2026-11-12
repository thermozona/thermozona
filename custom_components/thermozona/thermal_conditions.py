"""Bounded estimates of water heat delivery and outdoor heat loss."""
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class ThermalReference:
    """Conditions associated with configured gains or a learned warming rate."""

    supply_c: float
    outside_c: float
    room_c: float


@dataclass(frozen=True)
class ThermalCompensation:
    """A demand/lead-time multiplier; one retains the existing controller."""

    factor: float = 1.0
    supply_c: float | None = None
    outside_c: float | None = None
    reason: str = "disabled"
    solar_w_m2: float | None = None
    solar_equivalent_c: float = 0.0


def compensation(
    *, supply_c: float | None, outside_c: float | None, room_c: float | None,
    target_c: float, reference: ThermalReference,
    min_factor: float = .75, max_factor: float = 1.25,
    solar_equivalent_c: float = 0,
) -> ThermalCompensation:
    """Approximate loss/delivery changes, with conservative limits, not a heat meter."""
    values = (supply_c, outside_c, room_c, target_c, reference.supply_c,
              reference.outside_c, reference.room_c)
    if any(value is None or not math.isfinite(value) for value in values):
        return ThermalCompensation(reason="missing_conditions")
    if not 15 <= supply_c <= 45 or not -40 <= outside_c <= 50:
        return ThermalCompensation(reason="invalid_conditions")
    head = supply_c - room_c
    reference_head = reference.supply_c - reference.room_c
    if head < 1 or reference_head < 1:
        return ThermalCompensation(reason="insufficient_water_heat")
    # Outdoor heat loss and water-to-room temperature head act in opposite directions.
    loss_ratio = max(5.0, target_c - outside_c - solar_equivalent_c) / max(
        5.0, reference.room_c - reference.outside_c
    )
    factor = loss_ratio * reference_head / head
    return ThermalCompensation(
        max(min_factor, min(max_factor, factor)), supply_c, outside_c, "active"
    )


def compensate_duty(duty: float, factor: float) -> float:
    """Trim partial demand smoothly, preserving zero and full heat requests."""
    duty = max(0.0, min(100.0, duty))
    trim = duty * (factor - 1) * (1 - duty / 100)
    return max(0.0, min(100.0, duty + trim))
