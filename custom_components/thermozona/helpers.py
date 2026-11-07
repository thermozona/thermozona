"""Helper utilities for the Thermozona integration."""
from __future__ import annotations

from typing import Any

from . import CONF_CIRCUITS


def resolve_circuits(zone_config: dict[str, Any]) -> list[str]:
    """Return the configured circuits, falling back to legacy groups."""
    circuits = zone_config.get(CONF_CIRCUITS)
    if circuits is None:
        circuits = zone_config.get("groups")
    return circuits or []


def air_coil_heating_suppressed(
    requested_water_c: float | None,
    measured_water_c: float | None,
    was_suppressed: bool,
) -> bool:
    """Keep the air coil closed until both requested and actual water are cool."""
    if requested_water_c is None or measured_water_c is None:
        return True
    if was_suppressed:
        return requested_water_c > 25.0 or measured_water_c > 25.0
    return requested_water_c >= 27.0 or measured_water_c >= 27.0
