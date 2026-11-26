"""The orchestrator's `inbox` lists the queued requests and never clears them:
the engine's drain takes them. A report is logged, not queued."""
import contextlib
import io
import json
import unittest

from tests import fake
import scan

MESSAGES = [{"at": 1, "text": "REQUEST pause"},
            {"at": 2, "text": "REQUEST go a#1\n(confirmed over cap)"}]
KEY = "octo/app#1"


def run(func, *args):
    out = io.StringIO()
    with fake.fake_world(), contextlib.redirect_stdout(out):
        func(*args)
    return out.getvalue()


class InboxTest(unittest.TestCase):
    def test_lists_every_message_and_keeps_them_queued(self):
        state = {"inbox": [dict(m) for m in MESSAGES]}
        lines = run(scan.inbox, state).splitlines()
        self.assertEqual(lines[0], "pending requests (the engine drains them; act on none)")
        self.assertIn("REQUEST pause", lines[1])
        self.assertIn("REQUEST go a#1", lines[2])
        self.assertEqual(lines[3], "    (confirmed over cap)")
        self.assertEqual(state["inbox"], MESSAGES)

    def test_an_empty_inbox_says_so_under_the_header(self):
        out = run(scan.inbox, {}).splitlines()
        self.assertEqual(out, ["pending requests (the engine drains them; act on none)",
                               "(inbox empty)"])

    def test_the_header_counts_every_queued_message(self):
        out = run(scan.render, [], 0, 0, {"inbox": [dict(m) for m in MESSAGES]})
        self.assertIn("INBOX 2", out)


class ReportTest(unittest.TestCase):
    def test_a_report_is_logged_and_not_queued(self):
        state = {"items": {KEY: {"mode": "fix", "pr": 1, "session": "w1"}}, "inbox": []}
        with fake.fake_world():
            scan.save(state)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(scan.report(state, ["report", KEY, "ready", "abc1234",
                                                     "all", "good"]), 0)
            self.assertEqual(out.getvalue(), f"reported {KEY} ready\n")
            self.assertEqual(state["inbox"], [])
            self.assertEqual(scan.load()["inbox"], [])
            self.assertEqual(state["items"][KEY]["last_report"]["state"], "ready")
            self.assertIsInstance(state["items"][KEY]["last_report"]["at"], int)
            with open(scan.EVENTS_FILE) as fh:
                kinds = [json.loads(line)["kind"] for line in fh]
            self.assertEqual(kinds, ["report"])


if __name__ == "__main__":
    unittest.main()
