"""Agent reports that stop an agent-driven item: BLOCKED keeps its slot, HOLD
(the user's pause) gives it back. A quiet agent with no report stays STALLED."""
import contextlib
import copy
import io
import json
import os
import unittest
from unittest import mock

from tests import fake
import engine
import scan

ME = "octo-user"
KEY = "octo/app#5"
HEAD = "2222222bbbb"
OTHER = "reviewer"
T0 = "2026-11-10T10:00:00Z"
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


def fix_item(**extra):
    item = {"mode": "fix", "pr": 5, "branch": "feat/x", "session": "w1",
            "review_rounds": 0}
    item.update(extra)
    return item


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

    def test_held_is_hold_by_the_agent_and_holds_no_slot(self):
        rows, active = scan_at(agent_item(last_report=report("held", msg="")))
        self.assertEqual([kind for kind, _ in rows], ["HOLD"])
        self.assertIn("hold=agent", rows[0][1])
        self.assertIn("-> held, no reason given", rows[0][1])
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
        self.assertTrue(rows[0][1].endswith("-> held, no reason given"), rows[0][1])


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
        self.assertEqual(row["hold"], {"by": "agent", "reason": "wait for infra"})

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


T_FEEDBACK = "2026-01-01T00:00:00Z"


def hold(**extra):
    """A hold the user set at LAST_SEEN, against HEAD, with nothing approved."""
    h = {"by": "you", "reason": "waiting", "since": LAST_SEEN, "head": HEAD,
         "requested": False, "approved": False, "draft": False}
    h.update(extra)
    return h


def pr_with(**extra):
    d = detail()
    d.update(extra)
    return d


def run_scan(state, details):
    with fake.fake_world(pr_detail=lambda slug, number: details[number]) as world:
        rows, active, _, _ = scan.scan(state)
    return rows, active, world


def state_with(item, key=KEY):
    state = copy.deepcopy(scan.EMPTY)
    state["me"] = ME
    state["items"] = {key: item}
    return state


class HoldModelTest(unittest.TestCase):
    def test_load_migrates_a_parked_item_to_a_hold_and_is_idempotent(self):
        parked = {"at": LAST_SEEN, "head": HEAD, "requested": True}
        with fake.fake_world() as _:
            with open(scan.STATE_FILE, "w") as fh:
                json.dump({"items": {KEY: {"mode": "fix", "parked": parked}}}, fh)
            first = scan.load()
            item = first["items"][KEY]
            self.assertNotIn("parked", item)
            self.assertEqual(item["hold"], {"by": "you", "reason": "",
                                            "since": LAST_SEEN, "head": HEAD,
                                            "requested": True})
            scan.save(first)
            self.assertEqual(scan.load()["items"], first["items"])

    def test_hold_and_unhold_take_their_park_aliases_and_no_pr_items_too(self):
        state = state_with({"mode": "fix", "branch": "feat/x", "session": "w1"})
        with fake.fake_world(pr_detail=lambda slug, number: detail()), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(scan.dispatch("hold", ["hold", KEY, "waiting", "on",
                                                    "db"], state), 0)
            self.assertEqual(scan.hold_of(state["items"][KEY])["reason"],
                             "waiting on db")
            self.assertEqual(scan.dispatch("unpark", ["unpark", KEY], state), 0)
            self.assertIsNone(scan.hold_of(state["items"][KEY]))
            self.assertEqual(scan.dispatch("park", ["park", KEY], state), 0)
            self.assertEqual(scan.hold_of(state["items"][KEY])["by"], "you")
            self.assertEqual(scan.dispatch("unhold", ["unhold", KEY], state), 0)
            self.assertIsNone(scan.hold_of(state["items"][KEY]))

    def test_a_held_pr_item_is_on_hold_and_holds_no_slot(self):
        rows, active, _ = run_scan(state_with(fix_item(hold=hold())),
                                   {5: pr_with()})
        self.assertEqual([kind for kind, k, _ in rows if k == KEY], ["HOLD"])
        text = [extra for kind, k, extra in rows if k == KEY][0]
        self.assertIn("hold=you -> waiting", text)
        self.assertEqual(active, 0)

    def test_a_held_no_pr_item_is_on_hold_and_holds_no_slot(self):
        item = {"mode": "fix", "branch": "feat/x", "session": "w1",
                "hold": hold(reason="")}
        rows, active, _ = run_scan(state_with(item), {})
        self.assertEqual([kind for kind, k, _ in rows if k == KEY], ["HOLD"])
        self.assertIn("-> held as you asked", [e for _, k, e in rows if k == KEY][0])
        self.assertEqual(active, 0)

    def test_approval_wakes_a_hold_with_a_note(self):
        state = state_with(fix_item(hold=hold()))
        run_scan(state, {5: pr_with(reviewDecision="APPROVED")})
        item = state["items"][KEY]
        self.assertNotIn("hold", item)
        self.assertEqual(item["woke"]["reason"], "approved")

    def test_an_approval_already_there_at_the_hold_does_not_wake_it(self):
        state = state_with(fix_item(hold=hold(approved=True)))
        rows, _, _ = run_scan(state, {5: pr_with(reviewDecision="APPROVED")})
        self.assertEqual([kind for kind, k, _ in rows if k == KEY], ["HOLD"])
        self.assertIn("hold", state["items"][KEY])
        self.assertNotIn("woke", state["items"][KEY])

    def test_a_draft_turned_ready_wakes_a_hold(self):
        state = state_with(fix_item(hold=hold(draft=True)))
        run_scan(state, {5: pr_with(isDraft=False)})
        self.assertEqual(state["items"][KEY]["woke"]["reason"], "draft→ready")

    def test_new_feedback_wakes_a_hold(self):
        review = {"author": {"login": OTHER}, "state": "CHANGES_REQUESTED",
                  "submittedAt": T0, "body": "fix it"}
        state = state_with(fix_item(hold=hold(), feedback_seen=T_FEEDBACK))
        run_scan(state, {5: pr_with(reviews=[review])})
        self.assertEqual(state["items"][KEY]["woke"]["reason"], "new feedback")

    def test_a_re_request_of_review_wakes_a_hold(self):
        state = state_with(fix_item(hold=hold(requested=False)))
        run_scan(state, {5: pr_with(reviewRequests=[{"login": ME}])})
        self.assertEqual(state["items"][KEY]["woke"]["reason"], "re-requested")

    def test_a_newer_needs_report_wakes_a_hold(self):
        report_ = {"state": "blocked", "sha": HEAD, "msg": "stuck",
                   "at": LAST_SEEN + 100}
        state = state_with(fix_item(hold=hold(), driver="agent",
                                    last_report=report_))
        rows, _, _ = run_scan(state, {5: pr_with()})
        self.assertEqual(state["items"][KEY]["woke"]["reason"], "report: blocked")
        self.assertEqual([kind for kind, k, _ in rows if k == KEY], ["BLOCKED"])

    def test_red_ci_does_not_wake_a_hold(self):
        red = [{"conclusion": "FAILURE", "name": "test"}]
        state = state_with(fix_item(hold=hold()))
        rows, _, _ = run_scan(state, {5: pr_with(statusCheckRollup=red)})
        self.assertIn("hold", state["items"][KEY])
        self.assertEqual([kind for kind, k, _ in rows if k == KEY], ["HOLD"])

    def test_the_back_from_hold_note_stays_until_the_card_changes_kind(self):
        it = {"woke": {"reason": "approved", "kind": None}}
        self.assertEqual(scan.woke_note(it, "READY", "mode=fix -> you merge"),
                         "mode=fix -> you merge; back from hold: approved")
        self.assertIn("back from hold", scan.woke_note(it, "READY", "x"))
        self.assertEqual(scan.woke_note(it, "BLOCKED", "x"), "x")
        self.assertNotIn("woke", it)


class HoldEventsTest(unittest.TestCase):
    def events(self):
        with open(scan.EVENTS_FILE) as fh:
            return [json.loads(line) for line in fh]

    def test_hold_and_unhold_log_their_reason_only_when_given(self):
        state = state_with({"mode": "fix", "branch": "feat/x", "session": "w1"})
        with fake.fake_world(), contextlib.redirect_stdout(io.StringIO()):
            scan.dispatch("hold", ["hold", KEY, "waiting", "on", "db"], state)
            scan.dispatch("unhold", ["unhold", KEY], state)
            scan.dispatch("hold", ["hold", KEY], state)
            events = self.events()
        self.assertEqual([(e["kind"], e.get("reason")) for e in events],
                         [("hold", "waiting on db"), ("unhold", None), ("hold", None)])

    def test_unhold_of_an_item_not_on_hold_is_refused_and_logs_nothing(self):
        state = state_with({"mode": "fix", "branch": "feat/x", "session": "w1"})
        with fake.fake_world():
            r = scan.unhold_item(state, KEY)
            self.assertFalse(os.path.exists(scan.EVENTS_FILE))
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], f"{KEY} is not on hold")

    def test_a_wake_reports_its_cause_for_tick_to_log_as_unhold(self):
        state = state_with(fix_item(hold=hold()))
        info = {}
        with fake.fake_world(pr_detail=lambda slug, number: pr_with(
                reviewDecision="APPROVED")):
            scan.scan(state, info=info)
        self.assertEqual(info["revived"], [(KEY, "approved")])
        self.assertNotIn("hold", state["items"][KEY])


class ReadOnlyAckTest(unittest.TestCase):
    def read_only(self, **extra):
        item = {"mode": "fix", "branch": "feat/x", "session": "w1", "driver": "agent",
                "read_only": True, "last_seen": LAST_SEEN,
                "last_report": {"state": "ready", "sha": "", "msg": "found it",
                                "at": LAST_SEEN + 100}}
        item.update(extra)
        return item

    def test_a_read_only_ready_report_is_to_read_and_holds_no_slot(self):
        rows, active, _ = run_scan(state_with(self.read_only()), {})
        kinds = [(kind, extra) for kind, k, extra in rows if k == KEY]
        self.assertEqual([kind for kind, _ in kinds], ["READY"])
        self.assertIn("report to read -> found it", kinds[0][1])
        self.assertEqual(active, 0)

    def test_ack_marks_the_read_only_report_read_and_it_goes_to_done(self):
        state = state_with(self.read_only())
        with fake.fake_world():
            r = scan.ack_item(state, KEY)
        self.assertTrue(r["ok"])
        self.assertEqual(state["items"][KEY]["report_acked"], LAST_SEEN + 100)
        rows, active, _ = run_scan(state, {})
        self.assertEqual([kind for kind, k, _ in rows if k == KEY], ["DONE"])
        self.assertEqual(active, 0)

    def test_ack_on_a_pushed_pr_still_acknowledges_the_push(self):
        state = state_with(fix_item())
        with fake.fake_world(pr_detail=lambda slug, number: detail()):
            r = scan.ack_item(state, KEY)
        self.assertTrue(r["ok"])
        self.assertEqual(state["items"][KEY]["push_acked"], HEAD)


class FeedbackNoteTest(unittest.TestCase):
    def test_a_multi_line_review_body_with_since_stays_one_row(self):
        body = 'nit\nsince=x -> "quoted"'
        review = {"author": {"login": OTHER}, "state": "CHANGES_REQUESTED",
                  "submittedAt": T0, "body": body}
        state = state_with(fix_item(feedback_seen=T_FEEDBACK))
        rows, _, _ = run_scan(state, {5: pr_with(reviews=[review])})
        (kind, text), = [(kind, extra) for kind, k, extra in rows if k == KEY]
        self.assertEqual(kind, "FEEDBACK")
        self.assertNotIn("\n", text)
        fields = scan.parse_extra(text)
        self.assertEqual(fields["fields"]["since"], T0)
        self.assertEqual(fields["title"], "nit since=x -> quoted")


class HoldSnapshotFieldsTest(unittest.TestCase):
    def test_a_held_item_carries_its_hold_and_no_parked_alias(self):
        state = state_with(fix_item(hold=hold()))
        with fake.fake_world(pr_detail=lambda slug, number: pr_with()):
            snap = scan.snapshot(state)
        (row,) = [r for r in snap["rows"] if r["key"] == KEY]
        self.assertEqual(row["item"]["hold"]["by"], "you")
        self.assertNotIn("parked", row["item"])
        self.assertEqual(row["hold"], {"by": "you", "reason": "waiting"})


if __name__ == "__main__":
    unittest.main()
