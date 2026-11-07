# Compact Thermozona dashboard card

`thermozona-control-card.js` provides one compact Lovelace card with heating mode
buttons, automatic house/boiler charging switches, local-time charging windows,
and actionable sensor warnings. It uses browser APIs and Home Assistant elements,
with no additional HACS dependencies.

Copy the JavaScript file to `/config/www/thermozona-control-card.js` and add
`/local/thermozona-control-card.js` as a JavaScript module resource in Home
Assistant's dashboard resources. Reload the frontend after adding or updating it.

```yaml
type: custom:thermozona-control-card
```

The card uses the standard Thermozona entity IDs and the installation's
`sensor.elektriciteitsprijs_actief` price sensor. Its navigation links target the
Boiler and Detail views at `/dashboard-vloerverwarming/boiler` and
`/dashboard-vloerverwarming/detail`. The visible dashboard title can be
**Thermozona** while retaining that URL for existing links.

The switches enable or disable additional automatic charging. Ordinary comfort
control and native hot-water production remain governed by their existing
settings. Preview/monitor selections remain available through the standard
Home Assistant select entities, for example on the Detail view.
