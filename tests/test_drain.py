"""The drain: the inbox's requests handled in order, held ones kept, the queued
sends delivered once per item. GitHub, herdr and git are faked (tests/stand.py)."""
import contextlib
import io
import os
import time
import unittest
from unittest import mock

import engine
import scan
from tests.stand import HEAD, REPO, RequestCase

PR = f"{REPO}#31"
ISSUE = f"{REPO}!12"
OLD = "old1234"


def inbox(*texts):
    return [{"at": int(time.time()), "text": t} for t in texts]


class DrainTest(RequestCase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(scan, "reboot", side_effect=self.reboot)
        self.reboot_calls = patch.start()
        self.addCleanup(patch.stop)

    def reboot(self, state, k):
        state["items"][k]["phase"] = "booting"
        scan.save(state)
        return 0

    def drain(self, mode="on"):
        return engine.drain(mode, time.monotonic())

    def inbox_texts(self):
        return [m["text"] for m in scan.load().get("inbox") or []]

    def events(self):
        with open(scan.EVENTS_FILE) as fh:
            return fh.read()

    def test_requests_run_in_order_and_are_consumed(self):
        self.save({}, inbox=inbox(f"REQUEST no {PR}",
                                  f"OCTOPILOT {PR} ready {HEAD} done",
                                  "REQUEST pause", "REQUEST resume",
                                  "REQUEST nonsense here"))
        lines = self.drain()
        self.assertEqual(lines, [f"{PR}: declined", "paused", "resumed",
                                 "unknown request: nonsense here"])
        self.assertEqual(scan.load()["declined"], [PR])
        self.assertEqual(self.inbox_texts(), [])
        self.assertFalse(scan.load()["paused"])
        self.assertLess(self.events().index('"kind": "decline"'),
                        self.events().index('"kind": "pause"'))

    def test_a_paused_drain_holds_go_and_investigate_but_runs_no(self):
        self.save({}, paused=True, inbox=inbox(
            f"REQUEST go {ISSUE}",
            f"REQUEST no {PR}",
            "REQUEST /investigate owner/repo How does the cache work"))
        lines = self.drain()
        self.assertEqual(lines, [f"{PR}: declined"])
        self.assertEqual(scan.load()["declined"], [PR])
        self.assertEqual(self.inbox_texts(), [
            f"REQUEST go {ISSUE}",
            "REQUEST /investigate owner/repo How does the cache work"])
        self.assertEqual(self.spawns(), [])

    def test_resume_in_the_same_drain_releases_the_held_go(self):
        self.save({}, paused=True, inbox=inbox(f"REQUEST go {ISSUE}",
                                               "REQUEST resume"))
        lines = self.drain()
        self.assertEqual(lines[0], "resumed")
        self.assertIn(f"{ISSUE}: sent work", lines[1])
        self.assertEqual(self.inbox_texts(), [])

    def test_a_wind_down_hold_is_consumed_and_the_confirmed_request_starts(self):
        self.snapshot(rows=[{"kind": "PROPOSE", "key": ISSUE, "title": "Add login"}])
        self.save({}, inbox=inbox(f"REQUEST go {ISSUE}"))
        with mock.patch.object(scan, "wind_down", return_value="yes"):
            lines = self.drain()
        self.assertEqual(lines, [f"{ISSUE} held (wind-down): Add login"])
        self.assertEqual((self.inbox_texts(), self.spawns()), ([], []))
        self.save({}, inbox=inbox(f"REQUEST go {ISSUE} (confirmed past wind-down)"))
        with mock.patch.object(scan, "wind_down", return_value="yes"):
            self.drain()
        self.assertEqual(len(self.spawns()), 1)

    def test_a_cap_hold_is_consumed(self):
        self.snapshot(active=scan.MAX_ACTIVE)
        self.save({}, inbox=inbox(f"REQUEST go {ISSUE}"))
        with mock.patch.object(scan, "wind_down", return_value=None):
            self.assertIn("held (cap)", self.drain()[0])
        self.assertEqual((self.inbox_texts(), self.spawns()), ([], []))

    def test_a_request_that_raises_does_not_end_the_drain(self):
        self.save({}, inbox=inbox(f"REQUEST no {PR}", "REQUEST pause"))
        with mock.patch.dict(engine.HANDLERS, {"no": mock.Mock(
                side_effect=RuntimeError("boom"))}):
            lines = self.drain()
        self.assertEqual(lines, ["no failed: RuntimeError: boom", "paused"])
        self.assertIn('"kind": "request-failed"', self.events())
        self.assertTrue(scan.load()["paused"])

    def test_the_budget_stops_the_inbox_and_leaves_what_is_left(self):
        self.save({}, inbox=inbox(f"REQUEST no {PR}"))
        lines = engine.drain("on", 0, clock=lambda: engine.ACT_BUDGET_SECS)
        self.assertEqual(lines, ["budget spent: the inbox waits for the next drain"])
        self.assertEqual(self.inbox_texts(), [f"REQUEST no {PR}"])

    def test_plan_lists_what_it_would_do_and_changes_nothing(self):
        self.save({}, paused=False, inbox=inbox(
            f"REQUEST no {PR}", f"OCTOPILOT {PR} ready {HEAD} done"))
        before = scan.load()
        lines = self.drain(mode="plan")
        self.assertEqual(lines, [f"would handle: no {PR}",
                                 f"would drop: OCTOPILOT {PR} ready {HEAD} done"])
        self.assertEqual(scan.load(), before)
        self.assertEqual(self.addresses(), [])

    def test_plan_shows_a_paused_hold(self):
        self.save({}, paused=True, inbox=inbox(f"REQUEST go {ISSUE}"))
        self.assertEqual(self.drain(mode="plan"),
                         [f"held while paused: go {ISSUE}"])
        self.assertEqual(self.inbox_texts(), [f"REQUEST go {ISSUE}"])

    def test_one_send_per_item_when_a_request_and_the_queue_touch_it(self):
        self.save({PR: {"mode": "comment", "branch": "feat/b", "session": "w9",
                        "worktree": "/wt/feat-b", "phase": "fixing", "pr": 31,
                        "reviewed_sha": OLD, "review_rounds": 1}},
                  inbox=inbox("REQUEST re-review " + PR),
                  pending_sends=[{"key": PR, "template": "address-feedback",
                                  "subs": {"REPO": REPO, "PR": 31,
                                           "BRANCH": "feat/b", "BASE": "main"},
                                  "at": int(time.time()), "tries": 0, "error": None,
                                  "reason": "feedback"}])
        self.drain()
        self.assertEqual(self.addresses(), ["herdr:w9"])
        self.assertEqual(len(scan.load()["pending_sends"]), 1)
        self.drain()
        self.assertEqual(self.addresses(), ["herdr:w9", "herdr:w9"])
        self.assertEqual(scan.load()["pending_sends"], [])

    def test_drain_is_off_unless_the_engine_is_on(self):
        self.save({}, inbox=inbox(f"REQUEST no {PR}"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(scan.drain(["drain"]), 0)
        self.assertIn("engine off here", out.getvalue())
        self.assertEqual(self.inbox_texts(), [f"REQUEST no {PR}"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            scan.drain(["drain", "--quiet"])
        self.assertEqual(out.getvalue(), "")

    def test_drain_plan_mode_prints_and_changes_nothing(self):
        self.save({}, inbox=inbox(f"REQUEST no {PR}"))
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"OCTOPILOT_ENGINE": "on"}), \
                contextlib.redirect_stdout(out):
            scan.drain(["drain", "--plan"])
        self.assertEqual(out.getvalue().strip(), f"would handle: no {PR}")
        self.assertEqual(self.inbox_texts(), [f"REQUEST no {PR}"])
        self.assertEqual(scan.load()["declined"], [])

    def test_drain_is_self_locking(self):
        self.assertIn("drain", scan.SELF_LOCKING)


if __name__ == "__main__":
    unittest.main()
