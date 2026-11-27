"""Row actions: hold and unhold (park and unpark still accepted by --action),
the refusals each one gets, when ack is offered, the card's note when the
user holds it, and the events that refresh the view."""
import threading
import unittest
from unittest import mock

from tests import fake  # noqa: F401  (sets the scratch environment)

try:
    import dashboard
except ImportError:  # the rich package is the dashboard's own dependency
    dashboard = None

NOW = 1732530000.0
IDLE = {"tb1": {"state": "idle"}}


def entry(kind, key="o/r#1"):
    return {"kind": kind, "key": key, "title": key, "session": None,
            "pane": None, "path": None, "space": None}


def model(items, live=None):
    m = dashboard.Model.__new__(dashboard.Model)
    m.lock = threading.Lock()
    m.state = {"items": items}
    m.live = live or {}
    return m


def item(**kw):
    base = {"session": "tb1", "mode": "fix", "branch": "feat/x",
            "added": NOW - 3600, "last_seen": NOW - 60}
    base.update(kw)
    return base


@unittest.skipIf(dashboard is None, "dashboard needs rich")
class HoldRefusalTest(unittest.TestCase):
    def refusal(self, aid, kind, it):
        return dashboard.row_refusal(model({"o/r#1": it}), aid, entry(kind))

    def test_hold_is_offered_on_a_working_card(self):
        self.assertIsNone(self.refusal("hold", "WORKING", item()))

    def test_hold_is_refused_on_done_gone_and_engine_rows(self):
        for kind in ("DONE", "GONE", "MERGED", "RESTACK", "CLOSED", "BOOTING"):
            self.assertEqual(self.refusal("hold", kind, item()),
                             f"{kind} rows can't be held now", kind)

    def test_hold_is_refused_while_feedback_is_pending(self):
        self.assertEqual(self.refusal("hold", "FEEDBACK", item()),
                         "FEEDBACK rows can't be held: the feedback is pending")

    def test_hold_is_refused_on_a_card_already_held_by_you(self):
        it = item(hold={"by": "you", "reason": "", "since": NOW})
        self.assertEqual(self.refusal("hold", "WORKING", it),
                         "o/r#1 is already on hold")

    def test_hold_on_an_untracked_card_is_refused(self):
        self.assertEqual(dashboard.row_refusal(model({}), "hold",
                                               entry("WORKING")),
                         "d only applies to tracked items")

    def test_an_agent_hold_is_offered_hold_and_refuses_unhold_with_the_agent(self):
        it = item(last_report={"state": "held", "at": NOW - 30, "msg": "wait"})
        m = model({"o/r#1": it}, IDLE)
        self.assertIsNone(dashboard.row_refusal(m, "hold", entry("HOLD")))
        self.assertEqual(dashboard.row_refusal(m, "unhold", entry("HOLD")),
                         "the agent is holding it; message it to resume")

    def test_unhold_only_on_a_card_you_hold(self):
        self.assertEqual(self.refusal("unhold", "WORKING", item()),
                         "o/r#1 is not on hold")
        reviewer = item(hold={"by": "reviewer", "reason": "wait"})
        self.assertEqual(self.refusal("unhold", "HOLD", reviewer),
                         "o/r#1 is not on hold")
        mine = item(hold={"by": "you", "reason": "", "since": NOW})
        self.assertIsNone(self.refusal("unhold", "DONE", mine))


@unittest.skipIf(dashboard is None, "dashboard needs rich")
class AckRefusalTest(unittest.TestCase):
    READY = {"state": "ready", "at": NOW - 30}

    def refusal(self, aid, kind, it, live=IDLE):
        return dashboard.row_refusal(model({"o/r#1": it}, live), aid,
                                     entry(kind))

    def test_ack_is_offered_on_a_pushed_card(self):
        self.assertIsNone(self.refusal("ack", "PUSHED", item()))

    def test_ack_is_refused_on_a_working_card(self):
        self.assertEqual(self.refusal("ack", "WORKING", item()),
                         "x only applies to PUSHED (stale) rows and reports to read")

    def test_ack_is_offered_on_an_unread_read_only_report(self):
        it = item(read_only=True, last_report=self.READY)
        self.assertIsNone(self.refusal("ack", "READY", it))

    def test_ack_is_refused_once_the_report_is_read(self):
        it = item(read_only=True, last_report=self.READY,
                  report_acked=NOW - 30)
        self.assertIsNotNone(self.refusal("ack", "DONE", it))

    def test_rereview_only_on_a_pushed_card(self):
        self.assertIsNone(self.refusal("rereview", "PUSHED", item()))
        self.assertEqual(self.refusal("rereview", "WORKING", item()),
                         "e only applies to PUSHED (stale) rows")


@unittest.skipIf(dashboard is None, "dashboard needs rich")
class ActionIdsTest(unittest.TestCase):
    def test_hold_and_unhold_replace_park_and_unpark(self):
        self.assertIn("hold", dashboard.ROW_KEYS)
        self.assertIn("unhold", dashboard.ROW_KEYS)
        self.assertNotIn("park", dashboard.ROW_KEYS)
        self.assertNotIn("unpark", dashboard.ROW_KEYS)

    def test_park_and_unpark_still_run_hold_and_unhold(self):
        held = item(hold={"by": "you", "reason": "", "since": NOW})
        with mock.patch.object(dashboard, "find_row",
                               return_value=entry("WORKING")), \
                mock.patch.object(dashboard, "scan_run") as run:
            dashboard.run_action(model({"o/r#1": item()}), "park", "o/r#1", {})
            dashboard.run_action(model({"o/r#1": held}), "unpark", "o/r#1", {})
        self.assertEqual(run.call_args_list,
                         [mock.call(["hold", "o/r#1"]),
                          mock.call(["unhold", "o/r#1"])])

    def test_the_held_card_offers_resume_on_d(self):
        it = item(hold={"by": "you", "reason": "", "since": NOW})
        acts = {a["id"]: a for a in dashboard.row_actions(
            model({"o/r#1": it}), entry("WORKING"))}
        self.assertIn("unhold", acts)
        self.assertNotIn("hold", acts)
        self.assertEqual((acts["unhold"]["key"], acts["unhold"]["label"]),
                         ("d", "resume"))

    def test_the_card_note_says_held_with_its_reason(self):
        m = model({})
        it = item(hold={"by": "you", "reason": "waiting on the schema",
                        "since": NOW})
        e = dashboard.Model.entry(m, {"kind": "WORKING", "key": "o/r#1"},
                                  "o/r#1", it, {}, {}, NOW, None)
        self.assertEqual(e["note"], "held: waiting on the schema")
        it["hold"]["reason"] = ""
        e = dashboard.Model.entry(m, {"kind": "WORKING", "key": "o/r#1"},
                                  "o/r#1", it, {}, {}, NOW, None)
        self.assertEqual(e["note"], "held by you")

    def test_hold_and_unhold_refresh_the_view_promptly(self):
        for ev in ("hold", "unhold", "park", "unpark"):
            self.assertIn(ev, dashboard.KICK_EVENTS, ev)


if __name__ == "__main__":
    unittest.main()
