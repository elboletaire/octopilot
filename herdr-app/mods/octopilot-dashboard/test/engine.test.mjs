import { test } from "node:test";
import assert from "node:assert/strict";
import { engineLabel } from "../model.js";

// The view was written at epoch 1000; "now" is 5 s later.
const view = { generated_at: 1000 };
const now = 1005_000;

test("an engine on and idle or scanning reads as engine on", () => {
  for (const state of ["idle", "scanning"]) {
    assert.deepEqual(engineLabel({ mode: "on", state, since_s: null, last: null }, view, now),
      { text: "engine on", tone: "dim" });
  }
});

test("an acting engine shows how long it has acted, counted from the view's age", () => {
  const label = engineLabel({ mode: "on", state: "acting", since_s: 90, last: null }, view, now);
  assert.deepEqual(label, { text: "⏭ acting 1m", tone: "working" });
  assert.equal(engineLabel({ mode: "on", state: "acting", since_s: 30, last: null }, view, now).text,
    "⏭ acting 35s");
});

test("a plan or off engine says so, whatever its state", () => {
  assert.equal(engineLabel({ mode: "plan", state: "idle", since_s: null, last: null }, view, now).text, "engine plan");
  assert.equal(engineLabel({ mode: "off", state: "off", since_s: null, last: null }, view, now).text, "engine off");
});

test("a hung engine is red and says how long it has hung", () => {
  const label = engineLabel({ mode: "on", state: "stale", since_s: 600, last: null }, view, now);
  assert.deepEqual(label, { text: "engine hung? 10m", tone: "blocked" });
});

test("a view with no engine block has no engine label", () => {
  assert.equal(engineLabel(undefined, view, now), null);
  assert.equal(engineLabel(null, view, now), null);
});
