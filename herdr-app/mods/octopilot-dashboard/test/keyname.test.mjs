import { test } from "node:test";
import assert from "node:assert/strict";
import { keyName } from "../model.js";

test("Shift+Backspace names the Delete action", () => {
  assert.equal(keyName({ key: "backspace", modifiers: { shift: true } }), "delete");
});

test("plain Backspace keeps its own name", () => {
  assert.equal(keyName({ key: "backspace", modifiers: {} }), "backspace");
  assert.equal(keyName({ key: "backspace" }), "backspace");
});

test("Delete names the Delete action", () => {
  assert.equal(keyName({ key: "delete", modifiers: {} }), "delete");
});

test("chords with ctrl, alt or cmd still belong to Herdr", () => {
  assert.equal(keyName({ key: "backspace", modifiers: { shift: true, control: true } }), null);
});
