import unittest

from engine import Action, plan_rows

K = "repo#7"


def row(kind, key=K, **fields):
    return {"kind": kind, "key": key, "fields": {k: str(v) for k, v in fields.items()}}


class PlanRowsTest(unittest.TestCase):
    def test_merged_is_cleanup(self):
        state = {"items": {K: {}}, "handled_merges": []}
        self.assertEqual(plan_rows([row("MERGED")], state),
                         [Action("cleanup", K, {})])

    def test_restack_is_restack(self):
        state = {"items": {K: {"stack": [{"pr": 7}]}}}
        self.assertEqual(plan_rows([row("RESTACK")], state),
                         [Action("restack", K, {})])

    def test_review_is_review(self):
        state = {"items": {K: {"phase": "idle"}}}
        self.assertEqual(plan_rows([row("REVIEW", head="abc1234")], state),
                         [Action("review", K, {})])

    def test_booting_carries_template_and_prev(self):
        state = {"items": {K: {"phase": "booting"}}}
        rows = [row("BOOTING", mode="comment", template="re-review", PREV="abc1234")]
        self.assertEqual(plan_rows(rows, state),
                         [Action("boot", K, {"template": "re-review", "prev": "abc1234"})])

    def test_booting_without_prev(self):
        state = {"items": {K: {"phase": "booting"}}}
        rows = [row("BOOTING", mode="fix", template="review-fix")]
        self.assertEqual(plan_rows(rows, state),
                         [Action("boot", K, {"template": "review-fix", "prev": None})])

    def test_feedback_carries_since_pr_and_branch(self):
        state = {"items": {K: {"feedback_seen": "2026-11-20T00:00:00Z",
                               "layer_seen": {"9": "2026-11-20T00:00:00Z"}}}}
        rows = [row("FEEDBACK", mode="fix", pr=9, branch="feat/x",
                    since="2026-11-26T00:00:00Z")]
        self.assertEqual(plan_rows(rows, state),
                         [Action("feedback", K, {"since": "2026-11-26T00:00:00Z",
                                                 "pr": "9", "branch": "feat/x"})])

    def test_feedback_without_layer_has_no_pr(self):
        state = {"items": {K: {"feedback_seen": "2026-11-20T00:00:00Z"}}}
        rows = [row("FEEDBACK", mode="fix", since="2026-11-26T00:00:00Z")]
        self.assertEqual(plan_rows(rows, state),
                         [Action("feedback", K, {"since": "2026-11-26T00:00:00Z",
                                                 "pr": None, "branch": None})])

    def test_comment_mode_feedback_is_a_bug(self):
        state = {"items": {K: {"feedback_seen": "2026-11-20T00:00:00Z"}}}
        with self.assertRaises(AssertionError):
            plan_rows([row("FEEDBACK", mode="comment", since="2026-11-26T00:00:00Z")],
                      state)

    def test_other_kinds_are_not_planned(self):
        state = {"items": {K: {}}}
        for kind in ("CAPPED", "CLOSED", "DONE", "GONE", "PROPOSE", "PUSHED",
                     "READY", "STALLED", "UNKNOWN", "WORKING"):
            with self.subTest(kind=kind):
                self.assertEqual(plan_rows([row(kind)], state), [])

    def test_capped_stays_with_the_user(self):
        state = {"items": {K: {"review_rounds": 3}}}
        self.assertEqual(plan_rows([row("CAPPED", head="abc1234")], state), [])

    def test_paused_plans_nothing(self):
        state = {"paused": True, "items": {K: {"phase": "booting"}}}
        self.assertEqual(plan_rows([row("BOOTING", template="review-fix")], state), [])

    def test_stale_rows_are_dropped(self):
        state = {"items": {K: {"phase": "idle"}}, "handled_merges": [K]}
        rows = [row("BOOTING", template="review-fix"), row("MERGED")]
        self.assertEqual(plan_rows(rows, state), [])

    def test_row_without_item_is_dropped(self):
        self.assertEqual(plan_rows([row("MERGED")], {"items": {}}), [])

    def test_keeps_row_order(self):
        state = {"items": {K: {}, "repo#8": {}}, "handled_merges": []}
        rows = [row("MERGED", key="repo#8"), row("MERGED")]
        self.assertEqual([a.key for a in plan_rows(rows, state)], ["repo#8", K])


if __name__ == "__main__":
    unittest.main()
