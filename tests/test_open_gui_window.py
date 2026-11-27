"""open-gui.sh before its first step: the engine's locks, and ensure_window's herdr-app
window check. Runs the real launcher with herdr-app, systemd-run, systemctl and hc
shimmed on PATH; each shim appends its argv to a trace. No window is ever opened."""
import json
import os
import pty
import select
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
import uuid

from tests import SCRATCH

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(REPO, "herdr", "open-gui.sh")

# A shim logs its argv, and a line in $SHIM/fds when fd 8 or 9 (the engine's locks) is open.
# herdr-app status exits 2 with no window until systemd-run creates $SHIM/window ($SHIM/hang: 124,
# $SHIM/on_status runs first). With $SHIM/space, ls reads $SHIM/app-ls.json, pane.split and tab.new
# answer tp and tc, and other commands succeed with {}.
SHIMS = {
    "herdr-app": '''#!/bin/sh
echo "herdr-app $*" >> "$SHIM/trace"
[ -e /proc/self/fd/8 ] && echo "herdr-app 8" >> "$SHIM/fds"
[ -e /proc/self/fd/9 ] && echo "herdr-app 9" >> "$SHIM/fds"
if [ "$1" = status ]; then
  [ -f "$SHIM/on_status" ] && sh "$SHIM/on_status"
  [ -f "$SHIM/hang" ] && exit 124
  [ -f "$SHIM/window" ] && exit 0
  echo "herdr-app: no Herdr window at $SHIM/herdr-app.sock: No such file or directory" >&2
  exit 2
fi
if [ -f "$SHIM/space" ]; then
  if [ "$1" = ls ]; then cat "$SHIM/app-ls.json"; exit 0; fi
  case "$2" in
    pane.split) echo '{"terminal":"tp","pane":"p2"}' ;;
    tab.new) echo '{"terminal":"tc","pane":"p4"}' ;;
    space.new) echo '{"space":"s1","terminal":"th"}' ;;
    *) echo '{}' ;;
  esac
  exit 0
fi
exit 1
''',
    "systemd-run": '''#!/bin/sh
echo "systemd-run $*" >> "$SHIM/trace"
[ -e /proc/self/fd/8 ] && echo "systemd-run 8" >> "$SHIM/fds"
touch "$SHIM/window"
''',
    "systemctl": '''#!/bin/sh
echo "systemctl $*" >> "$SHIM/trace"
[ -f "$SHIM/env" ] && cat "$SHIM/env"
''',
    "hc": '''#!/bin/sh
echo "hc $*" >> "$SHIM/trace"
[ -e /proc/self/fd/8 ] && echo "hc 8" >> "$SHIM/fds"
case "$1 $2" in
  "ls --json")
    # ls --label <label> reads ls-<label>.json, a plain ls reads ls.json; none means no terminals.
    if [ "$3" = --label ]; then f="$SHIM/ls-$4.json"; else f="$SHIM/ls.json"; fi
    if [ -f "$f" ]; then cat "$f"; else echo '{"terminals": []}'; fi ;;
  "get "*) cat "$SHIM/get-$2.json" 2>/dev/null || echo '{}' ;;
esac
''',
}

# The checkout's scan.sh: restore-sessions and request are logged with whether the engine's
# locks are free (flock -n) at that moment; restore-sessions exits with $SHIM/restore_rc when
# there is one. Every other subcommand runs the real scan.sh (REAL below).
SCAN_SHIM = '''#!/bin/sh
locks() {
  if flock -n "$OCTOPILOT_STATE_DIR/tick.lock" true && flock -n "$OCTOPILOT_STATE_DIR/exec.lock" true
  then echo free; else echo held; fi
}
case "$1" in
  restore-sessions)
    echo "scan restore-sessions locks=$(locks)" >> "$SHIM/trace"
    [ -e /proc/self/fd/8 ] && echo "scan restore-sessions 8" >> "$SHIM/fds"
    echo "$OCTOPILOT_LAUNCHER_LOCKED" > "$SHIM/restore_env"
    [ -f "$SHIM/restore_rc" ] && exit "$(cat "$SHIM/restore_rc")"
    exit 0 ;;
  request)
    echo "scan $* locks=$(locks)" >> "$SHIM/trace"
    exit 0 ;;
esac
exec "REAL" "$@"
'''

# A terminal's `hc get --json` as the labels, foreground and creation time the launcher reads.
def terminal_json(dir_, labels=None, fg="zsh", created_ms=0, argv=None):
    doc = {"labels": {"octopilot.dir": dir_, **(labels or {})},
           "foreground": {"name": fg}, "created_ms": created_ms}
    if argv is not None:
        doc["argv"] = argv
    return doc


def now_ms():
    return int(time.time() * 1000)


class OpenGuiWindowTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="open-gui-", dir=SCRATCH)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.shim = os.path.join(self.root, "shim")
        self.state = os.path.join(self.root, "state")
        os.makedirs(self.shim)
        os.makedirs(self.state)
        for name, body in SHIMS.items():
            path = os.path.join(self.shim, name)
            with open(path, "w") as fh:
                fh.write(body)
            os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)

    def write(self, name, text):
        with open(os.path.join(self.shim, name), "w") as fh:
            fh.write(text)

    def copy_checkout(self):
        """A checkout with the launcher and config.sh copied, and the shim scan.sh (SCAN_SHIM)."""
        checkout = os.path.join(self.root, "octopilot")
        os.makedirs(os.path.join(checkout, "herdr"))
        shutil.copy(LAUNCHER, os.path.join(checkout, "herdr", "open-gui.sh"))
        shutil.copy(os.path.join(REPO, "config.sh"), checkout)
        scan = os.path.join(checkout, "scan.sh")
        with open(scan, "w") as fh:
            fh.write(SCAN_SHIM.replace('"REAL"', '"' + os.path.join(REPO, "scan.sh") + '"'))
        os.chmod(scan, os.stat(scan).st_mode | stat.S_IEXEC)
        return checkout

    def launcher_env(self, home=None):
        env = {
            **os.environ,
            "SHIM": self.shim,
            "PATH": self.shim + os.pathsep + os.environ["PATH"],
            "OCTOPILOT_STATE_DIR": self.state,
            "HERDR_APP_HOME": os.path.join(self.root, "app-home"),
            "HOME_REPOS": "none/none",
            "ISSUE_ORGS": "none-org",
            "CLONE_ROOTS": os.path.join(self.root, "no-clones"),
        }
        if home:
            env["HOME"] = home  # where scan.py finds the claude transcripts
        return env

    def launch(self, *args, home=None, checkout=None):
        launcher = LAUNCHER if checkout is None else os.path.join(checkout, "herdr", "open-gui.sh")
        return subprocess.run(["bash", launcher, *args], env=self.launcher_env(home), cwd=self.root,
                              capture_output=True, text=True, timeout=120)

    def trace(self):
        path = os.path.join(self.shim, "trace")
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return fh.read().splitlines()

    def argv_of(self, verb):
        """The argv (a JSON array) that the first `herdr-app do <verb>` call was given."""
        for line in self.trace():
            if line.startswith(f"herdr-app do {verb} "):
                argv, _ = json.JSONDecoder().raw_decode(line.split(" --argv ", 1)[1])
                return argv
        self.fail(f"no herdr-app do {verb} with an argv in {self.trace()}")

    def record_sessions(self, sessions, home, transcripts=True):
        """The state's launcher_sessions ({agent: (session, cwd)}), and each session's transcript."""
        with open(os.path.join(self.state, "state.json"), "w") as fh:
            json.dump({"launcher_sessions": {
                agent: {"session": sid, "cwd": cwd} for agent, (sid, cwd) in sessions.items()}}, fh)
        if not transcripts:
            return
        folder = os.path.join(home, ".claude", "projects", "scratch")
        os.makedirs(folder, exist_ok=True)
        for sid, _ in sessions.values():
            open(os.path.join(folder, sid + ".jsonl"), "w").close()

    def app_listing(self):
        """The space s1 with the host th (tab OCTOPILOT, pane p1), tp and tc, as herdr-app ls shows it."""
        self.write("app-ls.json", json.dumps({"spaces": [{"id": "s1", "name": "Octopilot", "tabs": [
            {"id": "t1", "name": "OCTOPILOT", "terminals": [{"terminal": "th", "pane": "p1"}]},
            {"id": "t3", "name": "engine", "terminals": [{"terminal": "tp", "pane": "p2"}]},
            {"id": "t5", "name": "scratch", "terminals": [{"terminal": "tc", "pane": "p4"}]},
        ]}]}))

    def running_app(self):
        """A herdr-app that answers (its window is open), with the app_listing."""
        self.write("window", "")
        self.write("space", "")
        self.app_listing()

    def shown_host(self, checkout=REPO):
        """This checkout's headless, shown in space s1 (tab OCTOPILOT, pane p1) of a running herdr-app."""
        self.running_app()
        self.write("ls-octopilot.agent=octopilot-headless.json",
                   json.dumps({"terminals": [{"address": "th"}]}))
        self.write("get-th.json", json.dumps(terminal_json(
            os.path.realpath(checkout), {"herdr.space": "s1", "octopilot.agent": "octopilot-headless"},
            fg="uv")))

    def test_no_window_and_no_session_stops_before_anything(self):
        self.write("env", "HOME=/nowhere\nLANG=C\n")
        res = self.launch()
        self.assertEqual(res.returncode, 1, res.stderr)
        # The launcher's output goes through its log tee to stdout.
        out = res.stdout + res.stderr
        self.assertIn("herdr-app has no window on", out)
        self.assertIn("then run Open Octopilot again", out)
        trace = self.trace()
        self.assertTrue(trace, "herdr-app status was not called")
        self.assertFalse([t for t in trace if t.startswith(("hc ", "systemd-run"))], trace)
        self.assertFalse([t for t in trace if t.startswith("herdr-app do")], trace)

    def test_timeout_never_starts_a_second_window(self):
        self.write("env", "WAYLAND_DISPLAY=wayland-1\n")
        self.write("hang", "")
        res = self.launch()
        self.assertEqual(res.returncode, 1)
        self.assertIn("herdr-app did not answer", res.stdout + res.stderr)
        self.assertFalse([t for t in self.trace() if t.startswith("systemd-run")], self.trace())

    def test_lock_wait_says_so_before_it_blocks(self):
        self.write("env", "")
        tick_lock = os.path.join(self.state, "tick.lock")
        holder = subprocess.Popen(["flock", tick_lock, "sleep", "2"])
        self.addCleanup(holder.wait)
        for _ in range(100):
            if subprocess.run(["flock", "-n", tick_lock, "true"]).returncode != 0:
                break
            time.sleep(0.05)
        res = self.launch()
        out = res.stdout + res.stderr
        self.assertIn("waiting for the engine's tick to finish", out)
        self.assertIn("herdr-app has no window", out)

    def test_children_do_not_inherit_the_locks(self):
        self.write("env", "WAYLAND_DISPLAY=wayland-1\n")
        self.launch()
        fds = os.path.join(self.shim, "fds")
        opened = open(fds).read().splitlines() if os.path.exists(fds) else []
        self.assertEqual(opened, [])

    def test_graphical_session_opens_the_window(self):
        self.write("env", "WAYLAND_DISPLAY=wayland-1\n")
        res = self.launch()
        trace = self.trace()
        run = next(i for i, t in enumerate(trace) if t.startswith("systemd-run"))
        self.assertEqual(trace[run], "systemd-run --user --collect --quiet"
                         f" --setenv=PATH={self.shim}{os.pathsep}{os.environ['PATH']}"
                         " --setenv=HERDR_APP_HOME=" + os.path.join(self.root, "app-home")
                         + " herdr-app")
        # The window answers after systemd-run: the run goes on past the check.
        self.assertIn("herdr-app status", trace[run:])
        self.assertNotIn("has no window", res.stdout + res.stderr)

    def test_held_lock_makes_a_tick_say_so(self):
        self.write("env", "")
        # Runs while the launcher holds tick.lock: the status check is its first herdr call.
        self.write("on_status", (
            f'OCTOPILOT_STATE_DIR={self.state} HOME_REPOS=none/none ISSUE_ORGS=none-org '
            f'CLONE_ROOTS={self.root}/no-clones STATE_FILE= '
            f'{REPO}/scan.sh tick > {self.shim}/tick.out 2>&1\n'))
        res = self.launch()
        self.assertEqual(res.returncode, 1)
        with open(os.path.join(self.shim, "tick.out")) as fh:
            self.assertEqual(fh.read().strip(), "tick already running")
        # Released with the launcher: a second holder of the same file gets it at once.
        held = subprocess.run(["flock", "-n", os.path.join(self.state, "tick.lock"), "true"])
        self.assertEqual(held.returncode, 0)
        held = subprocess.run(["flock", "-n", os.path.join(self.state, "exec.lock"), "true"])
        self.assertEqual(held.returncode, 0)

    def test_recorded_sessions_are_resumed_in_their_folders(self):
        self.shown_host()
        home = os.path.join(self.root, "home")
        prompter, code = str(uuid.uuid4()), str(uuid.uuid4())
        prompter_dir, code_dir = os.path.join(self.root, "prompter-dir"), os.path.join(self.root, "code-dir")
        os.makedirs(prompter_dir)
        os.makedirs(code_dir)
        self.record_sessions({"octopilot-prompter": (prompter, prompter_dir),
                              "octopilot-code": (code, code_dir)}, home)
        self.launch(home=home)
        argv = self.argv_of("pane.split")
        self.assertEqual(argv[argv.index("--resume") + 1], prompter)
        self.assertEqual(argv[argv.index("-C") + 1], prompter_dir)
        self.assertNotIn("/octopilot:prompter", argv)
        self.assertNotIn("--session-id", argv)
        argv = self.argv_of("tab.new")
        self.assertEqual(argv[argv.index("--resume") + 1], code)
        self.assertEqual(argv[argv.index("-C") + 1], code_dir)
        self.assertNotIn("--session-id", argv)
        trace = self.trace()
        self.assertTrue(any(t.startswith("hc label tp ") and f"octopilot.session={prompter}" in t
                            for t in trace), trace)
        self.assertTrue(any(t.startswith("hc label tc ") and f"octopilot.session={code}" in t
                            for t in trace), trace)

    def test_no_transcript_means_a_fresh_start(self):
        self.shown_host()
        home = os.path.join(self.root, "home")
        self.record_sessions({"octopilot-prompter": (str(uuid.uuid4()), self.root),
                              "octopilot-code": (str(uuid.uuid4()), self.root)},
                             home, transcripts=False)
        self.launch(home=home)
        argv = self.argv_of("pane.split")
        self.assertIn("/octopilot:prompter", argv)
        self.assertIn("--session-id", argv)
        self.assertNotIn("--resume", argv)
        argv = self.argv_of("tab.new")
        self.assertIn("--session-id", argv)
        self.assertNotIn("--resume", argv)

    def test_terminals_layout_resumes_the_recorded_code_session(self):
        self.running_app()
        home = os.path.join(self.root, "home")
        code, code_dir = str(uuid.uuid4()), os.path.join(self.root, "code-dir")
        os.makedirs(code_dir)
        self.record_sessions({"octopilot-code": (code, code_dir)}, home)
        self.launch("--terminals", home=home)
        argv = self.argv_of("tab.new")
        self.assertEqual(argv[argv.index("--resume") + 1], code)
        self.assertEqual(argv[argv.index("-C") + 1], code_dir)
        self.assertNotIn("--session-id", argv)
        self.assertTrue(any(t.startswith("hc label tc ") and f"octopilot.session={code}" in t
                            for t in self.trace()), self.trace())

    def test_terminals_layout_starts_the_code_agent_fresh_without_a_transcript(self):
        self.running_app()
        home = os.path.join(self.root, "home")
        self.record_sessions({"octopilot-code": (str(uuid.uuid4()), self.root)}, home,
                             transcripts=False)
        self.launch("--terminals", home=home)
        argv = self.argv_of("tab.new")
        self.assertNotIn("--resume", argv)
        self.assertNotIn("--session-id", argv)
        self.assertNotIn("-C", argv)

    def test_a_recorded_folder_that_is_gone_starts_fresh(self):
        self.shown_host()
        home = os.path.join(self.root, "home")
        gone = os.path.join(self.root, "removed-folder")
        self.record_sessions({"octopilot-prompter": (str(uuid.uuid4()), gone),
                              "octopilot-code": (str(uuid.uuid4()), gone)}, home)
        res = self.launch(home=home)
        self.assertNotIn("Traceback", res.stdout + res.stderr)
        self.assertIn(f"folder {gone} is gone", res.stdout + res.stderr)
        argv = self.argv_of("pane.split")
        self.assertIn("/octopilot:prompter", argv)
        self.assertNotIn("--resume", argv)
        argv = self.argv_of("tab.new")
        self.assertIn("--session-id", argv)
        self.assertNotIn("--resume", argv)

    def test_bare_shell_code_terminal_is_reaped_but_not_a_running_claude(self):
        self.write("window", "")
        real = os.path.realpath(REPO)
        old = now_ms() - 60000
        self.write("ls-octopilot.agent=octopilot-code.json", json.dumps({"terminals": [
            {"address": "t-shell"}, {"address": "t-young"}, {"address": "t-claude"}]}))
        code = {"octopilot.agent": "octopilot-code"}
        self.write("get-t-shell.json", json.dumps(terminal_json(real, code, "zsh", old)))
        self.write("get-t-young.json", json.dumps(terminal_json(real, code, "zsh", now_ms())))
        self.write("get-t-claude.json", json.dumps(terminal_json(real, code, "claude", old)))
        self.launch()
        trace = self.trace()
        # Under the launcher's locks a shell of any age is bare: the young one goes too.
        for t in ("t-shell", "t-young"):
            self.assertIn(f"hc kill {t}", trace)
            self.assertIn(f"hc rm {t}", trace)
        self.assertFalse([line for line in trace if line.startswith(("hc kill t-claude", "hc rm t-claude"))],
                         trace)

    def assert_restored_between_tabs_and_tick(self, trace):
        """restore-sessions runs after the tabs are set up, with the locks still held; the
        tick is requested after it, with the locks released."""
        restores = [t for t in trace if t.startswith("scan restore-sessions")]
        requests = [t for t in trace if t.startswith("scan request tick")]
        self.assertEqual(restores, ["scan restore-sessions locks=held"], trace)
        self.assertEqual(requests, ["scan request tick locks=free"], trace)
        restore = trace.index(restores[0])
        tabs = [i for i, t in enumerate(trace) if t.startswith("herdr-app do ")]
        self.assertLess(max(i for i in tabs if i < restore), restore, trace)
        self.assertLess(restore, trace.index(requests[0]), trace)
        self.assertNotIn("scan restore-sessions 8", trace)

    def test_restore_runs_after_the_tabs_and_before_the_tick_on_the_existing_space(self):
        checkout = self.copy_checkout()
        self.shown_host(checkout)
        res = self.launch(checkout=checkout)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assert_restored_between_tabs_and_tick(self.trace())
        for name in ("tick.lock", "exec.lock"):
            held = subprocess.run(["flock", "-n", os.path.join(self.state, name), "true"])
            self.assertEqual(held.returncode, 0, name)

    def test_restore_runs_on_a_new_space(self):
        checkout = self.copy_checkout()
        self.running_app()
        res = self.launch(checkout=checkout)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assert_restored_between_tabs_and_tick(self.trace())

    def test_restore_runs_on_terminals(self):
        checkout = self.copy_checkout()
        self.running_app()
        res = self.launch("--terminals", checkout=checkout)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assert_restored_between_tabs_and_tick(self.trace())

    def test_a_refused_restore_is_warned_about_and_the_run_succeeds(self):
        checkout = self.copy_checkout()
        self.shown_host(checkout)
        self.write("restore_rc", "1")
        res = self.launch(checkout=checkout)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("restore-sessions refused", res.stdout + res.stderr)
        self.assertIn("scan request tick locks=free", self.trace())

    def test_a_failed_launch_does_not_restore_and_releases_the_locks(self):
        checkout = self.copy_checkout()
        self.write("env", "HOME=/nowhere\nLANG=C\n")
        res = self.launch(checkout=checkout)
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertFalse([t for t in self.trace() if t.startswith("scan ")], self.trace())
        for name in ("tick.lock", "exec.lock"):
            held = subprocess.run(["flock", "-n", os.path.join(self.state, name), "true"])
            self.assertEqual(held.returncode, 0, name)


    def test_a_failed_run_frees_the_locks_while_it_waits_for_enter(self):
        checkout = self.copy_checkout()
        self.write("env", "HOME=/nowhere\nLANG=C\n")
        # The Enter wait needs a terminal on stdin: the run's pane.
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        proc = subprocess.Popen(["bash", os.path.join(checkout, "herdr", "open-gui.sh"), "--close-launcher"],
                                env=self.launcher_env(), cwd=self.root,
                                stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        self.addCleanup(proc.kill)
        output = b""
        deadline = time.time() + 60
        while b"Press Enter" not in output and time.time() < deadline and proc.poll() is None:
            if select.select([master], [], [], 1)[0]:
                try:
                    output += os.read(master, 4096)
                except OSError:
                    break
        self.assertIn(b"Press Enter", output)
        self.assertIsNone(proc.poll(), "the run must wait for Enter")
        for name in ("tick.lock", "exec.lock"):
            held = subprocess.run(["flock", "-n", os.path.join(self.state, name), "true"])
            self.assertEqual(held.returncode, 0, name)
        os.write(master, b"\n")
        self.assertEqual(proc.wait(timeout=60), 1)

    def test_young_bare_shells_are_reaped_on_the_systemd_run_path(self):
        checkout = self.copy_checkout()
        self.write("env", "WAYLAND_DISPLAY=wayland-1\n")
        self.write("space", "")
        self.app_listing()
        real = os.path.realpath(checkout)
        # Shells started just now, as a restore leaves them: the launcher's locks mean
        # nothing is still starting, so they are reaped all the same.
        young = {"octopilot-headless": "ys1", "octopilot-prompter": "ys2", "octopilot-code": "ys3"}
        for agent, term in young.items():
            self.write(f"ls-octopilot.agent={agent}.json", json.dumps({"terminals": [{"address": term}]}))
            self.write(f"get-{term}.json", json.dumps(terminal_json(
                real, {"herdr.space": "s1", "octopilot.agent": agent}, "zsh", now_ms())))
        res = self.launch(checkout=checkout)
        trace = self.trace()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        for term in young.values():
            self.assertIn(f"hc kill {term}", trace)
            self.assertIn(f"hc rm {term}", trace)
        first_app = next(i for i, t in enumerate(trace) if t.startswith("herdr-app do "))
        self.assertLess(trace.index("hc rm ys1"), first_app, trace)
        self.assertLess(trace.index("hc rm ys3"), first_app, trace)
        with open(os.path.join(self.shim, "restore_env")) as fh:
            self.assertEqual(fh.read().strip(), "1")


    def test_bare_shells_after_a_reboot_keep_their_space(self):
        self.running_app()
        # The headless of the restored space is a bare shell: it still names the space.
        self.write("ls-octopilot.agent=octopilot-headless.json",
                   json.dumps({"terminals": [{"address": "ys1"}]}))
        self.write("get-ys1.json", json.dumps(terminal_json(
            os.path.realpath(REPO), {"herdr.space": "s1", "octopilot.agent": "octopilot-headless"},
            "zsh", 0)))
        res = self.launch()
        trace = self.trace()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("hc kill ys1", trace)
        self.assertFalse([t for t in trace if t.startswith("herdr-app do space.new")], trace)
        self.assertTrue([t for t in trace if t.startswith("herdr-app do tab.new --space s1")], trace)


if __name__ == "__main__":
    unittest.main()
