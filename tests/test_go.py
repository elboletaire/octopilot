"""The `go` request: an issue or a PR becomes a session, with the first prompt the
engine would have sent. GitHub, herdr and git are faked (tests/stand.py); the
prompts are rendered from the real templates and sent through the real
send_item, to a fake send_to."""
import fcntl
import time
import unittest
from unittest import mock

import engine
import naming
import scan
from tests.stand import HEAD, REPO, RequestCase

ISSUE = f"{REPO}!12"
PR = f"{REPO}#31"


def lock_is_free():
    """True when no process holds the state lock (scan.locked)."""
    with open(scan.STATE_FILE + ".lock", "w") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return True


class GoTest(RequestCase):
    def test_go_issue_starts_work_on_a_named_branch(self):
        self.handle(f"go {ISSUE}")
        [args] = self.spawns()
        self.assertEqual((args[1], args[2], args[3], args[4]),
                         (ISSUE, "fix/add-login-12", "Add Login", True))
        self.assertEqual(self.addresses(), ["herdr:w9"])
        self.assertIn("fix/add-login-12", self.texts()[0])
        self.assertEqual([(k, key) for k, key, _ in self.logs], [("go", ISSUE)])

    def test_an_issue_branch_is_named_through_choose_names(self):
        with mock.patch.object(naming, "choose_names", wraps=naming.choose_names) as choose:
            self.handle(f"go {ISSUE}")
        self.assertEqual(choose.call_args.args[0], "issue")
        self.assertEqual(self.spawns()[0][2], "fix/add-login-12")

    def test_go_pr_stack_is_one_item_keyed_by_its_bottom(self):
        self.open_prs = [
            {"number": 30, "headRefName": "feat/a", "baseRefName": "main"},
            {"number": 31, "headRefName": "feat/b", "baseRefName": "feat/a"},
            {"number": 32, "headRefName": "feat/c", "baseRefName": "feat/b"}]
        self.template = "adopt-stack"
        self.handle(f"go {PR}")
        [call] = self.spawn_item.call_args_list
        self.assertEqual((call.args[1], call.args[2]), (f"{REPO}#30", "feat/a"))
        self.assertEqual(call.kwargs["stack_spec"], "30:feat/a 31:feat/b 32:feat/c")
        self.assertEqual(self.addresses(), ["herdr:w9"])
        self.assertIn("31:feat/b", self.texts()[0])

    def test_go_fix_pr_behind_base_sends_rebase_alone(self):
        self.template = "review-fix"
        self.behind = True
        self.handle(f"go {PR}")
        self.assertTrue(any("merge-base" in args[0]
                            for args, _ in self.world.named("sh")))
        self.assertEqual([(k, f["template"]) for k, _, f in self.logs],
                         [("go", "rebase")])

    def test_go_fix_pr_up_to_date_sends_review_fix(self):
        self.template = "review-fix"
        self.handle(f"go {PR}")
        self.assertEqual([(k, f["template"]) for k, _, f in self.logs],
                         [("go", "review-fix")])

    def test_go_comment_pr_checks_out_the_head_then_marks_it_reviewed(self):
        self.mode, self.template = "comment", "review-comment"
        self.handle(f"go {PR}")
        fetches = [args[0] for args, _ in self.world.named("sh") if "fetch" in args[0]]
        self.assertIn(["git", "-C", "/wt/feat-b", "fetch", "origin", "pull/31/head"],
                      fetches)
        self.assertEqual(self.addresses(), ["herdr:w9"])
        it = scan.load()["items"][PR]
        self.assertEqual((it["reviewed_sha"], it["phase"], it["review_rounds"]),
                         (HEAD, "fixing", 1))

    def test_go_re_review_passes_the_previous_review_sha(self):
        self.mode, self.template, self.prev = "comment", "re-review", "old1234"
        self.handle(f"go {PR}")
        self.assertIn("old1234", self.texts()[0])
        self.assertEqual(scan.load()["items"][PR]["reviewed_sha"], HEAD)

    def test_go_comment_pr_whose_checkout_fails_is_not_sent(self):
        self.mode, self.template = "comment", "review-comment"

        def dirty(args, cwd=None, timeout=60):
            if "status" in args:
                return 0, " M file.py", ""
            return self.git(args, cwd, timeout)
        with mock.patch.object(scan, "sh", side_effect=dirty):
            out = self.handle(f"go {PR}")
        self.assertEqual(self.addresses(), [])
        self.assertIn("uncommitted", out)
        it = scan.load()["items"][PR]
        self.assertIn("uncommitted", it["engine_error"])
        self.assertEqual(it["phase"], "booting")

    def test_the_prompt_is_rendered_with_the_lock_released(self):
        # The work prompt's issue context is a gh call: none may run under the lock.
        free = []
        real_gh = self.gh

        def probe(args, cwd=None, default=None):
            if args[:2] == ["issue", "view"]:
                free.append(lock_is_free())
            return real_gh(args, cwd, default)
        with mock.patch.object(scan, "gh_json", side_effect=probe):
            self.handle(f"go {ISSUE}")
        # The go's own issue read and the prompt's issue context both run.
        self.assertEqual(len(free), 2)
        self.assertTrue(all(free))

    def test_a_failed_checkout_is_retried_by_the_executors_boot(self):
        self.mode, self.template = "comment", "review-comment"
        with mock.patch.object(scan, "sh", side_effect=lambda args, cwd=None, timeout=60:
                               (1, " M x", "") if "status" in args
                               else self.git(args, cwd, timeout)):
            self.handle(f"go {PR}")
        self.assertEqual(self.addresses(), [])
        ctx = self.ctx()
        engine._boot(ctx, PR)
        self.assertEqual(self.addresses(), ["herdr:w9"])
        it = scan.load()["items"][PR]
        self.assertEqual((it["reviewed_sha"], it["phase"]), (HEAD, "fixing"))

    def test_git_runs_with_the_state_lock_released(self):
        self.mode, self.template = "comment", "review-comment"
        free = []

        def probe(args, cwd=None, timeout=60):
            if "rev-parse" in args:
                free.append(lock_is_free())
            return self.git(args, cwd, timeout)
        with mock.patch.object(scan, "sh", side_effect=probe):
            self.handle(f"go {PR}")
        self.assertEqual(free, [True])

    def test_go_refused_when_already_tracked(self):
        self.save({PR: {"mode": "fix", "branch": "feat/b", "session": "w9",
                        "phase": "working", "pr": 31}})
        out = self.handle(f"go {PR}")
        self.assertEqual(self.spawns(), [])
        self.assertIn("already tracked", out)
        self.assertEqual([k for k, _, _ in self.logs], ["go-refused"])

    def test_go_on_a_stack_layer_is_refused_when_its_stack_is_tracked(self):
        self.save({f"{REPO}#30": {"mode": "fix", "branch": "feat/a", "session": "w9",
                                  "phase": "working", "pr": 30,
                                  "stack": [{"pr": 30, "branch": "feat/a"},
                                            {"pr": 31, "branch": "feat/b"}]}})
        self.open_prs = [{"number": 30, "headRefName": "feat/a", "baseRefName": "main"},
                         {"number": 31, "headRefName": "feat/b", "baseRefName": "feat/a"}]
        out = self.handle(f"go {PR}")
        self.assertEqual(self.spawns(), [])
        self.assertIn("already tracked as", out)

    def test_go_refused_on_a_closed_issue(self):
        self.issue["state"] = "CLOSED"
        out = self.handle(f"go {ISSUE}")
        self.assertEqual(out, f"go {ISSUE} refused: issue is closed")
        self.assertEqual(self.spawns(), [])
        self.assertEqual([k for k, _, _ in self.logs], ["go-refused"])

    def test_go_refused_on_a_closed_or_merged_pr(self):
        for state, reason in (("CLOSED", "PR is closed"), ("MERGED", "PR is merged")):
            with self.subTest(state=state):
                self.prs[31]["state"] = state
                self.assertEqual(self.handle(f"go {PR}"),
                                 f"go {PR} refused: {reason}")
        self.assertEqual(self.spawns(), [])
        self.assertEqual([k for k, _, _ in self.logs], ["go-refused", "go-refused"])

    def test_go_refused_for_an_uncloned_repo_with_the_clone_hint(self):
        other = "other/repo!4"
        self.snapshot(rows=[{"kind": "UNCLONED", "key": other, "title": "Old"}])
        out = self.handle(f"go {other}")
        self.assertIn("clone it first", out)
        self.assertEqual(self.spawns(), [])
        self.assertEqual([k for k, _, _ in self.logs], ["go-refused"])

    def test_go_held_past_wind_down_until_confirmed(self):
        self.snapshot(rows=[{"kind": "PROPOSE", "key": ISSUE, "title": "Add login"}])
        with mock.patch.object(scan, "wind_down", return_value="yes"):
            out = self.handle(f"go {ISSUE}")
            self.assertEqual(self.spawns(), [])
            self.assertEqual(out, f"{ISSUE} held (wind-down): Add login")
            self.assertEqual([k for k, _, _ in self.logs], ["held"])
            self.handle(f"go {ISSUE} (confirmed past wind-down)")
        self.assertEqual(len(self.spawns()), 1)

    def test_go_held_over_the_cap_until_confirmed_over_cap(self):
        self.snapshot(active=scan.MAX_ACTIVE)
        with mock.patch.object(scan, "wind_down", return_value=None):
            out = self.handle(f"go {ISSUE}")
            self.assertEqual(self.spawns(), [])
            self.assertIn("held (cap)", out)
            self.handle(f"go {ISSUE} (confirmed over cap)")
        self.assertEqual(len(self.spawns()), 1)

    def test_a_start_after_the_snapshot_counts_against_the_cap(self):
        # Two starts in two drains, on one snapshot taken a minute earlier: the
        # second is over the cap even though the snapshot still shows one slot.
        self.snapshot(active=scan.MAX_ACTIVE - 1, generated_at=int(time.time()) - 60)
        with mock.patch.object(scan, "wind_down", return_value=None):
            self.assertNotIn("held", self.handle(f"go {ISSUE}"))
            self.assertIn("held (cap)", self.handle(f"go {PR}"))
        self.assertEqual(len(self.spawns()), 1)

    def test_an_item_the_snapshot_already_counted_is_not_counted_twice(self):
        now = int(time.time())
        self.save({f"{REPO}#99": {"mode": "fix", "branch": "feat/z", "session": "w1",
                                  "phase": "working", "pr": 99, "added": now - 1000}})
        self.snapshot(active=scan.MAX_ACTIVE - 1, generated_at=now)
        with mock.patch.object(scan, "wind_down", return_value=None):
            self.assertNotIn("held", self.handle(f"go {ISSUE}"))

    def test_untracked_sessions_count_against_the_cap(self):
        self.snapshot(active=scan.MAX_ACTIVE - 1, untracked=1)
        with mock.patch.object(scan, "wind_down", return_value=None):
            self.assertIn("held (cap)", self.handle(f"go {ISSUE}"))

    def test_a_refused_spawn_is_a_refusal_not_a_crash(self):
        with mock.patch.object(scan, "spawn_item", side_effect=SystemExit("no clone")):
            out = self.handle(f"go {ISSUE}")
        self.assertEqual(out, f"go {ISSUE} refused: spawn failed: no clone")
        self.assertEqual([k for k, _, _ in self.logs], ["go-refused"])

    def test_a_host_warning_queues_the_prompt_instead_of_sending(self):
        self.warning = "session started without its plugin"
        self.handle(f"go {ISSUE}")
        self.assertEqual(self.addresses(), [])
        [entry] = scan.load()["pending_sends"]
        self.assertEqual((entry["key"], entry["template"], entry["reason"]),
                         (ISSUE, "work", "go"))
        self.assertIs(self.logs[-1][2].get("queued"), True)

    def test_a_host_note_sends_the_prompt_now_and_is_logged(self):
        self.note = "no Octopilot workspace: the worker opens as its own workspace"
        self.handle(f"go {ISSUE}")
        self.assertEqual(len(self.addresses()), 1)
        self.assertEqual(scan.load()["pending_sends"], [])
        kind, _, fields = self.logs[-1]
        self.assertEqual((kind, fields.get("note")), ("go", self.note))
        self.assertNotIn("queued", fields)


if __name__ == "__main__":
    unittest.main()
