"""HcBackend with worker tabs: agents carry their space and tab ids, target finds
an agent by either, and the sessions list shows the tabs that run in a worktree."""
import unittest
from unittest import mock

from tests import SCRATCH  # noqa: F401  (imports the scratch state before scan)
import scan


def stub_session_matches(agent, session):
    """Stands in for the shared helper: an agent matches a session by any id."""
    return session in agent.get("ids", [])


class HcAgentsTest(unittest.TestCase):
    def setUp(self):
        self.terminals = []
        self.labels = {}
        mock.patch.object(scan.shutil, "which", return_value="/bin/herdr-app").start()
        mock.patch.object(scan, "hc", side_effect=self.fake_hc).start()
        mock.patch.object(scan, "session_matches", stub_session_matches,
                          create=True).start()
        self.addCleanup(mock.patch.stopall)

    def fake_hc(self, args, timeout=60):
        if args[:2] == ["ls", "--json"]:
            return 0, {"terminals": self.terminals}, ""
        if args[0] == "get":
            return 0, {"name": None, "labels": self.labels.get(args[1], {})}, ""
        raise AssertionError(f"unexpected hc call {args}")

    def test_agents_carry_the_space_and_tab_ids(self):
        self.terminals = [{"address": "t1", "agent": "claude-code", "status": "idle"},
                          {"address": "t2", "agent": "claude-code", "status": "done"},
                          {"address": "t3", "agent": None}]
        self.labels = {"t1": {"herdr.space": "sp1", "herdr.tab": "tb1",
                              "octopilot.agent": "ap-x-1"},
                       "t2": {"herdr.space": "sp2"}}
        agents = {a["pane"]: a for a in scan.HC.agents()}
        self.assertEqual(set(agents), {"t1", "t2"})
        self.assertEqual(agents["t1"]["ids"], ["sp1", "tb1"])
        self.assertEqual(agents["t1"]["session"], "sp1")
        self.assertEqual(agents["t1"]["agent"], "ap-x-1")
        self.assertEqual(agents["t2"]["ids"], ["sp2"])

    def test_target_finds_a_worker_by_its_tab(self):
        self.terminals = [{"address": "t1", "agent": "claude-code", "status": "idle"}]
        self.labels = {"t1": {"herdr.space": "sp-oct", "herdr.tab": "tb9",
                              "octopilot.agent": "ap-x-1"}}
        item = {"session": "tb9", "agent": "ap-x-1"}
        self.assertEqual(scan.HC.target(item), "t1")

    def test_target_of_a_space_session_still_matches(self):
        self.terminals = [{"address": "t1", "agent": "claude-code", "status": "idle"}]
        self.labels = {"t1": {"herdr.space": "sp1", "herdr.tab": "tb1",
                              "octopilot.agent": "ap-x-1"}}
        self.assertEqual(scan.HC.target({"session": "sp1", "agent": "other"}), "t1")

    def test_target_is_none_for_another_tab(self):
        self.terminals = [{"address": "t1", "agent": "claude-code", "status": "idle"}]
        self.labels = {"t1": {"herdr.space": "sp1", "herdr.tab": "tb1"}}
        self.assertIsNone(scan.HC.target({"session": "tb2"}))


WT = "/home/u/src/repo/.worktrees/feat-x"


def wt_row(session, title, space=None, path=WT):
    return {"id": session, "title": title, "path": path, "backend": "hc",
            "space": space or session,
            "worktree": {"branch": "feat/x", "main_repo_path": "/home/u/src/repo"}}


class HcSessionsTest(unittest.TestCase):
    def setUp(self):
        self.cwds = {}  # hc ls: terminal -> cwd
        self.labels = {}  # hc get: terminal -> labels
        self.gets = []
        # worktree_of: a folder inside WT is that worktree; everything else is not.
        mock.patch.object(scan, "worktree_of", side_effect=self.fake_worktree_of).start()
        self.calls = []
        mock.patch.object(scan, "hc", side_effect=self.fake_hc).start()
        self.addCleanup(mock.patch.stopall)

    def fake_worktree_of(self, folder):
        self.calls.append(folder)
        if folder.startswith(WT):
            return WT, "/home/u/src/repo", "feat/x"
        return None

    def fake_hc(self, args, timeout=60):
        if args[:2] == ["ls", "--json"]:
            return 0, {"terminals": [{"address": a, "cwd": c}
                                     for a, c in self.cwds.items()]}, ""
        if args[0] == "get":
            self.gets.append(args[1])
            if args[1] not in self.labels:
                return 1, {"error": {"kind": "not_found"}}, "terminal not found"
            return 0, {"labels": self.labels[args[1]]}, ""
        raise AssertionError(f"unexpected hc call {args}")

    def sessions(self, spaces):
        return scan.HC.sessions(spaces)

    def test_a_worktree_space_is_a_row_with_its_own_space(self):
        spaces = [{"id": "sp1", "name": "feat x", "folder": WT, "tabs": []}]
        self.assertEqual(self.sessions(spaces), [wt_row("sp1", "feat x")])

    def test_a_worker_tab_in_a_local_space_is_a_row(self):
        self.cwds = {"t1": WT + "/src"}
        self.labels = {"t1": {"herdr.tab": "tb1"}}
        spaces = [{"id": "spOct", "name": "Octopilot", "folder": "/home/u/src",
                   "tabs": [{"id": "tb1", "name": "Fix: x", "settled": True,
                             "terminals": [{"terminal": "t1"}]}]}]
        self.assertEqual(self.sessions(spaces),
                         [wt_row("tb1", "Fix: x", space="spOct")])

    def test_a_settled_tab_is_still_a_row(self):
        self.cwds = {"t1": WT}
        self.labels = {"t1": {}}
        spaces = [{"id": "spOct", "folder": "/home/u/src",
                   "tabs": [{"id": "tb1", "name": "Fix", "settled": True,
                             "terminals": [{"terminal": "t1"}]}]}]
        self.assertEqual([r["id"] for r in self.sessions(spaces)], ["tb1"])

    def test_the_launchers_terminals_are_not_rows(self):
        self.cwds = {"t1": WT, "t2": WT}
        self.labels = {"t1": {"octopilot.dir": "/home/u/src/octopilot"},
                       "t2": {"octopilot.dir": "/home/u/src/octopilot"}}
        spaces = [{"id": "spOct", "folder": "/home/u/src",
                   "tabs": [{"id": "tb1", "name": "engine",
                             "terminals": [{"terminal": "t1"}]},
                            {"id": "tb2", "name": "code",
                             "terminals": [{"terminal": "t2"}]}]}]
        self.assertEqual(self.sessions(spaces), [])

    def test_a_tab_outside_a_worktree_is_not_a_row_and_gets_no_label_call(self):
        self.cwds = {"t1": "/home/u/src"}
        spaces = [{"id": "spOct", "folder": "/home/u/src",
                   "tabs": [{"id": "tb1", "name": "engine",
                             "terminals": [{"terminal": "t1"}]}]}]
        self.assertEqual(self.sessions(spaces), [])
        self.assertEqual(self.gets, [])

    def test_hc_get_is_called_only_for_worktree_candidates(self):
        self.cwds = {"t1": "/home/u/src", "t2": WT}
        self.labels = {"t2": {}}
        spaces = [{"id": "spOct", "folder": "/home/u/src",
                   "tabs": [{"id": "tb1", "name": "engine",
                             "terminals": [{"terminal": "t1"}]},
                            {"id": "tb2", "name": "w",
                             "terminals": [{"terminal": "t2"}]}]}]
        self.assertEqual([r["id"] for r in self.sessions(spaces)], ["tb2"])
        self.assertEqual(self.gets, ["t2"])

    def test_a_space_with_a_worktree_folder_has_no_tab_rows(self):
        self.cwds = {"t1": WT}
        spaces = [{"id": "spW", "name": "feat", "folder": WT,
                   "tabs": [{"id": "tb1", "name": "1",
                             "terminals": [{"terminal": "t1"}]}]}]
        self.assertEqual(self.sessions(spaces), [wt_row("spW", "feat")])

    def test_a_tab_whose_terminal_has_no_known_cwd_is_skipped(self):
        spaces = [{"id": "spOct", "folder": "/home/u/src",
                   "tabs": [{"id": "tb1", "name": "x",
                             "terminals": [{"terminal": "t9"}]}]}]
        self.assertEqual(self.sessions(spaces), [])

    def test_a_remote_space_is_skipped(self):
        self.cwds = {"t1": WT}
        spaces = [{"id": "bolt/sp1", "machine": "bolt", "folder": WT, "tabs": []},
                  {"id": "bolt/sp2", "machine": "bolt", "folder": "/x",
                   "tabs": [{"id": "bolt/tb1", "terminals": [{"terminal": "t1"}]}]}]
        self.assertEqual(self.sessions(spaces), [])

    def test_worktree_of_is_called_once_per_folder(self):
        self.cwds = {"t1": WT, "t2": WT}
        self.labels = {"t1": {}, "t2": {}}
        spaces = [{"id": "spOct", "folder": "/home/u/src",
                   "tabs": [{"id": "tb1", "name": "a",
                             "terminals": [{"terminal": "t1"}]},
                            {"id": "tb2", "name": "b",
                             "terminals": [{"terminal": "t2"}]}]},
                  {"id": "spW", "folder": WT, "tabs": []}]
        self.sessions(spaces)
        self.assertEqual(self.calls.count(WT), 1)
        self.assertEqual(self.calls.count("/home/u/src"), 1)

    def test_listing_ids_are_spaces_and_every_tab(self):
        spaces = [{"id": "spOct", "folder": "/home/u/src",
                   "tabs": [{"id": "tb1", "settled": True, "terminals": []},
                            {"id": "tb2", "settled": False, "terminals": []}]}]
        with mock.patch.object(scan.HcBackend, "spaces", return_value=spaces):
            rows, answered, ids = scan.HC.listing()
        self.assertEqual(rows, [])
        self.assertTrue(answered)
        self.assertEqual(ids, {"spOct", "tb1", "tb2"})

    def test_checkout_of_a_tab(self):
        self.cwds = {"t1": WT}
        self.labels = {"t1": {}}
        spaces = [{"id": "spOct", "folder": "/home/u/src",
                   "tabs": [{"id": "tb1", "name": "a",
                             "terminals": [{"terminal": "t1"}]}]}]
        self.assertEqual(scan.HC.checkout_of(spaces, "tb1"), WT)
        self.assertIsNone(scan.HC.checkout_of(spaces, "tb9"))


if __name__ == "__main__":
    unittest.main()
