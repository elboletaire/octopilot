import { test } from "node:test";
import assert from "node:assert/strict";
import { arrange, resolveSel, engineBadge, holdLabel } from "../model.js";

const card = (key, extra = {}) => ({ key, open: true, ...extra });
const view = {
  columns: [
    { id: "done", entries: [card("d1", { open: false }), card("d2", { open: false })], hidden: 2 },
    { id: "orch", entries: [card("o1")] },
    { id: "hold", entries: [card("h1")] },
    { id: "needs", entries: [card("n1")] },
    { id: "running", entries: [card("r1")] },
  ],
  pick: [],
};
const none = { showDone: false, expanded: new Set() };

test("the board shows running, needs, hold and done in that order, whatever order view.json lists them", () => {
  const arr = arrange(view, none);
  assert.deepEqual(arr.columns.map((c) => c.id), ["running", "needs", "hold", "done"]);
});

test("a column with an unknown id is skipped, its cards are not selectable", () => {
  const arr = arrange(view, none);
  assert.ok(!arr.columns.some((c) => c.id === "orch"));
  assert.ok(!arr.selectable.includes("o1"));
  assert.deepEqual(arr.selectable, ["r1", "n1", "h1"]);
});

test("a view with no hold column still draws the other columns", () => {
  const arr = arrange({ ...view, columns: view.columns.filter((c) => c.id !== "hold") }, none);
  assert.deepEqual(arr.columns.map((c) => c.id), ["running", "needs", "done"]);
});

test("Done shows no card and says how many are hidden until D toggles it", () => {
  const hidden = arrange(view, none).columns.find((c) => c.id === "done");
  assert.deepEqual(hidden.shown, []);
  assert.equal(hidden.hidden, 2);
  const shown = arrange(view, { showDone: true, expanded: new Set() }).columns.find((c) => c.id === "done");
  assert.deepEqual(shown.shown.map((e) => e.key), ["d1", "d2"]);
  assert.equal(shown.hidden, 0);
});

test("a hidden Done card is never the cursor's row", () => {
  const arr = arrange(view, none);
  assert.equal(resolveSel("d1", arr), "r1");
});

test("the engine badge says paused while paused, else the engine's label", () => {
  assert.equal(engineBadge({ action: "cleanup", label: "cleaning up", paused: false }), "⏭ cleaning up");
  assert.equal(engineBadge({ action: "review", label: "starting review", paused: true }), "⏭ held: paused");
  assert.equal(engineBadge(null), null);
  assert.equal(engineBadge(undefined), null);
});

test("a hold reads as who holds the card and why", () => {
  assert.equal(holdLabel({ by: "you", reason: "waiting on the schema" }), "on hold: you — waiting on the schema");
  assert.equal(holdLabel({ by: "reviewer", reason: "" }), "on hold: reviewer");
  assert.equal(holdLabel({ by: "author", reason: "new commits acknowledged -> waiting on re-request" }),
    "on hold: author — new commits acknowledged -> waiting on re-request");
  assert.equal(holdLabel(null), null);
});
