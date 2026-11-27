"""Agent reports that stop an agent-driven item: BLOCKED keeps its slot, HOLD
(the user's pause) gives it back. A quiet agent with no report stays STALLED."""
import copy
import unittest
from unittest import mock

from tests import fake
import engine
import scan

ME = "octo-user"
KEY = "octo/app#5"
HEAD = "2222222bbbb"
LAST_SEEN = 1_000_000
LATER = LAST_SEEN + 50 * 60   # past AGENT_STALL_MIN with no heartbeat


def report(state, at=LAST_SEEN + 100, msg="the tests need a db"):
    return {"state": state, "sha": HEAD, "msg": msg, "at": at}


def detail():
    return {"url": "https://github.com/octo/app/pull/5", "title": "Fix it",
            "state": "OPEN", "isDraft": False, "headRefOid": HEAD,
            "headRefName": "feat/x", "statusCheckRollup": [],
            "reviews": [], "comments": [], "reviewRequests": [],
            "reviewDecision": None}


def agent_item(**extra):
    item = {"mode": "fix", "pr": 5, "branch": "feat/x", "session": "w1",
            "driver": "agent", "review_rounds": 1,
            "last_seen": LAST_SEEN}
    item.update(extra)
    return item


def no_pr_item(**extra):
    item = {"mode": "fix", "branch": "feat/x", "session": "w1",
            "driver": "agent", "last_seen": LAST_SEEN}
    item.update(extra)
    return item


def scan_at(item, when=LATER):
    """(rows for KEY, active) of one item scanned at `when`."""
    state = copy.deepcopy(scan.EMPTY)
    state["me"] = ME
    state["items"] = {KEY: item}
    with fake.fake_world(pr_detail=lambda slug, number: detail()), \
            mock.patch("scan.time.time", return_value=when):
        rows, active, _, _ = scan.scan(state)
    return [(kind, text) for kind, k, text in rows if k == KEY], active


class HoldRowsTest(unittest.TestCase):
    def test_a_failed_agent_is_blocked_not_stalled(self):
        rows, active = scan_at(agent_item(last_report=report("failed")))
        self.assertEqual([kind for kind, _ in rows], ["BLOCKED"])
        self.assertIn("report=failed", rows[0][1])
        self.assertIn("-> the tests need a db", rows[0][1])
        self.assertEqual(active, 1)

    def test_a_quiet_agent_with_no_report_is_still_stalled(self):
        rows, _ = scan_at(agent_item())
        self.assertEqual([kind for kind, _ in rows], ["STALLED"])

    def test_a_report_older_than_the_last_heartbeat_is_history(self):
        item = agent_item(last_report=report("failed", at=LAST_SEEN - 1))
        rows, _ = scan_at(item)
        self.assertEqual([kind for kind, _ in rows], ["STALLED"])

    def test_held_is_hold_for_you_and_holds_no_slot(self):
        rows, active = scan_at(agent_item(last_report=report("held", msg="")))
        self.assertEqual([kind for kind, _ in rows], ["HOLD"])
        self.assertIn("hold=you", rows[0][1])
        self.assertIn("-> held as you asked", rows[0][1])
        self.assertEqual(active, 0)

    def test_a_no_pr_item_reporting_blocked_is_blocked(self):
        item = no_pr_item(last_report=report("blocked"))
        rows, active = scan_at(item)
        self.assertEqual([kind for kind, _ in rows], ["BLOCKED"])
        self.assertIn("report=blocked", rows[0][1])
        self.assertEqual(active, 1)

    def test_a_no_pr_item_reporting_held_is_hold_and_holds_no_slot(self):
        item = no_pr_item(last_report=report("held", msg="later"))
        rows, active = scan_at(item)
        self.assertEqual([kind for kind, _ in rows], ["HOLD"])
        self.assertEqual(active, 0)

    def test_a_running_agent_ignores_its_old_report(self):
        state = copy.deepcopy(scan.EMPTY)
        state["me"] = ME
        state["items"] = {KEY: agent_item(last_report=report("failed"))}
        live = {"w1": {"state": "running", "age_secs": 5}}
        with fake.fake_world(pr_detail=lambda slug, number: detail(),
                             all_live=live), \
                mock.patch("scan.time.time", return_value=LATER):
            rows, _, _, _ = scan.scan(state)
        self.assertEqual([kind for kind, k, _ in rows if k == KEY], ["WORKING"])


class HoldNoteTest(unittest.TestCase):
    def test_a_quoted_multi_line_message_stays_one_note(self):
        msg = 'wait for "infra"\nfix the\tdb'
        rows, _ = scan_at(agent_item(last_report=report("blocked", msg=msg)))
        text = rows[0][1]
        self.assertNotIn('"', text)
        self.assertNotIn("\n", text)
        self.assertTrue(text.endswith("-> wait for infra fix the db"), text)
        self.assertEqual(scan.parse_extra(text)["fields"]["report"], "blocked")
        self.assertNotIn("title", scan.parse_extra(text))

    def test_an_empty_blocked_message_says_the_agent_stopped(self):
        rows, _ = scan_at(agent_item(last_report=report("failed", msg="")))
        self.assertTrue(rows[0][1].endswith("-> agent stopped (failed)"), rows[0][1])

    def test_an_empty_held_message_says_held_as_you_asked(self):
        rows, _ = scan_at(no_pr_item(last_report=report("held", msg="")))
        self.assertTrue(rows[0][1].endswith("-> held as you asked"), rows[0][1])


class HoldReviewedHeadTest(unittest.TestCase):
    def test_a_blocked_agent_on_its_reviewed_head_is_not_ready(self):
        rows, active = scan_at(agent_item(reviewed_sha=HEAD,
                                          last_report=report("blocked")))
        self.assertEqual([kind for kind, _ in rows], ["BLOCKED"])
        self.assertEqual(active, 1)


class HoldEngineTest(unittest.TestCase):
    def test_blocked_and_hold_rows_are_tracked_but_not_planned(self):
        for kind in ("BLOCKED", "HOLD"):
            self.assertIn(kind, engine.TRACKED)
            self.assertNotIn(kind, engine.PLANNED)


class HoldSnapshotTest(unittest.TestCase):
    def test_a_hold_row_carries_who_holds_it_and_why(self):
        state = copy.deepcopy(scan.EMPTY)
        state["me"] = ME
        state["items"] = {KEY: agent_item(last_report=report("held", msg="wait for infra"))}
        with fake.fake_world(pr_detail=lambda slug, number: detail()), \
                mock.patch("scan.time.time", return_value=LATER):
            snap = scan.snapshot(state)
        (row,) = [r for r in snap["rows"] if r["key"] == KEY]
        self.assertEqual(row["kind"], "HOLD")
        self.assertEqual(row["hold"], {"by": "you", "reason": "wait for infra"})

    def test_rows_without_a_hold_field_have_no_hold(self):
        state = copy.deepcopy(scan.EMPTY)
        state["me"] = ME
        state["items"] = {KEY: agent_item(last_report=report("failed"))}
        with fake.fake_world(pr_detail=lambda slug, number: detail()), \
                mock.patch("scan.time.time", return_value=LATER):
            snap = scan.snapshot(state)
        (row,) = [r for r in snap["rows"] if r["key"] == KEY]
        self.assertNotIn("hold", row)


class HoldOrderTest(unittest.TestCase):
    def test_blocked_follows_stalled_and_hold_precedes_done(self):
        self.assertLess(scan.ORDER.index("STALLED"), scan.ORDER.index("BLOCKED"))
        self.assertLess(scan.ORDER.index("HOLD"), scan.ORDER.index("DONE"))


if __name__ == "__main__":
    unittest.main()
