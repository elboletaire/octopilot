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
        state = {"items": {k: {"branch": "x"} for k in items}, "pending_sends": []}
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
        self.assertEqual([kind for kind, _, _ in self.logs], ["send-blocked"])

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
    def test_newer_entry_replaces_older_for_same_key(self):
        state = {"items": {}}
        queue_send(state, K, "review-fix", {"n": "1"}, "first")
        queue_send(state, K, "re-review", {"n": "2"}, "second")
        self.assertEqual(len(state["pending_sends"]), 1)
        entry = state["pending_sends"][0]
        self.assertEqual((entry["template"], entry["subs"], entry["reason"]),
                         ("re-review", {"n": "2"}, "second"))
        self.assertEqual((entry["tries"], entry["error"]), (0, None))

    def test_other_keys_kept(self):
        state = {"items": {}}
        queue_send(state, K, "review-fix", {}, "a")
        queue_send(state, "repo#8", "review-fix", {}, "b")
        self.assertEqual([p["key"] for p in state["pending_sends"]], [K, "repo#8"])


if __name__ == "__main__":
    unittest.main()
