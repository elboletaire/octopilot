"""The executor: each planned action against a scratch state, with GitHub,
herdr, git and the agents faked (fake_world). Nothing here reaches the live
state or a real agent."""
import contextlib
import io
import unittest
from unittest import mock

from tests.fake import fake_world
import scan
from engine import ACT_BUDGET_SECS, Action, Ctx, deliver_pending, execute, run_rows

REPO = "owner/repo"
MERGED, A, B, STACK, COMMENT = (f"{REPO}#7", f"{REPO}#8", f"{REPO}#9",
                                f"{REPO}#10", f"{REPO}#11")
HEAD = "new5678aaa"


class FakeBackend:
    """Stands in for a herdr backend: addresses are herdr:<session>."""
    name = "herdr"
    prefix = "herdr:"
    remove_label = "removed"
    noun = "workspace"

    def target(self, it):
        return it["session"]

    def discard(self, session, worktree):
        return 0, ""


BACKEND = FakeBackend()


def item(pr, branch, session, **kw):
    fields = {"mode": "fix", "pr": pr, "branch": branch, "session": session,
              "worktree": f"/wt/{branch.replace('/', '-')}", "phase": "idle",
              "reviewed_sha": "old1234", "review_rounds": 0}
    fields.update(kw)
    return fields


class ExecuteTest(unittest.TestCase):
    def setUp(self):
        self.reply = (True, None)     # what send_to answers
        self.reboot_code = 0
        self.prs = {}                 # pr number -> pr_detail answer
        self.git_head = HEAD
        self.git_dirty = ""
        world = fake_world(
            backend_for=lambda session: BACKEND,
            send_to=lambda address, text: self.reply,
            pr_detail=lambda slug, number: self.prs.get(number),
            sh=self.git,
        )
        self.world = world.__enter__()
        self.addCleanup(world.__exit__, None, None, None)
        # reboot waits for a real agent; a stand-in marks the item booting.
        reboot = mock.patch.object(scan, "reboot", side_effect=self.reboot)
        self.reboot_calls = reboot.start()
        self.addCleanup(reboot.stop)
        # send_item prints its outcome; keep the test output to the verdicts.
        quiet = contextlib.ExitStack()
        quiet.enter_context(contextlib.redirect_stdout(io.StringIO()))
        quiet.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.addCleanup(quiet.close)
        self.logs = []

    def git(self, args, cwd=None, timeout=60):
        if args[3:4] == ["rev-parse"]:
            return 0, self.git_head, ""
        if args[3:4] == ["status"]:
            return 0, self.git_dirty, ""
        return 0, "", ""

    def reboot(self, state, k):
        state["items"][k]["phase"] = "booting"
        scan.save(state)
        return self.reboot_code

    def log(self, kind, key, **fields):
        self.logs.append((kind, key, fields))

    def ctx(self, dry_run=False):
        return Ctx(dry_run=dry_run, sent_keys=set(), log=self.log, rows=[])

    def save(self, items, **extra):
        state = {"items": items, "handled_merges": [], "pending_sends": [],
                 "registry": {REPO: {"path": "/repo", "base": "main"}}}
        state.update(extra)
        scan.save(state)

    def state(self):
        return scan.load()

    def addresses(self):
        return [args[0] for args, _ in self.world.named("send_to")]

    def sends(self):
        return [args for args, _ in self.world.named("send_to")]

    def test_cleanup_fans_out_to_fix_siblings_only(self):
        self.save({
            MERGED: item(7, "feat/a", "w1"),
            A: item(8, "feat/b", "w2"),
            B: item(9, "feat/c", "w3", stack=[{"pr": 9, "branch": "feat/c"},
                                              {"pr": 10, "branch": "feat/d"}]),
            COMMENT: item(11, "feat/x", "w4", mode="comment"),
        })
        out = execute(Action("cleanup", MERGED, {}), self.ctx())
        state = self.state()
        self.assertNotIn(MERGED, state["items"])
        self.assertIn(MERGED, state["handled_merges"])
        self.assertEqual(self.addresses(), ["herdr:w2", "herdr:w3"])
        self.assertIn("feat/b", self.sends()[0][1])
        self.assertIn(f"{A}: sent rebase", out)
        self.assertIn(f"{B}: sent rebase-stack", out)
        self.assertNotIn(COMMENT, out)

    def test_restack_rekeys_and_sends_rebase_stack(self):
        self.save({B: item(9, "feat/c", "w3", stack=[
            {"pr": 9, "branch": "feat/c"}, {"pr": 10, "branch": "feat/d"},
            {"pr": 11, "branch": "feat/e"}])})
        self.prs = {9: {"state": "MERGED", "headRefOid": "top9sha"},
                    10: {"state": "OPEN", "headRefOid": "h10"},
                    11: {"state": "OPEN", "headRefOid": "h11"}}
        out = execute(Action("restack", B, {}), self.ctx())
        state = self.state()
        self.assertNotIn(B, state["items"])
        self.assertEqual(state["items"][STACK]["prev_keys"], [B])
        self.assertEqual([p["pr"] for p in state["items"][STACK]["stack"]], [10, 11])
        self.assertEqual(self.addresses(), ["herdr:w3"])
        self.assertIn(f"{STACK}: sent rebase-stack", out)

    def test_review_reboots_then_boots_now(self):
        self.save({A: item(8, "feat/b", "w2")})
        self.prs = {8: {"headRefOid": HEAD}}
        out = execute(Action("review", A, {}), self.ctx())
        self.assertEqual(self.reboot_calls.call_count, 1)
        self.assertEqual(self.addresses(), ["herdr:w2"])
        self.assertIn(f"{A}: sent review-fix", out)
        it = self.state()["items"][A]
        self.assertEqual((it["reviewed_sha"], it["phase"], it["review_rounds"]),
                         (HEAD, "fixing", 1))

    def test_review_at_the_cap_is_skipped(self):
        self.save({A: item(8, "feat/b", "w2", review_rounds=3)})
        out = execute(Action("review", A, {}), self.ctx())
        self.assertEqual(self.reboot_calls.call_count, 0)
        self.assertEqual(self.addresses(), [])
        self.assertIn("skipped", out)
        self.assertEqual([kind for kind, _, _ in self.logs], ["engine-skip"])

    def test_boot_blocked_agent_is_queued_without_a_mark(self):
        self.save({A: item(8, "feat/b", "w2", phase="booting")})
        self.prs = {8: {"headRefOid": HEAD}}
        self.reply = (False, "agent_blocked")
        ctx = self.ctx()
        execute(Action("boot", A, {}), ctx)
        state = self.state()
        self.assertEqual(state["items"][A]["reviewed_sha"], "old1234")
        self.assertEqual(state["items"][A]["phase"], "booting")
        [entry] = state["pending_sends"]
        self.assertEqual(entry["mark"], {"op": "reviewed", "sha": HEAD})
        # The same run's delivery does not send to the item again.
        deliver_pending(ctx)
        self.assertEqual(len(self.sends()), 1)
        # Next run, once the agent takes the prompt: sent, then marked.
        self.reply = (True, None)
        deliver_pending(self.ctx())
        self.assertEqual(len(self.sends()), 2)
        self.assertEqual(self.state()["pending_sends"], [])
        it = self.state()["items"][A]
        self.assertEqual((it["reviewed_sha"], it["phase"]), (HEAD, "fixing"))

    def test_boot_refuses_a_worktree_that_is_not_clean(self):
        self.save({A: item(8, "feat/b", "w2", phase="booting")})
        self.prs = {8: {"headRefOid": HEAD}}
        self.git_dirty = " M file.py"
        out = execute(Action("boot", A, {}), self.ctx())
        self.assertEqual(self.sends(), [])
        self.assertIn("uncommitted", out)
        self.assertEqual([kind for kind, _, _ in self.logs], ["engine-error"])

    def test_feedback_on_a_stack_marks_the_layer_only(self):
        self.save({B: item(9, "feat/c", "w3", phase="fixing",
                           feedback_seen="2026-11-20T00:00:00Z",
                           stack=[{"pr": 9, "branch": "feat/c"},
                                  {"pr": 10, "branch": "feat/d"}])})
        since = "2026-11-26T00:00:00Z"
        out = execute(Action("feedback", B, {"since": since, "pr": "10",
                                             "branch": "feat/d"}), self.ctx())
        it = self.state()["items"][B]
        self.assertEqual(self.addresses(), ["herdr:w3"])
        self.assertIn("feat/d", self.sends()[0][1])
        self.assertIn(f"{B}: sent address-feedback", out)
        self.assertEqual(it["layer_seen"], {"10": since})
        self.assertEqual(it["feedback_seen"], "2026-11-20T00:00:00Z")

    def test_action_raising_does_not_stop_the_next(self):
        self.save({A: item(8, "feat/b", "w2", phase="fixing"),
                   B: item(9, "feat/c", "w3", phase="fixing")})
        since = "2026-11-26T00:00:00Z"
        with mock.patch.object(scan, "render_for",
                               side_effect=[SystemExit("boom"), "TEXT"]):
            first = execute(Action("feedback", A, {"since": since}), self.ctx())
            second = execute(Action("feedback", B, {"since": since}), self.ctx())
        self.assertIn("failed", first)
        self.assertEqual(second, f"{B}: sent address-feedback")
        self.assertEqual(self.addresses(), ["herdr:w3"])
        self.assertEqual([(kind, key) for kind, key, _ in self.logs],
                         [("engine-error", A)])

    def test_dry_run_changes_nothing(self):
        self.save({MERGED: item(7, "feat/a", "w1"),
                   A: item(8, "feat/b", "w2")})
        before = self.state()
        out = execute(Action("cleanup", MERGED, {}), self.ctx(dry_run=True))
        self.assertEqual(out, f"{MERGED}: would cleanup")
        self.assertEqual(self.state(), before)
        self.assertEqual(self.sends(), [])

    def test_one_send_per_item_across_execute_and_delivery(self):
        self.save({A: item(8, "feat/b", "w2", phase="booting")},
                  pending_sends=[{"key": A, "template": "review-fix",
                                  "subs": {"REPO": REPO, "PR": 8,
                                           "BRANCH": "feat/b", "BASE": "main"},
                                  "at": 0, "tries": 0, "error": None,
                                  "reason": "boot",
                                  "mark": {"op": "reviewed", "sha": "old"}}])
        self.prs = {8: {"headRefOid": HEAD}}
        ctx = self.ctx()
        execute(Action("boot", A, {}), ctx)
        deliver_pending(ctx)
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.state()["pending_sends"], [])
        self.assertEqual(self.state()["items"][A]["reviewed_sha"], HEAD)


    def test_merge_and_restack_in_one_tick_send_each_prompt_once(self):
        # The probe: a cleanup fans out to the stack item B on w2, and B's own
        # restack then needs w2 too. One prompt goes out per run, the other waits.
        self.save({MERGED: item(7, "feat/a", "w1"),
                   B: item(9, "feat/c", "w2", stack=[
                       {"pr": 9, "branch": "feat/c"}, {"pr": 10, "branch": "feat/d"},
                       {"pr": 11, "branch": "feat/e"}])})
        self.prs = {9: {"state": "MERGED", "headRefOid": "top9sha"},
                    10: {"state": "OPEN", "headRefOid": "h10"},
                    11: {"state": "OPEN", "headRefOid": "h11"}}
        ctx = self.ctx()
        execute(Action("cleanup", MERGED, {}), ctx)
        execute(Action("restack", B, {}), ctx)
        self.assertEqual(self.addresses(), ["herdr:w2"])
        self.assertEqual(self.state()["pending_sends"][0]["reason"], "restack")
        # Next run: the restack prompt goes out, with the merged head it needs.
        deliver_pending(self.ctx())
        self.assertEqual(self.addresses(), ["herdr:w2", "herdr:w2"])
        self.assertIn("top9sha", self.sends()[1][1])
        self.assertEqual(self.state()["pending_sends"], [])

    def test_a_feedback_send_does_not_drop_a_queued_rebase(self):
        self.save({A: item(8, "feat/b", "w2", phase="fixing",
                           feedback_seen="2026-11-20T00:00:00Z",
                           pending=None)},
                  pending_sends=[{"key": A, "template": "rebase", "subs": {},
                                  "at": 0, "tries": 0, "error": None,
                                  "reason": "rebase"}])
        execute(Action("feedback", A, {"since": "2026-11-26T00:00:00Z"}),
                self.ctx())
        self.assertEqual([p["reason"] for p in self.state()["pending_sends"]],
                         ["rebase"])

    def test_queued_wait_is_logged_once_and_never_as_an_error(self):
        self.save({A: item(8, "feat/b", "w2", phase="booting")})
        self.prs = {8: {"headRefOid": HEAD}}
        self.reply = (False, "agent_blocked")
        ctx = self.ctx()
        execute(Action("boot", A, {}), ctx)
        deliver_pending(self.ctx())
        execute(Action("boot", A, {}), self.ctx())
        self.assertEqual([kind for kind, _, _ in self.logs], ["send-waiting"])
        self.assertNotIn("engine_error", self.state()["items"][A])

    def test_reboot_blocked_is_a_wait_not_an_error(self):
        self.save({A: item(8, "feat/b", "w2", review_rounds=1)})
        self.reboot_code = 4
        out = execute(Action("review", A, {}), self.ctx())
        self.assertIn("waiting", out)
        self.assertEqual([kind for kind, _, _ in self.logs], ["send-waiting"])
        self.assertEqual(self.state()["items"][A]["engine_wait"][:7], "waiting")

    def test_failure_is_on_the_item_and_logged_once_per_change(self):
        self.save({A: item(8, "feat/b", "w2", phase="booting")})
        self.prs = {8: {"headRefOid": HEAD}}
        self.git_dirty = " M file.py"
        execute(Action("boot", A, {}), self.ctx())
        execute(Action("boot", A, {}), self.ctx())
        it = self.state()["items"][A]
        self.assertIn("uncommitted", it["last_note"])
        self.assertEqual([kind for kind, _, _ in self.logs], ["engine-error"])
        self.assertIn("boot failed", self.logs[0][2]["msg"])
        # Once the worktree is clean the prompt goes out and the note clears.
        self.git_dirty = ""
        execute(Action("boot", A, {}), self.ctx())
        it = self.state()["items"][A]
        self.assertNotIn("engine_error", it)
        self.assertNotIn("last_note", it)

    def test_item_without_a_worktree_uses_its_session_folder(self):
        self.save({A: item(8, "feat/b", "w2", phase="booting", worktree=None)})
        self.prs = {8: {"headRefOid": HEAD}}
        with mock.patch.object(scan, "all_sessions", return_value=(
                [{"id": "w2", "path": "/wt/session-folder"}], set(), set())):
            execute(Action("boot", A, {}), self.ctx())
        git_calls = [args[0] for args, _ in self.world.named("sh")]
        self.assertIn(["git", "-C", "/wt/session-folder", "rev-parse", "FETCH_HEAD"],
                      git_calls)
        self.assertEqual(self.addresses(), ["herdr:w2"])

    def test_budget_stops_new_actions_and_leaves_the_rest(self):
        self.save({A: item(8, "feat/b", "w2", phase="fixing",
                           feedback_seen="2026-11-20T00:00:00Z"),
                   B: item(9, "feat/c", "w3", phase="fixing",
                           feedback_seen="2026-11-20T00:00:00Z")})
        rows = [("FEEDBACK", A, "mode=fix since=2026-11-26T00:00:00Z"),
                ("FEEDBACK", B, "mode=fix since=2026-11-26T00:00:00Z")]
        ticks = iter([0, ACT_BUDGET_SECS])
        clock = lambda: next(ticks, ACT_BUDGET_SECS)
        lines = run_rows(rows, "on", 0, clock=clock)
        self.assertEqual(self.addresses(), ["herdr:w2"])
        self.assertIn("1 action(s) left for the next tick", lines[-1])

    def test_plan_logs_only_when_the_planned_set_changes(self):
        self.save({MERGED: item(7, "feat/a", "w1")})
        rows = [("MERGED", MERGED, "branch=feat/a mode=fix")]
        for _ in range(2):
            self.assertEqual(run_rows(rows, "plan", 0), [f"{MERGED}: would cleanup"])
        with open(scan.EVENTS_FILE) as fh:
            self.assertEqual(fh.read().count('"kind": "engine-plan"'), 1)
        self.assertEqual(self.state()["items"][MERGED]["session"], "w1")
        self.assertEqual(self.sends(), [])

    def test_a_late_send_clears_the_failure_note(self):
        self.save({A: item(8, "feat/b", "w2", engine_error="rebase failed: x",
                           last_note="rebase failed: x", engine_wait="waiting")},
                  pending_sends=[{"key": A, "template": "rebase", "subs": {},
                                  "at": 0, "tries": 1, "error": "send failed",
                                  "reason": "rebase"}])
        deliver_pending(self.ctx())
        it = self.state()["items"][A]
        for f in ("engine_error", "last_note", "engine_wait"):
            self.assertNotIn(f, it)

    def test_restack_prompt_goes_first_and_covers_the_merge_fan_out(self):
        # MERGED #7 and RESTACK on B (session w2) in one tick: w2 gets the
        # restack prompt with RESTACK_FROM, and no stale rebase-stack follows.
        self.save({MERGED: item(7, "feat/a", "w1"),
                   B: item(9, "feat/c", "w2", stack=[
                       {"pr": 9, "branch": "feat/c"}, {"pr": 10, "branch": "feat/d"},
                       {"pr": 11, "branch": "feat/e"}])})
        self.prs = {9: {"state": "MERGED", "headRefOid": "top9sha"},
                    10: {"state": "OPEN", "headRefOid": "h10"},
                    11: {"state": "OPEN", "headRefOid": "h11"}}
        rows = [("MERGED", MERGED, "branch=feat/a mode=fix"),
                ("RESTACK", B, "mode=fix")]
        lines = run_rows(rows, "on", 0, clock=lambda: 0)
        self.assertEqual(self.addresses(), ["herdr:w2"], lines)
        self.assertIn("top9sha", self.sends()[0][1])
        state = self.state()
        self.assertNotIn(MERGED, state["items"])
        self.assertEqual(state["pending_sends"], [])

    def test_a_raising_entry_frees_the_slot_and_is_dropped_after_five_tries(self):
        self.save({A: item(8, "feat/b", "w2")},
                  pending_sends=[{"key": A, "template": "rebase", "subs": {},
                                  "at": 0, "tries": 0, "error": None,
                                  "reason": "rebase"}])
        ctx = self.ctx()
        with mock.patch.object(scan, "render_for", side_effect=KeyError("X")):
            for _ in range(5):
                ctx = self.ctx()
                deliver_pending(ctx)
                self.assertNotIn("w2", ctx.sent_keys)
        state = self.state()
        self.assertEqual(state["pending_sends"], [])
        self.assertIn("dropped", state["items"][A]["engine_error"])
        self.assertEqual([k for k, _, _ in self.logs].count("engine-error"), 1)

    def test_budget_also_stops_queued_sends(self):
        self.save({A: item(8, "feat/b", "w2")},
                  pending_sends=[{"key": A, "template": "rebase", "subs": {},
                                  "at": 0, "tries": 0, "error": None,
                                  "reason": "rebase"}])
        ctx = self.ctx()
        ctx.deadline, ctx.clock = 10, lambda: 10
        self.assertIn("budget spent", deliver_pending(ctx)[0])
        self.assertEqual(self.sends(), [])


if __name__ == "__main__":
    unittest.main()
