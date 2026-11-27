// The dashboard's view model: view.json arranged the way the board and the
// Pick next tree show it. No Herdr calls here, so the keys and cards read the
// same numbers the TUI does.

import { addressOn } from "./machine.js";

export const REPO_PREFIX = "repo:";
// A second press of the same action within this window confirms it (CONFIRM_SECS).
export const CONFIRM_MS = 4000;
export const FLASH_MS = 6000;
// A view.json older than this has no headless dashboard writing it (CONTRACT.md).
export const STALE_S = 15;
// The kitchen-closed dialog cancels itself after this long (KITCHEN_SECS).
export const KITCHEN_S = 15;
// Global keys, and the view.json global action ids they stand for.
export const GLOBAL_KEYS = {
  r: ["refresh"], P: ["pause", "resume"], i: ["investigate"], a: ["prompter"],
};
// Keys the selected row's own actions answer to: with a repo header selected
// they say so instead of doing nothing.
export const ROW_KEYS = ["v", "t", "o", "g", "n", "R", "e", "x", "z", "d", "delete", "enter"];

export const AGENT_GLYPH = {
  running: ["◐", "working"], waiting: ["⏸", "blocked"], idle: ["○", "dim"],
  stopped: ["■", "dim"], error: ["✗", "blocked"],
};
export const CI_GLYPH = {
  pass: ["✓", "done"], fail: ["✗", "blocked"], pending: ["⏳", "working"], none: ["–", "dim"],
};

export const repoKey = (slug) => REPO_PREFIX + slug;
export const slugOf = (key) => key.slice(REPO_PREFIX.length);
export const isRepoKey = (key) => Boolean(key) && key.startsWith(REPO_PREFIX);

// dashboard.py's ago(): 12s, 5m, 2h05, 3d.
export function ago(secs) {
  if (secs == null) return "–";
  const s = Math.max(0, Math.floor(secs));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}`;
  return `${Math.floor(s / 86400)}d`;
}

// Seconds since view.json was written, so ages keep counting between writes.
export const elapsed = (view, now) => Math.max(0, now / 1000 - (view.generated_at ?? 0));
export const liveAge = (secs, view, now) => (secs == null ? null : secs + elapsed(view, now));
export const isStale = (view, now) => elapsed(view, now) > STALE_S;

// The header's engine label (CONTRACT.md header.engine), or null for a view with no
// engine block (one written before the header had it, still on disk during a switch).
export function engineLabel(engine, view, now) {
  if (!engine) return null;
  const since = liveAge(engine.since_s, view, now);
  if (engine.state === "stale") return { text: `engine running ${ago(since)}+ (hung?)`, tone: "blocked" };
  if (engine.mode === "off") return { text: "engine off here", tone: "dim" };
  if (engine.state === "acting") return { text: `⏭ acting ${ago(since)}`, tone: "working" };
  if (engine.mode === "plan") return { text: "engine plan (logs only)", tone: "dim" };
  return { text: "engine on", tone: "dim" };
}

// The board's columns in this order, found by id; a column with any other id is skipped
// (CONTRACT.md).
const COLUMN_ORDER = ["running", "needs", "hold", "done"];

// Board columns with the rows they show, and the Pick next tree as the lines it draws, plus
// every key the cursor can stand on, in the order j/k walk them. Done shows none of its entries
// until D toggles it; `hidden` counts them.
export function arrange(view, { showDone, expanded }) {
  const columns = COLUMN_ORDER.flatMap((id) => {
    const c = view.columns.find((x) => x.id === id);
    if (!c) return [];
    if (id !== "done") return [{ ...c, shown: c.entries, hidden: 0 }];
    return [{ ...c, shown: showDone ? c.entries : [], hidden: showDone ? 0 : (c.hidden ?? c.entries.length) }];
  });
  const order = columns.flatMap((c) => c.shown.map((e) => e.key));
  const repoOf = {};
  const tree = [];
  for (const group of view.pick ?? []) {
    const key = repoKey(group.repo);
    const open = expanded.has(group.repo);
    tree.push({ kind: "repo", key, slug: group.repo, entries: group.entries, open });
    order.push(key);
    for (const e of group.entries) repoOf[e.key] = key;
    if (!open) continue;
    group.entries.forEach((e, i) => {
      tree.push({ kind: "row", key: e.key, entry: e, last: i === group.entries.length - 1 });
      order.push(e.key);
    });
  }
  return { columns, tree, repoOf, selectable: [...new Set(order)] };
}

// The cursor stays where it is; a row hidden by its repo's fold falls back to
// that repo's header, and anything gone falls back to the first row.
export function resolveSel(sel, arr) {
  if (arr.selectable.includes(sel)) return sel;
  const header = arr.repoOf[sel];
  if (header && arr.selectable.includes(header)) return header;
  return arr.selectable[0] ?? null;
}

// The engine's next action on a card (CONTRACT.md, Engine badge), or null when none is pending.
export function engineBadge(engine) {
  if (!engine) return null;
  return engine.paused ? "⏭ held: paused" : `⏭ ${engine.label}`;
}

// A card's hold line (CONTRACT.md, hold): who holds it and why, the reason left out when none was given.
export function holdLabel(hold) {
  if (!hold) return null;
  return hold.reason ? `on hold: ${hold.by} — ${hold.reason}` : `on hold: ${hold.by}`;
}

// The pane of a mod when it is the only one in the window, else null (several copies
// cannot be told apart from panes()).
export function soloPane(panes, mod) {
  const mine = panes.filter((p) => p.mod === mod);
  return mine.length === 1 ? mine[0] : null;
}

// This copy's own pane: the one of its mod named for it (open-gui.sh names them), else
// the mod's only pane, else null.
export function ownPane(panes, mod, name) {
  return panes.find((p) => p.mod === mod && p.name === name) ?? soloPane(panes, mod);
}

// The id of the shown tab, from spaces().
export function shownTab(spaces) {
  return spaces.find((s) => s.active)?.tabs.find((t) => t.active)?.id ?? null;
}

// Whether a pane may take the keyboard: nothing has it, or no terminal and no other pane
// does (focused() as herdr reports it).
export function keyboardFree(f, pane) {
  return !f || (f.terminal == null && (f.pane == null || f.pane === pane));
}

export function entryOf(view, key) {
  if (!key || isRepoKey(key)) return null;
  for (const c of view.columns) {
    const e = c.entries.find((x) => x.key === key);
    if (e) return e;
  }
  for (const g of view.pick ?? []) {
    const e = g.entries.find((x) => x.key === key);
    if (e) return e;
  }
  return null;
}

export function stepSel(arr, sel, d) {
  const i = arr.selectable.indexOf(sel);
  const j = Math.max(0, Math.min(arr.selectable.length - 1, (i < 0 ? 0 : i) + d));
  return arr.selectable[j] ?? sel;
}

// h/l between board columns and then the pick tree, skipping empty ones.
export function jumpSel(arr, sel, d) {
  const groups = [...arr.columns.map((c) => c.shown.map((e) => e.key)), arr.tree.map((l) => l.key)];
  const cur = groups.findIndex((g) => g.includes(sel));
  if (cur < 0) return sel;
  let i = cur + d;
  while (i >= 0 && i < groups.length && !groups[i].length) i += d;
  return i >= 0 && i < groups.length ? groups[i][0] : sel;
}

// "2 issue · 1 your PR", in the order the kinds first appear.
export function pickSummary(entries) {
  const counts = new Map();
  for (const e of entries) counts.set(e.pick_kind, (counts.get(e.pick_kind) ?? 0) + 1);
  return [...counts].map(([kind, n]) => `${n} ${kind}`).join(" · ");
}

// The key a keypress names: printable text as typed (so D is shift-d), else
// the key's own name. Chords with ctrl, alt or cmd belong to Herdr.
export function keyName(e) {
  const m = e.modifiers ?? {};
  if (m.control || m.alt || m.platform) return null;
  // Macs have no Delete key: Shift+Backspace is remove's alias.
  if (e.key === "backspace" && m.shift) return "delete";
  const ch = e.key_char;
  if (ch && ch.length === 1 && ch >= " " && ch !== "\x7f") return ch === " " ? "space" : ch;
  return e.key;
}

// Action keys come as "Delete", "Enter" or a single letter.
export const actionKey = (k) => (k.length === 1 ? k : k.toLowerCase());

// Why a parsed view.json can't be drawn, or null when it can.
export function viewError(v) {
  if (!v || typeof v !== "object" || v.v !== 1) return "view.json is not version 1";
  if (!Array.isArray(v.columns)) return "view.json has no columns";
  if (!v.header || typeof v.header !== "object") return "view.json has no header";
  return null;
}

// The shell's JSON answer: the last line of stdout.
export function parseAnswer(stdout) {
  const text = (stdout ?? "").trim();
  const line = text.split("\n").filter(Boolean).pop() ?? "";
  try {
    const j = JSON.parse(line);
    const answer = { ok: Boolean(j.ok), kitchen: Boolean(j.kitchen), message: String(j.message ?? "") };
    if (Array.isArray(j.holds)) answer.holds = j.holds.map(String);
    return answer;
  } catch {
    return { ok: false, kitchen: false, message: text || "no answer from octopilot" };
  }
}

// The --action args of a held start's second press. holds are the names the
// alert showed; without them (an older dashboard) only confirmed=1 is sent.
export function confirmArgs(args, holds) {
  return { ...(args ?? {}), confirmed: 1, ...(holds ? { holds: holds.join(",") } : {}) };
}

// A herdr-app tab id: `tb…`, or `w…:t…` in the TUI's form. Anything else is a space or workspace.
export const isTabId = (id) => /^tb/.test(id ?? "") || /^w[^:]+:t/.test(id ?? "");

// The herdr-app commands a remote row's focus or shell runs on machine `on` (see CONTRACT.md, Remote
// spaces). The space is entry.space, else the session when it is a space (an old-style session in
// its own space). A tab session is selected in its space, then as a tab. [] when no space is known.
export function remoteSteps(id, entry, on) {
  const session = entry.session ?? null;
  const space = entry.space ?? (isTabId(session) ? null : session);
  if (!space) return [];
  if (id === "shell") return [{ cmd: "tab.new", args: { space: `${on}/${space}`, folder: entry.path } }];
  const steps = [{ cmd: "space.select", args: { id: `${on}/${space}` } }];
  if (isTabId(session)) steps.push({ cmd: "tab.select", args: { id: `${on}/${session}` } });
  if (entry.pane) steps.push({ cmd: "agent.focus", args: { terminal: addressOn(on, entry.pane) } });
  return steps;
}
