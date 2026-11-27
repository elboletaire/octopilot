"""`tick` with the engine: off changes nothing, plan logs and changes nothing,
on acts, and a worktree never acts. The scan is replaced by fixed rows, and
GitHub, git and the agents are faked (fake_world)."""
import contextlib
import fcntl
import io
import os
import sys
import unittest
from unittest import mock

from tests.fake import fake_world
import scan

REPO = "owner/repo"
MERGED, A = f"{REPO}#7", f"{REPO}#8"
ROWS = [("MERGED", MERGED, "branch=feat/a mode=fix")]


class FakeBackend:
    name = "herdr"
    prefix = "herdr:"
    remove_label = "removed"
    noun = "workspace"

    def target(self, it):
        return it["session"]

    def discard(self, session, worktree):
        return 0, ""


def item(pr, branch, session):
    return {"mode": "fix", "pr": pr, "branch": branch, "session": session,
            "phase": "fixing", "reviewed_sha": "old1234", "review_rounds": 0}


class TickEngineTest(unittest.TestCase):
    def setUp(self):
        world = fake_world(backend_for=lambda session: FakeBackend(),
                           send_to=lambda address, text: (True, None))
        self.world = world.__enter__()
        self.addCleanup(world.__exit__, None, None, None)
        scanned = mock.patch.object(scan, "scan", side_effect=self.scan)
        scanned.start()
        self.addCleanup(scanned.stop)
        # Restores the environment the tests change.
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("OCTOPILOT_ENGINE", None)
        scan.save({"items": {MERGED: item(7, "feat/a", "w1"),
                             A: item(8, "feat/b", "w2")},
                   "handled_merges": [], "pending_sends": [],
                   "registry": {REPO: {"path": "/repo", "base": "main"}}})

    def scan(self, state, info=None, refresh=False):
        info.update(revived=[], rehomed=[], details={}, meta={})
        return ROWS, 2, 1, 0

    def tick(self, args=(), engine=None):
        if engine is None:
            os.environ.pop("OCTOPILOT_ENGINE", None)
        else:
            os.environ["OCTOPILOT_ENGINE"] = engine
        with contextlib.redirect_stdout(io.StringIO()):
            return scan.tick(list(args))

    def events(self):
        if not os.path.exists(scan.EVENTS_FILE):
            return ""
        with open(scan.EVENTS_FILE) as fh:
            return fh.read()

    def items(self):
        return scan.load()["items"]

    def sent(self):
        return self.world.named("send_to")

    def test_engine_off_changes_nothing(self):
        before = self.items()
        self.tick(engine="off")
        self.assertEqual(self.items(), before)
        self.assertEqual(self.sent(), [])
        self.assertNotIn("engine-", self.events())

    def test_plan_changes_nothing_and_logs_what_it_would_do(self):
        before = self.items()
        self.tick(engine="plan")
        self.assertEqual(self.items(), before)
        self.assertEqual(scan.load()["handled_merges"], [])
        self.assertEqual(self.sent(), [])
        self.assertIn('"kind": "engine-plan"', self.events())

    def test_tick_plan_flag_forces_plan(self):
        before = self.items()
        self.tick(["--plan"], engine="on")
        self.assertEqual(self.items(), before)
        self.assertEqual(self.sent(), [])
        self.assertIn('"kind": "engine-plan"', self.events())

    def test_on_acts_on_the_rows(self):
        self.tick(engine="on")
        self.assertNotIn(MERGED, self.items())
        self.assertIn(MERGED, scan.load()["handled_merges"])
        self.assertEqual([args[0] for args, _ in self.sent()], ["herdr:w2"])

    def test_worktree_never_acts(self):
        before = self.items()
        with mock.patch.object(scan, "installed_dir", return_value="/live/octopilot"), \
                mock.patch.object(scan, "OCTOPILOT_DIR", "/wt/octopilot"), \
                mock.patch.dict(os.environ, {"OCTOPILOT_ENGINE": "on"}):
            os.environ.pop("OCTOPILOT_STATE_DIR", None)
            self.assertEqual(scan.engine_mode(), "off")
            self.tick(engine="on")
        self.assertEqual(self.items(), before)
        self.assertEqual(self.sent(), [])

    def worktree(self):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(scan, "installed_dir",
                                              return_value="/live/octopilot"))
        stack.enter_context(mock.patch.object(scan, "OCTOPILOT_DIR", "/wt/octopilot"))
        stack.enter_context(mock.patch.dict(os.environ))
        os.environ.pop("OCTOPILOT_STATE_DIR", None)
        self.addCleanup(stack.close)

    def test_tick_plan_in_a_worktree_says_the_engine_is_off(self):
        self.worktree()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            scan.tick(["--plan"])
        self.assertIn("engine off here: /wt/octopilot is not the live checkout", out.getvalue())
        self.assertEqual(self.sent(), [])

    def test_scan_and_refresh_run_tick_without_deadlocking(self):
        # main() maps scan and refresh to tick before the lock checks: tick takes
        # the state lock itself, so a second lock in this process would hang.
        for argv, refresh in ((["scan.py", "scan"], False),
                              (["scan.py", "refresh"], True)):
            with self.subTest(argv=argv), \
                    mock.patch.object(scan, "scan", side_effect=self.scan) as scanned, \
                    mock.patch.object(sys, "argv", argv), \
                    mock.patch.dict(os.environ, {"OCTOPILOT_ENGINE": "off"}), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(scan.main(), 0)
                self.assertIs(scanned.call_args.kwargs["refresh"], refresh)
            self.assertIsInstance(scan.load()["last_tick"], int)

    def test_scan_with_the_engine_on_says_stop_and_leaves_the_engine_rows_out(self):
        # An orchestrator still running `scan` must not act: the dashboard runs the engine.
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"OCTOPILOT_ENGINE": "on"}), \
                mock.patch.object(sys, "argv", ["scan.py", "scan"]), \
                contextlib.redirect_stdout(out):
            self.assertEqual(scan.main(), 0)
        text = out.getvalue()
        self.assertTrue(text.startswith(
            "octopilot has no orchestrator any more: stop ticking, "
            "the engine acts on every row\n"), text)
        self.assertNotIn(f"MERGED   {MERGED}", text)
        # The legacy tick sent nothing and left the engine rows alone.
        self.assertEqual(self.sent(), [])
        self.assertIn(MERGED, self.items())
        self.assertNotIn(MERGED, scan.load()["handled_merges"])
        # The snapshot keeps every row.
        with open(scan.SNAPSHOT_FILE) as fh:
            snap = fh.read()
        self.assertIn(MERGED, snap)

    def test_a_direct_tick_with_the_engine_on_keeps_the_full_table(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"OCTOPILOT_ENGINE": "on"}), \
                contextlib.redirect_stdout(out):
            self.assertEqual(scan.tick(["tick"]), 0)
        text = out.getvalue()
        self.assertIn(f"MERGED   {MERGED}", text)
        self.assertNotIn("has no orchestrator", text)

    def test_a_quiet_tick_that_finds_exec_lock_held_prints_nothing(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"OCTOPILOT_ENGINE": "on"}), \
                open(scan.EXEC_LOCK, "w") as held, \
                contextlib.redirect_stdout(out):
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            self.assertEqual(scan.tick(["tick", "--quiet"]), 0)
        self.assertEqual(out.getvalue(), "")

    def test_quiet_tick_with_the_engine_on_prints_only_the_engine_lines(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"OCTOPILOT_ENGINE": "on"}), \
                contextlib.redirect_stdout(out):
            self.assertEqual(scan.tick(["tick", "--quiet"]), 0)
        self.assertIn(MERGED, out.getvalue())
        self.assertNotIn("PAUSED", out.getvalue())
        self.assertNotIn("ACTIVE", out.getvalue())

    def test_engine_mode_judges_the_callers_checkout(self):
        # An inherited OCTOPILOT_DIR must not decide for the dashboard's own.
        self.worktree()
        with mock.patch.dict(os.environ, {"OCTOPILOT_ENGINE": "on"}):
            self.assertEqual(scan.engine_mode(checkout="/live/octopilot"), "on")
            self.assertEqual(scan.engine_mode(checkout="/wt/other"), "off")
            self.assertEqual(scan.engine_mode(), "off")

    def test_engine_mode_reads_the_env_and_guard(self):
        with mock.patch.object(scan, "installed_dir", return_value="/live/octopilot"), \
                mock.patch.object(scan, "OCTOPILOT_DIR", "/live/octopilot"):
            os.environ.pop("OCTOPILOT_ENGINE", None)
            self.assertEqual(scan.engine_mode(), "on")
        with mock.patch.object(scan, "installed_dir", return_value="/live/octopilot"), \
                mock.patch.object(scan, "OCTOPILOT_DIR", "/wt/octopilot"):
            os.environ.pop("OCTOPILOT_STATE_DIR", None)
            self.assertEqual(scan.engine_mode(), "off")
            os.environ["OCTOPILOT_STATE_DIR"] = "/scratch/octopilot"
            os.environ["OCTOPILOT_ENGINE"] = "on"
            self.assertEqual(scan.engine_mode(), "on")
            self.assertEqual(scan.engine_mode(["--plan"]), "plan")
            os.environ["OCTOPILOT_ENGINE"] = "plan"
            self.assertEqual(scan.engine_mode(), "plan")
            os.environ["OCTOPILOT_ENGINE"] = "ON"
            self.assertEqual(scan.engine_mode(), "off")


if __name__ == "__main__":
    unittest.main()
