"""The `go` request: an issue or a PR becomes a session, with the first prompt the
engine would have sent. GitHub, herdr and git are faked (fake_world); the
prompts are rendered from the real templates and sent through the real
send_item, to a fake send_to."""
import contextlib
import io
import json
import time
import unittest
from unittest import mock

from tests.fake import fake_world
import scan
from engine import Ctx, do_go, parse_request

REPO = "owner/repo"
HEAD = "new5678aaa"
ISSUE = f"{REPO}!12"
PR = f"{REPO}#31"


class FakeBackend:
    """Stands in for a herdr backend: addresses are herdr:<session>."""
    name = "herdr"
    prefix = "herdr:"
    noun = "workspace"

    def target(self, it):
        return it["session"]


BACKEND = FakeBackend()


class GoTest(unittest.TestCase):
    def setUp(self):
        self.issue = {"number": 12, "title": "Add login", "labels": [{"name": "bug"}],
                      "body": "", "comments": []}
        self.prs = {31: {"headRefName": "feat/b", "headRefOid": HEAD,
                         "title": "Second layer"}}
        self.open_prs = [{"number": 31, "headRefName": "feat/b", "baseRefName": "main"}]
        self.behind = False
        self.mode, self.template, self.prev, self.warning = "fix", "work", None, None
        world = fake_world(
            backend_for=lambda session: BACKEND,
            send_to=lambda address, text: (True, None),
            gh_json=self.gh,
            pr_detail=lambda slug, number: self.prs.get(number),
            sh=self.git,
        )
        self.world = world.__enter__()
        self.addCleanup(world.__exit__, None, None, None)
        # A session needs a worktree and a herdr workspace: a stand-in tracks
        # the item as scan.spawn_item does, and the calls are still recorded.
        patch = mock.patch.object(scan, "spawn_item", side_effect=self.spawn)
        self.spawn_item = patch.start()
        self.addCleanup(patch.stop)
        quiet = contextlib.ExitStack()
        quiet.enter_context(contextlib.redirect_stdout(io.StringIO()))
        quiet.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.addCleanup(quiet.close)
        self.logs = []
        self.save({})

    # ---- stand-ins

    def gh(self, args, cwd=None, default=None):
        if args[:2] == ["issue", "view"]:
            return self.issue
        if args[:2] == ["pr", "list"]:
            return self.open_prs
        return default

    def git(self, args, cwd=None, timeout=60):
        if "merge-base" in args:
            return (1 if self.behind else 0), "", ""
        if "rev-parse" in args:
            return 0, HEAD, ""
        return 0, "", ""

    def spawn(self, state, key_, branch, title, new_branch, question=None,
              read_only=False, stack_spec=None):
        """What scan.spawn_item tracks, with no worktree or session behind it."""
        stack = None
        if stack_spec:
            stack = [{"pr": int(p), "branch": b} for p, b in
                     (layer.split(":", 1) for layer in stack_spec.split())]
        it = {"mode": self.mode, "branch": branch, "session": "w9",
              "worktree": "/wt/" + branch.replace("/", "-"), "phase": "working",
              "pr": int(key_.split("#")[1]) if "#" in key_ else None,
              "added": int(time.time())}
        if stack:
            it["stack"] = stack
        state["items"][key_] = it
        scan.save(state)
        return {"ok": True, "error": None, "key": key_, "template": self.template,
                "base": "main", "slug": REPO, "prev": self.prev, "stack": stack,
                "warning": self.warning, "model": ""}

    def log(self, kind, key, **fields):
        self.logs.append((kind, key, fields))

    def ctx(self):
        return Ctx(dry_run=False, sent_keys=set(), log=self.log, rows=[])

    def save(self, items, **extra):
        state = {"items": items, "handled_merges": [], "pending_sends": [],
                 "declined": [], "registry": {REPO: {"path": "/nonexistent/repo",
                                                    "base": "main"}}}
        state.update(extra)
        scan.save(state)

    def snapshot(self, active=0, untracked=0, rows=()):
        with open(scan.SNAPSHOT_FILE, "w") as fh:
            json.dump({"active": active, "untracked": untracked,
                       "rows": list(rows)}, fh)

    def go(self, text):
        return do_go(parse_request(text), self.ctx())

    def spawns(self):
        return [c.args for c in self.spawn_item.call_args_list]

    def addresses(self):
        return [args[0] for args, _ in self.world.named("send_to")]

    def texts(self):
        return [args[1] for args, _ in self.world.named("send_to")]

    # ---- cases

    def test_go_issue_starts_work_on_a_named_branch(self):
        self.go(f"go {ISSUE}")
        [args] = self.spawns()
        self.assertEqual((args[1], args[2], args[3], args[4]),
                         (ISSUE, "fix/add-login-12", "Add Login", True))
        self.assertEqual(self.addresses(), ["herdr:w9"])
        self.assertIn("fix/add-login-12", self.texts()[0])
        self.assertEqual([(k, key) for k, key, _ in self.logs], [("go", ISSUE)])

    def test_go_pr_stack_is_one_item_keyed_by_its_bottom(self):
        self.open_prs = [
            {"number": 30, "headRefName": "feat/a", "baseRefName": "main"},
            {"number": 31, "headRefName": "feat/b", "baseRefName": "feat/a"},
            {"number": 32, "headRefName": "feat/c", "baseRefName": "feat/b"}]
        self.template = "adopt-stack"
        self.go(f"go {PR}")
        [call] = self.spawn_item.call_args_list
        self.assertEqual((call.args[1], call.args[2]), (f"{REPO}#30", "feat/a"))
        self.assertEqual(call.kwargs["stack_spec"], "30:feat/a 31:feat/b 32:feat/c")
        self.assertEqual(self.addresses(), ["herdr:w9"])
        self.assertIn("31:feat/b", self.texts()[0])

    def test_go_fix_pr_behind_base_sends_rebase_alone(self):
        self.template = "review-fix"
        self.behind = True
        self.go(f"go {PR}")
        self.assertTrue(any("merge-base" in args[0]
                            for args, _ in self.world.named("sh")))
        self.assertEqual([(k, f["template"]) for k, _, f in self.logs],
                         [("go", "rebase")])

    def test_go_fix_pr_up_to_date_sends_review_fix(self):
        self.template = "review-fix"
        self.go(f"go {PR}")
        self.assertEqual([(k, f["template"]) for k, _, f in self.logs],
                         [("go", "review-fix")])

    def test_go_comment_pr_checks_out_the_head_then_marks_it_reviewed(self):
        self.mode, self.template = "comment", "review-comment"
        self.go(f"go {PR}")
        fetches = [args[0] for args, _ in self.world.named("sh") if "fetch" in args[0]]
        self.assertIn(["git", "-C", "/wt/feat-b", "fetch", "origin", "pull/31/head"],
                      fetches)
        self.assertEqual(self.addresses(), ["herdr:w9"])
        it = scan.load()["items"][PR]
        self.assertEqual((it["reviewed_sha"], it["phase"], it["review_rounds"]),
                         (HEAD, "fixing", 1))

    def test_go_re_review_passes_the_previous_review_sha(self):
        self.mode, self.template, self.prev = "comment", "re-review", "old1234"
        self.go(f"go {PR}")
        self.assertIn("old1234", self.texts()[0])
        self.assertEqual(scan.load()["items"][PR]["reviewed_sha"], HEAD)

    def test_go_comment_pr_whose_checkout_fails_is_not_sent(self):
        self.mode, self.template = "comment", "review-comment"

        def dirty(args, cwd=None, timeout=60):
            if "status" in args:
                return 0, " M file.py", ""
            return self.git(args, cwd, timeout)
        with mock.patch.object(scan, "sh", side_effect=dirty):
            out = self.go(f"go {PR}")
        self.assertEqual(self.addresses(), [])
        self.assertIn("uncommitted", out)
        self.assertIn("uncommitted", scan.load()["items"][PR]["engine_error"])

    def test_go_refused_when_already_tracked(self):
        self.save({PR: {"mode": "fix", "branch": "feat/b", "session": "w9",
                        "phase": "working", "pr": 31}})
        out = self.go(f"go {PR}")
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
        out = self.go(f"go {PR}")
        self.assertEqual(self.spawns(), [])
        self.assertIn("already tracked as", out)

    def test_go_refused_for_an_uncloned_repo_with_the_clone_hint(self):
        other = "other/repo!4"
        self.snapshot(rows=[{"kind": "UNCLONED", "key": other, "title": "Old"}])
        out = self.go(f"go {other}")
        self.assertIn("clone it first", out)
        self.assertEqual(self.spawns(), [])
        self.assertEqual([k for k, _, _ in self.logs], ["go-refused"])

    def test_go_held_past_wind_down_until_confirmed(self):
        self.snapshot(rows=[{"kind": "PROPOSE", "key": ISSUE, "title": "Add login"}])
        with mock.patch.object(scan, "wind_down", return_value="yes"):
            out = self.go(f"go {ISSUE}")
            self.assertEqual(self.spawns(), [])
            self.assertEqual(out, f"{ISSUE} held (wind-down): Add login")
            self.assertEqual([k for k, _, _ in self.logs], ["held"])
            self.go(f"go {ISSUE} (confirmed past wind-down)")
        self.assertEqual(len(self.spawns()), 1)

    def test_go_held_over_the_cap_until_confirmed_over_cap(self):
        self.snapshot(active=scan.MAX_ACTIVE)
        with mock.patch.object(scan, "wind_down", return_value=None):
            out = self.go(f"go {ISSUE}")
            self.assertEqual(self.spawns(), [])
            self.assertIn("held (cap)", out)
            self.go(f"go {ISSUE} (confirmed over cap)")
        self.assertEqual(len(self.spawns()), 1)

    def test_untracked_sessions_count_against_the_cap(self):
        self.snapshot(active=scan.MAX_ACTIVE - 1, untracked=1)
        with mock.patch.object(scan, "wind_down", return_value=None):
            self.assertIn("held (cap)", self.go(f"go {ISSUE}"))

    def test_a_refused_spawn_is_a_refusal_not_a_crash(self):
        with mock.patch.object(scan, "spawn_item", side_effect=SystemExit("no clone")):
            out = self.go(f"go {ISSUE}")
        self.assertEqual(out, f"go {ISSUE} refused: spawn failed: no clone")
        self.assertEqual([k for k, _, _ in self.logs], ["go-refused"])

    def test_a_host_warning_queues_the_prompt_instead_of_sending(self):
        self.warning = "session started without its plugin"
        self.go(f"go {ISSUE}")
        self.assertEqual(self.addresses(), [])
        [entry] = scan.load()["pending_sends"]
        self.assertEqual((entry["key"], entry["template"], entry["reason"]),
                         (ISSUE, "work", "go"))
        self.assertIs(self.logs[-1][2].get("queued"), True)


if __name__ == "__main__":
    unittest.main()
