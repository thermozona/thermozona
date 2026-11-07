/* Compact Lovelace controls; uses Home Assistant and the browser only. */
class ThermozonaControlCard extends HTMLElement {
  setConfig(config) {
    this._config = config;
    if (!this.shadowRoot) {
      this.attachShadow({ mode: "open" });
      this.shadowRoot.addEventListener("click", (event) => this._click(event));
    }
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    const entities = Object.entries(hass.states).filter(([id]) =>
      id.includes("thermozona") || id === "sensor.elektriciteitsprijs_actief" ||
      id === "schedule.werkkamer_buitenshuis" ||
      id === "binary_sensor.ventilation_floor_condensation_risk"
    );
    if (entities.length !== this._previous?.size ||
        entities.some(([id, state]) => this._previous?.get(id) !== state)) {
      this._previous = new Map(entities);
      this._render();
    }
  }

  getCardSize() { return 6; }
  getGridOptions() { return { columns: 12, rows: "auto", min_columns: 6 }; }

  _state(id) { return this._hass?.states[id]; }
  _value(id) { return this._state(id)?.state; }
  _attrs(id) { return this._state(id)?.attributes || {}; }
  _available(id) { return ![undefined, "unknown", "unavailable"].includes(this._value(id)); }
  _escape(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }
  _number(value, digits = 1) {
    if (value == null || value === "" || !Number.isFinite(Number(value))) return "—";
    return Number(value).toLocaleString("nl-NL", {
      minimumFractionDigits: digits, maximumFractionDigits: digits,
    });
  }
  _window(attrs) {
    const start = new Date(attrs.charge_start || "");
    const end = new Date(attrs.charge_end || "");
    if (!Number.isFinite(start.getTime()) || !Number.isFinite(end.getTime())) return "Geen laadblok gepland";
    const timeZone = this._hass?.config?.time_zone || "Europe/Amsterdam";
    const dateFormat = new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" });
    const dayNumber = (date) => {
      const parts = Object.fromEntries(dateFormat.formatToParts(date).map((p) => [p.type, p.value]));
      return Date.UTC(Number(parts.year), Number(parts.month) - 1, Number(parts.day)) / 86400000;
    };
    const delta = dayNumber(start) - dayNumber(new Date());
    const day = delta === 0 ? "Vandaag" : delta === 1 ? "Morgen" :
      new Intl.DateTimeFormat("nl-NL", { timeZone, day: "numeric", month: "short" }).format(start);
    const time = new Intl.DateTimeFormat("nl-NL", { timeZone, hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
    return `${day} ${time.format(start)}–${time.format(end)}`;
  }
  _row(kind, label, icon) {
    const boiler = kind === "boiler";
    const select = boiler ? "select.thermozona_hot_water" : "select.thermozona_price_charging";
    const sensor = boiler ? "sensor.thermozona_hot_water" : "sensor.thermozona_price_charging";
    const attrs = this._attrs(sensor);
    const mode = this._value(select);
    const checked = mode === "auto";
    let detail;
    if (!this._available(select) || !this._available(sensor)) detail = "Gegevens ontbreken";
    else if (mode === "off") detail = "Uit · normaal comfort blijft actief";
    else {
      const phase = this._value(sensor);
      const plan = phase === "learning_usage" ? "Leert je warmwatergebruik" :
        !attrs.charge_start ? "Geen voordelig laadmoment" : this._window(attrs);
      const target = boiler && attrs.target_temperature != null ? ` · tot ${this._number(attrs.target_temperature)} °C` : "";
      detail = `${mode === "auto" ? "" : "Alleen bekijken · "}${plan}${target}`;
    }
    const extra = boiler ? `${this._number(attrs.temperature)} °C` :
      mode === "off" ? "" : `+${this._number(attrs.planned_boost_c ?? attrs.configured_boost_c ?? 0.5)} °C`;
    return `<div class="charge-row">
      <ha-icon class="row-icon" icon="${icon}"></ha-icon>
      <div class="row-text"><div class="row-title">${label}<span class="row-value">${this._escape(extra)}</span></div>
        <div class="secondary">${this._escape(detail)}</div></div>
      <button class="switch ${checked ? "checked" : ""}" role="switch" aria-checked="${checked}"
        aria-label="${label} automatisch" data-action="toggle" data-entity="${select}"
        ${this._pending || !this._available(select) ? "disabled" : ""}><span></span></button>
    </div>`;
  }
  _workSchedule() {
    if (!this._state("schedule.werkkamer_buitenshuis")) return "";
    const active = this._value("schedule.werkkamer_buitenshuis") === "on";
    const attrs = this._attrs("climate.thermozona_begane_grond_werkkamer");
    const target = attrs.scheduled_target_temperature ?? attrs.temperature;
    return `<button class="work-plan" data-action="work-plan"><ha-icon icon="mdi:calendar-week"></ha-icon>
      <span>Werkkamer · ${active ? "Buitenshuis" : "Normaal"}${target == null ? "" : ` · ${this._number(target)} °C`}
      <small>Werkschema aanpassen</small></span><ha-icon icon="mdi:chevron-right"></ha-icon></button>`;
  }
  _alerts() {
    const problems = [
      ["binary_sensor.ventilation_floor_condensation_risk", "Condensrisico bij vloerkoeling"],
      ["binary_sensor.thermozona_ecoforest_mode_mismatch", "Warmtepomp wijkt af van de gevraagde modus"],
      ["binary_sensor.thermozona_ecoforest_setpoint_mismatch", "Warmtepomp wijkt af van de gevraagde temperatuur"],
      ["binary_sensor.thermozona_zone_temperature_problem", "Temperatuursensor ontbreekt"],
    ];
    const active = problems.filter(([id]) => this._value(id) === "on");
    if (!active.length) {
      const unknown = problems.some(([id]) => !this._available(id));
      return `<div class="quiet"><ha-icon icon="mdi:${unknown ? "help-circle-outline" : "check-circle-outline"}"></ha-icon>${unknown ? "Niet alle controles zijn beschikbaar" : "Geen meldingen"}</div>`;
    }
    return active.map(([id, label]) => {
      let description = "Tik voor details";
      if (id.endsWith("zone_temperature_problem")) {
        const missing = Object.values(this._hass.states).filter((state) =>
          state.entity_id.startsWith("climate.thermozona_") &&
          !Number.isFinite(state.attributes.current_temperature)
        ).map((state) => (state.attributes.friendly_name || state.entity_id).replace(/^Thermozona\s+/i, ""));
        if (missing.length) {
          label = missing.length === 1 ? "1 sensor ontbreekt" : `${missing.length} sensoren ontbreken`;
          description = missing.join(" en ");
        }
      }
      return `<button class="alert" data-action="info" data-entity="${id}">
        <ha-icon icon="mdi:alert-circle-outline"></ha-icon><span><strong>${this._escape(label)}</strong>
          <span class="alert-detail">${this._escape(description)}</span></span><ha-icon class="chevron" icon="mdi:chevron-right"></ha-icon></button>`;
    }).join("");
  }
  _render() {
    if (!this.shadowRoot || !this._hass) return;
    const mode = this._value("select.thermozona_heat_pump_mode");
    const boiler = this._attrs("sensor.thermozona_hot_water").space_heating_blocked;
    const status = this._value("sensor.thermozona_heat_pump_status");
    const statusLabel = boiler ? "Boiler heeft voorrang" : mode === "off" ? "Uitgeschakeld" :
      ({ heat: "Verwarmen", cool: "Koelen", idle: "Geen warmtevraag", off: "Uitgeschakeld" }[status] || "Status niet beschikbaar");
    const activeFlow = this._attrs("sensor.thermozona_flow_temperature").command_active === true;
    const flow = boiler ? "Vloerverwarming wacht" : activeFlow ?
      `${this._number(this._value("sensor.thermozona_flow_temperature"))} °C aanvoer` : "Geen actieve aanvoervraag";
    const modes = [["off", "Uit"], ["auto", "Auto"], ["heat", "Verwarmen"], ["cool", "Koelen"]];
    this.shadowRoot.innerHTML = `<style>
      :host{display:block;color:var(--primary-text-color);font-family:var(--primary-font-family,Roboto,sans-serif)}
      *{box-sizing:border-box}ha-card{display:block;padding:18px;border-radius:var(--ha-card-border-radius,16px);overflow:hidden}
      button{font:inherit;color:inherit;cursor:pointer;-webkit-tap-highlight-color:transparent}
      button:focus-visible{outline:2px solid var(--primary-color,#03a9f4);outline-offset:3px}
      button:disabled{cursor:wait;opacity:.5}.status{display:flex;align-items:center;gap:10px;margin-bottom:16px}
      .status-icon{color:var(--primary-color,#03a9f4);--mdc-icon-size:26px}.status-text{flex:1;min-width:0}
      .status-title{font-size:18px;font-weight:600;line-height:24px}.secondary{font-size:12px;line-height:19px;color:var(--secondary-text-color);overflow-wrap:anywhere}
      .price{font-size:12px;white-space:nowrap;align-self:flex-start;margin-top:3px;color:var(--secondary-text-color)}
      .modes{display:flex;gap:3px;padding:3px;background:var(--secondary-background-color);border:1px solid var(--divider-color);border-radius:10px}
      .mode{flex:1;min-width:0;border:1px solid transparent;background:none;border-radius:7px;min-height:36px;padding:4px;font-size:12px;font-weight:500}
      .mode.selected{border-color:var(--primary-color,#03a9f4);background:color-mix(in srgb,var(--primary-color,#03a9f4) 17%,transparent)}
      .charges{margin:16px 0 8px;border-top:1px solid var(--divider-color)}
      .charge-row{display:flex;align-items:center;gap:10px;min-height:74px;padding:12px 0;border-bottom:1px solid var(--divider-color)}
      .row-icon{--mdc-icon-size:22px;color:var(--secondary-text-color);flex-shrink:0}.row-text{flex:1;min-width:0}
      .row-title{display:flex;align-items:baseline;justify-content:space-between;gap:8px;font-size:14px;font-weight:500;line-height:23px}
      .row-value{font-size:12px;font-weight:400;color:var(--secondary-text-color);white-space:nowrap}
      .switch{border:0;flex-shrink:0;width:38px;height:23px;padding:3px;border-radius:14px;background:var(--disabled-text-color,#666);margin-left:2px}
      .switch span{display:block;width:17px;height:17px;background:var(--card-background-color,#fff);border-radius:50%}
      .switch.checked{background:var(--primary-color,#03a9f4)}.switch.checked span{transform:translateX(15px)}
      .work-plan{display:flex;align-items:center;gap:10px;width:100%;text-align:left;border:0;background:none;padding:12px 0;font-size:13px}
      .work-plan>span{flex:1}.work-plan ha-icon{--mdc-icon-size:20px;color:var(--secondary-text-color)}
      .work-plan small{display:block;margin-top:4px;font-size:12px;color:var(--secondary-text-color)}
      .alert{display:flex;width:100%;gap:9px;align-items:center;text-align:left;background:none;border:0;padding:10px 0;min-height:52px}
      .alert>ha-icon{color:var(--warning-color,#ffb74d);--mdc-icon-size:21px}.alert>span{flex:1}.alert strong{font-size:13px;font-weight:500}
      .alert-detail{display:block;font-size:12px;line-height:18px;color:var(--secondary-text-color)}.alert .chevron{color:var(--secondary-text-color);--mdc-icon-size:18px}
      .quiet{display:flex;align-items:center;gap:7px;font-size:12px;color:var(--secondary-text-color);min-height:38px}.quiet ha-icon{--mdc-icon-size:16px}
      .footer{display:flex;justify-content:space-between;gap:8px;padding-top:4px}.link{display:flex;align-items:center;gap:6px;border:0;background:none;font-size:12px;padding:8px 0;min-height:36px;color:var(--secondary-text-color)}
      .link ha-icon{--mdc-icon-size:17px}.error{font-size:12px;color:var(--error-color,#ef5350);padding-top:10px}
      @media(max-width:350px){ha-card{padding:14px}.status-title{font-size:16px}.price{font-size:11px}.row-value{display:none}}
    </style><ha-card>
      <div class="status"><ha-icon class="status-icon" icon="mdi:${boiler ? "water-boiler" : status === "cool" ? "snowflake" : "radiator"}"></ha-icon>
        <div class="status-text"><div class="status-title">${statusLabel}</div><div class="secondary">${flow}</div></div>
        <div class="price">€ ${this._number(this._value("sensor.elektriciteitsprijs_actief"), 3)}/kWh</div></div>
      <div class="modes" role="group" aria-label="Warmtepompmodus">${modes.map(([value, label]) =>
        `<button class="mode ${mode === value ? "selected" : ""}" aria-pressed="${mode === value}" data-action="mode" data-option="${value}" ${this._pending || !this._available("select.thermozona_heat_pump_mode") ? "disabled" : ""}>${label}</button>`
      ).join("")}</div>
      <div class="charges">${this._row("house", "Huis laden", "mdi:home-thermometer-outline")}${this._row("boiler", "Boiler laden", "mdi:water-boiler")}</div>
      ${this._workSchedule()}
      ${this._alerts()}
      <div class="footer"><button class="link" data-action="boiler"><ha-icon icon="mdi:water-boiler"></ha-icon>Boiler bekijken</button>
        <button class="link" data-action="settings"><ha-icon icon="mdi:tune-variant"></ha-icon>Instellingen</button></div>
      ${this._error ? `<div class="error" role="alert">${this._escape(this._error)}</div>` : ""}
    </ha-card>`;
  }
  async _click(event) {
    const button = event.target.closest?.("button[data-action]");
    if (!button || button.disabled || !this._hass) return;
    const action = button.dataset.action;
    if (action === "info") {
      this.dispatchEvent(new CustomEvent("hass-more-info", { bubbles: true, composed: true, detail: { entityId: button.dataset.entity } }));
      return;
    }
    if (action === "boiler" || action === "settings" || action === "work-plan") {
      const path = action === "work-plan" ? "/config/helpers" : action === "boiler" ? "/dashboard-vloerverwarming/boiler" : "/dashboard-vloerverwarming/detail";
      history.pushState(null, "", path);
      window.dispatchEvent(new Event("location-changed"));
      return;
    }
    const entity = action === "mode" ? "select.thermozona_heat_pump_mode" : button.dataset.entity;
    const option = action === "mode" ? button.dataset.option : this._value(entity) === "auto" ? "off" : "auto";
    this._pending = true;
    this._error = "";
    this._render();
    try {
      await this._hass.callService("select", "select_option", { entity_id: entity, option });
    } catch (_) {
      this._error = "Instelling niet gewijzigd. Probeer het opnieuw.";
    } finally {
      this._pending = false;
      this._render();
    }
  }
}

if (!customElements.get("thermozona-control-card")) {
  customElements.define("thermozona-control-card", ThermozonaControlCard);
}
window.customCards = window.customCards || [];
window.customCards.push({ type: "thermozona-control-card", name: "Thermozona", description: "Compacte bediening en laadplanning" });
