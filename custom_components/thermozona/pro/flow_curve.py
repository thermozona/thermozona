# SPDX-FileCopyrightText: 2026 Jaap van der Meer
# SPDX-License-Identifier: MIT
"""Runtime flow-curve offset manager."""
from __future__ import annotations

import weakref
from typing import Callable, Protocol


class FlowCurveOffsetEntity(Protocol):
    """Protocol for flow-curve helper number entity callbacks."""

    def set_current_value(self, value: float) -> None:
        """Push a value to entity state."""


class FlowCurveRuntimeManager:
    """Manage runtime flow-curve override and helper entity sync."""

    def __init__(
        self,
        *,
        get_yaml_value: Callable[[], float],
        notify_thermostats: Callable[[], None],
    ) -> None:
        self._get_yaml_value = get_yaml_value
        self._notify_thermostats = notify_thermostats
        self._override: float | None = None
        self._entity: weakref.ReferenceType[FlowCurveOffsetEntity] | None = None

    def register_entity(self, entity: FlowCurveOffsetEntity) -> None:
        """Register helper number entity."""
        self._entity = weakref.ref(entity)
        entity.set_current_value(self.get_value())

    def unregister_entity(self, entity: FlowCurveOffsetEntity) -> None:
        """Unregister helper number entity."""
        if self._entity is not None and self._entity() is entity:
            self._entity = None

    def get_value(self) -> float:
        """Return active flow-curve offset value."""
        if self._override is not None:
            return self._override
        return self._get_yaml_value()

    def set_override(self, value: float) -> None:
        """Set runtime override and notify listeners."""
        self._override = float(value)
        self._write_entity_state()
        self._notify_thermostats()

    def reset_override(self) -> None:
        """Clear runtime override and notify listeners."""
        self._override = None
        self._write_entity_state()
        self._notify_thermostats()

    def _write_entity_state(self) -> None:
        """Sync active value to helper entity, when available."""
        if self._entity is None:
            return
        entity = self._entity()
        if entity is None:
            self._entity = None
            return
        entity.set_current_value(self.get_value())
