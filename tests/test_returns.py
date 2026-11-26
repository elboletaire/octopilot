"""The return-value functions the CLI and the executor share: the state they
leave behind and what they return, with the GitHub, herdr and git calls faked."""
import json
import unittest
from unittest import mock

from tests import fake
import scan
from tests.test_rerequest import KEY, HEAD, REVIEWED, new_state, pr_detail


def events():
    """The event kinds logged so far; call it inside the fake world."""
    try:
        with open(scan.EVENTS_FILE) as fh:
            return [json.loads(line)["kind"] for line in fh]
    except FileNotFoundError:
        return []


class FakeHost:
    name = "herdr"
    prefix = "herdr:"
    noun = "workspace"
    remove_label = "removed"

    def __init__(self, close=(0, ""), discard=(0, "")):
        self._close, self._discard = close, discard
        self.closed, self.discarded = [], []

    def close(self, session):
        self.closed.append(session)
        return self._close

    def discard(self, session, worktree):
        self.discarded.append((session, worktree))
        return self._discard


def fix_item(pr, branch, **extra):
    item = {"mode": "fix", "pr": pr, "branch": branch, "session": "w1",
            "phase": "fixing", "review_rounds": 2, "feedback_seen": "2026-11-01T00:00:00Z"}
    item.update(extra)
    return item


class CleanupItemTest(unittest.TestCase):
    def test_untracked_key_changes_nothing(self):
        state = new_state()
        with fake.fake_world():
            r = scan.cleanup_item(state, "octo/app#99")
        self.assertEqual(r, {"ok": False, "msg": "octo/app#99 is not tracked; nothing to clean up",
                             "siblings": [], "base": None})

    def test_cleanup_drops_the_item_and_names_the_siblings(self):
        state = new_state()
        state["items"]["octo/app#6"] = fix_item(6, "feat/six", session="w2")
        state["items"]["octo/app#7"] = {"mode": "comment", "pr": 7, "branch": "x", "session": "w3"}
        host = FakeHost(discard=(0, ""))
        with fake.fake_world(backend_for=host):
            r = scan.cleanup_item(state, KEY)
            self.assertIn("cleanup", events())
        self.assertTrue(r["ok"])
        self.assertEqual(r["msg"], "removed w1: ok\ncleaned up octo/app#5")
        self.assertEqual(r["base"], "main")
        self.assertEqual([k for k, _ in r["siblings"]], ["octo/app#6"])
        self.assertNotIn(KEY, state["items"])
        self.assertEqual(state["handled_merges"], [KEY])
        self.assertEqual(host.discarded, [("w1", None)])

    def test_no_backend_means_no_backend_line(self):
        state = new_state()
        with fake.fake_world():
            r = scan.cleanup_item(state, KEY)
        self.assertEqual(r["msg"], "cleaned up octo/app#5")


class RestackItemTest(unittest.TestCase):
    def stack_state(self, layers):
        state = new_state()
        state["registry"] = {"octo/app": {"path": "/p", "base": "main"}}
        state["items"] = {"octo/app#1": fix_item(
            1, "feat/a", stack=layers,
            layer_seen={"1": "2026-11-02T00:00:00Z", "2": "2026-11-03T00:00:00Z"})}
        return state

    def details(self, states):
        def detail_of(slug, number):
            return {"state": states.get(number, "OPEN"), "headRefOid": f"sha{number}"}
        return detail_of

    def test_refuses_without_a_stack(self):
        state = new_state()
        state["items"][KEY]["mode"] = "fix"
        with fake.fake_world():
            r = scan.restack_item(state, KEY)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], f"{KEY} is not a stack")
        self.assertIsNone(r["new_key"])

    def test_refuses_when_the_bottom_is_not_merged(self):
        state = self.stack_state([{"pr": 1, "branch": "feat/a"}, {"pr": 2, "branch": "feat/b"}])
        with fake.fake_world(pr_detail=self.details({})):
            r = scan.restack_item(state, "octo/app#1")
        self.assertEqual(r["error"], "octo/app#1: the bottom PR is not merged; nothing to restack")
        self.assertIn("octo/app#1", state["items"])

    def test_restack_rekeys_to_the_next_layer_and_asks_for_rebase_stack(self):
        state = self.stack_state([{"pr": 1, "branch": "feat/a"},
                                  {"pr": 2, "branch": "feat/b"},
                                  {"pr": 3, "branch": "feat/c"}])
        with fake.fake_world(pr_detail=self.details({1: "MERGED"})):
            r = scan.restack_item(state, "octo/app#1")
        self.assertTrue(r["ok"])
        self.assertIsNone(r["error"])
        self.assertEqual(r["new_key"], "octo/app#2")
        self.assertEqual(r["template"], "rebase-stack")
        self.assertEqual(r["subs"], {"REPO": "octo/app", "BASE": "main",
                                     "KEY": "octo/app#2", "RESTACK_FROM": "sha1"})
        self.assertEqual(r["siblings"], [])
        item = state["items"]["octo/app#2"]
        self.assertEqual(item["branch"], "feat/b")
        self.assertEqual(item["prev_keys"], ["octo/app#1"])
        self.assertEqual([layer["pr"] for layer in item["stack"]], [2, 3])
        self.assertNotIn("octo/app#1", state["items"])
        self.assertEqual(state["handled_merges"], ["octo/app#1"])

    def test_last_layer_becomes_a_plain_pr_asking_for_rebase(self):
        state = self.stack_state([{"pr": 1, "branch": "feat/a"}, {"pr": 2, "branch": "feat/b"}])
        with fake.fake_world(pr_detail=self.details({1: "MERGED"})):
            r = scan.restack_item(state, "octo/app#1")
        self.assertEqual(r["template"], "rebase")
        self.assertEqual(r["subs"], {"REPO": "octo/app", "PR": 2, "BRANCH": "feat/b",
                                     "BASE": "main"})
        item = state["items"]["octo/app#2"]
        self.assertNotIn("stack", item)
        self.assertNotIn("layer_seen", item)
        # The last layer's own mark becomes the plain PR's watermark.
        self.assertEqual(item["feedback_seen"], "2026-11-03T00:00:00Z")


class SpawnItemTest(unittest.TestCase):
    def registry_state(self):
        state = new_state()
        state["registry"] = {"octo/app": {"path": "/repo/app", "base": "main"}}
        state["items"] = {}
        return state

    def host(self, warning=None):
        def spawn_host(key, path, base, branch, title, new_branch, model):
            host = {"backend": "herdr", "session": "w7", "agent": "ap-app-1",
                    "worktree": "/repo/app/.worktrees/feat-x"}
            if warning:
                host["warning"] = warning
            return host
        return spawn_host

    def test_issue_spawn_tracks_the_item_and_returns_its_facts(self):
        state = self.registry_state()
        with fake.fake_world(), \
                mock.patch.object(scan, "find_session", return_value=None), \
                mock.patch.object(scan, "spawn_host", side_effect=self.host()):
            r = scan.spawn_item(state, "octo/app!40", "feat/x", "Title", True)
            self.assertIn("spawn", events())
        self.assertEqual(r, {"key": "octo/app!40", "template": "work", "base": "main",
                             "slug": "octo/app", "prev": None, "stack": None,
                             "warning": None, "model": scan.MODEL_FOR.get("work", "")})
        item = state["items"]["octo/app!40"]
        self.assertEqual((item["mode"], item["branch"], item["session"]), ("fix", "feat/x", "w7"))

    def test_warning_comes_back_and_the_item_is_still_tracked(self):
        state = self.registry_state()
        with fake.fake_world(), \
                mock.patch.object(scan, "find_session", return_value=None), \
                mock.patch.object(scan, "spawn_host", side_effect=self.host("blocked")):
            r = scan.spawn_item(state, "octo/app!40", "feat/x", "Title", True)
        self.assertEqual(r["warning"], "blocked")
        self.assertIn("octo/app!40", state["items"])

    def test_unregistered_repo_is_refused_before_anything_is_created(self):
        state = self.registry_state()
        with fake.fake_world(), \
                mock.patch.object(scan, "spawn_host", side_effect=AssertionError("spawned")):
            with self.assertRaises(SystemExit):
                scan.spawn_item(state, "other/repo!1", "feat/x", "Title", True)
        self.assertEqual(state["items"], {})

    def test_a_refused_stack_leaves_no_trace(self):
        state = self.registry_state()
        with fake.fake_world(), \
                mock.patch.object(scan, "spawn_host", side_effect=AssertionError("spawned")), \
                mock.patch.object(scan, "check_stack", return_value=["fix", "comment"]):
            with self.assertRaises(SystemExit):
                scan.spawn_item(state, "octo/app#9", "feat/c", "T", False,
                                stack_spec="9:feat/c 10:feat/d")
        self.assertEqual(state["items"], {})

    def test_reviewed_comment_pr_gets_the_re_review_prompt(self):
        state = self.registry_state()
        detail = pr_detail("octo/app", 5, requested=False)
        detail["author"] = {"login": "someone"}
        with fake.fake_world(pr_detail=detail, gh_json=lambda *a, **kw: {"author": {"login": "someone"}}), \
                mock.patch.object(scan, "find_session", return_value=None), \
                mock.patch.object(scan, "spawn_host", side_effect=self.host()):
            r = scan.spawn_item(state, KEY, "feat/x", "T", False)
        self.assertEqual(r["template"], "re-review")
        self.assertEqual(r["prev"], REVIEWED)
        self.assertEqual(state["items"][KEY]["reviewed_sha"], REVIEWED)


class StateFunctionsTest(unittest.TestCase):
    def test_mark_reviewed_counts_a_round(self):
        state = new_state()
        with fake.fake_world():
            r = scan.mark_reviewed(state, KEY, "abcdef1234")
        self.assertEqual(r, {"ok": True, "round": 2})
        self.assertEqual(state["items"][KEY]["reviewed_sha"], "abcdef1234")
        self.assertEqual(state["items"][KEY]["phase"], "fixing")

    def test_mark_reviewed_on_an_untracked_key_is_not_ok(self):
        state = new_state()
        with fake.fake_world():
            self.assertEqual(scan.mark_reviewed(state, "octo/app#99", "abc"),
                             {"ok": False, "round": None})
        self.assertEqual(state["items"][KEY]["review_rounds"], 1)

    def test_mark_feedback_on_a_plain_pr_resets_rounds(self):
        state = new_state()
        state["items"][KEY]["capped_at"] = 5
        with fake.fake_world():
            r = scan.mark_feedback(state, KEY, "2026-11-20T00:00:00Z", pr=99)
        self.assertEqual(r, {"ok": True, "error": None, "pr": None})
        item = state["items"][KEY]
        self.assertEqual(item["feedback_seen"], "2026-11-20T00:00:00Z")
        self.assertEqual(item["review_rounds"], 0)
        self.assertNotIn("capped_at", item)

    def test_mark_feedback_moves_only_the_named_layer(self):
        state = new_state()
        state["items"][KEY] = {"mode": "fix", "pr": 5, "stack": [{"pr": 5, "branch": "a"},
                                                              {"pr": 6, "branch": "b"}],
                               "layer_seen": {"5": "2026-11-01T00:00:00Z"}}
        with fake.fake_world():
            r = scan.mark_feedback(state, KEY, "2026-11-20T00:00:00Z", pr=6)
        self.assertEqual(r["pr"], 6)
        self.assertEqual(state["items"][KEY]["layer_seen"],
                         {"5": "2026-11-01T00:00:00Z", "6": "2026-11-20T00:00:00Z"})

    def test_mark_feedback_refuses_a_stack_without_a_layer(self):
        state = new_state()
        state["items"][KEY] = {"mode": "fix", "stack": [{"pr": 5, "branch": "a"},
                                                       {"pr": 6, "branch": "b"}]}
        with fake.fake_world():
            r = scan.mark_feedback(state, KEY, "2026-11-20T00:00:00Z")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], f"{KEY} is a stack: name one of its layers (5 6)")
        self.assertNotIn("layer_seen", state["items"][KEY])

    def test_mark_feedback_on_an_untracked_key_is_silent(self):
        with fake.fake_world():
            r = scan.mark_feedback(new_state(), "octo/app#99", "x")
        self.assertEqual(r, {"ok": False, "error": None, "pr": None})

    def test_decline_adds_each_key_once(self):
        state = new_state()
        state["declined"] = ["octo/app#2"]
        with fake.fake_world():
            added = scan.decline(state, ["octo/app#2", "octo/app#3", "octo/app#3"])
            self.assertEqual(events().count("decline"), 3)
        self.assertEqual(added, ["octo/app#3"])
        self.assertEqual(state["declined"], ["octo/app#2", "octo/app#3"])

    def test_ack_push_stores_the_head(self):
        state = new_state()
        with fake.fake_world(pr_detail=pr_detail):
            r = scan.ack_push(state, KEY)
        self.assertEqual(r, {"ok": True, "error": None, "push_acked": HEAD})
        self.assertEqual(state["items"][KEY]["push_acked"], HEAD)

    def test_ack_push_refuses_an_item_without_a_pr(self):
        state = new_state()
        state["items"][KEY]["pr"] = None
        with fake.fake_world():
            r = scan.ack_push(state, KEY)
        self.assertEqual(r, {"ok": False, "error": f"unknown item {KEY}", "push_acked": None})

    def test_detach_closes_the_workspace_and_drops_the_item(self):
        state = new_state()
        host = FakeHost(close=(0, ""))
        with fake.fake_world(backend_for=host):
            code, msg = scan.detach_item(state, KEY)
        self.assertEqual(code, 0)
        self.assertEqual(msg, f"detached {KEY} session=w1 branch=feat/x -> workspace closed, "
                              "worktree kept, untracked")
        self.assertEqual(host.closed, ["w1"])
        self.assertNotIn(KEY, state["items"])

    def test_detach_keeps_the_item_when_the_close_fails(self):
        state = new_state()
        host = FakeHost(close=(1, "busy"))
        with fake.fake_world(backend_for=host):
            code, msg = scan.detach_item(state, KEY)
        self.assertEqual((code, msg), (1, "workspace close failed: busy"))
        self.assertIn(KEY, state["items"])

    def test_detach_untracked_key(self):
        with fake.fake_world():
            self.assertEqual(scan.detach_item(new_state(), "octo/app#99"),
                             (1, "unknown item octo/app#99"))

    def test_set_paused_sets_the_flag_and_logs_it(self):
        state = new_state()
        with fake.fake_world():
            self.assertTrue(scan.set_paused(state, True))
            self.assertTrue(state["paused"])
            self.assertFalse(scan.set_paused(state, False))
            self.assertFalse(state["paused"])
            self.assertEqual(events(), ["pause", "resume"])


if __name__ == "__main__":
    unittest.main()
