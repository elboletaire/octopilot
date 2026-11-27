"""HcBackend with worker tabs: agents carry their space and tab ids, target finds
an agent by either, and the sessions list shows the tabs that run in a worktree."""
import contextlib
import io
import json
import os
import unittest
import uuid
from unittest import mock

from tests import SCRATCH  # noqa: F401  (imports the scratch state before scan)
import scan


SID = "8f1c2a4e-5b6d-4e7f-8a9b-0c1d2e3f4a5b"  # the claude session id every spawn gets


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
        if args[0] == "ls":
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

    def test_agents_carry_the_live_claude_session(self):
        self.terminals = [{"address": "t1", "agent": "claude-code", "status": "idle"}]
        self.labels = {"t1": {"herdr.space": "sp1", "octopilot.agent": "ap-x-1"}}
        with mock.patch.object(scan, "live_session_id", return_value="c-new"):
            agents = scan.HC.agents()
        self.assertEqual(agents[0]["claude_session"], "c-new")
        live = scan.live_map(agents, {"idle": "idle"}, "hc")
        self.assertEqual(live["sp1"]["claude_session"], "c-new")

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
        mock.patch.object(scan, "worktree_of",
                          side_effect=self.fake_worktree_of).start()
        self.calls = []
        mock.patch.object(scan, "hc", side_effect=self.fake_hc).start()
        self.addCleanup(mock.patch.stopall)

    def fake_worktree_of(self, folder):
        self.calls.append(folder)
        if folder.startswith(WT):
            return WT, "/home/u/src/repo", "feat/x"
        return None

    def fake_hc(self, args, timeout=60):
        if args[0] == "ls":
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

    def test_terminal_cwds_lists_finished_terminals_too(self):
        listed = {"terminals": [{"address": "t1", "cwd": WT}]}
        with mock.patch.object(scan, "hc", return_value=(0, listed, "")) as hc:
            self.assertEqual(scan.HC.terminal_cwds(), {"t1": WT})
        hc.assert_called_once_with(["ls", "--all", "--json"], timeout=10)

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
    """HcBackend.spawn: a worker's tab in the Octopilot space, or a space of its own,
    started by hc run with the ids chosen here."""
    SPACE = "/home/u/src/repo/.worktrees/feat-x"
    TAB = "tb" + SID.replace("-", "")  # uuid4().hex of the patched uuid

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"WORKER_HOST": "tab", "SHELL": "/bin/zsh"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.window = True  # herdr-app answers
        self.replies = {"tab.rename": (0, {}, ""), "tab.settle": (0, {}, ""),
                        "space.rename": (0, {}, "")}
        self.app_calls = []
        self.labels = {}  # hc get: terminal -> labels
        self.hc_calls = []
        self.run_reply = (0, {"address": "t7"}, "")
        self.status_of = "idle"
        self.space = "spOct"
        mock.patch.object(scan, "window_open", side_effect=lambda: self.window).start()
        mock.patch.object(scan, "app_do", side_effect=self.fake_app).start()
        mock.patch.object(scan, "hc", side_effect=self.fake_hc).start()
        mock.patch.object(scan, "accept_trust", return_value=True).start()
        mock.patch.object(scan.HC, "status",
                          side_effect=lambda t: self.status_of).start()
        mock.patch.object(scan.HC, "wait_idle", return_value=(0, "")).start()
        mock.patch.object(scan, "agent_name", return_value="ap-x-1").start()
        mock.patch.object(scan, "plugin_settings", return_value="").start()
        mock.patch.object(scan, "octopilot_space", side_effect=lambda: self.space,
                          create=True).start()
        mock.patch.object(scan.time, "sleep").start()
        mock.patch.object(scan, "SETTLE_WORKER_TABS", True).start()
        mock.patch.object(scan.uuid, "uuid4", return_value=uuid.UUID(SID)).start()
        self.addCleanup(mock.patch.stopall)

    def fake_app(self, command, args, timeout=60):
        self.app_calls.append((command, args))
        return self.replies[command]

    def fake_hc(self, args, timeout=60):
        self.hc_calls.append(args)
        if args[0] == "run":
            return self.run_reply
        if args[0] == "get":
            return 0, {"labels": self.labels.get(args[1], {})}, ""
        return 0, {}, ""

    def argv(self, model=None):
        return scan.HC.spawn_command(model, session=SID)

    def run_args(self):
        """The argv of the one hc run."""
        runs = [a for a in self.hc_calls if a[0] == "run"]
        self.assertEqual(len(runs), 1, runs)
        return runs[0]

    def spawn(self, title="Fix: x", model=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            host = scan.HC.spawn("k#1", "/home/u/src/repo", self.SPACE, title, model)
        return host, out.getvalue()

    def calls(self, command):
        return [args for c, args in self.app_calls if c == command]

    def test_a_worker_is_a_tab_of_the_octopilot_space_started_with_no_parent(self):
        host, _ = self.spawn()
        self.assertEqual(self.run_args(), [
            "run", "--bg", "--json", "--no-parent", "--cwd", self.SPACE, "--keep",
            "--label", "herdr.space=spOct", "--label", f"herdr.tab={self.TAB}",
            "--label", "octopilot.agent=ap-x-1",
            "--", "/bin/zsh", "-ic", 'exec "$@"', "/bin/zsh", *self.argv()])
        self.assertEqual(host, {"backend": "hc", "session": self.TAB, "space": "spOct",
                                "agent": "ap-x-1", "worktree": self.SPACE,
                                "claude_session": SID})
        self.assertEqual(self.app_calls[0][0], "tab.rename")

    def test_no_tab_is_created_by_herdr_app_or_labelled_after_the_start(self):
        self.spawn()
        self.assertEqual([c for c, _ in self.app_calls],
                         ["tab.rename", "tab.settle"])
        self.assertNotIn("label", [a[0] for a in self.hc_calls])

    def test_the_tab_is_named_and_settled_when_a_window_shows_it(self):
        self.spawn()
        self.assertEqual(self.calls("tab.rename"), [[
            "--id", self.TAB, "--name", "Fix: x"]])
        self.assertEqual(self.calls("tab.settle"), [["--id", self.TAB]])

    def test_no_window_means_no_herdr_app_call_at_all(self):
        self.window = False
        host, _ = self.spawn()
        self.assertEqual(self.app_calls, [])
        self.assertEqual(host["session"], self.TAB)
        self.assertEqual(self.run_args()[:4], ["run", "--bg", "--json", "--no-parent"])

    def test_no_settle_when_settling_is_off(self):
        with mock.patch.object(scan, "SETTLE_WORKER_TABS", False):
            self.spawn()
        self.assertEqual(self.calls("tab.settle"), [])
        self.assertEqual(len(self.calls("tab.rename")), 1)

    def test_a_failed_rename_is_a_note_not_an_error(self):
        self.replies["tab.rename"] = (1, None, "no such tab")
        host, out = self.spawn()
        self.assertEqual(host["session"], self.TAB)
        self.assertIn(f"note: tab {self.TAB} not renamed: no such tab", out)

    def test_a_failed_run_is_an_error(self):
        self.run_reply = (1, {"error": {"kind": "spawn_failed"}}, "cannot start")
        with self.assertRaises(SystemExit) as ctx:
            self.spawn()
        self.assertEqual(str(ctx.exception), "hc run failed: cannot start")
        self.assertEqual(self.app_calls, [])

    def test_a_reserved_tab_name_gets_the_agent_name(self):
        for title in ("OCTOPILOT", "engine", "Octopilot (code changes)"):
            self.app_calls.clear()
            self.spawn(title=title)
            self.assertEqual(self.calls("tab.rename")[0][3], f"{title} (ap-x-1)")

    def test_a_worker_title_is_the_tab_name(self):
        self.spawn(title="Octopilot fixes")
        self.assertEqual(self.calls("tab.rename")[0][3], "Octopilot fixes")

    def test_the_model_is_in_the_run_argv(self):
        self.spawn(model="claude-opus-5-5[1m]")
        self.assertEqual(self.run_args()[-2:], ["--model", "claude-opus-5-5[1m]"])

    def test_the_claude_starts_under_the_session_id(self):
        self.spawn(model="claude-opus-5-5")
        argv = self.run_args()
        self.assertEqual(argv[argv.index("--session-id") + 1], SID)
        self.assertEqual(argv[-2:], ["--model", "claude-opus-5-5"])

    def test_a_blocked_agent_in_a_tab_warns_with_the_tab(self):
        self.status_of = "blocked"
        host, _ = self.spawn()
        self.assertEqual(host["warning"], f"agent ap-x-1 is blocked at startup; check "
                         f"tab {self.TAB} before sending it a prompt")

    def test_worker_host_space_spawns_a_space_and_names_it(self):
        os.environ["WORKER_HOST"] = "space"
        host, _ = self.spawn()
        self.assertEqual(self.run_args()[:4], ["run", "--bg", "--json", "--no-parent"])
        self.assertIn(f"herdr.space={self.TAB.replace('tb', 'sp')}", self.run_args())
        self.assertIn(f"herdr.tab={self.TAB}", self.run_args())
        self.assertEqual(self.calls("space.rename"), [[
            "--id", self.TAB.replace("tb", "sp"), "--name", "Fix: x"]])
        self.assertEqual(self.calls("tab.new"), [])
        self.assertEqual(host, {"backend": "hc", "session": self.TAB.replace("tb", "sp"),
                                "agent": "ap-x-1", "worktree": self.SPACE,
                                "claude_session": SID})

    def test_a_space_with_no_window_makes_no_herdr_app_call(self):
        os.environ["WORKER_HOST"] = "space"
        self.window = False
        self.spawn()
        self.assertEqual(self.app_calls, [])

    def test_no_octopilot_space_spawns_a_space_with_a_note(self):
        self.space = None
        host, _ = self.spawn()
        self.assertEqual(self.calls("tab.new"), [])
        self.assertEqual(host["note"], "no Octopilot space open; the worker gets "
                                       "a space of its own")
        self.assertNotIn("warning", host)
        self.assertTrue(host["session"].startswith("sp"))
        self.assertNotIn("space", host)

    def test_a_blocked_agent_in_a_space_warns_with_the_space(self):
        os.environ["WORKER_HOST"] = "space"
        self.status_of = "blocked"
        host, _ = self.spawn()
        self.assertEqual(host["warning"], "agent ap-x-1 is blocked at startup; check "
                         f"space {host['session']} before sending it a prompt")

    def test_a_restore_resumes_or_continues_a_session(self):
        self.assertEqual(scan.HC.spawn_command(None, resume="c-1")[-2:],
                         ["--resume", "c-1"])
        self.assertEqual(scan.HC.spawn_command(None, cont=True)[-1], "--continue")
        self.assertNotIn("--session-id", scan.HC.spawn_command(None, resume="c-1"))

    def test_a_session_cannot_resume_or_continue(self):
        with self.assertRaises(ValueError):
            scan.HC.spawn_command(None, session=SID, resume="c-1")
        with self.assertRaises(ValueError):
            scan.HC.spawn_command(None, session=SID, cont=True)


class HcStartTest(unittest.TestCase):
    """hc_start: a terminal with no parent, its argv in the login shell."""

    def setUp(self):
        self.hc_reply = (0, {"address": "t9"}, "")
        self.hc_calls = []
        mock.patch.object(scan, "hc", side_effect=self.fake_hc).start()
        self.addCleanup(mock.patch.stopall)

    def fake_hc(self, args, timeout=60):
        self.hc_calls.append((args, timeout))
        return self.hc_reply

    def test_the_terminal_has_no_parent_and_its_labels_and_cwd(self):
        with mock.patch.dict(os.environ, {"SHELL": "/bin/zsh"}):
            address = scan.hc_start(["env", "A=1", "claude"], "/wt",
                                    {"herdr.space": "sp1", "octopilot.agent": "ap-x"},
                                    keep=True)
        self.assertEqual(address, "t9")
        (args, timeout), = self.hc_calls
        self.assertEqual(args, [
            "run", "--bg", "--json", "--no-parent", "--cwd", "/wt", "--keep",
            "--label", "herdr.space=sp1", "--label", "octopilot.agent=ap-x",
            "--", "/bin/zsh", "-ic", 'exec "$@"', "/bin/zsh", "env", "A=1", "claude"])
        self.assertEqual(timeout, 60)

    def test_without_keep_there_is_no_keep_flag(self):
        with mock.patch.dict(os.environ, {"SHELL": "/bin/zsh"}):
            scan.hc_start(["claude"], "/wt", {}, keep=False)
        self.assertNotIn("--keep", self.hc_calls[0][0])
        self.assertNotIn("--label", self.hc_calls[0][0])

    def test_the_login_shell_of_the_passwd_is_the_fallback(self):
        passwd = mock.Mock(pw_shell="/usr/bin/fish")
        with mock.patch.dict(os.environ, {"SHELL": ""}), \
                mock.patch.object(scan.pwd, "getpwuid", return_value=passwd):
            scan.hc_start(["claude"], "/wt", {}, keep=False)
        self.assertEqual(self.hc_calls[0][0][-5:], ["/usr/bin/fish", "-ic",
                                                     'exec "$@"', "/usr/bin/fish",
                                                     "claude"])

    def test_a_failed_start_is_an_error(self):
        self.hc_reply = (1, {"error": {"kind": "spawn_failed"}}, "cannot start")
        with self.assertRaises(SystemExit) as ctx:
            scan.hc_start(["claude"], "/wt", {}, keep=False)
        self.assertEqual(str(ctx.exception), "hc run failed: cannot start")

    def test_no_address_is_an_error(self):
        self.hc_reply = (0, {}, "")
        with self.assertRaises(SystemExit):
            scan.hc_start(["claude"], "/wt", {}, keep=False)


class WindowOpenTest(unittest.TestCase):
    def test_a_window_is_open_when_herdr_app_status_answers(self):
        with mock.patch.object(scan, "cli", return_value=(0, {}, "")) as cli:
            self.assertTrue(scan.window_open())
        cli.assert_called_once_with(["herdr-app", "status"], timeout=10)

    def test_no_window_when_herdr_app_status_fails(self):
        with mock.patch.object(scan, "cli", return_value=(1, None, "no window")):
            self.assertFalse(scan.window_open())


def tab_space(tab="tb1", space="spOct", terminal="t7"):
    """herdr-app ls for one space with one tab."""
    return {"id": space, "name": "Octopilot", "folder": "/home/u/src",
            "tabs": [{"id": tab, "name": "Fix", "settled": True,
                      "terminals": [{"terminal": terminal}]}]}


class HcTabLifecycleTest(unittest.TestCase):
    """HcBackend.close, discard and focus, for a worker's tab and a legacy space."""
    WT = "/home/u/src/repo/.worktrees/feat-x"

    def setUp(self):
        self.replies = {}  # app_do command -> reply
        self.app_calls = []
        mock.patch.object(scan, "app_do", side_effect=self.fake_app).start()
        mock.patch.object(scan, "hc", side_effect=self.fake_hc).start()
        mock.patch.object(scan, "worktree_of", side_effect=lambda f: (
            (self.WT, "/home/u/src/repo", "feat/x") if f.startswith(self.WT) else None
        )).start()
        self.cwds = {"t7": self.WT}
        self.window = True
        mock.patch.object(scan, "window_open", side_effect=lambda: self.window).start()
        self.labelled = {}  # hc ls --label: "herdr.tab=tb1" -> terminal records
        mock.patch.object(scan.time, "sleep").start()
        self.git = mock.patch.object(scan, "sh", return_value=(0, "", "")).start()
        mock.patch.object(scan.os.path, "isdir", return_value=True).start()
        self.addCleanup(mock.patch.stopall)
        self.replies = {"tab.close": (0, {}, ""), "space.close": (0, {}, ""),
                        "space.select": (0, {}, ""), "tab.select": (0, {}, ""),
                        "agent.focus": (0, {}, "")}

    def fake_app(self, command, args, timeout=60):
        self.app_calls.append((command, args))
        return self.replies[command]

    def fake_hc(self, args, timeout=60):
        if args[0] == "ls" and "--label" in args:
            label = args[args.index("--label") + 1]
            return 0, {"terminals": self.labelled.get(label, [])}, ""
        if args[0] == "ls":
            return 0, {"terminals": [{"address": a, "cwd": c}
                                     for a, c in self.cwds.items()]}, ""
        if args[0] == "get":
            return 0, {"labels": {}}, ""
        if args[0] == "rm":
            return 0, {}, ""
        raise AssertionError(f"unexpected hc call {args}")

    def steps(self):
        return [c for c, _ in self.app_calls]

    def removed(self):
        return [c for c in self.git.call_args_list if "remove" in c.args[0]]

    def test_close_a_tab_closes_the_tab(self):
        self.assertEqual(scan.HC.close("tb1"), (0, ""))
        self.assertEqual(self.app_calls, [("tab.close", ["--id", "tb1",
                                                         "--confirmed", "true"])])

    def test_close_a_space_closes_the_space(self):
        self.assertEqual(scan.HC.close("sp1"), (0, ""))
        self.assertEqual(self.app_calls, [("space.close", ["--id", "sp1",
                                                           "--confirmed", "true"])])

    def test_focus_a_tab_selects_its_space_then_the_tab_then_the_agent(self):
        with mock.patch.object(scan.HC, "spaces", return_value=[tab_space()]):
            self.assertEqual(scan.HC.focus("tb1", "t7"), (True, ""))
        self.assertEqual(self.app_calls, [
            ("space.select", ["--id", "spOct"]), ("tab.select", ["--id", "tb1"]),
            ("agent.focus", ["--terminal", "t7"])])

    def test_focus_a_tab_without_a_target_stops_at_the_tab(self):
        with mock.patch.object(scan.HC, "spaces", return_value=[tab_space()]):
            self.assertEqual(scan.HC.focus("tb1"), (True, ""))
        self.assertEqual(self.steps(), ["space.select", "tab.select"])

    def test_focus_a_space_selects_it_then_the_agent(self):
        self.assertEqual(scan.HC.focus("sp1", "t7"), (True, ""))
        self.assertEqual(self.app_calls, [("space.select", ["--id", "sp1"]),
                                          ("agent.focus", ["--terminal", "t7"])])

    def test_focus_a_tab_not_open_is_refused(self):
        with mock.patch.object(scan.HC, "spaces", return_value=[tab_space("tb2")]):
            ok, raw = scan.HC.focus("tb1", "t7")
        self.assertFalse(ok)
        self.assertEqual(raw, "tb1 is not open in herdr-app")
        self.assertEqual(self.app_calls, [])

    def test_focus_a_tab_with_no_herdr_app_answer_is_refused(self):
        with mock.patch.object(scan.HC, "spaces", return_value=None):
            self.assertEqual(scan.HC.focus("tb1", "t7"),
                             (False, "herdr-app did not answer"))

    def test_focus_stops_at_the_first_failing_step(self):
        self.replies["tab.select"] = (1, None, "no such tab")
        with mock.patch.object(scan.HC, "spaces", return_value=[tab_space()]):
            self.assertEqual(scan.HC.focus("tb1", "t7"), (False, "no such tab"))
        self.assertEqual(self.steps(), ["space.select", "tab.select"])

    def test_discard_a_tab_closes_it_then_removes_its_worktree(self):
        before = [tab_space()]
        after = [{"id": "spOct", "folder": "/home/u/src", "tabs": []}]
        with mock.patch.object(scan.HC, "spaces", side_effect=[before, after]):
            self.assertEqual(scan.HC.discard("tb1", self.WT), (0, ""))
        self.assertEqual(self.steps(), ["tab.close"])
        self.git.assert_called_once_with(["git", "worktree", "remove", "--force",
                                          self.WT], cwd=self.WT, timeout=180)

    def test_discard_a_tab_finds_its_worktree_when_none_is_on_record(self):
        before = [tab_space()]
        after = [{"id": "spOct", "folder": "/home/u/src", "tabs": []}]
        with mock.patch.object(scan.HC, "spaces", side_effect=[before, after]):
            self.assertEqual(scan.HC.discard("tb1", None), (0, ""))
        self.git.assert_called_once()
        self.assertEqual(self.git.call_args.args[0][-1], self.WT)

    def test_discard_a_tab_still_listed_keeps_the_worktree(self):
        spaces = [tab_space()]
        with mock.patch.object(scan.HC, "spaces", side_effect=[spaces] + [spaces] * 5):
            code, raw = scan.HC.discard("tb1", self.WT)
        self.assertEqual(code, 1)
        self.assertEqual(raw, "still open: its worktree is kept")
        self.git.assert_not_called()

    def test_discard_a_tab_that_lingers_a_moment_is_removed(self):
        spaces = [tab_space()]
        gone = [{"id": "spOct", "folder": "/home/u/src", "tabs": []}]
        with mock.patch.object(scan.HC, "spaces",
                               side_effect=[spaces, spaces, spaces, gone]):
            self.assertEqual(scan.HC.discard("tb1", self.WT), (0, ""))
        self.git.assert_called_once()
        self.assertEqual(scan.time.sleep.call_count, 2)

    def test_discard_a_tab_with_an_unreadable_list_says_so_and_keeps_it(self):
        spaces = [tab_space()]
        with mock.patch.object(scan.HC, "spaces", side_effect=[spaces, None]):
            code, raw = scan.HC.discard("tb1", self.WT)
        self.assertEqual(code, 1)
        self.assertEqual(raw, "herdr-app did not answer: its worktree is kept")
        self.git.assert_not_called()

    def test_discard_a_tab_whose_close_failed_but_is_gone_removes_the_worktree(self):
        self.replies["tab.close"] = (1, None, "busy")
        after = [{"id": "spOct", "folder": "/home/u/src", "tabs": []}]
        with mock.patch.object(scan.HC, "spaces", side_effect=[[tab_space()], after]):
            self.assertEqual(scan.HC.discard("tb1", self.WT), (0, ""))
        self.git.assert_called_once()

    def test_discard_a_tab_whose_close_failed_and_is_listed_reports_it(self):
        self.replies["tab.close"] = (1, None, "busy")
        spaces = [tab_space()]
        with mock.patch.object(scan.HC, "spaces", side_effect=[spaces] * 6):
            self.assertEqual(scan.HC.discard("tb1", self.WT), (1, "busy"))
        self.git.assert_not_called()


    def test_close_without_a_window_ends_and_removes_the_tabs_terminals(self):
        self.window = False
        self.labelled = {"herdr.tab=tb1": [
            {"address": "t7", "finished_ms": None},
            {"address": "t8", "finished_ms": 5}]}
        with mock.patch.object(scan, "end_terminal", return_value=(0, "")) as end, \
                mock.patch.object(scan, "hc", side_effect=self.fake_hc) as hc:
            self.assertEqual(scan.HC.close("tb1"), (0, ""))
        end.assert_called_once_with("t7")
        hc.assert_any_call(["rm", "t8"])
        self.assertEqual(self.app_calls, [])

    def test_close_without_a_window_of_a_space_uses_its_herdr_space_label(self):
        self.window = False
        self.labelled = {"herdr.space=sp1": [{"address": "t7", "finished_ms": 5}]}
        with mock.patch.object(scan, "end_terminal") as end, \
                mock.patch.object(scan, "hc", side_effect=self.fake_hc) as hc:
            self.assertEqual(scan.HC.close("sp1"), (0, ""))
        end.assert_not_called()
        hc.assert_any_call(["ls", "--all", "--json", "--label", "herdr.space=sp1"],
                           timeout=10)
        hc.assert_any_call(["rm", "t7"])

    def test_close_without_a_window_with_no_terminals_is_done(self):
        self.window = False
        with mock.patch.object(scan, "end_terminal") as end:
            self.assertEqual(scan.HC.close("tb1"), (0, ""))
        end.assert_not_called()

    def test_close_without_a_window_and_hc_unanswered_is_an_error(self):
        self.window = False
        with mock.patch.object(scan, "hc", return_value=(1, None, "no core")):
            self.assertEqual(scan.HC.close("tb1"), (1, "hc did not answer"))

    def test_discard_without_a_window_removes_the_worktree_once_unlabelled(self):
        self.window = False
        with mock.patch.object(scan.HcBackend, "spaces", return_value=None):
            self.assertEqual(scan.HC.discard("tb1", self.WT), (0, ""))
        self.git.assert_called_once_with(["git", "worktree", "remove", "--force",
                                          self.WT], cwd=self.WT, timeout=180)

    def test_discard_without_a_window_keeps_the_worktree_of_a_tab_still_running(self):
        self.window = False
        self.labelled = {"herdr.tab=tb1": [{"address": "t7", "finished_ms": None}]}
        with mock.patch.object(scan.HcBackend, "spaces", return_value=None), \
                mock.patch.object(scan, "end_terminal", return_value=(1, "busy")):
            self.assertEqual(scan.HC.discard("tb1", self.WT), (1, "busy"))
        self.git.assert_not_called()

    def test_discard_a_space_closes_it_then_removes_its_worktree(self):
        after = [{"id": "spOther", "folder": "/x", "tabs": []}]
        before = [{"id": "sp1", "folder": self.WT, "tabs": []}]
        with mock.patch.object(scan.HC, "spaces", side_effect=[before, after]):
            self.assertEqual(scan.HC.discard("sp1", self.WT), (0, ""))
        self.assertEqual(self.steps(), ["space.close"])
        self.git.assert_called_once()

    def test_discard_a_space_still_open_keeps_the_worktree(self):
        self.replies["space.close"] = (1, None, "busy")
        before = [{"id": "sp1", "folder": self.WT, "tabs": []}]
        with mock.patch.object(scan.HC, "spaces", side_effect=[before, before]):
            self.assertEqual(scan.HC.discard("sp1", self.WT), (1, "busy"))
        self.git.assert_not_called()


if __name__ == "__main__":
    unittest.main()
