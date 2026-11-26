import { test } from "node:test";
import assert from "node:assert/strict";
import { parseAnswer, confirmArgs } from "../model.js";

test("a held answer keeps the holds it names", () => {
  const answer = parseAnswer('{"ok": false, "kitchen": true, "holds": ["wind-down", "cap"], "message": "held"}');
  assert.deepEqual(answer, { ok: false, kitchen: true, message: "held", holds: ["wind-down", "cap"] });
});

test("an answer without holds has none", () => {
  assert.equal("holds" in parseAnswer('{"ok": true, "message": "queued"}'), false);
});

test("the second press sends the holds the alert showed", () => {
  assert.deepEqual(confirmArgs({ repo: "owner/repo" }, ["wind-down", "cap"]),
    { repo: "owner/repo", confirmed: 1, holds: "wind-down,cap" });
});

test("an older dashboard gets confirmed=1 alone", () => {
  assert.deepEqual(confirmArgs(undefined, undefined), { confirmed: 1 });
  assert.deepEqual(confirmArgs({ repo: "owner/repo" }, undefined), { repo: "owner/repo", confirmed: 1 });
});
