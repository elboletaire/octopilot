"""open-gui.sh before its first step: the engine's locks, and ensure_window's herdr-app
window check. Runs the real launcher with herdr-app, systemd-run, systemctl and hc
shimmed on PATH; each shim appends its argv to a trace. No window is ever opened."""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

from tests import SCRATCH

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(REPO, "herdr", "open-gui.sh")

# A shim logs its argv; herdr-app status succeeds only once $SHIM/window exists, and
# systemd-run (the window opener) creates it. $SHIM/on_status, when present, runs first.
SHIMS = {
    "herdr-app": '''#!/bin/sh
echo "herdr-app $*" >> "$SHIM/trace"
if [ "$1" = status ]; then
  [ -f "$SHIM/on_status" ] && sh "$SHIM/on_status"
  [ -f "$SHIM/window" ] && exit 0
  echo "Connection refused" >&2
  exit 1
fi
exit 1
''',
    "systemd-run": '''#!/bin/sh
echo "systemd-run $*" >> "$SHIM/trace"
touch "$SHIM/window"
''',
    "systemctl": '''#!/bin/sh
echo "systemctl $*" >> "$SHIM/trace"
[ -f "$SHIM/env" ] && cat "$SHIM/env"
''',
    "hc": '''#!/bin/sh
echo "hc $*" >> "$SHIM/trace"
case "$1 $2" in
  "ls --json") echo '{"terminals": []}' ;;
esac
''',
}


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

    def launch(self, *args):
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
        return subprocess.run(["bash", LAUNCHER, *args], env=env, cwd=self.root,
                              capture_output=True, text=True, timeout=120)

    def trace(self):
        path = os.path.join(self.shim, "trace")
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return fh.read().splitlines()

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

    def test_graphical_session_opens_the_window(self):
        self.write("env", "WAYLAND_DISPLAY=wayland-1\n")
        res = self.launch()
        trace = self.trace()
        run = next(i for i, t in enumerate(trace) if t.startswith("systemd-run"))
        self.assertEqual(trace[run], "systemd-run --user --collect --quiet"
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


if __name__ == "__main__":
    unittest.main()
