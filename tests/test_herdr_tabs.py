"""The TUI backend's worker tabs: listing, target, focus, close, discard and spawn,
each beside the legacy workspace path. herdr's CLI is faked, never called."""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from tests import SCRATCH  # noqa: F401  (imports the scratch state before scan)
import scan

OK = (0, {"result": {}}, "")


def created(tab, pane):
    return (0, {"result": {"type": "tab_created", "tab": {"tab_id": tab},
                           "root_pane": {"pane_id": pane}}}, "")


def refused(code):
    return (1, {"error": {"code": code}}, code)


def same_session(agent, session):
    """A stand-in for scan.session_matches (the tabs-shared branch adds it)."""
    return session in (agent.get("ids") or [])


class FakeHerdr:
    """herdr's CLI: the first answer whose argv starts with the key wins."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, args, timeout=60):
        self.calls.append(list(args))
        for prefix, reply in self.answers.items():
            if tuple(args[:len(prefix)]) == prefix:
                return reply(args) if callable(reply) else reply
        return 1, None, f"no answer for {args}"

    def argvs(self, *prefix):
        return [c for c in self.calls if tuple(c[:len(prefix)]) == prefix]


class BackendCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="tabs-", dir=SCRATCH)
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.wt = os.path.join(self.folder, "wt")
        os.makedirs(self.wt)
        self.herdr_answers = {}
        self.patch(scan, "octopilot_workspace", return_value="w96", create=True)
        self.patch(scan, "session_matches", side_effect=same_session, create=True)
        self.patch(scan, "agent_name", return_value="ap-x-1")
        self.patch(scan.shutil, "which", return_value="/usr/bin/herdr")
        self.patch(scan, "time")
        self.patch(scan, "accept_trust", return_value=True)
        self.patch(scan, "sh", return_value=(0, "", ""))
        self.env = self.patch_env()

    def patch(self, obj, name, *args, **kwargs):
        patcher = mock.patch.object(obj, name, *args, **kwargs)
        patched = patcher.start()
        self.addCleanup(patcher.stop)
        return patched

    def patch_env(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for var in ("WORKER_HOST", "OCTOPILOT_DIR", "OCTOPILOT_STATE_DIR"):
            os.environ.pop(var, None)
        return os.environ

    def herdr(self, answers):
        fake = FakeHerdr(answers)
        self.patch(scan, "herdr", side_effect=fake)
        return fake


class AgentsIdsTest(BackendCase):
    def test_agents_carry_their_workspace_and_tab_ids(self):
        self.herdr({("agent", "list"): (0, {"result": {"agents": [
            {"workspace_id": "w96", "tab_id": "w96:t2", "name": "ap-x-1",
             "pane_id": "w96:p2", "agent_status": "idle"}]}}, "")})
        [agent] = scan.HERDR.agents()
        self.assertEqual(agent["ids"], ["w96", "w96:t2"])
        self.assertEqual(agent["session"], "w96")
        self.assertEqual(agent["agent"], "ap-x-1")


class TargetTest(BackendCase):
    def test_target_matches_the_tab_of_the_item(self):
        fake = self.herdr({
            ("agent", "list"): (0, {"result": {"agents": [
                {"workspace_id": "w96", "tab_id": "w96:t1", "name": "octopilot-code",
                 "pane_id": "w96:p1"},
                {"workspace_id": "w96", "tab_id": "w96:t2", "name": "ap-x-1",
                 "pane_id": "w96:p2"}]}}, ""),
            ("agent", "get"): refused("not_found"),
        })
        self.assertEqual(scan.HERDR.target({"session": "w96:t2", "agent": None}),
                         "ap-x-1")
        self.assertEqual(fake.argvs("agent", "get"), [])

    def test_target_falls_back_to_the_pane_of_an_unnamed_agent(self):
        self.herdr({("agent", "list"): (0, {"result": {"agents": [
            {"workspace_id": "w96", "tab_id": "w96:t2", "name": None,
             "pane_id": "w96:p2"}]}}, "")})
        self.assertEqual(scan.HERDR.target({"session": "w96:t2"}), "w96:p2")

    def test_target_is_none_without_an_agent_in_the_tab(self):
        self.herdr({("agent", "list"): (0, {"result": {"agents": []}}, "")})
        self.assertIsNone(scan.HERDR.target({"session": "w96:t2"}))


class ListingTest(BackendCase):
    def setUp(self):
        super().setUp()
        self.tabs = [{"tab_id": "w1:t1", "workspace_id": "w1", "label": "ws"},
                     {"tab_id": "w1:t2", "workspace_id": "w1", "label": "other"},
                     {"tab_id": "w96:t1", "workspace_id": "w96", "label": "OCTOPILOT"},
                     {"tab_id": "w96:t2", "workspace_id": "w96", "label": "fix"},
                     {"tab_id": "w96:t3", "workspace_id": "w96", "label": "code"},
                     {"tab_id": "w97:t1", "workspace_id": "w97", "label": "work"}]
        # Panes carry the agent's kind ("claude"), not its name.
        self.panes = [
            # A linked workspace's own pane: its workspace row covers it.
            {"pane_id": "w1:p1", "tab_id": "w1:t1", "workspace_id": "w1",
             "cwd": "/wt/ws", "agent": "claude"},
            # Another worktree's tab in that linked workspace is a worker of its own.
            {"pane_id": "w1:p2", "tab_id": "w1:t2", "workspace_id": "w1",
             "cwd": "/wt/ws2", "agent": "claude"},
            # A plain checkout: not a worktree, so no row.
            {"pane_id": "w96:p1", "tab_id": "w96:t1", "workspace_id": "w96",
             "cwd": "/home/x", "agent": None},
            # A worker tab on a worktree in the Octopilot workspace.
            {"pane_id": "w96:p2", "tab_id": "w96:t2", "workspace_id": "w96",
             "cwd": "/wt/a", "agent": "claude"},
            # The code tab runs in the checkout, which may be a worktree itself.
            {"pane_id": "w96:p3", "tab_id": "w96:t3", "workspace_id": "w96",
             "cwd": "/wt/code", "agent": "claude"},
            # A worker tab in another workspace; its second pane adds nothing.
            {"pane_id": "w97:p1", "tab_id": "w97:t1", "workspace_id": "w97",
             "cwd": "/wt/b", "agent": "claude"},
            {"pane_id": "w97:p2", "tab_id": "w97:t1", "workspace_id": "w97",
             "cwd": "/wt/b", "agent": "claude"},
        ]
        self.agents = [
            {"name": "ap-x-1", "pane_id": "w96:p2", "workspace_id": "w96",
             "tab_id": "w96:t2", "agent_status": "idle"},
            {"name": "octopilot-code", "pane_id": "w96:p3", "workspace_id": "w96",
             "tab_id": "w96:t3", "agent_status": "idle"},
        ]
        self.checkouts = {"/wt/ws": ("/wt/ws", "/repo", "feat/ws"),
                          "/wt/ws2": ("/wt/ws2", "/repo", "fix/ws2"),
                          "/wt/a": ("/wt/a", "/repo", "fix/a"),
                          "/wt/code": ("/wt/code", "/repo", "main"),
                          "/wt/b": ("/wt/b", "/repo", "feat/b"),
                          "/home/x": None}
        self.workspaces = [
            {"workspace_id": "w1", "label": "ws",
             "worktree": {"is_linked_worktree": True, "repo_root": "/repo",
                          "checkout_path": "/wt/ws"}},
            {"workspace_id": "w96", "label": "OCTOPILOT",
             "worktree": {"is_linked_worktree": False}},
            {"workspace_id": "w97", "label": "work", "worktree": {}}]
        self.patch(scan, "worktree_of", side_effect=lambda cwd: self.checkouts[cwd])

    def answer(self):
        return self.herdr({
            ("workspace", "list"): (0, {"result": {"workspaces": self.workspaces}}, ""),
            ("tab", "list"): (0, {"result": {"tabs": self.tabs}}, ""),
            ("pane", "list"): (0, {"result": {"panes": self.panes}}, ""),
            ("agent", "list"): (0, {"result": {"agents": self.agents}}, ""),
            ("worktree", "list"): (0, {"result": {"worktrees": [
                {"open_workspace_id": "w1", "branch": "feat/ws"}]}}, ""),
        })

    def test_lists_worker_tabs_beside_linked_workspaces(self):
        self.answer()
        rows, answered, ids = scan.HERDR.listing()
        self.assertTrue(answered)
        self.assertEqual(rows, [
            {"id": "w1", "title": "ws", "path": "/wt/ws", "backend": "herdr",
             "worktree": {"branch": "feat/ws", "main_repo_path": "/repo"}},
            {"id": "w1:t2", "title": "other", "path": "/wt/ws2", "backend": "herdr",
             "space": "w1",
             "worktree": {"branch": "fix/ws2", "main_repo_path": "/repo"}},
            {"id": "w96:t2", "title": "fix", "path": "/wt/a", "backend": "herdr",
             "space": "w96",
             "worktree": {"branch": "fix/a", "main_repo_path": "/repo"}},
            {"id": "w97:t1", "title": "work", "path": "/wt/b", "backend": "herdr",
             "space": "w97",
             "worktree": {"branch": "feat/b", "main_repo_path": "/repo"}},
        ])
        self.assertEqual(ids, {"w1", "w96", "w97", "w1:t1", "w1:t2", "w96:t1",
                               "w96:t2", "w96:t3", "w97:t1"})

    def test_a_launcher_is_skipped_by_its_pane_not_its_kind(self):
        # Every pane is a "claude"; only the code agent's pane is the launcher's.
        self.answer()
        rows, _, _ = scan.HERDR.listing()
        self.assertNotIn("w96:t3", [r["id"] for r in rows])
        self.assertIn("w96:t2", [r["id"] for r in rows])

    def test_a_failed_agent_list_leaves_the_launcher_unrecognised(self):
        self.herdr({
            ("workspace", "list"): (0, {"result": {"workspaces": self.workspaces}}, ""),
            ("tab", "list"): (0, {"result": {"tabs": self.tabs}}, ""),
            ("pane", "list"): (0, {"result": {"panes": self.panes}}, ""),
            ("agent", "list"): (1, None, "boom"),
            ("worktree", "list"): (0, {"result": {"worktrees": []}}, ""),
        })
        rows, answered, _ = scan.HERDR.listing()
        self.assertTrue(answered)
        self.assertIn("w96:t3", [r["id"] for r in rows])

    def test_a_failed_tab_list_leaves_the_listing_unanswered(self):
        fake = self.herdr({
            ("workspace", "list"): (0, {"result": {"workspaces": self.workspaces}}, ""),
            ("tab", "list"): (1, None, "boom"),
            ("pane", "list"): (0, {"result": {"panes": self.panes}}, ""),
        })
        rows, answered, _ = scan.HERDR.listing()
        self.assertEqual(rows, [])
        self.assertFalse(answered)
        self.assertEqual(len(fake.argvs("pane", "list")), 1)

    def test_checkout_of_finds_a_worker_tab(self):
        self.answer()
        self.assertEqual(scan.HERDR.checkout_of("w96:t2"), "/wt/a")
        self.assertIsNone(scan.HERDR.checkout_of("w96:t1"))


class FocusCloseDiscardTest(BackendCase):
    def test_focus_brings_the_agent_forward_when_given(self):
        fake = self.herdr({("agent", "focus"): OK, ("tab", "focus"): OK})
        self.assertEqual(scan.HERDR.focus("w96:t2", "ap-x-1")[0], True)
        self.assertEqual(fake.calls, [["agent", "focus", "ap-x-1"]])

    def test_focus_without_an_agent_selects_the_tab(self):
        fake = self.herdr({("tab", "focus"): OK})
        self.assertTrue(scan.HERDR.focus("w96:t2", None)[0])
        self.assertEqual(fake.calls, [["tab", "focus", "w96:t2"]])

    def test_focus_without_an_agent_selects_a_workspace(self):
        fake = self.herdr({("workspace", "focus"): OK})
        self.assertTrue(scan.HERDR.focus("w96", None)[0])
        self.assertEqual(fake.calls, [["workspace", "focus", "w96"]])

    def test_close_closes_the_tab_or_the_workspace(self):
        fake = self.herdr({("tab", "close"): OK, ("workspace", "close"): OK})
        scan.HERDR.close("w96:t2")
        scan.HERDR.close("w96")
        self.assertEqual(fake.calls, [["tab", "close", "w96:t2"],
                                      ["workspace", "close", "w96"]])

    def test_discard_closes_the_tab_then_removes_the_checkout(self):
        fake = self.herdr({("tab", "close"): OK})
        self.assertEqual(scan.HERDR.discard("w96:t2", self.wt), (0, ""))
        self.assertEqual(fake.calls, [["tab", "close", "w96:t2"]])
        scan.sh.assert_called_once_with(["git", "worktree", "remove", "--force",
                                         self.wt], cwd=self.wt, timeout=180)

    def test_discard_keeps_the_checkout_when_the_tab_stays_open(self):
        self.herdr({("tab", "close"): (1, None, "still open")})
        self.assertEqual(scan.HERDR.discard("w96:t2", self.wt), (1, "still open"))
        scan.sh.assert_not_called()

    def test_discard_removes_the_checkout_of_a_tab_already_closed(self):
        self.herdr({("tab", "close"): refused("tab_not_found")})
        self.assertEqual(scan.HERDR.discard("w96:t2", self.wt), (0, ""))
        scan.sh.assert_called_once_with(["git", "worktree", "remove", "--force",
                                         self.wt], cwd=self.wt, timeout=180)

    def test_discard_of_a_tab_already_closed_with_no_checkout_is_done(self):
        self.herdr({("tab", "close"): refused("tab_not_found")})
        self.assertEqual(scan.HERDR.discard("w96:t2", "/no/such/dir"), (0, ""))
        scan.sh.assert_not_called()

    def test_discard_of_a_tracked_tab_looks_up_its_checkout(self):
        self.herdr({("tab", "close"): OK})
        with mock.patch.object(scan.HERDR, "checkout_of", return_value=self.wt) as look:
            scan.HERDR.discard("w96:t2", None)
        look.assert_called_once_with("w96:t2")
        scan.sh.assert_called_once()

    def test_discard_of_a_workspace_still_uses_worktree_remove(self):
        fake = self.herdr({("worktree", "remove"): OK})
        scan.HERDR.discard("w96", self.wt)
        self.assertEqual(fake.calls, [["worktree", "remove", "--workspace", "w96",
                                       "--force"]])


class SpawnTabTest(BackendCase):
    def spawn(self, herdr_answers=None, title="fix title", model=None):
        answers = {("tab", "create"): created("w96:t9", "w96:p9"),
                   ("agent", "start"): OK}
        answers.update(herdr_answers or {})
        fake = self.herdr(answers)
        host = scan.HERDR.spawn("fix#1", "/repo", self.wt, title, model)
        return fake, host

    def test_spawns_a_tab_of_the_octopilot_workspace(self):
        fake, host = self.spawn()
        self.assertEqual(fake.calls[0], ["tab", "create", "--workspace", "w96",
                                         "--cwd", self.wt, "--label", "fix title",
                                         "--no-focus",
                                         "--env", f"OCTOPILOT_DIR={scan.OCTOPILOT_DIR}",
                                         "--env", "OCTOPILOT_STATE_DIR="
                                         + os.path.dirname(scan.STATE_FILE)])
        self.assertEqual(host, {"backend": "herdr", "session": "w96:t9",
                                "space": "w96", "agent": "ap-x-1",
                                "worktree": self.wt})
        self.assertEqual(fake.argvs("agent", "start")[0][:6],
                         ["agent", "start", "ap-x-1", "--kind", "claude", "--pane"])
        self.assertIn("w96:p9", fake.argvs("agent", "start")[0])

    def test_passes_the_octopilot_env_whether_or_not_it_is_set_here(self):
        self.env.pop("OCTOPILOT_DIR", None)
        self.env.pop("OCTOPILOT_STATE_DIR", None)
        fake, _ = self.spawn()
        self.assertIn(f"OCTOPILOT_DIR={scan.OCTOPILOT_DIR}", fake.calls[0])
        self.assertIn("OCTOPILOT_STATE_DIR=" + os.path.dirname(scan.STATE_FILE),
                      fake.calls[0])

    def test_a_reserved_title_gets_the_agent_name(self):
        for title in ("engine", "OCTOPILOT", "Octopilot (code changes)"):
            fake, _ = self.spawn(title=title)
            label = fake.calls[0][fake.calls[0].index("--label") + 1]
            self.assertEqual(label, f"{title} (ap-x-1)")

    def test_a_model_is_passed_to_claude(self):
        fake, _ = self.spawn(model="claude-opus-5-5[1m]")
        self.assertEqual(fake.argvs("agent", "start")[0][-3:],
                         ["--", "--model", "claude-opus-5-5[1m]"])

    def test_a_failed_tab_create_stops_the_spawn(self):
        self.herdr({("tab", "create"): (1, None, "no room")})
        with self.assertRaises(SystemExit) as ctx:
            scan.HERDR.spawn("fix#1", "/repo", self.wt, "t", None)
        self.assertEqual(str(ctx.exception), "herdr tab create failed: no room")

    def test_a_failed_agent_start_keeps_the_tab(self):
        self.herdr({("tab", "create"): created("w96:t9", "w96:p9"),
                     ("agent", "start"): (1, {"error": {"code": "boom"}}, "boom")})
        with self.assertRaises(SystemExit) as ctx:
            scan.HERDR.spawn("fix#1", "/repo", self.wt, "t", None)
        self.assertEqual(str(ctx.exception),
                         "herdr agent start failed (tab w96:t9 kept): boom")

    def test_an_agent_not_ready_is_accepted_through_the_trust_dialog(self):
        fake, host = self.spawn({("agent", "start"): refused("agent_not_ready")})
        self.assertEqual(len(fake.argvs("agent", "start")), 1)
        scan.accept_trust.assert_called_once_with(scan.HERDR, "ap-x-1", self.wt, "/repo")
        self.assertNotIn("warning", host)

    def test_another_failed_agent_start_is_retried_three_times(self):
        fake = self.herdr({("tab", "create"): created("w96:t9", "w96:p9"),
                           ("agent", "start"): refused("boom")})
        with self.assertRaises(SystemExit):
            scan.HERDR.spawn("fix#1", "/repo", self.wt, "t", None)
        self.assertEqual(len(fake.argvs("agent", "start")), 3)

    def test_a_blocked_agent_warns_with_the_tab(self):
        scan.accept_trust.return_value = False
        _, host = self.spawn({("agent", "start"): refused("agent_not_ready")})
        self.assertEqual(host["warning"], "agent ap-x-1 is blocked at startup; "
                                          "check tab w96:t9 before sending it a prompt")

    def test_a_tab_spawn_never_opens_a_workspace(self):
        fake, _ = self.spawn()
        self.assertEqual(fake.argvs("worktree", "open"), [])


class SpawnWorkspaceTest(BackendCase):
    """The legacy path: WORKER_HOST=space, or no Octopilot workspace to put a tab in."""

    def open_answers(self, agent_start=OK):
        return {("worktree", "open"): (0, {"result": {
                    "workspace": {"workspace_id": "w12"},
                    "root_pane": {"pane_id": "w12:p1"}}}, ""),
                ("agent", "start"): agent_start}

    def test_space_host_opens_a_workspace(self):
        self.env["WORKER_HOST"] = "space"
        fake = self.herdr(self.open_answers())
        host = scan.HERDR.spawn("fix#1", "/repo", self.wt, "fix title", None)
        self.assertEqual(fake.calls[0], ["worktree", "open", "--cwd", "/repo",
                                         "--path", self.wt, "--label", "fix title",
                                         "--no-focus"])
        self.assertEqual(host, {"backend": "herdr", "session": "w12",
                                "agent": "ap-x-1", "worktree": self.wt})
        self.assertEqual(fake.argvs("tab", "create"), [])

    def test_no_octopilot_workspace_opens_a_workspace_with_a_note(self):
        scan.octopilot_workspace.return_value = None
        fake = self.herdr(self.open_answers())
        host = scan.HERDR.spawn("fix#1", "/repo", self.wt, "t", None)
        self.assertEqual(host["note"], "no Octopilot workspace: the worker opens "
                                       "as its own workspace")
        self.assertNotIn("warning", host)
        self.assertEqual(host["session"], "w12")
        self.assertEqual(fake.argvs("tab", "create"), [])

    def test_a_failed_workspace_open_stops_the_spawn(self):
        self.env["WORKER_HOST"] = "space"
        self.herdr({("worktree", "open"): (1, None, "no")})
        with self.assertRaises(SystemExit) as ctx:
            scan.HERDR.spawn("fix#1", "/repo", self.wt, "t", None)
        self.assertEqual(str(ctx.exception), "herdr worktree open failed: no")

    def test_a_failed_agent_start_keeps_the_workspace(self):
        self.env["WORKER_HOST"] = "space"
        self.herdr(self.open_answers(
            agent_start=(1, {"error": {"code": "boom"}}, "boom")))
        with self.assertRaises(SystemExit) as ctx:
            scan.HERDR.spawn("fix#1", "/repo", self.wt, "t", None)
        self.assertEqual(str(ctx.exception),
                         "herdr agent start failed (workspace w12 kept): boom")

    def test_a_blocked_agent_warns_with_the_workspace(self):
        self.env["WORKER_HOST"] = "space"
        scan.accept_trust.return_value = False
        self.herdr(self.open_answers(agent_start=refused("agent_not_ready")))
        host = scan.HERDR.spawn("fix#1", "/repo", self.wt, "t", None)
        self.assertEqual(host["warning"], "agent ap-x-1 is blocked at startup; check "
                                          "workspace w12 before sending it a prompt")


class TabLabelTest(unittest.TestCase):
    def test_only_the_launchers_titles_take_the_agent_name(self):
        self.assertEqual(scan.tab_label("engine", "ap-x-1"), "engine (ap-x-1)")
        self.assertEqual(scan.tab_label("octopilot", "ap-x-1"), "octopilot (ap-x-1)")
        self.assertEqual(scan.tab_label("vocdoni-app#4 fix", "ap-x-1"),
                         "vocdoni-app#4 fix")


class AddNoteTest(unittest.TestCase):
    def test_a_note_is_kept_apart_from_a_startup_warning(self):
        host = scan.add_note({"warning": "blocked"}, "a")
        self.assertEqual(host, {"warning": "blocked", "note": "a"})


if __name__ == "__main__":
    unittest.main()
