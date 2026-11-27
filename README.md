# Octopilot

An engine for coding agents. It reads the GitHub issues and PRs of your repos,
proposes the next work, and spawns a Claude in its own git worktree for each one,
as a tab of the Octopilot space (GUI) or workspace (TUI): it implements an issue or
fixes a PR, runs the review-fix loop, and cleans up when the PR merges. You watch a
live dashboard (a herdr TUI, or herdr-app mods) and talk to the engine through a
prompter.

## Requirements

- [herdr](https://herdr.dev), the terminal workspace manager that hosts the agents
- herdr-app (optional): the GUI front end
- `python3` and [`uv`](https://docs.astral.sh/uv/), which runs the dashboard with
  its Python deps (`rich`)
- `git`, `gh`, `jq`, `uuidgen` and `sha1sum`, which the scripts and the GUI launcher call

## Install

```sh
git clone <this repo> ~/src/octopilot
cd ~/src/octopilot
./install.sh
```

`install.sh` links the herdr plugin `elboletaire.octopilot` (when a herdr server
runs), installs the herdr-app mods (when `herdr-app` is on PATH) and moves
PR Autopilot's state from `${XDG_STATE_HOME:-~/.local/state}/pr-autopilot` to
`.../octopilot` once. It is safe to rerun.
`./install.sh --help` lists that.

`OCTOPILOT_DIR` is where octopilot is installed on this machine, which the launcher
reads (default `~/src/octopilot`). Set it only when the checkout lives elsewhere.
Each Octopilot process sets it to the checkout it runs from, so a worktree's sessions
use their own code. The live checkout is the one whose plugin settings.json loads
(`scan.sh installed-dir`).

### Claude plugin

The prompter's skill and the takoyaki band in its claude are a Claude Code plugin,
loaded through `CLAUDE_CODE_PLUGIN_DIRS`. Point it at the plugin
folder of your checkout, for example in `~/.claude/settings.json`:

```json
{
  "env": {
    "CLAUDE_CODE_PLUGIN_DIRS": "~/src/octopilot/plugin"
  }
}
```

Use the path of your clone. Several plugin dirs are separated by `:`.

## Layout

- `engine.py`: the engine (planner and executor), run by `scan.py tick` and `drain`
- `scan.py`, `scan.sh`: the state, and its wrapper
- `dashboard.py`, `dashboard.sh`: the TUI (and `--headless`, which writes `view.json`)
- `config.sh`: repos, clone roots, models and limits; each is overridable by an env var
- `prompts/`: the worker and loop prompts
- `plugin/`: the Claude plugin (skills and the takoyaki hooks)
- `herdr/`: the herdr TUI plugin, and `open-gui.sh` for the herdr-app space
- `herdr-app/`: the herdr-app mods and their installer

[`AGENTS.md`](AGENTS.md) is the guide for changing the code, and
[`herdr/README.md`](herdr/README.md) and [`herdr-app/CONTRACT.md`](herdr-app/CONTRACT.md)
describe the herdr layout and the GUI contract.

## License

Apache-2.0, see [LICENSE](LICENSE).
