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
- Dew-point-aware floor cooling, air-coil cooling, and per-zone heating flow minimums.

## Requirements

- Home Assistant with YAML access.
- One temperature sensor per zone.
- One or more switch-like entities per zone for valves, relays, actuators, or manifold outputs.
- An outdoor temperature sensor.
- Optional: an indoor dew-point sensor for floor cooling protection.
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
- `select.thermozona_cooling_strategy`
- `sensor.thermozona_heat_pump_status`
- `number.thermozona_flow_temperature`
- `sensor.thermozona_flow_temperature`
- `number.thermozona_flow_curve_offset`
- `select.thermozona_price_charging`
- `sensor.thermozona_price_charging`

Set a zone target temperature from the climate entity. Thermozona will turn the configured circuit entities on or off based on demand.

## Connect Your Heat Pump

Thermozona does not directly know every heat pump protocol. Instead, it exposes local Home Assistant helper entities that you can map to Modbus, KNX, MQTT, ESPHome, automations, or scripts.

Use these entities on the plant side:

- `sensor.thermozona_heat_pump_status`: current demand direction, one of `heat`, `cool`, or `idle`.
- `number.thermozona_flow_temperature`: current target supply temperature calculated by Thermozona.
- `sensor.thermozona_flow_temperature`: same calculated flow temperature as a sensor with history and diagnostic attributes.
- `select.thermozona_heat_pump_mode`: internal selector for `auto`, `heat`, `cool`, or `off`, unless you configure an external mode entity.
- `select.thermozona_cooling_strategy`: selects `auto` or `air_coil_only` cooling; its selection is restored across restarts.
- `number.thermozona_flow_curve_offset`: runtime offset for tuning the heating/cooling curve from the UI, restored across restarts.

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
  auto_heat_lockout_above_c: 18.0
  auto_cool_lockout_below_c: 20.0
  auto_min_changeover_minutes: 30

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
| `auto_heat_lockout_above_c` | No | `18.0` | Disables automatic heating above this outdoor temperature. |
| `auto_cool_lockout_below_c` | No | `20.0` | Disables automatic cooling below this outdoor temperature. |
| `auto_min_changeover_minutes` | No | `30` | Minimum interval between automatic heat/cool changes, range `0` to `1440` minutes. |
| `flow` | No | see schema | Flow write throttling, supervised heating tuning, preheat, and cooling settings. |

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
| `zone_flow_weight` | No | `1.0` | Demand weighting for heating flow and automatic heat/cool selection. |
| `zone_solar_weight` | No | `1.0` | Solar softening weight for preheat compensation. |
| `zone_type` | No | `floor` | `floor`, `wall`, or `air_coil`. |
| `air_coil_requested_water_sensor` | No | `sensor.thermozona_flow_temperature` | Requested supply-water temperature entity in °C for the air-coil heating interlock. |
| `air_coil_measured_water_sensor` | No | `sensor.heatpump_heating_out` | Measured supply-water temperature entity in °C for the air-coil heating interlock. |
| `heating_min_flow_c` | No | none | Heating flow minimum requested while this zone is active and below target, range `15` to `35`°C. Changes are ramped. |
| `floor_material` | No | none | `wood` adds 1°C to the default dew-point margin unless a zone margin is explicitly configured. |
| `cooling_dewpoint_margin` | No | flow default | Zone-specific degrees above indoor dew point required during protected cooling, range `0` to `10`°C. |
| `cooling_dewpoint_protection` | No | `true` | Applies dew-point protection to this floor/wall zone when a dew-point sensor is configured. |

Legacy `groups` is still accepted as an alias for `circuits`, but new configs should use `circuits`.

### Automatic Mode

Automatic mode uses room temperature errors weighted by `zone_flow_weight`, ignoring errors within 0.2°C of target. It idles when there is no net demand, when the requested direction is locked out by outdoor temperature, or when the configured outdoor sensor has no valid value.

With the defaults, heating is allowed at or below 18°C outdoors and cooling at or above 20°C. A change between heating and cooling normally waits 30 minutes, idling during the wait. The changeover wait is bypassed when outdoor temperature already locks out the previous direction. Explicit `heat` and `cool` modes bypass these automatic lockouts.

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

### Cooling and Dew-Point Protection

Cooling settings inside `flow`:

| Key | Default | Description |
| --- | --- | --- |
| `cooling_dewpoint_sensor` | none | Indoor dew-point sensor in °C. Protection requires a configured sensor. |
| `cooling_floor_dewpoint_margin` | `2.0` | Default margin above dew point for protected floor/wall zones, range `0` to `10`°C. |
| `cooling_dehumidification_min_flow` | `5.0` | Lower cooling flow bound, range `5` to `25`°C. |
| `cooling_air_coil_only_offset` | `0.0` | Extra degrees subtracted from flow when only air-coil zones cool, range `0` to `15`°C. |

For example, merge these settings into your existing configuration:

```yaml
thermozona:
  flow:
    cooling_dewpoint_sensor: sensor.indoor_dewpoint
    cooling_floor_dewpoint_margin: 2.0
    cooling_dehumidification_min_flow: 5.0
    cooling_air_coil_only_offset: 5.0
  zones:
    living_room:
      temp_sensor: sensor.living_room_temperature
      circuits:
        - switch.manifold_living
      zone_type: floor
      floor_material: wood
    ventilation_coil:
      temp_sensor: sensor.indoor_temperature
      circuits:
        - switch.ventilation_coil
      zone_type: air_coil
      zone_response: fast
      air_coil_requested_water_sensor: sensor.thermozona_flow_temperature
      air_coil_measured_water_sensor: sensor.heat_pump_supply_temperature
```

Protected floor/wall zones raise the shared flow target to at least indoor dew point plus their margin. They close when that constraint exceeds 25°C or conflicts with colder air-coil demand. A configured dew-point sensor becoming unavailable also closes protected zones and blocks cooling commands while protected floor circuits remain active. Setting `cooling_dewpoint_protection: false` explicitly excludes a zone from this protection. Without a configured dew-point sensor, no dew-point limit is applied.

Selecting `air_coil_only` closes all floor/wall cooling zones, including zones with dew-point protection disabled. `auto` lets zones participate according to demand and dew-point constraints. Air-coil-only demand can use the lower cooling bound and extra offset for dehumidification.

### Per-Zone Heating Minimums

A wall zone can set `heating_min_flow_c: 32.0`, for example. Its minimum contributes only while its circuit is active and its room remains below target. After the initial command, the shared command ramps up by at most 1.5°C and down by at most 2°C per five minutes, alongside the normal heating curve slew limits.

### Charge the House During Cheaper Electricity Periods

Price charging temporarily raises the effective heating target of selected floor zones. Your normal climate target remains the comfort target, including after a restart. When charging ends, zones return to that target and use the accumulated warmth. The planner never lowers the comfort target or prevents normal heating during expensive periods.

This optional feature uses the installed Home Assistant Tibber integration and its `tibber.get_prices` action. No additional account token is stored in Thermozona. Price charging is off by default and each participating floor zone must opt in:

```yaml
thermozona:
  price_charging:
    mode: preview
    boost_c: 0.5
    charge_hours: 3
    coast_hours: 6
    min_price_spread: 0.03
    # home: My home  # Required only when Tibber returns multiple homes.
  zones:
    living_room:
      temp_sensor: sensor.living_room_temperature
      circuits:
        - switch.manifold_living
      price_charging: true
```

Merge this into your existing configuration. `select.thermozona_price_charging` offers:

- `off`: normal operation, with no price-related target adjustment.
- `preview`: fetch prices and display a plan while keeping normal targets.
- `auto`: apply temporary headroom during the selected charging block.

The selection is restored across restarts. Start with preview to inspect the plan, then choose auto to use it. The planner selects the cheapest complete, contiguous block starting within the next 24 hours whose average price is sufficiently below the following coast period. It retains an active block through charging and coasting. It uses timezone-aware hourly or quarter-hour intervals, checks coverage, and requests fresh prices hourly. Missing or invalid prices remove the boost and leave normal comfort control running.

| Option under `price_charging` | Default | Description |
| --- | --- | --- |
| `mode` | `off` | Initial mode: `off`, `preview`, or `auto`. The UI selection is restored when available. |
| `home` | only returned home | Tibber home name; required when the response contains multiple homes. |
| `boost_c` | `0.5` | Extra effective room target in °C, range `0` to `2`, capped by the climate entity maximum. |
| `charge_hours` | `3` | Contiguous charging window length, range `0.5` to `6` hours. |
| `coast_hours` | `6` | Following period used for price comparison and the coasting phase, range `1` to `12` hours. |
| `min_price_spread` | `0.03` | Required average price difference per kWh in the Tibber account currency. |

Only opted-in `floor` zones receive the boost. Manual off, cooling mode, sensor protections, and automatic heating lockouts take precedence. PWM demand is recalculated when headroom changes, and a zone closes at its effective charging target rather than extending its normal hysteresis above that target. Physical room temperature can still overshoot because floors retain heat.

`sensor.thermozona_price_charging` exposes the selected phase, `charge_start`, `charge_end`, average charging/following prices, price freshness, and errors. In preview, `planned_state` shows what auto would do. Each climate entity exposes `price_charging_enabled`, `price_charging_boost_c`, and `effective_target_temperature` alongside its unchanged normal target.

This first version compares electricity prices. It does not estimate stored kWh, guaranteed bill savings, future COP, heat losses, or solar gains. Existing weather compensation and flow limits remain active; use actual temperature and energy history to tune headroom and window length.

### Air-Coil Heating Interlock

Air-coil heating closes when requested or measured water reaches 27°C, and reopens only once both values are at or below 25°C. Missing or invalid water readings also keep it closed.

Set `air_coil_requested_water_sensor` and `air_coil_measured_water_sensor` on each `air_coil` zone to match your installation. Both entities must report temperatures in °C. The defaults preserve existing configurations: `sensor.thermozona_flow_temperature` for requested water temperature and `sensor.heatpump_heating_out` for measured water temperature. An unavailable configured sensor keeps air-coil heating closed; the interlock does not apply during cooling.

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

During integration unload/reload, pending control work is cancelled and circuits are closed. Startup holds control off until the mode and flow output platforms are ready. A missing or unavailable external mode entity keeps control off. Invalid room temperatures close the affected zone and reset its PWM state.

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

- `effective_mode`: `heat`, `cool`, or `idle` when a previous command becomes inactive.
- `command_active`: whether the cached flow value currently represents active demand.
- `command_updated_at`: timestamp of the latest command calculation or deactivation.
- `active_zones`: zones participating in the active command.
- `outside_temp_c`: outdoor temperature used in the calculation.
- `flow_curve_offset_c`: active flow curve offset.
- `flow_temp_unclamped_c`: calculated value before clamping.
- `flow_temp_c`: final value after clamping.
- `clamp_min_c` and `clamp_max_c`: applied bounds.

Cooling adds values such as `target_ref_c`, `base_offset_c`, `weather_slope`, `weather_comp_c`, `cooling_dewpoint_c`, `cooling_floor_constraint_c`, `cooling_air_coil_only_mode`, `cooling_active_floor_zones`, `cooling_active_air_coil_zones`, and `cooling_suppressed_floor_zones`.

Heating adds values such as `demand_index`, `di_slow`, `di_fast`, `trim_p_c`, `integral_c`, `fast_boost_c`, `preheat_boost_c`, `active_zone_min_flow_c`, and `active_min_flow_zones`.

The last numeric flow value is retained when demand ends. Use `command_active` and heat pump status to distinguish it from a current command.

## Safety

Thermozona is provided for DIY/home automation use at your own risk.

You are responsible for correct installation, wiring, relay sizing, actuator compatibility, hydronic safety, and heat pump protections. Follow local electrical and building codes and use a qualified installer where needed.

The authors and contributors are not liable for damages, losses, injuries, equipment failures, overheating, water damage, fire, or other incidents resulting from installation, configuration, or use.

## Contributing

Issues, feature requests, and pull requests are welcome. Please include your relevant YAML, entity IDs, logs, and hardware context when reporting problems.

## License

Thermozona is licensed under the MIT license. See [`LICENSE`](LICENSE).

### Domestic Hot Water

Optional `hot_water` support coordinates additional boiler charging through Home Assistant services. It does not require EVCC. The heat pump retains its native hot-water control; disabling Thermozona hot-water boosting does not disable ordinary hot-water production.

```yaml
thermozona:
  hot_water:
    mode: monitor
    temperature_sensor: sensor.hot_water_temperature
    soc_sensor: sensor.hot_water_soc
    demand_entity: input_boolean.hot_water_request
    source_entity: input_select.hot_water_source
    last_off_entity: input_datetime.hot_water_last_off
    started_at_entity: input_datetime.hot_water_started_at
    grid_sensor: sensor.grid_power
    start_service: input_boolean.turn_on
    start_data:
      entity_id: input_boolean.hot_water_request
    stop_service: input_boolean.turn_off
    stop_data:
      entity_id: input_boolean.hot_water_request
```

The request entity must connect to your installation's existing protected start/stop automations. Those automations must record actual start/stop timestamps and enforce the equipment's minimum runtime. The source selector must provide `none`, `solar`, and `price`. Timestamp helpers must include both date and time.

`select.thermozona_hot_water` offers `off`, `monitor`, and `auto`. Monitor fetches prices and displays the plan without requesting heat. Auto selects the cheapest complete one-hour block starting within the next 24 hours and requests a boost when SOC is below 40%, provided the four-hour cooldown has elapsed. Stable grid export above 2,500 W for five minutes can also request a solar boost. Solar requests end after five minutes of grid import above 500 W, or one minute of missing grid measurements. All additional requests end at 99% SOC, 65°C, invalid tank readings, or a 90-minute maximum runtime. Existing equipment protections and the Home Assistant actuation bridge remain necessary.

`sensor.thermozona_hot_water` exposes temperature, SOC, request ownership, the selected window, and errors. This first implementation uses the timing and thresholds above; it does not replace native hygiene cycles or calculate stored energy from temperature.
