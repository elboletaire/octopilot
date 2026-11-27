"""Desktop notifications: once per (item, cause), the v2 notified.json, its
silent first run and pruning, and the texts NOTIFY_WHY gives each reason."""
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

from tests import fake  # noqa: F401  (sets the scratch environment)

try:
    import dashboard
except ImportError:  # the rich package is the dashboard's own dependency
    dashboard = None

NOW = 1732530000.0


def card(key, cause, why="READY", note="", engine=None):
    return {"key": key, "cause": cause, "why": why, "title": key,
            "note": note, "engine": engine}


@unittest.skipIf(dashboard is None, "dashboard needs rich")
class NotifyTest(unittest.TestCase):
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

    def run_notify(self, *needs):
        self.model.notify({"needs": list(needs)})

    def saved(self):
        with open(self.file) as fh:
            return json.load(fh)

    def write_file(self, doc):
        with open(self.file, "w") as fh:
            json.dump(doc, fh)

    def test_first_run_is_silent_and_records_every_cause(self):
        self.run_notify(card("o/r#1", "READY:abc"))
        self.desktop.assert_not_called()
        self.assertEqual(self.saved(), {"v": 2, "seen": {"o/r#1": ["READY:abc"]}})

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
