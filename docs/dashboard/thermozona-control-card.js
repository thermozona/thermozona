/* Compact Lovelace controls; uses Home Assistant and the browser only. */
class ThermozonaControlCard extends HTMLElement {
  setConfig(config) {
    this._config = config;
    if (!this.shadowRoot) {
      this.attachShadow({ mode: "open" });
      this.shadowRoot.addEventListener("click", (event) => this._click(event));
      this.shadowRoot.addEventListener("input", (event) => this._plannerInput(event));
      this.shadowRoot.addEventListener("change", (event) => this._plannerInput(event));
    }
    if (config.view === "planner" && this._hass && !this._plannerOpen) {
      this._openPlanner();
      return;
    }
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (this._config?.view === "planner" && !this._plannerOpen) {
      this._openPlanner();
      return;
    }
    // Keep an open editor stable while live temperatures and diagnostics update.
    if (this._plannerOpen) {
      if (!this._plannerRoom && this._rooms().length) this._openPlanner();
      return;
    }
    const entities = Object.entries(hass.states).filter(([id]) =>
      id.includes("thermozona") || id === "sensor.elektriciteitsprijs_actief" ||
      id.startsWith("schedule.") ||
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
      const boilerPhase = boiler && ({
        requested: "Boilerstart aangevraagd",
        waiting_for_start: "Wacht op start warmtepomp",
        heating: "Boiler wordt opgewarmd",
        start_failed: "Boiler niet gestart · laadvraag gestopt",
        waiting_for_start_threshold: "Tank nog te warm om bij te laden",
        start_conditions_unavailable: "Startgegevens ontbreken",
      })[phase];
      const plan = boilerPhase || (phase === "learning_usage" ? "Leert je warmwatergebruik" :
        !attrs.charge_start ? "Geen voordelig laadmoment" : this._window(attrs));
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
    if (!this._rooms().length) return "";
    return `<button class="work-plan" data-action="work-plan"><ha-icon icon="mdi:calendar-week"></ha-icon>
      <span>Kamers plannen<small>Weekschema per ruimte aanpassen</small></span><ha-icon icon="mdi:chevron-right"></ha-icon></button>`;
  }
  _rooms() {
    return Object.values(this._hass?.states || {}).filter((s) =>
      s.entity_id?.startsWith("climate.thermozona_") && s.attributes?.setback_schedule
    ).sort((a, b) => (a.attributes.friendly_name || a.entity_id).localeCompare(b.attributes.friendly_name || b.entity_id, "nl"));
  }
  _weekdays() {
    return [["monday", "Maandag", "Ma"], ["tuesday", "Dinsdag", "Di"], ["wednesday", "Woensdag", "Wo"],
      ["thursday", "Donderdag", "Do"], ["friday", "Vrijdag", "Vr"], ["saturday", "Zaterdag", "Za"], ["sunday", "Zondag", "Zo"]];
  }
  _week(schedule) {
    return Object.fromEntries(this._weekdays().map(([day]) => [day, JSON.parse(JSON.stringify(schedule[day] || []))]));
  }
  _time(value, end = false) {
    if (end && (value?.startsWith("24:") || value === "23:59:59.999999")) return "24:00:00";
    return value?.length === 5 ? `${value}:00` : value;
  }
  _fingerprint(week) {
    return JSON.stringify(this._weekdays().map(([day]) => (week[day] || []).map((block) => ({
      from: this._time(block.from), to: this._time(block.to, true), data: block.data || {},
    }))));
  }
  async _openPlanner() {
    this._plannerOpen = true;
    this._plannerDirty = false;
    const rooms = this._rooms();
    const room = rooms.find((s) => s.entity_id === this._plannerRoom) ||
      rooms.find((s) => s.entity_id.includes("werkkamer")) || rooms[0];
    if (!room) { this._plannerError = "Er zijn nog geen kamerschema’s gekoppeld."; this._render(); return; }
    await this._loadPlannerRoom(room.entity_id);
  }
  async _loadPlannerRoom(entityId) {
    if (this._plannerDirty) {
      this._plannerError = "Sla eerst op of annuleer je wijzigingen voordat je een andere kamer kiest.";
      this._render();
      return;
    }
    const request = this._plannerRequest = (this._plannerRequest || 0) + 1;
    this._plannerRoom = entityId;
    this._plannerLoading = true;
    this._plannerError = "";
    this._plannerMessage = "";
    this._plannerDraft = null;
    this._render();
    try {
      const entity = this._attrs(entityId).setback_schedule;
      const schedules = await this._hass.callWS({ type: "schedule/list" });
      let schedule = schedules.find((s) => `schedule.${s.id}` === entity);
      if (!schedule) {
        const entry = await this._hass.callWS({ type: "config/entity_registry/get", entity_id: entity });
        schedule = schedules.find((s) => s.id === entry.unique_id);
      }
      if (!schedule) throw new Error("missing_schedule");
      if (!this._plannerOpen || request !== this._plannerRequest) return;
      this._plannerScheduleId = schedule.id;
      this._plannerDraft = this._week(schedule);
      this._plannerOriginal = this._fingerprint(this._plannerDraft);
    } catch (_) {
      if (request === this._plannerRequest) this._plannerError = "Het schema kon niet worden geladen. Probeer opnieuw.";
    } finally {
      if (request === this._plannerRequest) { this._plannerLoading = false; this._render(); }
    }
  }
  _plannerInput(event) {
    const field = event.target;
    if (field.dataset?.plannerRoom !== undefined && event.type === "change") {
      this._loadPlannerRoom(field.value);
      return;
    }
    if (!this._plannerDraft || !field.dataset?.day || !["from", "to"].includes(field.dataset.edge)) return;
    const block = this._plannerDraft[field.dataset.day]?.[Number(field.dataset.index)];
    if (!block) return;
    block[field.dataset.edge] = field.dataset.edge === "to" && field.value === "00:00" ? "24:00:00" :
      field.value ? `${field.value}:00` : "";
    this._plannerDirty = true;
    this._plannerError = "";
    this._plannerMessage = "";
    const save = this.shadowRoot.querySelector("#planner-save");
    if (save) save.disabled = false;
    const message = this.shadowRoot.querySelector(".planner-message");
    if (message) message.textContent = "";
  }
  async _savePlanner() {
    this._plannerError = "";
    const week = this._week(this._plannerDraft);
    for (const [day, label] of this._weekdays()) {
      const minutes = (value, end = false) => {
        const match = /^(\d{2}):(\d{2})(?::\d{2}(?:\.\d+)?)?$/.exec(value || "");
        if (!match || Number(match[2]) > 59 || Number(match[1]) > (end ? 24 : 23)) return NaN;
        return Number(match[1]) * 60 + Number(match[2]);
      };
      week[day].sort((a, b) => minutes(a.from) - minutes(b.from));
      let previous = -1;
      for (const block of week[day]) {
        const from = minutes(block.from), to = minutes(this._time(block.to, true), true);
        if (!Number.isFinite(from) || !Number.isFinite(to) || to > 1440 || from >= to) {
          this._plannerError = `${label}: kies een begintijd vóór de eindtijd. Verdeel een nachtperiode over twee dagen.`;
          this._render(); return;
        }
        if (from < previous) { this._plannerError = `${label}: de tijdblokken overlappen.`; this._render(); return; }
        previous = to;
      }
    }
    this._plannerSaving = true;
    this._render();
    try {
      const schedules = await this._hass.callWS({ type: "schedule/list" });
      const current = schedules.find((s) => s.id === this._plannerScheduleId);
      const desired = this._fingerprint(week);
      const existing = current && this._fingerprint(this._week(current));
      if (existing !== this._plannerOriginal && existing !== desired) {
        this._plannerError = "Dit schema is elders gewijzigd. Laad het opnieuw voordat je opslaat.";
        return;
      }
      if (existing !== desired) await this._hass.callWS({ type: "schedule/update",
        schedule_id: this._plannerScheduleId, name: current.name,
        ...(current.icon ? { icon: current.icon } : {}), ...week });
      const verified = (await this._hass.callWS({ type: "schedule/list" })).find((s) => s.id === this._plannerScheduleId);
      if (!verified || this._fingerprint(this._week(verified)) !== desired) throw new Error("verification_failed");
      this._plannerDraft = this._week(verified);
      this._plannerOriginal = desired;
      this._plannerDirty = false;
      this._plannerMessage = "Weekschema opgeslagen.";
    } catch (error) {
      this._plannerError = error.code === "unauthorized" ? "Je hebt beheerrechten nodig om schema’s aan te passen." :
        "Opslaan is niet bevestigd. Je wijzigingen staan nog in de editor; probeer opnieuw.";
    } finally { this._plannerSaving = false; this._render(); }
  }
  _plannerHTML() {
    const rooms = this._rooms(), attrs = this._attrs(this._plannerRoom);
    const disabled = this._plannerLoading || this._plannerSaving;
    const form = this._plannerDraft && this._weekdays().map(([day, label, short]) => {
      const blocks = this._plannerDraft[day];
      return `<div class="day"><span class="day-label">${short}</span><div class="day-times">
        ${blocks.map((block, index) => `<div class="time-block">
          <input type="time" step="60" value="${this._escape(block.from?.slice(0, 5))}" aria-label="${label} begintijd ${index + 1}"
            data-day="${day}" data-index="${index}" data-edge="from" ${disabled ? "disabled" : ""}>
          <span>tot</span><input type="time" step="60" value="${this._escape(this._time(block.to, true)?.startsWith("24:") ? "00:00" : block.to?.slice(0, 5))}"
            aria-label="${label} eindtijd ${index + 1}" data-day="${day}" data-index="${index}" data-edge="to" ${disabled ? "disabled" : ""}>
          <button class="remove" data-action="planner-remove" data-day="${day}" data-index="${index}" aria-label="${label} tijdblok ${index + 1} verwijderen" ${disabled ? "disabled" : ""}>×</button>
        </div>`).join("")}
        <button class="add" data-action="planner-add" data-day="${day}" ${disabled ? "disabled" : ""}>${blocks.length ? "+ Nog een blok" : "+ Tijdblok"}</button>
      </div></div>`;
    }).join("");
    return `<style>
      :host{display:block;color:var(--primary-text-color);font-family:var(--primary-font-family,Roboto,sans-serif)}
      *{box-sizing:border-box}ha-card{display:block;padding:18px;border-radius:var(--ha-card-border-radius,16px)}
      button,input,select{font:inherit;color:inherit}button{cursor:pointer}button:disabled{opacity:.5;cursor:wait}
      .heading{display:flex;justify-content:space-between;align-items:center;gap:12px}h2{font-size:18px;margin:0;font-weight:600}
      .close,.add,.remove,.reload{border:0;background:none;color:var(--primary-color,#03a9f4);padding:8px;min-height:36px}
      .close{font-size:12px}.intro,.hint{font-size:12px;line-height:19px;color:var(--secondary-text-color)}
      label{display:block;font-size:13px;margin-top:14px}select{display:block;width:100%;margin:6px 0 14px;padding:10px;background:var(--secondary-background-color);border:1px solid var(--divider-color);border-radius:6px;font-size:14px}
      .day{display:flex;gap:10px;padding:9px 0;border-top:1px solid var(--divider-color)}.day-label{flex:0 0 24px;font-size:13px;padding-top:9px;font-weight:500}
      .day-times{min-width:0;flex:1}.time-block{display:flex;gap:5px;align-items:center;margin-bottom:3px}.time-block>span{font-size:11px;color:var(--secondary-text-color)}
      input{min-width:0;width:0;flex:1;font-size:16px;padding:6px 2px;background:var(--secondary-background-color);border:1px solid var(--divider-color);border-radius:5px;color-scheme:dark light}
      .remove{flex:0 0 25px;padding:2px;font-size:22px;color:var(--secondary-text-color)}.add{font-size:12px;padding:2px 0;min-height:27px}
      .actions{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-top:14px}.save{padding:10px 18px;border:0;background:var(--primary-color,#03a9f4);color:var(--text-primary-color,#fff);border-radius:6px;min-height:40px}
      .planner-message{font-size:12px;line-height:18px;margin-top:10px;color:var(--primary-color,#03a9f4)}.planner-message.error{color:var(--error-color,#ef5350)}
      button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--primary-color,#03a9f4);outline-offset:2px}
    </style><ha-card>
      <div class="heading"><h2>Ruimteplanning</h2>${this._config?.view === "planner" ? "" : `<button class="close" data-action="planner-close" ${this._plannerSaving ? "disabled" : ""}>${this._plannerDirty ? "Annuleren" : "Sluiten"}</button>`}</div>
      <p class="intro">Kies de uren waarop een kamer ${this._number(attrs.setback_c ?? 1)} °C koeler mag zijn. Thermozona plant de normale temperatuur voor het einde van zo’n periode.</p>
      <label>Kamer<select data-planner-room ${disabled ? "disabled" : ""}>${rooms.map((s) => `<option value="${s.entity_id}" ${s.entity_id === this._plannerRoom ? "selected" : ""}>${this._escape((s.attributes.friendly_name || s.entity_id).replace(/^Thermozona\s+/i, ""))}</option>`).join("")}</select></label>
      ${this._value(this._plannerRoom) === "off" ? '<p class="hint">Deze kamer staat uit. Het schema zet de kamer niet aan.</p>' : ""}
      ${attrs.recovery_enabled ? '<p class="hint">Thermozona begint zo nodig eerder met opwarmen. De voorlooptijd leert mee met deze kamer.</p>' : ""}
      ${this._plannerLoading ? '<p class="hint">Schema laden…</p>' : form || ""}
      <div class="planner-message ${this._plannerError ? "error" : ""}" role="status">${this._escape(this._plannerError || this._plannerMessage)}</div>
      <div class="actions"><button class="reload" data-action="planner-reload" ${disabled ? "disabled" : ""}>${this._plannerDirty ? "Wijzigingen verwerpen" : "Opnieuw laden"}</button><button id="planner-save" class="save" data-action="planner-save" ${disabled || !this._plannerDraft || !this._plannerDirty ? "disabled" : ""}>Opslaan</button></div>
      <p class="hint">Eindtijd 00:00 betekent het einde van die dag. Lege dagen houden de normale temperatuur.</p>
    </ha-card>`;
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
    if (this._plannerOpen) { this.shadowRoot.innerHTML = this._plannerHTML(); return; }
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
      .footer{display:flex;justify-content:space-between;gap:8px;padding-top:4px}.link{display:flex;align-items:center;gap:6px;border:0;background:none;font-size:12px;padding:8px 0;min-height:36px;color:var(--secondary-text-color);text-decoration:none}
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
      <div class="footer"><a class="link" href="/dashboard-vloerverwarming/boiler"><ha-icon icon="mdi:water-boiler"></ha-icon>Boiler bekijken</a>
        <a class="link" href="/dashboard-vloerverwarming/detail"><ha-icon icon="mdi:tune-variant"></ha-icon>Instellingen</a></div>
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
    if (action === "work-plan") { await this._openPlanner(); return; }
    if (action === "planner-close") { this._plannerOpen = false; this._plannerRequest++; this._render(); return; }
    if (action === "planner-save") { await this._savePlanner(); return; }
    if (action === "planner-reload") { this._plannerDirty = false; await this._loadPlannerRoom(this._plannerRoom); return; }
    if (action === "planner-add" || action === "planner-remove") {
      const blocks = this._plannerDraft?.[button.dataset.day];
      if (!blocks) return;
      if (action === "planner-add") blocks.push({ from: "08:00:00", to: "18:00:00" });
      else blocks.splice(Number(button.dataset.index), 1);
      this._plannerDirty = true;
      this._plannerError = "";
      this._plannerMessage = "";
      this._render();
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
