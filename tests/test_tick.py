import contextlib
import fcntl
import io
import json
import os
import sys
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
            self.assertEqual(saved["last_tick"], snap["last_tick"])
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

    def test_main_runs_tick_without_taking_the_state_lock_first(self):
        with fake.fake_world(pr_detail=pr_detail), \
                mock.patch.object(sys, "argv", ["scan.py", "tick", "--quiet"]):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(scan.main(), 0)
            self.assertTrue(os.path.exists(scan.SNAPSHOT_FILE))
