"""Thermozona integration entrypoint."""
from homeassistant.config import async_hass_config_yaml
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.config_entries import ConfigEntry, SOURCE_IMPORT
from homeassistant.const import Platform
import voluptuous as vol
from homeassistant.helpers import config_validation as cv
from homeassistant.exceptions import HomeAssistantError

DOMAIN = "thermozona"
PLATFORMS = [Platform.CLIMATE, Platform.NUMBER, Platform.SELECT, Platform.SENSOR]

SERVICE_RELOAD = "reload"

CONF_ZONES = "zones"
CONF_CIRCUITS = "circuits"
CONF_TEMP_SENSOR = "temp_sensor"
CONF_OUTSIDE_TEMP_SENSOR = "outside_temp_sensor"
CONF_FLOW_TEMP_SENSOR = "flow_temp_sensor"
CONF_HEAT_PUMP_MODE = "heat_pump_mode"
CONF_HEATING_BASE_OFFSET = "heating_base_offset"
CONF_COOLING_BASE_OFFSET = "cooling_base_offset"
CONF_FLOW_CURVE_OFFSET = "flow_curve_offset"
CONF_WEATHER_SLOPE_HEAT = "weather_slope_heat"
CONF_WEATHER_SLOPE_COOL = "weather_slope_cool"
CONF_FLOW = "flow"
CONF_WRITE_DEADBAND_C = "write_deadband_c"
CONF_WRITE_MIN_INTERVAL_MINUTES = "write_min_interval_minutes"
CONF_PRO_KP = "kp"
CONF_PRO_USE_INTEGRAL = "use_integral"
CONF_PRO_TI_MINUTES = "ti_minutes"
CONF_PRO_I_MAX = "i_max"
CONF_PRO_ERROR_NORM_MAX = "error_norm_max"
CONF_PRO_DUTY_EMA_MINUTES = "duty_ema_minutes"
CONF_PRO_ERROR_WEIGHT = "error_weight"
CONF_PRO_DUTY_WEIGHT = "duty_weight"
CONF_PRO_SLOW_MIX_WEIGHT = "slow_mix_weight"
CONF_PRO_FAST_MIX_WEIGHT = "fast_mix_weight"
CONF_PRO_FAST_ERROR_DEADBAND_C = "fast_error_deadband_c"
CONF_PRO_FAST_BOOST_GAIN = "fast_boost_gain"
CONF_PRO_FAST_BOOST_CAP_C = "fast_boost_cap_c"
CONF_PRO_SLEW_UP_C_PER_5M = "slew_up_c_per_5m"
CONF_PRO_SLEW_DOWN_C_PER_5M = "slew_down_c_per_5m"
CONF_PRO_PREHEAT_ENABLED = "preheat_enabled"
CONF_PRO_PREHEAT_FORECAST_SENSOR = "preheat_forecast_sensor"
CONF_PRO_PREHEAT_SOLAR_SENSOR = "preheat_solar_sensor"
CONF_PRO_PREHEAT_GAIN = "preheat_gain"
CONF_PRO_PREHEAT_SOLAR_GAIN_PER_W_M2 = "preheat_solar_gain_per_w_m2"
CONF_PRO_PREHEAT_CAP_C = "preheat_cap_c"
CONF_PRO_PREHEAT_MIN_SLOW_DI = "preheat_min_slow_di"
CONF_CONTROL_MODE = "control_mode"
CONF_PWM_CYCLE_TIME = "pwm_cycle_time"
CONF_PWM_MIN_ON_TIME = "pwm_min_on_time"
CONF_PWM_MIN_OFF_TIME = "pwm_min_off_time"
CONF_PWM_KP = "pwm_kp"
CONF_PWM_KI = "pwm_ki"
CONF_PWM_ACTUATOR_DELAY = "pwm_actuator_delay"
CONF_ZONE_RESPONSE = "zone_response"
CONF_ZONE_FLOW_WEIGHT = "zone_flow_weight"
CONF_ZONE_SOLAR_WEIGHT = "zone_solar_weight"

CONTROL_MODE_BANG_BANG = "bang_bang"
CONTROL_MODE_PWM = "pwm"
ZONE_RESPONSE_SLOW = "slow"
ZONE_RESPONSE_FAST = "fast"

DEFAULT_HEATING_BASE_OFFSET = 3.0
DEFAULT_COOLING_BASE_OFFSET = 2.5
DEFAULT_FLOW_CURVE_OFFSET = 0.0
DEFAULT_WEATHER_SLOPE_HEAT = 0.25
DEFAULT_WEATHER_SLOPE_COOL = 0.2
DEFAULT_PRO_KP = 1.0
DEFAULT_PRO_USE_INTEGRAL = False
DEFAULT_PRO_TI_MINUTES = 180
DEFAULT_PRO_I_MAX = 1.5
DEFAULT_PRO_ERROR_NORM_MAX = 2.0
DEFAULT_PRO_DUTY_EMA_MINUTES = 20
DEFAULT_PRO_ERROR_WEIGHT = 0.6
DEFAULT_PRO_DUTY_WEIGHT = 0.4
DEFAULT_PRO_SLOW_MIX_WEIGHT = 0.8
DEFAULT_PRO_FAST_MIX_WEIGHT = 0.2
DEFAULT_PRO_FAST_ERROR_DEADBAND_C = 0.4
DEFAULT_PRO_FAST_BOOST_GAIN = 1.2
DEFAULT_PRO_FAST_BOOST_CAP_C = 1.2
DEFAULT_PRO_SLEW_UP_C_PER_5M = 0.3
DEFAULT_PRO_SLEW_DOWN_C_PER_5M = 0.2
DEFAULT_PRO_WRITE_DEADBAND_C = 0.3
DEFAULT_PRO_WRITE_MIN_INTERVAL_MINUTES = 10
DEFAULT_PRO_PREHEAT_ENABLED = False
DEFAULT_PRO_PREHEAT_FORECAST_SENSOR = None
DEFAULT_PRO_PREHEAT_SOLAR_SENSOR = None
DEFAULT_PRO_PREHEAT_GAIN = 0.35
DEFAULT_PRO_PREHEAT_SOLAR_GAIN_PER_W_M2 = 0.0
DEFAULT_PRO_PREHEAT_CAP_C = 1.2
DEFAULT_PRO_PREHEAT_MIN_SLOW_DI = 0.25
DEFAULT_ZONE_RESPONSE = ZONE_RESPONSE_SLOW
DEFAULT_ZONE_FLOW_WEIGHT = 1.0
DEFAULT_ZONE_SOLAR_WEIGHT = 1.0
CONF_HYSTERESIS = "hysteresis"

DEFAULT_CONTROL_MODE = CONTROL_MODE_BANG_BANG
DEFAULT_PWM_CYCLE_TIME = 15
DEFAULT_PWM_MIN_ON_TIME = 3
DEFAULT_PWM_MIN_OFF_TIME = 3
DEFAULT_PWM_KP = 30.0
DEFAULT_PWM_KI = 2.0
DEFAULT_PWM_ACTUATOR_DELAY = 3

FLOW_SCHEMA = vol.Schema(
    {
        vol.Optional(
            CONF_PRO_KP,
            default=DEFAULT_PRO_KP,
        ): vol.All(vol.Coerce(float), vol.Range(min=0, max=20)),
        vol.Optional(
            CONF_PRO_USE_INTEGRAL,
            default=DEFAULT_PRO_USE_INTEGRAL,
        ): vol.In([True, False]),
        vol.Optional(
            CONF_PRO_TI_MINUTES,
            default=DEFAULT_PRO_TI_MINUTES,
        ): vol.All(vol.Coerce(int), vol.Range(min=1, max=1440)),
        vol.Optional(
            CONF_PRO_I_MAX,
            default=DEFAULT_PRO_I_MAX,
        ): vol.All(vol.Coerce(float), vol.Range(min=0, max=20)),
        vol.Optional(
            CONF_PRO_ERROR_NORM_MAX,
            default=DEFAULT_PRO_ERROR_NORM_MAX,
        ): vol.All(vol.Coerce(float), vol.Range(min=0.1, max=20)),
        vol.Optional(
            CONF_PRO_DUTY_EMA_MINUTES,
            default=DEFAULT_PRO_DUTY_EMA_MINUTES,
        ): vol.All(vol.Coerce(int), vol.Range(min=1, max=360)),
        vol.Optional(
            CONF_PRO_ERROR_WEIGHT,
            default=DEFAULT_PRO_ERROR_WEIGHT,
        ): vol.All(vol.Coerce(float), vol.Range(min=0, max=1)),
        vol.Optional(
            CONF_PRO_DUTY_WEIGHT,
            default=DEFAULT_PRO_DUTY_WEIGHT,
        ): vol.All(vol.Coerce(float), vol.Range(min=0, max=1)),
        vol.Optional(
            CONF_PRO_SLOW_MIX_WEIGHT,
            default=DEFAULT_PRO_SLOW_MIX_WEIGHT,
        ): vol.All(vol.Coerce(float), vol.Range(min=0, max=1)),
        vol.Optional(
            CONF_PRO_FAST_MIX_WEIGHT,
            default=DEFAULT_PRO_FAST_MIX_WEIGHT,
        ): vol.All(vol.Coerce(float), vol.Range(min=0, max=1)),
        vol.Optional(
            CONF_PRO_FAST_ERROR_DEADBAND_C,
            default=DEFAULT_PRO_FAST_ERROR_DEADBAND_C,
        ): vol.All(vol.Coerce(float), vol.Range(min=0, max=5)),
        vol.Optional(
            CONF_PRO_FAST_BOOST_GAIN,
            default=DEFAULT_PRO_FAST_BOOST_GAIN,
        ): vol.All(vol.Coerce(float), vol.Range(min=0, max=20)),
        vol.Optional(
            CONF_PRO_FAST_BOOST_CAP_C,
            default=DEFAULT_PRO_FAST_BOOST_CAP_C,
        ): vol.All(vol.Coerce(float), vol.Range(min=0, max=20)),
        vol.Optional(
            CONF_PRO_SLEW_UP_C_PER_5M,
            default=DEFAULT_PRO_SLEW_UP_C_PER_5M,
        ): vol.All(vol.Coerce(float), vol.Range(min=0, max=20)),
        vol.Optional(
            CONF_PRO_SLEW_DOWN_C_PER_5M,
            default=DEFAULT_PRO_SLEW_DOWN_C_PER_5M,
        ): vol.All(vol.Coerce(float), vol.Range(min=0, max=20)),
        vol.Optional(
            CONF_WRITE_DEADBAND_C,
            default=DEFAULT_PRO_WRITE_DEADBAND_C,
        ): vol.All(vol.Coerce(float), vol.Range(min=0, max=5)),
        vol.Optional(
            CONF_WRITE_MIN_INTERVAL_MINUTES,
            default=DEFAULT_PRO_WRITE_MIN_INTERVAL_MINUTES,
        ): vol.All(vol.Coerce(int), vol.Range(min=1, max=120)),
        vol.Optional(
            CONF_PRO_PREHEAT_ENABLED,
            default=DEFAULT_PRO_PREHEAT_ENABLED,
        ): vol.In([True, False]),
        vol.Optional(
            CONF_PRO_PREHEAT_FORECAST_SENSOR,
            default=DEFAULT_PRO_PREHEAT_FORECAST_SENSOR,
        ): vol.Any(None, cv.entity_id),
        vol.Optional(
            CONF_PRO_PREHEAT_SOLAR_SENSOR,
            default=DEFAULT_PRO_PREHEAT_SOLAR_SENSOR,
        ): vol.Any(None, cv.entity_id),
        vol.Optional(
            CONF_PRO_PREHEAT_GAIN,
            default=DEFAULT_PRO_PREHEAT_GAIN,
        ): vol.All(vol.Coerce(float), vol.Range(min=0, max=20)),
        vol.Optional(
            CONF_PRO_PREHEAT_SOLAR_GAIN_PER_W_M2,
            default=DEFAULT_PRO_PREHEAT_SOLAR_GAIN_PER_W_M2,
        ): vol.All(vol.Coerce(float), vol.Range(min=0, max=1)),
        vol.Optional(
            CONF_PRO_PREHEAT_CAP_C,
            default=DEFAULT_PRO_PREHEAT_CAP_C,
        ): vol.All(vol.Coerce(float), vol.Range(min=0, max=20)),
        vol.Optional(
            CONF_PRO_PREHEAT_MIN_SLOW_DI,
            default=DEFAULT_PRO_PREHEAT_MIN_SLOW_DI,
        ): vol.All(vol.Coerce(float), vol.Range(min=0, max=1)),
    }
)

ZONE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CIRCUITS): [cv.entity_id],
        vol.Required(CONF_TEMP_SENSOR): cv.entity_id,
        vol.Optional(CONF_HYSTERESIS): vol.All(
            vol.Coerce(float),
            vol.Range(min=0, max=5),
        ),
        vol.Optional(
            CONF_ZONE_RESPONSE,
            default=DEFAULT_ZONE_RESPONSE,
        ): vol.In([ZONE_RESPONSE_SLOW, ZONE_RESPONSE_FAST]),
        vol.Optional(
            CONF_ZONE_FLOW_WEIGHT,
            default=DEFAULT_ZONE_FLOW_WEIGHT,
        ): vol.All(vol.Coerce(float), vol.Range(min=0, max=2)),
        vol.Optional(
            CONF_ZONE_SOLAR_WEIGHT,
            default=DEFAULT_ZONE_SOLAR_WEIGHT,
        ): vol.All(vol.Coerce(float), vol.Range(min=0, max=2)),
        vol.Optional(
            CONF_CONTROL_MODE,
            default=DEFAULT_CONTROL_MODE,
        ): vol.In([CONTROL_MODE_BANG_BANG, CONTROL_MODE_PWM]),
        vol.Optional(
            CONF_PWM_CYCLE_TIME,
            default=DEFAULT_PWM_CYCLE_TIME,
        ): vol.All(vol.Coerce(int), vol.Range(min=5, max=30)),
        vol.Optional(
            CONF_PWM_MIN_ON_TIME,
            default=DEFAULT_PWM_MIN_ON_TIME,
        ): vol.All(vol.Coerce(int), vol.Range(min=1, max=10)),
        vol.Optional(
            CONF_PWM_MIN_OFF_TIME,
            default=DEFAULT_PWM_MIN_OFF_TIME,
        ): vol.All(vol.Coerce(int), vol.Range(min=1, max=10)),
        vol.Optional(
            CONF_PWM_KP,
            default=DEFAULT_PWM_KP,
        ): vol.Coerce(float),
        vol.Optional(
            CONF_PWM_KI,
            default=DEFAULT_PWM_KI,
        ): vol.Coerce(float),
        vol.Optional(
            CONF_PWM_ACTUATOR_DELAY,
            default=DEFAULT_PWM_ACTUATOR_DELAY,
        ): vol.All(vol.Coerce(int), vol.Range(min=0, max=10)),
    }
)

CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({
        vol.Required(CONF_OUTSIDE_TEMP_SENSOR): cv.entity_id,
        vol.Optional(CONF_FLOW_TEMP_SENSOR): cv.entity_id,
        vol.Optional(CONF_HEAT_PUMP_MODE): cv.entity_id,
        vol.Optional(
            CONF_HEATING_BASE_OFFSET,
            default=DEFAULT_HEATING_BASE_OFFSET,
        ): vol.Coerce(float),
        vol.Optional(
            CONF_COOLING_BASE_OFFSET,
            default=DEFAULT_COOLING_BASE_OFFSET,
        ): vol.Coerce(float),
        vol.Optional(
            CONF_FLOW_CURVE_OFFSET,
            default=DEFAULT_FLOW_CURVE_OFFSET,
        ): vol.Coerce(float),
        vol.Optional(
            CONF_WEATHER_SLOPE_HEAT,
            default=DEFAULT_WEATHER_SLOPE_HEAT,
        ): vol.Coerce(float),
        vol.Optional(
            CONF_WEATHER_SLOPE_COOL,
            default=DEFAULT_WEATHER_SLOPE_COOL,
        ): vol.Coerce(float),
        vol.Optional(
            CONF_FLOW,
            default={},
        ): FLOW_SCHEMA,
        vol.Required(CONF_ZONES): {
            cv.string: ZONE_SCHEMA
        }
    })
}, extra=vol.ALLOW_EXTRA)


async def _async_load_yaml_config(hass: HomeAssistant) -> dict:
    """Return the current Thermozona YAML configuration."""
    yaml_config = await async_hass_config_yaml(hass)
    domain_config = yaml_config.get(DOMAIN)
    if domain_config is None:
        raise HomeAssistantError(
            "Thermozona is not configured in configuration.yaml"
        )

    return domain_config


def _validate_domain_config(domain_config: dict) -> dict:
    """Validate Thermozona YAML config against CONFIG_SCHEMA."""
    try:
        validated_config = CONFIG_SCHEMA({DOMAIN: domain_config})[DOMAIN]
    except vol.Invalid as err:
        raise HomeAssistantError(
            f"Invalid Thermozona configuration: {err}"
        ) from err

    return validated_config


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Thermozona component."""
    if DOMAIN not in config:
        return True

    hass.data[DOMAIN] = {}

    async def _async_handle_reload(_: ServiceCall) -> None:
        """Handle the reload service to re-import YAML configuration."""
        domain_config = await _async_load_yaml_config(hass)
        validated_config = _validate_domain_config(domain_config)

        entries = hass.config_entries.async_entries(DOMAIN)
        if entries:
            entry = entries[0]
            hass.config_entries.async_update_entry(entry, data=validated_config)
            await hass.config_entries.async_reload(entry.entry_id)
            return

        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_IMPORT},
                data=validated_config,
            )
        )

    hass.services.async_register(DOMAIN, SERVICE_RELOAD, _async_handle_reload)

    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_IMPORT},
            data=_validate_domain_config(config[DOMAIN]),
        )
    )

    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Thermozona from a config entry."""
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}

    domain_config = await _async_load_yaml_config(hass)
    validated_config = _validate_domain_config(domain_config)
    hass.config_entries.async_update_entry(entry, data=validated_config)

    hass.data[DOMAIN][entry.entry_id] = validated_config

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok and DOMAIN in hass.data:
        controllers = hass.data[DOMAIN].get("controllers")
        if controllers:
            controllers.pop(entry.entry_id, None)
            if not controllers:
                hass.data[DOMAIN].pop("controllers")
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
