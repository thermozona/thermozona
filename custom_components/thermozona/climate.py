"""Climate platform for the Thermozona integration."""
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import (
    CONF_AIR_COIL_MEASURED_WATER_SENSOR,
    CONF_AIR_COIL_REQUESTED_WATER_SENSOR,
    CONF_CONTROL_MODE,
    CONF_HYSTERESIS,
    CONF_PRICE_CHARGING,
    CONF_PWM_ACTUATOR_DELAY,
    CONF_PWM_CYCLE_TIME,
    CONF_PWM_KI,
    CONF_PWM_KP,
    CONF_PWM_MIN_OFF_TIME,
    CONF_PWM_MIN_ON_TIME,
    CONF_TEMP_SENSOR,
    CONF_COOLING_DEWPOINT_MARGIN,
    CONF_COOLING_DEWPOINT_PROTECTION,
    CONF_FLOOR_MATERIAL,
    CONF_ZONE_FLOW_WEIGHT,
    CONF_ZONE_HEATING_MIN_FLOW,
    CONF_ZONE_RESPONSE,
    CONF_ZONE_SOLAR_WEIGHT,
    CONF_ZONE_TYPE,
    DOMAIN,
)
from .helpers import resolve_circuits
from .thermostat import ThermozonaThermostat

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Thermozona climate devices."""
    _LOGGER.debug("Setting up climate platform")
    domain_data = hass.data[DOMAIN]
    entry_config = domain_data[config_entry.entry_id]
    zones = entry_config.get("zones", {})
    _LOGGER.debug("Found zones: %s", zones)

    controller = domain_data["controllers"][config_entry.entry_id]

    entities: list[ThermozonaThermostat] = []
    for zone_name, config in zones.items():
        _LOGGER.debug(
            "Creating thermostat for zone: %s with config: %s",
            zone_name,
            config,
        )
        circuits = resolve_circuits(config)
        if not circuits:
            _LOGGER.error("%s: No circuits defined for zone %s", DOMAIN, zone_name)
            continue
        entities.append(
            ThermozonaThermostat(
                hass,
                config_entry.entry_id,
                zone_name,
                circuits,
                config.get(CONF_TEMP_SENSOR),
                controller,
                config.get(CONF_HYSTERESIS),
                config.get(CONF_CONTROL_MODE),
                config.get(CONF_PWM_CYCLE_TIME),
                config.get(CONF_PWM_MIN_ON_TIME),
                config.get(CONF_PWM_MIN_OFF_TIME),
                config.get(CONF_PWM_KP),
                config.get(CONF_PWM_KI),
                config.get(CONF_PWM_ACTUATOR_DELAY),
                config.get(CONF_ZONE_RESPONSE),
                config.get(CONF_ZONE_FLOW_WEIGHT),
                config.get(CONF_ZONE_SOLAR_WEIGHT),
                config.get(CONF_ZONE_TYPE),
                config.get(CONF_FLOOR_MATERIAL),
                config.get(CONF_COOLING_DEWPOINT_MARGIN),
                config.get(CONF_COOLING_DEWPOINT_PROTECTION),
                config.get(CONF_ZONE_HEATING_MIN_FLOW),
                air_coil_requested_water_sensor=config.get(
                    CONF_AIR_COIL_REQUESTED_WATER_SENSOR
                ),
                air_coil_measured_water_sensor=config.get(
                    CONF_AIR_COIL_MEASURED_WATER_SENSOR
                ),
                price_charging=config.get(CONF_PRICE_CHARGING, False),
            )
        )

    async_add_entities(entities)
