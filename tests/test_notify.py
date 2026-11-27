"""Desktop notifications: once per (item, cause), the v2 notified.json, its
silent first run and pruning, and the texts NOTIFY_WHY gives each reason."""
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

from tests import fake  # noqa: F401  (sets the scratch environment)
import scan

try:
    import dashboard
except ImportError:  # the rich package is the dashboard's own dependency
    dashboard = None

NOW = 1732530000.0


def card(key, cause, why="READY", note="", engine=None):
    return {"key": key, "cause": cause, "why": why, "title": key,
            "note": note, "engine": engine}


@unittest.skipIf(dashboard is None, "dashboard needs rich")
class NotifyBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, True))
        self.file = os.path.join(tmp, "notified.json")
        patches = [
            mock.patch.object(dashboard, "NOTIFIED_FILE", self.file),
            mock.patch.object(dashboard, "NOTIFIED_LOCK", self.file + ".lock"),
            mock.patch.object(dashboard.shutil, "which",
                              return_value="/usr/bin/notify-send"),
            mock.patch.object(dashboard, "desktop"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.desktop = dashboard.desktop
        self.model = dashboard.Model.__new__(dashboard.Model)
        self.model.lock = threading.Lock()
        self.model.snap = {"generated_at": NOW, "rows": []}
        self.model.state = {"items": {"o/r#1": {}, "o/r#2": {}}}
        self.model.needs_before = set()

    def run_notify(self, *needs):
        self.model.notify({"needs": list(needs)})

    def saved(self):
        with open(self.file) as fh:
            return json.load(fh)

    def write_file(self, doc):
        with open(self.file, "w") as fh:
            json.dump(doc, fh)

@unittest.skipIf(dashboard is None, "dashboard needs rich")
class NotifyTest(NotifyBase):
    def test_first_run_is_silent_and_records_every_cause(self):
        self.run_notify(card("o/r#1", "READY:abc"))
        self.desktop.assert_not_called()
        self.assertEqual(self.saved(), {"v": 2, "needs": ["o/r#1"],
                                        "seen": {"o/r#1": ["READY:abc"]}})

    def test_once_per_cause_and_a_new_cause_notifies_again(self):
        self.run_notify(card("o/r#1", "READY:abc"))
        self.run_notify(card("o/r#1", "READY:abc"))
        self.desktop.assert_not_called()
        self.run_notify(card("o/r#1", "READY:def"))
        self.assertEqual(self.desktop.call_count, 1)
        self.run_notify(card("o/r#1", "READY:def"))
        self.assertEqual(self.desktop.call_count, 1)

    def test_a_bounce_with_the_same_cause_does_not_notify(self):
        self.run_notify(card("o/r#1", "CAPPED"))
        self.run_notify(card("o/r#1", "CAPPED"))
        self.run_notify()  # left Needs you
        self.run_notify(card("o/r#1", "CAPPED"))  # came back, same cause
        self.desktop.assert_not_called()

    def test_an_old_format_file_is_a_silent_first_run(self):
        self.write_file({"o/r#1": "READY"})
        self.run_notify(card("o/r#1", "READY:abc"), card("o/r#2", "CAPPED"))
        self.desktop.assert_not_called()
        self.assertEqual(self.saved()["v"], 2)

    def test_a_pruned_item_is_forgotten(self):
        self.write_file({"v": 2, "seen": {"o/r#1": ["CAPPED"],
                                          "gone#9": ["READY:x"]}})
        self.run_notify(card("o/r#1", "CAPPED"))
        self.assertEqual(self.saved()["seen"], {"o/r#1": ["CAPPED"]})
        self.desktop.assert_not_called()

    def test_a_notification_says_what_the_card_needs(self):
        self.write_file({"v": 2, "seen": {}})
        self.run_notify(card("o/r#1", "READY:abc"),
                        card("o/r#2", "report:blocked:1", why="blocked",
                             note="blocked: tests"))
        titles = [c.args[0] for c in self.desktop.call_args_list]
        self.assertEqual(titles, ["🔴 r#1: ready to merge",
                                  "🔴 r#2: agent is blocked"])


def placed(it, kind="WORKING", ag=None, stalled=False, head=None):
    """A card as build() would hand notify(): its cause from column_of."""
    row = {"kind": kind, "key": "o/r#1", "head": head}
    ctx = {"live": {}, "sends": [], "paused": False, "stalled": stalled}
    section, extras = dashboard.column_of(row, it, ag or {}, ctx)
    assert section == "needs", section
    return card("o/r#1", extras["cause"], why=extras["why"])


def episode_item(last_seen, **kw):
    base = {"session": "tb1", "added": NOW - 3600, "last_seen": last_seen}
    base.update(kw)
    return base


@unittest.skipIf(dashboard is None, "dashboard needs rich")
class EpisodeTest(NotifyBase):
    """Causes without a report keep their episode: a second question or a
    second stall is a new cause, and notifies, while a poll is not."""

    def test_asks_answered_asks_again_notifies_twice(self):
        waiting = {"tb1": {"state": "waiting"}}
        self.run_notify(placed(episode_item(NOW - 600), ag=waiting["tb1"]))
        self.run_notify(placed(episode_item(NOW - 600), ag=waiting["tb1"]))
        self.desktop.assert_not_called()
        self.run_notify()  # answered: the card leaves Needs you
        self.run_notify(placed(episode_item(NOW - 60), ag=waiting["tb1"]))
        self.assertEqual(self.desktop.call_count, 1)
        self.assertEqual(self.desktop.call_args.args[0],
                         "🔴 r#1: agent is asking something")

    def test_stalls_twice_notify_twice(self):
        self.write_file({"v": 2, "seen": {}})
        self.run_notify(placed(episode_item(NOW - 3000), stalled=True))
        self.run_notify()
        self.run_notify(placed(episode_item(NOW - 900), stalled=True))
        self.assertEqual(self.desktop.call_count, 2)

    def test_capped_again_after_a_new_head_notifies_again(self):
        self.write_file({"v": 2, "seen": {}})
        self.run_notify(placed(episode_item(NOW - 60), kind="CAPPED",
                               head="aaa1111"))
        self.run_notify(placed(episode_item(NOW - 60), kind="CAPPED",
                               head="aaa1111"))
        self.assertEqual(self.desktop.call_count, 1)
        self.run_notify(placed(episode_item(NOW - 60), kind="CAPPED",
                               head="bbb2222"))
        self.assertEqual(self.desktop.call_count, 2)

    def test_a_wait_whose_last_seen_moves_notifies_once(self):
        """A heartbeat mid-wait moves the asking cause, not the question."""
        self.write_file({"v": 2, "seen": {}})
        self.run_notify(placed(episode_item(NOW - 600), ag={"state": "waiting"}))
        self.run_notify(placed(episode_item(NOW - 300), ag={"state": "waiting"}))
        self.assertEqual(self.desktop.call_count, 1)
        self.assertEqual(self.saved()["seen"]["o/r#1"],
                         [f"asking:{NOW - 300}"])
        self.run_notify()  # answered
        self.run_notify(placed(episode_item(NOW - 60), ag={"state": "waiting"}))
        self.assertEqual(self.desktop.call_count, 2)

    def test_a_second_hold_and_wake_at_the_same_head_notifies(self):
        self.write_file({"v": 2, "seen": {}})

        def woken(since):
            it = episode_item(NOW - 60, woke={"reason": "approved", "kind": None,
                                             "since": since})
            row = {"kind": "READY", "key": "o/r#1", "head": "abc",
                   "note": "you merge; back from hold: approved"}
            section, extras = dashboard.column_of(row, it, {},
                {"live": {}, "sends": [], "paused": False, "stalled": False})
            assert section == "needs", section
            return card("o/r#1", extras["cause"], why=extras["why"])

        self.run_notify(woken(NOW - 900))
        self.run_notify()
        self.run_notify(woken(NOW - 100))
        self.assertEqual(self.desktop.call_count, 2)
        self.run_notify()
        self.run_notify(woken(NOW - 100))  # the same wake again: quiet
        self.assertEqual(self.desktop.call_count, 2)

    def test_a_restart_mid_wait_does_not_notify_the_same_wait(self):
        self.write_file({"v": 2, "needs": ["o/r#1"],
                         "seen": {"o/r#1": [f"asking:{NOW - 600}"]}})
        self.model.needs_before = set()  # a fresh dashboard
        self.run_notify(placed(episode_item(NOW - 300), ag={"state": "waiting"}))
        self.desktop.assert_not_called()
        self.assertEqual(self.saved()["seen"]["o/r#1"], [f"asking:{NOW - 300}"])

    def test_a_stall_without_last_seen_is_stamped_by_added(self):
        it = episode_item(None, added=NOW - 3000)
        self.assertEqual(placed(it, stalled=True)["cause"],
                         f"stalled:{NOW - 3000}")

    def test_an_empty_read_neither_prunes_nor_writes(self):
        self.write_file({"v": 2, "seen": {"o/r#1": ["READY:abc"]}})
        before = self.saved()
        self.model.state = {"items": {}}
        self.run_notify(card("o/r#1", "READY:abc"))
        self.assertEqual(self.saved(), before)
        self.desktop.assert_not_called()


@unittest.skipIf(dashboard is None, "dashboard needs rich")
class WakeStampTest(unittest.TestCase):
    def test_a_wake_keeps_the_holds_since(self):
        it = {"hold": {"by": "you", "reason": "", "since": NOW - 900}}
        scan.wake_item(it, "approved")
        self.assertNotIn("hold", it)
        self.assertEqual(it["woke"]["since"], NOW - 900)


@unittest.skipIf(dashboard is None, "dashboard needs rich")
class ViewWhyTest(unittest.TestCase):
    def test_the_view_says_why_in_words(self):
        m = dashboard.Model.__new__(dashboard.Model)
        m.state = {"items": {}}
        e = {"key": "o/r#1", "kind": "READY", "title": "t", "draft": False,
             "url": None, "checks": "pass", "agent": None, "rounds": 1,
             "driver": "agent", "quiet": 60, "stalled": False, "needs": True,
             "why": "READY", "note": "", "pick_kind": "your PR",
             "session": None, "space": None, "branch": "b", "pr": 1,
             "stack": 0, "pane": None, "path": None, "open": False,
             "engine": None, "cause": "READY:abc", "report": None,
             "hold": None}
        view = dashboard.entry_view(m, e, "needs")
        self.assertEqual(view["why"], "ready to merge")


@unittest.skipIf(dashboard is None, "dashboard needs rich")
class NotifyTextTest(unittest.TestCase):
    def text(self, why, **kw):
        return dashboard.notify_text(dict(card("o/r#1", "c", why=why), **kw))

    def test_every_needs_kind_has_a_text(self):
        for kind in dashboard.NEEDS_ROWS:
            self.assertIn(kind, dashboard.NOTIFY_WHY, kind)

    def test_blocked_states_read_as_the_report(self):
        self.assertEqual(self.text("blocked"), "agent is blocked")
        self.assertEqual(self.text("failed"), "agent reported failed")
        self.assertEqual(self.text("stalled"), "agent not reporting")
        self.assertEqual(self.text("BLOCKED"), "agent blocked, failed or stalled")

    def test_ready_read_only_is_a_report_to_read(self):
        self.assertEqual(self.text("ready"), "report to read")

    def test_a_wake_says_back_from_hold_with_its_reason(self):
        self.assertEqual(
            self.text("woke", note="READY; back from hold: waiting on schema"),
            "back from hold: waiting on schema")

    def test_an_engine_error_names_the_failed_action(self):
        self.assertEqual(
            self.text("engine_error", engine={"action": "cleanup",
                                              "label": "cleaning up"}),
            "engine: cleanup failed")

    def test_a_failed_send_names_its_reason(self):
        self.assertEqual(
            self.text("send_failed", engine={"action": "send",
                                             "label": "rebase"}),
            "engine: rebase failed")
        self.assertEqual(self.text("engine_error"), "engine failed")


if __name__ == "__main__":
    unittest.main()
