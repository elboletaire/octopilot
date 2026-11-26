import { test } from "node:test";
import assert from "node:assert/strict";
import { remoteSteps, isTabId } from "../model.js";

test("focus on a tab session selects its space, then the tab, then the agent", () => {
  const entry = { session: "tb4a71", space: "sp-oct", pane: "t7", backend: "hc" };
  assert.deepEqual(remoteSteps("focus", entry, "bolt"), [
    { cmd: "space.select", args: { id: "bolt/sp-oct" } },
    { cmd: "tab.select", args: { id: "bolt/tb4a71" } },
    { cmd: "agent.focus", args: { terminal: "bolt/t7" } },
  ]);
});

test("focus on a w…:t… tab session also selects the tab", () => {
  const entry = { session: "w8X:t2", space: "sp-oct", pane: null };
  assert.deepEqual(remoteSteps("focus", entry, "bolt"), [
    { cmd: "space.select", args: { id: "bolt/sp-oct" } },
    { cmd: "tab.select", args: { id: "bolt/w8X:t2" } },
  ]);
});

test("focus on an old-style session selects the session as its space, no tab", () => {
  const entry = { session: "sp-3f1a", space: "sp-3f1a", pane: "t12" };
  assert.deepEqual(remoteSteps("focus", entry, "bolt"), [
    { cmd: "space.select", args: { id: "bolt/sp-3f1a" } },
    { cmd: "agent.focus", args: { terminal: "bolt/t12" } },
  ]);
});

test("a missing space falls back to the session for an old-style session", () => {
  const entry = { session: "sp-3f1a", pane: null, path: "/home/u/wt" };
  assert.deepEqual(remoteSteps("focus", entry, "bolt"), [{ cmd: "space.select", args: { id: "bolt/sp-3f1a" } }]);
  assert.deepEqual(remoteSteps("shell", entry, "bolt"), [
    { cmd: "tab.new", args: { space: "bolt/sp-3f1a", folder: "/home/u/wt" } },
  ]);
});

test("a tab session with no space has no space to address", () => {
  const entry = { session: "tb4a71", space: null, pane: "t7", path: "/home/u/wt" };
  assert.deepEqual(remoteSteps("focus", entry, "bolt"), []);
  assert.deepEqual(remoteSteps("shell", entry, "bolt"), []);
});

test("no session and no space gives no steps", () => {
  assert.deepEqual(remoteSteps("focus", { session: null, space: null }, "bolt"), []);
});

test("the remote prefix goes on every id, and a bare terminal is addressed on the machine", () => {
  const entry = { session: "tb1", space: "sp-oct", pane: "t3" };
  const steps = remoteSteps("focus", entry, "bolt");
  assert.deepEqual(steps.map((s) => s.args.id ?? s.args.terminal), ["bolt/sp-oct", "bolt/tb1", "bolt/t3"]);
  assert.deepEqual(remoteSteps("focus", { session: "tb1", space: "sp-oct", pane: "bolt/t3" }, "bolt").at(-1), {
    cmd: "agent.focus", args: { terminal: "bolt/t3" },
  });
});

test("shell opens a tab in the row's space in its folder", () => {
  const entry = { session: "tb4a71", space: "sp-oct", path: "/home/u/wt" };
  assert.deepEqual(remoteSteps("shell", entry, "bolt"), [
    { cmd: "tab.new", args: { space: "bolt/sp-oct", folder: "/home/u/wt" } },
  ]);
});

test("isTabId tells a tab id from a space or workspace id", () => {
  assert.equal(isTabId("tb4a71"), true);
  assert.equal(isTabId("w8X:t2"), true);
  assert.equal(isTabId("sp-3f1a"), false);
  assert.equal(isTabId("w8X"), false);
  assert.equal(isTabId(null), false);
});
