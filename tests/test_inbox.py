"""The orchestrator's `inbox`: with the engine on it takes the reports and leaves
the requests to the engine's drain; with the engine off it takes everything."""
import contextlib
import io
import os
import unittest
from unittest import mock

from tests import fake
import scan

MESSAGES = [{"at": 1, "text": "REQUEST pause"},
            {"at": 2, "text": "OCTOPILOT a#1 ready abc all good"}]


class InboxTest(unittest.TestCase):
    def run_inbox(self, engine):
        out = io.StringIO()
        with fake.fake_world(), mock.patch.dict(os.environ, {"OCTOPILOT_ENGINE": engine}):
            state = {"inbox": [dict(m) for m in MESSAGES]}
            with contextlib.redirect_stdout(out):
                scan.inbox(state)
            return out.getvalue(), state["inbox"]

    def test_with_the_engine_on_only_reports_are_taken(self):
        out, left = self.run_inbox("on")
        self.assertIn("OCTOPILOT a#1 ready abc all good", out)
        self.assertNotIn("REQUEST", out)
        self.assertEqual(left, [MESSAGES[0]])

    def test_with_the_engine_off_everything_is_taken(self):
        out, left = self.run_inbox("off")
        self.assertIn("REQUEST pause", out)
        self.assertEqual(left, [])

    def test_with_only_requests_queued_under_the_engine_it_says_empty(self):
        with fake.fake_world(), mock.patch.dict(os.environ, {"OCTOPILOT_ENGINE": "on"}):
            state = {"inbox": [{"at": 1, "text": "REQUEST pause"}]}
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                scan.inbox(state)
        self.assertEqual(out.getvalue().strip(), "(inbox empty)")
        self.assertEqual(state["inbox"], [{"at": 1, "text": "REQUEST pause"}])


    def test_the_header_counts_only_what_inbox_would_show(self):
        box = [{"at": 1, "text": "REQUEST pause"},
               {"at": 2, "text": "OCTOPILOT a#1 ready abc all good"}]
        for engine, want in (("on", "INBOX 1"), ("off", "INBOX 2")):
            out = io.StringIO()
            with fake.fake_world(), mock.patch.dict(os.environ,
                                                    {"OCTOPILOT_ENGINE": engine}), \
                    contextlib.redirect_stdout(out):
                scan.render([], 0, 0, {"inbox": [dict(m) for m in box]})
            self.assertIn(want, out.getvalue())


if __name__ == "__main__":
    unittest.main()
