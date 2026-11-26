import contextlib
import fcntl
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from tests import fake
import scan
from tests.test_rerequest import FakeBackend, HEAD, KEY, new_state, pr_detail


def events():
    """The event log written so far, as dicts."""
    with open(scan.EVENTS_FILE) as fh:
        return [json.loads(line) for line in fh]


def run_tick(*args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = scan.tick(["tick", *args])
    return code, out.getvalue()


class TickTest(unittest.TestCase):
    def test_tick_saves_the_transitions_and_writes_the_snapshot(self):
        state = new_state()
        with fake.fake_world(pr_detail=pr_detail):
            scan.save(state)
            code, out = run_tick("--quiet")
            self.assertEqual(code, 0)
            self.assertEqual(out, "")
            with open(scan.STATE_FILE) as fh:
                saved = json.load(fh)
            with open(scan.SNAPSHOT_FILE) as fh:
                snap = json.load(fh)
            self.assertEqual(saved["items"][KEY]["pending_rerequest"], HEAD)
            self.assertEqual(saved["tick_counts"], {"REVIEW": 1})
            self.assertIsInstance(saved["last_tick"], int)
            self.assertNotIn("last_scan", saved)
            self.assertIsInstance(snap["last_tick"], int)
            self.assertNotIn("orch", saved)
            self.assertEqual([(r["kind"], r["key"]) for r in snap["rows"]],
                             [("REVIEW", KEY)])

    def test_tick_prints_the_table_unless_quiet(self):
        with fake.fake_world(pr_detail=pr_detail):
            scan.save(new_state())
            code, out = run_tick()
        self.assertEqual(code, 0)
        self.assertIn(KEY, out)

    def test_tick_logs_only_a_change_of_table(self):
        with fake.fake_world(pr_detail=pr_detail):
            scan.save(new_state())
            run_tick("--quiet")
            run_tick("--quiet")
            self.assertEqual([e["kind"] for e in events()], ["tick"])

    def test_tick_logs_a_gone_row_once_it_untracks_the_item(self):
        gone = {"items": {KEY: {"mode": "fix", "pr": 5, "session": "w1"}}}
        with fake.fake_world(all_sessions=([], {"herdr"}, set()),
                             backend_for=lambda s: FakeBackend(),
                             pr_detail=pr_detail):
            scan.save(dict(scan.EMPTY, **gone))
            run_tick("--quiet")
            with open(scan.STATE_FILE) as fh:
                self.assertEqual(json.load(fh)["items"], {})
            self.assertIn({"at": mock.ANY, "kind": "gone", "key": KEY}, events())

    def test_a_tick_that_finds_the_lock_held_returns_at_once(self):
        with fake.fake_world(), open(scan.TICK_LOCK, "w") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            code, out = run_tick("--quiet")
        self.assertEqual((code, out), (0, "tick already running\n"))
        self.assertFalse(os.path.exists(scan.STATE_FILE))

    def test_a_tick_that_finds_exec_lock_held_scans_but_does_not_wait_to_act(self):
        with fake.fake_world(), mock.patch.dict(os.environ, {"OCTOPILOT_ENGINE": "on"}), \
                open(scan.EXEC_LOCK, "w") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            code, out = run_tick()
        self.assertEqual(code, 0)
        self.assertIn("engine busy", out)

    def test_main_does_not_wrap_tick_in_the_state_lock(self):
        # tick takes the lock itself: main() wrapping it too would deadlock,
        # so count the entries instead of really locking.
        entries = []

        @contextlib.contextmanager
        def counting_lock():
            entries.append(1)
            yield

        with fake.fake_world(pr_detail=pr_detail), \
                mock.patch.object(scan, "locked", counting_lock), \
                mock.patch.object(sys, "argv", ["scan.py", "tick", "--quiet"]):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(scan.main(), 0)
            self.assertTrue(os.path.exists(scan.SNAPSHOT_FILE))
        self.assertEqual(len(entries), 1)

    def test_engine_runs_for_a_scratch_state_dir_or_the_installed_checkout(self):
        with tempfile.TemporaryDirectory() as live, \
                tempfile.TemporaryDirectory() as worktree, \
                mock.patch.object(scan, "installed_dir", return_value=live), \
                mock.patch.object(scan, "OCTOPILOT_DIR", live), \
                mock.patch.dict(os.environ):
            os.environ.pop("OCTOPILOT_STATE_DIR", None)
            self.assertTrue(scan.engine_allowed(live))
            # OCTOPILOT_DIR inherited from the live shell must not decide.
            self.assertFalse(scan.engine_allowed(worktree))
            os.environ["OCTOPILOT_STATE_DIR"] = fake.SCRATCH
            self.assertTrue(scan.engine_allowed(worktree))
