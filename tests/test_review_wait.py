"""A fix PR waiting on someone else's review: DONE `waiting on review` or
`waiting on re-review` (no slot), READY once the review is in, or READY
`re-review overdue` once the re-review has waited RE_REVIEW_QUIET_HOURS."""
import copy
import time
import unittest
from unittest import mock

from tests import fake
import scan

ME = "octo-user"
OTHER = "reviewer"
KEY = "octo/app#5"
HEAD = "2222222bbbb"
T0 = "2026-11-10T10:00:00Z"
T1 = "2026-11-11T10:00:00Z"


def review(login, state, at=T0):
    return {"author": {"login": login}, "state": state, "submittedAt": at,
            "commit": {"oid": HEAD}, "body": "nit"}


def detail(decision=None, requests=(), reviews=()):
    return {"url": "https://github.com/octo/app/pull/5", "title": "Fix it",
            "state": "OPEN", "isDraft": False, "headRefOid": HEAD,
            "headRefName": "feat/x", "statusCheckRollup": [],
            "reviews": list(reviews), "comments": [],
            "reviewRequests": [{"login": who} for who in requests],
            "reviewDecision": decision}


class ReviewWaitTest(unittest.TestCase):
    def test_review_required_and_asked_of_others_is_a_wait(self):
        self.assertEqual(scan.review_wait(detail("REVIEW_REQUIRED", [OTHER]), ME),
                         "waiting on review")

    def test_review_required_and_asked_of_me_alone_is_not_a_wait(self):
        self.assertIsNone(scan.review_wait(detail("REVIEW_REQUIRED", [ME]), ME))

    def test_approved_is_not_a_wait(self):
        self.assertIsNone(scan.review_wait(detail("APPROVED"), ME))

    def test_changes_requested_by_another_is_a_re_review_wait(self):
        d = detail("CHANGES_REQUESTED", reviews=[review(OTHER, "CHANGES_REQUESTED")])
        self.assertEqual(scan.review_wait(d, ME), "waiting on re-review")

    def test_changes_requested_by_me_alone_is_not_a_wait(self):
        d = detail("CHANGES_REQUESTED", reviews=[review(ME, "CHANGES_REQUESTED")])
        self.assertIsNone(scan.review_wait(d, ME))

    def test_without_a_decision_only_others_requests_wait(self):
        self.assertEqual(scan.review_wait(detail(None, [OTHER]), ME), "waiting on review")
        self.assertIsNone(scan.review_wait(detail(None, [ME]), ME))

    def test_each_author_latest_review_is_the_one_that_counts(self):
        d = detail("CHANGES_REQUESTED", reviews=[
            review(OTHER, "CHANGES_REQUESTED", T0), review(OTHER, "APPROVED", T1)])
        self.assertEqual(scan.review_blocks(d, ME), [])
        d = detail("CHANGES_REQUESTED", reviews=[
            review(OTHER, "CHANGES_REQUESTED", T0), review("third", "CHANGES_REQUESTED", T1)])
        self.assertEqual([r["author"]["login"] for r in scan.review_blocks(d, ME)],
                         [OTHER, "third"])

    def test_rereview_clock_starts_at_the_first_scan_that_sees_it(self):
        it = {}
        start = 1_000_000
        with mock.patch("scan.time.time", return_value=start):
            self.assertIsNone(scan.rereview_hours(it, HEAD, T0))
        with mock.patch("scan.time.time", return_value=start + 3 * 3600):
            self.assertIsNone(scan.rereview_hours(it, HEAD, T0))
        with mock.patch("scan.time.time", return_value=start + 5 * 3600):
            self.assertAlmostEqual(scan.rereview_hours(it, HEAD, T0), 5)

    def test_a_new_head_or_review_restarts_the_clock(self):
        it = {}
        with mock.patch("scan.time.time", return_value=1_000_000):
            scan.rereview_hours(it, HEAD, T0)
        with mock.patch("scan.time.time", return_value=1_000_000 + 9 * 3600):
            self.assertIsNone(scan.rereview_hours(it, "3333333ccc", T0))
            self.assertIsNone(scan.rereview_hours(it, "3333333ccc", T1))


def fix_item(**extra):
    item = {"mode": "fix", "pr": 5, "branch": "feat/x", "session": "w1",
            "reviewed_sha": HEAD, "review_rounds": 1}
    item.update(extra)
    return item


def scan_rows(details, item):
    """The (kind, detail) rows of one fix item, with PR `number` at details[number]."""
    state = copy.deepcopy(scan.EMPTY)
    state["me"] = ME
    state["items"] = {KEY: item}
    with fake.fake_world(pr_detail=lambda slug, number: details[number]):
        found, _, _, _ = scan.scan(state)
    return [(kind, text) for kind, k, text in found if k == KEY]


class ReviewWaitRowsTest(unittest.TestCase):
    def test_review_owed_by_someone_else_is_done_waiting_on_review(self):
        rows = scan_rows({5: detail("REVIEW_REQUIRED", [OTHER])}, fix_item())
        self.assertEqual([kind for kind, _ in rows], ["DONE"])
        self.assertIn("-> waiting on review", rows[0][1])

    def test_approved_is_ready_to_merge(self):
        rows = scan_rows({5: detail("APPROVED")}, fix_item())
        self.assertEqual([kind for kind, _ in rows], ["READY"])
        self.assertIn("-> you merge", rows[0][1])

    def test_review_asked_of_me_alone_is_ready(self):
        rows = scan_rows({5: detail("REVIEW_REQUIRED", [ME])}, fix_item())
        self.assertEqual([kind for kind, _ in rows], ["READY"])

    def test_changes_requested_and_addressed_is_done_waiting_on_re_review(self):
        d = detail("CHANGES_REQUESTED", reviews=[review(OTHER, "CHANGES_REQUESTED")])
        rows = scan_rows({5: d}, fix_item())
        self.assertEqual([kind for kind, _ in rows], ["DONE"])
        self.assertIn("-> waiting on re-review", rows[0][1])

    def test_a_re_review_waiting_past_the_quiet_hours_is_overdue(self):
        d = detail("CHANGES_REQUESTED", reviews=[review(OTHER, "CHANGES_REQUESTED")])
        item = fix_item(rereview_wait={"head": HEAD, "review": T0,
                                       "at": int(time.time()) - 5 * 3600})
        rows = scan_rows({5: d}, item)
        self.assertEqual([kind for kind, _ in rows], ["READY"])
        self.assertIn("wait=overdue -> re-review overdue (5h)", rows[0][1])

    def test_a_fresh_re_review_wait_is_still_done(self):
        d = detail("CHANGES_REQUESTED", reviews=[review(OTHER, "CHANGES_REQUESTED")])
        item = fix_item(rereview_wait={"head": HEAD, "review": T0,
                                       "at": int(time.time()) - 60})
        rows = scan_rows({5: d}, item)
        self.assertEqual([kind for kind, _ in rows], ["DONE"])

    def test_on_a_stack_the_bottom_layer_decides(self):
        stacked = fix_item(pr=6, stack=[{"pr": 5, "branch": "feat/x"},
                                        {"pr": 6, "branch": "feat/top"}])
        waiting = {5: detail("REVIEW_REQUIRED", [OTHER]),
                   6: detail("APPROVED")}
        rows = scan_rows(waiting, stacked)
        self.assertEqual([kind for kind, _ in rows], ["DONE"])
        approved_bottom = {5: detail("APPROVED"),
                           6: detail("REVIEW_REQUIRED", [OTHER])}
        rows = scan_rows(approved_bottom, stacked)
        self.assertEqual([kind for kind, _ in rows], ["READY"])


if __name__ == "__main__":
    unittest.main()
