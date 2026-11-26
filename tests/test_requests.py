"""The request handlers other than go: each request run against a scratch state,
with GitHub, herdr and git faked (tests/stand.py)."""
import unittest
from unittest import mock

import scan
from engine import _handle, parse_request
from tests.stand import HEAD, REPO, RequestCase

PR = f"{REPO}#31"
OLD = "old1234"


class NoDetachAckTest(RequestCase):
    def events(self):
        with open(scan.EVENTS_FILE) as fh:
            return fh.read()

    def test_no_declines_the_item_for_good(self):
        self.assertEqual(self.handle(f"no {PR}"), f"{PR}: declined")
        self.assertEqual(scan.load()["declined"], [PR])
        self.assertIn('"kind": "decline"', self.events())

    def test_detach_closes_the_session_and_untracks_the_item(self):
        self.save({PR: {"mode": "fix", "branch": "feat/b", "session": "w9",
                        "phase": "working", "pr": 31}})
        out = self.handle(f"detach {PR}")
        self.assertIn("workspace closed", out)
        self.assertNotIn(PR, scan.load()["items"])

    def test_detach_of_an_untracked_key_is_refused(self):
        self.assertIn("unknown item", self.handle(f"detach {PR}"))
        self.assertEqual([k for k, _, _ in self.logs], ["request-refused"])

    def test_ack_records_the_pushed_head(self):
        self.save({PR: {"mode": "comment", "branch": "feat/b", "session": "w9",
                        "phase": "fixing", "pr": 31}})
        self.assertEqual(self.handle(f"ack {PR}"), f"{PR}: acked")
        self.assertEqual(scan.load()["items"][PR]["push_acked"], HEAD)

    def test_pause_and_resume_set_the_flag(self):
        self.assertEqual(self.handle("pause"), "paused")
        self.assertTrue(scan.load()["paused"])
        self.assertEqual(self.handle("resume"), "resumed")
        self.assertFalse(scan.load()["paused"])
        self.assertIn('"kind": "pause"', self.events())
        self.assertIn('"kind": "resume"', self.events())


class ReReviewTest(RequestCase):
    def setUp(self):
        super().setUp()
        self.save({PR: {"mode": "comment", "branch": "feat/b", "session": "w9",
                        "worktree": "/wt/feat-b", "phase": "fixing", "pr": 31,
                        "reviewed_sha": OLD, "review_rounds": 1}})
        # reboot waits for a real agent to go idle: a stand-in marks it booting.
        patch = mock.patch.object(scan, "reboot", side_effect=self.reboot)
        self.reboot_calls = patch.start()
        self.addCleanup(patch.stop)

    def reboot(self, state, k):
        state["items"][k]["phase"] = "booting"
        scan.save(state)
        return 0

    def test_re_review_reboots_then_sends_the_follow_up_from_the_last_review(self):
        out = self.handle(f"re-review {PR}")
        self.assertEqual(self.reboot_calls.call_count, 1)
        self.assertEqual(self.addresses(), ["herdr:w9"])
        self.assertIn(OLD, self.texts()[0])
        self.assertEqual(out, f"{PR}: sent re-review")
        it = scan.load()["items"][PR]
        self.assertEqual((it["reviewed_sha"], it["phase"], it["review_rounds"]),
                         (HEAD, "fixing", 2))

    def test_re_review_of_a_fix_item_is_refused(self):
        self.save({PR: {"mode": "fix", "branch": "feat/b", "session": "w9",
                        "phase": "fixing", "pr": 31}})
        self.assertIn("only a comment PR", self.handle(f"re-review {PR}"))
        self.assertEqual(self.reboot_calls.call_count, 0)

    def test_re_review_at_the_cap_is_refused(self):
        state = scan.load()
        state["items"][PR]["review_rounds"] = scan.MAX_REVIEW_ROUNDS
        scan.save(state)
        self.assertIn("review cap", self.handle(f"re-review {PR}"))
        self.assertEqual(self.reboot_calls.call_count, 0)

    def test_re_review_of_an_untracked_pr_is_refused(self):
        self.save({})
        self.assertIn("not tracked", self.handle(f"re-review {PR}"))


class InvestigateTest(RequestCase):
    QUESTION = "How does the cache work"

    def test_investigation_is_named_apart_from_existing_ones(self):
        self.save({f"{REPO}?how-does-the-cache-work": {
            "mode": "fix", "branch": "chore/how-does-the-cache-work",
            "session": "w1", "phase": "working", "pr": None}})
        self.handle(f"/investigate owner/repo {self.QUESTION}")
        [args] = self.spawns()
        self.assertEqual(args[1:4], (f"{REPO}?how-does-the-cache-work-2",
                                     "chore/how-does-the-cache-work-2",
                                     "How Does The Cache Work 2"))
        self.assertEqual(args[4], True)

    def test_question_is_kept_verbatim_without_the_confirmation_suffix(self):
        self.handle(f"/investigate owner/repo {self.QUESTION} "
                    "(confirmed past wind-down)")
        item = scan.load()["items"][f"{REPO}?how-does-the-cache-work"]
        self.assertEqual(item["question"], self.QUESTION)
        self.assertIn(self.QUESTION, self.texts()[0])
        self.assertNotIn("confirmed", self.texts()[0])

    def test_read_only_uses_the_read_only_template_and_flags_the_item(self):
        out = self.handle("/investigate --read-only owner/repo Audit the auth code")
        self.assertEqual(self.spawns()[0][1], f"{REPO}?audit-the-auth-code")
        self.assertIs(self.spawn_item.call_args.kwargs["read_only"], True)
        self.assertTrue(scan.load()["items"][f"{REPO}?audit-the-auth-code"]["read_only"])
        self.assertIn("Audit the auth code", self.texts()[0])
        self.assertIn("audit-the-auth-code: sent", out)

    def test_repo_may_be_a_unique_suffix_of_a_registry_slug(self):
        self.handle(f"/investigate repo {self.QUESTION}")
        self.assertEqual(len(self.spawns()), 1)

    def test_unknown_repo_is_refused_with_the_clone_hint(self):
        out = self.handle(f"/investigate other/repo {self.QUESTION}")
        self.assertIn("clone it under CLONE_ROOTS first", out)
        self.assertEqual(self.spawns(), [])


class MiscTest(RequestCase):
    def test_unknown_request_is_logged_and_refused(self):
        self.assertEqual(self.handle("hello there"), "unknown request: hello there")
        self.assertEqual(self.logs, [("request-unknown", None,
                                      {"text": "hello there"})])

    def test_a_report_is_dropped_without_a_trace(self):
        before = scan.load()
        self.assertIsNone(self.handle(f"OCTOPILOT {PR} ready {HEAD} all done"))
        self.assertEqual(scan.load(), before)
        self.assertEqual(self.logs, [])

    def test_code_is_delivered_verbatim_to_the_code_agent(self):
        out = self.handle("/octopilot-code fix the kitchen (confirmed over cap)")
        self.assertEqual(self.addresses(), [scan.CODE_AGENT])
        self.assertEqual(self.texts(), ["fix the kitchen (confirmed over cap)"])
        self.assertIn("1 request(s) delivered", out)

    def test_code_the_agent_cannot_take_is_kept_pending(self):
        self.reply = (False, "tab closed")
        out = self.handle("/octopilot-code fix the kitchen")
        self.assertIn("kept, still pending", out)
        self.assertEqual(len(scan.load()["code_pending"]), 1)

    def test_tick_asks_for_a_tick_and_says_nothing(self):
        ctx = self.ctx()
        self.assertIsNone(_handle(parse_request("tick"), ctx))
        self.assertTrue(ctx.need_tick)


if __name__ == "__main__":
    unittest.main()
