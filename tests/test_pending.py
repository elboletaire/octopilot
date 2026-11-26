import unittest
from unittest import mock

from tests.fake import fake_world
import scan
from engine import BLOCKED, Ctx, FAILED, deliver_pending, queue_send

K = "repo#7"


class PendingTest(unittest.TestCase):
    def setUp(self):
        # fake_world points STATE_FILE at a scratch dir, so save/load/locked
        # run for real without touching the live state.
        cm = fake_world()
        cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)
        self.render = self.patch("render_for", return_value="TEXT")
        self.send = self.patch("send_item", return_value=0)
        self.logs = []

    def patch(self, name, **kw):
        patcher = mock.patch.object(scan, name, create=True, **kw)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def log(self, kind, key, **fields):
        self.logs.append((kind, key, fields))

    def ctx(self, **kw):
        return Ctx(dry_run=kw.get("dry_run", False), sent_keys=kw.get("sent", set()),
                   log=self.log, rows=[])

    def save_with_send(self, key=K, items=(K,)):
        state = {"items": {k: {"branch": "x", "phase": "booting"} for k in items}, "pending_sends": []}
        queue_send(state, key, "review-fix", {"PR": "7"}, "boot")
        scan.save(state)

    def pending(self):
        return scan.load()["pending_sends"]


class DeliverTest(PendingTest):
    def test_sent_entry_is_popped(self):
        self.save_with_send()
        out = deliver_pending(self.ctx())
        self.assertEqual(out, [f"{K}: sent"])
        self.assertEqual(self.pending(), [])
        self.render.assert_called_once()
        self.assertEqual(self.render.call_args.args[1:], ("review-fix", {"PR": "7"}))
        self.send.assert_called_once()

    def test_blocked_entry_kept_and_logged_once(self):
        self.save_with_send()
        self.send.return_value = 4
        deliver_pending(self.ctx())
        deliver_pending(self.ctx())
        entries = self.pending()
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0]["error"], entries[0]["tries"]), (BLOCKED, 0))
        self.assertEqual([kind for kind, _, _ in self.logs], ["send-waiting"])

    def test_failed_entry_counts_tries(self):
        self.save_with_send()
        self.send.return_value = 1
        deliver_pending(self.ctx())
        deliver_pending(self.ctx())
        entry = self.pending()[0]
        self.assertEqual((entry["error"], entry["tries"]), (FAILED, 2))
        self.assertEqual(self.logs, [])

    def test_missing_item_dropped_without_sending(self):
        self.save_with_send(items=())
        out = deliver_pending(self.ctx())
        self.assertEqual(out, [f"{K}: dropped, item gone"])
        self.assertEqual(self.pending(), [])
        self.send.assert_not_called()
        self.assertEqual([kind for kind, _, _ in self.logs], ["send-dropped"])

    def test_one_send_per_item_per_run(self):
        self.save_with_send()
        sent = {K}
        out = deliver_pending(self.ctx(sent=sent))
        self.assertEqual(out, [])
        self.send.assert_not_called()
        self.assertEqual(len(self.pending()), 1)

    def test_sent_key_recorded_in_context(self):
        self.save_with_send()
        ctx = self.ctx()
        deliver_pending(ctx)
        self.assertEqual(ctx.sent_keys, {K})

    def test_dry_run_sends_nothing(self):
        self.save_with_send()
        out = deliver_pending(self.ctx(dry_run=True))
        self.assertEqual(out, [f"{K}: would send review-fix"])
        self.send.assert_not_called()
        self.assertEqual(len(self.pending()), 1)


class QueueTest(PendingTest):
    def test_newer_entry_replaces_older_for_same_key_and_reason(self):
        state = {"items": {}}
        queue_send(state, K, "review-fix", {"n": "1"}, "first")
        queue_send(state, K, "re-review", {"n": "2"}, "first")
        self.assertEqual(len(state["pending_sends"]), 1)
        entry = state["pending_sends"][0]
        self.assertEqual((entry["template"], entry["subs"], entry["reason"]),
                         ("re-review", {"n": "2"}, "first"))
        self.assertEqual((entry["tries"], entry["error"]), (0, None))

    def test_other_keys_kept(self):
        state = {"items": {}}
        queue_send(state, K, "review-fix", {}, "a")
        queue_send(state, "repo#8", "review-fix", {}, "b")
        self.assertEqual([p["key"] for p in state["pending_sends"]], [K, "repo#8"])

    def test_newer_entry_replaces_one_queued_under_a_prev_key(self):
        state = {"items": {"repo#9": {"prev_keys": ["repo!5"]}}}
        queue_send(state, "repo!5", "review-fix", {}, "first")
        queue_send(state, "repo#9", "re-review", {}, "first")
        self.assertEqual([p["key"] for p in state["pending_sends"]], ["repo#9"])
        self.assertEqual(state["pending_sends"][0]["template"], "re-review")

    def test_other_reason_queues_behind_for_the_same_item(self):
        state = {"items": {K: {}}}
        queue_send(state, K, "rebase", {}, "rebase")
        queue_send(state, K, "address-feedback", {}, "feedback")
        self.assertEqual([p["reason"] for p in state["pending_sends"]],
                         ["rebase", "feedback"])


class ReKeyedTest(PendingTest):
    def setUp(self):
        super().setUp()
        state = {"items": {"repo#9": {"branch": "x", "phase": "booting", "prev_keys": ["repo!5"]}},
                 "pending_sends": []}
        queue_send(state, "repo!5", "review-fix", {}, "boot")
        scan.save(state)

    def test_one_send_per_item_across_its_keys(self):
        out = deliver_pending(self.ctx(sent={"repo#9"}))
        self.assertEqual(out, [])
        self.send.assert_not_called()
        self.assertEqual(len(self.pending()), 1)

    def test_sent_key_is_the_current_key(self):
        ctx = self.ctx()
        deliver_pending(ctx)
        self.assertEqual(ctx.sent_keys, {"repo#9"})
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.args[1], "repo#9")

    def test_dry_run_checks_the_current_key_too(self):
        ctx = self.ctx(dry_run=True, sent={"repo#9"})
        self.assertEqual(deliver_pending(ctx), [])
        self.send.assert_not_called()


class FailureTest(PendingTest):
    def queue_two(self):
        state = {"items": {K: {}, "repo#8": {}}, "pending_sends": []}
        queue_send(state, K, "review-fix", {}, "a")
        queue_send(state, "repo#8", "review-fix", {}, "b")
        scan.save(state)

    def test_render_error_counts_a_try_and_next_entry_still_goes(self):
        self.queue_two()
        self.render.side_effect = [RuntimeError("boom"), "TEXT"]
        out = deliver_pending(self.ctx())
        self.assertEqual(out, [f"{K}: failed, try 1", "repo#8: sent"])
        entry = self.pending()[0]
        self.assertEqual(entry["key"], K)
        self.assertTrue(entry["error"].startswith(FAILED))
        self.assertIn("boom", entry["error"])

    def test_send_exit_raising_system_exit_is_a_failed_try(self):
        self.save_with_send()
        self.send.side_effect = SystemExit("bad item")
        out = deliver_pending(self.ctx())
        self.assertEqual(out, [f"{K}: failed, try 1"])
        self.assertEqual(self.pending()[0]["tries"], 1)


class DryRunTest(PendingTest):
    def test_dry_run_reports_missing_item_without_dropping(self):
        self.save_with_send(items=())
        out = deliver_pending(self.ctx(dry_run=True))
        self.assertEqual(out, [f"{K}: would drop, item gone"])
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(self.logs, [])


if __name__ == "__main__":
    unittest.main()
