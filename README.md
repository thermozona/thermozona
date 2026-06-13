# Thermozona

<p align="center">
  <img src="https://raw.githubusercontent.com/thermozona/thermozona/main/assets/logo@2x.png" alt="Thermozona logo" height="256" />
</p>

<p align="center">
  <a href="https://my.home-assistant.io/redirect/hacs_repository/?owner=thermozona&repository=thermozona&category=integration">
    <img src="https://my.home-assistant.io/badges/hacs_repository.svg" alt="Open your Home Assistant instance and add Thermozona to HACS" />
  </a>
</p>

Thermozona is a Home Assistant custom integration for zoned underfloor heating and cooling. It turns rooms into climate entities, switches underfloor heating circuits, calculates heat or cool demand, and exposes helper entities you can use to drive your heat pump or mixing system.

Thermozona is YAML-first, local, and open source. It does not require cloud services.

Learn more at [thermozona.com](https://thermozona.com).

## What You Get

- One Home Assistant climate entity per configured zone.
- Heating and cooling support with manual or automatic mode selection.
- `bang_bang` and `pwm` zone control modes.
- Weather-aware flow temperature calculation.
- Internal helper entities for heat pump status, flow temperature, flow temperature history, and runtime curve offset.
- Demand-weighted heating flow calculation with slow/fast zone weighting, slew limiting, preheat forecast, and solar forecast compensation.

## Requirements

- Home Assistant with YAML access.
- One temperature sensor per zone.
- One or more switch-like entities per zone for valves, relays, actuators, or manifold outputs.
- An outdoor temperature sensor.
- Optional: a heat pump, mixing valve, or automation that uses Thermozona's helper entities.

Circuit entities can be real switches, relay outputs, KNX switches, Zigbee relays, ESPHome outputs, MQTT switches, or `input_boolean` helpers for testing.

## Install

### HACS

1. Open HACS in Home Assistant.
2. Go to `Integrations`.
3. Search for `Thermozona`.
4. Install the integration.
5. Restart Home Assistant.

### Manual Install

1. Clone this repository.
2. Copy the integration into your Home Assistant config directory:

```bash
./scripts/install_manual.sh /path/to/home-assistant-config
```

Example for a typical container install:

```bash
./scripts/install_manual.sh /config
```

Restart Home Assistant after copying the files.

## Quick Start

Add this to `configuration.yaml` and change the entity IDs to match your Home Assistant setup:

```yaml
thermozona:
  outside_temp_sensor: sensor.outdoor_temperature

  zones:
    living_room:
      temp_sensor: sensor.living_room_temperature
      circuits:
        - switch.manifold_living_left
        - switch.manifold_living_right

    bathroom:
      temp_sensor: sensor.bathroom_temperature
      circuits:
        - switch.manifold_bathroom
```

Restart Home Assistant.

After startup you should see:

- `climate.thermozona_living_room`
- `climate.thermozona_bathroom`
- `select.thermozona_heat_pump_mode`
- `sensor.thermozona_heat_pump_status`
- `number.thermozona_flow_temperature`
- `sensor.thermozona_flow_temperature`
- `number.thermozona_flow_curve_offset`

Set a zone target temperature from the climate entity. Thermozona will turn the configured circuit entities on or off based on demand.

## Connect Your Heat Pump

Thermozona does not directly know every heat pump protocol. Instead, it exposes local Home Assistant helper entities that you can map to Modbus, KNX, MQTT, ESPHome, automations, or scripts.

Use these entities on the plant side:

- `sensor.thermozona_heat_pump_status`: current demand direction, one of `heat`, `cool`, or `idle`.
- `number.thermozona_flow_temperature`: current target supply temperature calculated by Thermozona.
- `sensor.thermozona_flow_temperature`: same calculated flow temperature as a sensor with history and diagnostic attributes.
- `select.thermozona_heat_pump_mode`: internal selector for `auto`, `heat`, `cool`, or `off`, unless you configure an external mode entity.
- `number.thermozona_flow_curve_offset`: temporary offset for tuning the heating/cooling curve from the UI.

For an Ecoforest Modbus example, see [`docs/heatpump-ecoforest.md`](docs/heatpump-ecoforest.md).

## Configuration Reference

Thermozona is configured exclusively through YAML. There is no UI config flow for settings yet.

### Minimal Required Keys

```yaml
thermozona:
  outside_temp_sensor: sensor.outdoor_temperature
  zones:
    living_room:
      temp_sensor: sensor.living_room_temperature
      circuits:
        - switch.manifold_living
```

### Full Example

```yaml
thermozona:
  outside_temp_sensor: sensor.outdoor_temperature

  # Optional: external entity that controls heat/cool/auto/off.
  # If omitted, Thermozona creates select.thermozona_heat_pump_mode.
  # heat_pump_mode: input_select.heat_pump_mode

  # Optional: external input_number to receive the calculated flow temperature.
  # If omitted, use number.thermozona_flow_temperature.
  # flow_temp_sensor: input_number.heat_pump_flow_temperature

  heating_base_offset: 3.0
  cooling_base_offset: 2.5
  flow_curve_offset: 0.0
  weather_slope_heat: 0.25
  weather_slope_cool: 0.20

  flow:
    write_deadband_c: 0.3
    write_min_interval_minutes: 10
    kp: 1.0
    use_integral: false
    preheat_enabled: false

  zones:
    living_room:
      temp_sensor: sensor.living_room_temperature
      circuits:
        - switch.manifold_living_left
        - switch.manifold_living_right
      hysteresis: 0.3
      control_mode: bang_bang

    bathroom:
      temp_sensor: sensor.bathroom_temperature
      circuits:
        - switch.manifold_bathroom
      hysteresis: 0.2
```

### Top-Level Options

| Key | Required | Default | Description |
| --- | --- | --- | --- |
| `outside_temp_sensor` | Yes | none | Outdoor temperature entity used for weather compensation. |
| `zones` | Yes | none | Mapping of zone names to temperature sensors and circuit outputs. |
| `flow_temp_sensor` | No | internal number entity | Optional external `input_number` target for the calculated flow temperature. |
| `heat_pump_mode` | No | internal select entity | Optional external entity for heat/cool/auto/off mode. |
| `heating_base_offset` | No | `3.0` | Base degrees added above the warmest active target in heating. |
| `cooling_base_offset` | No | `2.5` | Base degrees subtracted below the coolest active target in cooling. |
| `flow_curve_offset` | No | `0.0` | Baseline offset applied to both heating and cooling flow calculations. |
| `weather_slope_heat` | No | `0.25` | Heating weather compensation slope. |
| `weather_slope_cool` | No | `0.20` | Cooling weather compensation slope. |
| `flow` | No | see schema | Flow write throttling, supervised heating tuning, and optional preheat settings. |

### Zone Options

| Key | Required | Default | Description |
| --- | --- | --- | --- |
| `temp_sensor` | Yes | none | Room temperature sensor for the zone. |
| `circuits` | Yes | none | List of switch-like entities controlled by the zone. |
| `hysteresis` | No | `0.3` | Deadband around the target temperature for `bang_bang` mode. |
| `control_mode` | No | `bang_bang` | `bang_bang` or `pwm`. |
| `pwm_cycle_time` | No | `15` | PWM cycle length in minutes, range `5` to `30`. |
| `pwm_min_on_time` | No | `3` | Minimum PWM on time in minutes, range `1` to `10`. |
| `pwm_min_off_time` | No | `3` | Minimum PWM off time in minutes, range `1` to `10`. |
| `pwm_kp` | No | `30.0` | PWM proportional gain. |
| `pwm_ki` | No | `2.0` | PWM integral gain. |
| `pwm_actuator_delay` | No | `3` | Delay compensation for slow thermal actuators, in minutes. |
| `zone_response` | No | `slow` | `slow` or `fast`, used by the heating flow calculation. |
| `zone_flow_weight` | No | `1.0` | Demand weighting for the heating flow calculation. |
| `zone_solar_weight` | No | `1.0` | Solar softening weight for preheat compensation. |

Legacy `groups` is still accepted as an alias for `circuits`, but new configs should use `circuits`.

## Flow Temperature Calculation

Thermozona uses one flow-temperature path:

- Heating uses demand-weighted supervision. It considers zone demand, duty cycle, slow/fast zone behavior, optional integral trim, slew limits, weather compensation, and optional preheat forecast compensation.
- Cooling uses the cooling curve based on the coolest active target, cooling base offset, cooling weather slope, and flow curve offset.

Optional flow tuning:

Example:

```yaml
thermozona:
  flow:
    kp: 1.0
    use_integral: false
    ti_minutes: 180
    i_max: 1.5
    error_norm_max: 2.0
    duty_ema_minutes: 20
    error_weight: 0.6
    duty_weight: 0.4
    slow_mix_weight: 0.8
    fast_mix_weight: 0.2
    fast_error_deadband_c: 0.4
    fast_boost_gain: 1.2
    fast_boost_cap_c: 1.2
    slew_up_c_per_5m: 0.3
    slew_down_c_per_5m: 0.2
    write_deadband_c: 0.3
    write_min_interval_minutes: 10
```

Optional preheat forecast:

```yaml
thermozona:
  flow:
    preheat_enabled: true
    preheat_forecast_sensor: sensor.outdoor_forecast_2h
    preheat_solar_sensor: sensor.solar_irradiance_forecast_2h
    preheat_gain: 0.35
    preheat_solar_gain_per_w_m2: 0.002
    preheat_cap_c: 1.2
    preheat_min_slow_di: 0.25
```

`preheat_forecast_sensor` should expose a forecast outdoor temperature in degrees Celsius. `preheat_solar_sensor` should expose forecast solar irradiance in `W/m2`.

## Zone Control Modes

### `control_mode: bang_bang`

This is the default. Thermozona turns circuit outputs on when the room needs heat or cooling, then turns them off when the target plus hysteresis is reached.

### `control_mode: pwm`

PWM mode calculates a 0-100% duty cycle each cycle. It is useful for high thermal-mass floors where plain on/off control overshoots.

Example:

```yaml
thermozona:
  zones:
    living_room:
      temp_sensor: sensor.living_room_temperature
      circuits:
        - switch.manifold_living
      control_mode: pwm
      pwm_cycle_time: 15
      pwm_min_on_time: 3
      pwm_min_off_time: 3
      pwm_kp: 30.0
      pwm_ki: 2.0
      pwm_actuator_delay: 3
```

## Reloading YAML

After changing `configuration.yaml`, use one of these options:

- Restart Home Assistant.
- Reload Thermozona from the Integrations UI.
- Call the `thermozona.reload` service from Developer Tools.

Thermozona re-imports YAML on setup and reload so the YAML file remains the source of truth.

## Debugging

Enable debug logging:

```yaml
logger:
  logs:
    custom_components.thermozona: debug
```

Useful checks:

- Confirm each `temp_sensor` has a numeric state.
- Confirm each circuit entity can be turned on and off manually from Home Assistant.
- Inspect `sensor.thermozona_flow_temperature` attributes to see calculation details.
- Inspect `sensor.thermozona_heat_pump_status` to see whether Thermozona currently requests heat, cool, or idle.
- If no flow temperature is written, verify `outside_temp_sensor` exists and has a numeric state.

## Flow Temperature Attributes

`sensor.thermozona_flow_temperature` exposes diagnostic attributes for dashboards and tuning.

Common attributes include:

- `effective_mode`: `heat` or `cool`.
- `outside_temp_c`: outdoor temperature used in the calculation.
- `flow_curve_offset_c`: active flow curve offset.
- `flow_temp_unclamped_c`: calculated value before clamping.
- `flow_temp_c`: final value after clamping.
- `clamp_min_c` and `clamp_max_c`: applied bounds.

Cooling adds values such as `target_ref_c`, `base_offset_c`, `weather_slope`, and `weather_comp_c`.

Heating adds values such as `demand_index`, `di_slow`, `di_fast`, `trim_p_c`, `integral_c`, `fast_boost_c`, and `preheat_boost_c`.

## Safety

Thermozona is provided for DIY/home automation use at your own risk.

You are responsible for correct installation, wiring, relay sizing, actuator compatibility, hydronic safety, and heat pump protections. Follow local electrical and building codes and use a qualified installer where needed.

The authors and contributors are not liable for damages, losses, injuries, equipment failures, overheating, water damage, fire, or other incidents resulting from installation, configuration, or use.

## Contributing

Issues, feature requests, and pull requests are welcome. Please include your relevant YAML, entity IDs, logs, and hardware context when reporting problems.

## License

Thermozona is licensed under the MIT license. See [`LICENSE`](LICENSE).
