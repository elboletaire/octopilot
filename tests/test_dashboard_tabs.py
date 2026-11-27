"""The dashboard reaches a worker's tab, a space or a workspace through its
backend: focus, shell, the space in view.json and the remove wording."""
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests import fake  # noqa: F401  (sets the scratch environment)

try:
    import dashboard
except ImportError:  # the rich package is the dashboard's own dependency
    dashboard = None


@unittest.skipIf(dashboard is None, "dashboard needs rich")
class FocusTest(unittest.TestCase):
    def setUp(self):
        self.hosts = dashboard.Hosts()
        self.hc = mock.patch.object(dashboard.scan.HC, "focus",
                                    return_value=(True, ""))
        self.herdr = mock.patch.object(dashboard.scan.HERDR, "focus",
                                       return_value=(True, ""))
        self.hc_focus = self.hc.start()
        self.herdr_focus = self.herdr.start()
        self.addCleanup(self.hc.stop)
        self.addCleanup(self.herdr.stop)

    def test_a_gui_tab_focuses_through_hc_with_its_agent(self):
        self.assertTrue(self.hosts.focus("tb1a", {"tb1a": {"pane": "term1"}}))
        self.hc_focus.assert_called_once_with("tb1a", "term1", timeout=10)
        self.herdr_focus.assert_not_called()

    def test_a_gui_space_focuses_through_hc_without_an_agent(self):
        self.assertTrue(self.hosts.focus("sp1", {}))
        self.hc_focus.assert_called_once_with("sp1", None, timeout=10)

    def test_a_tui_tab_focuses_through_herdr_with_its_agent(self):
        self.hosts.focus("w1:t2", {"w1:t2": {"pane": "p9"}})
        self.herdr_focus.assert_called_once_with("w1:t2", "p9", timeout=10)
        self.hc_focus.assert_not_called()

    def test_a_tui_workspace_focuses_through_herdr_without_the_dashboard(self):
        self.hosts.focus("w1", {})
        self.herdr_focus.assert_called_once_with("w1", None, timeout=10)

    def test_a_refused_focus_names_the_session(self):
        model = mock.Mock(lock=threading.Lock(), live={},
                          backend=mock.Mock(focus=mock.Mock(return_value=False)))
        res = dashboard.focus_row(model, {"session": "w1:t2"})
        self.assertEqual(res, dashboard.Result(False, "could not focus session w1:t2"))


@unittest.skipIf(dashboard is None, "dashboard needs rich")
class ShellRowTest(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.path)

    def test_a_gui_tab_opens_its_tab_in_its_space(self):
        e = {"session": "tb1a", "space": "sp7", "path": self.path}
        with mock.patch.object(dashboard.scan, "app_do",
                               return_value=(0, "", "")) as app_do:
            res = dashboard.shell_row(None, e)
        self.assertEqual(res, dashboard.Result(True, ""))
        app_do.assert_called_once_with("tab.new", ["--space", "sp7",
                                                   "--folder", self.path],
                                       timeout=10)

    def test_the_tui_focus_passes_its_timeout_to_herdr(self):
        with mock.patch.object(dashboard.scan, "herdr", return_value=(0, {}, "")) as herdr:
            dashboard.scan.HERDR.focus("w1:t2", None, timeout=10)
        herdr.assert_called_once_with(["tab", "focus", "w1:t2"], timeout=10)

    def test_a_gui_tab_without_a_space_is_refused(self):
        e = {"session": "tb1a", "space": None, "path": self.path}
        with mock.patch.object(dashboard.scan, "app_do") as app_do:
            res = dashboard.shell_row(None, e)
        self.assertEqual(res, dashboard.Result(False, "no space for this row"))
        app_do.assert_not_called()

    def test_a_tui_tab_opens_the_herdr_popup_in_its_worktree(self):
        e = {"session": "w1:t2", "space": None, "path": self.path}
        with mock.patch.object(dashboard, "popup", return_value=(True, "")) as popup:
            res = dashboard.shell_row(None, e)
        self.assertEqual(res, dashboard.Result(True, ""))
        popup.assert_called_once_with("shell", {}, cwd=self.path)


@unittest.skipIf(dashboard is None, "dashboard needs rich")
class ShellRefusalTest(unittest.TestCase):
    def refusal(self, e):
        with mock.patch.object(dashboard, "tracked_item", return_value=None):
            return dashboard.row_refusal(None, "shell", e)

    def test_a_gui_tab_without_a_space_is_refused_before_the_action(self):
        e = {"session": "tb1a", "space": None, "path": tempfile.gettempdir(),
             "kind": "WORKING", "key": "owner/repo#8"}
        self.assertEqual(self.refusal(e), "no space for this row")

    def test_a_gui_tab_with_a_space_and_a_tui_tab_pass(self):
        base = {"path": tempfile.gettempdir(), "kind": "WORKING",
                "key": "owner/repo#8"}
        self.assertIsNone(self.refusal({**base, "session": "tb1a", "space": "sp7"}))
        self.assertIsNone(self.refusal({**base, "session": "w1:t2", "space": None}))

    def test_no_worktree_is_still_refused_first(self):
        e = {"session": "tb1a", "space": None, "path": None,
             "kind": "WORKING", "key": "owner/repo#8"}
        self.assertEqual(self.refusal(e), "no worktree for this row")


@unittest.skipIf(dashboard is None, "dashboard needs rich")
class EntrySpaceTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        for name in ("SNAPSHOT_FILE", "STATE_FILE", "EVENTS_FILE"):
            patch = mock.patch.object(dashboard, name, os.path.join(d, name))
            patch.start()
            self.addCleanup(patch.stop)
        self.model = dashboard.Model(None)

    def space_of(self, it, sessions=None):
        row = {"kind": "WORKING", "title": "t"}
        e = self.model.entry(row, "owner/repo#8", it, {}, sessions or {},
                             time.time(), None)
        return e["space"]

    def test_a_gui_tab_takes_its_space_from_the_session_row(self):
        self.assertEqual(self.space_of({"session": "tb1a"},
                                       {"tb1a": {"space": "sp7"}}), "sp7")

    def test_a_gui_tab_falls_back_to_the_items_space(self):
        self.assertEqual(self.space_of({"session": "tb1a", "space": "sp7"}), "sp7")

    def test_a_gui_tab_with_no_known_space_has_none(self):
        self.assertIsNone(self.space_of({"session": "tb1a"}))

    def test_a_gui_space_is_its_own_space(self):
        self.assertEqual(self.space_of({"session": "sp1"}), "sp1")

    def test_a_tui_tab_and_workspace_have_no_space(self):
        self.assertIsNone(self.space_of({"session": "w1:t2"},
                                        {"w1:t2": {"space": "w1"}}))
        self.assertIsNone(self.space_of({"session": "w1"}))

    def test_no_session_has_no_space(self):
        self.assertIsNone(self.space_of({}))

    def test_view_json_carries_the_space(self):
        e = self.model.entry({"kind": "WORKING", "title": "t"}, "owner/repo#8",
                             {"session": "tb1a"}, {}, {"tb1a": {"space": "sp7"}},
                             time.time(), None)
        with mock.patch.object(dashboard, "tracked_item", return_value=None):
            view = dashboard.entry_view(self.model, e, "running")
        self.assertEqual(view["space"], "sp7")
        self.assertEqual(view["session"], "tb1a")


@unittest.skipIf(dashboard is None, "dashboard needs rich")
class RemoveWordingTest(unittest.TestCase):
    def confirm(self, session):
        e = {"key": "owner/repo#8", "kind": "DONE"}
        with mock.patch.object(dashboard, "tracked_item",
                               return_value={"session": session}):
            return dashboard.row_confirm(None, "remove", e)

    def test_a_tab_says_the_tab_and_worktree_go(self):
        self.assertIn("tab and worktree deleted", self.confirm("tb1a"))
        self.assertIn("tab and worktree deleted", self.confirm("w1:t2"))

    def test_a_space_or_workspace_keeps_the_legacy_wording(self):
        self.assertIn("workspace and worktree deleted", self.confirm("sp1"))
        self.assertIn("workspace and worktree deleted", self.confirm("w1"))

    def test_an_item_without_a_session_keeps_only_the_worktree(self):
        self.assertIn("worktree kept", self.confirm(None))


if __name__ == "__main__":
    unittest.main()
