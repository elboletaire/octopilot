"""restore-sessions: reopening the herdr-app workers after a reboot. Nothing reaches
herdr, hc or herdr-app: their calls and the transcripts are faked."""
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from tests import SCRATCH
from tests.fake import fake_world
import scan

# herdr-app ids: a space is sp<hex>, a tab tb<hex> (backend_for reads them).
OPEN_SPACE = "sp0c3"
OLD_TAB, NEW_TAB, OTHER_TAB = "tb0a1", "tb0b2", "tb0d4"
OLD_SPACE, NEW_SPACE = "sp0a1", "sp0b2"
# fake_world answers backend_for with None: restore_sessions gets the real one.
REAL_BACKEND_FOR = scan.backend_for


def tab(tid, *terms):
    return {"id": tid, "terminals": [{"terminal": t, "pane": t} for t in terms]}


class RestoreTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.mkdtemp(prefix="restore-", dir=SCRATCH)
        self.addCleanup(shutil.rmtree, folder, True)
        self.repo = os.path.join(folder, "repo")
        self.wt = os.path.join(folder, "wt")
        os.makedirs(self.repo)
        os.makedirs(self.wt)
        self.order = []
        self.transcripts = set()
        self.spaces = [{"id": OPEN_SPACE, "tabs": []}]
        mock.patch.dict(os.environ, {"WORKER_HOST": "tab"}).start()
        mock.patch.object(scan, "plugin_settings", return_value="").start()
        mock.patch.object(scan, "octopilot_space", return_value=OPEN_SPACE).start()
        mock.patch.object(scan, "transcript_file", side_effect=lambda s: (
            f"/t/{s}.jsonl" if s in self.transcripts else None)).start()
        mock.patch.object(scan.HC, "spaces", side_effect=lambda: self.spaces).start()
        mock.patch.object(scan.HC, "close", side_effect=self.fake_close).start()
        self.spawn_tab = mock.patch.object(
            scan.HC, "spawn_tab", side_effect=self.fake_spawn(NEW_TAB)).start()
        self.spawn_space = mock.patch.object(
            scan.HC, "spawn_space", side_effect=self.fake_spawn(NEW_SPACE)).start()
        self.addCleanup(mock.patch.stopall)

    def fake_close(self, session):
        self.order.append(("close", session))
        return 0, ""

    def fake_end(self, term):
        self.order.append(("end", term))
        return 0, ""

    def fake_spawn(self, new_session):
        def spawn(*args, **kwargs):
            self.order.append(("spawn", args[1] if len(args) > 1 else None))
            host = {"backend": "hc", "session": new_session, "agent": kwargs["name"],
                    "worktree": self.wt}
            if kwargs.get("resume"):
                host["claude_session"] = kwargs["resume"]
            if new_session == NEW_TAB:
                host["space"] = OPEN_SPACE
            return host
        return spawn

    def item(self, k, session, **extra):
        it = {"mode": "fix", "branch": "feat/x", "session": session, "backend": "hc",
              "space": OLD_SPACE, "worktree": self.wt, "agent": "ap-repo-1",
              "phase": "working", "added": 1, "driver": "agent", "pr": 1,
              "claude_session": "c-old", "model": "claude-x", "title": "The title",
              "review_rounds": 2, "reviewed_sha": "abc", "last_report": {"at": 5},
              "hold": {"by": "you"}}
        it.update(extra)
        return it

    def run_restore(self, items, **answers):
        """Saves items to the scratch state and runs restore-sessions.
        -> (exit code, output, the items after, the events written)."""
        out = io.StringIO()
        answers.setdefault("all_sessions", ([], {"hc"}, {OPEN_SPACE}))
        answers.setdefault("backend_for", REAL_BACKEND_FOR)
        with fake_world(**answers):
            scan.save({"items": items,
                       "registry": {"o/repo": {"path": self.repo, "base": "main"}}})
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = scan.restore_sessions([])
            after = scan.load()["items"]
            events = ""
            if os.path.exists(scan.EVENTS_FILE):
                with open(scan.EVENTS_FILE) as fh:
                    events = fh.read()
        return code, out.getvalue(), after, events

    def test_refused_when_herdr_app_does_not_answer(self):
        before = self.item("o/repo#1", OLD_TAB)
        code, out, after, _ = self.run_restore(
            {"o/repo#1": before}, all_sessions=([], set(), set()))
        self.assertEqual(code, 1)
        self.assertIn("herdr-app did not answer", out)
        self.assertEqual(self.spawn_tab.call_count, 0)
        self.assertEqual(after["o/repo#1"], before)

    def test_refused_without_an_octopilot_space(self):
        with mock.patch.object(scan, "octopilot_space", return_value=None):
            code, out, after, _ = self.run_restore(
                {"o/repo#1": self.item("o/repo#1", OLD_TAB)})
        self.assertEqual(code, 1)
        self.assertIn("no Octopilot space open", out)
        self.assertEqual(self.spawn_tab.call_count, 0)

    def test_gone_session_reopens_in_the_octopilot_space_resuming(self):
        self.transcripts = {"c-old"}
        code, out, after, _ = self.run_restore(
            {"o/repo#1": self.item("o/repo#1", OLD_TAB)})
        self.assertEqual(code, 0)
        args, kwargs = self.spawn_tab.call_args
        self.assertEqual(args[:6], (OPEN_SPACE, "o/repo#1", self.repo, self.wt,
                                    "The title", "claude-x"))
        self.assertEqual(kwargs["name"], "ap-repo-1")
        self.assertEqual(kwargs["resume"], "c-old")
        self.assertFalse(kwargs["cont"])
        self.assertIn(f"o/repo#1: restored in {NEW_TAB} (resumed c-old)", out)

    def test_only_four_keys_change(self):
        self.transcripts = {"c-old"}
        before = self.item("o/repo#1", OLD_TAB)
        _, _, after, _ = self.run_restore({"o/repo#1": before})
        now = after["o/repo#1"]
        self.assertEqual((now["session"], now["space"], now["backend"],
                          now["claude_session"]),
                         (NEW_TAB, OPEN_SPACE, "hc", "c-old"))
        keys = ("session", "space", "backend", "claude_session")
        self.assertEqual({k: v for k, v in now.items() if k not in keys},
                         {k: v for k, v in before.items() if k not in keys})

    def test_without_a_transcript_the_last_conversation_continues(self):
        code, out, after, _ = self.run_restore(
            {"o/repo#1": self.item("o/repo#1", OLD_TAB)})
        kwargs = self.spawn_tab.call_args[1]
        self.assertIsNone(kwargs["resume"])
        self.assertTrue(kwargs["cont"])
        self.assertIn("continued the last conversation", out)
        self.assertNotIn("claude_session", after["o/repo#1"])

    def test_a_rehomed_agent_is_skipped(self):
        before = self.item("o/repo#1", OLD_TAB)
        sessions = [{"id": OTHER_TAB, "space": OPEN_SPACE, "path": self.wt}]
        live = {OTHER_TAB: {"session": OTHER_TAB, "agent": "ap-repo-1",
                            "state": "idle"}}
        _, out, after, _ = self.run_restore(
            {"o/repo#1": before},
            all_sessions=(sessions, {"hc"}, {OTHER_TAB, OPEN_SPACE}),
            all_live=live)
        self.assertEqual(self.spawn_tab.call_count, 0)
        self.assertIn(f"its agent is live in {OTHER_TAB}, left alone", out)
        self.assertEqual(after["o/repo#1"], before)

    def test_a_parked_agent_is_skipped(self):
        before = self.item("o/repo#1", OLD_TAB)
        _, out, after, _ = self.run_restore(
            {"o/repo#1": before},
            parked_agent={"pane": "t9", "agent": "ap-repo-1", "ids": []})
        self.assertEqual(self.spawn_tab.call_count, 0)
        self.assertIn("parked in terminal t9, left alone", out)
        self.assertEqual(after["o/repo#1"], before)

    def test_a_tab_of_bare_shells_is_ended_closed_then_reopened(self):
        self.spaces = [{"id": OPEN_SPACE, "tabs": [tab(OLD_TAB, "t1", "t2")]}]
        code, out, after, _ = self.run_restore(
            {"o/repo#1": self.item("o/repo#1", OLD_TAB)},
            all_sessions=([], {"hc"}, {OLD_TAB, OPEN_SPACE}),
            terminal_state=lambda term, now_ms: "bare",
            end_terminal=self.fake_end)
        self.assertEqual(self.order, [("end", "t1"), ("end", "t2"),
                                      ("close", OLD_TAB), ("spawn", "o/repo#1")])
        self.assertEqual(after["o/repo#1"]["session"], NEW_TAB)
        self.assertIn(f"restored in {NEW_TAB}", out)

    def test_a_tab_with_a_non_shell_terminal_is_left_alone(self):
        self.spaces = [{"id": OPEN_SPACE, "tabs": [tab(OLD_TAB, "t1", "t2")]}]
        before = self.item("o/repo#1", OLD_TAB)
        _, out, after, _ = self.run_restore(
            {"o/repo#1": before},
            all_sessions=([], {"hc"}, {OLD_TAB, OPEN_SPACE}),
            terminal_state=lambda term, now_ms: "busy" if term == "t2" else "bare",
            end_terminal=self.fake_end)
        self.assertEqual(self.order, [])
        self.assertEqual(self.spawn_tab.call_count, 0)
        self.assertIn(f"terminal t2 in {OLD_TAB} runs something other than a shell",
                      out)
        self.assertEqual(after["o/repo#1"], before)

    def test_an_open_tab_with_a_live_agent_is_left_alone(self):
        self.spaces = [{"id": OPEN_SPACE, "tabs": [tab(OLD_TAB, "t1")]}]
        before = self.item("o/repo#1", OLD_TAB)
        _, out, _, _ = self.run_restore(
            {"o/repo#1": before},
            all_sessions=([], {"hc"}, {OLD_TAB, OPEN_SPACE}),
            all_live={OLD_TAB: {"session": OLD_TAB, "agent": "ap-repo-1"}},
            terminal_state=lambda term, now_ms: "bare",
            end_terminal=self.fake_end)
        self.assertEqual(self.order, [])
        self.assertIn(f"its agent is live in {OLD_TAB}, left alone", out)

    def test_a_herdr_tui_item_is_skipped_with_a_hint(self):
        before = self.item("o/repo#1", "w12:t3", backend="herdr")
        _, out, after, _ = self.run_restore({"o/repo#1": before})
        self.assertEqual(self.spawn_tab.call_count, 0)
        self.assertIn("TUI", out)
        self.assertIn("prompter skill", out)
        self.assertEqual(after["o/repo#1"], before)

    def test_a_missing_worktree_is_skipped(self):
        missing = self.wt + "-gone"
        before = self.item("o/repo#1", OLD_TAB, worktree=missing)
        _, out, after, _ = self.run_restore({"o/repo#1": before})
        self.assertEqual(self.spawn_tab.call_count, 0)
        self.assertIn(f"worktree {missing} is missing", out)
        self.assertEqual(after["o/repo#1"], before)

    def test_a_space_of_its_own_is_reopened_as_a_space(self):
        self.transcripts = {"c-old"}
        with mock.patch.dict(os.environ, {"WORKER_HOST": "space"}):
            _, _, after, _ = self.run_restore(
                {"o/repo#1": self.item("o/repo#1", OLD_SPACE)})
        self.assertEqual(self.spawn_tab.call_count, 0)
        self.assertEqual(self.spawn_space.call_args[1]["resume"], "c-old")
        self.assertEqual(after["o/repo#1"]["session"], NEW_SPACE)
        self.assertNotIn("space", after["o/repo#1"])

    def test_a_restore_logs_an_event(self):
        _, _, _, events = self.run_restore(
            {"o/repo#1": self.item("o/repo#1", OLD_TAB)})
        self.assertIn('"kind": "restore"', events)


class RestoreModelTest(unittest.TestCase):
    def test_the_model_of_each_kind(self):
        cases = [
            ("o/repo!3", {}, scan.MODEL_FOR["work"]),
            ("o/repo?look", {}, scan.MODEL_FOR["investigate"]),
            ("o/repo#4", {"mode": "fix"}, scan.MODEL_FOR["review-fix"]),
            ("o/repo#4", {"mode": "fix", "stack": [{"pr": 4}]},
             scan.MODEL_FOR["adopt-stack"]),
            ("o/repo#4", {"mode": "comment"}, scan.MODEL_FOR["review-comment"]),
            ("o/repo#4", {"mode": "comment", "stack": [{"pr": 4}]},
             scan.MODEL_FOR["review-stack"]),
        ]
        for k, extra, model in cases:
            self.assertEqual(scan.restore_model(k, extra), model, k)

    def test_an_items_own_model_wins(self):
        self.assertEqual(scan.restore_model("o/repo#4", {"mode": "fix", "model": "m-x"}),
                         "m-x")


class SpawnResumeTest(unittest.TestCase):
    def test_a_resumed_tab_passes_resume_and_no_new_session(self):
        with mock.patch.object(scan, "app_do", return_value=(
                    0, {"terminal": "t1", "tab": NEW_TAB}, "")) as app_do, \
                mock.patch.object(scan.HC, "start_agent", return_value=None), \
                mock.patch.object(scan, "plugin_settings", return_value=""):
            host = scan.HC.spawn_tab(OPEN_SPACE, "o/repo#1", "/repo", "/wt", "T", "m",
                                     name="ap-repo-1", resume="c-old")
        new_tab = next(c.args[1] for c in app_do.call_args_list if c.args[0] == "tab.new")
        argv = json.loads(new_tab[new_tab.index("--argv") + 1])
        self.assertIn("--resume", argv)
        self.assertNotIn("--session-id", argv)
        self.assertEqual(host["claude_session"], "c-old")
        self.assertEqual(host["session"], NEW_TAB)


class RestoreWiringTest(unittest.TestCase):
    def test_restore_is_self_locking(self):
        self.assertIn("restore-sessions", scan.SELF_LOCKING)


if __name__ == "__main__":
    unittest.main()
