import unittest

from engine import revalidate_row

K = "repo#7"


def row(kind, key=K, **fields):
    return {"kind": kind, "key": key, "fields": {k: str(v) for k, v in fields.items()}}


class RevalidateRowTest(unittest.TestCase):
    def test_tracked_row_needs_its_item(self):
        self.assertFalse(revalidate_row(row("WORKING"), {"items": {}}))

    def test_tracked_row_found_through_prev_keys(self):
        state = {"items": {K: {"prev_keys": ["repo!3"]}}}
        self.assertTrue(revalidate_row(row("WORKING", key="repo!3"), state))

    def test_gone_row_kept_without_item(self):
        self.assertTrue(revalidate_row(row("GONE"), {"items": {}}))

    def test_merged_dropped_once_handled(self):
        item = {K: {"mode": "fix"}}
        self.assertTrue(revalidate_row(row("MERGED"), {"items": item, "handled_merges": []}))
        self.assertFalse(revalidate_row(row("MERGED"), {"items": item, "handled_merges": [K]}))

    def test_restack_needs_a_stack(self):
        self.assertTrue(revalidate_row(row("RESTACK"), {"items": {K: {"stack": [{"pr": 7}]}}}))
        self.assertFalse(revalidate_row(row("RESTACK"), {"items": {K: {"stack": []}}}))

    def test_review_dropped_while_booting(self):
        self.assertTrue(revalidate_row(row("REVIEW"), {"items": {K: {"phase": "idle"}}}))
        self.assertFalse(revalidate_row(row("REVIEW"), {"items": {K: {"phase": "booting"}}}))

    def test_booting_needs_booting_phase(self):
        self.assertTrue(revalidate_row(row("BOOTING"), {"items": {K: {"phase": "booting"}}}))
        self.assertFalse(revalidate_row(row("BOOTING"), {"items": {K: {"phase": "idle"}}}))

    def test_feedback_compares_layer_watermark_when_pr_set(self):
        item = {K: {"feedback_seen": "2026-11-20T00:00:00Z",
                    "layer_seen": {"9": "2026-11-25T00:00:00Z"}}}
        newer = row("FEEDBACK", pr=9, since="2026-11-26T00:00:00Z")
        older = row("FEEDBACK", pr=9, since="2026-11-24T00:00:00Z")
        self.assertTrue(revalidate_row(newer, {"items": item}))
        self.assertFalse(revalidate_row(older, {"items": item}))

    def test_feedback_without_pr_compares_feedback_seen(self):
        item = {K: {"feedback_seen": "2026-11-20T00:00:00Z",
                    "layer_seen": {"9": "2026-11-25T00:00:00Z"}}}
        self.assertTrue(revalidate_row(row("FEEDBACK", since="2026-11-21T00:00:00Z"),
                                       {"items": item}))
        self.assertFalse(revalidate_row(row("FEEDBACK", since="2026-11-19T00:00:00Z"),
                                        {"items": item}))

    def test_propose_dropped_when_tracked_or_declined(self):
        self.assertTrue(revalidate_row(row("PROPOSE"), {"items": {}, "declined": []}))
        self.assertFalse(revalidate_row(row("PROPOSE"),
                                        {"items": {}, "declined": [K]}))
        self.assertFalse(revalidate_row(row("PROPOSE"),
                                        {"items": {K: {"prev_keys": []}}}))

    def test_other_kinds_kept(self):
        self.assertTrue(revalidate_row(row("STALE"), {"items": {}}))
        self.assertTrue(revalidate_row(row("UNCLONED"), {"items": {}}))


if __name__ == "__main__":
    unittest.main()
