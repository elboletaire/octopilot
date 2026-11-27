"""The board's placement (dashboard.column_of, herdr-app/CONTRACT.md's
Placement), the ⏭ engine badge table, and build()'s sections."""
import threading
import unittest

from tests import fake  # noqa: F401  (sets the scratch environment)
import engine

try:
    import dashboard
except ImportError:  # the rich package is the dashboard's own dependency
    dashboard = None

NOW = 1732530000.0
IDLE = {"tb1": {"state": "idle"}}
RUNNING = {"tb1": {"state": "running"}}
WAITING = {"tb1": {"state": "waiting"}}


def item(**kw):
    base = {"session": "tb1", "mode": "fix", "branch": "feat/x",
            "added": NOW - 3600, "last_seen": NOW - 60}
    base.update(kw)
    return base


def place(kind, it, live=IDLE, sends=(), paused=False, stalled=False):
    ctx = {"live": live, "sends": list(sends), "paused": paused,
           "stalled": stalled}
    ag = live.get(it.get("session"), {})
    return dashboard.column_of({"kind": kind, "key": "o/r#1"}, it, ag, ctx)


def section_of(kind, it, **kw):
    return place(kind, it, **kw)[0]


@unittest.skipIf(dashboard is None, "dashboard needs rich")
class PlacementTest(unittest.TestCase):
    def test_1_merged_and_restack_run_with_their_badge(self):
        self.assertEqual(place("MERGED", item()),
                         ("running", {"engine": {"action": "cleanup",
                                                 "label": "cleaning up",
                                                 "paused": False},
                                      "cause": None, "report": None}))
        self.assertEqual(place("RESTACK", item())[1]["engine"]["action"],
                         "restack")

    def test_2_gone_is_done_and_closed_needs_you(self):
        self.assertEqual(section_of("GONE", item()), "done")
        self.assertEqual(section_of("CLOSED", item()), "needs")

    def test_3_engine_error_needs_you_with_a_cause(self):
        it = item(engine_error="cleanup failed: no worktree")
        section, extras = place("WORKING", it)
        self.assertEqual(section, "needs")
        self.assertEqual(extras["cause"],
                         "engine-error:cleanup failed: no worktree")

    def test_3_failed_queued_send_needs_you_and_badges_the_send(self):
        send = {"key": "o/r#1", "reason": "rebase", "at": NOW - 5,
                "error": engine.FAILED}
        section, extras = place("READY", item(), sends=[send])
        self.assertEqual(section, "needs")
        self.assertEqual(extras["engine"], {"action": "send", "label": "rebase",
                                            "paused": False})
        self.assertEqual(extras["cause"], f"send:rebase:{NOW - 5}")

    def test_3_blocked_send_is_not_failed(self):
        send = {"key": "o/r#1", "reason": "feedback", "at": NOW,
                "error": engine.BLOCKED}
        section, extras = place("WORKING", item(), sends=[send])
        self.assertEqual(section, "running")
        self.assertEqual(extras["engine"]["label"], "feedback")

    def test_4_agent_waiting_needs_you_whatever_the_kind(self):
        self.assertEqual(section_of("WORKING", item(), live=WAITING), "needs")
        self.assertEqual(section_of("DONE", item(), live=WAITING), "needs")

    def test_4_engine_kinds_waiting_need_you_and_keep_their_badge(self):
        section, extras = place("REVIEW", item(), live=WAITING)
        self.assertEqual(section, "needs")
        self.assertEqual(extras["engine"]["action"], "review")

    def test_5_engine_kinds_run_with_their_badge(self):
        for kind, action in (("REVIEW", "review"), ("BOOTING", "boot"),
                             ("FEEDBACK", "feedback")):
            section, extras = place(kind, item())
            self.assertEqual(section, "running", kind)
            self.assertEqual(extras["engine"]["action"], action, kind)

    def test_6_running_agent_runs_whatever_the_row_says(self):
        self.assertEqual(section_of("WORKING", item(), live=RUNNING), "running")
        self.assertEqual(section_of("CAPPED", item(), live=RUNNING), "running")
        self.assertIsNone(place("WORKING", item(), live=RUNNING)[1]["engine"])

    def test_7_held_by_you_is_on_hold(self):
        it = item(hold={"by": "you", "reason": "", "since": NOW - 300})
        self.assertEqual(section_of("DONE", it), "hold")

    def test_7_newer_needs_report_wakes_a_hold(self):
        it = item(hold={"by": "you", "reason": "", "since": NOW - 300},
                  last_report={"state": "blocked", "at": NOW - 30,
                               "msg": "stuck"})
        section, extras = place("WORKING", it)
        self.assertEqual(section, "needs")
        self.assertEqual(extras["report"], "blocked")

    def test_8_live_blocked_failed_stalled_report_needs_you(self):
        for state in ("blocked", "failed", "stalled"):
            it = item(last_report={"state": state, "at": NOW - 30})
            section, extras = place("WORKING", it)
            self.assertEqual(section, "needs", state)
            self.assertEqual(extras["report"], state)
            self.assertEqual(extras["cause"], f"report:{state}:{NOW - 30}")

    def test_8_live_capped_report_needs_you_with_no_report_state(self):
        it = item(last_report={"state": "capped", "at": NOW - 30})
        section, extras = place("WORKING", it)
        self.assertEqual((section, extras["report"]), ("needs", None))

    def test_8_live_held_report_is_on_hold(self):
        it = item(last_report={"state": "held", "at": NOW - 30, "msg": "wait"})
        section, extras = place("WORKING", it)
        self.assertEqual((section, extras["report"]), ("hold", "held"))

    def test_8_read_only_ready_needs_you_until_acked_then_done(self):
        rep = {"state": "ready", "at": NOW - 30}
        it = item(read_only=True, last_report=rep)
        self.assertEqual(section_of("READY", it), "needs")
        it = item(read_only=True, last_report=rep, report_acked=NOW - 30)
        self.assertEqual(section_of("DONE", it), "done")

    def test_8_a_report_the_agent_outlived_is_history(self):
        it = item(last_report={"state": "blocked", "at": NOW - 120})
        self.assertEqual(section_of("WORKING", it), "running")

    def test_9_needs_rows(self):
        for kind in ("CAPPED", "PUSHED", "STALLED", "READY", "BLOCKED"):
            self.assertEqual(section_of(kind, item()), "needs", kind)

    def test_9_hold_and_done_rows(self):
        self.assertEqual(section_of("HOLD", item()), "hold")
        self.assertEqual(section_of("DONE", item()), "done")

    def test_10_live_stalled_needs_you_otherwise_running(self):
        self.assertEqual(place("WORKING", item(), stalled=True)[0], "needs")
        self.assertEqual(section_of("WORKING", item()), "running")
        self.assertEqual(section_of("NEW", item()), "running")


@unittest.skipIf(dashboard is None, "dashboard needs rich")
class EngineBadgeTest(unittest.TestCase):
    def badge(self, kind, sends=(), paused=False):
        return dashboard.engine_badge(kind, list(sends), paused)

    def test_the_table(self):
        for kind, action, label in (
                ("MERGED", "cleanup", "cleaning up"),
                ("RESTACK", "restack", "restacking"),
                ("REVIEW", "review", "starting review"),
                ("BOOTING", "boot", "sending review"),
                ("FEEDBACK", "feedback", "sending feedback")):
            self.assertEqual(self.badge(kind),
                             {"action": action, "label": label,
                              "paused": False}, kind)

    def test_a_queued_send_is_labelled_by_its_reason(self):
        send = {"reason": "rebase", "error": None}
        self.assertEqual(self.badge("WORKING", [send]),
                         {"action": "send", "label": "rebase",
                          "paused": False})

    def test_the_row_action_wins_over_a_queued_send(self):
        self.assertEqual(self.badge("MERGED", [{"reason": "rebase"}])["action"],
                         "cleanup")

    def test_paused_is_carried(self):
        self.assertTrue(self.badge("MERGED", paused=True)["paused"])

    def test_nothing_pending_is_none(self):
        self.assertIsNone(self.badge("WORKING"))
        self.assertIsNone(self.badge("DONE"))


@unittest.skipIf(dashboard is None, "dashboard needs rich")
class BuildSectionsTest(unittest.TestCase):
    def model(self, rows, items, live=None, pending=()):
        m = dashboard.Model.__new__(dashboard.Model)
        m.lock = threading.Lock()
        m.snap = {"generated_at": NOW, "rows": rows, "active": 0,
                  "max_active": 6, "untracked": 0}
        m.state = {"items": items, "me": "me", "pending_sends": list(pending)}
        m.live = live or IDLE
        m.sessions = {}
        return m

    def test_sections_and_the_fields_the_entries_carry(self):
        def row(kind, key, **kw):
            return dict({"kind": kind, "key": key, "title": key}, **kw)

        rows = [row("MERGED", "o/r#1"), row("DONE", "o/r#2"),
                row("HOLD", "o/r#3", hold={"by": "reviewer",
                                            "reason": "waiting on review"}),
                row("WORKING", "o/r#4"), row("READY", "o/r#5")]
        items = {"o/r#1": item(session="tb1"), "o/r#2": item(session="tb2"),
                 "o/r#3": item(session="tb3"),
                 "o/r#4": item(session="tb4",
                               hold={"by": "you", "reason": "schema",
                                     "since": NOW - 300}),
                 "o/r#5": item(session="tb5"),
                 "o/r#6": item(session="tb6")}
        pending = [{"key": "o/r#5", "reason": "rebase", "at": NOW,
                    "error": engine.FAILED}]
        data, hdr = self.model(rows, items, pending=pending).build()
        self.assertEqual(set(data) - {"notes", "pick"},
                         {"running", "needs", "hold", "done"})
        placed = {sec: sorted(e["key"] for e in data[sec])
                  for sec in ("running", "needs", "hold", "done")}
        self.assertEqual(placed, {"running": ["o/r#1", "o/r#6"],
                                  "needs": ["o/r#5"],
                                  "hold": ["o/r#3", "o/r#4"],
                                  "done": ["o/r#2"]})
        by_key = {e["key"]: e for sec in data.values() for e in sec}
        self.assertEqual(by_key["o/r#1"]["engine"]["action"], "cleanup")
        self.assertEqual(by_key["o/r#3"]["hold"],
                         {"by": "reviewer", "reason": "waiting on review"})
        self.assertEqual(by_key["o/r#4"]["hold"],
                         {"by": "you", "reason": "schema"})
        self.assertEqual(by_key["o/r#5"]["engine"]["label"], "rebase")
        self.assertTrue(by_key["o/r#5"]["needs"])
        self.assertFalse(by_key["o/r#4"]["needs"])
        self.assertEqual(hdr["paused"], False)


if __name__ == "__main__":
    unittest.main()
