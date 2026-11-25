#!/usr/bin/env bash
# Sets Octopilot up from this checkout: links the herdr plugin, installs the herdr-app
# mods when herdr-app is there, and moves PR Autopilot's state over once. Safe to rerun.
#
#   install.sh [--help]
#
# The Claude plugin is not linked here: settings.json's CLAUDE_CODE_PLUGIN_DIRS loads
# plugin/ (see README.md).
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

usage() {
  cat <<'EOF'
usage: install.sh [--help]

Links the herdr plugin elboletaire.octopilot (herdr/) when a herdr server runs,
installs the herdr-app mods (herdr-app/install.sh) when herdr-app is on PATH, and
moves ${XDG_STATE_HOME:-~/.local/state}/pr-autopilot to .../octopilot when that is
still pending. Nothing else is written.
EOF
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  "") ;;
  *) echo "install.sh: unknown argument $1" >&2; usage >&2; exit 2 ;;
esac

link_herdr_plugin() {
  # The herdr TUI plugin: the Octopilot workspace and its dashboard. Only linked when
  # herdr is installed.
  if ! command -v herdr &>/dev/null; then
    echo "herdr not found; skipping the herdr plugin link"
    return 0
  fi
  # Plugin commands need a live server and exit 0 even when there is none. Captured
  # first: grep -q closing a pipe would fail herdr under pipefail.
  local status
  status=$(herdr status server 2>/dev/null || true)
  if [[ "$status" == *"not running"* ]]; then
    echo "herdr server not running; skipping the herdr plugin link"
    return 0
  fi
  # Octopilot's id before the rename from PR Autopilot.
  if [[ -n "$(_herdr_plugin_root elboletaire.autopilot)" ]]; then
    herdr plugin unlink elboletaire.autopilot >/dev/null || true
  fi
  _link_herdr_plugin elboletaire.octopilot "$DIR/herdr"
}

link_herdr_app() {
  # Octopilot's herdr-app mods and their programs.json entry. Only when herdr-app is
  # installed; a failure there does not stop the rest.
  if ! command -v herdr-app &>/dev/null; then
    echo "herdr-app not found; skipping its mods"
    return 0
  fi
  bash "$DIR/herdr-app/install.sh" ||
    echo "WARNING: Octopilot's herdr-app setup failed (see above)" >&2
}

_herdr_plugin_root() {
  # Where the herdr plugin with this id is linked from; empty when it is not.
  herdr plugin list --json 2>/dev/null | python3 -c '
import json, sys
try:
    plugins = json.load(sys.stdin)["result"]["plugins"]
except Exception:
    plugins = []
print(next((p["plugin_root"] for p in plugins if p["plugin_id"] == sys.argv[1]), ""))
' "$1" || true
}

_link_herdr_plugin() {
  # Relinks when it points anywhere else (e.g. a worktree it was tested from).
  local id=$1 root=$2 current
  current=$(_herdr_plugin_root "$id")
  if [[ "$current" == "$root" ]]; then
    return 0
  fi
  if [[ -n "$current" ]]; then
    herdr plugin unlink "$id" >/dev/null || return 0
  fi
  if herdr plugin link "$root" >/dev/null; then
    echo "Linked herdr plugin $id -> $root"
  else
    echo "WARNING: could not link the herdr plugin (is a herdr server running?)" >&2
  fi
}

migrate_octopilot_state() {
  # PR Autopilot was renamed Octopilot: carry its state over once, and point the
  # recorded orchestrator at the renamed herdr agent.
  local root=${XDG_STATE_HOME:-$HOME/.local/state}
  local old=$root/pr-autopilot new=$root/octopilot
  [[ -d "$old" && ! -e "$new" ]] || return 0
  mv "$old" "$new" || return 0
  if [[ -f "$new/state.json" ]]; then
    sed 's/"herdr:autopilot"/"herdr:octopilot"/' "$new/state.json" > "$new/state.json.tmp" &&
      mv "$new/state.json.tmp" "$new/state.json" ||
      echo "WARNING: could not update the orchestrator in $new/state.json" >&2
  fi
  echo "Moved Octopilot state: $old -> $new"
}

migrate_octopilot_state
link_herdr_plugin
link_herdr_app
echo "Octopilot set up from $DIR"
