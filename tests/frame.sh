#!/usr/bin/env bash
# One dashboard frame from the fixture state, on stdout. Diff it before and
# after a change that must not alter the layout: it is byte-identical unless
# the frame's clock ticks over between two runs, so capture back to back.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
cp "$ROOT/tests/fixtures/frame/state.json" "$ROOT/tests/fixtures/frame/snapshot.json" "$scratch/"
OCTOPILOT_STATE_DIR="$scratch" HOME_REPOS=none/none ISSUE_ORGS=none-org \
  CLONE_ROOTS=/tmp/octo-empty COLUMNS=160 LINES=60 "$ROOT/dashboard.sh" --once
