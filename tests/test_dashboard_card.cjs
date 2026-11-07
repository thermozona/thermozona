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

test("planning button opens an editable week directly, including renamed helpers", async () => {
  const { card } = fixture({ renamed: true });
  await card._click({ target: { closest: () => ({ dataset: { action: "work-plan" } }) } });
  assert.equal(card._plannerOpen, true);
  assert.equal(card._plannerDraft.friday[0].from, "09:00:00");
  assert.equal(card._plannerLoading, false);
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
