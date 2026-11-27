"""The board's TUI and kitchen: the ⏭ badge on a card, the On hold count in
the header, the kitchen's pan, shelf and On hold lists by the board's
sections, and the kitchen's lines for hold, unhold and a wake."""
import threading
import unittest
from unittest import mock

from tests import fake  # noqa: F401  (sets the scratch environment)

try:
    import dashboard
    from rich.console import Console
except ImportError:  # the rich package is the dashboard's own dependency
    dashboard = None

NOW = 1732530000.0


def entry(key, engine=None, paused=False):
    return {"key": key, "kind": "WORKING", "title": "t", "draft": False,
            "agent": None, "checks": None, "rounds": 0, "driver": "agent",
            "quiet": 60, "stalled": False, "note": "a note",
            "engine": engine, "why": None}


@unittest.skipIf(dashboard is None, "dashboard needs rich")
class KitchenReactionTest(unittest.TestCase):
    def test_a_hold_is_a_zzz_line_with_its_reason(self):
        r = dashboard.kitchen_reaction({"kind": "hold", "key": "o/r#1",
                                        "reason": "waiting on the schema"})
        self.assertEqual((r["mood"], r["burst"], r["key"], r["text"]),
                         ("flip", "zzz", "o/r#1",
                          "on hold r#1: waiting on the schema"))

    def test_a_wake_with_a_reason_is_back_from_hold(self):
        r = dashboard.kitchen_reaction({"kind": "unhold", "key": "o/r#1",
                                        "reason": "approved"})
        self.assertEqual((r["mood"], r["burst"], r["tone"], r["text"]),
                         ("surprised", "bang", "good",
                          "back from hold r#1: approved"))

    def test_an_unhold_without_a_reason_is_a_plain_line(self):
        r = dashboard.kitchen_reaction({"kind": "unhold", "key": "o/r#1"})
        self.assertEqual((r["mood"], r["burst"], r["text"]),
                         ("flip", None, "unhold r#1"))

    def test_park_and_unpark_keep_their_lines(self):
        self.assertEqual(dashboard.kitchen_reaction(
            {"kind": "park", "key": "o/r#1"})["text"], "on hold r#1")
        self.assertEqual(dashboard.kitchen_reaction(
            {"kind": "unpark", "key": "o/r#1"})["text"], "unhold r#1")


@unittest.skipIf(dashboard is None, "dashboard needs rich")
class KitchenPanTest(unittest.TestCase):
    def test_the_pan_and_the_lists_follow_the_board(self):
        items = {"o/r#1": {"added": 100}, "o/r#2": {"added": 200},
                 "o/r#3": {"added": 300}, "o/r#4": {"added": 400},
                 "o/r#5": {"added": 500}}
        m = dashboard.Model.__new__(dashboard.Model)
        m.state = {"items": items}
        m.snap = None
        data = {"running": [entry("o/r#1")], "needs": [entry("o/r#2")],
                "hold": [entry("o/r#3"), entry("o/r#5")],
                "done": [entry("o/r#4")], "pick": []}
        pan = dashboard.kitchen_pan(m, {"max_active": 6}, [], NOW, False, data)
        self.assertEqual([b["key"] for b in pan["balls"]],
                         ["o/r#1", "o/r#2"])
        self.assertEqual([b["key"] for b in pan["hold"]], ["o/r#3", "o/r#5"])
        self.assertEqual(pan["hold_total"], 2)
        self.assertEqual([b["key"] for b in pan["shelf"]], ["o/r#4"])
        self.assertEqual(pan["shelf_total"], 1)

    def test_the_lists_are_the_oldest_eight(self):
        items = {f"o/r#{i}": {"added": i} for i in range(1, 11)}
        m = dashboard.Model.__new__(dashboard.Model)
        m.state = {"items": items}
        m.snap = None
        data = {"running": [], "needs": [], "pick": [],
                "hold": [entry(f"o/r#{i}") for i in range(1, 11)],
                "done": []}
        pan = dashboard.kitchen_pan(m, {"max_active": 6}, [], NOW, False, data)
        self.assertEqual(len(pan["hold"]), 8)
        self.assertEqual(pan["hold"][0]["key"], "o/r#1")
        self.assertEqual(pan["hold_total"], 10)


@unittest.skipIf(dashboard is None, "dashboard needs rich")
class CardBadgeTest(unittest.TestCase):
    def body(self, e):
        view = dashboard.Dashboard.__new__(dashboard.Dashboard)
        view.console = Console(width=80)
        return "\n".join(t.plain for t in view.card_body(e, 30))

    def test_a_pending_action_shows_its_badge(self):
        e = entry("o/r#1", {"action": "cleanup", "label": "cleaning up",
                            "paused": False})
        self.assertIn("⏭ cleaning up", self.body(e))

    def test_paused_says_held_paused(self):
        e = entry("o/r#1", {"action": "cleanup", "label": "cleaning up",
                            "paused": True})
        self.assertIn("⏭ held: paused", self.body(e))

    def test_no_badge_when_nothing_is_pending(self):
        self.assertNotIn("⏭", self.body(entry("o/r#1")))

    def test_the_next_tick_note_is_gone(self):
        m = dashboard.Model.__new__(dashboard.Model)
        m.state = {"items": {"o/r#1": {"added": 1, "last_seen": 1}}}
        e = dashboard.Model.entry(m, {"kind": "MERGED", "key": "o/r#1"},
                                  "o/r#1", m.state["items"]["o/r#1"], {}, {},
                                  NOW, None)
        self.assertNotIn("next tick", e["note"])


@unittest.skipIf(dashboard is None, "dashboard needs rich")
class HeaderHoldTest(unittest.TestCase):
    def test_the_header_counts_the_on_hold_column(self):
        view = dashboard.Dashboard.__new__(dashboard.Dashboard)
        view.console = Console(width=120)
        view.m = mock.Mock(snap_running=False, snap_err=None)
        view.m.engine_status.return_value = {"state": "idle", "mode": "on",
                                             "since_s": None, "last": None}
        hdr = {"active": None, "untracked": 0, "generated_at": None,
               "last_tick": None}
        data = {"needs": [], "running": [], "hold": [entry("o/r#1"),
                                                    entry("o/r#2")],
                "done": [], "pick": []}
        self.assertIn("💤 2 on hold", view.header(data, hdr, NOW).plain)


if __name__ == "__main__":
    unittest.main()
