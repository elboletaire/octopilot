# Octopilot (code changes)

You are changing Octopilot, the code in this repo. Its root is the checkout installed
on this machine (the dotfiles clone it to `~/src/octopilot`).

## What it is

Octopilot drives Claude worktree sessions from GitHub issues and PRs across the
repos in `HOME_REPOS` and `CLONE_ROOTS`. The engine (`engine.py`, run by the
dashboard as `scan.py tick` and `scan.py drain`) reads a table built from GitHub
(PR state, CI) and from the herdr agents, and acts on its rows. It also acts on the
user's requests: `go N` under "Pick next" spawns a worker, and `no`, re-review,
investigate and the rest are queued the same way. A worker is a claude in its own
git worktree and herdr workspace, named `ap-<repo>-<number>`. It implements an issue
or fixes a PR, then the review-fix loop runs. When a PR merges, the engine cleans up.

The user watches the dashboard (TUI or GUI) and talks to a prompter chat. Both
queue their commands for the engine, and agent reports are recorded on the item and
shown on the cards, the kitchen and in notifications. The Octopilot workspace has three
tabs: OCTOPILOT (kitchen, chat, dashboard), "Octopilot (code changes)", a claude started
in this checkout for changing this code, and "engine" (the prompter and the headless
dashboard, which hosts the engine), shown third unless the launcher runs with
`--hide-engine` (see `herdr/README.md`).

The GUI is native mods over the same Python backend. The decisions stay in
`scan.py` and `dashboard.py`; the mods only render `view.json` and send actions
(`dashboard.sh --action`). `herdr-app/CONTRACT.md` is that contract: the
`view.json` shape, the actions and the remote-space rules.

## Install and parallel work

`./install.sh` (see `README.md`) links the herdr plugin `elboletaire.octopilot`
(`herdr/`), runs `herdr-app/install.sh` when herdr-app is installed, and moves PR
Autopilot's state over once. Nothing else is written: the Claude plugin is loaded
through `CLAUDE_CODE_PLUGIN_DIRS` (settings.json, set by the dotfiles), and the herdr
keys are bound by the dotfiles' herdr config, not here. The code, the mods and the
prompts never hard-code the live checkout: it is the plugin dir that settings.json
loads (`scan.sh installed-dir`). `OCTOPILOT_DIR` names the checkout a process runs
from (Octopilot sets it per process), and the launcher falls back to `~/src/octopilot`
when it is unset.

Parallel work uses a git worktree in this repo, under `.worktrees/` (git-ignored),
branched from a fresh `origin/main`:

```sh
git fetch origin
git worktree add --no-track -b feat/x .worktrees/feat-x origin/main
```

## Main pieces

- `scan.py`: the state. Scans GitHub and agent state, keeps `state.json`
  and `events.jsonl`, and holds the subcommands (`tick`, `drain`, `request`,
  `speech`, `chat`, `chat-send`, `plugin-settings`, ...). `scan` and `refresh` are
  aliases of `tick`.
- `engine.py`: the engine, run by `scan.py tick` (the rows: MERGED, RESTACK, REVIEW,
  BOOTING, FEEDBACK) and `scan.py drain` (the queued requests and sends). Behind
  `OCTOPILOT_ENGINE` (`on` by default; `plan` logs the row actions without acting; `off`
  acts on nothing).
- `naming.py`: the branch and session-title names for a spawn, by heuristic.
- `tests/`: the unit tests. `fake.py` fakes GitHub, herdr and git for them; `frame.sh`
  prints the TUI frame for the visual checks.
- `scan.sh`: wrapper. Sources `config.sh`, exports it, then runs `scan.py`.
  `scan.sh doorbell` only prints a stop notice for a leftover orchestrator's Monitor.
- `dashboard.py`, `dashboard.sh`: the TUI (default), `--once` (one frame),
  `--headless` (the loops, writing `view.json`) and `--action <id>`. Run by `uv`.
- `config.sh`: repos, clone roots, models, limits. Each value can be overridden
  by an environment variable.
- `prompts/`: the worker and loop prompts (`work.md`, `review-fix.md`, `rebase.md`,
  `investigate.md`, ...), with shared fragments in `prompts/shared/`.
- `plugin/`: the Claude plugin, loaded through `CLAUDE_CODE_PLUGIN_DIRS`. The skill
  `prompter` (the user's chat), and `hooks/` (the takoyaki kitchen feed, scene and
  band, in TS/TSX).
- `herdr/`: the herdr TUI plugin `elboletaire.octopilot` (`open.sh`, `open-gui.sh`,
  `herdr-plugin.toml`, `README.md`).
- `herdr-app/`: the GUI mods (`mods/octopilot-{dashboard,kitchen,chat,launcher}`),
  `install.sh` (links the mods and the `programs.json` entry) and `CONTRACT.md`.

## Testing safely

- **`OCTOPILOT_ENGINE=on` acts on what the scratch state holds.** A
  scratch state that carries real sessions makes `on` clean up real worktrees
  and send real prompts. Use fakes (`tests/fake.py`) or an empty state.
- **Never test against the live state.** `~/.local/state/octopilot` is the
  running octopilot. Set `OCTOPILOT_STATE_DIR` to a scratch directory, and narrow
  the scan with `HOME_REPOS`, `ISSUE_ORGS` and `CLONE_ROOTS`. Do not start the
  engine or spawn workers on the live checkout to try a change. Dashboard
  loops (`--headless`, the TUI) started from the live checkout save to the live
  state through `tick`, so never run them there without `OCTOPILOT_STATE_DIR`. A
  scratch-state tick still makes read-only `gh search prs` queries against the
  whole real account: `HOME_REPOS` and `CLONE_ROOTS` don't narrow those (only
  `ISSUE_ORGS` narrows the issue search).
- **Unit tests**: `python3 -m unittest discover -s tests -v` from the repo root.
- **GUI test space**: `open-gui.sh` refuses without `OCTOPILOT_STATE_DIR`. Run it
  from your worktree, then close the test space with
  `herdr-app do space.close --id <space> --confirmed true`. The
  `herdr/README.md` has the full recipe.
- **Mods**: `node --test test/*.mjs` in the mod's directory. `herdr-app check <mod>`
  loads the mod and exits 1 on load or render errors (it does not check layout or
  paint). `herdr-app types <mod>` writes `gpui-kit.d.ts` into the mod folder (git-ignored);
  run it on a copy outside the repo (for example under `/tmp`) to keep that file out
  of the worktree.
- **TUI changes**: `tests/frame.sh` prints the `dashboard.sh --once` frame for a
  fixed scratch state; capture it before and after the change, and diff them. A
  change that is not meant to alter the frame must give byte-identical output.
- **Plugin**: `plugin/tests/kitchen.test.tsx` covers the kitchen's feed and scene.

Keep the changes within the request. Update `herdr/README.md` or
`herdr-app/CONTRACT.md` when behaviour they describe changes.
