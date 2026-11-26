#!/usr/bin/env bash
# Build the Octopilot workspace, or focus it if it already exists and add
# whatever it is missing. Run by the plugin's `open` action.
#
# Tab "OCTOPILOT": the user's prompter pane on the left, the dashboard on the
# right. Tab "Octopilot (code changes)": a claude started in this checkout, for
# changing octopilot itself.
#
# The prompter is started once, on a fresh pane; the user types in it after that.
set -euo pipefail
H="${HERDR_BIN_PATH:-herdr}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="Octopilot"
PROMPTER_AGENT="octopilot-prompter"
RETIRED_AGENT="octopilot"              # a retired agent: left alone while it runs, closed by hand
DASHBOARD_TITLE="dashboard"
MAIN_TAB="OCTOPILOT"
CODE_TAB="Octopilot (code changes)"
CODE_AGENT="octopilot-code"            # scan.py's CODE_AGENT
CODE_CWD="$DIR"                        # the octopilot dir: its AGENTS.md is the code agent's guide

# Start the prompter in a fresh pane, named so the launcher can find it again.
start_prompter() {
  "$H" pane rename "$1" "prompter" >/dev/null
  "$H" agent start "$PROMPTER_AGENT" --kind claude --pane "$1" \
    --timeout 60000 >/dev/null || return 0
  # A brand-new pane: nobody is typing in it yet.
  "$H" agent prompt "$PROMPTER_AGENT" "/octopilot:prompter" >/dev/null || true
}

# Split the given pane down and start the prompter in the new pane.
add_prompter() {
  local split pane
  split=$("$H" pane split "$1" --direction down --focus)
  pane=$(jq -r '.result.pane.pane_id // .result.pane_id' <<<"$split")
  start_prompter "$pane"
}

# The code-changes tab, unless the workspace has one; a claude starts in it.
add_code_tab() {
  local tabs created pane
  tabs=$("$H" tab list --workspace "$1")
  jq -e --arg l "$CODE_TAB" '.result.tabs[] | select(.label == $l)' \
    <<<"$tabs" >/dev/null && return 0
  created=$("$H" tab create --workspace "$1" --label "$CODE_TAB" \
    --cwd "$CODE_CWD" --no-focus)
  pane=$(jq -r '.result.root_pane.pane_id' <<<"$created")
  "$H" agent start "$CODE_AGENT" --kind claude --pane "$pane" \
    --timeout 60000 >/dev/null || true
}

# The tab holding the given pane is the main one.
name_main_tab() {
  local tab
  tab=$("$H" pane list | jq -r --arg p "$1" \
    '.result.panes[] | select(.pane_id == $p) | .tab_id')
  [ -n "$tab" ] && "$H" tab rename "$tab" "$MAIN_TAB" >/dev/null
}

# The pane of an agent when it runs in workspace $ws, empty otherwise.
agent_pane() {
  "$H" agent get "$1" 2>/dev/null |
    jq -r --arg w "$ws" '.result.agent | select(.workspace_id == $w) | .pane_id' ||
    true
}

# The dashboard's pane in workspace $ws, or the workspace's first pane.
dashboard_pane() {
  "$H" pane list --workspace "$ws" | jq -r --arg t "$DASHBOARD_TITLE" \
    '(first(.result.panes[] | select(.title == $t)) // .result.panes[0]).pane_id'
}

ws=$("$H" workspace list | jq -r --arg l "$LABEL" \
  '.result.workspaces[] | select(.label == $l) | .workspace_id' | head -n1)
if [ -n "$ws" ]; then
  "$H" workspace focus "$ws" >/dev/null
  prompter_pane=$(agent_pane "$PROMPTER_AGENT")
  if [ -n "$prompter_pane" ]; then
    name_main_tab "$prompter_pane"
  elif ! "$H" agent get "$PROMPTER_AGENT" >/dev/null 2>&1; then
    # No prompter anywhere: split it from the retired agent's pane if one runs
    # here, else from the dashboard.
    anchor=$(agent_pane "$RETIRED_AGENT")
    [ -n "$anchor" ] || anchor=$(dashboard_pane)
    add_prompter "$anchor"
  fi
  if [ -n "$(agent_pane "$RETIRED_AGENT")" ]; then
    echo "Octopilot: a retired 'octopilot' agent still runs in this workspace; close its pane."
  fi
  add_code_tab "$ws"
  exit 0
fi

# A prompter already running elsewhere: go to it rather than starting a second one.
if "$H" agent get "$PROMPTER_AGENT" >/dev/null 2>&1; then
  exec "$H" agent focus "$PROMPTER_AGENT"
fi

set -a
# shellcheck source=../config.sh
source "$DIR/config.sh"
set +a

created=$("$H" workspace create --label "$LABEL" \
  --cwd "${OCTOPILOT_ORCH_CWD:-$HOME}" --focus)
root=$(jq -r '.result.root_pane.pane_id' <<<"$created")
name_main_tab "$root"
start_prompter "$root"

# The dashboard beside the prompter, which keeps the left column.
"$H" plugin pane open --plugin elboletaire.octopilot --entrypoint dashboard \
  --placement split --target-pane "$root" \
  --direction right --no-focus >/dev/null
add_code_tab "$(jq -r '.result.workspace.workspace_id // .result.workspace_id' <<<"$created")"
