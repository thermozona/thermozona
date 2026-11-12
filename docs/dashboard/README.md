# Compact Thermozona dashboard card

`thermozona-control-card.js` provides one compact Lovelace card with heating mode
buttons, automatic house/boiler charging switches, local-time charging windows,
and actionable sensor warnings. It uses browser APIs and Home Assistant elements,
with no additional HACS dependencies.

**Kamers plannen** opens a weekly editor inside the card. Each climate entity
with a `setback_schedule` attribute appears in the room selector. Add the periods
when that room may be cooler, then select **Opslaan**. Multiple blocks per day
are supported; split overnight periods at midnight. End time `00:00` means the
end of that day. Empty days leave normal comfort active. A room that is off
stays off. With `schedule_recovery.enabled`, Thermozona can resume normal
comfort before the period ends, using its estimated warming time.

The editor reads and writes UI Schedule helpers through Home Assistant's native
WebSocket API; administrator access is required. It preserves custom block data
and checks for concurrent edits before saving. Live sensor updates do not reset
unsaved input. The editor reports a successful save only after reading it back.

Copy the JavaScript file to `/config/www/thermozona-control-card.js` and add
`/local/thermozona-control-card.js` as a JavaScript module resource in Home
Assistant's dashboard resources. Reload the frontend after adding or updating it.

```yaml
type: custom:thermozona-control-card
```

For a visible **Kamerschema’s** dashboard tab, add a view with the editor opened
directly. The room selector lists every linked room, including rooms that are off.

```yaml
title: Kamerschema’s
path: schemas
icon: mdi:calendar-week
type: sections
max_columns: 1
sections:
  - type: grid
    cards:
      - type: custom:thermozona-control-card
        view: planner
```

Empty weeks mean no cooler periods have been configured yet. Select a room and
add its time blocks. Use **Wijzigingen verwerpen** to discard unsaved edits.

The card uses the standard Thermozona entity IDs and the installation's
`sensor.elektriciteitsprijs_actief` price sensor. Its navigation links target the
Boiler and Detail views at `/dashboard-vloerverwarming/boiler` and
`/dashboard-vloerverwarming/detail`. The visible dashboard title can be
**Thermozona** while retaining that URL for existing links.

The switches enable or disable additional automatic charging. Ordinary comfort
control and native hot-water production remain governed by their existing
settings. Preview/monitor selections remain available through the standard
Home Assistant select entities, for example on the Detail view.

Run card regression checks with `node --test tests/test_dashboard_card.cjs`.
