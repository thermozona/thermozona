/* Run with: node --test tests/test_dashboard_card.cjs */
const { readFileSync } = require("node:fs");
const { runInNewContext } = require("node:vm");
const { test } = require("node:test");
const assert = require("node:assert/strict");

let Card;
runInNewContext(readFileSync("docs/dashboard/thermozona-control-card.js", "utf8"), {
  HTMLElement: class {}, window: {},
  customElements: { get: () => undefined, define: (_, cls) => { Card = cls; } },
});
const days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"];
const clone = value => JSON.parse(JSON.stringify(value));

function fixture({ renamed = false } = {}) {
  const schedule = { id: "office", name: "Office", ...Object.fromEntries(days.map(day => [day, []])) };
  schedule.friday = [{ from: "09:00:00", to: "12:00:00", data: { note: "keep me" } }];
  const card = new Card();
  const writes = [];
  card._render = () => {};
  card._hass = {
    states: { "climate.thermozona_office": { entity_id: "climate.thermozona_office", state: "auto",
      attributes: { setback_schedule: renamed ? "schedule.renamed_office" : "schedule.office" } } },
    callWS: async message => {
      if (message.type === "schedule/list") return [clone(schedule)];
      if (message.type === "config/entity_registry/get") return { unique_id: "office" };
      if (message.type === "schedule/update") {
        assert.equal(message.name, schedule.name, "Native HA update requires the helper name");
        writes.push(clone(message));
        for (const day of days) schedule[day] = clone(message[day]);
        return {};
      }
      throw new Error("Unexpected request");
    },
    callService: () => { throw new Error("Planner must not call device services"); },
  };
  return { card, schedule, writes };
}

test("boiler row distinguishes failed and unconfirmed requests from heating", () => {
  const { card } = fixture();
  card._hass.states["select.thermozona_hot_water"] = { state: "auto" };
  for (const [phase, expected] of [
    ["waiting_for_start", "Wacht op start warmtepomp"],
    ["start_failed", "Boiler niet gestart · laadvraag gestopt"],
    ["waiting_for_start_threshold", "Tank nog te warm om bij te laden"],
    ["heating", "Boiler wordt opgewarmd"],
  ]) {
    card._hass.states["sensor.thermozona_hot_water"] = { state: phase, attributes: {
      charge_start: "2026-11-11T02:30:00Z", charge_end: "2026-11-11T03:30:00Z",
      target_temperature: 55, temperature: 49.4,
    } };
    assert.ok(card._row("boiler", "Boiler laden", "mdi:water-boiler").includes(expected));
  }
});

for (const hassFirst of [false, true]) {
  test(`dedicated schedule view opens directly with hass ${hassFirst ? "before" : "after"} config`, async () => {
    const { card } = fixture();
    const hass = card._hass;
    card._hass = undefined;
    card.shadowRoot = { addEventListener() {} };
    if (hassFirst) card.hass = hass;
    card.setConfig({ view: "planner" });
    if (!hassFirst) card.hass = hass;
    await new Promise(setImmediate);
    assert.equal(card._plannerOpen, true);
    assert.equal(card._plannerDraft.friday[0].from, "09:00:00");
    assert.doesNotMatch(card._plannerHTML(), /data-action="planner-close"/);
    assert.match(card._plannerHTML(), /data-action="planner-reload"/);

    const draft = card._plannerDraft;
    draft.monday = [{ from: "08:00:00", to: "18:00:00" }];
    card._plannerDirty = true;
    card._hass.callWS = () => { throw new Error("Live updates must not reload the editor"); };
    card.hass = { ...hass };
    assert.equal(card._plannerDraft, draft);
    assert.equal(card._plannerDirty, true);
  });
}

test("dedicated schedule view loads rooms when Home Assistant states arrive later", async () => {
  const { card } = fixture();
  const hass = card._hass;
  card._hass = undefined;
  card.shadowRoot = { addEventListener() {} };
  card.setConfig({ view: "planner" });
  card.hass = { ...hass, states: {} };
  assert.match(card._plannerError, /geen kamerschema/);
  card.hass = hass;
  await new Promise(setImmediate);
  assert.equal(card._plannerRoom, "climate.thermozona_office");
  assert.equal(card._plannerDraft.friday[0].from, "09:00:00");
  assert.equal(card._plannerError, "");
});

test("planning button opens an editable week directly, including renamed helpers", async () => {
  const { card } = fixture({ renamed: true });
  await card._click({ target: { closest: () => ({ dataset: { action: "work-plan" } }) } });
  assert.equal(card._plannerOpen, true);
  assert.equal(card._plannerDraft.friday[0].from, "09:00:00");
  assert.equal(card._plannerLoading, false);
  assert.match(card._plannerHTML(), /data-action="planner-close"/);
});

test("saving changes one room and preserves other days, custom data and midnight", async () => {
  const { card, schedule, writes } = fixture();
  await card._openPlanner();
  card._plannerDraft.monday = [{ from: "08:30:00", to: "24:00:00" }];
  await card._savePlanner();
  assert.equal(writes.length, 1);
  assert.equal(writes[0].schedule_id, "office");
  assert.equal(schedule.monday[0].to, "24:00:00");
  assert.equal(schedule.friday[0].data.note, "keep me");
  assert.equal(card._plannerMessage, "Weekschema opgeslagen.");
});

test("concurrent schedule changes are preserved and draft remains available", async () => {
  const { card, schedule, writes } = fixture();
  await card._openPlanner();
  card._plannerDraft.monday = [{ from: "08:00:00", to: "18:00:00" }];
  schedule.tuesday = [{ from: "10:00:00", to: "11:00:00" }];
  await card._savePlanner();
  assert.equal(writes.length, 0);
  assert.match(card._plannerError, /elders gewijzigd/);
  assert.equal(card._plannerDraft.monday.length, 1);
  assert.equal(schedule.tuesday.length, 1);
});

test("overlapping and overnight blocks do not reach Home Assistant", async () => {
  const { card, writes } = fixture();
  await card._openPlanner();
  card._plannerDraft.monday = [{ from: "08:00:00", to: "18:00:00" }, { from: "17:00:00", to: "19:00:00" }];
  await card._savePlanner();
  assert.match(card._plannerError, /overlappen/);
  card._plannerDraft.monday = [{ from: "22:00:00", to: "08:00:00" }];
  await card._savePlanner();
  assert.match(card._plannerError, /twee dagen/);
  assert.equal(writes.length, 0);
});

test("failed writes retain the draft and do not show success", async () => {
  const { card } = fixture();
  await card._openPlanner();
  const original = card._hass.callWS;
  card._hass.callWS = message => {
    if (message.type === "schedule/update") throw new Error("offline");
    return original(message);
  };
  card._plannerDraft.monday = [{ from: "08:00:00", to: "18:00:00" }];
  await card._savePlanner();
  assert.match(card._plannerError, /niet bevestigd/);
  assert.equal(card._plannerDraft.monday.length, 1);
  assert.equal(card._plannerMessage, "");
});
