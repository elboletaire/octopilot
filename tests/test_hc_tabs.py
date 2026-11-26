"""HcBackend with worker tabs: agents carry their space and tab ids, target finds
an agent by either, and the sessions list shows the tabs that run in a worktree."""
import contextlib
import io
import json
import os
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


class HcSpawnTest(unittest.TestCase):
    """HcBackend.spawn: a worker's tab in the Octopilot space, or a space of its own."""
    SPACE = "/home/u/src/repo/.worktrees/feat-x"

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"WORKER_HOST": "tab"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.replies = {}  # app_do command -> reply
        self.app_calls = []
        self.labels = {}  # hc get: terminal -> labels
        self.hc_calls = []
        self.status_of = "idle"
        self.space = "spOct"
        mock.patch.object(scan, "app_do", side_effect=self.fake_app).start()
        mock.patch.object(scan, "hc", side_effect=self.fake_hc).start()
        mock.patch.object(scan, "accept_trust", return_value=True).start()
        mock.patch.object(scan.HC, "status", side_effect=lambda t: self.status_of).start()
        mock.patch.object(scan.HC, "wait_idle", return_value=(0, "")).start()
        mock.patch.object(scan, "agent_name", return_value="ap-x-1").start()
        mock.patch.object(scan, "plugin_settings", return_value="").start()
        mock.patch.object(scan, "octopilot_space", side_effect=lambda: self.space,
                          create=True).start()
        mock.patch.object(scan.time, "sleep").start()
        mock.patch.object(scan, "SETTLE_WORKER_TABS", True).start()
        self.addCleanup(mock.patch.stopall)
        self.replies = {
            "tab.new": (0, {"terminal": "t7", "tab": "tb1"}, ""),
            "space.new": (0, {"space": "sp7", "terminal": "t7"}, ""),
            "tab.settle": (0, {}, ""),
        }

    def fake_app(self, command, args, timeout=60):
        self.app_calls.append((command, args))
        return self.replies[command]

    def fake_hc(self, args, timeout=60):
        self.hc_calls.append(args)
        if args[0] == "get":
            return 0, {"labels": self.labels.get(args[1], {})}, ""
        return 0, {}, ""

    def argv(self, model=None):
        return json.dumps(scan.HC.spawn_command(model))

    def spawn(self, title="Fix: x", model=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            host = scan.HC.spawn("k#1", "/home/u/src/repo", self.SPACE, title, model)
        return host, out.getvalue()

    def calls(self, command):
        return [args for c, args in self.app_calls if c == command]

    def test_a_worker_is_a_tab_of_the_octopilot_space(self):
        host, _ = self.spawn()
        self.assertEqual(self.calls("tab.new"), [[
            "--space", "spOct", "--name", "Fix: x", "--folder", self.SPACE,
            "--argv", self.argv(), "--focus", "false"]])
        self.assertEqual(self.calls("space.new"), [])
        self.assertEqual(host, {"backend": "hc", "session": "tb1", "space": "spOct",
                                "agent": "ap-x-1", "worktree": self.SPACE})
        self.assertIn(["label", "t7", "octopilot.agent=ap-x-1"], self.hc_calls)

    def test_the_tab_is_settled_and_the_tab_comes_from_the_reply(self):
        self.spawn()
        self.assertEqual(self.calls("tab.settle"), [["--id", "tb1"]])

    def test_no_settle_when_settling_is_off(self):
        with mock.patch.object(scan, "SETTLE_WORKER_TABS", False):
            self.spawn()
        self.assertEqual(self.calls("tab.settle"), [])

    def test_the_tab_label_is_read_when_the_reply_has_no_tab(self):
        self.replies["tab.new"] = (0, {"terminal": "t7"}, "")
        self.labels = {"t7": {"herdr.tab": "tb5"}}
        host, _ = self.spawn()
        self.assertEqual(host["session"], "tb5")
        self.assertEqual(self.calls("tab.settle"), [["--id", "tb5"]])

    def test_a_tab_with_no_label_is_an_error_and_is_kept(self):
        self.replies["tab.new"] = (0, {"terminal": "t7"}, "")
        with self.assertRaises(SystemExit) as ctx:
            self.spawn()
        self.assertIn("t7 has no tab label (tab kept)", str(ctx.exception))
        self.assertEqual(self.calls("tab.settle"), [])

    def test_a_reserved_tab_name_gets_the_agent_name(self):
        for title in ("OCTOPILOT", "engine", "Octopilot (code changes)"):
            self.app_calls.clear()
            self.spawn(title=title)
            name = self.calls("tab.new")[0][3]
            self.assertEqual(name, f"{title} (ap-x-1)")

    def test_a_worker_title_is_the_tab_name(self):
        self.spawn(title="Octopilot fixes")
        self.assertEqual(self.calls("tab.new")[0][3], "Octopilot fixes")

    def test_the_model_is_in_the_tab_argv(self):
        self.spawn(model="claude-opus-5-5[1m]")
        argv = json.loads(self.calls("tab.new")[0][7])
        self.assertEqual(argv[-2:], ["--model", "claude-opus-5-5[1m]"])

    def test_a_blocked_agent_in_a_tab_warns_with_the_tab(self):
        self.status_of = "blocked"
        host, _ = self.spawn()
        self.assertEqual(host["warning"], "agent ap-x-1 is blocked at startup; check "
                         "tab tb1 before sending it a prompt")

    def test_worker_host_space_spawns_a_space(self):
        os.environ["WORKER_HOST"] = "space"
        host, _ = self.spawn()
        self.assertEqual([c for c, _ in self.app_calls], ["space.new"])
        self.assertEqual(self.calls("space.new")[0][:4],
                         ["--name", "Fix: x", "--folder", self.SPACE])
        self.assertEqual(host, {"backend": "hc", "session": "sp7", "agent": "ap-x-1",
                                "worktree": self.SPACE})

    def test_no_octopilot_space_spawns_a_space_with_a_note(self):
        self.space = None
        host, out = self.spawn()
        self.assertEqual([c for c, _ in self.app_calls], ["space.new"])
        self.assertIn("no Octopilot space open", out)
        self.assertEqual(host["session"], "sp7")
        self.assertNotIn("space", host)

    def test_a_blocked_agent_in_a_space_warns_with_the_space(self):
        os.environ["WORKER_HOST"] = "space"
        self.status_of = "blocked"
        host, _ = self.spawn()
        self.assertEqual(host["warning"], "agent ap-x-1 is blocked at startup; check "
                         "space sp7 before sending it a prompt")

    def test_a_failed_tab_new_is_an_error(self):
        self.replies["tab.new"] = (1, None, "no such space")
        with self.assertRaises(SystemExit) as ctx:
            self.spawn()
        self.assertEqual(str(ctx.exception), "herdr-app tab.new failed: no such space")

    def test_a_failed_label_keeps_the_tab(self):
        def label_fails(args, timeout=60):
            if args[0] == "label":
                return 1, None, "no terminal"
            return 0, {}, ""
        with mock.patch.object(scan, "hc", side_effect=label_fails), \
                self.assertRaises(SystemExit) as ctx:
            self.spawn()
        self.assertEqual(str(ctx.exception), "hc label failed (tab tb1 kept): no terminal")


if __name__ == "__main__":
    unittest.main()
