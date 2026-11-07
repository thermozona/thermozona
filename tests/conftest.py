from __future__ import annotations

import asyncio
import pathlib
import sys
import types
from enum import Enum, IntFlag

import pytest
import voluptuous as vol

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class _BaseEntity:
    async def async_added_to_hass(self) -> None:
        return None

    async def async_will_remove_from_hass(self) -> None:
        return None

    def async_write_ha_state(self) -> None:
        return None


class _RestoreEntity:
    async def async_get_last_state(self):
        return None


class _RestoreNumber(_BaseEntity, _RestoreEntity):
    async def async_get_last_number_data(self):
        return None


class _SelectEntity(_BaseEntity):
    @property
    def options(self):
        return self._attr_options


class _ClimateEntityFeature(IntFlag):
    TARGET_TEMPERATURE = 1
    TURN_ON = 2
    TURN_OFF = 4


class _HVACMode(str, Enum):
    HEAT = "heat"
    COOL = "cool"
    AUTO = "auto"
    OFF = "off"


class _HVACAction(str, Enum):
    OFF = "off"
    HEATING = "heating"
    COOLING = "cooling"
    IDLE = "idle"


class _Platform(str, Enum):
    CLIMATE = "climate"
    NUMBER = "number"
    SELECT = "select"
    SENSOR = "sensor"


class _UnitOfTemperature(str, Enum):
    CELSIUS = "°C"


class _EntityCategory(str, Enum):
    CONFIG = "config"
    DIAGNOSTIC = "diagnostic"


class _SensorDeviceClass(str, Enum):
    TEMPERATURE = "temperature"


class _SensorStateClass(str, Enum):
    MEASUREMENT = "measurement"


class _ConfigFlow:
    async def async_set_unique_id(self, *_):
        return None

    def _abort_if_unique_id_configured(self):
        return None

    def async_create_entry(self, *, title, data):
        return {"type": "create_entry", "title": title, "data": data}

    def async_abort(self, *, reason, description_placeholders=None):
        return {
            "type": "abort",
            "reason": reason,
            "description_placeholders": description_placeholders,
        }

    def __init_subclass__(cls, **kwargs):
        return super().__init_subclass__()


class _State:
    def __init__(self, state):
        self.state = state


class FakeStates:
    def __init__(self):
        self._states: dict[str, _State] = {}

    def get(self, entity_id: str):
        return self._states.get(entity_id)

    def set(self, entity_id: str, value):
        self._states[entity_id] = _State(value)


class FakeServices:
    def __init__(self, hass):
        self.calls: list[tuple[str, str, dict, bool]] = []
        self._hass = hass

    async def async_call(self, domain, service, data, blocking=False):
        self.calls.append((domain, service, data, blocking))
        entity_id = data.get("entity_id")
        if domain in {"input_boolean", "switch"} and entity_id:
            self._hass.states.set(entity_id, "on" if service == "turn_on" else "off")
        if domain == "input_number" and service == "set_value" and entity_id:
            self._hass.states.set(entity_id, str(data.get("value")))

    def async_register(self, *_args, **_kwargs):
        return None


class FakeConfigEntries:
    def async_entries(self, _domain):
        return []

    def async_update_entry(self, _entry, data):
        return data

    async def async_reload(self, _entry_id):
        return None

    async def async_forward_entry_setups(self, _entry, _platforms):
        return None

    async def async_unload_platforms(self, _entry, _platforms):
        return True

    @property
    def flow(self):
        return types.SimpleNamespace(async_init=lambda *args, **kwargs: None)


class FakeHass:
    def __init__(self):
        self.states = FakeStates()
        self.services = FakeServices(self)
        self.data = {}
        self.config_entries = FakeConfigEntries()

    def async_create_task(self, coro):
        return asyncio.create_task(coro)


class ConfigEntry:
    def __init__(self, entry_id="entry-1", data=None):
        self.entry_id = entry_id
        self.data = data or {}


def pytest_configure():
    ha = types.ModuleType("homeassistant")

    config = types.ModuleType("homeassistant.config")

    async def async_hass_config_yaml(_hass):
        return {}

    config.async_hass_config_yaml = async_hass_config_yaml

    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = FakeHass
    core.ServiceCall = dict

    config_entries = types.ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = ConfigEntry
    config_entries.SOURCE_IMPORT = "import"
    config_entries.ConfigFlow = _ConfigFlow

    const = types.ModuleType("homeassistant.const")
    const.Platform = _Platform
    const.ATTR_TEMPERATURE = "temperature"
    const.STATE_UNKNOWN = "unknown"
    const.STATE_UNAVAILABLE = "unavailable"
    const.UnitOfTemperature = _UnitOfTemperature

    exceptions = types.ModuleType("homeassistant.exceptions")
    exceptions.HomeAssistantError = type("HomeAssistantError", (Exception,), {})

    cv = types.ModuleType("homeassistant.helpers.config_validation")
    cv.entity_id = str
    cv.string = str
    cv.boolean = vol.Boolean()

    entity_platform = types.ModuleType("homeassistant.helpers.entity_platform")
    entity_platform.AddEntitiesCallback = object

    entity = types.ModuleType("homeassistant.helpers.entity")
    entity.EntityCategory = _EntityCategory

    restore_state = types.ModuleType("homeassistant.helpers.restore_state")
    restore_state.RestoreEntity = _RestoreEntity

    event = types.ModuleType("homeassistant.helpers.event")
    event.async_track_state_change_event = lambda *_args, **_kwargs: (lambda: None)
    event.async_track_time_interval = lambda *_args, **_kwargs: (lambda: None)

    climate = types.ModuleType("homeassistant.components.climate")
    climate.HVACMode = _HVACMode
    climate.HVACAction = _HVACAction
    climate.ClimateEntity = _BaseEntity
    climate.ClimateEntityFeature = _ClimateEntityFeature

    number = types.ModuleType("homeassistant.components.number")
    number.NumberEntity = _BaseEntity
    number.RestoreNumber = _RestoreNumber

    select = types.ModuleType("homeassistant.components.select")
    select.SelectEntity = _SelectEntity

    sensor = types.ModuleType("homeassistant.components.sensor")
    sensor.SensorEntity = _BaseEntity
    sensor.SensorDeviceClass = _SensorDeviceClass
    sensor.SensorStateClass = _SensorStateClass

    data_entry_flow = types.ModuleType("homeassistant.data_entry_flow")
    data_entry_flow.FlowResult = dict

    helpers = types.ModuleType("homeassistant.helpers")
    helpers.config_validation = cv

    components = types.ModuleType("homeassistant.components")

    sys.modules.update(
        {
            "homeassistant": ha,
            "homeassistant.config": config,
            "homeassistant.core": core,
            "homeassistant.config_entries": config_entries,
            "homeassistant.const": const,
            "homeassistant.exceptions": exceptions,
            "homeassistant.helpers": helpers,
            "homeassistant.helpers.config_validation": cv,
            "homeassistant.helpers.entity_platform": entity_platform,
            "homeassistant.helpers.entity": entity,
            "homeassistant.helpers.restore_state": restore_state,
            "homeassistant.helpers.event": event,
            "homeassistant.components": components,
            "homeassistant.components.climate": climate,
            "homeassistant.components.number": number,
            "homeassistant.components.select": select,
            "homeassistant.components.sensor": sensor,
            "homeassistant.data_entry_flow": data_entry_flow,
        }
    )

    ha.components = components


__all__ = ["FakeHass", "ConfigEntry"]


@pytest.fixture
def fake_hass() -> FakeHass:
    return FakeHass()
