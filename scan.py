#!/usr/bin/env python3
"""Octopilot reconciler.

Reads GitHub + herdr, diffs against a small state file, prints a compact table of
actionable rows. Deterministic; no model involved. engine.py acts on the rows.
"""
import copy
import fcntl
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

HOME_REPOS = os.environ.get("HOME_REPOS", "").split()
ISSUE_ORGS = os.environ.get("ISSUE_ORGS", "").split()
CLONE_ROOTS = os.environ.get("CLONE_ROOTS", "").split()
CLONE_PREFER = [os.path.expanduser(p).rstrip("/")
                for p in os.environ.get("CLONE_PREFER", "").split()]
MAX_ACTIVE = int(os.environ.get("MAX_ACTIVE", "6"))
# Local HH:MM from which ticks remind the user to close work, not start it.
WIND_DOWN_AT = os.environ.get("WIND_DOWN_AT", "").strip()
MAX_REVIEW_ROUNDS = int(os.environ.get("MAX_REVIEW_ROUNDS", "3"))
AGENT_STALL_MIN = int(os.environ.get("AGENT_STALL_MIN", "35"))
# A review-only PR whose author pushed after our review but never re-requested
# it is flagged PUSHED once its head has been unchanged this long.
RE_REVIEW_QUIET_HOURS = float(os.environ.get("RE_REVIEW_QUIET_HOURS", "4"))

# Model per spawn, keyed by the template that creates the session. Pinned onto
# the claude command line, so a worker never runs on the user's default model.
MODEL_FOR = {
    "work": os.environ.get("MODEL_WORK", "claude-opus-5-5[1m]"),
    "investigate": os.environ.get("MODEL_INVESTIGATE", "claude-fable-5-2[1m]"),
    "investigate-readonly": os.environ.get("MODEL_INVESTIGATE",
                                           "claude-fable-5-2[1m]"),
    "review-fix": os.environ.get("MODEL_REVIEW_FIX", "claude-opus-5-5[1m]"),
    "review-stack": os.environ.get("MODEL_REVIEW_COMMENT", "claude-opus-5-5[1m]"),
    "adopt-stack": os.environ.get("MODEL_REVIEW_FIX", "claude-opus-5-5[1m]"),
    "rebase-stack": os.environ.get("MODEL_REVIEW_FIX", "claude-opus-5-5[1m]"),
    "review-comment": os.environ.get("MODEL_REVIEW_COMMENT", "claude-opus-5-5[1m]"),
    "re-review": os.environ.get("MODEL_REVIEW_COMMENT", "claude-opus-5-5[1m]"),
}
# The Agent-tool aliases for the two review passes. A legacy MODEL_COLD_REVIEW
# still sets the verify pass, so an old exported value keeps working.
MODEL_REVIEW_FIND = os.environ.get("MODEL_REVIEW_FIND", "fable")
MODEL_REVIEW_VERIFY = os.environ.get(
    "MODEL_REVIEW_VERIFY", os.environ.get("MODEL_COLD_REVIEW", "opus"))
STALE_HOURS = int(os.environ.get("STALE_HOURS", "48"))
ISSUE_MAX_AGE_DAYS = int(os.environ.get("ISSUE_MAX_AGE_DAYS", "120"))
STATE_FILE = os.environ.get(
    "STATE_FILE", os.path.expanduser("~/.local/state/octopilot/state.json"))
# Both live next to the state file and exist for the dashboard: the event log
# is appended by every mutating command, the snapshot is the last scan's rows
# rendered as JSON.
EVENTS_FILE = os.path.join(os.path.dirname(STATE_FILE), "events.jsonl")
SNAPSHOT_FILE = os.path.join(os.path.dirname(STATE_FILE), "snapshot.json")
# The headless dashboard's view (dashboard.py VIEW_FILE): speech reads the
# engine's last lines from it, and a view older than VIEW_STALE_SECS is no engine.
VIEW_FILE = os.path.join(os.path.dirname(STATE_FILE), "view.json")
VIEW_STALE_SECS = 15  # three times dashboard.py's VIEW_EVERY
# One tick at a time: a tick that finds it held returns at once.
TICK_LOCK = os.path.join(os.path.dirname(STATE_FILE), "tick.lock")
# Held by each engine phase that acts (engine.run_rows), taken after the state
# lock is released: its steps take that lock one at a time.
EXEC_LOCK = os.path.join(os.path.dirname(STATE_FILE), "exec.lock")
EVENTS_MAX_BYTES = 512 * 1024
EVENTS_KEEP = 1000
# Backend new sessions spawn on: "herdr" (the TUI) or "hc" (herdr-app's GUI).
OCTOPILOT_BACKEND = os.environ.get("OCTOPILOT_BACKEND", "herdr")
# Settle a new worker tab at spawn ("1") or leave it as herdr opened it ("0").
SETTLE_WORKER_TABS = os.environ.get("SETTLE_WORKER_TABS", "1") == "1"
# The octopilot code dir: where workers find scan.sh and their plugin.
OCTOPILOT_DIR = (os.environ.get("OCTOPILOT_DIR")
                 or os.path.dirname(os.path.abspath(__file__)))
# herdr-app's terminal running the headless dashboard (open-gui.sh), which writes
# VIEW_FILE.
HEADLESS_AGENT = "octopilot-headless"
# herdr agent in the "Octopilot (code changes)" tab (herdr/open.sh): changes
# and fixes to octopilot itself go there.
CODE_AGENT = "octopilot-code"
# The name herdr/open.sh gives the Octopilot workspace (the TUI's host).
OCTOPILOT_LABEL = "Octopilot"

AGENT_MARKER = "controlled by elboletaire"
FEEDBACK_IGNORE = set(
    (os.environ.get("FEEDBACK_IGNORE_AUTHORS") or "github-actions").split())

EMPTY = {"paused": False, "registry": {}, "declined": [], "handled_merges": [],
         "items": {}, "me": None}


# ---------------------------------------------------------------- utilities

def sh(args, cwd=None, timeout=60):
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        return 1, "", str(e)


def gh_json(args, cwd=None, default=None):
    code, out, _ = sh(["gh"] + args, cwd=cwd)
    if code != 0 or not out:
        return default
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return default


def load():
    try:
        with open(STATE_FILE) as fh:
            s = json.load(fh)
    except (OSError, json.JSONDecodeError):
        s = {}
    out = dict(EMPTY)
    out.update(s)
    for k, v in EMPTY.items():
        out.setdefault(k, v)
    return out


def save(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = f"{STATE_FILE}.tmp.{os.getpid()}"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, STATE_FILE)


@contextmanager
def locked():
    """Exclusive lock for the whole read-modify-write of the state file.

    save() is atomic on its own, but every mutating command does
    load() -> mutate -> save(); without this, two concurrent writers both read
    the same counter and the second silently discards the first's increment.
    The counter that gets lost is review_rounds -- the cap itself. Readers
    (scan/status) do not take the lock.
    """
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    fh = open(STATE_FILE + ".lock", "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def log_event(kind, key_=None, **fields):
    """Append one line to the event log the dashboard tails.

    Called from mutating commands, which all hold the state lock, so the
    trim below cannot race another writer. A failed write never fails the
    command: the log is a convenience, the state file is the truth.
    """
    ev = {"at": int(time.time()), "kind": kind}
    if key_:
        ev["key"] = key_
    ev.update({k: v for k, v in fields.items() if v is not None})
    try:
        os.makedirs(os.path.dirname(EVENTS_FILE), exist_ok=True)
        with open(EVENTS_FILE, "a") as fh:
            fh.write(json.dumps(ev, sort_keys=True) + "\n")
        if os.path.getsize(EVENTS_FILE) > EVENTS_MAX_BYTES:
            with open(EVENTS_FILE) as fh:
                keep = fh.readlines()[-EVENTS_KEEP:]
            tmp = f"{EVENTS_FILE}.tmp.{os.getpid()}"
            with open(tmp, "w") as fh:
                fh.writelines(keep)
            os.replace(tmp, EVENTS_FILE)
    except OSError:
        pass


def whoami(state):
    if not state.get("me"):
        state["me"] = gh_json(["api", "user", "--jq", ".login"], default=None) \
            or sh(["gh", "api", "user", "--jq", ".login"])[1]
    return state["me"]


# --------------------------------------------------------------- agent hosts
#
# An item's session id names its backend: a TUI workspace (`w12`) or a herdr-app
# space (`sp<hex>`), or a tab in either (`w12:t3`, `tb<hex>`), since it survives
# restarts that pane ids and names may not.
# New items spawn on OCTOPILOT_BACKEND; a bare agent name is the spawn backend's.

HERDR_WS = re.compile(r"^w[0-9A-Za-z]{1,8}$")
HC_SPACE = re.compile(r"^sp[0-9a-f]+$")
HERDR_TAB = re.compile(r"^w[0-9A-Za-z]{1,8}:t[0-9A-Za-z]{1,8}$")
HC_TAB = re.compile(r"^tb[0-9a-f]+$")
# Agent states in the vocabulary the scan logic speaks.
HERDR_STATE = {"working": "running", "blocked": "waiting", "idle": "idle",
               "done": "idle", "unknown": "unknown"}
HC_STATE = {"working": "running", "blocked": "waiting", "error": "waiting",
            "idle": "idle", "done": "idle"}


def cli(args, timeout=60):
    """-> (code, parsed JSON or None, raw text). The CLIs print results on
    stdout and errors as JSON on stderr."""
    code, out, err = sh(args, timeout=timeout)
    data = None
    for text in (out, err):
        try:
            data = json.loads(text)
            break
        except (json.JSONDecodeError, TypeError):
            continue
    return code, data, (err or out)


def herdr(args, timeout=60):
    return cli(["herdr"] + args, timeout)


def hc(args, timeout=60):
    return cli(["hc"] + args, timeout)


def app_do(command, args, timeout=60):
    """`herdr-app do <command>`: the GUI's spaces and focus."""
    return cli(["herdr-app", "do", command] + args, timeout)


def error_code(data):
    """The error's code (herdr) or kind (hc), or ''."""
    err = (data or {}).get("error") or {}
    if not isinstance(err, dict):
        return err or ""
    return err.get("code") or err.get("kind") or ""


def gather(fn):
    """fn(backend) for every backend in parallel, so a hung core cannot stall the other."""
    with ThreadPoolExecutor(max_workers=len(BACKENDS)) as ex:
        return list(ex.map(fn, BACKENDS.values()))


def owned_worktree(wt, repo):
    """Whether wt is a worktree of repo: the only checkouts whose trust dialog
    octopilot answers."""
    return os.path.realpath(wt).startswith(os.path.realpath(repo) + os.sep)


def live_map(agents, states, backend):
    """-> {id: entry}, each agent under every id it carries (its space or
    workspace, and its tab); on a shared id the ap-* agent wins."""
    out = {}
    for a in agents:
        mine = (a.get("agent") or "").startswith("ap-")
        for ws in a.get("ids") or [a.get("session")]:
            if not ws or (ws in out and not mine):
                continue
            out[ws] = {"session": ws, "agent": a.get("agent"), "pane": a.get("pane"),
                       "backend": backend,
                       "state": states.get(a.get("status"), "unknown")}
    return out


def session_matches(agent, session):
    """Whether a backend's agent is in session: by its ids when it has them."""
    ids = agent.get("ids")
    if ids:
        return session in ids
    return agent.get("session") == session


def worktree_of(folder):
    """-> (checkout, main repo, branch) when folder is a linked worktree, else
    None."""
    code, out, _ = sh(["git", "rev-parse", "--path-format=absolute",
                       "--show-toplevel", "--git-dir", "--git-common-dir"],
                      cwd=folder, timeout=10)
    if code != 0:
        return None
    top, gitdir, common = out.splitlines()
    if gitdir == common:
        return None
    _, branch, _ = sh(["git", "branch", "--show-current"], cwd=top)
    return top, os.path.dirname(common), branch


def accept_trust(backend, target, wt, repo):
    """Answer claude's "do you trust this folder?" for a worktree of one of our
    own clones, and nothing else. -> True once the agent is idle."""
    if not owned_worktree(wt, repo):
        return False
    _, screen = backend.read(target)
    if "trust this folder" not in screen:
        return False
    # The dialog defaults to "No, exit": move to "Yes" before confirming.
    backend.press(target, ["down", "enter"])
    code, _ = backend.wait_idle(target)
    return code == 0


class HerdrBackend:
    """The herdr TUI, through its `herdr` CLI. A session is a workspace."""
    name = "herdr"
    prefix = "herdr:"
    noun = "herdr workspace"
    remove_label = "herdr worktree remove"

    def workspaces(self):
        """Every open workspace, worktree or not; None when herdr could not be
        asked."""
        if not shutil.which("herdr"):
            return []
        code, data, _ = herdr(["workspace", "list"])
        if code != 0 or not data:
            return None
        return (data.get("result") or {}).get("workspaces", [])

    def sessions(self, workspaces, tabs, panes, launcher_panes):
        """Linked-worktree workspaces as [{id,title,path,backend,worktree}], then
        the worker tabs on a worktree, with their workspace as "space". The panes
        of launcher_panes (the launcher's agents) are no worker."""
        if workspaces is None or tabs is None or panes is None:
            return None
        by_repo = {}
        linked = {}  # workspace id -> its checkout
        for w in workspaces:
            wt = w.get("worktree") or {}
            if wt.get("is_linked_worktree") and wt.get("repo_root"):
                by_repo.setdefault(wt["repo_root"], []).append((w, wt))
                linked[w["workspace_id"]] = wt.get("checkout_path")
        out = []
        for repo, rows in by_repo.items():
            code, d, _ = herdr(["worktree", "list", "--cwd", repo])
            listed_ = ((d or {}).get("result") or {}).get("worktrees", []) \
                if code == 0 else []
            branch = {x.get("open_workspace_id"): x.get("branch") for x in listed_}
            for w, wt in rows:
                out.append({"id": w["workspace_id"], "title": w.get("label"),
                            "path": wt.get("checkout_path"), "backend": self.name,
                            "worktree": {"branch": branch.get(w["workspace_id"]),
                                         "main_repo_path": repo}})
        labels = {t.get("tab_id"): t.get("label") for t in tabs}
        checked, seen = {}, set()
        for p in panes:
            tab, cwd, ws = p.get("tab_id"), p.get("cwd"), p.get("workspace_id")
            if tab in seen or not cwd or p.get("pane_id") in launcher_panes:
                continue
            if cwd not in checked:
                checked[cwd] = worktree_of(cwd)
            if not checked[cwd]:
                continue
            path, repo, branch = checked[cwd]
            # A linked workspace's own checkout is already its row.
            own = linked.get(ws)
            if own and os.path.realpath(own) == os.path.realpath(path):
                continue
            seen.add(tab)
            out.append({"id": tab, "title": labels.get(tab), "path": path,
                        "backend": self.name, "space": p.get("workspace_id"),
                        "worktree": {"branch": branch or None,
                                     "main_repo_path": repo}})
        return out

    def tabs(self):
        """Every open tab, or None when herdr could not be asked."""
        return self.result_list(["tab", "list"], "tabs")

    def panes(self):
        """Every open pane, or None when herdr could not be asked."""
        return self.result_list(["pane", "list"], "panes")

    def result_list(self, args, key):
        if not shutil.which("herdr"):
            return []
        code, data, _ = herdr(args)
        if code != 0 or not data:
            return None
        return (data.get("result") or {}).get(key, [])

    def agents(self):
        """[{session, agent, pane, status, ids}] for every herdr agent; ids are
        its workspace and tab."""
        if not shutil.which("herdr"):
            return []
        code, data, _ = herdr(["agent", "list"])
        if code != 0 or not data:
            return []
        return [{"session": a.get("workspace_id"), "agent": a.get("name"),
                 "pane": a.get("pane_id"), "status": a.get("agent_status"),
                 "ids": [a.get("workspace_id"), a.get("tab_id")]}
                for a in (data.get("result") or {}).get("agents", [])]

    def listing(self):
        """-> (worktree sessions, answered, open session ids)."""
        workspaces, tabs, panes = self.workspaces(), self.tabs(), self.panes()
        launchers = {a["pane"] for a in self.agents()
                     if a["agent"] in (HEADLESS_AGENT, CODE_AGENT, PROMPTER_AGENT)}
        rows = self.sessions(workspaces, tabs, panes, launchers)
        ids = {w.get("workspace_id") for w in workspaces or []} \
            | {t.get("tab_id") for t in tabs or []}
        return rows or [], rows is not None, ids

    def live(self):
        return live_map(self.agents(), HERDR_STATE, self.name)

    def target(self, it):
        """The live agent for an item: its name when that still resolves, else
        whichever agent now occupies its workspace."""
        name = it.get("agent")
        if name and herdr(["agent", "get", name])[0] == 0:
            return name
        for a in self.agents():
            if session_matches(a, it.get("session")):
                return a["agent"] or a["pane"]
        return None

    def send(self, target, text):
        code, data, raw = herdr(["agent", "prompt", target, text], timeout=120)
        return (True, "") if code == 0 else (False, error_code(data) or raw)

    def read(self, target):
        """-> (ok, visible screen, or the error)."""
        code, out, err = sh(["herdr", "agent", "read", target, "--source", "visible"])
        return code == 0, (out if code == 0 else err)

    def press(self, target, keys):
        herdr(["agent", "send-keys", target, *keys])

    def wait_idle(self, target):
        code, _, raw = herdr(["agent", "wait", target, "--until", "idle",
                              "--timeout", "60000"], timeout=90)
        return code, raw

    def focus(self, session, target, timeout=60):
        """The agent's pane, else the tab or workspace the session is."""
        if target:
            args = ["agent", "focus", target]
        elif is_tab(session):
            args = ["tab", "focus", session]
        else:
            args = ["workspace", "focus", session]
        code, _, raw = herdr(args, timeout=timeout)
        return code == 0, raw

    def rename(self, entry, name):
        return herdr(["agent", "rename", entry["pane"], name])[0] == 0

    def close(self, session):
        """Close the tab or the workspace."""
        noun = "tab" if is_tab(session) else "workspace"
        code, _, raw = herdr([noun, "close", session])
        return code, raw

    def discard(self, session, wt):
        """Remove the tab or workspace and its worktree -> (code, message)."""
        if is_tab(session):
            # herdr's worktree remove takes only a workspace: git removes the checkout.
            wt = wt or self.checkout_of(session)
            code, data, raw = herdr(["tab", "close", session])
            # A tab already gone leaves its checkout behind: remove that.
            if code != 0 and error_code(data) != "tab_not_found":
                return code, raw
            code, raw = 0, ""
            if wt and os.path.isdir(wt):
                code, out, err = sh(["git", "worktree", "remove", "--force", wt],
                                    cwd=wt, timeout=180)
                raw = err or out
            return code, raw
        code, _, raw = herdr(["worktree", "remove", "--workspace", session,
                              "--force"], timeout=180)
        if code != 0 and wt and os.path.isdir(wt):
            # The workspace is already gone; the checkout may not be.
            code, out, err = sh(["git", "worktree", "remove", "--force", wt],
                                cwd=wt, timeout=180)
            raw = err or out
        return code, raw

    def checkout_of(self, session):
        """The worktree path of a worker tab, or None."""
        rows, _, _ = self.listing()
        return next((r["path"] for r in rows if r["id"] == session), None)

    def spawn(self, key_, path, wt, title, model):
        """A tab of the Octopilot workspace on the worktree when WORKER_HOST is tab,
        else a workspace; either runs a named claude agent."""
        tab = worker_host() == "tab"
        ws = octopilot_workspace() if tab else None
        if ws:
            return self.spawn_tab(ws, key_, path, wt, title, model)
        note = "no Octopilot workspace: the worker opens as its own workspace" \
            if tab else None
        code, data, raw = herdr(["worktree", "open", "--cwd", path, "--path", wt,
                                 "--label", title, "--no-focus"])
        if code != 0:
            raise SystemExit(f"herdr worktree open failed: {raw}")
        res = data["result"]
        ws, pane = res["workspace"]["workspace_id"], res["root_pane"]["pane_id"]
        name = agent_name(key_)
        host = {"backend": self.name, "session": ws, "agent": name, "worktree": wt}
        warning = self.start_agent(pane, name, path, wt, model, f"workspace {ws}")
        if warning:
            host["warning"] = warning
        return add_note(host, note) if note else host

    def spawn_tab(self, ws, key_, path, wt, title, model):
        """A tab of workspace ws on the worktree, its root pane running claude."""
        name = agent_name(key_)
        # The tab inherits herdr's environment, not this one: pass the octopilot settings.
        argv = ["tab", "create", "--workspace", ws, "--cwd", wt,
                "--label", tab_label(title, name), "--no-focus",
                "--env", f"OCTOPILOT_DIR={OCTOPILOT_DIR}",
                "--env", f"OCTOPILOT_STATE_DIR={os.path.dirname(STATE_FILE)}"]
        code, data, raw = herdr(argv, timeout=90)
        if code != 0:
            raise SystemExit(f"herdr tab create failed: {raw}")
        res = data["result"]
        tab, pane = res["tab"]["tab_id"], res["root_pane"]["pane_id"]
        host = {"backend": self.name, "session": tab, "space": ws, "agent": name,
                "worktree": wt}
        warning = self.start_agent(pane, name, path, wt, model, f"tab {tab}")
        if warning:
            host["warning"] = warning
        return host

    def start_agent(self, pane, name, path, wt, model, where):
        """Start the claude agent name in pane -> its startup warning, or None.
        where names the host in the messages; a failed start raises SystemExit."""
        args = ["agent", "start", name, "--kind", "claude", "--pane", pane,
                "--timeout", "60000"]
        if model:
            # argv straight to claude, no shell: a [1m] suffix needs no quoting here.
            args += ["--", "--model", model]
        # The pane's shell may still be starting; agent start needs its prompt.
        for attempt in range(3):
            code, data, raw = herdr(args, timeout=90)
            if code == 0 or error_code(data) == "agent_not_ready":
                break
            time.sleep(2)
        if code == 0:
            return None
        if error_code(data) == "agent_not_ready":
            if accept_trust(self, name, wt, path):
                return None
            return (f"agent {name} is blocked at startup; check {where} "
                    "before sending it a prompt")
        raise SystemExit(f"herdr agent start failed ({where} kept): {raw}")


class HcBackend:
    """herdr-app's GUI, its terminals run by herdr-core (`hc`). A session is a
    space; an agent is a terminal labelled octopilot.agent=<name> at spawn."""
    name = "hc"
    prefix = "hc:"
    noun = "herdr-app space"
    remove_label = "herdr-app space remove"

    def spaces(self):
        """Every space herdr-app shows, on any machine; None when herdr-app is
        missing or not answering, so nothing is taken for closed."""
        if not shutil.which("herdr-app"):
            return None
        code, data, _ = cli(["herdr-app", "ls"], timeout=10)
        if code != 0 or not data:
            return None
        return data.get("spaces", [])

    def sessions(self, spaces):
        """Linked-worktree spaces, and the tabs of other local spaces that run in
        a linked worktree (a worker's), shaped like HerdrBackend.sessions()."""
        if spaces is None:
            return None
        checkouts = {}  # worktree_of() once per folder in this scan

        def checkout(folder):
            if folder not in checkouts:
                checkouts[folder] = worktree_of(folder)
            return checkouts[folder]
        cwds = None
        out = []
        for sp in spaces:
            # A space on another machine has a folder this one cannot see.
            if sp.get("machine"):
                continue
            wt = checkout(sp["folder"]) if sp.get("folder") else None
            if wt:
                out.append(self.session_row(sp["id"], sp.get("name"), sp["id"], wt))
                continue
            if cwds is None:
                cwds = self.terminal_cwds()
            for tab in sp.get("tabs", []):
                row = self.tab_row(sp, tab, cwds, checkout)
                if row:
                    out.append(row)
        return out

    def session_row(self, session, title, space, wt):
        """A session row for a checkout: wt is worktree_of()'s (path, repo, branch)."""
        path, repo, branch = wt
        return {"id": session, "title": title, "path": path, "backend": self.name,
                "space": space,
                "worktree": {"branch": branch or None, "main_repo_path": repo}}

    def terminal_cwds(self):
        """{terminal: cwd} for the terminals hc lists, finished ones too (a tab
        whose claude exited keeps its session), {} when it cannot say."""
        code, data, _ = hc(["ls", "--all", "--json"], timeout=10)
        if code != 0 or not data:
            return {}
        return {t["address"]: t.get("cwd") for t in data.get("terminals", [])}

    def tab_row(self, space, tab, cwds, checkout):
        """The row of a tab with a terminal in a linked worktree, or None. The
        launcher's terminals (octopilot.dir) are not a worker's."""
        for t in tab.get("terminals", []):
            folder = cwds.get(t.get("terminal"))
            wt = checkout(folder) if folder else None
            if not wt:
                continue
            code, info, _ = hc(["get", t["terminal"], "--json"], timeout=10)
            if code != 0 or not isinstance(info, dict):
                continue  # labels unknown: not a worker's either
            if (info.get("labels") or {}).get("octopilot.dir"):
                continue
            return self.session_row(tab["id"], tab.get("name"), space["id"], wt)
        return None

    def agents(self):
        """[{session, ids, agent, pane, status}] for every terminal running an agent.
        The agent's name is its octopilot.agent label, else the terminal's name."""
        code, data, _ = hc(["ls", "--json"], timeout=10)
        if code != 0 or not data:
            return []
        out = []
        for t in data.get("terminals", []):
            if not t.get("agent"):
                continue
            _, info, _ = hc(["get", t["address"], "--json"], timeout=10)
            info = info or {}
            labels = info.get("labels") or {}
            out.append({"session": labels.get("herdr.space"),
                        "ids": [labels[k] for k in ("herdr.space", "herdr.tab")
                                if labels.get(k)],
                        "agent": labels.get("octopilot.agent") or info.get("name"),
                        "pane": t["address"], "status": t.get("status")})
        return out

    def listing(self):
        """-> (worktree sessions, answered, open session ids)."""
        spaces = self.spaces()
        rows = self.sessions(spaces)
        return rows or [], rows is not None, open_ids(spaces or [])

    def live(self):
        return live_map(self.agents(), HC_STATE, self.name)

    def target(self, it):
        """The terminal of an item's agent: the one labelled with its name, else
        any agent in its space."""
        found = [a for a in self.agents() if session_matches(a, it.get("session"))]
        for a in found:
            if it.get("agent") and a["agent"] == it["agent"]:
                return a["pane"]
        return found[0]["pane"] if found else None

    def find(self, name):
        """-> "hc:<terminal>" of the live terminal labelled octopilot.agent=<name>,
        or None. This checkout's terminal first, else one with no octopilot.dir
        label (a worker's); another checkout's is never picked."""
        code, data, _ = hc(["ls", "--json", "--label", f"octopilot.agent={name}"],
                           timeout=10)
        terms = (data or {}).get("terminals", []) if code == 0 else []
        unlabelled = None
        for t in terms:
            code, info, _ = hc(["get", t["address"], "--json"], timeout=10)
            if code != 0 or not isinstance(info, dict):
                continue  # labels unknown: not ours, and not a worker's either
            d = (info.get("labels") or {}).get("octopilot.dir")
            if not d:
                unlabelled = unlabelled or t["address"]
            elif os.path.realpath(d) == os.path.realpath(OCTOPILOT_DIR):
                return f"hc:{t['address']}"
        return f"hc:{unlabelled}" if unlabelled else None

    def status(self, target):
        _, info, _ = hc(["get", target, "--json"])
        return ((info or {}).get("status") or {}).get("state")

    def send(self, target, text):
        # `--` keeps text that starts with a dash from reading as a flag.
        code, data, raw = hc(["send", target, "--", text], timeout=120)
        if code == 9 or error_code(data) == "blocked":
            return False, "agent_blocked"
        return (True, "") if code == 0 else (False, error_code(data) or raw)

    def read(self, target):
        """-> (ok, visible screen, or the error)."""
        code, data, raw = hc(["read", target, "--json"])
        if code != 0 or data is None:
            return False, raw
        return True, data.get("text", "")

    def press(self, target, keys):
        hc(["keys", target, *keys])

    def wait_idle(self, target):
        code, _, raw = hc(["wait", target, "--timeout", "60"], timeout=90)
        return code, raw

    def focus(self, session, target=None, timeout=60):
        """Show the session's space, then its tab, then the target agent."""
        steps = [("space.select", ["--id", session])]
        if is_tab(session):
            spaces = self.spaces()
            if spaces is None:
                return False, "herdr-app did not answer"
            space = next((sp["id"] for sp in spaces for t in sp.get("tabs", [])
                          if t.get("id") == session), None)
            if space is None:
                return False, f"{session} is not open in herdr-app"
            steps = [("space.select", ["--id", space]),
                     ("tab.select", ["--id", session])]
        if target:
            steps.append(("agent.focus", ["--terminal", target]))
        code, raw = 0, ""
        for command, args in steps:
            code, _, raw = app_do(command, args, timeout=timeout)
            if code != 0:
                break
        return code == 0, raw

    def rename(self, entry, name):
        return hc(["label", entry["pane"], f"octopilot.agent={name}"])[0] == 0

    def close(self, session):
        """Close the session's tab, or its space."""
        command = "tab.close" if is_tab(session) else "space.close"
        code, _, raw = app_do(command, ["--id", session, "--confirmed", "true"])
        return code, raw

    def discard(self, session, wt):
        """Close the space or tab, then remove its worktree -> (code, message). A
        tracked item has none on record: its space's worktree stands in, and
        when the space list is unreadable the worktree is kept."""
        spaces = self.spaces()
        if not wt:
            wt = self.checkout_of(spaces, session)
        code, raw = self.close(session)
        # A closed tab is checked against the list too, whatever close said.
        if code != 0 or is_tab(session):
            gone = self.gone(session, TAB_GONE_TRIES if is_tab(session) else 1)
            if gone is None:
                return code or 1, "herdr-app did not answer: its worktree is kept"
            if not gone:
                # Still open: its worktree is still in use.
                return code or 1, raw or "still open: its worktree is kept"
            code, raw = 0, ""
        if not wt and spaces is None:
            spaces = self.spaces()
            if spaces is None:
                return 1, "space closed, worktree kept: its path is unknown"
            wt = self.checkout_of(spaces, session)
        if wt and os.path.isdir(wt):
            code, out, err = sh(["git", "worktree", "remove", "--force", wt],
                                cwd=wt, timeout=180)
            raw = err or out
        return code, raw

    def gone(self, session, tries):
        """True once herdr-app no longer lists the session, False while it still
        does after `tries` looks, None when the list cannot be read. A closed tab
        can linger in the list a moment."""
        for attempt in range(tries):
            spaces = self.spaces()
            if spaces is None:
                return None
            if session not in open_ids(spaces):
                return True
            if attempt < tries - 1:
                time.sleep(0.5)
        return False

    def checkout_of(self, spaces, session):
        """The worktree path of a space, or None."""
        return next((s["path"] for s in self.sessions(spaces) or []
                     if s["id"] == session), None)

    def spawn(self, key_, path, wt, title, model):
        """A worker's tab in the Octopilot space, else a space of its own."""
        if worker_host() == "tab":
            sp = octopilot_space()
            if sp:
                return self.spawn_tab(sp, key_, path, wt, title, model)
            return add_note(
                self.spawn_space(key_, path, wt, title, model),
                "no Octopilot space open; the worker gets a space of its own")
        return self.spawn_space(key_, path, wt, title, model)

    def spawn_command(self, model):
        """The argv of a worker's terminal: a named claude on this octopilot."""
        # The terminal inherits herdr-core's login environment, not this one,
        # so the octopilot settings the worker needs travel in its argv.
        env = ["OCTOPILOT_BACKEND=hc", f"OCTOPILOT_DIR={OCTOPILOT_DIR}",
               f"OCTOPILOT_STATE_DIR={os.path.dirname(STATE_FILE)}"]
        # A worktree's octopilot replaces the installed plugin through
        # --settings: --plugin-dir would load a second copy, and the installed
        # one wins.
        settings = plugin_settings()
        plugin = ["--settings", settings] if settings else []
        return (["env"] + env + ["claude"] + plugin
                + (["--model", model] if model else []))

    def spawn_space(self, key_, path, wt, title, model):
        """A space on the worktree, its first terminal running a named claude."""
        argv = json.dumps(self.spawn_command(model))
        code, data, raw = app_do("space.new", ["--name", title, "--folder", wt,
                                               "--argv", argv, "--focus", "false"],
                                 timeout=90)
        if code != 0 or not data or "terminal" not in data:
            raise SystemExit(f"herdr-app space.new failed: {raw}")
        ws, term = data["space"], data["terminal"]
        name = agent_name(key_)
        warning = self.start_agent(term, wt, path, name, f"space {ws}")
        return self.host_of(ws, name, wt, warning)

    def spawn_tab(self, sp, key_, path, wt, title, model):
        """A tab of the Octopilot space on the worktree, its terminal running a
        named claude. A reserved tab name gets the agent's name after it."""
        name = agent_name(key_)
        argv = json.dumps(self.spawn_command(model))
        code, data, raw = app_do("tab.new", ["--space", sp, "--name",
                                             tab_label(title, name),
                                             "--folder", wt, "--argv", argv,
                                             "--focus", "false"], timeout=90)
        if code != 0 or not data or "terminal" not in data:
            raise SystemExit(f"herdr-app tab.new failed: {raw}")
        term = data["terminal"]
        tab = data.get("tab") if isinstance(data.get("tab"), str) else self.tab_of(term)
        if not tab:
            raise SystemExit(f"herdr-app tab.new: {term} has no tab label (tab kept)")
        warning = self.start_agent(term, wt, path, name, f"tab {tab}")
        if SETTLE_WORKER_TABS:
            code, _, raw = app_do("tab.settle", ["--id", tab])
            if code != 0:
                print(f"note: tab {tab} not settled: {raw}")
        host = self.host_of(tab, name, wt, warning)
        host["space"] = sp
        return host

    def tab_of(self, term):
        """The tab of a terminal, from its herdr.tab label once herdr-core sets it."""
        for _ in range(30):
            _, info, _ = hc(["get", term, "--json"], timeout=10)
            tab = ((info or {}).get("labels") or {}).get("herdr.tab")
            if tab:
                return tab
            time.sleep(0.5)
        return None

    def start_agent(self, term, wt, path, name, where):
        """Label the terminal as the named agent, answer its trust dialog and wait
        for it to settle -> the warning when it is blocked, else None. where
        names the host in messages."""
        code, _, raw = hc(["label", term, f"octopilot.agent={name}"])
        if code != 0:
            raise SystemExit(f"hc label failed ({where} kept): {raw}")
        # claude draws its trust dialog a moment after it starts: keep looking
        # for it for a while, then wait for the agent to settle.
        for _ in range(30):
            if accept_trust(self, term, wt, path):
                break
            if self.status(term) in ("idle", "blocked"):
                break
            time.sleep(1)
        self.wait_idle(term)
        if self.status(term) == "blocked":
            return (f"agent {name} is blocked at startup; check {where} "
                    "before sending it a prompt")
        return None

    def host_of(self, session, name, wt, warning):
        """The host a spawn returns: the session, its agent and checkout."""
        host = {"backend": self.name, "session": session, "agent": name,
                "worktree": wt}
        if warning:
            host["warning"] = warning
        return host


# Looks at herdr-app's list for a closed tab, half a second apart.
TAB_GONE_TRIES = 5


def open_ids(spaces):
    """Every space and tab id herdr-app lists, settled tabs included."""
    return ({sp.get("id") for sp in spaces}
            | {t.get("id") for sp in spaces for t in sp.get("tabs", [])})


HERDR = HerdrBackend()
HC = HcBackend()
# Both are listed on every scan; a session id belongs to exactly one of them.
BACKENDS = {"herdr": HERDR, "hc": HC}
# Every label a discard is logged under, so a failed removal is recognised.
REMOVE_LABELS = (HERDR.remove_label, HC.remove_label, "herdr tab remove",
                 "herdr-app tab remove")


def backend_for(session):
    """The backend a session id belongs to, or None."""
    if HERDR_WS.match(session or "") or HERDR_TAB.match(session or ""):
        return HERDR
    if HC_SPACE.match(session or "") or HC_TAB.match(session or ""):
        return HC
    return None


def is_tab(session):
    """True for a worker's tab id, not for a whole workspace or space."""
    return bool(HERDR_TAB.match(session or "") or HC_TAB.match(session or ""))


def short_noun(session):
    """What a detach or park leaves open, short: a tab, else the workspace."""
    return "tab" if is_tab(session) else "workspace"


def host_noun(backend, session):
    """What the session's host is called in messages: a tab for a tab id."""
    if HC_TAB.match(session or ""):
        return "herdr-app tab"
    if HERDR_TAB.match(session or ""):
        return "herdr tab"
    return backend.noun


def remove_label_for(backend, session):
    """The command a discard of the session is logged under, by its host."""
    if HC_TAB.match(session or ""):
        return "herdr-app tab remove"
    if HERDR_TAB.match(session or ""):
        return "herdr tab remove"
    return backend.remove_label


def spawn_backend():
    backend = BACKENDS.get(OCTOPILOT_BACKEND)
    if backend is None:
        raise SystemExit(f"OCTOPILOT_BACKEND must be herdr or hc, not "
                         f"{OCTOPILOT_BACKEND!r}")
    return backend


def own_labels(agent):
    """The labels of this checkout's terminal running agent, or None: only a
    terminal labelled with this checkout's dir, as open-gui.sh's is_mine."""
    code, data, _ = hc(["ls", "--json", "--label", f"octopilot.agent={agent}"],
                       timeout=10)
    for t in (data or {}).get("terminals", []) if code == 0 else []:
        code, info, _ = hc(["get", t["address"], "--json"], timeout=10)
        if code != 0 or not isinstance(info, dict):
            continue
        labels = info.get("labels") or {}
        d = labels.get("octopilot.dir")
        if d and os.path.realpath(d) == os.path.realpath(OCTOPILOT_DIR):
            return labels
    return None


def octopilot_space():
    """The GUI's Octopilot space: that of the headless engine, the code agent or
    the prompter (their herdr.space label, else octopilot.space once parked),
    when this computer lists it. None when none is found."""
    local = {s.get("id") for s in HC.spaces() or [] if not s.get("machine")}
    for label in (HEADLESS_AGENT, CODE_AGENT, PROMPTER_AGENT):
        labels = own_labels(label) or {}
        space = labels.get("herdr.space") or labels.get("octopilot.space")
        if space in local:
            return space
    return None


def octopilot_workspace():
    """The TUI's Octopilot workspace (labelled as herdr/open.sh does), else the
    one the prompter runs in. None when neither is found."""
    for w in HERDR.workspaces() or []:
        if w.get("label") == OCTOPILOT_LABEL:
            return w.get("workspace_id")
    code, data, _ = herdr(["agent", "get", PROMPTER_AGENT])
    if code != 0:
        return None
    return (((data or {}).get("result") or {}).get("agent") or {}).get("workspace_id")


def worker_host():
    """Where worker sessions open, read at use: "tab" or "space"."""
    host = os.environ.get("WORKER_HOST", "tab")
    if host not in ("tab", "space"):
        raise SystemExit(f"WORKER_HOST must be tab or space, not {host!r}")
    return host


# The launcher's tab titles: keep in step with MAIN_TAB, CODE_TAB, ENGINE_TAB in herdr/open*.sh.
LAUNCHER_TAB_TITLES = ("OCTOPILOT", "engine", "Octopilot (code changes)")


def tab_label(title, name):
    """A worker tab's label: its spawn title, with the agent's name after it when
    the title is a launcher tab's (case-insensitively)."""
    if title.lower() in {t.lower() for t in LAUNCHER_TAB_TITLES}:
        return f"{title} ({name})"
    return title


def add_note(host, note):
    """The host with note: what the spawn did instead of what was asked, not a
    startup problem, so it never holds back the first prompt."""
    host["note"] = note
    return host


def split_address(address):
    """-> (backend, target) for "hc:t7" or "herdr:<agent>". A bare name belongs
    to the spawn backend: under hc, the terminal labelled with it."""
    for backend in (HC, HERDR):
        if address.startswith(backend.prefix):
            return backend, address[len(backend.prefix):]
    if OCTOPILOT_BACKEND == "hc":
        found = HC.find(address)
        return HC, found[len(HC.prefix):] if found else address
    return HERDR, address


def all_sessions():
    """-> (sessions, answered backend names, open session ids). A session missing
    from a listing counts as closed only when its own backend answered."""
    rows, answered, ids = [], set(), set()
    for name, (r, ok, open_ids) in gather(lambda b: (b.name, b.listing())):
        rows += r
        ids |= open_ids
        if ok:
            answered.add(name)
    return rows, answered, ids


def listed(answered, session):
    """Whether the backend holding session answered the listing."""
    backend = backend_for(session)
    return backend is not None and backend.name in answered


def all_live():
    out = {}
    for live in gather(lambda b: b.live()):
        out.update(live)
    return out


def all_agents():
    return [a for backend in BACKENDS.values() for a in backend.agents()]


def parked_agent(it, session):
    """The hc agent of an item whose session is a herdr-app tab or space that is
    gone, when herdr-app parked the agent's terminal (no space or tab left, its
    claude still running). None otherwise."""
    if backend_for(session) is not HC or not it.get("agent"):
        return None
    return next((a for a in HC.agents()
                 if a.get("agent") == it["agent"] and not a.get("ids")), None)


def session_age(s, live):
    """Seconds since the session did anything: the last commit on the
    worktree's branch stands in."""
    code, out, _ = sh(["git", "log", "-1", "--format=%ct"], cwd=s.get("path"))
    if code == 0 and out.isdigit():
        return int(time.time()) - int(out)
    return 0


def address_of(it):
    backend = backend_for(it.get("session"))
    target = backend.target(it) if backend else None
    return f"{backend.prefix}{target}" if target else None


def send_to(address, text):
    """-> (ok, error code or message)."""
    backend, target = split_address(address)
    return backend.send(target, text)


def sid(session_id):
    return (session_id or "")[:8]


# ------------------------------------------------------------------ registry

def resolve_repo(path):
    info = gh_json(["repo", "view", "--json", "nameWithOwner,defaultBranchRef"],
                   cwd=path)
    if not info:
        return None
    slug = info.get("nameWithOwner")
    default = (info.get("defaultBranchRef") or {}).get("name") or "main"
    code, _, _ = sh(["git", "rev-parse", "--verify", "--quiet",
                     "refs/remotes/origin/develop"], cwd=path)
    base = "develop" if code == 0 else default
    return slug, base


NEW_IGNORED = []


def clone_dirs():
    """Git clones directly under each CLONE_ROOTS folder. Only a real `.git`
    directory counts: a worktree's `.git` is a file, so worktrees are skipped.

    A symlink to a clone that is already listed is the same clone: it is
    dropped, or `gh repo view` would re-probe it on every tick."""
    found = []
    for root in CLONE_ROOTS:
        root = os.path.expanduser(root)
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for n in names:
            p = os.path.join(root, n)
            if os.path.isdir(os.path.join(p, ".git")):
                found.append(p)
    keep, seen = set(), set()
    # Real directories first, so the symlink is the one dropped.
    for p in sorted(found, key=os.path.islink):
        real = os.path.realpath(p)
        if real not in seen:
            seen.add(real)
            keep.add(p)
    return [p for p in found if p in keep]


def build_registry(old, refresh=False, old_ignored=None):
    """slug -> {path, base}, derived from the clones on disk.

    Folders that do not resolve on GitHub (no remote, no access) are ignored
    and memoised so they are not re-probed every tick; `refresh` clears the
    memo. When two clones resolve to the same slug, a CLONE_PREFER path wins,
    else the first in alphabetical order.
    """
    reg = {} if refresh else dict(old)
    known = {v["path"] for v in reg.values()}
    memo = set() if refresh else set(old_ignored or [])
    todo = [p for p in clone_dirs() if p not in known and p not in memo]
    if todo:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for p, res in zip(todo, ex.map(resolve_repo, todo)):
                if not res:
                    NEW_IGNORED.append(p)
                    continue
                slug, base = res
                cur = reg.get(slug)
                if cur and cur["path"].rstrip("/") in CLONE_PREFER:
                    continue
                if cur and p.rstrip("/") not in CLONE_PREFER \
                        and cur["path"] < p:
                    continue
                reg[slug] = {"path": p, "base": base}
    return reg


# ----------------------------------------------------------------- discovery

PR_FIELDS = ("number,headRefName,headRefOid,state,isDraft,baseRefName,"
             "author,assignees,url,title,mergedAt")


def home_prs(slug):
    rows = gh_json(["pr", "list", "-R", slug, "--state", "open", "--limit", "50",
                    "--json", PR_FIELDS], default=[]) or []
    for r in rows:
        r["repo"] = slug
    return rows


def search_prs(me, *query_flags):
    rows = gh_json(["search", "prs", *query_flags, "--state", "open",
                    "--archived=false", "--limit",
                    "60", "--json",
                    "number,repository,title,author,assignees,url"],
                   default=[]) or []
    for r in rows:
        r["repo"] = (r.get("repository") or {}).get("nameWithOwner")
    return [r for r in rows if r.get("repo")]


def cutoff():
    import datetime
    d = datetime.date.today() - datetime.timedelta(days=ISSUE_MAX_AGE_DAYS)
    return d.isoformat()


def home_issues(slug, me):
    out = {}
    fresh = f"updated:>={cutoff()}"
    for args in (["--assignee", me, "--search", fresh],
                 ["--author", me, "--search", f"no:assignee {fresh}"]):
        rows = gh_json(["issue", "list", "-R", slug, "--state", "open",
                        "--limit", "50", "--json", "number,title,url"] + args,
                       default=[]) or []
        for r in rows:
            r["repo"] = slug
            out[r["number"]] = r
    return list(out.values())


def org_issues(me):
    rows = []
    for org in ISSUE_ORGS:
        got = gh_json(["search", "issues", "--assignee", me, "--state", "open",
                       "--owner", org, "--archived=false",
                       "--updated", f">={cutoff()}",
                       "--limit", "60", "--json",
                       "number,repository,title,url"], default=[]) or []
        for r in got:
            r["repo"] = (r.get("repository") or {}).get("nameWithOwner")
        rows += [r for r in got if r.get("repo")]
    return rows


def open_heads(slug):
    rows = gh_json(["pr", "list", "-R", slug, "--state", "open", "--limit",
                    "100", "--json", "headRefName"], default=[]) or []
    return [r["headRefName"] for r in rows]


def pr_detail(slug, number):
    d = gh_json(["pr", "view", str(number), "-R", slug, "--json",
                 "number,state,mergedAt,headRefOid,headRefName,baseRefName,"
                 "statusCheckRollup,isDraft,url,title,reviews,comments,"
                 "reviewRequests"],
                default=None)
    return d


def last_own_review(detail, me):
    """Commit the user's latest submitted review was made at, or None.

    GitHub keeps the commit of every review, so this survives a detach or a
    lost state.json. A thread reply is an empty COMMENTED review at the head
    commit; counting it would skip commits nobody reviewed."""
    own = [r for r in (detail or {}).get("reviews") or []
           if (r.get("author") or {}).get("login") == me
           and r.get("submittedAt")
           and (r.get("state") in ("APPROVED", "CHANGES_REQUESTED")
                or (r.get("state") == "COMMENTED"
                    and (r.get("body") or "").strip()))]
    if not own:
        return None
    last = max(own, key=lambda r: r["submittedAt"])
    return (last.get("commit") or {}).get("oid") or None


def review_requested(detail, me):
    """Whether `me` has a review request pending on the PR in `detail`."""
    return me in [(r.get("login") or r.get("name"))
                  for r in (detail or {}).get("reviewRequests") or []]


def pr_review_comments(detail):
    """REST review comments for the PR in `detail` (gh pr view has none)."""
    url = (detail or {}).get("url") or ""
    m = re.search(r"github\.com/([^/]+/[^/]+)/pull/(\d+)", url)
    if not m:
        return []
    return gh_json(["api", f"repos/{m.group(1)}/pulls/{m.group(2)}/comments"
                    "?per_page=100"], default=[]) or []


def new_feedback(detail, since):
    """Reviews/comments newer than `since` that octopilot did not itself write.

    Author alone cannot identify the agent: it posts with the user's token, so
    its comments are authored by the user. The signature line the prompt
    library mandates is the discriminator."""
    out = []
    since = since or ""
    review_comments = None
    for r in (detail or {}).get("reviews") or []:
        when = r.get("submittedAt") or ""
        body = r.get("body") or ""
        author = (r.get("author") or {}).get("login", "?")
        # FEEDBACK_IGNORE has to cover reviews as well as comments: a CI bot
        # that reviews on every push would otherwise reset the review-round
        # counter forever and defeat MAX_REVIEW_ROUNDS.
        if when <= since or AGENT_MARKER in body or author in FEEDBACK_IGNORE:
            continue
        state_ = r.get("state") or ""
        if state_ not in ("CHANGES_REQUESTED", "COMMENTED", "APPROVED"):
            continue
        # A reply on a review thread arrives as an empty-body COMMENTED
        # review; the agent's signature is then only in the inline comment.
        # Without this check the agent's own reply re-triggers FEEDBACK every
        # tick, forever. Review comments are fetched once per PR, lazily.
        if not body.strip() and state_ == "COMMENTED":
            if review_comments is None:
                review_comments = pr_review_comments(detail)
            mine = [c for c in review_comments
                    if (c.get("created_at") or "") == when]
            if mine and all(AGENT_MARKER in (c.get("body") or "")
                            for c in mine):
                continue
        # A bare approval is "ship it", not feedback. Only an approval that
        # carries an actual note is worth waking the agent for.
        if state_ == "APPROVED" and not body.strip():
            continue
        out.append((author, when, r.get("state", "REVIEW"), body[:60]))
    for c in (detail or {}).get("comments") or []:
        when = c.get("createdAt") or ""
        body = c.get("body") or ""
        author = (c.get("author") or {}).get("login", "?")
        if when <= since or AGENT_MARKER in body or author in FEEDBACK_IGNORE:
            continue
        out.append((author, when, "comment", body[:60]))
    return sorted(out, key=lambda x: x[1])


def worst_checks(details):
    """checks_of over several PRs: the worst result wins."""
    rank = {"fail": 0, "pending": 1, "none": 2, "pass": 3}
    return min((checks_of(d) for d in details), key=rank.get)


def checks_of(detail):
    roll = (detail or {}).get("statusCheckRollup") or []
    if not roll:
        return "none"
    states = set()
    for c in roll:
        states.add(c.get("conclusion") or c.get("state") or "")
    if {"FAILURE", "TIMED_OUT", "CANCELLED", "ERROR", "ACTION_REQUIRED"} & states:
        return "fail"
    if {"PENDING", "IN_PROGRESS", "QUEUED", ""} & states:
        return "pending"
    return "pass"


# ---------------------------------------------------------------------- mode

def mode_for(pr_row, me):
    """'fix' for PRs you own, 'comment' for everyone else's.

    You own a PR when you opened it, or when it is assigned to you (as
    assignee, not reviewer) -- the explicit signal to adopt someone else's
    PR. On any other PR the useful output is a review they can act on, not
    commits pushed over their work. The repo does not change this.
    """
    author = (pr_row.get("author") or {}).get("login")
    assignees = [a.get("login") for a in (pr_row.get("assignees") or [])]
    return "fix" if author == me or me in assignees else "comment"


# ---------------------------------------------------------------------- main

SEP = {"pr": "#", "issue": "!", "investigate": "?"}


def key(slug, num, kind="pr"):
    return f"{slug}{SEP[kind]}{num}"


# A stack item's `stack` is its layers bottom to top: [{pr, branch}], the
# bottom included. Each layer keeps its own feedback watermark, in layer_seen.

def layer_since(it, pr):
    """Feedback watermark of one layer; a layer with no mark of its own starts
    from the item's feedback_seen, as a plain PR does."""
    return (it.get("layer_seen") or {}).get(str(pr)) or it.get("feedback_seen")


def stack_feedback(it, detail_of):
    """The first layer, bottom up, with feedback past its watermark, as
    (layer, feedback); (None, []) when no layer has any."""
    for layer in it["stack"]:
        fb = new_feedback(detail_of(layer["pr"]), layer_since(it, layer["pr"]))
        if fb:
            return layer, fb
    return None, []


def closed_layer(layers, detail_of):
    """The first of `layers` that GitHub reports CLOSED (not merged), or None."""
    for layer in layers:
        if (detail_of(layer["pr"]) or {}).get("state") == "CLOSED":
            return layer
    return None


def stack_rest(it, detail_of):
    """Drop the merged layers from the bottom of a stack -> (merged, left).

    Several can merge between two ticks, so all of them are dropped."""
    left, merged = list(it["stack"]), []
    while left and (detail_of(left[0]["pr"]) or {}).get("state") == "MERGED":
        merged.append(left.pop(0))
    return merged, left


def boot_template(it, d, me):
    """The review prompt a booting item gets: -> (template, prev), prev the sha
    a re-review covers from. A reviewed comment item gets a follow-up on the
    author's new commits, not a second full review."""
    prev = it.get("reviewed_sha")
    if it["mode"] == "comment" and not prev:
        prev = last_own_review(d, me)
    if it.get("stack") and it["mode"] == "comment":
        return "review-stack", None
    if it["mode"] == "comment" and prev:
        return "re-review", prev
    if it["mode"] == "comment":
        return "review-comment", None
    return "review-fix", None


def rehome_of(it, sessions, live, open_ws, held):
    """-> (session, space) of the open tab or space the item's agent is live in,
    or None. A session in held is another item's: it is never taken."""
    name = it.get("agent")
    rows = {s["id"]: s for s in sessions}
    homes = [sess for sess, a in live.items()
             if name and a.get("agent") == name and sess in open_ws
             and sess in rows and sess not in held and sess != it.get("session")]
    if not homes:
        return None
    home = next((h for h in homes if is_tab(h)), homes[0])
    return home, rows[home].get("space")


def scan(state, refresh=False, info=None):
    """Reconcile and return the table rows.

    `info`, when given, is filled with what the rows only summarise -- full
    titles, URLs and the tracked PRs' details -- for the dashboard snapshot.
    """
    me = whoami(state)
    sessions, answered, open_ws = all_sessions()
    live = all_live()
    reg = build_registry(state.get("registry", {}), refresh,
                         state.get("ignored_paths", []))
    state["registry"] = reg
    state["ignored_paths"] = sorted(
        (set() if refresh else set(state.get("ignored_paths", [])))
        | set(NEW_IGNORED))

    path_to_slug = {v["path"].rstrip("/"): k for k, v in reg.items()}
    session_paths = {s["id"]: s.get("path") for s in sessions}

    # branch -> session, per repo path
    by_branch = {}
    for s in sessions:
        wt = s.get("worktree")
        if not wt:
            continue
        mp = (wt.get("main_repo_path") or "").rstrip("/")
        br = wt.get("branch")
        if mp and br:
            by_branch.setdefault((mp, br), s)

    declined = set(state.get("declined", []))
    handled = set(state.get("handled_merges", []))
    items = state.get("items", {})

    # ---- gather (parallel)
    jobs = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        for slug in HOME_REPOS:
            jobs[("prs", slug)] = ex.submit(home_prs, slug)
            if slug in reg:
                jobs[("iss", slug)] = ex.submit(home_issues, slug, me)
        jobs[("rr", "*")] = ex.submit(search_prs, me, f"--review-requested={me}")
        jobs[("as", "*")] = ex.submit(search_prs, me, f"--assignee={me}")
        # Your own PRs nobody was assigned to are yours to drive too.
        jobs[("mine", "*")] = ex.submit(search_prs, me, f"--author={me}",
                                        "--no-assignee")
        jobs[("oi", "*")] = ex.submit(org_issues, me)
        tracked = {k: ex.submit(pr_detail, k.split("#")[0], v["pr"])
                   for k, v in items.items() if v.get("pr")}
        # Every layer of a stack, not just its bottom: feedback and merges are
        # checked per layer.
        layer_jobs = {(k, layer["pr"]): ex.submit(pr_detail, k.split("#")[0],
                                                  layer["pr"])
                      for k, v in items.items() if v.get("pr")
                      for layer in v.get("stack") or []
                      if layer["pr"] != v["pr"]}
        results = {k: f.result() for k, f in jobs.items()}
        details = {k: f.result() for k, f in tracked.items()}
        layer_details = {kp: f.result() for kp, f in layer_jobs.items()}

    def layer_detail(k, it, pr):
        """GitHub's view of one layer of a tracked stack item."""
        return details.get(k) if pr == it.get("pr") else layer_details.get((k, pr))

    meta = {}
    # Revivals are reported, not logged, here: snapshot() runs this on a
    # copy and must not write the event log. dispatch() logs them on save.
    revived = []
    # Re-homed items, as (key, session): the tick logs them, as it does revivals.
    rehomed = []
    if info is not None:
        info["details"] = details
        info["meta"] = meta
        info["revived"] = revived
        info["rehomed"] = rehomed

    rows = []
    active = 0

    # ---- tracked items
    # Every item's session: a re-home never takes one of them.
    held = {it.get("session") for it in items.values()}
    for k, it in list(items.items()):
        slug = k.split("#")[0].split("!")[0].split("?")[0]
        d = details.get(k)
        sess = it.get("session")
        smeta = f"session={sid(sess)}"

        # A session closed by hand: an item whose agent is live elsewhere follows
        # it (rehome_of); otherwise it is untracked and frees its slot. A merged
        # PR still goes through MERGED below. Only when its backend answered:
        # silence is not removal.
        if sess and sess not in open_ws \
                and listed(answered, sess) and not (
                it.get("pr") and d and d.get("state") == "MERGED"):
            home = rehome_of(it, sessions, live, open_ws, held)
            term = parked_agent(it, sess) if home is None else None
            if term:
                # herdr-app parked its terminal: no tab to follow, but the claude runs.
                active += 1
                rows.append(("WORKING", k, f"mode={it['mode']} {smeta} parked in "
                                           "herdr-app with no tab: `herdr-app do "
                                           f"terminal.keep --terminal {term['pane']}` "
                                           "shows it"))
                continue
            if home is None:
                items.pop(k)
                rows.append(("GONE", k, f"{smeta} removed outside octopilot "
                                        "-> untracked, slot freed"))
                continue
            # Its agent is live in another tab or space: the item follows it.
            sess, space = home
            held.add(sess)
            it["session"], it["backend"] = sess, backend_for(sess).name
            if space:
                it["space"] = space
            else:
                it.pop("space", None)
            smeta = f"session={sid(sess)}"
            rehomed.append((k, sess))

        if it.get("pr") and d and d.get("state") == "MERGED":
            # A fix stack whose bottom merged, with layers still open, is
            # restacked, not cleaned up. Comment stacks are never restacked:
            # nothing of theirs is pushed to.
            if it.get("stack") and it["mode"] == "fix":
                merged, left = stack_rest(it, lambda pr: layer_detail(k, it, pr))
                cl = closed_layer(left, lambda pr: layer_detail(k, it, pr))
                if cl:
                    rows.append(("CLOSED", k, f"branch={it.get('branch')} "
                                              f"{smeta} layer=#{cl['pr']} -> not "
                                              f"merged; bottom=#{merged[0]['pr']} "
                                              "merged, fan-out pending"))
                    continue
                if left:
                    gone = ",".join(f"#{m['pr']}" for m in merged)
                    rows.append(("RESTACK", k,
                                 f"merged={gone} next=#{left[0]['pr']} "
                                 f"branch={left[0]['branch']} {smeta}"))
                    continue
            if k not in handled:
                rows.append(("MERGED", k, f"branch={it.get('branch')} {smeta}"))
            continue

        if it.get("pr") and d and d.get("state") == "CLOSED":
            rows.append(("CLOSED", k, f"branch={it.get('branch')} {smeta} "
                                      "-> not merged; session left alone"))
            continue

        if it.get("stack") and it["mode"] == "fix":
            cl = closed_layer(it["stack"], lambda pr: layer_detail(k, it, pr))
            if cl:
                rows.append(("CLOSED", k, f"branch={it.get('branch')} {smeta} "
                                          f"layer=#{cl['pr']} -> not merged; "
                                          "session left alone"))
                continue

        if it.get("phase") == "booting":
            active += 1
            # A reboot whose wait timed out leaves a busy agent: a BOOTING row
            # would have the engine prompt it mid-task.
            if live.get(sess, {}).get("state") == "running":
                rows.append(("WORKING", k, f"mode={it['mode']} {smeta} "
                                           "booting, agent not idle yet"))
                continue
            tmpl, prev = boot_template(it, d, me)
            tmpl = f"template={tmpl}" + (f" PREV={prev}" if tmpl == "re-review" else "")
            rows.append(("BOOTING", k, f"mode={it['mode']} {smeta} {tmpl}"))
            continue

        if not it.get("pr"):
            # issue-driven work, PR not opened yet
            repo_path = reg.get(slug, {}).get("path", "").rstrip("/")
            # The agent may have switched its worktree to another branch and
            # opened the PR from there; the branch recorded at spawn/track time
            # would then never match. Follow the worktree's live branch.
            wt_path = session_paths.get(sess)
            if wt_path and os.path.isdir(wt_path):
                code, cur, _ = sh(["git", "branch", "--show-current"],
                                  cwd=wt_path)
                if code == 0 and cur and cur != it.get("branch"):
                    it["branch"] = cur
            found = None
            for pr in results.get(("prs", slug), []) or []:
                if pr["headRefName"] == it.get("branch"):
                    found = pr
                    break
            if found is None and repo_path:
                # --state all: a PR opened and merged between two ticks must
                # still be found, or its session lingers as WORKING forever.
                got = gh_json(["pr", "list", "-R", slug, "--state", "all",
                               "--head", it.get("branch", ""), "--json",
                               PR_FIELDS], default=[]) or []
                found = got[0] if got else None
            if found:
                it["pr"] = found["number"]
                nk = key(slug, found["number"])
                # The agent was handed the issue key in its prompt and will
                # keep using it for claim-round/heartbeat; remember it so those
                # calls still resolve after the re-key.
                it.setdefault("prev_keys", []).append(k)
                items[nk] = items.pop(k)
                # The list row has no checks; without the detail the new PR
                # shows with no title or CI until the next scan.
                d = pr_detail(slug, found["number"])
                if d:
                    details[nk] = d
                if found.get("state") == "MERGED":
                    if nk not in handled:
                        rows.append(("MERGED", nk, f"branch={it.get('branch')} {smeta}"))
                    continue
                if found.get("state") == "CLOSED":
                    rows.append(("CLOSED", nk, f"branch={it.get('branch')} {smeta} "
                                               "-> not merged; session left alone"))
                    continue
                active += 1
                # An agent-driven item runs its own review loop; a REVIEW row
                # here would tell the engine to reboot it mid-flight.
                if it.get("driver") == "agent":
                    rows.append(("WORKING", nk,
                                 f"branch={it.get('branch')} {smeta} driver=agent "
                                 f"rounds={int(it.get('review_rounds', 0))}/"
                                 f"{MAX_REVIEW_ROUNDS} quiet=0m"))
                else:
                    rows.append(("REVIEW", nk,
                                 f"mode={it['mode']} {smeta} head={found['headRefOid'][:7]} "
                                 f"reviewed=none"))
                continue
            st = live.get(sess, {}).get("state", "?")
            age = live.get(sess, {}).get("age_secs")
            if age is None:
                age = int(time.time()) - int(it.get("added") or time.time())
            active += 1
            rows.append(("WORKING", k, f"branch={it.get('branch')} {smeta} "
                                       f"state={st} age={age // 60}m"))
            continue

        # tracked open PR
        head = (d or {}).get("headRefOid", "")
        if not d:
            rows.append(("UNKNOWN", k, f"{smeta} -> gh pr view failed"))
            continue
        # A card parked with `d` holds no slot. It is still tracked, so its
        # merge is still seen, and new activity brings it back.
        if it.get("parked"):
            revive = None
            if it["mode"] == "fix":
                if it.get("stack"):
                    fb = stack_feedback(it, lambda pr: layer_detail(k, it, pr))[1]
                else:
                    fb = new_feedback(d, it.get("feedback_seen"))
                if fb:
                    revive = "new feedback"
            elif review_requested(d, me) and (
                    not it["parked"].get("requested")
                    or head != it["parked"].get("head")):
                revive = ("review requested" if not it["parked"].get("requested")
                          else "new commits, review still requested")
            if revive:
                it.pop("parked")
                revived.append((k, revive))
            else:
                rows.append(("DONE", k, f"mode={it['mode']} {smeta} parked by you "
                                        "-> waiting on merge or new activity"))
                continue
        active += 1
        rv = it.get("reviewed_sha") or ""

        # A human (or review bot) leaving a review outranks everything below:
        # it is explicit instruction, where REVIEW is only octopilot's own
        # second guess. Items tracked before feedback_seen existed get it
        # stamped now, so pre-existing reviews do not stampede on first scan.
        if it["mode"] == "fix":
            if "feedback_seen" not in it:
                it["feedback_seen"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                    time.gmtime())
            if it.get("stack"):
                layer, fb = stack_feedback(it, lambda pr: layer_detail(k, it, pr))
            else:
                layer, fb = None, new_feedback(d, it.get("feedback_seen"))
            if fb:
                who, when, kind, snippet = fb[-1]
                # since= is the newest feedback's timestamp: mark-feedback
                # needs it to move the watermark past this feedback.
                where = f" pr={layer['pr']} branch={layer['branch']}" \
                    if layer else ""
                rows.append(("FEEDBACK", k,
                             f"mode=fix {smeta}{where} since={when} "
                             f"{who} {kind} \"{snippet}\""))
                continue

        rounds = int(it.get("review_rounds", 0))
        # A stack's agent reports its top layer's head, so READY and REVIEW
        # compare that one. Without its detail the item stays WORKING.
        top = layer_detail(k, it, it["stack"][-1]["pr"]) if it.get("stack") \
            else d
        if it.get("stack") and not top:
            rows.append(("WORKING", k, f"mode={it['mode']} {smeta} "
                                       f"layer=#{it['stack'][-1]['pr']} "
                                       "detail unavailable"))
            continue
        cmp_head = (top or {}).get("headRefOid", "") if it.get("stack") \
            else head

        # An agent-driven item runs its own loop: it waits on its own CI,
        # claims its own rounds and reports when it is done. The engine
        # never sends it a review prompt -- it only watches for the agent
        # going quiet, because a dead agent and a working one look identical
        # from here.
        #
        # Its state comes from what the agent says, not from the counter:
        # claim-round counts a round when it starts, so rounds == MAX while
        # the last round is still running. Only a `capped` report or a
        # refused claim means it stopped, and only a `ready` report for this
        # head means it is done (the agent never calls mark-reviewed). A
        # report is current until the agent shows a later sign of life.
        if it.get("driver") == "agent":
            rep = it.get("last_report") or {}
            if rep.get("at", 0) < int(it.get("last_seen") or 0) or \
                    live.get(sess, {}).get("state") == "running":
                rep = {}
            if rep.get("state") == "capped" or it.get("capped_at"):
                rows.append(("CAPPED", k,
                             f"mode={it['mode']} {smeta} head={head[:7]} "
                             f"rounds={rounds}/{MAX_REVIEW_ROUNDS} "
                             "-> agent stopped at the cap; you decide"))
                continue
            if rep.get("state") == "ready" and rep.get("sha") \
                    and cmp_head.startswith(rep["sha"]):
                rv = cmp_head   # falls through to READY below
            if not rv or not cmp_head.startswith(rv):
                quiet = (int(time.time()) - int(it.get("last_seen")
                         or it.get("added") or 0)) // 60
                # Mid-turn (a long review subagent, say) sends no heartbeat
                # but is plainly alive.
                if quiet >= AGENT_STALL_MIN and \
                        live.get(sess, {}).get("state") != "running":
                    rows.append(("STALLED", k,
                                 f"mode={it['mode']} {smeta} head={head[:7]} "
                                 f"rounds={rounds}/{MAX_REVIEW_ROUNDS} "
                                 f"quiet={quiet}m -> agent not reporting"))
                else:
                    rows.append(("WORKING", k,
                                 f"branch={it.get('branch')} {smeta} "
                                 f"driver=agent rounds={rounds}/"
                                 f"{MAX_REVIEW_ROUNDS} quiet={quiet}m"))
                continue

        # Once the user's account has approved someone else's PR, that PR is
        # finished for us: a later push by the author must not trigger another
        # review, which would post "Request changes" over the approval.
        if it["mode"] == "comment":
            mine = [r for r in (d.get("reviews") or [])
                    if (r.get("author") or {}).get("login") == me
                    and r.get("state") in ("APPROVED", "CHANGES_REQUESTED")]
            if mine and mine[-1].get("state") == "APPROVED":
                # A review the user asked for anyway (`go`) is still work in
                # progress, and holds its slot, until its agent stops.
                if live.get(sess, {}).get("state") == "running":
                    rows.append(("WORKING", k, f"mode=comment {smeta} "
                                               "reviewing (approved before)"))
                    continue
                active -= 1
                rows.append(("DONE", k, f"mode=comment {smeta} approved by you "
                                        "-> no further reviews"))
                continue

        # Follow-up reviews of someone else's PR run when the author asks for
        # one (re-requests the review), not on every push. A push that is never
        # followed by a re-request is flagged PUSHED once the branch has been
        # quiet for RE_REVIEW_QUIET_HOURS, so the PR cannot sit in limbo.
        if it["mode"] == "comment" and rv:
            running = live.get(sess, {}).get("state") == "running"
            requested = review_requested(d, me)
            if running:
                rows.append(("WORKING", k, f"mode=comment {smeta} "
                                           "reviewing, not posted yet"))
                continue
            active -= 1
            # `mine` non-empty: our review is posted, so a pending request is a
            # re-request. Once per head (rerequest_head, set by reboot()), so a
            # request left pending cannot loop.
            if requested and mine and it.get("rerequest_head") != head:
                if rounds >= MAX_REVIEW_ROUNDS:
                    it.pop("pending_rerequest", None)
                    rows.append(("CAPPED", k,
                                 f"mode=comment {smeta} head={head[:7]} "
                                 f"rounds={rounds}/{MAX_REVIEW_ROUNDS} "
                                 "re-requested -> octopilot stopped; you decide"))
                    continue
                it["pending_rerequest"] = head
                active += 1
                rows.append(("REVIEW", k, f"mode=comment {smeta} "
                                          f"head={head[:7]} reviewed={rv[:7]} "
                                          f"round={rounds + 1}/{MAX_REVIEW_ROUNDS} "
                                          "re-requested"))
                continue
            # The request was withdrawn before reboot() acted on it.
            it.pop("pending_rerequest", None)
            if not head.startswith(rv):
                if it.get("head_seen") != head:
                    it["head_seen"] = head
                    it["head_seen_at"] = int(time.time())
                quiet_h = (int(time.time()) - int(it["head_seen_at"])) / 3600
                if it.get("push_acked") == head:
                    rows.append(("DONE", k, f"mode=comment {smeta} new commits "
                                            "acknowledged -> waiting on re-request"))
                elif quiet_h >= RE_REVIEW_QUIET_HOURS:
                    rows.append(("PUSHED", k, f"mode=comment {smeta} head={head[:7]} "
                                              f"reviewed={rv[:7]} quiet={quiet_h:.0f}h "
                                              "-> pushed since our review, never re-requested"))
                else:
                    rows.append(("DONE", k, f"mode=comment {smeta} new commits "
                                            f"{quiet_h:.1f}h ago -> waiting on re-request"))
                continue
            rows.append(("DONE", k, f"mode=comment {smeta} review posted "
                                    "-> not yours to merge"))
            continue

        # the table prints head[:7] and mark-reviewed stores whatever it was
        # given, so compare by prefix rather than exact match
        if not rv or not cmp_head.startswith(rv):
            if rounds >= MAX_REVIEW_ROUNDS:
                rows.append(("CAPPED", k,
                             f"mode={it['mode']} {smeta} head={head[:7]} "
                             f"rounds={rounds}/{MAX_REVIEW_ROUNDS} "
                             "-> octopilot stopped; you decide"))
                continue
            rows.append(("REVIEW", k, f"mode={it['mode']} {smeta} "
                                      f"head={head[:7]} "
                                      f"reviewed={(it.get('reviewed_sha') or 'none')[:7]} "
                                      f"round={rounds + 1}/{MAX_REVIEW_ROUNDS}"))
        elif it["mode"] == "comment":
            # mark-reviewed runs right after the prompt is sent, so the sha
            # match alone does not mean the review is posted: while the session
            # is still running it is in flight and holds its slot. Once idle,
            # the review waits on the author, not on an agent -- no slot. It is
            # still tracked, so a new push brings back REVIEW.
            if live.get(sess, {}).get("state") == "running":
                rows.append(("WORKING", k, f"mode=comment {smeta} "
                                           "reviewing, not posted yet"))
            else:
                active -= 1
                rows.append(("DONE", k, f"mode=comment {smeta} review posted "
                                        "-> not yours to merge"))
        else:
            checks = worst_checks([layer_detail(k, it, layer["pr"])
                                   for layer in it["stack"]]) \
                if it.get("stack") else checks_of(d)
            rows.append(("READY", k, f"mode=fix {smeta} checks={checks} "
                                     "-> you merge"))

    # ---- candidates
    # prev_keys too: an issue re-keyed to its PR is still being worked on, and
    # proposing it again would let "go N" spawn a second agent on it.
    seen = set(items) | declined | {pk for it in items.values()
                                    for pk in it.get("prev_keys") or []}
    # The layers above a stack's bottom belong to its item: never their own
    # row, never a proposal.
    seen |= {f"{k.split('#')[0]}#{layer['pr']}" for k, it in items.items()
             for layer in it.get("stack") or []}
    cand = {}

    for slug in HOME_REPOS:
        for pr in results.get(("prs", slug), []) or []:
            cand[key(slug, pr["number"])] = (slug, pr, mode_for(pr, me))
    for pr in results.get(("as", "*"), []) or []:
        k = key(pr["repo"], pr["number"])
        if k not in cand:
            cand[k] = (pr["repo"], pr, mode_for(pr, me))
    for pr in results.get(("rr", "*"), []) or []:
        k = key(pr["repo"], pr["number"])
        if k not in cand:
            cand[k] = (pr["repo"], pr, mode_for(pr, me))
    # Only in cloned repos: the rest (advisory forks, old experiments) would
    # flood the UNCLONED notes.
    for pr in results.get(("mine", "*"), []) or []:
        k = key(pr["repo"], pr["number"])
        if k not in cand and pr["repo"] in reg:
            cand[k] = (pr["repo"], pr, mode_for(pr, me))

    for k, (slug, pr, mode) in sorted(cand.items()):
        if k in seen:
            continue
        # a session may already exist on this branch without being tracked
        branch = pr.get("headRefName")
        author = (pr.get("author") or {}).get("login", "?")
        title = (pr.get("title") or "")[:52]
        meta[k] = {"title": pr.get("title"), "url": pr.get("url"),
                   "author": author, "mode": mode, "item_kind": "pr"}
        if slug not in reg:
            rows.append(("UNCLONED", k, f"mode={mode} {author} \"{title}\""))
            continue
        rows.append(("PROPOSE", k, f"mode={mode} {author} \"{title}\""))

    for iss in ((sum((results.get(("iss", s), []) or [] for s in HOME_REPOS), []))
                + (results.get(("oi", "*"), []) or [])):
        k = key(iss["repo"], iss["number"], "issue")
        if k in seen:
            continue
        seen.add(k)
        meta[k] = {"title": iss.get("title"), "url": iss.get("url"),
                   "mode": "fix", "item_kind": "issue"}
        if iss["repo"] not in reg:
            rows.append(("UNCLONED", k, f"mode=fix issue \"{iss['title'][:52]}\""))
        else:
            rows.append(("PROPOSE", k, f"mode=fix issue \"{iss['title'][:52]}\""))

    # ---- stale worktree sessions with no tracked item and no open PR
    # The open-branch set must cover EVERY registered repo, not just the home
    # ones: a session on a non-home repo with a live PR is not stale.
    open_branches = set()
    for slug in HOME_REPOS:
        for pr in results.get(("prs", slug), []) or []:
            open_branches.add((reg.get(slug, {}).get("path", "").rstrip("/"),
                               pr["headRefName"]))
    others = [s2 for s2 in reg if s2 not in HOME_REPOS]
    if others:
        with ThreadPoolExecutor(max_workers=8) as ex:
            heads = dict(zip(others, ex.map(open_heads, others)))
        for slug, brs in heads.items():
            path = reg[slug]["path"].rstrip("/")
            for br in brs:
                open_branches.add((path, br))
    tracked_sessions = {v.get("session") for v in items.values()}
    for (mp, br), s in sorted(by_branch.items()):
        if s["id"] in tracked_sessions or (mp, br) in open_branches:
            continue
        if mp not in path_to_slug:
            continue
        age = session_age(s, live)
        if age < STALE_HOURS * 3600:
            continue
        rows.append(("STALE", path_to_slug[mp],
                     f"branch={br} session={sid(s['id'])} "
                     "nopr "
                     f"idle={age // 3600}h"))

    # Sessions running on this machine that octopilot did not start. They are
    # real load even though they are not tracked, so the header shows them --
    # otherwise ACTIVE reads 0/6 while 15 agents are already running.
    untracked = sum(
        1 for (mp, br), s2 in by_branch.items()
        if s2["id"] not in tracked_sessions and mp in path_to_slug)

    state["items"] = items
    return rows, active, len(reg), untracked


# ------------------------------------------------------- render & spawn

def resolve_key(state, k):
    """Exact key, else an item that used to be known by it.

    An issue-driven item is re-keyed from owner/repo!ISSUE to owner/repo#PR the
    moment its PR appears, but the agent's prompt still carries the old key.
    Without this, its first claim-round after opening the PR fails and the
    round is never counted."""
    items = state.get("items", {})
    if k in items:
        return k
    for cand, it in items.items():
        if k in (it.get("prev_keys") or []):
            return cand
    return k


def split_key(k):
    """'owner/repo#123' -> (slug, 123, 'pr'); '...!123' -> (slug, 123, 'issue')"""
    if "#" in k:
        slug, num = k.split("#", 1)
        return slug, int(num), "pr"
    if "!" in k:
        slug, num = k.split("!", 1)
        return slug, int(num), "issue"
    if "?" in k:
        slug, name = k.split("?", 1)
        return slug, name, "investigate"
    raise SystemExit(f"bad key {k!r} (want owner/repo#PR, owner/repo!ISSUE "
                     f"or owner/repo?investigation)")


def issue_context(slug, number, body_max=8000, comment_max=1500, keep=8):
    """The issue, verbatim, for splicing into the prompt.

    Fetched here rather than summarised: a paraphrase of a requirement is lossy
    in exactly the way that matters, and an agent that reads only the title and
    body misses the discussion, which often lives in the comments.

    Body and comments get separate budgets. A single overlong body must never
    be able to push the comments out -- the comments are the part the
    agent has not seen, so they are the last thing to drop.
    """
    d = gh_json(["issue", "view", str(number), "-R", slug, "--json",
                 "number,title,body,labels,comments"], default=None)
    if not d:
        return ""

    def clip(text, limit, hint):
        text = (text or "").strip()
        if len(text) <= limit:
            return text
        return text[:limit] + f"\n\n[...{hint} truncated at {limit} chars]"

    out = [f"### {slug}#{d['number']}: {d.get('title') or ''}"]
    labels = ", ".join(l.get("name", "") for l in (d.get("labels") or []))
    if labels:
        out.append(f"labels: {labels}")
    out += ["", clip(d.get("body"), body_max, "body") or "_(no description)_"]

    comments = d.get("comments") or []
    dropped = max(0, len(comments) - keep)
    if dropped:
        out += ["", f"[{dropped} earlier comment(s) omitted -- read them with "
                    f"`gh issue view {number} -R {slug} --comments`]"]
    for c in comments[-keep:]:
        who = (c.get("author") or {}).get("login", "?")
        when = (c.get("createdAt") or "")[:10]
        out += ["", f"--- comment by {who} ({when}) ---",
                clip(c.get("body"), comment_max, "comment")]
    return "\n".join(out)


def render_prompt(name, subs):
    pdir = os.environ.get("PROMPT_DIR") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "prompts")
    path = os.path.join(pdir, name if name.endswith(".md") else name + ".md")
    with open(path) as fh:
        body = fh.read()
    with open(os.path.join(pdir, "shared", "pr-hygiene.md")) as fh:
        subs.setdefault("HYGIENE", fh.read().strip())
    # {{LOOP}} is only substituted for templates that actually carry it, so
    # review-comment.md (someone else's branch) never gets a push loop. Only
    # address-feedback on a stack layer gets the stack loop: it restacks and
    # pushes the layers above, and the feedback stays on its one PR.
    stacked = name == "address-feedback" and bool(subs.get("STACK"))
    if "{{LOOP}}" in body:
        loop = "stack-loop.md" if stacked else "agent-loop.md"
        with open(os.path.join(pdir, "shared", loop)) as fh:
            subs.setdefault("LOOP", fh.read().strip())
    # The stack-only wording: a sync before anything else, and a restack with
    # one atomic push in place of the plain push. Both empty for a plain PR.
    if stacked:
        with open(os.path.join(pdir, "shared", "stack-feedback.md")) as fh:
            subs.setdefault("STACK_FEEDBACK", "\n" + fh.read().strip() + "\n")
        with open(os.path.join(pdir, "shared", "stack-push.md")) as fh:
            subs.setdefault("STACK_PUSH", " " + fh.read().strip())
    if "{{STACK_LOOP}}" in body:
        with open(os.path.join(pdir, "shared", "stack-loop.md")) as fh:
            subs.setdefault("STACK_LOOP", fh.read().strip())
    # Set unconditionally: FIND_MODEL and VERIFY_MODEL also live inside the
    # LOOP fragment, not only in the template body, so an `in body` test would
    # never fire.
    subs.setdefault("FIND_MODEL", MODEL_REVIEW_FIND)
    subs.setdefault("VERIFY_MODEL", MODEL_REVIEW_VERIFY)
    # Read whenever the file exists, for the same reason: MERGE may sit inside
    # the LOOP fragment. Without the file the sweep below strips the placeholder.
    merge = os.path.join(pdir, "shared", "merge-result.md")
    if os.path.exists(merge):
        with open(merge) as fh:
            subs.setdefault("MERGE", fh.read().strip())
    if "{{CONTEXT}}" in body and "CONTEXT" not in subs and subs.get("ISSUE") \
            and subs.get("REPO"):
        subs["CONTEXT"] = issue_context(subs["REPO"], subs["ISSUE"])
    # Fail before the KEY autofill below, which would turn a layer's PR into
    # its own key instead of the stack's.
    if name in ("adopt-stack", "rebase-stack"):
        required = ("STACK", "KEY")
    elif name == "review-stack":
        required = ("STACK",)
    elif name == "address-feedback" and subs.get("STACK"):
        required = ("KEY",)
    else:
        required = ()
    missing = [k for k in required if not subs.get(k)]
    if missing:
        hint = "" if missing == ["STACK"] else \
            " (a stack item's KEY is its bottom PR's key)"
        raise SystemExit(f"render {name} needs {' and '.join(missing)}{hint}")
    if "KEY" not in subs and subs.get("REPO"):
        if subs.get("PR"):
            subs["KEY"] = f"{subs['REPO']}#{subs['PR']}"
        elif subs.get("ISSUE"):
            subs["KEY"] = f"{subs['REPO']}!{subs['ISSUE']}"
    subs.setdefault("REVIEW_CMD", os.environ.get("REVIEW_CMD", "/review"))
    if name in ("review-comment", "re-review", "review-stack"):
        subs.setdefault("REVIEW_LEVEL",
                        os.environ.get("REVIEW_LEVEL_COMMENT", "medium"))
    subs.setdefault("REVIEW_LEVEL", os.environ.get("REVIEW_LEVEL", "high"))
    subs.setdefault("REVIEW_LEVEL_FOLLOWUP",
                    os.environ.get("REVIEW_LEVEL_FOLLOWUP", "high"))
    # Shared fragments are spliced in FIRST: they carry their own {{REPO}},
    # {{PR}} and {{BRANCH}} placeholders, and if they were substituted in the
    # same pass as everything else those would survive the pass and then be
    # stripped to empty by the sweep below -- yielding `gh pr checks  -R  `.
    for frag in ("CONTEXT", "LOOP", "STACK_LOOP", "MERGE", "HYGIENE",
                 "STACK_FEEDBACK", "STACK_PUSH"):
        if frag in subs:
            body = body.replace("{{" + frag + "}}", str(subs[frag]))
    for k, v in subs.items():
        if k in ("CONTEXT", "LOOP", "STACK_LOOP", "MERGE", "HYGIENE",
                 "STACK_FEEDBACK", "STACK_PUSH"):
            continue
        body = body.replace("{{" + k + "}}", str(v))
    return re.sub(r"\{\{[A-Z_]+\}\}", "", body).strip()


def render_for(state, template, subs):
    """The prompt `template` renders to for `subs`, with the facts of the item
    it is for (KEY, question, branch, stack, read-only) filled in from state.
    Mutates `subs`; the caller's own KEY=VAL values win over the item's."""
    if "KEY" not in subs and subs.get("REPO") and subs.get("PR"):
        # BOOTING rows send no KEY: a layer's PR resolves to its stack.
        found = item_key_for(state, subs["REPO"], subs["PR"])
        if found:
            subs["KEY"] = found
    it_ = state.get("items", {}).get(subs.get("KEY", ""), {})
    if it_.get("question"):
        subs.setdefault("QUESTION", it_["question"])
        subs.setdefault("BRANCH", it_.get("branch", ""))
    if it_.get("stack"):
        subs.setdefault("STACK", stack_text(it_["stack"]))
    # A read-only item never gets a prompt that tells it to commit and
    # open a PR, whatever template the caller asked for.
    tmpl = "investigate-readonly" if it_.get("read_only") else template
    return render_prompt(tmpl, subs)


def resolve_mode(slug, num, kind, me):
    """Re-derived here, never taken on trust from the caller."""
    if kind in ("issue", "investigate"):
        return "fix"
    d = gh_json(["pr", "view", str(num), "-R", slug, "--json",
                 "author,assignees"],
                default={}) or {}
    return mode_for(d, me)


def find_session(repo_path, branch):
    for s in all_sessions()[0]:
        wt = s.get("worktree") or {}
        if (wt.get("main_repo_path") or "").rstrip("/") == repo_path.rstrip("/") \
                and wt.get("branch") == branch:
            return s
    return None


def flag_value(args, flag):
    """The word after `flag` in argv, or None when the flag is absent."""
    if flag not in args:
        return None
    i = args.index(flag)
    if i + 1 >= len(args):
        raise SystemExit(f"{flag} needs a value")
    return args[i + 1]


def parse_stack(spec):
    """'704:payg/0-prep 705:payg/1-foundation' -> [{pr, branch}], bottom up."""
    layers = []
    for part in spec.split():
        num, _, branch = part.partition(":")
        if not num.isdigit() or not branch:
            raise SystemExit(f"bad --stack entry {part!r} (want PR:branch)")
        layers.append({"pr": int(num), "branch": branch})
    if len(layers) < 2:
        raise SystemExit("--stack needs at least two PR:branch entries")
    if len({layer["pr"] for layer in layers}) != len(layers):
        raise SystemExit("--stack lists a PR twice")
    return layers


def check_stack(slug, num, branch, base, layers, me):
    """Refuses unless the layers are open PRs chained bottom to top onto base.
    -> each layer's mode, bottom up. One gh call covers the whole repo."""
    if layers[0] != {"pr": num, "branch": branch}:
        raise SystemExit(f"the first --stack entry must be {num}:{branch}, "
                         "the PR this key names")
    open_prs = {r["number"]: r for r in gh_json(
        ["pr", "list", "-R", slug, "--state", "open", "--limit", "200",
         "--json", PR_FIELDS], default=[]) or []}
    modes, below = [], base
    for layer in layers:
        pr = open_prs.get(layer["pr"])
        if pr is None:
            raise SystemExit(f"#{layer['pr']} is not an open PR in {slug}")
        if pr.get("headRefName") != layer["branch"]:
            raise SystemExit(f"#{layer['pr']} is on {pr.get('headRefName')}, "
                             f"not {layer['branch']}")
        if pr.get("baseRefName") != below:
            raise SystemExit(f"#{layer['pr']} targets {pr.get('baseRefName')}, "
                             f"not {below}: the stack is not chained")
        modes.append(mode_for(pr, me))
        below = layer["branch"]
    return modes


def spawn_item(state, key_, branch, title, new_branch, question=None,
               read_only=False, stack_spec=None):
    """Create the item's worktree and session, and track it.

    Raises SystemExit on a refusal (unknown repo, a stack that does not check
    out, a session already open on the branch), on a failed git fetch, and when
    the host cannot start the session. A host failure can come after
    add_worktree has created the worktree, which is left in place.

    -> {"ok", "error", "key", "template", "base", "slug", "prev", "stack",
    "warning", "note", "model"}: ok is always True here (refusals raise), template
    is the prompt to render, prev the sha a re-review follows (or None), stack the
    parsed layers (or None), warning the host's startup warning (or None), note
    what the host did instead of what was asked (or None)."""
    me = whoami(state)
    slug, num, kind = split_key(key_)
    reg = state.get("registry", {})
    if slug not in reg:
        raise SystemExit(f"{slug} is not in the registry -- no local clone under "
                         f"CLONE_ROOTS ({' '.join(CLONE_ROOTS)}). Clone it first.")
    r = reg[slug]
    path, base = r["path"], r["base"]
    mode = resolve_mode(slug, num, kind, me)

    # Validated before anything is created: a refused stack leaves no trace.
    stack = None
    if stack_spec is not None:
        if kind != "pr" or new_branch:
            raise SystemExit("--stack takes an existing PR key, without --new")
        stack = parse_stack(stack_spec)
        modes = check_stack(slug, num, branch, base, stack, me)
        not_mine = [f"#{layer['pr']}" for layer, m in zip(stack, modes)
                    if m != "fix"]
        if not_mine and len(not_mine) < len(stack):
            raise SystemExit(f"mixed stack: {' '.join(not_mine)} not yours "
                             "(mode=comment). A stack is all yours or none of "
                             "it: spawn the yours and the others separately")
        held = {layer["pr"] for k2, it2 in state.get("items", {}).items()
                if k2 != key_ and k2.startswith(slug + "#")
                for layer in it2.get("stack") or [{"pr": it2.get("pr")}]}
        busy = [f"#{layer['pr']}" for layer in stack[1:] if layer["pr"] in held]
        if busy:
            raise SystemExit(f"{' '.join(busy)} already tracked by octopilot")

    code, _, err = sh(["git", "fetch", "origin"], cwd=path, timeout=180)
    if code != 0:
        raise SystemExit(f"git fetch failed in {path}: {err}")
    if new_branch:
        sh(["git", "pull", "--ff-only"], cwd=path, timeout=180)

    if find_session(path, branch):
        raise SystemExit(f"a session already exists on {branch} in {slug}")

    # Someone else's PR the user already reviewed (possibly before a detach)
    # gets a follow-up, not a second full review.
    reviewed = None
    if kind == "pr" and mode == "comment" and not stack:
        reviewed = last_own_review(pr_detail(slug, num), me)
    tmpl = {"issue": "work", "investigate": "investigate"}.get(
        kind, "review-fix" if mode == "fix" else "review-comment")
    if reviewed:
        tmpl = "re-review"
    if stack:
        tmpl = "adopt-stack" if mode == "fix" else "review-stack"
    if read_only:
        tmpl = "investigate-readonly"
    model = MODEL_FOR.get(tmpl, "")

    host = spawn_host(key_, path, base, branch, title, new_branch, model)

    item = {"mode": mode, "branch": branch, "pr": num if kind == "pr" else None,
            "reviewed_sha": reviewed, "phase": "working", "added": int(time.time()),
            "driver": "agent" if mode == "fix" else "orchestrator"}
    item.update({k: v for k, v in host.items() if k not in ("warning", "note")})
    if stack:
        item["stack"] = stack
    if question:
        item["question"] = question
    if read_only:
        item["read_only"] = True
    state["items"][key_] = item
    save(state)
    log_event("spawn", key_, mode=mode, branch=branch, session=item["session"],
              title=title)
    return {"ok": True, "error": None, "key": key_, "template": tmpl, "base": base,
            "slug": slug, "prev": reviewed, "stack": stack,
            "warning": host.get("warning"), "note": host.get("note"),
            "model": model}


def spawn(state, key_, branch, title, new_branch, question=None,
          read_only=False, stack_spec=None):
    """spawn <key> <branch> <title> [--new] [--stack "PR:branch ..."]: prints
    what the caller needs to send the first prompt."""
    r = spawn_item(state, key_, branch, title, new_branch, question=question,
                   read_only=read_only, stack_spec=stack_spec)
    it = state["items"][key_]
    where = f"agent={it['agent']} worktree={it['worktree']}"
    print(f"spawned {key_} mode={it['mode']} branch={branch} "
          f"session={it['session']} {where} model={r['model'] or 'default'}")
    if r["note"]:
        print(f"NOTE: {r['note']}")
    if r["warning"]:
        print(f"WARNING: {r['warning']}")
    tmpl, prev = r["template"], r["prev"]
    print(f"next: `$A send {key_} ...` with the '{tmpl}' prompt"
          + (" (PREV= from the last line)" if prev else ""))
    prev_arg = f" PREV={prev}" if prev else ""
    print(f"PROMPT_TEMPLATE={tmpl} BASE={r['base']} REPO={r['slug']}{prev_arg}")
    if r["stack"]:
        print(f"STACK=\"{stack_text(r['stack'])}\"")
    return 0


def stack_text(stack):
    """[{pr, branch}] -> '704:payg/0-prep 705:payg/1-foundation' (STACK=)."""
    return " ".join(f"{layer['pr']}:{layer['branch']}" for layer in stack)


def item_key_for(state, slug, pr):
    """The key of the item at slug#pr, or else of the stack item that has
    slug#pr as a layer (a layer is never an item of its own); '' for neither."""
    items = state.get("items") or {}
    k = key(slug, pr)
    if k in items:
        return k
    for k_, it in items.items():
        if k_.startswith(f"{slug}#") and any(
                str(layer["pr"]) == str(pr) for layer in it.get("stack") or []):
            return k_
    return ""


def worktree_dir(repo, branch):
    """<repo>/.worktrees/<branch with / as ->."""
    return os.path.join(repo.rstrip("/"), ".worktrees", branch.replace("/", "-"))


def agent_name(key_):
    """A herdr agent name ([a-z][a-z0-9_-]{0,31}) unique among live agents:
    ap-<repo>-<number>, e.g. ap-vocdoni-app-1782."""
    slug, num, _ = split_key(key_)
    base = re.sub(r"[^a-z0-9_-]+", "-",
                  f"ap-{slug.split('/', 1)[-1]}-{num}".lower()).strip("-")
    taken = {a.get("agent") for a in all_agents()}
    name, n = base[:32], 2
    while name in taken:
        sfx = f"-{n}"
        name, n = base[:32 - len(sfx)] + sfx, n + 1
    return name


def add_worktree(path, base, branch, new_branch):
    """Create the branch's worktree with git, not `herdr worktree create` (that
    makes a local branch off HEAD for an origin-only PR). -> the worktree's path."""
    wt = worktree_dir(path, branch)
    porcelain = sh(["git", "worktree", "list", "--porcelain"], cwd=path)[1]
    if f"worktree {wt}" not in porcelain.splitlines():
        if new_branch:
            args = ["git", "worktree", "add", "--no-track", "-b", branch, wt,
                    f"origin/{base}"]
        elif sh(["git", "rev-parse", "--verify", "--quiet",
                 f"refs/heads/{branch}"], cwd=path)[0] == 0:
            args = ["git", "worktree", "add", wt, branch]
        else:
            args = ["git", "worktree", "add", "--track", "-b", branch, wt,
                    f"origin/{branch}"]
        code, out, err = sh(args, cwd=path, timeout=180)
        if code != 0:
            raise SystemExit(f"git worktree add failed: {err or out}")
    return wt


def spawn_host(key_, path, base, branch, title, new_branch, model):
    """git worktree, then a session on OCTOPILOT_BACKEND running a named claude."""
    backend = spawn_backend()
    wt = add_worktree(path, base, branch, new_branch)
    return backend.spawn(key_, path, wt, title, model)


def siblings(state, slug, exclude=None):
    """Tracked mode=fix items in `slug` with an open PR -- the rebase fan-out
    set. mode=comment items are never included: they are other people's
    branches and must not be pushed to."""
    out = []
    for k, it in sorted(state.get("items", {}).items()):
        if k == exclude:
            continue
        if not k.startswith(slug + "#") and not k.startswith(slug + "!"):
            continue
        if it.get("mode") != "fix" or not it.get("pr") or not it.get("session"):
            continue
        out.append((k, it))
    return out


def print_sibling_rows(rows, base):
    if not rows:
        print("SIBLINGS: none")
        return
    print("SIBLINGS:")
    for k, it in rows:
        # A trailing `stack` marks a sibling that needs rebase-stack, not rebase.
        marker = " stack" if it.get("stack") else ""
        print(f"  {it['session']} {it['branch']} {it['pr']} {base} {k}{marker}")


def print_siblings(state, slug, exclude=None):
    base = state.get("registry", {}).get(slug, {}).get("base", "main")
    print_sibling_rows(siblings(state, slug, exclude), base)


def cleanup_item(state, key_):
    """Remove a merged item's session and worktree, then mark it handled.

    -> {"ok", "error", "msg", "discard_error", "siblings", "base"}: error is the
    refusal (untracked item); msg is the text the CLI prints for it, including
    the failed discard line; discard_error is the backend's message when its
    discard failed, which does not stop the cleanup; siblings is the rebase
    fan-out set afterwards and base the repo's base branch."""
    it = state.get("items", {}).get(key_)
    if not it:
        return {"ok": False, "error": f"{key_} is not tracked; nothing to clean up",
                "msg": None, "discard_error": None, "siblings": [], "base": None}
    lines, discard_error = [], None
    sess = it.get("session")
    backend = backend_for(sess)
    if backend:
        code, raw = backend.discard(sess, it.get("worktree"))
        lines.append(f"{remove_label_for(backend, sess)} {sess}: "
                     f"{'ok' if code == 0 else raw}")
        if code != 0:
            discard_error = raw
    if key_ not in state["handled_merges"]:
        state["handled_merges"].append(key_)
    state["items"].pop(key_, None)
    save(state)
    log_event("cleanup", key_)
    lines.append(f"cleaned up {key_}")
    slug = key_.split("#")[0].split("!")[0]
    return {"ok": True, "error": None, "msg": "\n".join(lines),
            "discard_error": discard_error, "siblings": siblings(state, slug),
            "base": state.get("registry", {}).get(slug, {}).get("base", "main")}


def cleanup(state, key_):
    """cleanup <key>: remove a merged item's session and worktree."""
    r = cleanup_item(state, key_)
    if not r["ok"]:
        print(r["error"], file=sys.stderr)
        return 1
    print(r["msg"])
    print_sibling_rows(r["siblings"], r["base"])
    return 0


def restack_item(state, k):
    """Drop a stack's merged bottom layers and re-key the item to the new
    bottom. Its session and worktree stay; everything else on the item moves
    with it. A single layer left is no stack: the item becomes a plain PR.

    -> {"ok", "error", "new_key", "template", "subs", "siblings"}: error is the
    refusal the CLI prints (stderr); on success template and subs are the next
    prompt to render, and siblings the rebase fan-out set without the item."""
    def refused(error):
        return {"ok": False, "error": error, "new_key": None, "template": None,
                "subs": {}, "siblings": [], "base": None}

    k = resolve_key(state, k)
    it = state.get("items", {}).get(k)
    if it is None:
        return refused(f"unknown item {k}")
    if not it.get("stack"):
        return refused(f"{k} is not a stack")
    if it.get("mode") != "fix":
        return refused(f"{k} is mode={it.get('mode')}: never restacked, nothing "
                       "of theirs is pushed to")
    slug = k.split("#")[0]
    # Asked again rather than taken from the row: the row is one tick old.
    merged, left = stack_rest(it, lambda pr: pr_detail(slug, pr))
    if not merged:
        return refused(f"{k}: the bottom PR is not merged; nothing to restack")
    if not left:
        return refused(f"{k}: every layer is merged; cleanup takes it from here")
    nxt = left[0]
    nk = key(slug, nxt["pr"])
    if nk in state["items"]:
        return refused(f"{nk} is already tracked")
    nxt_d = pr_detail(slug, nxt["pr"])
    if nxt_d is None:
        return refused(f"{k}: gh pr view failed for #{nxt['pr']}")
    if nxt_d.get("state") == "CLOSED":
        return refused(f"{k}: #{nxt['pr']} is closed, not merged; the stack "
                       "cannot restack past it -- you decide")
    # rebase-stack replays the layers above from this head, passed on the
    # command line: a squash merge leaves it out of base's history.
    top_head = (pr_detail(slug, merged[-1]["pr"]) or {}).get("headRefOid")
    if not top_head:
        return refused(f"{k}: could not read the merged head of "
                       f"#{merged[-1]['pr']}")
    base = state.get("registry", {}).get(slug, {}).get("base", "main")

    for layer in merged:
        (it.get("layer_seen") or {}).pop(str(layer["pr"]), None)
    if len(left) > 1:
        it["stack"] = left
    else:
        # The last layer keeps its feedback mark as the plain PR watermark,
        # so feedback it already had does not come back.
        seen_next = (it.get("layer_seen") or {}).get(str(nxt["pr"]))
        if seen_next and seen_next > (it.get("feedback_seen") or ""):
            it["feedback_seen"] = seen_next
        it.pop("stack")
        it.pop("layer_seen", None)
    it["pr"], it["branch"] = nxt["pr"], nxt["branch"]
    it.setdefault("prev_keys", []).append(k)
    state["items"][nk] = state["items"].pop(k)
    if k not in state["handled_merges"]:
        state["handled_merges"].append(k)
    save(state)
    gone = ",".join(f"#{layer['pr']}" for layer in merged)
    log_event("restack", nk, old=k, merged=gone, next=f"#{nxt['pr']}")
    if len(left) > 1:
        template = "rebase-stack"
        subs = {"REPO": slug, "BASE": base, "KEY": nk, "RESTACK_FROM": top_head}
    else:
        template = "rebase"
        subs = {"REPO": slug, "PR": nxt["pr"], "BRANCH": nxt["branch"], "BASE": base}
    return {"ok": True, "error": None, "new_key": nk, "template": template,
            "subs": subs, "siblings": siblings(state, slug, exclude=nk),
            "base": base}


def restack(state, k):
    """restack <key>: drop a stack's merged bottom layers and re-key it."""
    k = resolve_key(state, k)
    r = restack_item(state, k)
    if not r["ok"]:
        print(r["error"], file=sys.stderr)
        return 1
    nk, subs = r["new_key"], r["subs"]
    it = state["items"][nk]
    layers = len(it["stack"]) if it.get("stack") else 1
    print(f"restacked {k} -> {nk} branch={it['branch']} "
          f"layers={layers} session={sid(it.get('session'))}")
    if r["template"] == "rebase-stack":
        print(f"next: `$A send {nk} \"$($A render rebase-stack REPO={subs['REPO']} "
              f"BASE={subs['BASE']} KEY={nk} RESTACK_FROM={subs['RESTACK_FROM']})\"`")
    else:
        print(f"next: `$A send {nk} \"$($A render rebase REPO={subs['REPO']} "
              f"PR={subs['PR']} BRANCH={subs['BRANCH']} BASE={subs['BASE']})\"`")
    print_sibling_rows(r["siblings"], r["base"])
    return 0


def mark_reviewed(state, k, sha):
    """Record a review of `k` at `sha`: the item moves to fixing and counts one
    more round. -> {"ok", "error", "round"}; error names an untracked item."""
    it = state["items"].get(k)
    if it is None:
        return {"ok": False, "error": f"{k} is not tracked", "round": None}
    it["reviewed_sha"] = sha
    it["phase"] = "fixing"
    it["review_rounds"] = int(it.get("review_rounds", 0)) + 1
    save(state)
    log_event("reviewed", k, sha=sha[:7], round=it["review_rounds"])
    return {"ok": True, "error": None, "round": it["review_rounds"]}


def mark_feedback(state, k, since, pr=None):
    """Move `k`'s feedback watermark to `since` and reset its review rounds. On a
    stack, `pr` names the layer whose watermark moves; a plain PR ignores it.
    -> {"ok", "error", "pr"}: error names an untracked item or a `pr` that is
    not one of the stack's layers."""
    it = state["items"].get(k)
    if it is None:
        return {"ok": False, "error": f"{k} is not tracked", "pr": None}
    if it.get("stack"):
        if pr not in [layer["pr"] for layer in it["stack"]]:
            names = " ".join(str(layer["pr"]) for layer in it["stack"])
            return {"ok": False, "pr": None, "error": f"{k} is a stack: name one "
                    f"of its layers ({names})"}
        it.setdefault("layer_seen", {})[str(pr)] = since
    else:
        pr = None
        it["feedback_seen"] = since
    it["review_rounds"] = 0
    it.pop("capped_at", None)
    save(state)
    log_event("feedback", k, since=since, pr=pr)
    return {"ok": True, "error": None, "pr": pr}


def decline(state, keys):
    """Add `keys` to the declined list, each once, and log each. -> {"ok",
    "error", "added"}: added holds the keys this call added."""
    added = []
    for k in keys:
        if k not in state["declined"]:
            state["declined"].append(k)
            added.append(k)
    save(state)
    for k in keys:
        log_event("decline", k)
    return {"ok": True, "error": None, "added": added}


def ack_push(state, k):
    """The user has seen the PUSHED flag: stop flagging this head. A re-request
    or a further push brings the PR back. -> {"ok", "error", "push_acked"}; error
    names an untracked item or one without a PR yet."""
    it = state["items"].get(k)
    if it and it.get("pr"):
        d = pr_detail(k.split("#")[0], it["pr"]) or {}
        it["push_acked"] = d.get("headRefOid")
        save(state)
        return {"ok": True, "error": None, "push_acked": it["push_acked"]}
    return {"ok": False, "error": f"unknown item {k}", "push_acked": None}


def detach_item(state, k):
    """Close the item's workspace or space, keep its worktree and branch, and
    drop the item so it holds no slot (it is proposed again). -> {"ok", "error",
    "msg"}: msg is the line to print on success; error is the refusal."""
    it = state["items"].get(k)
    if not it:
        return {"ok": False, "error": f"unknown item {k}", "msg": None}
    sess = it.get("session")
    backend = backend_for(sess)
    if backend:
        code, raw = backend.close(sess)
        if code != 0:
            return {"ok": False,
                    "error": f"{host_noun(backend, sess)} close failed: {raw}",
                    "msg": None}
    state["items"].pop(k)
    save(state)
    log_event("detach", k, branch=it.get("branch"))
    worktree = f", worktree kept at {it['worktree']}" if it.get("worktree") \
        else ", worktree kept"
    return {"ok": True, "error": None,
            "msg": (f"detached {k} session={sid(sess)} branch={it.get('branch')} "
                    f"-> {short_noun(sess)} closed{worktree}, untracked")}


def set_paused(state, paused):
    """Pause or resume the engine's work. -> {"ok", "error", "paused"}."""
    state["paused"] = paused
    save(state)
    log_event("pause" if paused else "resume")
    return {"ok": True, "error": None, "paused": paused}


ORDER = ["MERGED", "RESTACK", "REVIEW", "FEEDBACK", "BOOTING", "WORKING",
         "STALLED", "READY", "CAPPED", "PUSHED", "DONE", "CLOSED", "GONE",
         "PROPOSE", "UNCLONED", "STALE", "UNKNOWN"]


def row_order(r):
    return (ORDER.index(r[0]) if r[0] in ORDER else 99, r[1])


def wind_down():
    """'yes'/'no' against WIND_DOWN_AT in local time, None when unset."""
    if not WIND_DOWN_AT:
        return None
    return "yes" if time.strftime("%H:%M") >= WIND_DOWN_AT.zfill(5) else "no"


def render(rows, active, nrepos, state, untracked=0):
    extra = f"   UNTRACKED {untracked}" if untracked else ""
    wd = wind_down()
    if wd:
        extra += f"   WIND_DOWN {wd} (from {WIND_DOWN_AT})"
    box = state.get("inbox") or []
    if box:
        extra += f"   INBOX {len(box)} (run inbox)"
    print(f"PAUSED {'yes' if state.get('paused') else 'no'}   "
          f"REPOS {nrepos}   ACTIVE {active}/{MAX_ACTIVE}{extra}")
    rows.sort(key=row_order)
    if not rows:
        print("(nothing actionable)")
    for kind, k, extra in rows:
        print(f"{kind:<9}{k:<38}{extra}")


ROW_TITLE = re.compile(r'"(.*)"')
ROW_FIELD = re.compile(r"(\w+)=(\S+)")
# Item fields the dashboard shows; the rest of an item is bookkeeping.
SNAPSHOT_ITEM_FIELDS = ("mode", "branch", "session", "pr", "driver", "phase",
                        "review_rounds", "last_seen", "last_note",
                        "last_report", "added", "question", "reviewed_sha",
                        "read_only", "parked")


def parse_extra(extra):
    """A row's free-text tail -> {fields, title, note}.

    The quoted title goes first: it may itself contain `=` or `->`."""
    out = {}
    m = ROW_TITLE.search(extra)
    if m:
        out["title"] = m.group(1)
        extra = extra[:m.start()] + extra[m.end():]
    head, _, note = extra.partition(" -> ")
    out["fields"] = dict(ROW_FIELD.findall(head))
    if note.strip():
        out["note"] = note.strip()
    return out


def snapshot(state):
    """Run a scan on a throwaway copy of the state and publish it as JSON.

    Nothing is saved: this runs where `tick` may not (see engine_allowed), and
    a worktree must not consume the live state's transitions.
    """
    work = copy.deepcopy(state)
    info = {}
    rows, active, nrepos, untracked = scan(work, info=info)
    snap = snapshot_doc(state, work, rows, active, nrepos, untracked, info)
    write_snapshot(snap)
    return snap


def snapshot_doc(state, work, rows, active, nrepos, untracked, info):
    """The dashboard's JSON view of one scan: its rows with their item and
    GitHub details, and the header numbers. `work` is the scanned state."""
    items = work.get("items", {})
    out = []
    for kind, k, extra in sorted(rows, key=row_order):
        r = {"kind": kind, "key": k, "extra": extra}
        r.update(parse_extra(extra))
        it = items.get(k)
        if it:
            r["item"] = {f: it[f] for f in SNAPSHOT_ITEM_FIELDS
                         if it.get(f) is not None}
        d = info["details"].get(k)
        m = info["meta"].get(k)
        if d:
            r.update(title=d.get("title"), url=d.get("url"),
                     checks=checks_of(d), head=(d.get("headRefOid") or "")[:7],
                     draft=bool(d.get("isDraft")))
        elif m:
            r.update({f: v for f, v in m.items() if v is not None})
        out.append(r)
    return {"generated_at": int(time.time()),
            "paused": bool(state.get("paused")),
            "repos": nrepos, "active": active, "max_active": MAX_ACTIVE,
            "untracked": untracked, "max_rounds": MAX_REVIEW_ROUNDS,
            "stall_min": AGENT_STALL_MIN,
            "last_tick": state.get("last_tick"),
            "rows": out}


def write_snapshot(snap):
    """Replace SNAPSHOT_FILE atomically: the dashboard reads it at any time."""
    os.makedirs(os.path.dirname(SNAPSHOT_FILE), exist_ok=True)
    tmp = f"{SNAPSHOT_FILE}.tmp.{os.getpid()}"
    with open(tmp, "w") as fh:
        json.dump(snap, fh, indent=2, sort_keys=True)
    os.replace(tmp, SNAPSHOT_FILE)


def item_or_exit(state, k):
    k = resolve_key(state, k)
    it = state["items"].get(k)
    if it is None:
        raise SystemExit(f"unknown item {k}")
    return k, it


def send_item(state, k, text):
    """Exit 4 when the agent is sitting on an approval or question: that is
    the user's call, and typing a prompt into it would answer it blindly."""
    k, it = item_or_exit(state, k)
    addr = address_of(it)
    if not addr:
        print(f"{k}: no live agent to send to", file=sys.stderr)
        return 1
    ok, err = send_to(addr, text)
    if not ok:
        if err == "agent_blocked":
            print(f"{k}: agent is waiting on an approval or a question; "
                  "nothing sent -- report it to the user", file=sys.stderr)
            return 4
        print(f"{k}: send to {addr} failed: {err}", file=sys.stderr)
        return 1
    # A new prompt makes the agent's last report history.
    it["last_seen"] = int(time.time())
    save(state)
    log_event("sent", k, text=text.strip().splitlines()[0][:80] if text.strip()
              else "")
    print(f"sent to {k} ({addr})")
    return 0


def reboot(state, k):
    """Give an item's agent a fresh conversation for its next review prompt,
    then mark it booting. herdr clears the conversation in place, keeping
    the pinned model."""
    k, it = item_or_exit(state, k)
    backend = backend_for(it.get("session"))
    target = backend.target(it) if backend else None
    if not target:
        where = host_noun(backend, it.get("session")) if backend else "workspace"
        print(f"{k}: no live agent in {where} {it.get('session')}",
              file=sys.stderr)
        return 1
    ok, err = send_to(f"{backend.prefix}{target}", "/clear")
    if not ok:
        print(f"{k}: /clear failed: {err}", file=sys.stderr)
        return 4 if err == "agent_blocked" else 1
    code, raw = backend.wait_idle(target)
    it["phase"] = "booting"
    if it.get("pending_rerequest"):
        it["rerequest_head"] = it.pop("pending_rerequest")
    save(state)
    log_event("reboot", k)
    # Still booting either way; a non-zero exit tells the engine not to
    # send the prompt in this same tick to an agent that is not idle yet.
    if code != 0:
        print(f"{k} cleared but not idle yet -> booting: {raw}",
              file=sys.stderr)
        return 3
    print(f"{k} rebooted -> booting")
    return 0


def read_item(state, k):
    """Print the visible screen of an item's agent, whichever backend runs it."""
    k, it = item_or_exit(state, k)
    backend = backend_for(it.get("session"))
    target = backend.target(it) if backend else None
    if not target:
        print(f"{k}: no live agent to read", file=sys.stderr)
        return 1
    ok, text = backend.read(target)
    if not ok:
        print(f"{k}: read failed: {text}", file=sys.stderr)
        return 1
    print(text.rstrip("\n"))
    return 0


def focus_item(state, k):
    """Show an item's agent in the window: its workspace or space."""
    k, it = item_or_exit(state, k)
    backend = backend_for(it.get("session"))
    target = backend.target(it) if backend else None
    if not target:
        print(f"{k}: no live agent to focus", file=sys.stderr)
        return 1
    ok, err = backend.focus(it["session"], target)
    if not ok:
        print(f"{k}: focus failed: {err}", file=sys.stderr)
        return 1
    print(f"focused {k} ({backend.prefix}{target})")
    return 0


REPORT_STATES = ("ready", "capped", "blocked", "failed", "stalled")


def report(state, args):
    """report <key> <state> <sha> <message...> [--to <session>]

    Records the agent's final report on its item and logs it as a `report`
    event: the dashboard, the kitchen and the Needs-you list show it from there.

    `--to` is accepted and ignored: agents spawned earlier still pass it.
    """
    if "--to" in args:
        i = args.index("--to")
        args = args[:i] + args[i + 2:]
    if len(args) < 4:
        print("usage: report <key> <state> <sha> <message...> [--to <session>]",
              file=sys.stderr)
        return 1
    k = resolve_key(state, args[1])
    st, sha, msg = args[2], args[3], " ".join(args[4:])
    if st not in REPORT_STATES:
        print(f"bad report state {st!r} (want {'|'.join(REPORT_STATES)})",
              file=sys.stderr)
        return 1
    now = int(time.time())
    it = state["items"].get(k)
    if it is None:
        # Detached or cleaned up while its agent was still mid-turn: nobody
        # is waiting for this, so nothing acts on it. Keep a neutral trace in
        # the log and tell the agent to stop.
        log_event("orphan-report", k, state=st, sha=sha[:7], msg=msg[:300])
        print(f"{k} is no longer tracked by octopilot (detached or cleaned "
              "up): report not delivered. Stop here; there is nothing left "
              "to do for this item.")
        return 0
    it["last_report"] = {"state": st, "sha": sha, "msg": msg[:300], "at": now}
    it["last_seen"] = now
    save(state)
    log_event("report", k, state=st, sha=sha[:7], msg=msg[:300])
    print(f"reported {k} {st}")
    return 0


INBOX_MAX = 200


def send_code(state, text=None):
    """Queue `text` for the code-changes agent and deliver the queue, oldest
    first. -> (delivered, error of the first failed send or None).

    A request that cannot be delivered (the tab closed, the agent gone) stays
    queued in `code_pending` and is retried on every tick, so it is never
    lost. Caller holds the lock."""
    q = state.setdefault("code_pending", [])
    if text:
        q.append({"at": int(time.time()), "text": text})
    sent, err = 0, None
    while q:
        ok, err = send_to(CODE_AGENT, q[0]["text"])
        if not ok:
            if text:  # once per request, not once per tick
                log_event("send-code-pending", text=text[:200],
                          error=str(err)[:200])
            break
        log_event("send-code", text=q.pop(0)["text"][:200])
        sent, err = sent + 1, None
    if not q:
        state.pop("code_pending", None)
    save(state)
    return sent, err


def to_inbox(state, text):
    """Queue one message for the engine's drain. Caller holds the lock."""
    box = state.setdefault("inbox", [])
    box.append({"at": int(time.time()), "text": text})
    del box[:-INBOX_MAX]
    save(state)


def inbox(state):
    """List the queued requests, oldest first, without clearing them: the
    engine's drain takes them. Continuation lines are indented, so every
    message starts at column 0."""
    print("pending requests (the engine drains them; act on none)")
    box = state.get("inbox") or []
    if not box:
        print("(inbox empty)")
        return 0
    for m in box:
        first, *rest = m["text"].splitlines() or [""]
        print(f"{time.strftime('%H:%M', time.localtime(m['at']))} {first}")
        for line in rest:
            print(f"    {line}")
    return 0


# ------------------------------------------------------------ speech and chat
#
# The GUI's two chats, read from the claudes' transcripts (CONTRACT.md, "Speech
# and chat"). open-gui.sh labels each claude octopilot.session=<uuid>.

PROMPTER_AGENT = "octopilot-prompter"
PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")
# A transcript can run to hundreds of MB: only this tail is parsed.
TRANSCRIPT_TAIL_BYTES = 5 * 1024 * 1024
CHAT_LIMIT = 50
TOOL_INPUT_KEYS = ("command", "file_path", "path", "pattern", "url", "query",
                   "description", "skill")
TOOL_SUMMARY_MAX = 100
# User entries the harness writes for itself (command output, notifications,
# other sessions' messages, interrupts); a person's text starts with none of them.
INJECTED_PREFIXES = ("<command-", "<local-command-", "<bash-", "<task-notification>",
                     "<system-reminder>", "[Request interrupted",
                     "Another Claude session sent a message")
HC_WRAPPER = re.compile(r"^<hc\b[^>]*>(.*)</hc>$", re.S)
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def agent_address(agent):
    """The address of the hc terminal labelled octopilot.agent=<agent>, or None."""
    found = HC.find(agent)
    return found[len(HC.prefix):] if found else None


def terminal_info(address):
    """`hc get --json` of a terminal, or None when it is gone."""
    code, info, _ = hc(["get", address, "--json"], timeout=10)
    return info if code == 0 and isinstance(info, dict) else None


def view_doc():
    """The headless dashboard's view.json, or None when it is missing, unreadable
    or older than VIEW_STALE_SECS (no headless dashboard is writing it)."""
    try:
        with open(VIEW_FILE) as fh:
            view = json.load(fh)
    except (OSError, ValueError):
        return None
    gen = view.get("generated_at") if isinstance(view, dict) else None
    if not isinstance(gen, (int, float)) or time.time() - gen > VIEW_STALE_SECS:
        return None
    return view


def proc_start(pid):
    """/proc/<pid>/stat's start time (field 22) as a string, else None."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def live_session_id(info):
    """The session id of the claude in a terminal's foreground, from claude's own
    ~/.claude/sessions/<pid>.json (rewritten when /clear starts a new session),
    or None when no claude runs there or the file is not that process's."""
    fg = info.get("foreground") or {}
    pid = fg.get("pid")
    if fg.get("name") != "claude" or not isinstance(pid, int):
        return None
    try:
        with open(os.path.join(SESSIONS_DIR, f"{pid}.json")) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("pid") != pid:
        return None
    start = data.get("procStart")
    now = proc_start(pid)
    if start is not None and now is not None and str(start) != now:
        return None  # a recycled pid: the file is an earlier process's
    sid = data.get("sessionId")
    return sid if isinstance(sid, str) and UUID_RE.match(sid) else None


def transcript_path(info):
    """The transcript of a terminal's claude: the session of the claude running
    in its foreground (claude's sessions/<pid>.json, which follows /clear), else
    the octopilot.session label's, else None. A live session whose transcript is
    not written yet gives None, never the stale label. No guess from the folder:
    other claude sessions, the user's own included, may run in the prompter's folder."""
    session = live_session_id(info) or (info.get("labels") or {}).get("octopilot.session") or ""
    if not UUID_RE.match(session):
        return None
    hits = glob.glob(os.path.join(PROJECTS_DIR, "*", f"{session}.jsonl"))
    return hits[0] if hits else None


def parse_line(raw):
    try:
        entry = json.loads(raw)
    except ValueError:
        return None
    return entry if isinstance(entry, dict) else None


def tail_entries(path):
    """The entries in the last TRANSCRIPT_TAIL_BYTES of a transcript, oldest
    first. The line the window cuts is dropped."""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        start = max(0, fh.tell() - TRANSCRIPT_TAIL_BYTES)
        fh.seek(start)
        lines = fh.read().split(b"\n")
    if start:
        lines = lines[1:]
    return [e for e in map(parse_line, lines) if e]


def stream_entries(path):
    """Every entry of a transcript, oldest first, one line at a time."""
    with open(path, "rb") as fh:
        for raw in fh:
            entry = parse_line(raw)
            if entry:
                yield entry


def typed_text(entry):
    """The text a person typed in a user entry, else None (tool results, meta
    and the harness's own messages are not the person's). An hc message loses
    its wrapper; an image alone reads as [image]."""
    if entry.get("isMeta") or entry.get("isCompactSummary") \
            or entry.get("isVisibleInTranscriptOnly"):
        return None
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        blocks = [b for b in content if isinstance(b, dict)]
        if any(b.get("type") == "tool_result" for b in blocks):
            return None
        text = "\n".join(b.get("text") or "" for b in blocks if b.get("type") == "text")
        if not text.strip() and any(b.get("type") == "image" for b in blocks):
            text = "[image]"
    else:
        return None
    text = text.strip()
    wrapped = HC_WRAPPER.match(text)
    if wrapped:
        text = wrapped.group(1).strip()
    if not text or text.startswith(INJECTED_PREFIXES):
        return None
    return text


def tool_summary(block):
    """'<name>: <short input>' for a tool_use block."""
    name = block.get("name") or "tool"
    inp = block.get("input") if isinstance(block.get("input"), dict) else {}
    short = next((str(inp[k]) for k in TOOL_INPUT_KEYS if inp.get(k)), "")
    if not short and inp:
        short = json.dumps(inp, ensure_ascii=False)
    short = " ".join(short.split())
    if len(short) > TOOL_SUMMARY_MAX:
        short = short[:TOOL_SUMMARY_MAX - 3] + "..."
    return f"{name}: {short}" if short else name


def finish_turn(turn):
    return {"id": turn["id"], "role": "assistant",
            "text": "\n\n".join(turn["texts"]).strip(), "at": turn["at"],
            "tools": turn["tools"]}


def chat_messages(entries):
    """Chat messages, oldest first: what a person typed, and each assistant turn.
    Claude writes one entry per content block, all with the API message id, so
    they merge into one turn."""
    turn = None
    for e in entries:
        if e.get("isSidechain"):
            continue
        if e.get("type") == "user":
            text = typed_text(e)
            if text is None:
                continue
            if turn is not None:
                yield finish_turn(turn)
                turn = None
            yield {"id": e.get("uuid"), "role": "user", "text": text,
                   "at": e.get("timestamp"), "tools": []}
        elif e.get("type") == "assistant":
            msg = e.get("message") or {}
            api_id = msg.get("id") or e.get("uuid")
            if turn is None or turn["api"] != api_id:
                if turn is not None:
                    yield finish_turn(turn)
                turn = {"api": api_id, "id": e.get("uuid"), "at": e.get("timestamp"),
                        "texts": [], "tools": []}
            content = msg.get("content")
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    turn["texts"].append(block["text"])
                elif block.get("type") == "tool_use":
                    turn["tools"].append(tool_summary(block))
    if turn is not None:
        yield finish_turn(turn)


def status_of(info):
    return (info.get("status") or {}).get("state") or "idle"


def blocked_of(info):
    status = info.get("status") or {}
    if status.get("state") != "blocked":
        return None
    return {"kind": status.get("kind"), "msg": status.get("msg")}


def speech_cmd(state):
    """The engine's last lines (view.json's header.engine.last) and its state."""
    out = {"agent": None, "terminal": None, "status": "offline",
           "paused": bool(state.get("paused")), "text": "", "at": None, "seq": 0}
    view = view_doc()
    engine = ((view or {}).get("header") or {}).get("engine") or {}
    if view is None or engine.get("mode") == "off":
        return out
    address = agent_address(HEADLESS_AGENT)
    # A hung engine is one the user must look at: the kitchen shows it as blocked.
    status = {"acting": "working", "stale": "blocked"}.get(engine.get("state"), "idle")
    out.update(terminal=address, agent=HC.prefix + address if address else None,
               status=status)
    last = engine.get("last")
    if last:
        text = "\n".join("- " + line for line in last.get("lines") or [])
        at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(last["at"]))
        # seq moves with the text, so a re-read of the same lines is a no-op.
        out.update(text=text, at=at, seq=zlib.crc32(f"{at}\n{text}".encode()))
    return out


def chat_page(path, after, limit):
    """-> (messages, reset): the newest `limit`, or the anchor then up to `limit`
    newer ones (the anchor first, since the mod updates by id). An unknown
    anchor (a session after /clear) gives the newest with reset set."""
    tail = list(chat_messages(tail_entries(path)))
    if after is None:
        return tail[-limit:], False
    ids = [m["id"] for m in tail]
    if after in ids:
        at = ids.index(after)
        return tail[at:at + 1 + limit], False
    # Older than the tail: stream from the start, keeping the anchor and the
    # messages after it.
    page = []
    for m in chat_messages(stream_entries(path)):
        if page:
            page.append(m)
            if len(page) > limit:
                break
        elif m["id"] == after:
            page.append(m)
    if page:
        return page, False
    return tail[-limit:], True


def chat_cmd(args):
    """The prompter's conversation (see chat_page) and its state."""
    after = flag_value(args, "--after")
    try:
        limit = int(flag_value(args, "--limit") or CHAT_LIMIT)
    except ValueError:
        raise SystemExit("--limit needs a number")
    limit = max(1, limit)
    out = {"terminal": None, "session": None, "status": "offline", "blocked": None,
           "reset": False, "messages": []}
    address = agent_address(PROMPTER_AGENT)
    info = terminal_info(address) if address else None
    if info is None:
        return out
    path = transcript_path(info)
    out.update(terminal=address, status=status_of(info), blocked=blocked_of(info))
    if path:
        out["session"] = os.path.basename(path)[:-len(".jsonl")]
        out["messages"], out["reset"] = chat_page(path, after, limit)
    elif after is not None:
        # A new session that has no transcript yet: what the mod holds is stale.
        out["reset"] = True
    return out


def chat_result(ok, error=None):
    """-> (exit code, the JSON to print) for chat-send and chat-keys."""
    if ok:
        return 0, {"ok": True}
    return (4 if error == "agent_blocked" else 1), {"ok": False, "error": error}


def chat_send(args):
    """chat-send <text...>: type it into the prompter. Exit 4 when the prompter
    is blocked on a prompt (answer that with chat-keys), 3 when none runs."""
    text = " ".join(args[1:]).strip()
    if not text:
        print("usage: scan.sh chat-send <text...>", file=sys.stderr)
        return 1
    address = agent_address(PROMPTER_AGENT)
    if address is None:
        print(json.dumps({"ok": False, "error": "no prompter running"}))
        return 3
    ok, error = HC.send(address, text)
    code, out = chat_result(ok, error)
    print(json.dumps(out))
    return code


def chat_keys(args):
    """chat-keys <keys...>: press keys in the prompter, e.g. 1, 2 or esc to
    answer a permission prompt. Exit 4 when it is not blocked on a prompt, so
    no stray keystrokes reach it."""
    keys = args[1:]
    if not keys:
        print("usage: scan.sh chat-keys <keys...>", file=sys.stderr)
        return 1
    address = agent_address(PROMPTER_AGENT)
    info = terminal_info(address) if address else None
    if info is None:
        print(json.dumps({"ok": False, "error": "no prompter running"}))
        return 3
    if status_of(info) != "blocked":
        print(json.dumps({"ok": False, "error": "prompter is not blocked"}))
        return 4
    code, data, raw = hc(["keys", address, *keys])
    result_code, out = chat_result(code == 0, None if code == 0 else (error_code(data) or raw))
    print(json.dumps(out))
    return result_code


SELF_LOCKING = {"tick", "drain"}
READ_ONLY = {"render", "status", "siblings", "snapshot", "wind-down", "plugin-settings",
             "inbox", "installed-dir", "speech", "chat", "chat-send", "chat-keys",
             "host-space"}


def claude_plugin_dirs():
    """The entries of CLAUDE_CODE_PLUGIN_DIRS in ~/.claude/settings.json, as listed."""
    try:
        with open(os.path.expanduser("~/.claude/settings.json")) as f:
            settings = json.load(f)
    except (OSError, ValueError):
        return []
    env = settings.get("env") if isinstance(settings, dict) else None
    listed = env.get("CLAUDE_CODE_PLUGIN_DIRS") if isinstance(env, dict) else None
    return [e for e in (listed if isinstance(listed, str) else "").split(":") if e]


def is_octopilot_plugin(entry):
    """True when the plugin dir of a settings entry (~ expanded) is octopilot's,
    by the name in its manifest."""
    try:
        with open(os.path.join(os.path.expanduser(entry), ".claude-plugin", "plugin.json")) as f:
            return json.load(f).get("name") == "octopilot"
    except (OSError, ValueError, AttributeError):
        return False


def installed_dir():
    """The live checkout: the parent of the octopilot plugin dir that settings.json
    loads. ~/src/octopilot when settings.json lists none."""
    for entry in claude_plugin_dirs():
        if is_octopilot_plugin(entry):
            return os.path.dirname(os.path.realpath(os.path.expanduser(entry)))
    return os.path.realpath(os.path.expanduser("~/src/octopilot"))


def plugin_settings():
    """--settings JSON that loads this checkout's octopilot plugin in place of
    the installed one, keeping the other plugin dirs of settings.json. Empty on
    the live checkout, which already loads its own plugin."""
    if os.path.realpath(OCTOPILOT_DIR) == os.path.realpath(installed_dir()):
        return ""
    own = os.path.join(OCTOPILOT_DIR, "plugin")
    dirs, placed = [], False
    for entry in claude_plugin_dirs():
        # Every octopilot copy goes, so only the worktree's loads; the first one's
        # place is kept.
        if is_octopilot_plugin(entry):
            if not placed:
                dirs.append(own)
                placed = True
        else:
            dirs.append(entry)
    if not placed:
        dirs.insert(0, own)
    return json.dumps({"env": {"CLAUDE_CODE_PLUGIN_DIRS": ":".join(dirs)}})


def engine_allowed(checkout):
    """Whether the checkout at `checkout` may run `tick`, which saves state: the
    live one, or any with a scratch state dir (OCTOPILOT_STATE_DIR). A worktree
    must not consume the live state's transitions."""
    if os.environ.get("OCTOPILOT_STATE_DIR"):
        return True
    return os.path.realpath(checkout) == os.path.realpath(installed_dir())


def engine_mode(args=(), checkout=None):
    """off, plan or on: OCTOPILOT_ENGINE picks it, and `tick --plan` forces plan.
    Off wherever engine_allowed() refuses the checkout (this one unless
    `checkout` names the caller's): a worktree's events would land in the live
    log."""
    mode = "plan" if "--plan" in args else os.environ.get("OCTOPILOT_ENGINE", "on")
    if mode not in ("plan", "on") or not engine_allowed(checkout or OCTOPILOT_DIR):
        return "off"
    return mode


@contextmanager
def exec_lock():
    """Yields True while holding exec.lock, False at once when another run holds
    it. Waiting would spend the dashboard's timeout on someone else's actions."""
    os.makedirs(os.path.dirname(EXEC_LOCK), exist_ok=True)
    with open(EXEC_LOCK, "w") as ex:
        try:
            fcntl.flock(ex.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True


def tick(args):
    """tick [--quiet] [--plan] [--refresh] [--legacy]: one scan that saves the
    transitions it finds, writes the dashboard's snapshot from the same rows,
    then runs the engine. `scan` and `refresh` are this command with --legacy:
    an orchestrator still running them is told to stop, and gets no engine rows."""
    mode = engine_mode(args)
    os.makedirs(os.path.dirname(TICK_LOCK), exist_ok=True)
    with open(TICK_LOCK, "w") as lk:
        try:
            fcntl.flock(lk.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("tick already running")
            return 0
        with locked():
            state = load()
            info = {}
            rows, active, n, untracked = scan(state, refresh="--refresh" in args,
                                              info=info)
            for k, reason in info["revived"]:
                log_event("unpark", k, reason=reason)
            for k, session in info["rehomed"]:
                log_event("rehome", k, session=session)
            for kind, k, _ in rows:
                if kind == "GONE":
                    log_event("gone", k)
            counts = {}
            for kind, _, _ in rows:
                counts[kind] = counts.get(kind, 0) + 1
            # The event log is shown on every dashboard; log a tick only when
            # the table changed, not once per two minutes.
            changed = counts != state.get("tick_counts")
            state["tick_counts"] = counts
            state["last_tick"] = int(time.time())
            state.pop("orch", None)
            save(state)
            write_snapshot(snapshot_doc(state, state, rows, active, n,
                                        untracked, info))
            if changed:
                log_event("tick", active=active, counts=counts)
            # `shown` is only what is printed: the engine below gets every row.
            shown = rows
            if "--legacy" in args and mode == "on":
                # The engine acts on these kinds itself: the table leaves them out.
                import engine
                print("octopilot has no orchestrator any more: stop ticking, "
                      "the engine acts on every row")
                shown = [r for r in rows if r[0] not in engine.PLANNED]
            if "--quiet" not in args:
                render(shown, active, n, state, untracked)
        if mode == "off" and "--plan" in args and "--quiet" not in args:
            print(f"engine off here: {OCTOPILOT_DIR} is not the live checkout "
                  "and OCTOPILOT_STATE_DIR is unset")
        # A legacy tick never acts: the dashboard runs the engine, and a
        # leftover orchestrator's Bash call would time out mid-action.
        if mode != "off" and "--legacy" not in args:
            import engine
            with exec_lock() as held:
                # The budget counts from here: the scan is not the engine's to pay for.
                if held:
                    lines = engine.run_rows(rows, mode, time.monotonic())
                elif "--quiet" not in args:
                    lines = ["engine busy: another run acts, the next tick does"]
                else:
                    lines = []
            if lines:
                print("\n".join(lines))
    return 0


def drain(args):
    """drain [--quiet] [--plan]: handle the inbox's requests in order, then the
    queued sends. The dashboard runs it quiet when a request lands; --quiet
    hides only the off and busy notices, never the outcome lines."""
    mode = engine_mode(args)
    if mode == "off":
        if "--quiet" not in args:
            print("engine off here: nothing drained")
        return 0
    import engine
    with exec_lock() as held:
        if not held:
            # The dashboard's next wake (a minute at most) drains again.
            if "--quiet" not in args:
                print("engine busy: another run acts, the drain retries later")
            return 0
        lines = engine.drain(mode, time.monotonic())
    if lines:
        print("\n".join(lines))
    return 0


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "scan"
    # scan and refresh are tick: mapped before the lock checks below, or tick
    # would take the state lock twice in one process and deadlock.
    if cmd in ("scan", "refresh"):
        args = ["tick", *args[1:], "--legacy"]
        args += ["--refresh"] if cmd == "refresh" else []
        cmd = "tick"
    if cmd in READ_ONLY:
        return dispatch(cmd, args, load())
    # These take the state lock themselves, step by step, rather than for the
    # whole command.
    if cmd in SELF_LOCKING:
        return dispatch(cmd, args, None)
    # scan mutates (registry memo, feedback_seen stamps) and every other
    # command is a read-modify-write, so both run under the lock.
    with locked():
        return dispatch(cmd, args, load())


def dispatch(cmd, args, state):
    if cmd == "tick":
        return tick(args)
    if cmd == "drain":
        return drain(args)
    if cmd == "snapshot":
        snap = snapshot(state)
        if "--quiet" not in args:
            print(json.dumps(snap, indent=2, sort_keys=True))
        return 0
    if cmd == "report":
        return report(state, args)
    if cmd == "inbox":
        return inbox(state)
    if cmd == "request":
        # A command from the dashboard or the prompter (go/no/re-review/
        # investigate...), queued for the engine's drain.
        text = " ".join(args[1:]).strip()
        if not text:
            print("usage: request <command...>", file=sys.stderr)
            return 1
        to_inbox(state, f"REQUEST {text}")
        log_event("request", text=text[:200])
        print(f"queued: {text}")
        return 0
    if cmd == "send":
        # send <key> <text...>: prompt the item's agent, wherever it runs.
        return send_item(state, args[1], " ".join(args[2:]))
    if cmd == "send-code":
        # send-code <text...>: prompt the code-changes agent. Not an item: it
        # holds no slot and is never scanned.
        text = " ".join(args[1:]).strip()
        if not text:
            print("usage: send-code <text...>", file=sys.stderr)
            return 1
        sent, err = send_code(state, text)
        if err:
            # Kept, not dropped: the tab may just be closed for now.
            print(f"{CODE_AGENT}: {err} -- kept, {len(state['code_pending'])} "
                  "pending; the engine retries them on its next run. Do not resend.")
            return 0
        print(f"sent to {CODE_AGENT}" + (f" ({sent} with earlier queued)"
                                         if sent > 1 else ""))
        return 0
    if cmd == "host-space":
        # host-space: the Octopilot space (GUI) or workspace (TUI) that worker tabs open in.
        host = octopilot_space() if spawn_backend() is HC else octopilot_workspace()
        if host:
            print(host)
        return 0
    if cmd == "speech":
        print(json.dumps(speech_cmd(state)))
        return 0
    if cmd == "chat":
        print(json.dumps(chat_cmd(args)))
        return 0
    if cmd == "chat-send":
        return chat_send(args)
    if cmd == "chat-keys":
        return chat_keys(args)
    if cmd == "reboot":
        return reboot(state, args[1])
    if cmd in ("read", "focus") and len(args) < 2:
        print(f"usage: scan.sh {cmd} <item>", file=sys.stderr)
        return 1
    if cmd == "read":
        # read <item>: the item's agent screen, whichever backend runs it.
        return read_item(state, args[1])
    if cmd == "focus":
        # focus <item>: show the item's agent in its workspace or space.
        return focus_item(state, args[1])
    if cmd == "render":
        # render <template> KEY=VAL ...
        subs = dict(kv.split("=", 1) for kv in args[2:] if "=" in kv)
        print(render_for(state, args[1], subs))
        return 0
    if cmd == "spawn":
        # spawn <key> <branch> <title> [--new] [--stack "PR:branch ..."]
        return spawn(state, args[1], args[2], args[3], "--new" in args[4:],
                     stack_spec=flag_value(args, "--stack"))
    if cmd == "restack":
        return restack(state, args[1])
    if cmd == "investigate":
        # investigate <slug> <name> <branch> <title> <question> [--read-only]
        slug_, name, branch, title, question = args[1:6]
        return spawn(state, key(slug_, name, "investigate"), branch, title,
                     True, question=question,
                     read_only="--read-only" in args[6:])
    if cmd == "siblings":
        print_siblings(state, args[1],
                       args[2] if len(args) > 2 else None)
        return 0
    if cmd == "cleanup":
        return cleanup(state, args[1])
    if cmd == "wind-down":
        print(f"WIND_DOWN {wind_down() or 'off'} (from {WIND_DOWN_AT or '-'})")
        return 0
    if cmd == "status":
        print(json.dumps(state, indent=2, sort_keys=True))
        return 0
    if cmd in ("pause", "resume"):
        print(f"paused={set_paused(state, cmd == 'pause')['paused']}")
        return 0
    if cmd == "decline":
        decline(state, args[1:])
        print("declined:", " ".join(args[1:]))
        return 0
    if cmd == "undecline":
        state["declined"] = [k for k in state["declined"] if k not in args[1:]]
        save(state)
        print("undeclined:", " ".join(args[1:]))
        return 0
    if cmd == "installed-dir":
        # installed-dir: the live checkout, as settings.json loads it (open-gui.sh uses it).
        print(installed_dir())
        return 0
    if cmd == "plugin-settings":
        # plugin-settings: the --settings JSON for a worktree's claude, nothing
        # on the live checkout (open-gui.sh uses it).
        settings = plugin_settings()
        if settings:
            print(settings)
        return 0
    if cmd == "track":
        # track <key> <mode> <branch> <session> [pr]
        k, mode, branch, session = args[1:5]
        pr = int(args[5]) if len(args) > 5 else None
        backend = backend_for(session)
        if backend is None:
            print(f"{session} is not a herdr workspace (w12), herdr tab (w12:t1), "
                  "herdr-app space (sp<hex>) or herdr-app tab (tb<hex>) id",
                  file=sys.stderr)
            return 1
        sessions, answered, open_ws = all_sessions()
        if listed(answered, session) and session not in open_ws:
            print(f"{session} is not an open {host_noun(backend, session)}",
                  file=sys.stderr)
            return 1
        reviewed = None
        if pr and mode == "comment":
            reviewed = last_own_review(
                pr_detail(k.split("#")[0], pr), whoami(state))
        state["items"][k] = {"mode": mode, "branch": branch, "session": session,
                             "pr": pr, "reviewed_sha": reviewed,
                             "phase": "working", "added": int(time.time()),
                             "driver": "agent" if mode == "fix" else "orchestrator"}
        # Name its agent so it can be addressed.
        it = state["items"][k]
        it["backend"] = backend.name
        space = next((s.get("space") for s in sessions if s["id"] == session), None) \
            if is_tab(session) else None
        if space:
            it["space"] = space
        occupant = backend.live().get(session) or {}
        if occupant.get("agent"):
            it["agent"] = occupant["agent"]
        elif occupant.get("pane"):
            name = agent_name(k)
            if backend.rename(occupant, name):
                it["agent"] = name
        save(state)
        log_event("track", k, mode=mode, branch=branch, session=session)
        print(f"tracked {k} mode={mode} branch={branch} session={sid(session)}")
        return 0
    if cmd == "ack-push":
        # ack-push <key>: the user has seen the PUSHED flag; stop flagging this
        # head. A re-request or a further push brings the PR back.
        r = ack_push(state, args[1])
        if not r["ok"]:
            print(r["error"], file=sys.stderr)
            return 1
        print(f"{args[1]} push_acked={str(r['push_acked'])[:7]}")
        return 0
    if cmd == "park":
        # park <key>: the dashboard's `d` moves a card to Done. Unlike detach
        # the workspace stays open and the item stays tracked; it just holds
        # no slot until new activity revives it (see scan()).
        k = args[1]
        it = state["items"].get(k)
        if it is None:
            print(f"unknown item {k}", file=sys.stderr)
            return 1
        if not it.get("pr"):
            print(f"{k} has no PR yet, nothing to park", file=sys.stderr)
            return 1
        d = pr_detail(k.split("#")[0], it["pr"])
        if d is None:
            print(f"gh pr view failed for {k}", file=sys.stderr)
            return 1
        # The head and the pending request are what a later re-request is
        # measured against; the stamp is what new feedback is measured from.
        it["parked"] = {"at": int(time.time()), "head": d.get("headRefOid") or None,
                        "requested": review_requested(d, whoami(state))}
        it.setdefault("feedback_seen", time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                     time.gmtime()))
        save(state)
        log_event("park", k)
        print(f"parked {k} session={sid(it.get('session'))} "
              f"-> {short_noun(it.get('session'))} kept, holds no slot")
        return 0
    if cmd == "unpark":
        k = args[1]
        it = state["items"].get(k)
        if it is None:
            print(f"unknown item {k}", file=sys.stderr)
            return 1
        it.pop("parked", None)
        save(state)
        log_event("unpark", k)
        print(f"unparked {k} -> holds a slot again")
        return 0
    if cmd == "detach":
        # detach <key>: close its workspace or space, keep the worktree and
        # branch, and drop the item so it holds no slot (it is proposed again).
        # Reverse by hand: reopen the worktree, start claude in it, then `track`.
        r = detach_item(state, args[1])
        if not r["ok"]:
            print(r["error"], file=sys.stderr)
            return 1
        print(r["msg"])
        return 0
    if cmd == "untrack":
        for k in args[1:]:
            state["items"].pop(k, None)
            log_event("untrack", k)
        save(state)
        print("untracked:", " ".join(args[1:]))
        return 0
    if cmd == "set-phase":
        k, phase = args[1], args[2]
        if k in state["items"]:
            state["items"][k]["phase"] = phase
            save(state)
            log_event("phase", k, phase=phase)
            print(f"{k} phase={phase}")
        else:
            print(f"unknown item {k}", file=sys.stderr)
            return 1
        return 0
    if cmd == "mark-feedback":
        # mark-feedback <key> <since> [<layer PR>]: a stack needs the layer's
        # PR, so one layer's mark never hides another layer's feedback.
        k, when = args[1], args[2]
        pr = int(args[3]) if len(args) > 3 and args[3].isdigit() else None
        r = mark_feedback(state, k, when, pr)
        if not r["ok"]:
            # An untracked key is a silent no-op.
            if k in state["items"]:
                print(r["error"], file=sys.stderr)
                return 1
            return 0
        layer = f" layer=#{r['pr']}" if r["pr"] else ""
        print(f"{k}{layer} feedback_seen={when} rounds=0")
        return 0
    if cmd == "mark-reviewed":
        k, sha = args[1], args[2]
        r = mark_reviewed(state, k, sha)
        if not r["ok"]:
            # An untracked key is a silent no-op.
            return 0
        print(f"{k} reviewed={sha[:7]} round={r['round']}/{MAX_REVIEW_ROUNDS}")
        return 0
    if cmd == "review-base":
        # The base sha a review merged the PR into, so a later base move
        # is detectable.
        k = resolve_key(state, args[1])
        it = state["items"].get(k)
        if it is None:
            print(f"unknown item {k}", file=sys.stderr)
            return 1
        sha = args[2]
        it["reviewed_base"] = sha
        save(state)
        log_event("review-base", k, sha=sha[:7])
        print(f"{k} reviewed_base={sha[:7]}")
        return 0
    if cmd == "claim-round":
        k = resolve_key(state, args[1])
        it = state["items"].get(k)
        if it is None:
            print(f"unknown item {k}", file=sys.stderr)
            return 1
        n = int(it.get("review_rounds", 0))
        if n >= MAX_REVIEW_ROUNDS:
            # The scan shows CAPPED from here even if the agent dies before
            # its `capped` report; reset-rounds and mark-feedback clear it.
            it["capped_at"] = int(time.time())
            save(state)
            log_event("capped", k, round=n)
            print(f"CAPPED rounds={n}/{MAX_REVIEW_ROUNDS}")
            return 3
        n += 1
        it["review_rounds"] = n
        it["phase"] = "fixing"
        it["last_seen"] = int(time.time())
        save(state)
        log_event("round", k, round=n)
        print(f"PROCEED round={n}/{MAX_REVIEW_ROUNDS}")
        return 0
    if cmd == "heartbeat":
        k = resolve_key(state, args[1])
        if k in state["items"]:
            state["items"][k]["last_seen"] = int(time.time())
            if len(args) > 2:
                state["items"][k]["last_note"] = " ".join(args[2:])[:200]
            save(state)
            log_event("heartbeat", k, note=state["items"][k].get("last_note"))
            print(f"{k} last_seen=now")
        return 0
    if cmd == "reset-rounds":
        for k in args[1:]:
            if k in state["items"]:
                state["items"][k]["review_rounds"] = 0
                state["items"][k].pop("capped_at", None)
                log_event("reset-rounds", k)
                print(f"{k} rounds=0/{MAX_REVIEW_ROUNDS}")
        save(state)
        return 0
    if cmd == "mark-merged":
        for k in args[1:]:
            if k not in state["handled_merges"]:
                state["handled_merges"].append(k)
            state["items"].pop(k, None)
            log_event("merged", k)
        save(state)
        print("merged:", " ".join(args[1:]))
        return 0

    print(f"unknown command: {cmd}", file=sys.stderr)
    print("usage: scan.sh [tick|drain|status|pause|resume|decline|undecline|"
          "track|untrack|detach|park|unpark|set-phase|mark-reviewed|claim-round|heartbeat|"
          "reset-rounds|review-base|"
          "mark-merged|restack|"
          "render|spawn|"
          "investigate|snapshot|report|inbox|request|send|send-code|reboot|read|focus|wind-down|"
          "speech|chat|chat-send|chat-keys]", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
