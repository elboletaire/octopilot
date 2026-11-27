"""The dashboard's GitHub pass refreshes snapshot.json: a tick where the engine
may act, else a plain snapshot."""
import contextlib
import json
import os
import shutil
import tempfile
import time
import types
import unittest
from unittest import mock

from tests import fake  # noqa: F401  (sets the scratch environment)
import engine

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

    def test_engine_allowed_refreshes_with_a_tick(self):
        with mock.patch.object(dashboard.scan, "engine_allowed", return_value=True):
            self.model.github_pass(False)
        self.model.refresh_snapshot.assert_called_once_with(False, "tick")

    def test_engine_not_allowed_refreshes_with_a_snapshot(self):
        with mock.patch.object(dashboard.scan, "engine_allowed", return_value=False):
            self.model.github_pass(True)
        self.model.refresh_snapshot.assert_called_once_with(True, "snapshot")

    def test_nothing_rings_an_orchestrator(self):
        self.assertFalse(hasattr(dashboard.Model, "wake_orchestrator"))


@unittest.skipIf(dashboard is None, "dashboard needs rich")
class OpenPrompterTest(unittest.TestCase):
    def test_no_prompter_running_is_refused_without_the_orchestrator(self):
        with mock.patch.object(dashboard.scan, "OCTOPILOT_BACKEND", "herdr"), \
                mock.patch.object(dashboard.scan, "herdr", return_value=(1, "", "")):
            res = dashboard.open_prompter()
        self.assertEqual(res, dashboard.Result(False, "no prompter running"))
        self.assertFalse(hasattr(dashboard.scan, "orchestrator_target"))


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

    def test_activity_shows_why_a_request_was_refused(self):
        ev = {"kind": "go-refused", "key": "owner/repo#8", "verb": "go",
              "reason": "issue is closed"}
        self.assertEqual(dashboard.event_detail(ev), "issue is closed")
        ev = {"kind": "request-unknown", "text": "hello there"}
        self.assertEqual(dashboard.event_detail(ev), "hello there")
        ev = {"kind": "request-failed", "verb": "no", "error": "RuntimeError: boom"}
        self.assertEqual(dashboard.event_detail(ev), "RuntimeError: boom")


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
        with mock.patch.object(dashboard, "queue_request", return_value="queued") as queue:
            held = dashboard.go_request(model, "owner/repo#3")
            self.assertTrue(held.kitchen)
            queue.assert_not_called()
            dashboard.go_request(model, "owner/repo#3", confirmed=True)
        queue.assert_called_once_with("go owner/repo#3 (confirmed over cap)")

    def test_both_holds_send_both_markers_on_one_confirmation(self):
        self.wind.return_value = "yes"
        cap = dashboard.scan.MAX_ACTIVE
        with mock.patch.object(dashboard, "queue_request", return_value="queued") as queue:
            dashboard.investigate_request(self.model(active=cap), "owner/repo",
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
        with mock.patch.object(dashboard, "queue_request", return_value="queued") as queue:
            # The alert showed the cap; wind-down came up since: ask again.
            again = dashboard.go_request(model, "owner/repo#3", confirmed=("cap",))
            self.assertTrue(again.kitchen)
            self.assertEqual(again.holds, ("wind-down", "cap"))
            queue.assert_not_called()
            dashboard.go_request(model, "owner/repo#3", confirmed=again.holds)
        queue.assert_called_once_with(
            "go owner/repo#3 (confirmed past wind-down) (confirmed over cap)")

    def test_a_shown_hold_that_no_longer_applies_is_not_marked(self):
        cap = dashboard.scan.MAX_ACTIVE
        with mock.patch.object(dashboard, "queue_request", return_value="queued") as queue:
            dashboard.go_request(self.model(active=cap), "owner/repo#3",
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
        with mock.patch.object(dashboard, "queue_request", return_value=queued) as queue:
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
        with mock.patch.object(dashboard, "queue_request", return_value="queued") as queue:
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
    def test_a_request_wakes_the_drain_and_other_events_do_not(self):
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
                    fh.write(json.dumps({"kind": "report"}) + "\n")
                model.poll_files()
                self.assertFalse(model.drain_kick.is_set())
                with open(events, "a") as fh:
                    fh.write(json.dumps({"kind": "request", "text": "go 3"}) + "\n")
                model.poll_files()
                self.assertTrue(model.drain_kick.is_set())


@unittest.skipIf(dashboard is None, "dashboard needs rich")
class HoldMarkerTest(unittest.TestCase):
    def test_the_wind_down_hold_asks_for_the_engine_marker(self):
        self.assertEqual(dashboard.HOLDS["wind-down"][0], engine.CONFIRMED_PAST)
        self.assertEqual(dashboard.HOLDS["cap"][0], engine.CONFIRMED_OVER)


@unittest.skipIf(dashboard is None, "dashboard needs rich")
class EngineStatusTest(unittest.TestCase):
    """The header's engine state: the GitHub pass and a request's drain."""

    NOW = 1100

    def setUp(self):
        mode = mock.patch.object(dashboard.scan, "engine_mode", return_value="on")
        self.mode = mode.start()
        self.addCleanup(mode.stop)

    def model(self, **attrs):
        model = dashboard.Model(None)
        for name, value in attrs.items():
            setattr(model, name, value)
        return model

    def test_off_wherever_the_engine_is_not_on(self):
        self.mode.return_value = "off"
        status = self.model().engine_status(self.NOW)
        self.assertEqual((status["mode"], status["state"], status["since_s"]),
                         ("off", "off", None))

    def test_plan_is_reported_as_plan(self):
        self.mode.return_value = "plan"
        self.assertEqual(self.model().engine_status(self.NOW)["mode"], "plan")

    def test_idle_when_nothing_runs(self):
        status = self.model().engine_status(self.NOW)
        self.assertEqual((status["state"], status["since_s"]), ("idle", None))

    def test_scanning_while_the_pass_has_not_written_yet(self):
        model = self.model(snap_running=True, snap_started=1000,
                           snap={"generated_at": 990})
        status = model.engine_status(self.NOW)
        self.assertEqual((status["state"], status["since_s"]), ("scanning", 100))

    def test_acting_once_the_pass_has_written_its_snapshot(self):
        model = self.model(snap_running=True, snap_started=1000,
                           snap={"generated_at": 1005})
        status = model.engine_status(self.NOW)
        self.assertEqual((status["state"], status["since_s"]), ("acting", 100))

    def test_acting_while_a_drain_runs(self):
        model = self.model(drain_running=True, drain_started=1050)
        status = model.engine_status(self.NOW)
        self.assertEqual((status["state"], status["since_s"]), ("acting", 50))

    def test_stale_once_a_pass_runs_past_the_limit(self):
        started = self.NOW - dashboard.TICK_STALE_SECS - 1
        model = self.model(snap_running=True, snap_started=started,
                           snap={"generated_at": 0})
        status = model.engine_status(self.NOW)
        self.assertEqual((status["state"], status["since_s"]),
                         ("stale", dashboard.TICK_STALE_SECS + 1))

    def test_acting_in_the_second_the_pass_started(self):
        model = self.model(snap_running=True, snap_started=1000.5,
                           snap={"generated_at": 1000})
        status = model.engine_status(self.NOW)
        self.assertEqual(status["state"], "acting")

    def test_stale_once_a_drain_runs_past_the_limit(self):
        started = self.NOW - dashboard.TICK_STALE_SECS - 1
        model = self.model(drain_running=True, drain_started=started)
        status = model.engine_status(self.NOW)
        self.assertEqual((status["state"], status["since_s"]),
                         ("stale", dashboard.TICK_STALE_SECS + 1))

    def test_the_last_lines_pass_through(self):
        last = {"at": 1000, "verb": "drain", "lines": ["go 3 started"]}
        self.assertEqual(self.model(engine_last=last).engine_status(self.NOW)["last"],
                         last)


@unittest.skipIf(dashboard is None, "dashboard needs rich")
class EngineOutputTest(unittest.TestCase):
    def test_a_tick_keeps_its_lines_and_logs_each_but_the_busy_note(self):
        model = dashboard.Model(None)
        logged = []
        model.log_engine = logged.append
        model.engine_output("tick", "engine busy: another run acts\ngo 3 started\nno 4 done")
        self.assertEqual(model.engine_last["lines"], ["go 3 started", "no 4 done"])
        self.assertEqual(model.engine_last["verb"], "tick")
        self.assertEqual(logged, ["go 3 started", "no 4 done"])

    def test_a_busy_only_run_keeps_nothing(self):
        model = dashboard.Model(None)
        model.engine_output("drain", "engine busy: the drain retries later")
        self.assertIsNone(model.engine_last)

    def test_a_tick_pass_captures_its_output(self):
        model = dashboard.Model(None)
        with mock.patch.object(dashboard, "sh", return_value=(0, "go 3 started", "")):
            model.refresh_snapshot(force=True, verb="tick")
        self.assertEqual(model.engine_last["lines"], ["go 3 started"])


@unittest.skipIf(dashboard is None, "dashboard needs rich")
class EngineViewTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        paths = {"EVENTS_FILE": os.path.join(d, "events.jsonl"),
                 "SNAPSHOT_FILE": os.path.join(d, "snapshot.json"),
                 "STATE_FILE": os.path.join(d, "state.json")}
        for name, value in paths.items():
            patch = mock.patch.object(dashboard, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        mode = mock.patch.object(dashboard.scan, "engine_mode", return_value="on")
        mode.start()
        self.addCleanup(mode.stop)
        self.model = dashboard.Model(None)

    def test_the_header_carries_the_engine_and_no_orchestrator_label(self):
        view = dashboard.view_for(self.model)
        self.assertEqual(view["header"]["engine"]["state"], "idle")
        self.assertEqual(view["header"]["engine"]["mode"], "on")
        self.assertNotIn("orch_label", view["header"])
        self.assertFalse(view["pan"]["busy"])

    def test_the_pan_is_busy_while_the_engine_acts(self):
        self.model.drain_running, self.model.drain_started = True, time.time()
        view = dashboard.view_for(self.model)
        self.assertTrue(view["pan"]["busy"])

    def test_since_s_does_not_count_as_a_change(self):
        view = dashboard.view_for(self.model)
        self.assertNotIn("since_s", dashboard.stable(view)["header"]["engine"])

    def test_the_header_names_the_engine_and_its_state(self):
        view = dashboard.Dashboard.__new__(dashboard.Dashboard)
        view.m = self.model
        hdr = {"active": None, "untracked": 0, "generated_at": None,
               "last_tick": None}
        data = {"needs": [], "running": [], "orch": [], "pick": []}
        self.assertIn("engine on", view.header(data, hdr, time.time()).plain)
        self.model.drain_running, self.model.drain_started = True, time.time()
        self.assertIn("⏭ acting", view.header(data, hdr, time.time()).plain)
        self.model.snap_running, self.model.snap_started = True, 1
        self.assertIn("hung?", view.header(data, hdr, time.time()).plain)


@unittest.skipIf(dashboard is None, "dashboard needs rich")
class EngineReactionTest(unittest.TestCase):
    def test_a_skipped_budget_names_what_was_cut(self):
        ev = {"kind": "engine-skip", "key": "budget", "reason": "act budget spent",
              "left": "go owner/repo#3, no owner/repo#4"}
        self.assertIn("go owner/repo#3, no owner/repo#4", dashboard.event_detail(ev))
        r = dashboard.kitchen_reaction(ev)
        self.assertEqual(r["text"], "skipped: go owner/repo#3, no owner/repo#4")
        self.assertEqual(r["tone"], "bad")

    def test_a_skip_with_nothing_left_gives_the_reason(self):
        ev = {"kind": "engine-skip", "key": "budget", "reason": "act budget spent"}
        self.assertEqual(dashboard.kitchen_reaction(ev)["text"],
                         "skipped: act budget spent")

    def test_a_hold_says_what_held_it(self):
        ev = {"kind": "held", "key": "owner/repo#3", "reason": "at the cap"}
        r = dashboard.kitchen_reaction(ev)
        self.assertEqual(r["text"], "held repo#3: at the cap")
        self.assertEqual(r["tone"], "info")

    def test_a_refused_go_or_request_is_bad_with_its_reason(self):
        ev = {"kind": "go-refused", "key": "owner/repo#8", "verb": "go",
              "reason": "issue is closed"}
        r = dashboard.kitchen_reaction(ev)
        self.assertEqual(r["text"], "go repo#8 refused: issue is closed")
        self.assertEqual(r["tone"], "bad")
        ev = {"kind": "request-refused", "verb": "no", "reason": "nothing tracked"}
        r = dashboard.kitchen_reaction(ev)
        self.assertEqual(r["text"], "no refused: nothing tracked")
        self.assertEqual(r["tone"], "bad")

    def test_a_hold_without_a_key_is_a_request(self):
        ev = {"kind": "held", "reason": "kitchen closed"}
        self.assertEqual(dashboard.kitchen_reaction(ev)["text"],
                         "held request: kitchen closed")

    def test_a_started_go_is_good(self):
        ev = {"kind": "go", "key": "owner/repo#3", "title": "Fix the thing"}
        r = dashboard.kitchen_reaction(ev)
        self.assertEqual(r["text"], "started repo#3 Fix the thing")
        self.assertEqual(r["tone"], "good")
