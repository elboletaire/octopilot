"""The dashboard's GitHub pass rings the orchestrator only when the engine
does not act: with the engine on there is nobody to wake."""
import contextlib
import json
import os
import tempfile
import types
import unittest
from unittest import mock

from tests import fake  # noqa: F401  (sets the scratch environment)

try:
    import dashboard
except ImportError:  # the rich package is the dashboard's own dependency
    dashboard = None


@unittest.skipIf(dashboard is None, "dashboard needs rich")
class GithubPassTest(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        self.model = dashboard.Model.__new__(dashboard.Model)
        self.model.refresh_snapshot = mock.Mock()
        self.model.wake_orchestrator = mock.Mock()

    def test_engine_on_does_not_wake_the_orchestrator(self):
        os.environ["OCTOPILOT_ENGINE"] = "on"
        self.model.github_pass(False)
        self.model.refresh_snapshot.assert_called_once()
        self.model.wake_orchestrator.assert_not_called()

    def test_engine_off_still_wakes_the_orchestrator(self):
        os.environ.pop("OCTOPILOT_ENGINE", None)
        self.model.github_pass(False)
        self.model.wake_orchestrator.assert_called_once()

    def test_engine_plan_still_wakes_the_orchestrator(self):
        os.environ["OCTOPILOT_ENGINE"] = "plan"
        self.model.github_pass(False)
        self.model.wake_orchestrator.assert_called_once()


@unittest.skipIf(dashboard is None, "dashboard needs rich")
class EngineErrorViewTest(unittest.TestCase):
    def test_kitchen_says_the_reason_in_a_bad_tone(self):
        ev = {"kind": "engine-error", "key": "owner/repo#8",
              "msg": "boot failed: worktree has uncommitted changes"}
        r = dashboard.kitchen_reaction(ev)
        self.assertEqual(r["text"], ev["msg"])
        self.assertEqual(r["tone"], "bad")

    def test_activity_shows_the_reason(self):
        ev = {"kind": "engine-error", "key": "owner/repo#8",
              "msg": "boot failed: worktree has uncommitted changes"}
        self.assertEqual(dashboard.event_detail(ev), ev["msg"])


@unittest.skipIf(dashboard is None, "dashboard needs rich")
class StartHoldsTest(unittest.TestCase):
    """A go or an investigation started now is held past wind-down and at the cap."""

    def model(self, active, untracked=0, items=None, generated_at=1000):
        snap = {"active": active, "untracked": untracked, "generated_at": generated_at}
        state = {"items": items or {}}
        return types.SimpleNamespace(snap=snap, state=state)

    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        wind = mock.patch.object(dashboard.scan, "wind_down", return_value="no")
        self.wind = wind.start()
        self.addCleanup(wind.stop)

    def test_no_hold_below_the_cap(self):
        self.assertEqual(dashboard.start_holds(self.model(active=5)), [])

    def test_the_cap_holds_with_its_marker(self):
        cap = dashboard.scan.MAX_ACTIVE
        [(name, msg)] = dashboard.start_holds(self.model(active=cap - 1, untracked=1))
        self.assertEqual(name, "cap")
        self.assertIn(f"{cap}/{cap}", msg)

    def test_untracked_counts_toward_the_cap(self):
        cap = dashboard.scan.MAX_ACTIVE
        self.assertEqual(dashboard.start_holds(self.model(active=cap - 2, untracked=1)), [])
        self.assertEqual(len(dashboard.start_holds(self.model(active=cap - 2, untracked=2))), 1)

    def test_an_item_started_after_the_snapshot_counts(self):
        cap = dashboard.scan.MAX_ACTIVE
        items = {"owner/repo#1": {"added": 1000}, "owner/repo#2": {"added": 999}}
        model = self.model(active=cap - 1, items=items)
        self.assertEqual(len(dashboard.start_holds(model)), 1)

    def test_wind_down_and_the_cap_both_hold(self):
        self.wind.return_value = "yes"
        cap = dashboard.scan.MAX_ACTIVE
        holds = dashboard.start_holds(self.model(active=cap))
        self.assertEqual([name for name, _ in holds], ["wind-down", "cap"])

    def test_go_over_the_cap_is_held_until_confirmed_and_then_marked(self):
        cap = dashboard.scan.MAX_ACTIVE
        model = self.model(active=cap)
        with mock.patch.object(dashboard, "to_orch", return_value="queued") as queue:
            held = dashboard.go_orch(model, "owner/repo#3")
            self.assertTrue(held.kitchen)
            queue.assert_not_called()
            dashboard.go_orch(model, "owner/repo#3", confirmed=True)
        queue.assert_called_once_with("go owner/repo#3 (confirmed over cap)")

    def test_both_holds_send_both_markers_on_one_confirmation(self):
        self.wind.return_value = "yes"
        cap = dashboard.scan.MAX_ACTIVE
        with mock.patch.object(dashboard, "to_orch", return_value="queued") as queue:
            dashboard.investigate_orch(self.model(active=cap), "owner/repo",
                                       "why?", False, confirmed=True)
        queue.assert_called_once_with(
            "/investigate owner/repo why? (confirmed past wind-down) (confirmed over cap)")

    def test_the_row_prompt_names_both_holds(self):
        self.wind.return_value = "yes"
        cap = dashboard.scan.MAX_ACTIVE
        prompt = dashboard.row_confirm(self.model(active=cap), "go", {"key": "owner/repo#3"})
        self.assertIn("kitchen closed", prompt)
        self.assertIn("at the cap", prompt)
        self.assertIn(" · ", prompt)

    def test_confirm_carries_only_the_holds_that_were_shown(self):
        self.wind.return_value = "yes"
        cap = dashboard.scan.MAX_ACTIVE
        model = self.model(active=cap)
        with mock.patch.object(dashboard, "to_orch", return_value="queued") as queue:
            # The alert showed the cap; wind-down came up since: ask again.
            again = dashboard.go_orch(model, "owner/repo#3", confirmed=("cap",))
            self.assertTrue(again.kitchen)
            self.assertEqual(again.holds, ("wind-down", "cap"))
            queue.assert_not_called()
            dashboard.go_orch(model, "owner/repo#3", confirmed=again.holds)
        queue.assert_called_once_with(
            "go owner/repo#3 (confirmed past wind-down) (confirmed over cap)")

    def test_a_shown_hold_that_no_longer_applies_is_not_marked(self):
        cap = dashboard.scan.MAX_ACTIVE
        with mock.patch.object(dashboard, "to_orch", return_value="queued") as queue:
            dashboard.go_orch(self.model(active=cap), "owner/repo#3",
                              confirmed=("wind-down", "cap"))
        queue.assert_called_once_with("go owner/repo#3 (confirmed over cap)")

    def test_the_alert_confirms_what_it_showed(self):
        self.wind.return_value = "yes"
        cap = dashboard.scan.MAX_ACTIVE
        view = dashboard.Dashboard.__new__(dashboard.Dashboard)
        view.m = self.model(active=cap)
        view.flash = ("", 0)
        # The alert opened for the cap alone, long enough ago to be past the
        # grace. A wind-down that came up since must not be sent unseen.
        view.alert = ("owner/repo#3", "a title", dashboard.KITCHEN_SECS,
                      "⚠ At the cap", ("cap",))
        queued = dashboard.Result(True, "queued")
        with mock.patch.object(dashboard, "to_orch", return_value=queued) as queue:
            view.handle("g", None, None)
            queue.assert_not_called()
            self.assertEqual(view.alert[4], ("wind-down", "cap"))
            # Past the grace again: the second press sends both markers.
            view.alert = view.alert[:2] + (dashboard.KITCHEN_SECS,) + view.alert[3:]
            view.handle("g", None, None)
        queue.assert_called_once_with(
            "go owner/repo#3 (confirmed past wind-down) (confirmed over cap)")

    def test_run_action_confirms_the_shown_holds_only(self):
        self.wind.return_value = "yes"
        cap = dashboard.scan.MAX_ACTIVE
        args = {"repo": "owner/repo", "question": "why?", "confirmed": "1",
                "holds": "wind-down"}
        with mock.patch.object(dashboard, "to_orch", return_value="queued") as queue:
            res = dashboard.run_action(self.model(active=cap), "investigate", None, args)
        self.assertTrue(res.kitchen)
        self.assertEqual(res.holds, ("wind-down", "cap"))
        queue.assert_not_called()

    def test_confirmed_holds_reads_the_args(self):
        self.assertFalse(dashboard.confirmed_holds({}))
        self.assertFalse(dashboard.confirmed_holds({"holds": "cap"}))
        self.assertTrue(dashboard.confirmed_holds({"confirmed": "1"}))
        self.assertEqual(dashboard.confirmed_holds(
            {"confirmed": "1", "holds": "wind-down,cap"}), ["wind-down", "cap"])
        self.assertEqual(dashboard.confirmed_holds({"confirmed": "1", "holds": ""}), [])


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(dashboard is None, "dashboard needs rich")
class DrainKickTest(unittest.TestCase):
    def test_a_request_or_report_wakes_the_drain_and_other_events_do_not(self):
        with tempfile.TemporaryDirectory() as d:
            events = os.path.join(d, "events.jsonl")
            with open(events, "w") as fh:
                fh.write(json.dumps({"kind": "tick"}) + "\n")
            paths = {"EVENTS_FILE": events,
                     "SNAPSHOT_FILE": os.path.join(d, "snapshot.json"),
                     "STATE_FILE": os.path.join(d, "state.json")}
            with contextlib.ExitStack() as stack:
                for name, value in paths.items():
                    stack.enter_context(mock.patch.object(dashboard, name, value))
                model = dashboard.Model(None)
                model.poll_files()
                with open(events, "a") as fh:
                    fh.write(json.dumps({"kind": "sent"}) + "\n")
                model.poll_files()
                self.assertFalse(model.drain_kick.is_set())
                with open(events, "a") as fh:
                    fh.write(json.dumps({"kind": "request", "text": "go 3"}) + "\n")
                model.poll_files()
                self.assertTrue(model.drain_kick.is_set())
