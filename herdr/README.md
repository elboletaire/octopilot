# Octopilot in herdr

A herdr plugin that gives octopilot a home: an **Octopilot** workspace with the
prompter on the left and the live dashboard (`../dashboard.sh`) on the right.
The dashboard updates by itself, and the engine acts on the table without anyone
asking it to.

Every work session is a tab on its worktree, in the Octopilot space (herdr-app) or
workspace (herdr TUI), with a claude agent named `ap-<repo>-<number>` (see Worker
tabs). The engine is not an agent: the dashboard runs it as `scan.py tick` and
`scan.py drain`.

The prompter and the code agent start on `MODEL_PROMPTER` and `MODEL_CODE` (config.sh, passed
to claude as `--model`), in both the TUI and the GUI launcher, not on the global default; a running
one keeps its model.

## Worker tabs

`WORKER_HOST` (config.sh) picks where a worker opens. It is read from the environment when
the process that spawns workers starts (the headless engine, which a launcher run starts with
the value it was given), so restart the engine after changing it:

- `tab` (the default): a tab of the Octopilot space or workspace. `scan.sh host-space`
  prints the one it uses: the GUI's space where the headless, the code agent or the
  prompter runs, or the TUI's workspace labelled `Octopilot`, else the prompter's. It prints
  nothing with `space`, so a session brought back then opens as a space of its own.
- `space`: a space or workspace of its own, as before. Sessions already open keep theirs.

Without an Octopilot space or workspace (the launcher has not run), the worker opens as
a space or workspace of its own, and the spawn prints `NOTE:` saying so. A remote
Octopilot space (see `CONTRACT.md`) is not used: workers open on this computer.

`SETTLE_WORKER_TABS` (GUI only; herdr has no settle, read at the same start as `WORKER_HOST`)
settles each new worker tab at spawn: herdr-app's `tab.settle` takes it out of the lists, still running. It comes back
when you open it from the dashboard (`⏎`) or when its agent needs you. `0`
leaves the tab shown.

A tab is named by its item's title. A title that is one of the launcher's tab names
(`OCTOPILOT`, `engine`, `Octopilot (code changes)`, in any case) gets the agent's name
after it: `engine (ap-<repo>-<number>)`.

> **Closing the Octopilot space or workspace ends every worker tab.** Their worktrees
> are kept, and their items show `GONE` and are untracked, freeing their slots. To end
> one worker, use `z` or `del` on its row.

A tab closed by hand does not end its session when its agent is live in another open
tab or space (not held by another item; a tab is preferred): the session follows the
agent there (re-homed), and the row stays. An agent herdr-app parked (`terminal.park`: its terminal is labelled with the agent's name,
no open tab holds it, its claude still runs) keeps its item tracked: `WORKING` with its
terminal's address and the `terminal.keep` command to show it, and no prompt is sent until then
(feedback waits; new feedback on a parked PR is counted in the row, not sent or marked). A card you held with `d` stays On hold; a parked PR that closed is
`CLOSED`. Cleanup and detach (`z`) end the parked terminal (`hc kill`) first, and cleanup keeps the item
tracked when the kill fails. Only an agent that is gone shows `GONE`.

**Upgrading.** Sessions already in their own spaces or workspaces keep working; there is
nothing to migrate. Both id shapes are read: tabs as `tb…` (GUI) or `w…:t…` (TUI), spaces
and workspaces as `sp…` or `w…`.

**Rolling back** past this step: first detach or finish the tab sessions (`z`, or `del`
once their work is merged), or set `WORKER_HOST=space` and let the tab sessions drain.
Older code cannot handle tab ids.

## Setup

`./install.sh` (this repo) links the plugin when herdr is installed. By hand:

```sh
herdr plugin link ~/src/octopilot/herdr
```

Its actions are bound by the dotfiles' herdr config, `config/herdr/config.toml` there (linked
to `~/.config/herdr/config.toml` by the dotfiles' installer), not by this repo:

```toml
[[keys.command]]
key = "prefix+shift+a"          # build or focus the Octopilot workspace
type = "plugin_action"
command = "elboletaire.octopilot.open"

[[keys.command]]
key = "prefix+a"                # the dashboard over whatever you are doing
type = "plugin_action"
command = "elboletaire.octopilot.peek"
```

The workspace has two tabs: **OCTOPILOT**, described below, and **Octopilot
(code changes)**, a claude started in this checkout for changing octopilot itself
(`octopilot-code`). In OCTOPILOT, the dashboard pane is titled
`dashboard`.

`open` starts the **prompter** as a fresh claude in `OCTOPILOT_ORCH_CWD` (`~/src`),
running `/octopilot:prompter`, in a pane on the left, with the **dashboard** beside
it. Type in the prompter: it answers from octopilot's state and queues your commands
for the engine. The dashboard's keys are queued for the engine too, so nothing is
ever typed into a pane you are writing in. On an existing workspace, `open` adds
whichever of the prompter and the code-changes tab is missing, and names the first
tab OCTOPILOT. A retired `octopilot` agent still running there is left alone, and
`open` says so: close its pane.

Show octopilot's state on each worker's row in the herdr sidebar (the
dashboard publishes these tokens; they expire ten minutes after it stops):

```toml
[ui]
agent_panel_sort = "priority"   # blocked and finished agents first
status_indicators = "symbols"
sidebar_width = 34

[ui.sidebar.agents]
rows = [["state_icon", "workspace", "tab"],
        ["$ap_kind", "$ap_pr", "$ap_rounds", "$ap_ci"],
        ["$ap_note"]]
```

`tab` shows each worker's tab name beside its workspace.

Notifications: the dashboard sends a desktop notification (`notify-send`) when
an item enters 🔴 Needs you -- once per item and reason, never on repeats --
and the engine and its agents send none of their own. Keep herdr's own
per-turn notifications inside herdr so they do not drown it out:

```toml
[ui.toast]
delivery = "herdr"   # in-app toasts only; "off" silences them entirely

[ui.sound.agents]
claude = "off"       # no state-change sounds from Claude agents (all of them)
```

The dashboard also runs outside herdr: `~/src/octopilot/dashboard.sh`.
It needs `uv` (which provides `rich`); there is nothing else to install.

The takoyaki band in the prompter's claude (`plugin/hooks/register.tsx`) follows the
engine: it is busy while the engine acts (`header.engine.state` is `acting`), and
falls back to the prompter's own turns when there is no fresh view.

## GUI (herdr-app)

`herdr/open-gui.sh` builds Octopilot in the Herdr app (`herdr-app`, its
terminals run in herdr-core) as one space with two tabs by default, OCTOPILOT and the code tab, plus the engine tab third with `--show-engine`:

| Tab | Holds |
|---|---|
| **OCTOPILOT** | the `octopilot-kitchen` mod, a pane 300 px tall, on the left, over the `octopilot-chat` mod (the prompter as a chat, its input at the bottom); on the right the `octopilot-dashboard` mod (the table below), full height (`props`: `dir`, `state_dir`; its pane is named by `pane.rename`, the mod reads no name) |
| **Octopilot (code changes)** | the code agent's own claude (`octopilot-code`, a claude started in this checkout, an hc terminal that is never parked) |
| **engine** | the prompter (`octopilot-prompter`, `/octopilot:prompter`) and `dashboard.sh --headless` (`octopilot-headless`, the engine's host: it runs `scan.py tick` and `drain`), each an hc terminal. Parked (hidden) by default; `--show-engine` shows them in a third tab (see below) |

Every GUI run keeps OCTOPILOT, code and (when shown) engine at tabs 1-3 in that order (`place_tabs`), so
worker tabs opened later land after them. The `--terminals` layout keeps OCTOPILOT and the code tab
in the same order.

The space is named `Octopilot` (`LABEL`): `space.new` does not keep the name it
is given here, so the run calls `space.rename` when the space is not named so (best
effort: a refusal is warned about). A remote herdr-app lists a new terminal a moment
after it is made, so the run waits up to 15 s for each one it locates.

The engine is parked by default. A run parks its two terminals after building
(`herdr-app do terminal.park`): the pane goes, the process keeps running and core
keep is off, so the empty engine tab goes too. They are found by their
`octopilot.agent` labels, not by panes, so a rerun finds them parked. A parked
headless keeps its space in an `octopilot.space` label (parking removes
`herdr.space`). A parked terminal is shown with `terminal.peek` (the chef in the
kitchen, the chat's Terminal button, the dashboard's `v` and `a`), which works on
it. `--show-engine` shows parked engine terminals (`terminal.keep`: the first takes
a tab named `engine`, the others go beside it) and, as every GUI run does, keeps
the engine tab third; a run with it leaves them shown. A run without it parks
them again, also a shown engine. `--hide-engine` is still accepted and means the
default. `--terminals` refuses on a parked engine and lists the parked terminals,
each to show with `herdr-app do terminal.keep`.

Every run writes its output to `$OCTOPILOT_STATE_DIR/open-gui.log` (the default
state dir without the variable): the last run, and the one before as `open-gui.log.1`.
A run from the launcher (`--close-launcher`) that fails keeps its pane open: it
prints the error and the log path and waits for Enter. Success closes the pane
as before. This covers `set -e` failures too (the ERR trap names the line when it can).
The log's tee ignores HUP, so a pane closed mid-run still writes the whole log.

A run first checks that herdr-app has a window to answer (`herdr-app status`, the machine it
runs on). After a reboot or a logout there is none: with a graphical session on that
computer (`WAYLAND_DISPLAY` or `DISPLAY` in the systemd user environment) the run opens the
window with `systemd-run --user` and waits up to 30 s for it; without one it stops before
it touches the space, and says so (log in on that computer, or run herdr-app there, then
run Open Octopilot again). A run also holds the engine's `tick.lock` and `exec.lock` until it
exits: a tick or a drain started meanwhile prints `tick already running` and acts on nothing.

To recover from the Mac when the Octopilot space is on caprica and caprica's window is gone:
open a space on caprica (`herdr-app do space.new --machine caprica`, or the new-space dialog),
then run Open Octopilot from it. The palette entry opens its tab on the focused space's
machine, so the run is caprica's, and the window is opened there as above.

Viewed from another computer (the Octopilot space is on a remote machine, see
`CONTRACT.md`), the mods run their commands and read `view.json` on that machine,
from their `dir` and `state_dir` props, and poll it every 2 s (no folder watch).
Their headers say `· on <machine>`.

Rerunning finds what is there. A parked headless whose space is closed means
octopilot was closed with it: its engine terminals of this checkout are ended,
and it is built again. After a core restart the parked engine is gone, but the
space and its code agent are back: the engine's terminals start in the space named
`Octopilot` (or the code agent's), in a new tab that the run parks. An engine
terminal not running its claude (or the headless's uv) is ended and started again.
The launcher never ends the code agent: it is your own claude session.

An `octopilot` agent from before the engine (the old messenger) in this checkout is
retired by the next `open-gui.sh` run: its chat and kitchen labels move to the prompter
(or the code agent), its headless and any old curses dashboard are ended, and the
headless starts again. The prompter keeps running: `/clear` it, then run
`/octopilot:prompter`.

The code agent is not an engine terminal: its claude is the terminal of the code
tab, so its pickers and sub-agents are Claude's own, and the engine flags do not
touch it.

A code agent from an earlier run is converted on a rerun. A parked agent (the
previous version's chat tab) is labelled with the space and, if that does not show
it, given a tab with `terminal.keep`; a shown agent is never moved. Only when it is
then alone in a tab of this space does that tab take the name and the old chat tab
go; otherwise the run warns and leaves it. A side panel an earlier run put beside the
agent (its label `octopilot.side=<tab>:<pane>`) is closed when that label names this
tab and `pane.rename` finds the pane, and the label is removed. A tab that holds a
terminal other than the agent's is refused, never closed.

In the `--terminals` layout, the dashboard's `a` peeks the prompter, as the GUI layout does, and the code agent stays a terminal in its
own "Octopilot (code changes)" tab (the TUI layout has no mods).

Every engine terminal starts through the user's interactive shell (`$SHELL -ic
'exec "$@"'`), so the gateway env in `~/.zshrc.local` reaches claude. herdr-app
already runs the argv of `space.new` and `tab.new` in the shell (and keeps the
shell open after the program), but runs `pane.split` argv as given, so the
helpers beside the engine's host are wrapped by open-gui.sh (`shell_argv`). It
uses `exec`, not herdr's keep-shell form: a shell left behind would keep a dead
agent's terminal alive and labelled, and the reruns would never restart it.

The headless dashboard runs the loops and writes `view.json`; the mods only
render it and send actions. The kitchen is the `octopilot-kitchen` mod, a pane
split down from the chat (`pane.split`), moved above it (`pane.swap`), then sized to 300 px
with `pane.resize` (both only when it is created; a rerun never moves or resizes it).
The run records the chat's pane on the headless (`octopilot.chat=<tab>:<pane>`)
and the kitchen's (`octopilot.kitchen=<tab>:<pane>`). A record counts only when its
tab is this space's OCTOPILOT tab, and a rerun keeps the kitchen while its pane
exists (a `pane.rename` probe), else splits a new one. The
steps are best effort: a failure is warned about and the run goes on. A tab from
an earlier run has no `octopilot.chat` record, so a rerun cannot split its kitchen:
it says so and leaves the tab as it is. Add it once by hand: focus the chat pane,
take its id (`herdr-app status` `focused_pane`) and the OCTOPILOT tab's id
(`herdr-app ls`), record `octopilot.chat=<tab>:<pane>` on the headless terminal, and
rerun `open-gui.sh`.

Each claude starts with `claude --session-id <uuid>` and is labelled
`octopilot.session=<uuid>`. Its transcript (`~/.claude/projects/*/<uuid>.jsonl`)
is found from the claude in the terminal's foreground, through its
`~/.claude/sessions/<pid>.json` (which follows `/clear`); the label is only the
fallback when no claude runs there. `scan.sh speech --json` reads the
engine's last lines (`header.engine.last`), `scan.sh chat --json [--after <id>]` the prompter's
conversation, and `scan.sh chat-send <text>` / `chat-keys <keys>` type into the
prompter. Under the TUI backend, or with nothing running, they report
`"status": "offline"`.

**Launch and install.** `herdr-app/install.sh` links each
`herdr-app/mods/<name>` into `~/.config/herdr-app/mods/` (a real file or folder
there is refused, never replaced), removes our links whose mod is gone
(links into this checkout's mods whose folder has no `mod.json`; a foreign link is kept;
a folder holding only herdr's generated files is deleted too), and adds an
`octopilot` entry to
`programs.json` that runs `herdr/open-gui.sh --close-launcher`. The
`octopilot-launcher` mod's palette entry, "Open Octopilot" (ctrl-shift-p), runs
the same thing in a new tab, which closes again once the space is up. The
palette entry opens `$OCTOPILOT_DIR`, else `~/src/octopilot`, the checkout installed on this machine
(Octopilot's own processes set `OCTOPILOT_DIR` to theirs); `--dir` moves only
the mod symlinks and the `programs.json` entry to another checkout. The launcher
closes its own pane when the space is up (never the tab: the tab goes only with
its last pane). A failed run keeps that pane, with the error (see the GUI section). `./install.sh` (this repo) runs the
installer when `herdr-app` is installed. It does not bind a key: pass `--key`
to bind `ctrl-alt-o` in `keys.json` as well (backed up first, a key bound to
something else is left alone). Other options: `--dry-run`, `--uninstall`
(removes what is ours, recognised by shape). `HERDR_APP_HOME` picks another app
home, as herdr-app does. A `keys.json` you edit stops getting herdr-app's new
default shortcuts on upgrade, which is why the key is opt-in. A rejected
`programs.json` or `keys.json` leaves a `.error` file, which the installer reports.

**No octopilot layout.** Earlier versions wrote `~/.config/herdr-app/layouts/octopilot.json`
(the kitchen as a floating block of every tab). The installer removes it when it is
ours (recognised by that block), on install and on `--uninstall`; when the app shows
it, the app is switched to `main` first. A layout of that name that is not ours is
left alone. `open-gui.sh --no-layout` is accepted and does nothing.

**Fallback.** `open-gui.sh --terminals` builds the TUI-style layout instead:
one OCTOPILOT tab with the prompter on the left of the curses dashboard (the engine's
host, labelled `octopilot-headless`), plus the code-changes tab as a terminal tab. On a space that already has the GUI
layout it refuses, and a rerun on a space with the curses dashboard of that
layout refuses too, so two dashboard loops never run.

To test it next to a live TUI octopilot, run it from a worktree with a scratch
state dir. Without `OCTOPILOT_STATE_DIR` it refuses, so a worktree never
shares the live `state.json`:

```sh
OCTOPILOT_STATE_DIR=~/.local/state/octopilot-gui-test \
HOME_REPOS="<owner/repo>" ISSUE_ORGS="<org>" \
  ~/src/octopilot/.worktrees/<branch>/herdr/open-gui.sh
```

`HOME_REPOS`, `ISSUE_ORGS` and `CLONE_ROOTS` narrow what the test scans; only
the values you set are passed on, so config.sh's defaults keep applying to
everything else. The launcher only touches this checkout's terminals (matched by their
`octopilot.dir` label, since labels are global): an `octopilot` agent from another
checkout is left as it is. Its own sessions (the engine tab, the headless
dashboard and the code tab) get `OCTOPILOT_BACKEND=hc`, `OCTOPILOT_DIR` and
`OCTOPILOT_STATE_DIR` from this script. The workers that scan.py spawns get them
from scan.py, so the test is only isolated as far as that hand-off goes.

Plugins: settings.json's `CLAUDE_CODE_PLUGIN_DIRS` loads the installed
`octopilot` plugin into every session. A worktree's `--plugin-dir` does not
replace it: the installed copy still wins, and the worktree's takoyaki hook
never loads (`claude -p ... --plugin-dir <worktree>/plugin --debug` prints
`octopilot: hooks module not loaded: another plugin of that name loads first`).
Setting the env var in the process does not replace it either, since settings
env overwrites the process env. What works is `--settings`, which open-gui.sh
and the GUI spawns pass: `scan.sh plugin-settings` prints that JSON, with the
worktree's plugin in place of the installed one and the other plugin dirs
kept, or nothing on the live checkout. Checked with `claude --settings
"$(scan.sh plugin-settings)" plugin list --json`: `octopilot@inline` points at
the worktree, and `arxiu-bibliotecaria@inline` and `taller@inline` remain.

Only the GUI spawns carry `OCTOPILOT_DIR`, `OCTOPILOT_STATE_DIR` and the
`--settings` override. A TUI worker opened as a workspace (`WORKER_HOST=space`) gets
neither, so it runs the live scan.sh and plugin. A TUI tab worker gets the two
variables (`tab create --env`), but not the `--settings` override, so it loads the
installed plugin.

Close the test space with `herdr-app do space.close --id <space> --confirmed true`.

## Upgrading from the orchestrator

The engine replaces the messenger (the `octopilot` agent). Before switching, clear
the old requests: `scan.sh inbox` lists them, and the engine runs them on its first
drain. Either let them run, or remove the stale `REQUEST` entries from the `inbox`
list of `state.json` while the dashboards are stopped (there is no command for that).

Stage the switch while the orchestrator still runs (layers 1-3): run with
`OCTOPILOT_ENGINE=plan` first, which logs what the engine would do, and
`scan.py drain --plan` shows what a drain would do with each request. Set `on` once
that looks right. Under this layer the orchestrator is retired, so `plan` leaves
nobody acting: deploy with the engine already `on`.

Rerun the launcher right after pulling this layer. Until it runs, the old headless
keeps its code and engine mode: with the engine off nothing drains the requests, and
the old orchestrator's doorbell exits at once.

- **GUI**: run Open Octopilot (or `herdr/open-gui.sh`). When an orchestrator is still
  running, it retires it and restarts the headless. If you closed the orchestrator
  yourself before pulling, nothing restarts the headless: end the old
  `octopilot-headless` terminal first (`hc kill`, or close its tab), then rerun the
  launcher. Then `/clear` the prompter and run `/octopilot:prompter`, and reload the
  mods (or restart herdr-app) so the new mod code reads `view.json`.
- **TUI**: close the old `octopilot` pane (the messenger). Restart the dashboard pane:
  `q`, then reopen it (`prefix+a`). The curses dashboard is the engine's host and keeps
  running the old code until it is restarted. Then `/clear` the prompter and run
  `/octopilot:prompter` in it.

To change `OCTOPILOT_ENGINE` later, end the `octopilot-headless` terminal and then rerun
the launcher (GUI) or reopen the dashboard pane (TUI): a rerun keeps a running headless
as it is, so the mode it started with stays. The TUI dashboard pane is a herdr plugin
pane and takes the herdr server's environment (`open.sh` passes none), so for the TUI
the variable must be set in the shell that starts herdr, and changing it means
restarting the herdr server, not just the pane.

**Rollback.** Previous main with `OCTOPILOT_ENGINE=off` is not enough: the previous
launcher never restarts a running headless, which keeps acting, and it starts a new
messenger. End the `octopilot-headless` terminal first (GUI: `hc kill` it, or close its
tab; TUI: `q` in the dashboard pane). Unset any `OCTOPILOT_ENGINE=on` override (for the
TUI, in the shell that starts herdr, then restart herdr), then rerun the old launcher
(GUI) or reopen the dashboard pane with the old code (TUI). The old launcher may warn
that the prompter pane is not recorded in the tab (`octopilot.chat`): the chat and
kitchen records lived on the killed headless. The existing tab and kitchen stay, and
the warning is harmless.

## What it shows

| Section | Rows |
|---|---|
| 🟡 Running | `WORKING`, and anything spawned since the last GitHub refresh. Also `MERGED`, `RESTACK`, `REVIEW`, `BOOTING` and `FEEDBACK` with a ⏭ badge (`⏭ cleaning up`, `⏭ held: paused`, ...) for what the engine does next; a REVIEW, BOOTING or FEEDBACK whose agent waits for you is in Needs you instead |
| 🔴 Needs you | `CAPPED`, `READY`, `PUSHED`, `STALLED`, `CLOSED`, `BLOCKED` (the agent reported blocked, failed or stalled), `engine_error` cards and failed sends, agents waiting on a question, and a read-only investigation's `ready` report until acked |
| 💤 On hold | `HOLD`: cards you held with `d` or `hold`, a fix PR waiting on a reviewer (`waiting on review`, or `waiting on re-review` after requested changes; past `RE_REVIEW_QUIET_HOURS` that re-review shows as READY, `re-review overdue`), a comment waiting on its author, pushed commits you acked (`ack`: waiting on re-request), and agents that reported `held` (held by the agent: its own `held` report, which only the agent resumes, by a message). A hold wakes on approval, draft turned ready, new feedback, a re-request, or a newer blocked, failed, stalled or capped report; a red CI does not wake it. Holds keep their tab or workspace and hold no slot |
| ✔ Done | `DONE` (finished only), `GONE`, and a read-only investigation's report once acked. Hidden by default; `D` shows them |
| ⚪ Pick next | `PROPOSE` |

Agent state comes from herdr every 3s, heartbeats, rounds and reports the moment
they are written, and GitHub (PR state, CI) every `DASHBOARD_REFRESH_SECS`
(config.sh, 120s) or right after an event that changes the table. Several open
dashboards share one refresh. GitHub notifications are polled every 60s
(`NOTIFY_POLL_SECS`) and trigger an early refresh. Each GitHub pass runs `scan.py tick`
(`snapshot`, which saves nothing, where the engine may not run), and the engine acts on
the ⏭ rows itself. `OCTOPILOT_ENGINE` picks what it does: `on` (the default,
set in config.sh) acts; `plan` logs the tick's row actions as "would ..." without acting,
and `off` acts on nothing. It is read when open-gui.sh (or the TUI dashboard pane)
starts; to change it, see Upgrading from the orchestrator. The dashboard drains the inbox only under `on`, so under
`plan` and `off` requests wait there (`scan.py drain --plan` prints what a drain would
do). Under `on`, the dashboard runs `scan.py drain` at once on each request it sees, and
at least every 60s (`DRAIN_EVERY`). The `g`/`n`/`i` keys and the prompter's commands are
requests, queued for the engine.

## Keys

| Key | Does |
|---|---|
| `↑↓` / `jk` | move |
| `⏎` | go to the agent: in the GUI, its space, then its tab (a settled one comes back), then the agent's terminal; in the TUI, the agent's pane |
| `v` | the agent in a popup over the dashboard; `ctrl+b q` closes it and leaves the agent running |
| `a` | go to the prompter (refused when none runs) |
| `t` | shell in the item's worktree: in the GUI, a new tab in its space (the Octopilot space for a worker tab) |
| `o` | PR or issue in the browser, opened on the computer you are at (the mod runs `xdg-open`, `open` or `start`), not on the octopilot's machine; no URL: `dashboard.sh --action browser` |
| `g` / `n` | on a Pick next row: queue `go <key>` / `no <key>` for the engine (press twice; past `WIND_DOWN_AT`, or with the slots full (`ACTIVE + UNTRACKED` at `MAX_ACTIVE`), the second press confirms starting anyway, and the alert's `g` sends it with `(confirmed past wind-down)` and/or `(confirmed over cap)`) |
| `R` | on a CAPPED row: grant another `MAX_REVIEW_ROUNDS` review rounds (press twice) |
| `e` / `x` | on a PUSHED (stale) row: queue a re-review / ack the new commits (press twice; the acked row goes to On hold). On a read-only investigation's `ready` report, `x` marks it read and it goes to Done |
| `i` | start an investigation: type the repo, the question, and whether it is read-only. Past `WIND_DOWN_AT` or with the slots full, the second press confirms starting anyway, as for `g` |
| `P` | pause / resume octopilot (press twice) |
| `z` | detach a tracked item: closes its tab (or its workspace or space), keeps the worktree, stops tracking it (press twice) |
| `del` / `⌫` | remove a tracked item: deletes its tab (or its workspace or space) and worktree, stops tracking it (press twice). The confirm says what goes: `tab and worktree deleted`, `workspace and worktree deleted`, or `worktree kept` for an item with no session. Refused on MERGED and RESTACK rows: the engine handles those |
| `r` | refresh from GitHub now |
| `d` | on a tracked card: hold it / resume it (press twice; the prompter's `hold` / `unhold`). It goes to On hold, keeps its tab or workspace and holds no slot; the engine still merges and cleans up |
| `D` | show / hide the Done rows (DONE, GONE; hidden by default) |
| `q` | quit |
