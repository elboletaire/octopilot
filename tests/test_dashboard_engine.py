"""The dashboard's GitHub pass rings the orchestrator only when the engine
does not act: with the engine on there is nobody to wake."""
import os
import unittest
from unittest import mock

from tests import fake  # noqa: F401  (sets the scratch environment)

try:
    import dashboard
except ImportError:  # the rich package is the dashboard's own dependency
    dashboard = None


@unittest.skipIf(dashboard is None, "dashboard needs rich")
class GithubPassTest(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        self.model = dashboard.Model.__new__(dashboard.Model)
        self.model.refresh_snapshot = mock.Mock()
        self.model.wake_orchestrator = mock.Mock()

    def test_engine_on_does_not_wake_the_orchestrator(self):
        os.environ["OCTOPILOT_ENGINE"] = "on"
        self.model.github_pass(False)
        self.model.refresh_snapshot.assert_called_once()
        self.model.wake_orchestrator.assert_not_called()

    def test_engine_off_still_wakes_the_orchestrator(self):
        os.environ.pop("OCTOPILOT_ENGINE", None)
        self.model.github_pass(False)
        self.model.wake_orchestrator.assert_called_once()

    def test_engine_plan_still_wakes_the_orchestrator(self):
        os.environ["OCTOPILOT_ENGINE"] = "plan"
        self.model.github_pass(False)
        self.model.wake_orchestrator.assert_called_once()


@unittest.skipIf(dashboard is None, "dashboard needs rich")
class EngineErrorViewTest(unittest.TestCase):
    def test_kitchen_says_the_reason_in_a_bad_tone(self):
        ev = {"kind": "engine-error", "key": "owner/repo#8",
              "msg": "boot failed: worktree has uncommitted changes"}
        r = dashboard.kitchen_reaction(ev)
        self.assertEqual(r["text"], ev["msg"])
        self.assertEqual(r["tone"], "bad")

    def test_activity_shows_the_reason(self):
        ev = {"kind": "engine-error", "key": "owner/repo#8",
              "msg": "boot failed: worktree has uncommitted changes"}
        self.assertEqual(dashboard.event_detail(ev), ev["msg"])


if __name__ == "__main__":
    unittest.main()
