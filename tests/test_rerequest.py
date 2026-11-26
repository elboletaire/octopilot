import contextlib
import copy
import io
import unittest

from tests import fake
import scan

ME = "octo-user"
KEY = "octo/app#5"
REVIEWED = "1111111aaaa"
HEAD = "2222222bbbb"


class FakeBackend:
    name = "herdr"
    prefix = "herdr:"

    def target(self, it):
        return "w1"

    def wait_idle(self, target):
        return 0, ""


def pr_detail(slug, number):
    """PR #5, which we reviewed at REVIEWED; its author re-requested a review."""
    return {"url": f"https://github.com/{slug}/pull/{number}", "title": "Fix it",
            "state": "OPEN", "isDraft": False, "headRefOid": HEAD,
            "headRefName": "feat/x",
            "reviews": [{"author": {"login": ME}, "state": "CHANGES_REQUESTED",
                         "submittedAt": "2026-11-10T10:00:00Z",
                         "commit": {"oid": REVIEWED}, "body": "nit"}],
            "reviewRequests": [{"login": ME}]}


def new_state():
    state = copy.deepcopy(scan.EMPTY)
    state["me"] = ME
    state["items"] = {KEY: {"mode": "comment", "pr": 5, "branch": "feat/x",
                            "session": "w1", "reviewed_sha": REVIEWED,
                            "review_rounds": 1}}
    return state


def rows(state):
    found, _, _, _ = scan.scan(state)
    return [(kind, k) for kind, k, _ in found]


class RerequestTest(unittest.TestCase):
    def test_a_scan_repeated_before_the_row_is_acted_on_still_returns_it(self):
        state = new_state()
        with fake.fake_world(pr_detail=pr_detail):
            self.assertEqual(rows(state), [("REVIEW", KEY)])
            self.assertEqual(rows(state), [("REVIEW", KEY)])

    def test_reboot_marks_the_head_done_and_mark_reviewed_settles_it(self):
        state = new_state()
        with fake.fake_world(pr_detail=pr_detail, backend_for=lambda s: FakeBackend()):
            self.assertEqual(rows(state), [("REVIEW", KEY)])
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(scan.reboot(state, KEY), 0)
            self.assertEqual(state["items"][KEY]["rerequest_head"], HEAD)
            self.assertNotIn("pending_rerequest", state["items"][KEY])
            self.assertEqual(rows(state), [("BOOTING", KEY)])
            with contextlib.redirect_stdout(io.StringIO()):
                scan.dispatch("mark-reviewed", ["mark-reviewed", KEY, HEAD], state)
            self.assertEqual(rows(state), [("DONE", KEY)])
