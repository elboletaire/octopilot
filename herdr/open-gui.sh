#!/usr/bin/env bash
# The GUI twin of open.sh: the Octopilot space in herdr-app, with two tabs by default
# (a third, engine, with --show-engine):
#   OCTOPILOT: the octopilot-kitchen mod on the left, over the octopilot-chat mod (the
#     kitchen KITCHEN_PX tall); on the right the octopilot-dashboard mod, full height
#   Octopilot (code changes): the code agent's own claude (a terminal, never parked)
#   engine: the headless dashboard (the engine's host, dashboard.sh --headless) and the
#     prompter beside it, as hc terminals; parked after building by default (no pane,
#     still running), so the third tab is not shown. --show-engine shows it; a rerun
#     without it parks them again. An agent of an earlier launcher is retired here.
# Each claude is labelled octopilot.agent=<name> and octopilot.session=<uuid>
# (its transcript, read by scan.py speech and chat). --terminals builds the
# TUI-style layout instead (the curses dashboard is the host, the prompter on its left).
# --no-layout is accepted and does nothing (there is no octopilot layout any more).
# --close-launcher closes this terminal's own pane at the end (the tab goes with
# its last pane), for the key or the palette (programs.json). A failed run keeps that
# pane open (the error, the log, Enter). From a worktree it needs OCTOPILOT_STATE_DIR
# (see herdr/README.md).
set -euo pipefail
APP="herdr-app"
HC="hc"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# The live checkout, as settings.json loads it (scan.py installed_dir). OCTOPILOT_DIR is
# the running checkout, so a worktree's would be wrong here.
LIVE_DIR="$("$DIR/scan.sh" installed-dir 2>/dev/null || true)"
LIVE_DIR="${LIVE_DIR:-$(cd "$HOME/src/octopilot" 2>/dev/null && pwd -P || echo "$HOME/src/octopilot")}"
LABEL="Octopilot"
ORCH_AGENT="octopilot"                 # the label of a retired agent: the migration only
PROMPTER_AGENT="octopilot-prompter"    # scan.py's PROMPTER_AGENT
HEADLESS_AGENT="octopilot-headless"    # scan.py's HEADLESS_AGENT: the engine's host
CODE_AGENT="octopilot-code"            # scan.py's CODE_AGENT
# The roles of the mod panes. Each pane is named role-<hash of the checkout's dir> (pane_name),
# and its mod finds it in panes() by the name given in props.pane_name.
DASHBOARD_PANE="octopilot-dashboard"
PROMPTER_PANE="octopilot-prompter"
KITCHEN_PANE="octopilot-kitchen"
KITCHEN_PX=300                         # the kitchen's height above the chat
MAIN_TAB="OCTOPILOT"
ENGINE_TAB="engine"
CODE_TAB="Octopilot (code changes)"
CODE_CWD="$DIR"                        # the octopilot dir: its AGENTS.md is the code agent's guide

TERMINALS=0
CLOSE_LAUNCHER=0
HIDE_ENGINE=1
# The terminals this run has ended, space-separated and padded: find_agent never adopts them.
ENDED=" "
LOG=""
TEE_PID=""
# The terminal's stdout and stderr, kept on fds 3 and 4 while the log tee runs.
exec 3>&1 4>&2
# Puts stdout and stderr back on the terminal, then ends the log tee once it has drained the
# pipe. The 5 s cap is for a daemon that inherited the pipe: on_exit closes the saved fds
# that bash keeps for a failing function run with a redirection (fd 10 and up).
flush_log() {
  exec 1>&3 2>&4
  if [ -n "$TEE_PID" ]; then
    for _ in $(seq 50); do kill -0 "$TEE_PID" 2>/dev/null || break; sleep 0.1; done
    kill "$TEE_PID" 2>/dev/null || true
    wait "$TEE_PID" 2>/dev/null || true
    TEE_PID=""
  fi
}
# Runs at every exit, set -e failures included. A failed run from the launcher keeps its
# pane, so the error, the log and Enter stay readable until Enter closes it.
on_exit() {
  local status=$?
  trap - EXIT
  exec 10>&- 11>&- 12>&- || true
  if [ "$status" -ne 0 ] && [ "$CLOSE_LAUNCHER" = 1 ]; then
    if [ -n "$LOG" ]; then echo "open-gui.sh: failed (status $status); the log is $LOG" >&2; fi
    flush_log
    printf 'Press Enter to close this pane. ' >&4
    if [ -t 0 ]; then read -r _ || true; fi
  fi
  flush_log
  exit "$status"
}
trap on_exit EXIT
trap 'echo "open-gui.sh: line $LINENO: $BASH_COMMAND failed" >&2' ERR
for arg in "$@"; do
  case "$arg" in
    --terminals) TERMINALS=1 ;;
    --close-launcher) CLOSE_LAUNCHER=1 ;;
    --no-layout) ;;
    --hide-engine) HIDE_ENGINE=1 ;;
    --show-engine) HIDE_ENGINE=0 ;;
    -h|--help) echo "usage: open-gui.sh [--terminals] [--show-engine] [--hide-engine] [--no-layout] [--close-launcher]"; exit 0 ;;
    *) echo "open-gui.sh: unknown argument $arg" >&2; exit 2 ;;
  esac
done

if [ "$DIR" != "$LIVE_DIR" ] && [ -z "${OCTOPILOT_STATE_DIR:-}" ]; then
  echo "open-gui.sh: $DIR is not the live octopilot ($LIVE_DIR), and would" \
    "share its state.json. Set OCTOPILOT_STATE_DIR to a scratch state dir." >&2
  exit 1
fi

# The scope knobs and the engine mode the caller set, read before config.sh
# supplies its defaults: only these are forwarded, so the defaults keep applying.
declare -a scope=()
for v in HOME_REPOS ISSUE_ORGS CLONE_ROOTS OCTOPILOT_ENGINE WORKER_HOST SETTLE_WORKER_TABS; do
  if [ -n "${!v:-}" ]; then scope+=("$v=${!v}"); fi
done

set -a
# shellcheck source=../config.sh
source "$DIR/config.sh"
set +a

# Everything this run prints goes to the log too: the last run, and the one before as .1.
LOG="$STATE_DIR/open-gui.log"
mkdir -p "$STATE_DIR"
if [ -e "$LOG" ]; then mv -f "$LOG" "$LOG.1"; fi
# The tee ignores HUP: a pane closed while the script runs still writes the whole log.
exec > >(trap '' HUP; exec tee "$LOG") 2>&1
TEE_PID=$!

# The engine's locks, held for the whole run, in the order the engine takes them (tick, then
# exec): a tick or a drain finds them held and says so, so it acts on nothing while workers
# are restored. Released when this process exits.
exec 8>"$STATE_DIR/tick.lock"
if ! flock -n 8; then
  echo "open-gui.sh: waiting for the engine's tick to finish" >&2
  if ! flock -w 600 8; then
    echo "open-gui.sh: $STATE_DIR/tick.lock is still held after 600 s; a tick is running" >&2
    exit 1
  fi
fi
exec 9>"$STATE_DIR/exec.lock"
if ! flock -n 9; then
  echo "open-gui.sh: waiting for the engine's drain to finish" >&2
  if ! flock -w 600 9; then
    echo "open-gui.sh: $STATE_DIR/exec.lock is still held after 600 s; a drain is running" >&2
    exit 1
  fi
fi
# The children do not inherit the locks: a herdr-app or hc that outlived this run would
# otherwise keep the engine locked.
herdr-app() { command herdr-app "$@" 8>&- 9>&-; }
hc() { command hc "$@" 8>&- 9>&-; }

# Every session and pane started here gets the hc backend, this octopilot's
# directory (the skills' scan.sh, see OCTOPILOT_DIR in plugin/skills), and the
# state dir and scope knobs a test sets.
envs=(OCTOPILOT_BACKEND=hc "OCTOPILOT_DIR=$DIR")
if [ -n "${OCTOPILOT_STATE_DIR:-}" ]; then envs+=("OCTOPILOT_STATE_DIR=$OCTOPILOT_STATE_DIR"); fi
envs+=("${scope[@]+"${scope[@]}"}")

# The name of a mod pane: its role and a short hash of this checkout's dir, so two
# checkouts in one window each find their own.
DIR_HASH=$(printf '%s' "$DIR" | sha1sum | cut -c1-8)
pane_name() {
  printf '%s-%s' "$1" "$DIR_HASH"
}

# The props of the mods, which read their octopilot from them.
MOD_PROPS="$(jq -nc --arg dir "$DIR" --arg state "$STATE_DIR" '{dir: $dir, state_dir: $state}')"
DASH_PROPS="$(jq -nc --argjson mod "$MOD_PROPS" --arg name "$(pane_name "$DASHBOARD_PANE")" \
  '$mod + {pane_name: $name}')"
PROMPTER_PROPS="$(jq -nc --argjson mod "$MOD_PROPS" --arg name "$(pane_name "$PROMPTER_PANE")" \
  '$mod + {pane_name: $name}')"
# The kitchen's pane is named by pane.rename to the same name, which its mod finds by pane_name.
KITCHEN_PROPS="$(jq -nc --argjson mod "$MOD_PROPS" --arg name "$(pane_name "$KITCHEN_PANE")" \
  '$mod + {pane_name: $name}')"

# `herdr-app ...` with its output on stdout; a failure stops here with the
# output it printed, so a refused command says why.
app() {
  local out
  if ! out=$("$APP" "$@" 2>&1); then
    echo "open-gui.sh: $APP $* failed: $out" >&2
    exit 1
  fi
  printf '%s\n' "$out"
}

# herdr-app's status, from its exit code: 0 when the window answers, 2 when there is none
# (no socket, or a stale one that refuses), anything else when it did not answer in time.
# Only 2 may start a window; a second one over a slow answer would be a second window.
window_status() {
  local rc=0
  timeout 5 "$APP" status >/dev/null 2>&1 8>&- 9>&- || rc=$?
  return "$rc"
}

# Makes sure herdr-app has a window to answer on, before anything reads or changes a
# space: after a reboot there is none. With a graphical session the window is opened
# with systemd-run (it outlives this run), else the run stops here, before any step.
ensure_window() {
  local i rc app_home="${HERDR_APP_HOME:-$HOME/.config/herdr-app}" session
  rc=0
  window_status || rc=$?
  if [ "$rc" = 0 ]; then return 0; fi
  if [ "$rc" != 2 ]; then
    echo "open-gui.sh: herdr-app did not answer on $(uname -n) (status $rc); no window is started" \
      "over it. Check herdr-app there, then run Open Octopilot again." >&2
    exit 1
  fi
  session=$(systemctl --user show-environment 2>/dev/null || true)
  if ! grep -qE '^(WAYLAND_DISPLAY|DISPLAY)=' <<<"$session"; then
    echo "open-gui.sh: herdr-app has no window on $(uname -n) ($app_home/herdr-app.sock does not answer)" \
      "and no graphical session to open one in: log in on $(uname -n)'s screen (or enable autologin)," \
      "or run herdr-app there, then run Open Octopilot again." >&2
    exit 1
  fi
  local run=(systemd-run --user --collect --quiet)
  if [ -n "${HERDR_APP_HOME:-}" ]; then run+=("--setenv=HERDR_APP_HOME=$HERDR_APP_HOME"); fi
  "${run[@]}" "$APP" >/dev/null 2>&1 8>&- 9>&- || true
  for i in $(seq 30); do
    if window_status; then return 0; fi
    sleep 1
  done
  echo "open-gui.sh: started herdr-app with systemd-run but it did not answer in 30 s;" \
    "see journalctl --user" >&2
  exit 1
}

# A JSON argv array from its arguments, as herdr-app takes it.
argv_json() {
  jq -nc '$ARGS.positional' --args -- "$@"
}

# A JSON argv run in the user's interactive shell (the gateway env of
# ~/.zshrc.local). pane.split runs its argv as given; exec ends the terminal
# with its program.
shell_argv() {
  local sh="${SHELL:-}"
  if [ -z "$sh" ]; then sh=$(getent passwd "$(id -un)" | cut -d: -f7); fi
  jq -nc --arg sh "$sh" --argjson argv "$1" '[$sh, "-ic", "exec \"$@\"", $sh] + $argv'
}

# The argv of the headless dashboard, the engine's host (run through the shell by herdr-app).
headless_argv() {
  argv_json env "${envs[@]}" bash "$DIR/dashboard.sh" --headless
}

# A fresh session id for a claude that is started with --session-id.
new_session() {
  uuidgen | tr '[:upper:]' '[:lower:]'
}

# The --settings that put a worktree's octopilot plugin in place of the
# installed one (empty on the live checkout), for every claude started here.
SETTINGS="$(OCTOPILOT_DIR="$DIR" "$DIR/scan.sh" plugin-settings)"

# argv for a claude session: its session id (empty: none), an optional prompt, a folder (empty:
# the pane's own, `env -C` otherwise) and a model (empty: the default). A 5th argument "resume"
# resumes session $1 (--resume, no prompt: the skill is already in the conversation).
session_argv() {
  local args=(env)
  if [ -n "${3:-}" ]; then args+=(-C "$3"); fi
  args+=("${envs[@]}" claude)
  if [ -n "$SETTINGS" ]; then args+=(--settings "$SETTINGS"); fi
  if [ -n "${4:-}" ]; then args+=(--model "$4"); fi
  if [ "${5:-}" = resume ]; then
    args+=(--resume "$1")
  elif [ -n "${1:-}" ]; then
    args+=(--session-id "$1")
  fi
  if [ -n "${2:-}" ]; then args+=("$2"); fi
  argv_json "${args[@]}"
}

# "<session>\t<cwd>" of a launcher's claude as its last run recorded it, when its transcript
# exists and its folder still does (scan.sh launcher-session); nothing otherwise: a fresh start.
recorded_session() {
  local out
  out=$(env "${envs[@]}" "$DIR/scan.sh" launcher-session "$1" 2>/dev/null) || out=""
  if [ -z "$out" ]; then return 0; fi
  if [ ! -d "${out#*$'\t'}" ]; then
    echo "open-gui.sh: the $1 claude's folder ${out#*$'\t'} is gone; it starts fresh" >&2
    return 0
  fi
  echo "$out"
}

# True when the terminal is labelled as run from this checkout. An unlabelled
# one is never ours: only labelled terminals are ever killed or reused.
is_mine() {
  local d
  d=$("$HC" get "$1" --json | jq -r '.labels["octopilot.dir"] // empty')
  [ -n "$d" ] && [ "$(realpath -m "$d")" = "$DIR" ]
}

# The address of this checkout's terminal labelled as an octopilot agent, if
# any. Pure lookup: it never kills (see reap_dead).
find_agent() {
  local t
  for t in $("$HC" ls --json --label "octopilot.agent=$1" | jq -r '.terminals[].address'); do
    case "$ENDED" in *" $t "*) continue ;; esac
    if is_mine "$t"; then echo "$t"; return 0; fi
  done
}

# The engine's host is this checkout's headless dashboard: the terminal the chat, the
# kitchen and the space are recorded on. An unlabelled one is never adopted.
find_host() { find_agent "$HEADLESS_AGENT"; }

# The value of a label of a terminal, if it has it.
label_of() {
  "$HC" get "$1" --json | jq -r --arg k "$2" '.labels[$k] // empty'
}

# Ends a terminal of this checkout: kill, wait (bounded) until herdr lists it live no
# more, then rm. Remembered as ended, so find_agent never adopts it while it dies.
end_terminal() {
  local i
  "$HC" kill "$1" >/dev/null || true
  for i in $(seq 10); do
    "$HC" ls --json | jq -e --arg t "$1" '.terminals[] | select(.address == $t)' >/dev/null || break
    sleep 0.5
  done
  "$HC" rm "$1" >/dev/null || true
  ENDED="$ENDED$1 "
}

# Ends this checkout's terminals of an agent that are not running its program (a
# shell herdr kept after claude exited, a dead headless), so it starts again.
reap_dead() {
  local t info fg created want=claude
  if [ "$1" = "$HEADLESS_AGENT" ]; then want=uv; fi
  for t in $("$HC" ls --json --label "octopilot.agent=$1" | jq -r '.terminals[].address'); do
    is_mine "$t" || continue
    info=$("$HC" get "$t" --json)
    fg=$(jq -r '.foreground.name // empty' <<<"$info")
    if [ "$fg" = "$want" ]; then continue; fi
    # Still starting: its shell has not exec'd the program yet (created_ms, epoch ms).
    created=$(jq -r '.created_ms // 0' <<<"$info")
    case "$fg" in
      zsh|bash|sh|fish)
        if [ $(( $(date +%s%3N) - created )) -lt 30000 ]; then continue; fi ;;
      *)
        # The code agent's terminal is the person's own: only a shell is reaped there.
        if [ "$1" = "$CODE_AGENT" ]; then continue; fi ;;
    esac
    end_terminal "$t"
  done
}

# "space tab pane" of a terminal as herdr-app shows it, if it shows it.
locate() {
  "$APP" ls | jq -r --arg t "$1" '.spaces[] as $s | $s.tabs[] as $tab |
    $tab.terminals[] | select(.terminal == $t) | [$s.id, $tab.id, .pane] | @tsv'
}

# The id of a tab of a space by its name, if it has one. A worker's tab may take the same
# name: the hinted tab $3 (the one holding this checkout's expected terminal) wins when it
# is one of them, else the first named so.
tab_named() {
  local space=$1 name=$2 hint=${3:-} ids
  ids=$("$APP" ls | jq -r --arg s "$space" --arg n "$name" \
    '.spaces[] | select(.id == $s) | .tabs[] | select(.name == $n) | .id')
  if [ -n "$hint" ] && grep -qxF "$hint" <<<"$ids"; then
    echo "$hint"
    return 0
  fi
  head -n1 <<<"$ids"
}

# The tab of space $2 that holds this checkout's agent $1, if it runs there: a hint for tab_named.
agent_tab() {
  local t located
  t=$(find_agent "$1")
  if [ -z "$t" ]; then return 0; fi
  located=$(locate "$t")
  if [ -n "$located" ] && [ "$(cut -f1 <<<"$located")" = "$2" ]; then cut -f2 <<<"$located"; fi
}

# The tab the host's octopilot.chat or octopilot.kitchen record names, if any: a hint for the
# OCTOPILOT tab in tab_named.
recorded_main_tab() {
  local host v
  host=$(find_host)
  if [ -z "$host" ]; then return 0; fi
  for v in octopilot.chat octopilot.kitchen; do
    v=$(label_of "$host" "$v")
    if [ -n "$v" ]; then echo "${v%%:*}"; return 0; fi
  done
}

# True when herdr-app shows a space with this id.
space_exists() {
  "$APP" ls | jq -e --arg s "$1" '.spaces[] | select(.id == $s)' >/dev/null
}

# Labels a terminal as an agent of this checkout: its name, its dir, and for a
# claude its session id.
name_agent() {
  local labels=("octopilot.agent=$2" "octopilot.dir=$DIR")
  if [ -n "${3:-}" ]; then labels+=("octopilot.session=$3"); fi
  "$HC" label "$1" "${labels[@]}" >/dev/null
}

# Starts a terminal with argv (JSON, unwrapped) beside the host's pane, or in a tab of
# the space while it is parked (pane empty). Prints its address.
start_engine_term() {
  local space=$1 pane=$2 direction=$3 argv=$4 name=$5 created term p
  if [ -n "$pane" ]; then
    created=$(app do pane.split --direction "$direction" --pane "$pane" \
      --argv "$(shell_argv "$argv")")
  else
    created=$(app do tab.new --space "$space" --argv "$argv" --focus false)
  fi
  term=$(jq -r '.terminal' <<<"$created")
  p=$(await_shown "$term" | cut -f3) || true
  if [ -n "$p" ]; then app do pane.rename --pane "$p" --name "$name" >/dev/null; fi
  echo "$term"
}

# Names a mod pane (its id, then its name). Best effort: a pane that cannot be named
# is warned about, and its copy falls back to the only pane of its mod.
name_mod_pane() {
  local out
  if [ -z "$1" ]; then
    echo "open-gui.sh: no pane to name $2" >&2
    return 0
  fi
  if ! out=$("$APP" do pane.rename --pane "$1" --name "$2" 2>&1); then
    echo "open-gui.sh: could not name pane $1 $2: $out" >&2
  fi
}

# Starts the prompter, labelled with its claude's session: the recorded one resumed, else fresh.
add_prompter() {
  local session term recorded cwd argv
  recorded=$(recorded_session "$PROMPTER_AGENT")
  if [ -n "$recorded" ]; then
    IFS=$'\t' read -r session cwd <<<"$recorded"
    argv=$(session_argv "$session" "" "$cwd" "$MODEL_PROMPTER" resume)
  else
    session=$(new_session)
    argv=$(session_argv "$session" "/octopilot:prompter" "" "$MODEL_PROMPTER")
  fi
  term=$(start_engine_term "$1" "$2" "$3" "$argv" prompter)
  name_agent "$term" "$PROMPTER_AGENT" "$session"
}

# Starts the engine's host (the headless, or the --terminals curses dashboard) beside pane
# $2 in direction $3, with argv $4, its pane named $5.
add_host() {
  local term
  term=$(start_engine_term "$1" "$2" "$3" "$4" "$5")
  name_agent "$term" "$HEADLESS_AGENT"
}

# The engine tab's helpers beside the host (space, pane), those that are missing.
add_engine_agents() {
  if [ -z "$(find_agent "$HEADLESS_AGENT")" ]; then add_host "$1" "$2" down "$(headless_argv)" headless; fi
  if [ -z "$(find_agent "$PROMPTER_AGENT")" ]; then add_prompter "$1" "$2" right; fi
}

# The prompter on the left of the dashboard pane $2 (--terminals). pane.split only goes
# right or down, so it is split right and swapped with the dashboard.
add_prompter_left() {
  local prompter
  add_prompter "$1" "$2" right
  prompter=$(locate "$(find_agent "$PROMPTER_AGENT")" | cut -f3)
  if [ -n "$prompter" ]; then app do pane.swap --pane "$prompter" --target "$2" >/dev/null; fi
}

# The pane a terminal's label records as <tab>:<pane>, when the tab is $3; else nothing.
# A record of another tab (closed since) is never trusted.
recorded_pane() {
  local v
  v=$("$HC" get "$1" --json | jq -r --arg k "$2" '.labels[$k] // empty' 2>/dev/null || true)
  case "$v" in
    "$3:"*) echo "${v#*:}" ;;
  esac
}

# Records a <tab>:<pane> label on a terminal. Best effort: a refusal is warned about.
record_label() {
  local out
  if ! out=$("$HC" label "$1" "$2=$3" 2>&1); then
    echo "open-gui.sh: could not record $2 on $1: $out" >&2
  fi
}

# The OCTOPILOT tab, unless the space has one: the dashboard mod, and the chat
# mod (the prompter) beside it. The chat's pane is recorded on the host
# (octopilot.chat=<tab>:<pane>) so the kitchen can split from it; herdr-app ls lists no mod panes.
add_main_tab() {
  local created chat pane tab
  if [ -n "$(tab_named "$1" "$MAIN_TAB" "$(recorded_main_tab)")" ]; then return 0; fi
  created=$(app do tab.new --space "$1" --name "$MAIN_TAB" \
    --mod octopilot-chat --props "$PROMPTER_PROPS" --focus false)
  chat=$(jq -r '.pane // empty' <<<"$created")
  if [ -z "$chat" ]; then
    echo "open-gui.sh: the $MAIN_TAB tab has no pane to split the dashboard from" >&2
    exit 1
  fi
  name_mod_pane "$chat" "$(pane_name "$PROMPTER_PANE")"
  created=$(app do pane.split --direction right --pane "$chat" \
    --mod octopilot-dashboard --props "$DASH_PROPS")
  pane=$(jq -r '.pane // empty' <<<"$created")
  name_mod_pane "$pane" "$(pane_name "$DASHBOARD_PANE")"
  tab=$(tab_named "$1" "$MAIN_TAB" "$(recorded_main_tab)")
  if [ -n "$chat" ] && [ -n "$tab" ]; then record_label "$(find_host)" octopilot.chat "$tab:$chat"; fi
}

# Sizes the kitchen (pane $1, in tab $2) to its minimum plus KITCHEN_PX: herdr resizes
# only panes on screen, so the tab is shown first. $3 is 1 when the kitchen is the top
# pane (above the chat): its "up" shrinks it and "down" grows it; below the chat it is
# the other way round. Best effort: a refused move is warned about, and the kitchen
# keeps the split's size.
size_kitchen() {
  local out shrink=down grow=up
  if [ "$3" = 1 ]; then shrink=up; grow=down; fi
  "$APP" do tab.select --id "$2" >/dev/null 2>&1 || true
  "$APP" do pane.resize --direction "$shrink" --pane "$1" --px 10000 >/dev/null 2>&1 || true
  sleep 0.5
  if ! out=$("$APP" do pane.resize --direction "$grow" --pane "$1" --px "$KITCHEN_PX" 2>&1); then
    echo "open-gui.sh: could not size pane $1 to $KITCHEN_PX px: $out" >&2
  fi
}

# The kitchen above the chat in the OCTOPILOT tab (space $1), once. The host records
# the kitchen and the chat as <tab>:<pane>; a record counts only in this space's
# OCTOPILOT tab, and a pane is kept while pane.rename finds it. Best effort: what cannot
# be done is warned about and the run goes on. A tab from an earlier run has no chat
# record, so its kitchen is not split (the note says so).
add_kitchen() {
  local space=$1 host main kitchen chat created top out
  host=$(find_host)
  main=$(tab_named "$space" "$MAIN_TAB" "$(recorded_main_tab)")
  if [ -z "$host" ] || [ -z "$main" ]; then
    echo "open-gui.sh: no $MAIN_TAB tab or $HEADLESS_AGENT agent to add the $KITCHEN_PANE pane to" >&2
    return 0
  fi
  kitchen=$(recorded_pane "$host" octopilot.kitchen "$main")
  if [ -n "$kitchen" ] \
    && "$APP" do pane.rename --pane "$kitchen" --name "$(pane_name "$KITCHEN_PANE")" >/dev/null 2>&1; then
    return 0
  fi
  chat=$(recorded_pane "$host" octopilot.chat "$main")
  if [ -z "$chat" ] \
    || ! "$APP" do pane.rename --pane "$chat" --name "$(pane_name "$PROMPTER_PANE")" >/dev/null 2>&1; then
    echo "open-gui.sh: the $PROMPTER_PANE pane is not recorded in the $MAIN_TAB tab (octopilot.chat)," \
      "so the $KITCHEN_PANE pane is not added; see herdr/README.md" >&2
    return 0
  fi
  if ! created=$("$APP" do pane.split --direction down --pane "$chat" --mod octopilot-kitchen \
    --props "$KITCHEN_PROPS" 2>&1); then
    echo "open-gui.sh: could not split the $KITCHEN_PANE pane from the chat: $created" >&2
    return 0
  fi
  kitchen=$(jq -r '.pane // empty' <<<"$created" 2>/dev/null || true)
  if [ -z "$kitchen" ]; then
    echo "open-gui.sh: no pane came back for the $KITCHEN_PANE mod" >&2
    return 0
  fi
  name_mod_pane "$kitchen" "$(pane_name "$KITCHEN_PANE")"
  # Moved above the chat at creation only: a kept kitchen is never moved on a rerun.
  top=0
  if out=$("$APP" do pane.swap --pane "$kitchen" --target "$chat" 2>&1); then
    top=1
  else
    echo "open-gui.sh: could not move the $KITCHEN_PANE pane above the chat: $out" >&2
  fi
  size_kitchen "$kitchen" "$main" "$top"
  record_label "$host" octopilot.kitchen "$main:$kitchen"
}

# The code agent's claude in a tab of its own, named CODE_TAB: its recorded session resumed in
# its folder, else a fresh one started in this checkout. Prints its terminal.
start_code_tab() {
  local session created term recorded cwd argv
  recorded=$(recorded_session "$CODE_AGENT")
  if [ -n "$recorded" ]; then
    IFS=$'\t' read -r session cwd <<<"$recorded"
    argv=$(session_argv "$session" "" "$cwd" "$MODEL_CODE" resume)
  else
    session=$(new_session)
    argv=$(session_argv "$session" "" "$CODE_CWD" "$MODEL_CODE")
  fi
  created=$(app do tab.new --space "$1" --name "$CODE_TAB" --argv "$argv" --focus false)
  term=$(jq -r '.terminal // empty' <<<"$created")
  if [ -z "$term" ]; then
    echo "open-gui.sh: the $CODE_TAB tab has no terminal for the $CODE_AGENT agent" >&2
    exit 1
  fi
  await_shown "$term" >/dev/null || true
  name_agent "$term" "$CODE_AGENT" "$session"
  echo "$term"
}

# The terminals a tab holds, by id. herdr-app ls lists no mod panes.
tab_terminals() {
  "$APP" ls | jq -r --arg t "$1" '.spaces[].tabs[] | select(.id == $t) | .terminals[].terminal'
}

# Waits (up to 15 s) until a terminal shows in a tab, and prints its "space tab pane".
# herdr applies new terminals, keep and park in the background, and a remote herdr-app
# lists them a moment later, so the callers wait for them.
await_shown() {
  local i located
  for i in $(seq 30); do
    located=$(locate "$1")
    if [ -n "$located" ]; then echo "$located"; return 0; fi
    sleep 0.5
  done
  echo "open-gui.sh: $1 did not show in a tab" >&2
  return 1
}

# Shows a parked code agent in its space (labelled, else terminal.keep), never moving one
# that is shown. Prints its "space tab pane" only when it is in $space, alone in its tab.
show_code_tab() {
  local space=$1 code=$2 located
  if [ -z "$(locate "$code")" ]; then
    "$HC" label "$code" "herdr.space=$space" "octopilot.space=$space" >/dev/null
    if [ -z "$(locate "$code")" ]; then app do terminal.keep --terminal "$code" >/dev/null; fi
  fi
  located=$(await_shown "$code") || return 1
  if [ "$(cut -f1 <<<"$located")" != "$space" ] \
    || [ "$(tab_terminals "$(cut -f2 <<<"$located")")" != "$code" ]; then
    echo "open-gui.sh: the $CODE_AGENT terminal ($code) is not alone in a tab of" \
      "this space; left as it is" >&2
    return 1
  fi
  echo "$located"
}

# Closes the octopilot-side panel an earlier run put beside the code agent (its pane $3 in
# tab $2), and removes the agent's octopilot.side label. The panel is closed only when the
# label names this tab and pane.rename finds that pane: that proves the pane exists, not that
# it is ours, so the agent's own pane is never closed here.
drop_side_panel() {
  local code=$1 tab=$2 pane=$3 side
  side=$(recorded_pane "$code" octopilot.side "$tab")
  if [ -n "$side" ] && [ "$side" != "$pane" ] \
    && "$APP" do pane.rename --pane "$side" --name "$(pane_name octopilot-side)" >/dev/null 2>&1; then
    app do pane.close --pane "$side" >/dev/null
  fi
  if "$HC" get "$code" --json | jq -e '.labels["octopilot.side"]' >/dev/null 2>&1; then
    "$HC" label "$code" octopilot.side= >/dev/null
  fi
}

# The code-changes tab: the code agent's claude. A rerun converts the previous chat tab
# or a parked agent, never ending its claude; a tab holding another terminal is refused.
add_code_tab() {
  local space=$1 old code located t
  code=$(find_agent "$CODE_AGENT")
  old=$(tab_named "$space" "$CODE_TAB" "$(agent_tab "$CODE_AGENT" "$space")")
  if [ -n "$old" ]; then
    for t in $(tab_terminals "$old"); do
      if [ "$t" != "$code" ]; then
        echo "open-gui.sh: the $CODE_TAB tab holds $t, which is not the $CODE_AGENT agent;" \
          "close it first" >&2
        exit 1
      fi
    done
  fi
  if [ -z "$code" ]; then
    # What is left of the old tab holds no terminal: mods only, at most an earlier panel.
    if [ -n "$old" ]; then app do tab.close --id "$old" >/dev/null; old=""; fi
    code=$(start_code_tab "$space")
  fi
  located=$(show_code_tab "$space" "$code") || return 0
  t=$(cut -f2 <<<"$located")
  # The old tab holds no terminal now (checked above, and the agent is in $t): mods only.
  if [ -n "$old" ] && [ "$old" != "$t" ]; then app do tab.close --id "$old" >/dev/null; fi
  app do tab.rename --id "$t" --name "$CODE_TAB" >/dev/null
  drop_side_panel "$code" "$t" "$(cut -f3 <<<"$located")"
}

# The --terminals code tab: a terminal tab with its claude, resumed when the launcher recorded
# one, else started in this checkout, unless this checkout's code agent runs already (another
# checkout's is left alone).
add_code_terminal_tab() {
  local created recorded session="" cwd="" argv
  if [ -n "$(find_agent "$CODE_AGENT")" ]; then return 0; fi
  recorded=$(recorded_session "$CODE_AGENT")
  if [ -n "$recorded" ]; then
    IFS=$'\t' read -r session cwd <<<"$recorded"
    argv=$(session_argv "$session" "" "$cwd" "$MODEL_CODE" resume)
  else
    argv=$(session_argv "" "" "" "$MODEL_CODE")
  fi
  created=$(app do tab.new --space "$1" --name "$CODE_TAB" --folder "$CODE_CWD" \
    --argv "$argv" --focus false)
  name_agent "$(jq -r '.terminal' <<<"$created")" "$CODE_AGENT" "$session"
}

# The terminal of a dashboard.sh run as the TUI layout's curses dashboard, if
# the space has one: a second dashboard loop next to the headless one. herdr-app
# runs an argv inside a shell, so the foreground's argv and the pane's name
# (`dashboard`, as the TUI layout's builder gives it) count too.
legacy_dashboard() {
  local t
  for t in $("$APP" ls | jq -r --arg s "$1" \
    '.spaces[] | select(.id == $s) | .tabs[].terminals[] |
      select(.name == "dashboard") | .terminal'); do
    echo "$t"
    return 0
  done
  for t in $("$APP" ls | jq -r --arg s "$1" \
    '.spaces[] | select(.id == $s) | .tabs[].terminals[].terminal'); do
    if "$HC" get "$t" --json | jq -e '[(.argv // []), (.foreground.argv // [])] |
      any(join(" ") | test("dashboard[.]sh") and (test("--headless") | not))' >/dev/null; then
      echo "$t"
      return 0
    fi
  done
}

# Closes this terminal's own pane, so no idle launcher tab stays behind (a tab
# whose last pane closes goes with it). Never the tab itself: the terminal may
# share it with panes that are not ours. The last step of a path that worked.
close_launcher() {
  local located pane code
  if [ -z "${HC_TERMINAL_ID:-}" ]; then return 0; fi
  # Run from the code agent's own terminal: its pane is the agent's, never closed here.
  code=$(find_agent "$CODE_AGENT")
  if [ "$HC_TERMINAL_ID" = "$code" ]; then return 0; fi
  located=$(locate "$HC_TERMINAL_ID") || return 0
  IFS=$'\t' read -r _ _ pane <<<"$located"
  if [ -z "$pane" ]; then return 0; fi
  app do pane.close --pane "$pane" >/dev/null
}

# The space of an engine terminal: its herdr.space label, or octopilot.space once
# park_engine parked it (parking removes herdr.space).
space_of() {
  "$HC" get "$1" --json | jq -r '.labels["herdr.space"] // .labels["octopilot.space"] // empty'
}

# This checkout's engine terminals that have no pane, as "label address" lines.
parked_engine() {
  local label t
  for label in "$HEADLESS_AGENT" "$PROMPTER_AGENT"; do
    t=$(find_agent "$label")
    if [ -n "$t" ] && [ -z "$(locate "$t")" ]; then echo "$label $t"; fi
  done
}

# Shows the parked engine terminals of this checkout again, each labelled with its space
# first (as show_code_tab does). The first takes a tab named engine, the others go beside
# it after the host is focused. One that does not show in this space's engine tab
# is parked again, and a refused one stays parked (both warned about).
unpark_engine() {
  local space=$1 label t tab located out lspace ltab
  local keep
  for label in "$HEADLESS_AGENT" "$PROMPTER_AGENT"; do
    t=$(find_agent "$label")
    if [ -z "$t" ] || [ -n "$(locate "$t")" ]; then continue; fi
    "$HC" label "$t" "herdr.space=$space" "octopilot.space=$space" >/dev/null
    tab=$(tab_named "$space" "$ENGINE_TAB" "$(agent_tab "$HEADLESS_AGENT" "$space")")
    keep=(--terminal "$t")
    if [ -n "$tab" ]; then
      keep+=(--place right)
      "$APP" do agent.focus --terminal "$(find_host)" >/dev/null 2>&1 || true
    fi
    # Kept only when still not listed: a lagging listing may show it by now (as show_code_tab).
    if [ -z "$(locate "$t")" ]; then
      if ! out=$("$APP" do terminal.keep "${keep[@]}" 2>&1); then
        echo "open-gui.sh: could not show the $label terminal ($t): $out" >&2
        continue
      fi
    fi
    if ! located=$(await_shown "$t"); then continue; fi
    IFS=$'\t' read -r lspace ltab _ <<<"$located"
    if [ "$lspace" != "$space" ] || { [ -n "$tab" ] && [ "$ltab" != "$tab" ]; }; then
      echo "open-gui.sh: the $label terminal ($t) did not show in the $ENGINE_TAB tab of" \
        "this space; parked again" >&2
      "$APP" do terminal.park --terminal "$t" >/dev/null 2>&1 || true
      continue
    fi
    if [ -z "$tab" ]; then app do tab.rename --id "$ltab" --name "$ENGINE_TAB" >/dev/null; fi
  done
}

# Parks the engine's terminals (the default; --show-engine skips it): their panes go, the
# processes keep running, and the engine tab goes away. The host's space stays in a label (octopilot.space) for
# space_of. The code agent is not an engine terminal.
park_engine() {
  local space=$1 label t
  if [ "$HIDE_ENGINE" = 0 ]; then return 0; fi
  for label in "$HEADLESS_AGENT" "$PROMPTER_AGENT"; do
    t=$(find_agent "$label")
    if [ -z "$t" ] || [ -z "$(locate "$t")" ]; then continue; fi
    if [ "$label" = "$HEADLESS_AGENT" ]; then
      "$HC" label "$t" "octopilot.space=$space" >/dev/null
    fi
    app do terminal.park --terminal "$t" >/dev/null
  done
}

# Ends this checkout's engine terminals, by label: their space is gone, so octopilot is
# stopped and built again. Never the code agent: it is the person's own claude session.
stop_engine() {
  local label t
  for label in "$HEADLESS_AGENT" "$PROMPTER_AGENT"; do
    for t in $("$HC" ls --json --label "octopilot.agent=$label" | jq -r '.terminals[].address'); do
      if is_mine "$t"; then end_terminal "$t"; fi
    done
  done
}

# The space a new host starts in: the one of a terminal labelled with this checkout's dir,
# never a match by name. Empty: a new space. ORCH is the last fallback, for a migrated space.
existing_space() {
  local label t s
  for label in "$CODE_AGENT" "$HEADLESS_AGENT" "$PROMPTER_AGENT" "$ORCH_AGENT"; do
    t=$(find_agent "$label")
    if [ -z "$t" ]; then continue; fi
    s=$(space_of "$t")
    if [ -n "$s" ] && space_exists "$s"; then echo "$s"; return 0; fi
  done
}

# The terminal that keeps a retired agent's chat and kitchen labels until the new host
# takes them: this checkout's prompter, else its code agent. Empty if neither runs.
records_holder() {
  local t
  t=$(find_agent "$PROMPTER_AGENT")
  if [ -z "$t" ]; then t=$(find_agent "$CODE_AGENT"); fi
  echo "$t"
}

# This checkout's curses dashboard of the TUI layout in space $1, if any: unlabelled, run
# from this checkout's dashboard.sh, never the labelled host.
own_curses_dashboard() {
  local t
  if [ -z "$1" ]; then return 0; fi
  for t in $("$APP" ls | jq -r --arg s "$1" \
    '.spaces[] | select(.id == $s) | .tabs[].terminals[].terminal'); do
    if "$HC" get "$t" --json | jq -e --arg d "$DIR/dashboard.sh" '
      (.labels["octopilot.agent"] == null) and
      ([(.argv // []), (.foreground.argv // [])] |
        any(join(" ") | contains($d) and (contains("--headless") | not)))' >/dev/null; then
      echo "$t"
      return 0
    fi
  done
}

# Retires this checkout's agent of an earlier launcher (octopilot.agent=octopilot), with its
# headless and its old curses dashboard (old code). Its chat and kitchen labels move to
# records_holder; the prompter keeps running.
retire_orchestrator() {
  local space=$1 t v holder retired=0
  holder=$(records_holder)
  for t in $("$HC" ls --json --label "octopilot.agent=$ORCH_AGENT" | jq -r '.terminals[].address'); do
    if ! is_mine "$t"; then
      echo "open-gui.sh: the $ORCH_AGENT agent ($t) runs from another octopilot; left as it is" >&2
      continue
    fi
    echo "open-gui.sh: retiring the $ORCH_AGENT agent ($t)" >&2
    for v in octopilot.chat octopilot.kitchen; do
      if [ -z "$(label_of "$t" "$v")" ]; then continue; fi
      if [ -n "$holder" ]; then
        record_label "$holder" "$v" "$(label_of "$t" "$v")"
      else
        echo "open-gui.sh: no prompter or code agent keeps $v; add it by hand" >&2
      fi
    done
    end_terminal "$t"
    retired=1
  done
  if [ "$retired" = 0 ]; then return 0; fi
  t=$(find_agent "$HEADLESS_AGENT")
  if [ -n "$t" ]; then
    echo "open-gui.sh: ending this checkout's $HEADLESS_AGENT agent ($t): it runs the old code" >&2
    end_terminal "$t"
  fi
  t=$(own_curses_dashboard "$space")
  if [ -n "$t" ]; then
    echo "open-gui.sh: ending the old curses dashboard ($t) of the TUI layout" >&2
    end_terminal "$t"
  fi
  if [ -n "$(find_agent "$PROMPTER_AGENT")" ]; then
    echo "open-gui.sh: the prompter keeps its chat: /clear it, then run /octopilot:prompter" >&2
  fi
}

# Moves the chat and kitchen labels a retired agent left on records_holder to the new host
# (argument), and clears them there, so a kept OCTOPILOT tab can take its kitchen.
keep_saved_records() {
  local holder v value
  holder=$(records_holder)
  if [ -z "$holder" ]; then return 0; fi
  for v in octopilot.chat octopilot.kitchen; do
    value=$(label_of "$holder" "$v")
    if [ -z "$value" ]; then continue; fi
    record_label "$1" "$v" "$value"
    # Cleared from the holder only once the host shows the value.
    if [ "$(label_of "$1" "$v")" = "$value" ]; then "$HC" label "$holder" "$v=" >/dev/null; fi
  done
}

# The pane of this checkout's prompter when it is in tab $2 of space $1, so a host can be
# split beside it; nothing otherwise.
prompter_pane_in() {
  local t located
  t=$(find_agent "$PROMPTER_AGENT")
  if [ -z "$t" ] || [ -z "$2" ]; then return 0; fi
  located=$(locate "$t")
  if [ -n "$located" ] && [ "$(cut -f1 <<<"$located")" = "$1" ] \
    && [ "$(cut -f2 <<<"$located")" = "$2" ]; then
    cut -f3 <<<"$located"
  fi
}

# Runs at the end of every path that did its work: the engine parked (the GUI
# layout, unless --show-engine), then the launcher's own pane.
finish() {
  local space=$1
  trap - EXIT
  flush_log
  if [ "$CLOSE_LAUNCHER" = 1 ]; then close_launcher; fi
}

# Runs after the space, its tabs and the park: restores the workers (a refusal is only warned
# about), releases the engine's locks, then requests a tick. Success paths only: a failure
# exits first, and the exit releases the locks.
restore_and_tick() {
  # The workers' children do not keep the locks, as in herdr-app and hc above.
  if ! env "${envs[@]}" "$DIR/scan.sh" restore-sessions 8>&- 9>&-; then
    echo "open-gui.sh: restore-sessions refused; no worker was reopened (see above)" >&2
  fi
  exec 9>&- 8>&-
  if ! env "${envs[@]}" "$DIR/scan.sh" request tick; then
    echo "open-gui.sh: could not request a tick; the engine ticks on its own schedule" >&2
  fi
}

# Names the space LABEL, best effort: space.new's name is not always kept, and an unnamed
# space is called after its first terminal's folder. A refusal is warned about, never fatal.
name_space() {
  local name out
  name=$("$APP" ls | jq -r --arg s "$1" '.spaces[] | select(.id == $s) | .name') || true
  if [ "$name" = "$LABEL" ]; then return 0; fi
  if ! out=$("$APP" do space.rename --id "$1" --name "$LABEL" 2>&1); then
    echo "open-gui.sh: could not name space $1 $LABEL: $out" >&2
  fi
}

# Puts the space's tabs in order: OCTOPILOT first, the code tab second, the engine tab third,
# each only when it is present (the default parks the engine's, so its tab is not there).
# Worker tabs opened later land after them.
place_tabs() {
  local space=$1 tab engine
  tab=$(tab_named "$space" "$MAIN_TAB" "$(recorded_main_tab)")
  if [ -n "$tab" ]; then app do tab.move --id "$tab" --index 1 >/dev/null; fi
  tab=$(tab_named "$space" "$CODE_TAB" "$(agent_tab "$CODE_AGENT" "$space")")
  if [ -n "$tab" ]; then app do tab.move --id "$tab" --index 2 >/dev/null; fi
  engine=$(agent_tab "$HEADLESS_AGENT" "$space")
  if [ -z "$engine" ]; then engine=$(agent_tab "$PROMPTER_AGENT" "$space"); fi
  tab=$(tab_named "$space" "$ENGINE_TAB" "$engine")
  if [ -n "$tab" ]; then app do tab.move --id "$tab" --index 3 >/dev/null; fi
}

# Shows the space and its OCTOPILOT tab.
focus_main() {
  app do space.select --id "$1" >/dev/null
  app do tab.select --id "$(tab_named "$1" "$MAIN_TAB" "$(recorded_main_tab)")" >/dev/null
}

ensure_window
# The space is found before the retirement and the reap, which may end the terminals that prove it.
known_space=$(existing_space)
# Dead shells go first: the pre-checks and the records holder must not see them.
for a in "$HEADLESS_AGENT" "$PROMPTER_AGENT" "$CODE_AGENT"; do reap_dead "$a"; done
# Layouts this run cannot build over are refused before anything is ended.
if [ -n "$known_space" ] && [ "$TERMINALS" = 0 ]; then
  old=$(legacy_dashboard "$known_space")
  if [ -n "$old" ]; then
    echo "open-gui.sh: terminal $old is the curses dashboard of the TUI layout;" \
      "close its pane first (two dashboards would run), or use --terminals" >&2
    exit 1
  fi
fi
if [ -n "$known_space" ] && [ "$TERMINALS" = 1 ] && [ -z "$(find_host)" ]; then
  main=$(tab_named "$known_space" "$MAIN_TAB")
  if [ -n "$main" ] && [ -z "$(tab_terminals "$main")" ]; then
    echo "open-gui.sh: the space already has an $MAIN_TAB tab (the GUI layout);" \
      "run without --terminals" >&2
    exit 1
  fi
  if [ -n "$main" ] && [ -z "$(prompter_pane_in "$known_space" "$main")" ]; then
    echo "open-gui.sh: the $MAIN_TAB tab holds no $PROMPTER_AGENT to put the dashboard beside;" \
      "close the tab first" >&2
    exit 1
  fi
fi
retire_orchestrator "$known_space"
host=$(find_host)
if [ -n "$host" ]; then
  located=$(locate "$host")
  if [ -n "$located" ]; then
    IFS=$'\t' read -r space tab pane <<<"$located"
  else
    # Parked by an earlier run: no pane. Its space must still be shown, else
    # octopilot was closed with it and is stopped here.
    space=$(space_of "$host")
    if [ -z "$space" ]; then
      echo "open-gui.sh: the $HEADLESS_AGENT agent ($host) is not in a Herdr space" >&2
      exit 1
    fi
    if space_exists "$space"; then
      tab=""
      pane=""
    else
      echo "open-gui.sh: the space of the $HEADLESS_AGENT agent is closed; stopping" \
        "octopilot and building it again" >&2
      stop_engine
      host=""
      known_space=""
    fi
  fi
fi

if [ -n "$host" ]; then
  # A run that failed after the host was labelled left the records on the holder.
  keep_saved_records "$host"
  name_space "$space"
  if [ "$TERMINALS" = 1 ]; then
    if [ -n "$(tab_named "$space" "$MAIN_TAB")" ] && [ "$(tab_named "$space" "$MAIN_TAB")" != "$tab" ]; then
      echo "open-gui.sh: the space already has an $MAIN_TAB tab (the GUI layout);" \
        "run without --terminals" >&2
      exit 1
    fi
    parked=$(parked_engine)
    if [ -n "$parked" ]; then
      echo "open-gui.sh: these engine terminals are parked (no pane); show each with" \
        "herdr-app do terminal.keep --terminal <address>:" >&2
      sed 's/^/  /' <<<"$parked" >&2
      exit 1
    fi
    app do tab.rename --id "$tab" --name "$MAIN_TAB" >/dev/null
    if [ -z "$(find_agent "$PROMPTER_AGENT")" ]; then add_prompter_left "$space" "$pane"; fi
    add_code_terminal_tab "$space"
    place_tabs "$space"
    # Best effort: a terminal created a moment ago may not be focusable yet.
    "$APP" do agent.focus --terminal "$host" >/dev/null 2>&1 || true
  else
    old=$(legacy_dashboard "$space")
    if [ -n "$old" ]; then
      echo "open-gui.sh: terminal $old is the curses dashboard of the TUI layout;" \
        "close its pane first (two dashboards would run), or use --terminals" >&2
      exit 1
    fi
    if [ -n "$pane" ]; then app do tab.rename --id "$tab" --name "$ENGINE_TAB" >/dev/null; fi
    if [ "$HIDE_ENGINE" = 0 ]; then
      unpark_engine "$space"
      pane=$(await_shown "$host" | cut -f3) || true
    fi
    add_engine_agents "$space" "$pane"
    add_main_tab "$space"
    add_kitchen "$space"
    add_code_tab "$space"
    place_tabs "$space"
    focus_main "$space"
  fi
  if [ "$TERMINALS" = 0 ]; then park_engine "$space"; fi
  restore_and_tick
  finish "$space"
  exit 0
fi

# No host running: it starts in this checkout's space, or in a new one, and the prompter
# beside it. --terminals starts the curses dashboard as the host. A kept prompter in the
# host's tab (a retired agent's) gets the host beside it, to its right.
space="$known_space"
tab_name="$ENGINE_TAB"
host_argv=$(headless_argv)
host_name=headless
if [ "$TERMINALS" = 1 ]; then
  tab_name="$MAIN_TAB"
  host_argv=$(argv_json env "${envs[@]}" bash "$DIR/dashboard.sh")
  host_name=dashboard
fi
beside=""
if [ -n "$space" ]; then beside=$(prompter_pane_in "$space" "$(tab_named "$space" "$tab_name")"); fi
if [ -n "$beside" ]; then
  add_host "$space" "$beside" right "$host_argv" "$host_name"
  host=$(find_host)
elif [ -n "$space" ]; then
  created=$(app do tab.new --space "$space" --name "$tab_name" --argv "$host_argv" --focus false)
  host=$(jq -r '.terminal' <<<"$created")
  name_agent "$host" "$HEADLESS_AGENT"
else
  created=$(app do space.new --name "$LABEL" --folder "$OCTOPILOT_ORCH_CWD" \
    --tab "$tab_name" --argv "$host_argv" --focus false)
  space=$(jq -r '.space' <<<"$created")
  host=$(jq -r '.terminal' <<<"$created")
  name_agent "$host" "$HEADLESS_AGENT"
fi
if ! located=$(await_shown "$host"); then
  echo "open-gui.sh: the new $HEADLESS_AGENT terminal ($host) is not in a Herdr space" >&2
  exit 1
fi
name_space "$space"
root=$(cut -f3 <<<"$located")
keep_saved_records "$host"

if [ "$TERMINALS" = 1 ]; then
  app do pane.rename --pane "$root" --name dashboard >/dev/null
  if [ -z "$(find_agent "$PROMPTER_AGENT")" ]; then add_prompter_left "$space" "$root"; fi
  add_code_terminal_tab "$space"
  place_tabs "$space"
  # Best effort: a terminal created a moment ago may not be focusable yet.
  "$APP" do agent.focus --terminal "$host" >/dev/null 2>&1 || true
else
  "$APP" do pane.rename --pane "$root" --name headless >/dev/null 2>&1 || true
  if [ "$HIDE_ENGINE" = 0 ]; then unpark_engine "$space"; fi
  add_engine_agents "$space" "$root"
  add_main_tab "$space"
  add_kitchen "$space"
  add_code_tab "$space"
  place_tabs "$space"
  focus_main "$space"
fi
if [ "$TERMINALS" = 0 ]; then park_engine "$space"; fi
restore_and_tick
finish "$space"
