"""restore-sessions: reopening the herdr-app workers after a reboot. Nothing reaches
herdr, hc or herdr-app: their calls and the transcripts are faked."""
import contextlib
import io
import json
import os
import re
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
        self.projects = os.path.join(folder, "projects")
        mock.patch.object(scan, "PROJECTS_DIR", self.projects).start()
        mock.patch.object(scan, "octopilot_space", return_value=OPEN_SPACE).start()
        mock.patch.object(scan, "transcript_file", side_effect=lambda s: (
            f"/t/{s}.jsonl" if s in self.transcripts else None)).start()
        mock.patch.object(scan.HC, "spaces", side_effect=lambda: self.spaces).start()
        mock.patch.object(scan.HC, "close", side_effect=self.fake_close).start()
        self.spawn_tab = mock.patch.object(
            scan.HC, "spawn_tab", side_effect=self.fake_spawn(NEW_TAB)).start()
        self.spawn_space = mock.patch.object(
            scan.HC, "spawn_space", side_effect=self.fake_spawn(NEW_SPACE)).start()
        self.hc_start = mock.patch.object(
            scan, "hc_start", side_effect=self.fake_hc_start).start()
        mock.patch.object(scan.HC, "start_agent", return_value=None).start()
        self.addCleanup(mock.patch.stopall)

    def write_transcript(self, wt):
        """A transcript in the worktree's claude project folder, as claude names it."""
        folder = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(wt))
        os.makedirs(os.path.join(self.projects, folder), exist_ok=True)
        with open(os.path.join(self.projects, folder, "t.jsonl"), "w"):
            pass

    def fake_close(self, session):
        self.order.append(("close", session))
        return 0, ""

    def fake_end(self, term):
        self.order.append(("end", term))
        return 0, ""

    def fake_hc_start(self, argv, cwd, labels, keep):
        self.order.append(("start", labels["herdr.tab"]))
        return "tnew"

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
        self.assertIn("herdr-core did not answer", out)
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

    def test_a_worktree_transcript_continues_the_last_conversation(self):
        self.write_transcript(self.wt)
        code, out, after, _ = self.run_restore(
            {"o/repo#1": self.item("o/repo#1", OLD_TAB)})
        kwargs = self.spawn_tab.call_args[1]
        self.assertIsNone(kwargs["resume"])
        self.assertTrue(kwargs["cont"])
        self.assertIn("continued the last conversation", out)
        self.assertNotIn("claude_session", after["o/repo#1"])

    def test_without_any_transcript_it_starts_fresh(self):
        code, out, after, _ = self.run_restore(
            {"o/repo#1": self.item("o/repo#1", OLD_TAB)})
        kwargs = self.spawn_tab.call_args[1]
        self.assertIsNone(kwargs["resume"])
        self.assertFalse(kwargs["cont"])
        self.assertIn("started fresh, no transcript to resume", out)
        self.assertNotIn("claude_session", after["o/repo#1"])

    def test_a_bare_tab_whose_agent_is_live_elsewhere_is_not_closed(self):
        self.spaces = [{"id": OPEN_SPACE, "tabs": [tab(OLD_TAB, "t1")]}]
        before = self.item("o/repo#1", OLD_TAB)
        sessions = [{"id": OTHER_TAB, "space": OPEN_SPACE, "path": self.wt}]
        live = {OTHER_TAB: {"session": OTHER_TAB, "agent": "ap-repo-1",
                            "state": "idle"}}
        _, out, after, _ = self.run_restore(
            {"o/repo#1": before},
            all_sessions=(sessions, {"hc"}, {OLD_TAB, OTHER_TAB, OPEN_SPACE}),
            all_live=live,
            terminal_state=lambda term, now_ms: "bare",
            end_terminal=self.fake_end)
        self.assertEqual(self.order, [])
        self.assertEqual(self.spawn_tab.call_count, 0)
        self.assertIn(f"its agent is live in {OTHER_TAB}, left alone", out)
        self.assertEqual(after["o/repo#1"], before)

    def test_a_bare_tab_whose_agent_is_parked_is_not_closed(self):
        self.spaces = [{"id": OPEN_SPACE, "tabs": [tab(OLD_TAB, "t1")]}]
        before = self.item("o/repo#1", OLD_TAB)
        _, out, _, _ = self.run_restore(
            {"o/repo#1": before},
            all_sessions=([], {"hc"}, {OLD_TAB, OPEN_SPACE}),
            parked_agent={"pane": "t9", "agent": "ap-repo-1", "ids": []},
            terminal_state=lambda term, now_ms: "bare",
            end_terminal=self.fake_end)
        self.assertEqual(self.order, [])
        self.assertIn("parked in terminal t9, left alone", out)

    def test_an_empty_tab_with_a_labelled_terminal_is_left_alone(self):
        self.spaces = [{"id": OPEN_SPACE, "tabs": [tab(OLD_TAB)]}]
        before = self.item("o/repo#1", OLD_TAB)
        _, out, after, _ = self.run_restore(
            {"o/repo#1": before},
            all_sessions=([], {"hc"}, {OLD_TAB, OPEN_SPACE}),
            labelled_terminals=lambda t: ["t7"] if t == OLD_TAB else [])
        self.assertEqual(self.order, [])
        self.assertIn(f"a terminal is labelled for {OLD_TAB}", out)
        self.assertEqual(after["o/repo#1"], before)

    def test_an_empty_tab_nothing_labelled_gets_its_claude_in_place(self):
        self.spaces = [{"id": OPEN_SPACE, "tabs": [tab(OLD_TAB)]}]
        _, out, after, _ = self.run_restore(
            {"o/repo#1": self.item("o/repo#1", OLD_TAB)},
            all_sessions=([], {"hc"}, {OLD_TAB, OPEN_SPACE}))
        self.assertEqual(self.order, [("start", OLD_TAB)])
        self.assertEqual(self.spawn_tab.call_count, 0)
        self.assertIn(f"restored in {OLD_TAB}", out)
        self.assertEqual(after["o/repo#1"]["session"], OLD_TAB)

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

    def test_a_tab_of_bare_shells_gets_its_claude_then_the_shells_end(self):
        self.spaces = [{"id": OPEN_SPACE, "tabs": [tab(OLD_TAB, "t1", "t2")]}]
        self.transcripts = {"c-old"}
        before = self.item("o/repo#1", OLD_TAB)
        code, out, after, _ = self.run_restore(
            {"o/repo#1": before},
            all_sessions=([], {"hc"}, {OLD_TAB, OPEN_SPACE}),
            terminal_state=lambda term, now_ms: "bare",
            end_terminal=self.fake_end)
        self.assertEqual(self.order, [("start", OLD_TAB), ("end", "t1"), ("end", "t2")])
        self.assertEqual(self.spawn_tab.call_count, 0)
        argv, _, labels = self.hc_start.call_args.args[:3]
        self.assertTrue(self.hc_start.call_args.kwargs["keep"])
        self.assertIn("--resume", argv)
        self.assertEqual(labels["herdr.space"], OPEN_SPACE)
        self.assertEqual(labels["herdr.tab"], OLD_TAB)
        self.assertEqual(labels["octopilot.agent"], "ap-repo-1")
        now = after["o/repo#1"]
        self.assertEqual((now["session"], now["space"], now["claude_session"]),
                         (OLD_TAB, OPEN_SPACE, "c-old"))
        self.assertIn(f"restored in {OLD_TAB} (resumed c-old)", out)

    def test_a_tab_of_bare_shells_is_never_closed(self):
        self.spaces = [{"id": OPEN_SPACE, "tabs": [tab(OLD_TAB, "t1")]}]
        self.run_restore(
            {"o/repo#1": self.item("o/repo#1", OLD_TAB)},
            all_sessions=([], {"hc"}, {OLD_TAB, OPEN_SPACE}),
            terminal_state=lambda term, now_ms: "bare",
            end_terminal=self.fake_end)
        self.assertNotIn("close", [step[0] for step in self.order])

    def test_a_tab_missing_from_every_space_is_left_alone(self):
        before = self.item("o/repo#1", OLD_TAB)
        _, out, after, _ = self.run_restore(
            {"o/repo#1": before},
            all_sessions=([], {"hc"}, {OLD_TAB, OPEN_SPACE}),
            labelled_terminals=lambda t: [])
        self.assertEqual(self.order, [])
        self.assertEqual(self.hc_start.call_count, 0)
        self.assertIn(f"{OLD_TAB} is in no space, left alone", out)
        self.assertEqual(after["o/repo#1"], before)

    def test_a_space_of_its_own_with_bare_shells_is_ended_closed_reopened(self):
        self.spaces = [{"id": OPEN_SPACE, "tabs": [tab("tb0e5", "t1")]}]
        with mock.patch.dict(os.environ, {"WORKER_HOST": "space"}), \
                mock.patch.object(scan.HC, "close", return_value=(1, "gone")) as close, \
                mock.patch.object(scan.HC, "gone", return_value=True):
            _, out, after, _ = self.run_restore(
                {"o/repo#1": self.item("o/repo#1", OPEN_SPACE)},
                terminal_state=lambda term, now_ms: "bare",
                end_terminal=self.fake_end)
        self.assertEqual([step[0] for step in self.order], ["end", "spawn"])
        close.assert_called_once_with(OPEN_SPACE)
        self.assertEqual(self.hc_start.call_count, 0)
        self.assertEqual(self.spawn_space.call_count, 1)
        self.assertEqual(after["o/repo#1"]["session"], NEW_SPACE)
        self.assertIn(f"restored in {NEW_SPACE}", out)

    def test_a_space_whose_close_fails_while_open_is_left_as_it_is(self):
        self.spaces = [{"id": OPEN_SPACE, "tabs": [tab("tb0e5", "t1")]}]
        before = self.item("o/repo#1", OPEN_SPACE)
        with mock.patch.dict(os.environ, {"WORKER_HOST": "space"}), \
                mock.patch.object(scan.HC, "close", return_value=(1, "busy")), \
                mock.patch.object(scan.HC, "gone", return_value=False):
            _, out, after, _ = self.run_restore(
                {"o/repo#1": before},
                terminal_state=lambda term, now_ms: "bare",
                end_terminal=self.fake_end)
        self.assertEqual(self.spawn_space.call_count, 0)
        self.assertIn(f"closing {OPEN_SPACE} failed: busy, left as it is", out)
        self.assertEqual(after["o/repo#1"], before)

    def test_a_failed_start_in_a_tab_leaves_its_shells_and_the_item(self):
        self.spaces = [{"id": OPEN_SPACE, "tabs": [tab(OLD_TAB, "t1")]}]
        before = self.item("o/repo#1", OLD_TAB)
        self.hc_start.side_effect = SystemExit("hc run failed: boom")
        code, out, after, _ = self.run_restore(
            {"o/repo#1": before},
            all_sessions=([], {"hc"}, {OLD_TAB, OPEN_SPACE}),
            terminal_state=lambda term, now_ms: "bare",
            end_terminal=self.fake_end)
        self.assertEqual(self.order, [])
        self.assertIn(f"reopening in {OLD_TAB} failed: hc run failed: boom", out)
        self.assertIn("its shells are left as they were", out)
        self.assertEqual(after["o/repo#1"], before)

    def test_a_shell_that_does_not_end_is_a_warning_and_the_item_is_restored(self):
        self.spaces = [{"id": OPEN_SPACE, "tabs": [tab(OLD_TAB, "t1", "t2")]}]
        self.transcripts = {"c-old"}
        _, out, after, _ = self.run_restore(
            {"o/repo#1": self.item("o/repo#1", OLD_TAB)},
            all_sessions=([], {"hc"}, {OLD_TAB, OPEN_SPACE}),
            terminal_state=lambda term, now_ms: "bare",
            end_terminal=lambda term: (1, "busy") if term == "t1" else (0, ""))
        self.assertIn(f"restored in {OLD_TAB}", out)
        self.assertIn("WARNING: could not end shell t1: busy", out)
        self.assertEqual(after["o/repo#1"]["claude_session"], "c-old")

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

    def test_a_session_another_claude_has_open_starts_fresh_apart_from_it(self):
        self.transcripts = {"c-old"}
        self.write_transcript(self.wt)
        with mock.patch.object(scan, "session_live", return_value=True):
            _, out, after, _ = self.run_restore(
                {"o/repo#1": self.item("o/repo#1", OLD_TAB)})
        kwargs = self.spawn_tab.call_args[1]
        self.assertIsNone(kwargs["resume"])
        self.assertFalse(kwargs["cont"])
        self.assertIn("started fresh, c-old is open in another claude", out)
        self.assertNotIn("claude_session", after["o/repo#1"])

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

    def test_the_model_of_a_read_only_investigation_and_a_re_review(self):
        models = {"investigate": "m-inv", "investigate-readonly": "m-ro",
                  "re-review": "m-re", "review-comment": "m-cm"}
        with mock.patch.dict(scan.MODEL_FOR, models):
            self.assertEqual(scan.restore_model("o/repo?look", {"read_only": True}), "m-ro")
            self.assertEqual(scan.restore_model("o/repo?look", {}), "m-inv")
            self.assertEqual(scan.restore_model(
                "o/repo#4", {"mode": "comment", "reviewed_sha": "abc"}), "m-re")
            self.assertEqual(scan.restore_model("o/repo#4", {"mode": "comment"}), "m-cm")

    def test_an_items_own_model_wins(self):
        self.assertEqual(scan.restore_model("o/repo#4", {"mode": "fix", "model": "m-x"}),
                         "m-x")


class TerminalStateTest(unittest.TestCase):
    NOW = 1_000_000

    def state(self, created, flag):
        info = (0, {"foreground": {"name": "zsh"}, "created_ms": created}, "")
        env = {"OCTOPILOT_LAUNCHER_LOCKED": "1"} if flag else {}
        with mock.patch.object(scan, "hc", return_value=info), \
                mock.patch.dict(os.environ, env):
            if not flag:
                os.environ.pop("OCTOPILOT_LAUNCHER_LOCKED", None)
            return scan.terminal_state("t1", self.NOW)

    def test_a_young_shell_is_young_until_the_launcher_marks_the_restore(self):
        self.assertEqual(self.state(self.NOW - 1000, flag=False), "young")
        self.assertEqual(self.state(self.NOW - 1000, flag=True), "bare")
        self.assertEqual(self.state(self.NOW - 60000, flag=False), "bare")


class EndTerminalTest(unittest.TestCase):
    def test_kill_waits_until_the_terminal_is_listed_no_more_then_rm(self):
        listed = [{"terminals": [{"address": "t1"}]}, {"terminals": [{"address": "t1"}]},
                  {"terminals": []}]
        calls = []

        def fake_hc(args, timeout=60):
            calls.append(list(args))
            return (0, listed.pop(0), "") if args[0] == "ls" else (0, None, "")

        with mock.patch.object(scan, "hc", side_effect=fake_hc), \
                mock.patch.object(scan.time, "sleep") as sleep:
            self.assertEqual(scan.end_terminal("t1"), (0, ""))
        self.assertEqual(calls, [["kill", "t1"], ["ls", "--json"], ["ls", "--json"],
                                 ["ls", "--json"], ["rm", "t1"]])
        self.assertEqual(sleep.call_count, 2)

    def test_a_failed_kill_is_the_failure(self):
        with mock.patch.object(scan, "hc", return_value=(1, None, "no terminal")) as hc:
            self.assertEqual(scan.end_terminal("t1"), (1, "no terminal"))
        self.assertEqual(hc.call_count, 1)


class SpawnResumeTest(unittest.TestCase):
    def test_a_resumed_tab_passes_resume_and_no_new_session(self):
        with mock.patch.object(scan, "hc_start", return_value="t1") as hc_start, \
                mock.patch.object(scan, "window_open", return_value=False), \
                mock.patch.object(scan.HC, "start_agent", return_value=None), \
                mock.patch.object(scan, "plugin_settings", return_value=""):
            host = scan.HC.spawn_tab(OPEN_SPACE, "o/repo#1", "/repo", "/wt", "T", "m",
                                     name="ap-repo-1", resume="c-old")
        argv, cwd, labels = hc_start.call_args.args[:3]
        self.assertIn("--resume", argv)
        self.assertNotIn("--session-id", argv)
        self.assertEqual(cwd, "/wt")
        self.assertEqual(labels["herdr.space"], OPEN_SPACE)
        self.assertEqual(labels["herdr.tab"], host["session"])
        self.assertEqual(labels["octopilot.agent"], "ap-repo-1")
        self.assertTrue(host["session"].startswith("tb"))
        self.assertEqual(host["claude_session"], "c-old")


class RestoreWiringTest(unittest.TestCase):
    def test_restore_is_self_locking(self):
        self.assertIn("restore-sessions", scan.SELF_LOCKING)


if __name__ == "__main__":
    unittest.main()
