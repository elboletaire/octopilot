"""`tick` with the engine: off changes nothing, plan logs and changes nothing,
on acts, and a worktree never acts. The scan is replaced by fixed rows, and
GitHub, git and the agents are faked (fake_world)."""
import contextlib
import io
import os
import unittest
from unittest import mock

from tests.fake import fake_world
import scan

REPO = "owner/repo"
MERGED, A = f"{REPO}#7", f"{REPO}#8"
ROWS = [("MERGED", MERGED, "branch=feat/a mode=fix")]


class FakeBackend:
    name = "herdr"
    prefix = "herdr:"
    remove_label = "removed"
    noun = "workspace"

    def target(self, it):
        return it["session"]

    def discard(self, session, worktree):
        return 0, ""


def item(pr, branch, session):
    return {"mode": "fix", "pr": pr, "branch": branch, "session": session,
            "phase": "fixing", "reviewed_sha": "old1234", "review_rounds": 0}


class TickEngineTest(unittest.TestCase):
    def setUp(self):
        world = fake_world(backend_for=lambda session: FakeBackend(),
                           send_to=lambda address, text: (True, None))
        self.world = world.__enter__()
        self.addCleanup(world.__exit__, None, None, None)
        scanned = mock.patch.object(scan, "scan", side_effect=self.scan)
        scanned.start()
        self.addCleanup(scanned.stop)
        # Restores the environment the tests change.
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("OCTOPILOT_ENGINE", None)
        scan.save({"items": {MERGED: item(7, "feat/a", "w1"),
                             A: item(8, "feat/b", "w2")},
                   "handled_merges": [], "pending_sends": [],
                   "registry": {REPO: {"path": "/repo", "base": "main"}}})

    def scan(self, state, info=None):
        info.update(revived=[], details={}, meta={})
        return ROWS, 2, 1, 0

    def tick(self, args=(), engine=None):
        if engine is None:
            os.environ.pop("OCTOPILOT_ENGINE", None)
        else:
            os.environ["OCTOPILOT_ENGINE"] = engine
        with contextlib.redirect_stdout(io.StringIO()):
            return scan.tick(list(args))

    def events(self):
        if not os.path.exists(scan.EVENTS_FILE):
            return ""
        with open(scan.EVENTS_FILE) as fh:
            return fh.read()

    def items(self):
        return scan.load()["items"]

    def sent(self):
        return self.world.named("send_to")

    def test_engine_off_changes_nothing(self):
        before = self.items()
        self.tick()
        self.assertEqual(self.items(), before)
        self.assertEqual(self.sent(), [])
        self.assertNotIn("engine-", self.events())

    def test_plan_changes_nothing_and_logs_what_it_would_do(self):
        before = self.items()
        self.tick(engine="plan")
        self.assertEqual(self.items(), before)
        self.assertEqual(scan.load()["handled_merges"], [])
        self.assertEqual(self.sent(), [])
        self.assertIn('"kind": "engine-plan"', self.events())

    def test_tick_plan_flag_forces_plan(self):
        before = self.items()
        self.tick(["--plan"], engine="on")
        self.assertEqual(self.items(), before)
        self.assertEqual(self.sent(), [])
        self.assertIn('"kind": "engine-plan"', self.events())

    def test_on_acts_on_the_rows(self):
        self.tick(engine="on")
        self.assertNotIn(MERGED, self.items())
        self.assertIn(MERGED, scan.load()["handled_merges"])
        self.assertEqual([args[0] for args, _ in self.sent()], ["herdr:w2"])

    def test_worktree_never_acts(self):
        before = self.items()
        with mock.patch.object(scan, "installed_dir", return_value="/live/octopilot"), \
                mock.patch.object(scan, "OCTOPILOT_DIR", "/wt/octopilot"), \
                mock.patch.dict(os.environ, {"OCTOPILOT_ENGINE": "on"}):
            os.environ.pop("OCTOPILOT_STATE_DIR", None)
            self.assertEqual(scan.engine_mode(), "off")
            self.tick(engine="on")
        self.assertEqual(self.items(), before)
        self.assertEqual(self.sent(), [])

    def test_engine_mode_reads_the_env_and_guard(self):
        with mock.patch.object(scan, "OCTOPILOT_DIR", "/wt/octopilot"):
            self.assertEqual(scan.engine_mode(), "off")
            os.environ["OCTOPILOT_ENGINE"] = "on"
            self.assertEqual(scan.engine_mode(), "on")
            self.assertEqual(scan.engine_mode(["--plan"]), "plan")
            os.environ["OCTOPILOT_ENGINE"] = "plan"
            self.assertEqual(scan.engine_mode(), "plan")
            os.environ["OCTOPILOT_ENGINE"] = "ON"
            self.assertEqual(scan.engine_mode(), "off")


if __name__ == "__main__":
    unittest.main()
