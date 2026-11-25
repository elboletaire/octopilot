# Octopilot

An orchestrator for coding agents. It reads the GitHub issues and PRs of your
repos, proposes the next work, and spawns a Claude in its own git worktree and
herdr workspace for each one: it implements an issue or fixes a PR, runs the
review-fix loop, and cleans up when the PR merges. You watch a live dashboard
(a herdr TUI, or herdr-app mods) and talk to the orchestrator through a prompter.

## Requirements

- [herdr](https://herdr.dev), the terminal workspace manager that hosts the agents
- [herdr-app](https://herdr.dev) (optional): the GUI front end
- `python3` and [`uv`](https://docs.astral.sh/uv/), which runs the dashboard with
  its Python deps (`rich`)

## Install

```sh
git clone <this repo> ~/src/octopilot
cd ~/src/octopilot
./install.sh
```

`install.sh` links the herdr plugin `elboletaire.octopilot` (when a herdr server
runs), installs the herdr-app mods (when `herdr-app` is on PATH) and moves
PR Autopilot's state to `~/.local/state/octopilot` once. It is safe to rerun.
`./install.sh --help` lists that.

The checkout can live elsewhere: the scripts use `OCTOPILOT_DIR` when it is set,
else `~/src/octopilot`.

### Claude plugin

The orchestrator's skills and the takoyaki kitchen above its prompt are a Claude
Code plugin, loaded through `CLAUDE_CODE_PLUGIN_DIRS`. Point it at the plugin
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

- `scan.py`, `scan.sh`: the state engine, and its wrapper
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
