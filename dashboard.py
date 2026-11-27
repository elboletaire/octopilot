#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["rich>=13.7"]
# ///
"""Octopilot live dashboard.

A read-only view over three sources, each refreshed at its own pace:

- agent state from herdr (`herdr agent list`), every few seconds;
- octopilot's own state.json and events.jsonl, the moment they change;
- GitHub, through `scan.py tick` (the scan that saves its transitions) when
  this checkout may run it, else `scan.py snapshot` (a scan that saves
  nothing), every DASHBOARD_REFRESH_SECS and right after any event that
  changes the table.

It never acts on the table: the engine does. The things it sends are the ones
you ask for: requests queued for the engine (go/no, re-review, investigate) and
pause/resume.
Run it through dashboard.sh so config.sh is loaded.
"""
import argparse
import collections
import fcntl
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import termios
import threading
import time
import tty

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

DIR = os.path.dirname(os.path.abspath(__file__))
# scan.py's herdr layer is the one source of truth for where an agent runs and
# how to reach it.
sys.path.insert(0, DIR)
import scan  # noqa: E402
import engine  # noqa: E402
STATE_FILE = os.environ.get(
    "STATE_FILE", os.path.expanduser("~/.local/state/octopilot/state.json"))
STATE_DIR = os.path.dirname(STATE_FILE)
EVENTS_FILE = os.path.join(STATE_DIR, "events.jsonl")
SNAPSHOT_FILE = os.path.join(STATE_DIR, "snapshot.json")
SNAPSHOT_LOCK = SNAPSHOT_FILE + ".lock"
# What each item last sent a desktop notification for, shared by every open
# dashboard so only one of them notifies.
NOTIFIED_FILE = os.path.join(STATE_DIR, "notified.json")
NOTIFIED_LOCK = NOTIFIED_FILE + ".lock"
# The format of notified.json: {"v": 2, "seen": {item: [cause, ...]}}.
NOTIFIED_VERSION = 2
# GitHub notifications watermark, shared so whichever dashboard polls next
# carries on from it; only the lock holder polls.
NOTIFY_WATCH_FILE = os.path.join(STATE_DIR, "notifications.json")
NOTIFY_LOCK = os.path.join(STATE_DIR, "notifications.lock")
NOTIFY_WHY = {"READY": "ready to merge", "overdue": "re-review overdue",
              "CAPPED": "out of review rounds",
              "PUSHED": "stale: pushed, never re-requested",
              "STALLED": "agent not reporting", "stalled": "agent not reporting",
              "CLOSED": "closed without merging",
              "BLOCKED": "agent blocked, failed or stalled",
              "blocked": "agent is blocked", "failed": "agent reported failed",
              "capped": "out of review rounds", "asking": "agent is asking something",
              "ready": "report to read", "woke": "back from hold",
              "engine_error": "engine failed", "send_failed": "engine failed"}
REFRESH_SECS = int(os.environ.get("DASHBOARD_REFRESH_SECS", "120"))
NOTIFY_POLL_SECS = int(os.environ.get("NOTIFY_POLL_SECS", "60"))
MAX_REVIEW_ROUNDS = int(os.environ.get("MAX_REVIEW_ROUNDS", "3"))
AGENT_STALL_MIN = int(os.environ.get("AGENT_STALL_MIN", "35"))
PLUGIN_ID = "elboletaire.octopilot"
IN_HERDR = os.environ.get("HERDR_ENV") == "1"

LIVE_SECS = 3          # herdr agent list
SESSIONS_SECS = 30     # herdr workspaces (titles, worktree paths)
META_SOURCE = "octopilot"
META_TTL_MS = 10 * 60 * 1000   # sidebar tokens vanish if the dashboard dies
META_EVERY = 180               # re-push unchanged tokens before they expire
KICK_DEBOUNCE = 15     # min seconds between event-triggered GitHub refreshes
CONFIRM_SECS = 4       # window for the second press of g / n / P
KITCHEN_SECS = 15      # the held-start alert cancels itself after this
KITCHEN_GRACE = 0.5    # a g this soon after the alert opened is a typed-ahead
                       # ggg, not a confirmation of something seen

# The headless dashboard writes its view here; a `refresh` action touches
# KICK_FILE to ask it for a forced GitHub refresh.
VIEW_FILE = os.path.join(STATE_DIR, "view.json")
VIEW_EVERY = 5         # rewrite at least this often, even when unchanged
KICK_FILE = os.path.join(STATE_DIR, "kick")
# A refresh is refused when view.json is older than this: no daemon is running.
VIEW_STALE_SECS = scan.VIEW_STALE_SECS
# How long the chef rests after the last event before dozing (register.tsx).
DOZE_SECS = 10 * 60
# Clock-relative fields move every second: left out of the change check.
VIEW_VOLATILE = ("generated_at", "quiet_s", "age_s", "last_tick_s", "heat",
                 "since_s")
INVESTIGATE_ARGS = ["repo", "question", "read_only"]
# Row actions: id, the TUI key that runs it, and its footer label. The order
# is the order the mods list them in.
ROW_ACTIONS = (
    ("focus", "\r", "open"), ("peek", "v", "agent"), ("shell", "t", "shell"),
    ("browser", "o", "browser"), ("go", "g", "go"), ("no", "n", "no"),
    ("grant", "R", "another round"), ("rereview", "e", "re-review"),
    ("ack", "x", "ack"), ("detach", "z", "detach"), ("hold", "d", "hold"),
    ("unhold", "d", "resume"), ("remove", "\x1b[3~", "remove"),
)
ROW_KEYS = {aid: key for aid, key, _ in ROW_ACTIONS}
# The older names of hold and unhold, still accepted by --action.
ROW_ALIASES = {"park": "hold", "unpark": "unhold"}
ROW_IDS = set(ROW_KEYS) | set(ROW_ALIASES)
# The TUI keys that map straight to a row action (d picks hold or unhold).
KEY_ROW = {"\r": "focus", "\n": "focus", "v": "peek", "t": "shell",
           "o": "browser", "g": "go", "n": "no", "R": "grant", "e": "rereview",
           "x": "ack", "z": "detach", "\x1b[3~": "remove"}
KEY_NAMES = {"\r": "Enter", "\x1b[3~": "Delete"}
# Terminals send Backspace (Shift+Backspace too) as these bytes; Macs have no Delete key.
KEY_ALIASES = {"\x7f": "\x1b[3~", "\x08": "\x1b[3~"}

# Rows by who has the next move: column_of() places them. A live report is
# read by scan.live_report(), the rule the scan itself uses.
NEEDS_ROWS = {"CAPPED", "READY", "PUSHED", "STALLED", "BLOCKED", "CLOSED"}
# Rows the engine acts on at its next tick, not the user (the note says what).
ORCH_ROWS = {"MERGED": "cleanup + rebase fan-out",
             "RESTACK": "restack + rebase-stack",
             "REVIEW": "reboot for review",
             "FEEDBACK": "send address-feedback", "BOOTING": "send review prompt"}
RUN_ROWS = {"WORKING"}
# The engine's next action on a card: the ⏭ badge (herdr-app/CONTRACT.md).
ENGINE_ACTIONS = {"MERGED": ("cleanup", "cleaning up"),
                  "RESTACK": ("restack", "restacking"),
                  "REVIEW": ("review", "starting review"),
                  "BOOTING": ("boot", "sending review"),
                  "FEEDBACK": ("feedback", "sending feedback")}
PICK_ROWS = {"PROPOSE"}
NOTE_ROWS = {"UNCLONED", "STALE", "UNKNOWN"}
REPORT_NEEDS = {"blocked", "failed", "stalled", "capped"}
# Events after which the GitHub view is worth refreshing early.
KICK_EVENTS = {"report", "spawn", "sent", "cleanup", "track", "detach",
               "hold", "unhold", "park", "unpark", "untrack", "decline",
               "merged", "tick-asked"}
# Events that wake the engine's drain: a request queued.
DRAIN_EVENTS = {"request"}
# The drain runs at once on those, and at least this often otherwise.
DRAIN_EVERY = 60
# Approximate, not a bound: engine.ACT_BUDGET_SECS (240) plus one go's worst case
# (about 1380 of timeouts). Left out: the wait for the state lock behind a tick's
# GitHub pass, and spawn_item's own gh calls (2 x 60). The margin is for those.
DRAIN_TIMEOUT_SECS = 2400
# A tick or drain running longer than this is reported as hung (the header's
# red flag).
TICK_STALE_SECS = 900

AGENT_GLYPH = {"running": ("◐", "yellow"), "waiting": ("⏸", "bold red"),
               "idle": ("○", "dim"), "stopped": ("■", "dim"),
               "error": ("✗", "red")}
CI_GLYPH = {"pass": ("✓", "green"), "fail": ("✗", "bold red"),
            "pending": ("⏳", "yellow"), "none": ("–", "dim")}
# The pick tree's room the board never takes: its title, eight rows, "+N more".
PICK_MIN_LINES = 10
# Pick repo headers are selectable under this prefix, which no row key has.
REPO_PREFIX = "repo:"


# ----------------------------------------------------------------- helpers

def sh(args, timeout=30, cwd=None):
    try:
        p = subprocess.run(args, capture_output=True, text=True,
                           timeout=timeout, cwd=cwd)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        return 1, "", str(e)


def read_json(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


def ago(secs):
    if secs is None:
        return "–"
    secs = max(0, int(secs))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h{(secs % 3600) // 60:02d}"
    return f"{secs // 86400}d"


def short(key):
    """vocdoni/vocdoni-app#452 -> vocdoni-app#452"""
    return key.split("/", 1)[1] if "/" in key else key


def desktop(title, body):
    """Fire-and-forget KDE/freedesktop notification."""
    try:
        subprocess.Popen(["notify-send", "-a", "Octopilot", title, body],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def notify_text(e):
    """The headline of a desktop notification for a card in Needs you."""
    why = e["why"] or "needs you"
    note = e["note"] or ""
    if why == "woke" and "back from hold" in note:
        return note[note.index("back from hold"):]
    if why in ("engine_error", "send_failed"):
        eng = e["engine"] or {}
        subject = eng.get("label") if eng.get("action") == "send" \
            else eng.get("action")
        if subject:
            return f"engine: {subject} failed"
    return NOTIFY_WHY.get(why, why)


def poll_notifications(watch):
    """One conditional read of the user's notifications, updating `watch`
    (Last-Modified, newest updated_at). True when something is newer than the
    newest seen; the first read only records it. gh exits 1 on a 304, so the
    status line decides, not the exit code."""
    args = ["gh", "api", "-i"]
    if watch.get("modified"):
        args += ["-H", f"If-Modified-Since: {watch['modified']}"]
    _, out, _ = sh(args + ["notifications"])
    head, _, body = out.replace("\r\n", "\n").partition("\n\n")
    lines = head.splitlines()
    # No status line: gh failed (no auth, no network).
    if not lines or not lines[0].startswith("HTTP/"):
        return False
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if name.lower() == "last-modified":
            watch["modified"] = value.strip()
    if lines[0].split()[1:2] != ["200"]:
        return False
    try:
        items = json.loads(body)
    except ValueError:
        return False
    newest = max([watch.get("newest", "")]
                 + [n.get("updated_at") or "" for n in items])
    fresh = "newest" in watch and newest > watch["newest"]
    watch["newest"] = newest
    return fresh


def check_notifications():
    """poll_notifications under a lock, so one dashboard polls for all of
    them. The watermark is persisted; a busy lock means another one polls."""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(NOTIFY_LOCK, "w") as lk:
        try:
            fcntl.flock(lk.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        watch = read_json(NOTIFY_WATCH_FILE)
        if not isinstance(watch, dict):
            watch = {}
        before = dict(watch)
        fresh = poll_notifications(watch)
        if watch != before or not os.path.exists(NOTIFY_WATCH_FILE):
            tmp = f"{NOTIFY_WATCH_FILE}.tmp.{os.getpid()}"
            with open(tmp, "w") as fh:
                json.dump(watch, fh)
            os.replace(tmp, NOTIFY_WATCH_FILE)
        return fresh


def split_key(key):
    for sep, kind in (("#", "pr"), ("!", "issue"), ("?", "investigate")):
        if sep in key:
            slug, num = key.split(sep, 1)
            return slug, num, kind
    return key, "", "?"


def pick_ref(key):
    """vocdoni/vocdoni-app#452 -> #452 (the repo is the header above it)"""
    return key[len(split_key(key)[0]):] or key


def repo_key(slug):
    """The key of a pick repo's header line, so the header can be selected."""
    return REPO_PREFIX + slug


# ----------------------------------------------------------------- backend

class Hosts:
    """Where the agents live: herdr workspaces and herdr-app spaces, through
    scan.py so the dashboard and the engine agree on them."""

    def live(self):
        return scan.all_live()

    def sessions(self):
        return {r["id"]: r for r in scan.all_sessions()[0]}

    def send(self, address, text):
        return scan.send_to(address, text)

    def focus(self, session, live):
        """Bring the session forward in its own backend: its tab, space or
        workspace, and the item's agent in it."""
        backend = scan.backend_for(session)
        if backend is None:
            return False
        target = (live.get(session) or {}).get("pane")
        return backend.focus(session, target, timeout=10)[0]

    def publish(self, entries):
        """Octopilot's view of each herdr agent, as pane metadata tokens the
        herdr sidebar renders ($ap_kind, $ap_pr, ...; see herdr/README.md)."""
        for pane, tokens in entries:
            args = ["pane", "report-metadata", pane, "--source", META_SOURCE,
                    "--ttl-ms", str(META_TTL_MS)]
            for k, v in tokens.items():
                args += ["--token", f"{k}={v}"] if v else ["--clear-token", k]
            scan.herdr(args, timeout=10)


# ------------------------------------------------------------------- model

class Model:
    """Everything the screen shows, refreshed by background threads."""

    def __init__(self, backend):
        self.backend = backend
        self.lock = threading.Lock()
        self.snap = read_json(SNAPSHOT_FILE)
        self.snap_mtime = 0
        self.snap_err = None
        self.snap_running = False
        self.snap_started = 0
        self.state = read_json(STATE_FILE, {}) or {}
        self.state_mtime = 0
        self.events = []
        self.events_size = 0
        self.live = {}
        self.sessions = {}
        self.kick = threading.Event()
        self.drain_kick = threading.Event()
        # Set and read under self.lock together: a drain is acting from its start.
        self.drain_running = False
        self.drain_started = None
        # The last lines a tick or drain printed, for the header and the kitchen.
        self.engine_last = None
        # Where the headless dashboard logs those lines; the TUI keeps stderr clean.
        self.log_engine = None
        self.changed = threading.Event()
        # Set by a headless refresh: the GitHub loop then skips its debounce.
        self.urgent = threading.Event()

    # -- sources

    def poll_files(self):
        """state.json, events.jsonl and snapshot.json: cheap stat()s, run
        every loop. The snapshot is re-read here rather than only after our
        own refresh, so every open dashboard shows whichever refresh ran."""
        try:
            m = os.stat(SNAPSHOT_FILE).st_mtime
        except OSError:
            m = 0
        if m != self.snap_mtime:
            snap = read_json(SNAPSHOT_FILE)
            if snap is not None:
                with self.lock:
                    self.snap, self.snap_mtime = snap, m
                self.changed.set()
        try:
            m = os.stat(STATE_FILE).st_mtime
        except OSError:
            m = 0
        if m != self.state_mtime:
            st = read_json(STATE_FILE)
            if st is not None:
                with self.lock:
                    self.state, self.state_mtime = st, m
                self.changed.set()
        try:
            size = os.stat(EVENTS_FILE).st_size
        except OSError:
            size = 0
        if size != self.events_size:
            fresh = []
            try:
                with open(EVENTS_FILE) as fh:
                    if 0 < self.events_size < size:
                        fh.seek(self.events_size)
                    lines = fh.readlines()
            except OSError:
                lines = []
            for line in lines:
                try:
                    fresh.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
            first = self.events_size == 0
            with self.lock:
                # A shrink means the log was trimmed: start over from it.
                self.events = (fresh if first or size < self.events_size
                               else self.events + fresh)[-200:]
                self.events_size = size
            if not first and any(e.get("kind") in KICK_EVENTS for e in fresh):
                self.kick.set()
            if not first and any(e.get("kind") in DRAIN_EVENTS for e in fresh):
                self.drain_kick.set()
            self.changed.set()

    def refresh_snapshot(self, force=False, verb="snapshot"):
        """Refresh snapshot.json by running scan.py `verb`, unless another
        dashboard (the pane and a peek overlay, say) is already running one or
        ran one recently: they all read the same snapshot.json."""
        if not force:
            gen = (read_json(SNAPSHOT_FILE) or {}).get("generated_at") or 0
            if time.time() - gen < REFRESH_SECS - 5:
                return
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(SNAPSHOT_LOCK, "w") as lk:
            try:
                fcntl.flock(lk.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return
            self.snap_running, self.snap_started = True, time.time()
            self.changed.set()
            code, out, err = sh([sys.executable, os.path.join(DIR, "scan.py"),
                                 verb, "--quiet"], timeout=1200)
            if verb == "tick":
                self.engine_output("tick", out)
            with self.lock:
                self.snap_err = None if code == 0 else \
                    (err or "snapshot failed").splitlines()[-1][:120]
                self.snap_running = False
        self.changed.set()

    def github_loop(self):
        force = False
        while True:
            self.github_pass(force)
            force = False
            deadline = time.time() + REFRESH_SECS
            while time.time() < deadline:
                if self.kick.wait(timeout=max(0.1, deadline - time.time())):
                    self.kick.clear()
                    wait = KICK_DEBOUNCE - (time.time() - self.snap_started)
                    if wait > 0 and not self.urgent.is_set():
                        time.sleep(wait)
                    self.urgent.clear()
                    force = True
                    break

    def github_pass(self, force):
        """One snapshot refresh; an error must not end the loop."""
        try:
            self.refresh_snapshot(force, "tick" if scan.engine_allowed(DIR)
                                  else "snapshot")
        except Exception:
            # Silent like notify_loop: the next pass retries.
            pass

    def engine_output(self, verb, out):
        """Keep the lines a tick or drain printed for the header, and log each one
        when headless. Both callers run --quiet, so a busy note is only a guard."""
        lines = [l for l in out.splitlines()
                 if l.strip() and not l.startswith("engine busy")]
        if not lines:
            return
        self.engine_last = {"at": time.time(), "verb": verb, "lines": lines[-12:]}
        if self.log_engine:
            for line in lines:
                self.log_engine(line)

    def engine_loop(self):
        """Drain the engine's inbox (scan.py drain): once at start, then on each
        request in the events log, and at least every DRAIN_EVERY seconds. Only
        where the engine is on."""
        while True:
            # Cleared before the drain, so a request that lands during it wakes the next.
            self.drain_kick.clear()
            try:
                if scan.engine_mode(checkout=DIR) == "on":
                    with self.lock:
                        self.drain_started, self.drain_running = time.time(), True
                    try:
                        _, out, _ = sh([sys.executable, os.path.join(DIR, "scan.py"),
                                        "drain", "--quiet"], timeout=DRAIN_TIMEOUT_SECS)
                        self.engine_output("drain", out)
                    finally:
                        with self.lock:
                            self.drain_running = False
            except Exception:
                # Silent like the other loops: the next wake retries.
                pass
            self.drain_kick.wait(timeout=DRAIN_EVERY)

    def notify_loop(self):
        """Kick the GitHub loop when the user's notifications bring something
        new, so a review request or a comment reaches the engine within
        about a minute, not at the next scheduled refresh."""
        while True:
            try:
                if check_notifications():
                    self.kick.set()
            except Exception:
                # Silent by design: the snapshot still catches up on its own.
                pass
            time.sleep(NOTIFY_POLL_SECS)

    def agents_loop(self):
        last_sessions = 0
        while True:
            live = self.backend.live()
            sessions = None
            if time.time() - last_sessions > SESSIONS_SECS:
                sessions = self.backend.sessions()
                last_sessions = time.time()
            with self.lock:
                self.live = live
                if sessions is not None:
                    self.sessions = sessions
            self.changed.set()
            time.sleep(LIVE_SECS)

    def publish_loop(self):
        """Mirror each herdr agent's row into the herdr sidebar: on change,
        and again before the tokens' TTL runs out."""
        last, last_at = {}, 0
        while True:
            time.sleep(5)
            data, _ = self.build()
            self.notify(data)
            cur = {}
            for section in ("needs", "running", "hold", "done"):
                for e in data[section]:
                    # The sidebar is herdr's TUI: herdr-app agents have none.
                    if e["pane"] and \
                            scan.backend_for(e["session"]) is not scan.HC:
                        cur[e["pane"]] = sidebar_tokens(e, section)
            stale = time.time() - last_at > META_EVERY
            push = [(p, t) for p, t in cur.items() if stale or last.get(p) != t]
            if push:
                self.backend.publish(push)
            if stale:
                last_at = time.time()
            last = cur

    def notify(self, data):
        """A desktop notification when a card enters Needs you for a cause it
        has not notified for. Once per (item, cause): a card that bounces out
        of Needs and back with the same cause stays quiet, and a new cause
        (a new head, a new report) notifies again. The first run, with no
        notified.json or one of an older format, only records the causes
        already there, so a fresh dashboard does not replay old news."""
        if self.snap is None or not shutil.which("notify-send"):
            return
        with self.lock:
            items = dict((self.state or {}).get("items") or {})
        # An empty read is no reason to forget every item: skip it.
        if not items:
            return
        tracked = set(items)
        for it in items.values():
            tracked.update(it.get("prev_keys") or [])
        with open(NOTIFIED_LOCK, "w") as lk:
            fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
            sent = read_json(NOTIFIED_FILE)
            first = not (isinstance(sent, dict)
                         and sent.get("v") == NOTIFIED_VERSION)
            seen = {} if first else {k: set(c) for k, c in
                                     (sent.get("seen") or {}).items()}
            for e in data["needs"]:
                causes = seen.setdefault(e["key"], set())
                cause = e["cause"] or e["why"] or "needs you"
                if cause in causes:
                    continue
                causes.add(cause)
                if not first:
                    desktop(f"🔴 {short(e['key'])}: {notify_text(e)}",
                            f"{e['title']}\n{(e['note'] or '')[:200]}")
            out = {"v": NOTIFIED_VERSION,
                   "seen": {k: sorted(c) for k, c in seen.items()
                            if k in tracked}}
            if first or out != sent:
                tmp = f"{NOTIFIED_FILE}.tmp.{os.getpid()}"
                with open(tmp, "w") as fh:
                    json.dump(out, fh)
                os.replace(tmp, NOTIFIED_FILE)

    def start(self):
        for fn in (self.github_loop, self.agents_loop, self.publish_loop,
                   self.notify_loop, self.engine_loop):
            threading.Thread(target=fn, daemon=True).start()

    # -- view

    def item_for(self, items, key):
        if key in items:
            return key, items[key]
        for k, it in items.items():
            if key in (it.get("prev_keys") or []):
                return k, it
        return key, None

    def build(self):
        """Snapshot rows + live state -> {running, needs, hold, done, pick,
        notes} lists of entry dicts, plus the header numbers."""
        with self.lock:
            snap = self.snap or {}
            state = self.state or {}
            live = dict(self.live)
            sessions = dict(self.sessions)
        now = time.time()
        items = state.get("items", {}) or {}
        declined = set(state.get("declined", []) or [])
        me = state.get("me")
        out = {s: [] for s in ("running", "needs", "hold", "done", "pick",
                               "notes")}
        seen = set()
        # A queued send sits on the item it resolves to, if the item is there.
        sends = collections.defaultdict(list)
        for p in state.get("pending_sends") or []:
            _, owner = self.item_for(items, p.get("key"))
            if owner is not None:
                sends[id(owner)].append(p)
        paused = bool(state.get("paused"))

        def place(row, key, it):
            """A tracked row's entry, and the column it sits in."""
            e = self.entry(row, key, it, live, sessions, now, me)
            ctx = {"live": live, "paused": paused, "stalled": e["stalled"],
                   "sends": sends.get(id(it), [])}
            section, extras = column_of(row, it,
                                        live.get(e["session"], {}), ctx)
            e.update(extras, section=section, needs=section == "needs")
            # herdr's live state is seconds old; the snapshot can be minutes
            # old. An agent at work is running whatever its last row or
            # report says, and the note says what it was.
            kind = row.get("kind")
            if section == "running" and e["agent"] == "running" and \
                    kind not in RUN_ROWS | {"NEW", "GONE"} | set(ORCH_ROWS):
                e["note"] = f"working again · was {kind.lower()}"
            return section, e

        for row in snap.get("rows", []):
            kind, key = row.get("kind"), row.get("key")
            cur_key, it = self.item_for(items, key)
            tracked_kind = kind not in PICK_ROWS | NOTE_ROWS
            # Cleaned up, detached or untracked since the snapshot.
            if tracked_kind and it is None and kind != "GONE":
                continue
            # Picked or declined since the snapshot. `it`, not `key in items`:
            # an issue re-keyed to its PR is only in its item's prev_keys.
            if kind in PICK_ROWS and (it is not None or key in declined):
                continue
            seen.add(cur_key)
            if kind in NOTE_ROWS or kind in PICK_ROWS:
                e = self.entry(row, cur_key, it, live, sessions, now, me)
                out["notes" if kind in NOTE_ROWS else "pick"].append(e)
            else:
                section, e = place(row, cur_key, it)
                out[section].append(e)

        # Spawned or tracked since the last snapshot: show them right away.
        for k, it in items.items():
            if k in seen:
                continue
            section, e = place({"kind": "NEW", "key": k,
                                "title": it.get("question")}, k, it)
            out[section].append(e)

        # Oldest wait first: the longer something has waited on you, the
        # more it is costing.
        out["needs"].sort(key=lambda e: -(e["quiet"] or 0))
        hdr = {"paused": bool(state.get("paused")),
               "active": snap.get("active"), "max_active": snap.get("max_active"),
               "untracked": snap.get("untracked") or 0,
               "generated_at": snap.get("generated_at"),
               "last_tick": state.get("last_tick")}
        return out, hdr

    def engine_status(self, now):
        """The engine for the header and the pan: its mode, what it is doing
        (off, idle, scanning a GitHub pass, acting on the table, or a tick
        hung past TICK_STALE_SECS) and the last lines it printed."""
        mode = scan.engine_mode(checkout=DIR)
        with self.lock:
            running, started = self.snap_running, self.snap_started
            generated = (self.snap or {}).get("generated_at") or 0
            drain_running, drain_started = self.drain_running, self.drain_started
            last = self.engine_last
        # generated_at is whole seconds: compare it with the start's second.
        if mode == "off":
            state, since = "off", None
        elif running and now - started > TICK_STALE_SECS:
            state, since = "stale", started
        elif drain_running and now - drain_started > TICK_STALE_SECS:
            state, since = "stale", drain_started
        elif drain_running:
            state, since = "acting", drain_started
        elif running and generated >= int(started):
            state, since = "acting", started
        elif running:
            state, since = "scanning", started
        else:
            state, since = "idle", None
        return {"mode": mode, "state": state,
                "since_s": int(now - since) if since else None, "last": last}

    def entry(self, row, key, it, live, sessions, now, me):
        it = it or {}
        kind = row.get("kind")
        sess = it.get("session")
        ag = live.get(sess, {}) if sess else {}
        agent_state = ag.get("state")
        last = it.get("last_seen") or it.get("added")
        quiet = (now - last) if last else None
        rep = scan.live_report(it, live)
        # scan.py's STALLED rule, applied live: the snapshot can be two
        # minutes behind, and the red Quiet column used to say "stalled"
        # while the row still sat under Running.
        # An agent that reported and stopped is not stalled, it is done.
        stalled = (it.get("driver") == "agent" and kind in RUN_ROWS | {"NEW"}
                   and quiet is not None and quiet >= AGENT_STALL_MIN * 60
                   and agent_state != "running" and not rep)
        hold = scan.hold_of(it) or {}
        if rep:
            note = f"{rep.get('state')}: {rep.get('msg') or ''}"
        elif stalled:
            note = "agent not reporting"
        elif hold.get("by") == "you":
            note = (f"held: {hold['reason']}" if hold.get("reason")
                    else "held by you")
        elif it.get("engine_error"):
            note = it["engine_error"]
        elif kind in ORCH_ROWS:
            note = f"next tick: {ORCH_ROWS[kind]}"
        elif kind == "NEW":
            note = "spawned, waiting for the next GitHub refresh"
        else:
            note = row.get("note") or it.get("last_note") or ""
        if agent_state == "waiting" and not rep:
            note = "agent is asking something" + (f" · {note}" if note else "")
        mode = it.get("mode") or row.get("mode") or \
            (row.get("fields") or {}).get("mode")
        if row.get("item_kind") == "issue" or split_key(key)[2] == "issue":
            pick_kind = "issue"
        elif mode == "comment":
            pick_kind = "review only"
        elif row.get("author") and me and row.get("author") != me:
            pick_kind = "adopt PR"
        else:
            pick_kind = "your PR"
        sinfo = sessions.get(sess, {}) if sess else {}
        # herdr-app's space holds the session; a tab is not one. The TUI has none.
        space = None
        if scan.backend_for(sess) is scan.HC:
            space = (sinfo.get("space") or it.get("space")
                     or (None if scan.is_tab(sess) else sess))
        pr = it.get("pr") or (split_key(key)[1] if split_key(key)[2] == "pr"
                              else None)
        # build() places the card: its section, needs, engine, cause, report.
        return {"kind": kind, "key": key, "title": row.get("title") or
                it.get("branch") or "", "url": row.get("url"),
                "checks": row.get("checks"), "draft": row.get("draft"),
                "session": sess, "space": space, "agent": agent_state,
                "stalled": stalled,
                "rounds": it.get("review_rounds"), "driver": it.get("driver"),
                "quiet": quiet, "note": note, "needs": False,
                "pick_kind": pick_kind, "path": sinfo.get("path"),
                "branch": it.get("branch"), "pr": pr,
                "stack": len(it.get("stack") or []),
                "pane": ag.get("pane"), "open": bool(sess and sess in sessions),
                "why": None, "hold": hold_view(row, it),
                "engine": None, "cause": None, "report": None}


def sidebar_tokens(e, section):
    """Row -> the $ap_* tokens herdr/README.md's sidebar layout shows."""
    kind = e["kind"].lower()
    rnd = f"{e['rounds'] or 0}/{MAX_REVIEW_ROUNDS}" \
        if e["rounds"] is not None or e["driver"] == "agent" else ""
    return {"ap_kind": ("🔴 " if section == "needs" else "") + kind,
            "ap_pr": f"#{e['pr']}" if e["pr"] else "",
            "ap_rounds": rnd,
            "ap_ci": CI_GLYPH.get(e["checks"] or "", ("", ""))[0],
            "ap_note": (e["note"] or "")[:48]}


def held_by_you(it):
    """Whether the user holds the item (`d`): its hold is by you."""
    return (scan.hold_of(it) or {}).get("by") == "you"


def hold_view(row, it):
    """The entry's hold, whatever its section: the item's, else the row's (a
    reviewer or author hold, its note as the reason). None when there is none."""
    hold = scan.hold_of(it) or row.get("hold")
    if not hold:
        return None
    return {"by": hold.get("by"), "reason": hold.get("reason") or ""}


def send_failed(p):
    """A queued send that went out and failed. A blocked one is waiting on
    the agent, not failed."""
    return bool(p.get("error")) and p["error"] != engine.BLOCKED


def engine_badge(kind, sends, paused, failed=None):
    """The ⏭ badge: the row's own engine action, then the action that failed
    (a send's reason fails as a send), then a queued send (the failing one
    first). None when nothing is pending."""
    if kind in ENGINE_ACTIONS:
        action, label = ENGINE_ACTIONS[kind]
    elif failed:
        labels = dict(ENGINE_ACTIONS.values())
        action, label = (failed, labels[failed]) if failed in labels \
            else ("send", failed)
    elif sends:
        first = next((p for p in sends if send_failed(p)), sends[0])
        action, label = "send", first["reason"]
    else:
        return None
    return {"action": action, "label": label, "paused": bool(paused)}


def column_of(row, it, ag, ctx):
    """Where a tracked row sits on the board: (section, extras). The first
    placement rule that matches wins (herdr-app/CONTRACT.md, Placement).
    extras: engine (the ⏭ badge), why (the reason a Needs card gives, for the
    notification's text), cause (the key a notification is once per, or None)
    and report (the live report state behind a BLOCKED or HOLD card, or None).
    ctx: live (the scan's agents), sends (the item's queued sends), paused,
    stalled (the dashboard's live check)."""
    kind = row.get("kind")
    it = it or {}
    sends = ctx.get("sends") or []
    failing = next((p for p in sends if send_failed(p)), None)
    rep = scan.live_report(it, ctx["live"])
    state = rep.get("state")
    failed = it.get("engine_error_action") if it.get("engine_error") else None
    extras = {"engine": engine_badge(kind, sends, ctx.get("paused"), failed),
              "why": None, "cause": None, "report": None}
    agent = ag.get("state")

    def needs(why, cause):
        extras.update(why=why, cause=cause)
        return "needs", extras

    if kind in ("MERGED", "RESTACK"):
        return "running", extras
    if kind == "GONE":
        return "done", extras
    if kind == "CLOSED":
        return needs("CLOSED", f"CLOSED:{row.get('head') or ''}")
    if it.get("engine_error"):
        return needs("engine_error", f"engine-error:{it['engine_error']}:"
                                     f"{it.get('engine_error_at')}")
    if failing:
        return needs("send_failed",
                     f"send:{failing['reason']}:{failing['at']}")
    if agent == "waiting":
        # The episode: the last sign of life before the question.
        return needs("asking", f"asking:{it.get('last_seen')}")
    if kind in ("REVIEW", "BOOTING", "FEEDBACK") or agent == "running":
        return "running", extras
    hold = scan.hold_of(it) or {}
    last = it.get("last_report") or {}
    newer = last.get("state") in REPORT_NEEDS and \
        last.get("at", 0) > hold.get("since", 0)
    if hold.get("by") == "you" and not newer:
        return "hold", extras
    if state in REPORT_NEEDS:
        if state != "capped":
            extras["report"] = state
        return needs(state, f"report:{state}:{rep.get('at')}")
    if state == "held":
        extras["report"] = state
        return "hold", extras
    if state == "ready" and it.get("read_only"):
        if it.get("report_acked") == rep.get("at"):
            return "done", extras
        return needs("ready", f"report:ready:{rep.get('at')}")
    if kind in NEEDS_ROWS:
        fields = row.get("fields") or {}
        why = "overdue" if kind == "READY" and \
            fields.get("wait") == "overdue" else kind
        if kind == "BLOCKED" and fields.get("report") in REPORT_NEEDS:
            why = fields["report"]
        # Each kind's episode: the head of a READY or PUSHED card, the head
        # and rounds of a CAPPED one, the last sign of life of a STALLED one
        # and the report's time of a BLOCKED one.
        episode = {"READY": row.get("head"), "PUSHED": row.get("head"),
                   "CAPPED": f"{row.get('head')}:{it.get('review_rounds')}",
                   "STALLED": it.get("last_seen"),
                   "BLOCKED": (it.get("last_report") or {}).get("at")}
        cause = f"{why}:{episode[kind] or ''}" if kind in episode else why
        # A card the user woke from a hold notifies once for its wake.
        if "back from hold" in (row.get("note") or ""):
            return needs("woke", f"woke:{cause}")
        return needs(why, cause)
    if kind == "HOLD":
        return "hold", extras
    if kind == "DONE":
        return "done", extras
    if ctx.get("stalled"):
        return needs("stalled", f"stalled:{it.get('last_seen')}")
    return "running", extras


# ---------------------------------------------------------------- actions
#
# What a key does, and what --action does: the TUI and the headless dashboard
# both come here. A row action is checked with row_refusal() and, when it
# asks, confirmed with row_confirm(); perform() then runs it.

Result = collections.namedtuple("Result", "ok message kitchen holds",
                                defaults=(False, ()))


def truthy(value):
    return str(value or "").lower() in ("1", "y", "yes", "true")


def tracked_item(model, key):
    return ((model.state or {}).get("items") or {}).get(key)


def kitchen_message():
    return f"🍳 The kitchen is closed (past {scan.WIND_DOWN_AT})"


# A start held past wind-down or at the cap: hold name (as engine._held names
# it) -> the marker the request ends with, and the short reason the row prompt
# gives.
HOLDS = {
    "wind-down": (engine.CONFIRMED_PAST, "🍳 kitchen closed"),
    "cap": (engine.CONFIRMED_OVER, "⚠ at the cap"),
}


def start_holds(model):
    """What a go or an investigation started now must be confirmed past: a list
    of (hold name, the message). Past wind-down and at the cap (the engine's
    count, as engine._held makes it) are the two."""
    holds = []
    if scan.wind_down() == "yes":
        holds.append(("wind-down", kitchen_message()))
    used = engine.slots_used(model.state, model.snap)
    if used >= scan.MAX_ACTIVE:
        holds.append(("cap",
                      f"⚠ At the cap: {used}/{scan.MAX_ACTIVE} slots in use"))
    return holds


def queue_request(text):
    """Queue a request for the engine (scan.py request). Nothing is typed into a
    pane, which would also send whatever the user is half-way through."""
    code, out, err = sh([sys.executable, os.path.join(DIR, "scan.py"),
                         "request", text])
    if code == 0:
        return Result(True, f"queued '{text}'")
    return Result(False, f"queue failed: {(err or out)[:100]}")


def start_request(model, cmd, confirmed=False):
    """Queue cmd, a go or an investigation, past its holds. `confirmed` is the
    hold names the user was shown: a hold not among them asks again, with the
    message of each hold now. True is an old mod's confirmed=1: every hold now."""
    holds = start_holds(model)
    if not holds:
        return queue_request(cmd)
    names = [name for name, _ in holds]
    if confirmed is True:
        confirmed = names
    if not confirmed or any(name not in confirmed for name in names):
        return Result(False, "\n".join(msg for _, msg in holds), kitchen=True,
                      holds=tuple(names))
    return queue_request(cmd + "".join(f" {HOLDS[name][0]}"
                                       for name in names))


def go_request(model, key, confirmed=False):
    """`go KEY`. A start held past wind-down or at the cap needs confirmed: the
    second press of the held-start alert."""
    return start_request(model, f"go {key}", confirmed)


def investigate_request(model, repo, question, read_only, confirmed=False):
    """`/investigate`, which a hold needs confirmed like go_request."""
    cmd = f"/investigate {'--read-only ' if read_only else ''}{repo} {question}"
    return start_request(model, cmd, confirmed)


def set_paused(verb):
    """`pause` or `resume` through scan.py; its output is the message."""
    code, out, err = sh([sys.executable, os.path.join(DIR, "scan.py"), verb])
    return Result(code == 0, out or err)


def scan_run(args, timeout=30):
    """A scan.py command that needs no judgement, as its one-line outcome."""
    code, out, err = sh([sys.executable, os.path.join(DIR, "scan.py"), *args],
                        timeout=timeout)
    lines = (out or err).splitlines()
    # cleanup ends with its SIBLINGS output; show a failed removal first,
    # else its "cleaned up" line.
    removed = tuple(f"{label} " for label in scan.REMOVE_LABELS)
    failed = next((l for l in lines if not l.endswith(": ok")
                   and l.startswith(removed)), None)
    done = next((l for l in lines if l.startswith("cleaned up")), None)
    message = failed or done or (lines[-1] if lines
                                 else f"{args[0]} exit {code}")
    return Result(code == 0 and failed is None, message)


def popup(entrypoint, env, cwd=None):
    """Open one of the plugin's popups -> (opened, error). Nothing opens
    outside herdr."""
    if not IN_HERDR:
        return False, ""
    args = ["herdr", "plugin", "pane", "open", "--plugin", PLUGIN_ID,
            "--entrypoint", entrypoint, "--placement", "popup",
            "--width", "92%", "--height", "88%"]
    for k, v in env.items():
        args += ["--env", f"{k}={v}"]
    if cwd:
        args += ["--cwd", cwd]
    code, out, err = sh(args, timeout=20)
    if code != 0:
        return False, f"herdr popup failed: {(err or out)[:100]}"
    return True, ""


def peek_hc(terminal):
    """Peek at an hc terminal over its tab (herdr-app's terminal.peek), which
    works on a parked one: agent.focus needs a pane -> bool."""
    return scan.app_do("terminal.peek", ["--terminal", terminal], timeout=10)[0] == 0


def open_prompter():
    """Focus the prompter, where the user types. It is looked up only in the
    dashboard's own backend."""
    own = scan.HC if scan.OCTOPILOT_BACKEND == "hc" else scan.HERDR
    if own is scan.HC:
        # Peek, not agent.focus: the engine terminals are parked (no pane).
        prompter = scan.agent_address(scan.PROMPTER_AGENT)
        if prompter and peek_hc(prompter):
            return Result(True, "")
    elif scan.herdr(["agent", "focus", scan.PROMPTER_AGENT], timeout=10)[0] == 0:
        return Result(True, "")
    return Result(False, "no prompter running")


def focus_row(model, e):
    """Bring the row's agent forward in its own backend."""
    with model.lock:
        live = dict(model.live)
    if model.backend.focus(e["session"], live):
        # From the GUI, a TUI session comes forward in the herdr TUI, not here: say so.
        gui_on_tui = scan.OCTOPILOT_BACKEND == "hc" and scan.backend_for(e["session"]) is scan.HERDR
        return Result(True, "focused in the herdr TUI (this session lives there)" if gui_on_tui else "")
    return Result(False, f"could not focus session {e['session']}")


def peek_row(model, e):
    """The row's agent in a popup over the dashboard, without leaving it.
    `ctrl+b q` detaches herdr's attach client, which closes the popup."""
    if scan.backend_for(e["session"]) is scan.HC:
        # herdr-app's peek is its popup: the terminal over this tab.
        code, _, raw = scan.app_do("terminal.peek",
                                   ["--terminal", e["pane"]], timeout=10)
        if code != 0:
            return Result(False, f"peek failed: {raw[:100]}")
        return Result(True, "")
    opened, err = popup("agent", {"AP_AGENT": e["pane"] or
                                  tracked_item(model, e["key"])["agent"]})
    if opened:
        return Result(True, "ctrl+b q closes the popup")
    return Result(False, err or "peek needs herdr")


def shell_row(model, e, suspend=None):
    """A shell in the row's worktree: a herdr-app tab in its space, or the
    herdr popup, or else `suspend` runs one in the dashboard's own terminal."""
    path = e["path"]
    if scan.backend_for(e["session"]) is scan.HC:
        if not e["space"]:
            return Result(False, "no space for this row")
        code, _, raw = scan.app_do("tab.new", ["--space", e["space"],
                                               "--folder", path], timeout=10)
        if code != 0:
            return Result(False, f"new tab failed: {raw[:100]}")
        return Result(True, "")
    opened, err = popup("shell", {}, cwd=path)
    if opened:
        return Result(True, "")
    if suspend is None:
        return Result(False, err or "shell needs herdr or a terminal")
    suspend([os.environ.get("SHELL", "sh")])
    return Result(True, err)


def browse_row(e):
    """The row's issue or PR on GitHub, in the browser (gh --web)."""
    slug, num, kind = split_key(e["key"])
    args = ["gh", "pr" if kind == "pr" else "issue", "view", num, "-R",
            slug, "--web"]
    subprocess.Popen(args, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    return Result(True, f"opening {short(e['key'])} in the browser")


def unread_report(model, it):
    """Whether a read-only investigation's ready report is still to be read:
    the report `x` acknowledges."""
    if not (it or {}).get("read_only"):
        return False
    with model.lock:
        live = dict(model.live)
    rep = scan.live_report(it, live)
    return rep.get("state") == "ready" and it.get("report_acked") != rep.get("at")


def row_refusal(model, aid, e):
    """Why the TUI refuses aid on this row, as its message; None when it runs."""
    kind = e["kind"]
    it = tracked_item(model, e["key"])
    if aid == "focus":
        return None if e["session"] else "no session for this row"
    if aid == "shell":
        if not (e["path"] and os.path.isdir(e["path"])):
            return "no worktree for this row"
        if scan.backend_for(e["session"]) is scan.HC and not e["space"]:
            return "no space for this row"
        return None
    if aid in ("go", "no"):
        return None if kind == "PROPOSE" else \
            "go/no only applies to Pick next rows"
    if aid == "grant":
        return None if kind == "CAPPED" else "R only applies to CAPPED rows"
    if aid == "rereview":
        return None if kind == "PUSHED" else \
            "e only applies to PUSHED (stale) rows"
    if aid == "ack":
        if kind == "PUSHED" or unread_report(model, it):
            return None
        return "x only applies to PUSHED (stale) rows and reports to read"
    if aid == "detach":
        return None if it is not None else \
            "detach only applies to tracked items"
    if aid in ("hold", "unhold"):
        if it is None:
            return "d only applies to tracked items"
        if aid == "unhold":
            return None if held_by_you(it) else f"{e['key']} is not on hold"
        if kind in ("MERGED", "RESTACK", "CLOSED", "GONE", "DONE", "BOOTING"):
            return f"{kind} rows can't be held now"
        # Its pending feedback would revive the card on the next tick.
        if kind == "FEEDBACK":
            return "FEEDBACK rows can't be held: the feedback is pending"
        if held_by_you(it):
            return f"{e['key']} is already on hold"
        return None
    if aid == "peek":
        if scan.backend_for(e["session"]) is scan.HC:
            return None if e["pane"] else "no live agent for this row"
        if not (e["pane"] or (it or {}).get("agent")):
            return "no live agent for this row"
        return None if IN_HERDR else "peek needs herdr"
    if aid == "browser":
        return (None if split_key(e["key"])[2] in ("pr", "issue")
                else "nothing to open for an investigation")
    if aid == "remove":
        if it is None:
            return "remove only applies to tracked items"
        # MERGED needs the SIBLINGS fan-out, RESTACK a restack, not a removal.
        if kind in ("MERGED", "RESTACK"):
            return "octopilot handles MERGED and RESTACK rows itself"
    return None


def row_confirm(model, aid, e):
    """The prompt shown before aid runs here (a second press confirms), or
    None when it runs at once."""
    key = e["key"]
    it = tracked_item(model, key) or {}
    if aid == "go":
        holds = start_holds(model)
        if not holds:
            return f"press g again to queue 'go {key}'"
        why = " · ".join(HOLDS[name][1] for name, _ in holds)
        return f"{why} -- press g again to start {key} anyway"
    if aid == "no":
        return f"press n again to decline {key}"
    if aid == "grant":
        return (f"press R again to grant {key} another "
                f"{MAX_REVIEW_ROUNDS} review rounds")
    if aid == "rereview":
        return f"press e again to ask for a re-review of {key}"
    if aid == "ack":
        return f"press x again to ack {key}"
    if aid == "detach":
        return f"press z again to detach {key} (worktree kept, untracked)"
    if aid == "hold":
        return f"press d again to put {key} on hold (session kept, slot freed)"
    if aid == "unhold":
        return f"press d again to take {key} off hold"
    if aid == "remove":
        busy = ("⚠ its agent is working -- " if e["kind"] in
                ("WORKING", "BOOTING", "REVIEW", "FEEDBACK") else "")
        # An item without a workspace is only untracked by cleanup.
        gone = ("tab and worktree deleted" if scan.is_tab(it.get("session"))
                else "workspace and worktree deleted" if it.get("session")
                else "worktree kept")
        return f"{busy}press del (⌫) again to remove {key} ({gone}, untracked)"
    return None


def perform(model, aid, e, confirmed=False, suspend=None):
    """Run the row action aid on e; the caller has checked row_refusal().
    `confirmed` lets go past its holds (wind-down, the cap) through. `suspend`
    runs a shell in the dashboard's own terminal when no popup can."""
    key = e["key"]
    if aid == "focus":
        return focus_row(model, e)
    if aid == "peek":
        return peek_row(model, e)
    if aid == "shell":
        return shell_row(model, e, suspend)
    if aid == "browser":
        return browse_row(e)
    if aid == "go":
        return go_request(model, key, confirmed)
    if aid == "no":
        return queue_request(f"no {key}")
    if aid == "grant":
        return scan_run(["reset-rounds", key])
    if aid == "rereview":
        return queue_request(f"re-review {key}")
    if aid == "ack":
        return scan_run(["ack-push", key])
    if aid == "detach":
        return scan_run(["detach", key])
    if aid in ("hold", "unhold"):
        return scan_run([aid, key])
    if aid == "remove":
        return scan_run(["cleanup", key], timeout=400)
    return Result(False, f"unknown action {aid}")


def pick_groups(rows):
    """[(repo slug, [(#, entry)])], repos in order of first appearance. The #
    is the row's place in rows, which `go N` and the engine both count
    by, so rows are not re-sorted, even when a repo's rows are not together."""
    groups = {}
    for i, e in enumerate(rows):
        groups.setdefault(split_key(e["key"])[0], []).append((i, e))
    return list(groups.items())


# -------------------------------------------------------------- view.json

def event_detail(e):
    return " ".join(str(e[f]) for f in ("state", "round", "phase", "branch",
                                        "msg", "note", "reason", "left", "text",
                                        "error")
                    if e.get(f) not in (None, ""))


def activity_rows(model):
    """The last six events but ticks, newest first, as the activity panel
    shows them."""
    with model.lock:
        evs = [e for e in model.events if e.get("kind") != "tick"][-6:]
    return [{"at": e.get("at", 0), "kind": e.get("kind", ""),
             "key": short(e.get("key", "")), "detail": event_detail(e)}
            for e in reversed(evs)]


def kitchen_reaction(ev):
    """feed.ts's react(): the mood and how long it lasts, the burst, the item
    it is about (None unless the kind carries one) and the log line (None for
    a heartbeat)."""
    kind = ev.get("kind") or ""
    key = ev.get("key") if isinstance(ev.get("key"), str) else None
    k = short(key or "")

    def text(name):
        v = ev.get(name)
        return v if isinstance(v, str) else ""

    def rx(mood, ms, burst=None, keyed=False, line=None, good=False, bad=False):
        tone = "good" if good else "bad" if bad else "info"
        return {"mood": mood, "ms": ms, "burst": burst,
                "key": key if keyed else None, "text": line,
                "good": good, "tone": tone}

    if kind == "engine-error":
        return rx("worried", 3000, "smoke", True, text("msg") or f"engine-error {k}",
                  bad=True)
    if kind == "engine-skip":
        return rx("worried", 3000, "smoke", line=f"skipped: {text('left') or text('reason')}",
                  bad=True)
    if kind == "held":
        return rx("worried", 2500, "smoke", True,
                  f"held {short(key or 'request')}: {text('reason')}")
    if kind in ("go-refused", "request-refused"):
        verb = text("verb") or ("go" if kind == "go-refused" else "request")
        return rx("sad", 3500, "smoke", True,
                  " ".join(p for p in (verb, k, "refused:", text("reason")) if p),
                  bad=True)
    if kind == "go":
        return rx("flip", 2000, None, True, f"started {k} {text('title')}".strip(),
                  True)
    if kind == "tick":
        active = ev.get("active")
        active = int(active) if isinstance(active, (int, float)) else 0
        return rx("flip", 2500, "steam", line=f"tick · {active} cooking")
    if kind == "spawn":
        return rx("pour", 2500, "batter", True,
                  f"new order {k} {text('title')}".strip(), True)
    if kind == "sent":
        return rx("flip", 1500, None, True, f"→ {k} {text('text')}".strip())
    if kind == "report":
        state, msg = text("state"), text("msg")
        line = f"{state} {k}" + (f": {msg}" if msg else "")
        if state == "ready":
            return rx("happy", 4000, "sparkle", True, line, True)
        if state == "capped":
            return rx("happy", 3000, "steam", True, line)
        if state == "stalled":
            return rx("worried", 3000, "smoke", True, line, bad=True)
        return rx("sad", 4500, "smoke", True, line, bad=True)
    if kind == "request":
        return rx("surprised", 2000, "bang", line=f"asked: {text('text')}")
    if kind in ("cleanup", "merged"):
        return rx("serve", 3500, "heart", True, f"served {k} (merged)", True)
    if kind == "pause":
        return rx("sleep", 2000, "zzz", line="paused")
    if kind == "resume":
        return rx("surprised", 1500, line="resumed")
    if kind in ("round", "reviewed"):
        return rx("flip", 2000, None, True, f"review round {k}")
    if kind == "capped":
        return rx("worried", 2500, None, True, f"out of review rounds {k}",
                  bad=True)
    if kind == "feedback":
        return rx("flip", 1500, None, True, f"feedback {k}")
    if kind == "heartbeat":
        return rx("idle", 0, "steam", True)
    if kind == "reboot":
        return rx("surprised", 2000, "bang", True, f"reboot {k}", bad=True)
    if kind in ("decline", "untrack", "detach"):
        return rx("worried", 1500, "ink", line=f"{kind} {k}".strip())
    if kind == "track":
        return rx("pour", 2000, "batter", True, f"track {k}")
    return rx("idle", 0, line=f"{kind} {k}".strip())


def kitchen_view(evs):
    """The kitchen log: the last 20 events that have a line, oldest first."""
    out = []
    for ev in evs:
        r = kitchen_reaction(ev)
        if r["text"]:
            out.append({"at": ev.get("at", 0), "kind": ev.get("kind") or "",
                        "mood": r["mood"], "text": r["text"], "good": r["good"],
                        "tone": r["tone"],
                        "key": r["key"], "ms": r["ms"], "burst": r["burst"]})
    return out[-20:]


def ball_state(item, done_kind):
    """readKitchen's ballState: the snapshot's READY, CAPPED and DONE, else the
    agent's last report, else cooking once a session exists."""
    if done_kind in ("READY", "CAPPED", "DONE"):
        return {"READY": "ready", "CAPPED": "capped", "DONE": "done"}[done_kind]
    report = item.get("last_report") or {}
    state = report.get("state") if isinstance(report, dict) else None
    if state in ("ready", "capped", "stalled"):
        return state
    if state in ("blocked", "failed"):
        return "burnt"
    return "cooking" if item.get("session") else "raw"


def kitchen_pan(model, hdr, evs, now, busy):
    """The pan and the chef: readKitchen() over the tracked items, the focus
    of the newest event on the pan, and the chef's mood (register.tsx's
    frame() and resting())."""
    state = model.state or {}
    items = state.get("items") or {}
    snap_kinds = {r.get("key"): r.get("kind")
                  for r in (model.snap or {}).get("rows") or []
                  if isinstance(r, dict)}

    def added_of(item):
        v = item.get("added")
        return float(v) if isinstance(v, (int, float)) else 0.0

    holes = hdr["max_active"] or 6
    balls = []
    for key, item in sorted(items.items(), key=lambda kv: added_of(kv[1])):
        added = added_of(item)
        age = max(0.0, (now - added) / 60) if added else 0.0
        balls.append({"key": key, "label": short(key),
                      "state": ball_state(item, snap_kinds.get(key)),
                      "heat": round(min(1.0, age / 25), 2)})
    served = [{"key": b["key"], "label": b["label"]}
              for b in balls if b["state"] == "done"]
    on_pan = [b for b in balls if b["state"] != "done"][:holes]
    on_pan_keys = {b["key"] for b in on_pan}
    focus = next((r["key"] for r in map(kitchen_reaction, reversed(evs))
                  if r["key"] in on_pan_keys), None)
    for b in on_pan:
        b["focus"] = b["key"] == focus

    paused = state.get("paused") is True
    last = evs[-1].get("at", 0) if evs else now
    if paused:
        rest = "sleep"
    elif busy:
        rest = "busy"
    elif now - last > DOZE_SECS:
        rest = "doze"
    else:
        rest = "idle"
    chef = {"mood": rest, "since": last, "ms": 0, "resting": rest}
    # The newest event that sets a mood decides it, while its time lasts.
    for ev in reversed(evs):
        r = kitchen_reaction(ev)
        if r["ms"] > 0:
            at = ev.get("at", 0)
            if now < at + r["ms"] / 1000:
                chef = {"mood": r["mood"], "since": at, "ms": r["ms"],
                        "resting": rest}
            break
    return {"holes": holes, "paused": paused, "busy": busy, "balls": on_pan,
            "shelf": served[:8], "shelf_total": len(served), "chef": chef}


def row_actions(model, e):
    """The row's actions, the ones the TUI would run on its keys."""
    out = []
    for aid, key, label in ROW_ACTIONS:
        if row_refusal(model, aid, e) is None:
            out.append({"id": aid, "key": KEY_NAMES.get(key, key),
                        "label": label,
                        "confirm": row_confirm(model, aid, e)})
    return out


def global_actions(model):
    verb = "resume" if (model.state or {}).get("paused") else "pause"
    return [
        {"id": "refresh", "key": "r", "label": "refresh", "confirm": None},
        {"id": verb, "key": "P", "label": verb,
         "confirm": f"press P again to {verb} octopilot"},
        {"id": "investigate", "key": "i", "label": "investigate",
         "confirm": None, "args": INVESTIGATE_ARGS},
        {"id": "toggle_done", "key": "D", "label": "done", "confirm": None},
        {"id": "prompter", "key": "a", "label": "prompter", "confirm": None},
    ]


def entry_view(model, e, section, index=None):
    """A row as view.json has it: what the TUI draws, plus what a mod needs
    to show it and act on it."""
    it = tracked_item(model, e["key"])
    layers = e.get("stack") or 0
    backend = scan.backend_for(e["session"])
    pr = e["pr"]
    # The TUI card shows 0/3 for an agent-driven row whose count is not set yet.
    rounds = e["rounds"]
    if rounds is None and e["driver"] == "agent":
        rounds = 0
    view = {
        "key": e["key"],
        "label": short(e["key"]) + (f" +{layers - 1}" if layers > 1 else ""),
        "kind": e["kind"], "section": section, "title": e["title"],
        "draft": bool(e["draft"]), "url": e["url"], "checks": e["checks"],
        "agent": e["agent"], "rounds": rounds,
        "max_rounds": MAX_REVIEW_ROUNDS, "driver": e["driver"],
        "quiet_s": None if e["quiet"] is None else int(e["quiet"]),
        "stalled": e["stalled"], "needs": e["needs"],
        "why": notify_text(e) if e["why"] else None,
        "note": e["note"], "pick_kind": e["pick_kind"],
        "session": e["session"], "space": e["space"],
        "backend": backend.name if backend else None,
        "pane": e["pane"], "path": e["path"], "branch": e["branch"],
        "pr": int(pr) if str(pr or "").isdigit() else None,
        "stack": layers, "open": e["open"],
        "parked": held_by_you(it),
        "hold": e["hold"], "engine": e["engine"], "cause": e["cause"],
        "report": e["report"],
        "tracked": it is not None, "actions": row_actions(model, e),
    }
    if index is not None:
        view["index"] = index
    return view


def view_for(model):
    """The document view.json holds: the board the TUI draws, read from the
    same build()."""
    data, hdr = model.build()
    now = time.time()
    gen = hdr["generated_at"]
    if model.snap_running:
        github = "refreshing"
    elif not gen:
        github = "none"
    else:
        age = now - gen
        github = ("ok" if age < REFRESH_SECS * 1.5 else
                  "stale" if age < REFRESH_SECS * 4 else "old")
    last_tick = hdr["last_tick"]

    def column(cid, title, section, rows, **extra):
        return {"id": cid, "title": title,
                "entries": [entry_view(model, e, section) for e in rows],
                **extra}

    with model.lock:
        evs = list(model.events)
    engine = model.engine_status(now)
    busy = engine["state"] == "acting"
    return {
        "v": 1,
        "generated_at": now,
        "header": {
            "needs": len(data["needs"]),
            "running": len(data["running"]),
            "hold": len(data["hold"]),
            "pick": len(data["pick"]),
            "active": hdr["active"], "max_active": hdr["max_active"],
            "untracked": hdr["untracked"],
            "github": {"state": github,
                       "age_s": None if not gen else int(now - gen),
                       "error": model.snap_err},
            "last_tick_s": int(now - last_tick) if last_tick else None,
            "engine": engine,
            "paused": hdr["paused"],
            "wind_down": {"state": scan.wind_down() or "off",
                          "at": scan.WIND_DOWN_AT or None},
        },
        "columns": [
            column("running", "🟡 Running", "running", data["running"],
                   hint="agents at work, their own CI waits and review loops "
                        "included; ⏭ = the engine acts on it next"),
            column("needs", "🔴 Needs you", "needs", data["needs"],
                   hint="waiting on you"),
            column("hold", "💤 On hold", "hold", data["hold"],
                   hint="waiting on someone else, or held by you; holds no slot"),
            column("done", "✔ Done", "done", data["done"],
                   hint="finished; holds no slot", hidden=len(data["done"])),
        ],
        "pick": [{"repo": slug,
                  "entries": [entry_view(model, e, "pick", i + 1)
                              for i, e in grp]}
                 for slug, grp in pick_groups(data["pick"])],
        "notes": [{"kind": e["kind"], "key": e["key"]} for e in data["notes"]],
        "activity": activity_rows(model),
        "kitchen": kitchen_view(evs),
        "pan": kitchen_pan(model, hdr, evs, now, busy),
        "global_actions": global_actions(model),
    }


def find_row(model, key):
    data, _ = model.build()
    return next((e for rows in data.values() for e in rows
                 if e["key"] == key), None)


def confirmed_holds(args):
    """The holds a --action confirm answers: the names the alert showed
    (holds=wind-down,cap), True for an old mod's confirmed=1 alone, False when
    nothing was confirmed."""
    if not truthy(args.get("confirmed")):
        return False
    if "holds" in args:
        return [name for name in args["holds"].split(",") if name]
    return True


def run_action(model, aid, key, args):
    """--action: one row or global action by its id, through the TUI key's
    code path. The mod asks the second press (`confirm`), so this runs at
    once; confirmed=1 with holds=<the shown names> is the kitchen alert's
    second press."""
    aid = ROW_ALIASES.get(aid, aid)
    if aid == "refresh":
        # Only a running headless dashboard rewrites view.json; it watches the
        # kick file's mtime (see headless()).
        view = read_json(VIEW_FILE) or {}
        if time.time() - (view.get("generated_at") or 0) > VIEW_STALE_SECS:
            return Result(False, "no headless dashboard running")
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(KICK_FILE, "a"):
            pass
        os.utime(KICK_FILE)
        return Result(True, "refreshing from GitHub…")
    if aid in ("pause", "resume"):
        return set_paused(aid)
    if aid == "investigate":
        repo = args.get("repo", "").strip()
        question = args.get("question", "").strip()
        if not (repo and question):
            return Result(False, "investigate needs a repo and a question")
        return investigate_request(model, repo, question,
                                   truthy(args.get("read_only")),
                                   confirmed_holds(args))
    if aid == "prompter":
        return open_prompter()
    if aid == "toggle_done":
        return Result(False, "toggle_done is the mod's own: the mod filters "
                             "Done")
    if aid not in ROW_IDS:
        return Result(False, f"unknown action {aid}")
    e = find_row(model, key) if key else None
    if e is None:
        return Result(False, f"no row {key}" if key
                      else f"{aid} needs a row key")
    refused = row_refusal(model, aid, e)
    if refused:
        return Result(False, refused)
    return perform(model, aid, e, confirmed=confirmed_holds(args))


# -------------------------------------------------------------------- view

class Dashboard:
    def __init__(self, model, console):
        self.m = model
        self.console = console
        self.sel_key = None
        self.show_done = False
        self.hidden_done = 0
        self.flash = ("", 0)
        self.confirm = None   # (action, key, deadline)
        self.alert = None     # (key, title, deadline, message, holds): a start is held
        self.selectable = []
        self.groups = []      # keys per board column, then the pick list
        self.expanded = set() # pick repos showing their rows; the rest collapse
        self.repos = []       # pick repo slugs, in order, as last rendered
        self.repo_of = {}     # item key -> its repo header's key

    def say(self, msg):
        self.flash = (msg, time.time())

    # -- rendering

    def render(self):
        now = time.time()
        if self.alert and now >= self.alert[2]:
            self.alert = None
            self.say("start cancelled")
        if self.alert:
            return self.kitchen()
        data, hdr = self.m.build()
        head = [self.header(data, hdr, now)]
        if hdr["paused"]:
            head.append(Text(" ⏸  PAUSED — octopilot starts and acts on nothing "
                             "until resumed (P)", style="bold black on yellow"))
        # Done rows that still hold a workspace or session stay on the board,
        # so they can be opened or detached (z) without toggling D first.
        still_open = [e for e in data["done"] if e["open"]]
        self.hidden_done = 0 if self.show_done else \
            len(data["done"]) - len(still_open)
        columns = [("🟡 Running", data["running"], "yellow"),
                   ("🔴 Needs you", data["needs"], "red"),
                   ("💤 On hold", data["hold"], "blue"),
                   ("💤 Done", data["done"] if self.show_done else still_open,
                    "green")]
        # The selection state first: the footer reads it, and it needs no
        # heights, so it is settled before anything is measured.
        groups = pick_groups(data["pick"])
        self.repos = [slug for slug, _ in groups]
        self.repo_of = {e["key"]: repo_key(slug)
                        for slug, grp in groups for _, e in grp}
        # The order j/k walk and h/l jump across: board columns, then picks.
        # A collapsed repo's rows are not in it; its header is.
        pick_keys = []
        for slug, grp in groups:
            pick_keys.append(repo_key(slug))
            if slug in self.expanded:
                pick_keys += [e["key"] for _, e in grp]
        self.groups = [[e["key"] for e in rows] for _, rows, _ in columns]
        self.groups.append(pick_keys)
        # move() navigates by key, so a key listed twice would send ↓ back
        # to its first copy and trap the cursor. Keep the first of each.
        self.selectable = list(dict.fromkeys(k for g in self.groups for k in g))
        if self.sel_key not in self.selectable:
            # A row hidden by its repo's collapse falls back to that header.
            fallback = self.repo_of.get(self.sel_key)
            if fallback not in self.selectable:
                fallback = self.selectable[0] if self.selectable else None
            self.sel_key = fallback
        tail = [self.notes(data["notes"])] if data["notes"] else []
        tail += [self.activity(now), self.footer(data, now)]
        # Everything measured: the board gets what is left after the pick
        # tree's minimum, and the pick tree whatever the board did not take.
        fixed = sum(self.height(p) for p in head + tail)
        board = self.board(columns, self.console.height - fixed - PICK_MIN_LINES)
        head.append(board)
        pick_room = self.console.height - fixed - self.height(board) - 2
        pick = self.pick_tree(groups, max(PICK_MIN_LINES - 2, pick_room))
        return Group(*head, pick, *tail)

    def kitchen(self):
        """The held-start alert (wind-down or cap), alone and centered on the screen."""
        key, title, _, message, _ = self.alert
        body = Text(justify="center")
        body.append(f"{message}\n\n", style="bold red")
        body.append(f"{key}\n", style="bold")
        body.append(f"{title or ''}\n\n")
        body.append("Press g to start it anyway", style="bold yellow")
        body.append(" · any other key cancels", style="dim")
        panel = Panel(body, title="🍳 Start held", border_style="bold red",
                      box=box.DOUBLE, padding=(1, 4), expand=False)
        return Align.center(panel, vertical="middle",
                            height=self.console.height)

    def height(self, renderable):
        """Lines the renderable takes on this console."""
        return len(self.console.render_lines(renderable, self.console.options))

    def header(self, data, hdr, now):
        t = Text()
        t.append(" Octopilot ", style="bold reverse")
        t.append("  ")
        t.append(f"🔴 {len(data['needs'])} needs you", style="bold red"
                 if data["needs"] else "dim")
        t.append(" · ")
        t.append(f"🟡 {len(data['running'])} running",
                 style="yellow")
        t.append(" · ")
        t.append(f"⚪ {len(data['pick'])} to pick")
        if hdr["active"] is not None:
            t.append(f" · {hdr['active']}/{hdr['max_active']} slots")
            if hdr["untracked"]:
                t.append(f" (+{hdr['untracked']} untracked)", style="dim")
        t.append("\n ")
        gen = hdr["generated_at"]
        engine = self.m.engine_status(now)
        if engine["state"] == "acting":
            t.append("⏭ acting", style="cyan")
        elif engine["state"] == "stale":
            t.append(f"engine running {ago(engine['since_s'])}+ (hung?)",
                     style="red")
        elif self.m.snap_running:
            t.append("GitHub ⟳ refreshing", style="cyan")
        elif gen:
            age = now - gen
            style = "green" if age < REFRESH_SECS * 1.5 else \
                "yellow" if age < REFRESH_SECS * 4 else "red"
            t.append(f"GitHub {ago(age)} ago", style=style)
        else:
            t.append("GitHub: no snapshot yet", style="yellow")
        if self.m.snap_err:
            t.append(f" ({self.m.snap_err})", style="red")
        t.append("  ·  last tick ")
        t.append(ago(now - hdr["last_tick"]) + " ago" if hdr["last_tick"]
                 else "never", style="dim")
        label = {"on": "engine on", "plan": "engine plan (logs only)",
                 "off": "engine off here"}[engine["mode"]]
        t.append(f"  ·  {label}", style="dim")
        t.append(f"  ·  {time.strftime('%H:%M:%S')}", style="dim")
        return t

    def board(self, columns, budget):
        """Four equal columns, always all shown: cards move, columns don't.
        A column too tall for budget lines shows the cards around the selected
        one and a "+N more" line for the rest."""
        width = self.card_width()
        cells = []
        for title, rows, color in columns:
            bodies = [self.card_body(e, width) for e in rows]
            sel = next((i for i, e in enumerate(rows)
                        if e["key"] == self.sel_key), None)
            a, b = self.card_window([len(x) + 2 for x in bodies], budget - 1,
                                    sel)
            cards = [self.card(e, color, x)
                     for e, x in zip(rows[a:b], bodies[a:b])]
            if b - a < len(rows):
                cards.append(Text(f"+{len(rows) - (b - a)} more", style="dim"))
            cells.append(Group(Text(f"{title} ({len(rows)})", no_wrap=True,
                                    overflow="ellipsis", style=f"bold {color}"),
                               *(cards or [Text("—", style="dim")])))
        # A one-cell gap between columns: the narrowest is card_width() + 4.
        grid = Table.grid(expand=True)
        row = []
        for i, cell in enumerate(cells):
            if i:
                grid.add_column(width=1)
                row.append("")
            grid.add_column(ratio=1)
            row.append(cell)
        grid.add_row(*row)
        return grid

    def card_window(self, heights, limit, sel):
        """[a, b) of the cards shown: all when they fit in limit lines, else
        the ones around the selected card that fit beside a "+N more" line."""
        if sum(heights) <= limit:
            return 0, len(heights)
        budget = limit - 1
        a, b = (sel, sel + 1) if sel is not None else (0, 0)
        used = heights[a] if sel is not None else 0
        grew = True
        while grew:
            grew = False
            if b < len(heights) and used + heights[b] <= budget:
                used += heights[b]
                b += 1
                grew = True
            if a > 0 and used + heights[a - 1] <= budget:
                a -= 1
                used += heights[a]
                grew = True
        return a, b

    def card_width(self):
        """Text width inside a card: the narrowest board column less the
        card's borders and padding."""
        return max(4, (self.console.width - 3) // 4 - 4)

    def two_lines(self, text, width, style=""):
        """At most two lines of text, measured in cells, the second ellipsised."""
        lines = list(Text(text).wrap(self.console, width)) or [Text("")]
        if len(lines) > 2:
            rest = Text(" ".join(line.plain for line in lines[1:]))
            rest.truncate(width, overflow="ellipsis")
            lines = [lines[0], rest]
        if style:
            for line in lines:
                line.stylize(style)
        return lines

    def card_body(self, e, width):
        """A card's lines inside its border: title, status, note."""
        title = ("[draft] " if e["draft"] else "") + (e["title"] or "")
        status = Text(no_wrap=True, overflow="ellipsis")
        if e["agent"]:
            g, st = AGENT_GLYPH.get(e["agent"], ("?", "dim"))
            status.append(g + " ", style=st)
        status.append(e["kind"].lower())
        if e["checks"] in CI_GLYPH:
            g, st = CI_GLYPH[e["checks"]]
            status.append(f" {g}", style=st)
        if e["rounds"] is not None or e["driver"] == "agent":
            status.append(f" {e['rounds'] or 0}/{MAX_REVIEW_ROUNDS}")
        stalled = e["stalled"] or e["kind"] == "STALLED"
        status.append(f" {ago(e['quiet'])}", style="red" if stalled else "")
        return (self.two_lines(title, width) + [status]
                + self.two_lines(e["note"] or "", width, "dim"))

    def card(self, e, color, body):
        sel = e["key"] == self.sel_key
        # A stack's card shows its bottom PR and the layers above: #2022 +8.
        layers = e.get("stack") or 0
        name = short(e["key"]) + (f" +{layers - 1}" if layers > 1 else "")
        return Panel(Group(*body), title=Text(name, style="bold"),
                     title_align="left", box=box.HEAVY if sel else box.ROUNDED,
                     border_style=f"bold {color}" if sel else color,
                     style="bold" if sel else "none", padding=(0, 1),
                     expand=True)

    def pick_tree(self, groups, limit):
        """Pick next as a tree by repo. A collapsed repo is its header alone,
        with a summary of what it holds."""
        rows = [e for _, grp in groups for _, e in grp]
        nw = len(str(len(rows)))
        rw = max((len(pick_ref(e["key"])) for e in rows), default=0)
        kw = max((len(e["pick_kind"]) for e in rows), default=0)
        width = self.console.width
        lines = []   # (Text, key, is a repo header, is an open repo's header)
        for slug, grp in groups:
            is_open = slug in self.expanded
            hdr = repo_key(slug)
            lines.append((self.repo_line(slug, grp, is_open, hdr == self.sel_key,
                                         width), hdr, True, is_open))
            if is_open:
                for pos, (i, e) in enumerate(grp):
                    lines.append((self.pick_line(i + 1, e, pos == len(grp) - 1,
                                                 nw, rw, kw, width),
                                  e["key"], False, False))
        # Scroll only once the selection falls below the window. An open
        # header is aimed past itself, so its first row is on screen with it.
        sel = next((p for p, line in enumerate(lines)
                    if line[1] == self.sel_key), None)
        aim = sel + 1 if sel is not None and lines[sel][3] else sel
        start = aim - limit + 1 if aim is not None and aim >= limit else 0
        end = min(start + limit, len(lines))
        if start and not lines[start][2]:
            # Never open mid-repo: the repo's header takes the top row.
            head = max(p for p in range(start) if lines[p][2])
            shown = [head] + list(range(start + 1, end))
        else:
            shown = list(range(start, end))
        if len(shown) > 1 and lines[shown[-1]][3] and shown[-1] != sel:
            shown.pop()   # an open header with none of its rows under it
        out = [lines[p][0] for p in shown]
        # Hidden rows: each item, and each collapsed repo's header (it stands
        # for the rows under it). An open header is not counted: its rows are.
        hidden = sum(1 for p, line in enumerate(lines)
                     if p not in shown and not (line[2] and line[3]))
        if hidden:
            out.append(Text(f"+{hidden} more (↑/↓ to scroll)", style="dim"))
        return Group(Text(f"⚪ Pick next ({len(rows)})", style="bold"), *out)

    def repo_line(self, slug, grp, is_open, sel, width):
        line = Text()
        line.append("▶ " if sel else "  ")
        line.append("▾ " if is_open else "▸ ")
        line.append(slug.rsplit("/", 1)[-1], style="bold")
        line.append(f"  {len(grp)}", style="dim")
        if not is_open:
            kinds = collections.Counter(e["pick_kind"] for _, e in grp)
            line.append("  " + " · ".join(f"{n} {k}" for k, n in kinds.items()),
                        style="dim")
        line.truncate(width - 1, overflow="ellipsis")
        if sel:
            line.stylize("reverse")
        return line

    def pick_line(self, n, e, last, nw, rw, kw, width):
        sel = e["key"] == self.sel_key
        # Marker, connector, number, ref and kind keep fixed columns; the
        # title takes the middle, cut by cell width so wide glyphs fit.
        fixed = 2 + 3 + nw + 2 + rw + 2
        room = max(1, width - 1 - fixed - 2 - kw)
        title = Text(e["title"] or "")
        title.truncate(room, overflow="ellipsis", pad=True)
        line = Text()
        line.append("▶ " if sel else "  ")
        line.append("└─ " if last else "├─ ", style="dim")
        line.append(f"{n:>{nw}}  ", style="dim")
        line.append(f"{pick_ref(e['key']):<{rw}}  ", style="bold")
        line.append(title)
        line.append("  ")
        line.append(e["pick_kind"].ljust(kw), style="dim")
        if sel:
            line.stylize("reverse")
        return line

    def notes(self, rows):
        t = Text(" notes: ", style="dim")
        for e in rows:
            t.append(f"{e['kind'].lower()} {short(e['key'])}  ", style="dim")
        return t

    def activity(self, now):
        tb = Table(title="Activity", title_justify="left", title_style="bold dim",
                   box=None, expand=True, show_header=False, pad_edge=False)
        tb.add_column(no_wrap=True, style="dim")
        tb.add_column(no_wrap=True)
        tb.add_column(no_wrap=True, style="bold")
        tb.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        rows = activity_rows(self.m)
        if not rows:
            tb.add_row("", Text("no events yet", style="dim"), "", "")
        for r in rows:
            style = {"report": "magenta", "spawn": "cyan", "cleanup": "green",
                     "capped": "red"}.get(r["kind"], "")
            tb.add_row(time.strftime("%H:%M", time.localtime(r["at"])),
                       Text(r["kind"], style=style), r["key"], r["detail"])
        return tb

    def footer(self, data, now):
        msg, at = self.flash
        t = Text()
        if self.confirm and now < self.confirm[2]:
            t.append(f" {self.confirm[3]} ", style="bold black on yellow")
            t.append("\n")
        elif msg and now - at < 6:
            t.append(f" {msg}", style="bold cyan")
            t.append("\n")
        # The row's own decisions first, then what works everywhere.
        sel = next((e for rows in data.values() for e in rows
                    if e["key"] == self.sel_key), None)
        kind = sel["kind"] if sel else None
        items = (self.m.state or {}).get("items") or {}
        tracked = sel and sel["key"] in items
        repo = self.sel_repo()
        acts = {a["id"] for a in row_actions(self.m, sel)} if sel else set()
        keys = []
        if repo:
            keys.append(("spc", "fold" if repo in self.expanded else "open"))
        elif self.sel_key in self.repo_of:
            keys.append(("spc", "fold"))
        keys += [("g/n", "go/no")] if kind == "PROPOSE" else []
        if kind == "CAPPED":
            keys.append(("R", "another round"))
        if "rereview" in acts:
            keys.append(("e", "re-review"))
        if "ack" in acts:
            keys.append(("x", "ack"))
        if tracked:
            keys.append(("z", "detach"))
            if kind not in ("MERGED", "RESTACK"):
                keys.append(("del/⌫", "remove"))
        if acts & {"hold", "unhold"}:
            keys.append(("d", "resume" if "unhold" in acts else "hold"))
        if not repo:
            keys += [("v", "agent"), ("t", "shell"), ("o", "browser")]
        keys += [("i", "investigate"), ("a", "prompter"),
                 ("r", "refresh"),
                 ("D", "hide done" if self.show_done else
                  f"show done (+{self.hidden_done} hidden)" if self.hidden_done
                  else "show done"),
                 ("C", "open all" if self.any_collapsed() else "fold all"),
                 ("P", "pause"), ("q", "quit")]
        for k, what in keys:
            t.append(f" {k}", style="bold")
            t.append(f" {what} ", style="dim")
        return t

    # -- actions

    def selected(self):
        data, _ = self.m.build()
        for section in data.values():
            for e in section:
                if e["key"] == self.sel_key:
                    return e
        return None

    def sel_repo(self):
        """The slug of the selected pick repo's header, else None."""
        if self.sel_key and self.sel_key.startswith(REPO_PREFIX):
            return self.sel_key[len(REPO_PREFIX):]
        return None

    def any_collapsed(self):
        return any(slug not in self.expanded for slug in self.repos)

    def toggle_all(self):
        """C: expand every repo if any is collapsed, else collapse them all."""
        if self.any_collapsed():
            self.expanded.update(self.repos)
        else:
            self.expanded.clear()

    def move(self, d):
        if not self.selectable:
            return
        try:
            i = self.selectable.index(self.sel_key)
        except ValueError:
            i = 0
        self.sel_key = self.selectable[max(0, min(len(self.selectable) - 1,
                                                  i + d))]

    def jump(self, d):
        """h/l: the first card of the previous or next non-empty column, or of
        the pick list past the last column."""
        cur = next((i for i, g in enumerate(self.groups) if self.sel_key in g),
                   None)
        if cur is None:
            return
        i = cur + d
        while 0 <= i < len(self.groups) and not self.groups[i]:
            i += d
        if 0 <= i < len(self.groups):
            self.sel_key = self.groups[i][0]

    def tree_step(self, d):
        """h/l on the pick tree act like a file tree: l opens a repo, then steps
        into it; h closes it, or from a row closes its repo and goes to the
        header. A collapsed header passes h on to the board."""
        repo = self.sel_repo()
        if d > 0:
            if repo is None:
                return
            if repo not in self.expanded:
                self.expanded.add(repo)
            else:
                # repo_of, not the last frame's pick list: a fast `ll` lands
                # here before the render that shows the repo's rows.
                self.sel_key = next((k for k, h in self.repo_of.items()
                                     if h == self.sel_key), self.sel_key)
        elif repo is not None and repo not in self.expanded:
            self.jump(-1)
        elif repo is None:
            self.expanded.discard(split_key(self.sel_key)[0])
            self.sel_key = self.repo_of[self.sel_key]
        else:
            self.expanded.discard(repo)

    def ask(self, action, key, prompt):
        """Second press of the same key within CONFIRM_SECS confirms."""
        now = time.time()
        c = self.confirm
        if c and c[0] == action and c[1] == key and now < c[2]:
            self.confirm = None
            return True
        self.confirm = (action, key, now + CONFIRM_SECS, prompt)
        return False

    def report(self, res):
        """Show an action's outcome in the footer, when it has one."""
        if res.message:
            self.say(res.message)

    def row_action(self, aid, e, live, term):
        """A row key: refused with its message, or run once a second press
        confirms it. Past wind-down, go opens the kitchen alert instead."""
        refused = row_refusal(self.m, aid, e)
        if refused:
            self.say(refused)
            return
        prompt = row_confirm(self.m, aid, e)
        if prompt and not self.ask(ROW_KEYS[aid], e["key"], prompt):
            return
        res = perform(self.m, aid, e, suspend=lambda cmd: term.suspend(
            live, cmd, cwd=e["path"]))
        if res.kitchen:
            self.hold_alert(e["key"], e["title"], res)
        else:
            self.report(res)

    def hold_alert(self, key, title, res):
        """Open the held-start alert for res, keeping the holds it shows: the
        second press confirms those and no others."""
        self.alert = (key, title, time.time() + KITCHEN_SECS, res.message,
                      res.holds)

    def handle(self, key, live, term):
        if self.alert:
            opened = self.alert[2] - KITCHEN_SECS
            if key == "g" and time.time() - opened < KITCHEN_GRACE:
                return True
            # Only g gets through the alert; anything else (q too) cancels.
            k, title, _, _, shown = self.alert
            self.alert = None
            if key == "g":
                res = go_request(self.m, k, confirmed=shown)
                # A hold that came up since the alert is asked again, not sent.
                if res.kitchen:
                    self.hold_alert(k, title, res)
                else:
                    self.report(res)
            else:
                self.say("start cancelled")
            return True
        e = self.selected()
        repo = self.sel_repo()
        if key in ("q", "\x03"):
            return False
        if key in ("j", "\x1b[B"):
            self.move(1)
        elif key in ("k", "\x1b[A"):
            self.move(-1)
        elif key in ("h", "\x1b[D", "l", "\x1b[C") and (
                repo or self.sel_key in self.repo_of):
            self.tree_step(1 if key in ("l", "\x1b[C") else -1)
        elif key in ("h", "\x1b[D"):
            self.jump(-1)
        elif key in ("l", "\x1b[C"):
            self.jump(1)
        elif key in ("\x1b[5~",):
            self.move(-10)
        elif key in ("\x1b[6~",):
            self.move(10)
        elif key == "r":
            self.m.kick.set()
            self.say("refreshing from GitHub…")
        elif key == "D":
            self.show_done = not self.show_done
        elif key == "C":
            self.toggle_all()
        elif key == "P":
            paused = bool((self.m.state or {}).get("paused"))
            verb = "resume" if paused else "pause"
            if self.ask("P", "", f"press P again to {verb} octopilot"):
                self.report(set_paused(verb))
        elif key == "a":
            self.report(open_prompter())
        elif key == "i":
            self.investigate(live, term)
        elif key in (" ", "\r", "\n") and repo:
            if repo in self.expanded:
                self.expanded.discard(repo)
            else:
                self.expanded.add(repo)
        elif key == " " and self.sel_key in self.repo_of:
            # Collapse the item's repo and put the cursor on its header.
            self.expanded.discard(split_key(self.sel_key)[0])
            self.sel_key = self.repo_of[self.sel_key]
        elif not e:
            # A repo header has no row, so the row keys have nothing to act on.
            if repo and key in ("v", "t", "o", "g", "n", "R", "e", "x", "z",
                                "d", "\x1b[3~"):
                self.say("select a row, not a repo header")
            return True
        elif key == "d":
            it = tracked_item(self.m, e["key"])
            self.row_action("unhold" if held_by_you(it) else "hold",
                            e, live, term)
        elif key in KEY_ROW:
            self.row_action(KEY_ROW[key], e, live, term)
        return True

    def investigate(self, live, term):
        """/investigate [--read-only] <repo> <question>, typed here and
        queued."""
        repo = term.read_line(live, "Investigate -- repo (owner/repo or name): ")
        question = repo and term.read_line(live, "Question: ")
        if not (repo and question):
            self.say("investigation cancelled")
            return
        ro = term.read_line(live, "Read-only (a report, no commits or PR)? "
                            "[y/N] ")
        read_only = ro.lower() in ("y", "yes")
        res = investigate_request(self.m, repo, question, read_only)
        if res.kitchen:
            held = " · ".join(res.message.splitlines())
            ok = term.read_line(live, f"{held} -- start it anyway? [y/N] ")
            if ok.lower() not in ("y", "yes"):
                self.say("investigation not started (held)")
                return
            res = investigate_request(self.m, repo, question, read_only,
                                      confirmed=res.holds)
            if res.kitchen:
                held = " · ".join(res.message.splitlines())
                self.say(f"investigation not started (held again: {held})")
                return
        self.report(res)


# --------------------------------------------------------------- terminal

def split_keys(data):
    out, i = [], 0
    while i < len(data):
        if data[i] == "\x1b" and i + 1 < len(data) and data[i + 1] in "[O":
            j = i + 2
            while j < len(data) and not (data[j].isalpha() or data[j] == "~"):
                j += 1
            out.append(data[i:j + 1])
            i = j + 1
        else:
            out.append(data[i])
            i += 1
    return out


class Terminal:
    """cbreak stdin for single-key input, restorable around child programs."""

    def __init__(self):
        self.fd = sys.stdin.fileno()
        self.saved = termios.tcgetattr(self.fd)

    def __enter__(self):
        tty.setcbreak(self.fd)
        return self

    def __exit__(self, *a):
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)

    def keys(self, timeout):
        """Keys typed since the last call. One read can carry several
        (fast typing, key repeat), so it is split into single characters
        and whole escape sequences."""
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return []
        data = os.read(self.fd, 64).decode(errors="ignore")
        return [KEY_ALIASES.get(k, k) for k in split_keys(data)]

    def read_line(self, live, prompt):
        """One line typed by the user, with the dashboard paused around it.
        Empty on Ctrl-C or Ctrl-D, which callers treat as cancel."""
        live.stop()
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        try:
            return input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            return ""
        finally:
            tty.setcbreak(self.fd)
            live.start(refresh=True)

    def suspend(self, live, cmd, cwd=None):
        live.stop()
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        try:
            subprocess.run(cmd, cwd=cwd)
        except OSError as e:
            print(f"failed: {e}")
            time.sleep(2)
        finally:
            tty.setcbreak(self.fd)
            live.start(refresh=True)


# ---------------------------------------------------------------- headless

def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


def kick_mtime():
    try:
        return os.stat(KICK_FILE).st_mtime
    except OSError:
        return 0


def write_json_atomic(path, doc):
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as fh:
        json.dump(doc, fh)
    os.replace(tmp, path)


def stable(node):
    """The view without its clock-relative fields, for the change check."""
    if isinstance(node, dict):
        return {k: stable(v) for k, v in node.items()
                if k not in VIEW_VOLATILE}
    if isinstance(node, list):
        return [stable(v) for v in node]
    return node


def headless(model):
    """dashboard.sh --headless: the TUI's loops with no screen, and the view
    written to view.json for the herdr-app mods."""
    # A plain list, not an Event: a signal handler that takes a lock the
    # interrupted thread holds would wedge the daemon.
    stopping = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda signum, _frame: stopping.append(signum))
    model.poll_files()
    model.log_engine = log
    model.start()
    log(f"headless dashboard: writing {VIEW_FILE}")
    kick_seen = kick_mtime()
    last_sig, last_write, last_err = None, 0, None
    while not stopping:
        try:
            model.poll_files()
            mtime = kick_mtime()
            if mtime != kick_seen:
                # A refresh action: skip the GitHub loop's debounce.
                kick_seen = mtime
                model.urgent.set()
                model.kick.set()
            view = view_for(model)
            sig = json.dumps(stable(view), sort_keys=True)
            if sig != last_sig or time.time() - last_write >= VIEW_EVERY:
                write_json_atomic(VIEW_FILE, view)
                last_sig, last_write = sig, time.time()
        except Exception as e:
            # Once per distinct error, not once a second while it lasts.
            if repr(e) != last_err:
                last_err = repr(e)
                log(f"view failed: {last_err}")
        # Half a second: a refresh action starts its scan within a second.
        time.sleep(0.5)
    log("headless dashboard: stopped")
    return 0


# ------------------------------------------------------------------- main

class ArgParser(argparse.ArgumentParser):
    def error(self, message):
        # --action always answers with one JSON line, usage errors included.
        if any(a == "--action" or a.startswith("--action=") for a in sys.argv):
            print(json.dumps({"ok": False, "message": message}))
            sys.exit(0)
        super().error(message)


def main():
    ap = ArgParser(
        description="Octopilot live dashboard (run it through dashboard.sh).")
    ap.add_argument("--once", action="store_true",
                    help="one frame on stdout, for checking the layout")
    ap.add_argument("--refresh", action="store_true",
                    help="with --once: refresh GitHub first")
    ap.add_argument("--headless", action="store_true",
                    help="run the loops and write view.json, no screen")
    ap.add_argument("--action", metavar="ID",
                    help="run one action and print its result as JSON")
    ap.add_argument("key", nargs="?",
                    help="with --action: the row it acts on")
    ap.add_argument("--arg", action="append", default=[], metavar="NAME=VALUE",
                    help="with --action: an argument")
    opts = ap.parse_args()
    backend = Hosts()
    model = Model(backend)

    if opts.action:
        args = {}
        for pair in opts.arg:
            name, _, value = pair.partition("=")
            args[name] = value
        try:
            model.poll_files()
            # Only the row actions and the prompter need the live agents.
            if opts.action in ROW_IDS or opts.action == "prompter":
                model.live = backend.live()
                model.sessions = backend.sessions()
            res = run_action(model, opts.action, opts.key, args)
            out = {"ok": res.ok, "message": res.message}
            if res.kitchen:
                out["kitchen"] = True
                out["holds"] = list(res.holds)
        except Exception as e:
            out = {"ok": False, "message": f"{type(e).__name__}: {e}"}
        print(json.dumps(out))
        return 0

    if opts.headless:
        return headless(model)

    if opts.once:
        # One frame on stdout, for checking the layout without a TTY.
        if opts.refresh:
            model.refresh_snapshot(force=True)
        model.poll_files()
        model.live = backend.live()
        model.sessions = backend.sessions()
        width = int(os.environ.get("COLUMNS", "160"))
        height = int(os.environ.get("LINES", "50"))
        console = Console(width=width, height=height, force_terminal=True)
        console.print(Dashboard(model, console).render())
        return 0

    if not sys.stdin.isatty():
        print("dashboard needs a terminal (or pass --once)", file=sys.stderr)
        return 1

    console = Console()
    dash = Dashboard(model, console)
    model.poll_files()
    model.start()
    with Terminal() as term, Live(dash.render(), console=console, screen=True,
                                  auto_refresh=False) as live:
        last_draw = 0
        while True:
            quit_ = False
            for k in term.keys(0.25):
                if not dash.handle(k, live, term):
                    quit_ = True
                    break
                model.changed.set()
            if quit_:
                break
            model.poll_files()
            now = time.time()
            if model.changed.is_set() or now - last_draw >= 1:
                model.changed.clear()
                live.update(dash.render(), refresh=True)
                last_draw = now
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
