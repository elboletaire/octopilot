"""The TUI backend's worker tabs: listing, target, focus, close, discard and spawn,
each beside the legacy workspace path. herdr's CLI is faked, never called."""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from tests import SCRATCH  # noqa: F401  (imports the scratch state before scan)
import scan

OK = (0, {"result": {}}, "")


def created(tab, pane):
    return (0, {"result": {"type": "tab_created", "tab": {"tab_id": tab},
                           "root_pane": {"pane_id": pane}}}, "")


def refused(code):
    return (1, {"error": {"code": code}}, code)


def same_session(agent, session):
    """A stand-in for scan.session_matches (the tabs-shared branch adds it)."""
    return session in (agent.get("ids") or [])


class FakeHerdr:
    """herdr's CLI: the first answer whose argv starts with the key wins."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, args, timeout=60):
        self.calls.append(list(args))
        for prefix, reply in self.answers.items():
            if tuple(args[:len(prefix)]) == prefix:
                return reply(args) if callable(reply) else reply
        return 1, None, f"no answer for {args}"

    def argvs(self, *prefix):
        return [c for c in self.calls if tuple(c[:len(prefix)]) == prefix]


class BackendCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="tabs-", dir=SCRATCH)
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.wt = os.path.join(self.folder, "wt")
        os.makedirs(self.wt)
        self.herdr_answers = {}
        self.patch(scan, "octopilot_workspace", return_value="w96", create=True)
        self.patch(scan, "session_matches", side_effect=same_session, create=True)
        self.patch(scan, "agent_name", return_value="ap-x-1")
        self.patch(scan.shutil, "which", return_value="/usr/bin/herdr")
        self.patch(scan, "time")
        self.patch(scan, "accept_trust", return_value=True)
        self.patch(scan, "sh", return_value=(0, "", ""))
        self.env = self.patch_env()

    def patch(self, obj, name, *args, **kwargs):
        patcher = mock.patch.object(obj, name, *args, **kwargs)
        patched = patcher.start()
        self.addCleanup(patcher.stop)
        return patched

    def patch_env(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for var in ("WORKER_HOST", "OCTOPILOT_DIR", "OCTOPILOT_STATE_DIR"):
            os.environ.pop(var, None)
        return os.environ

    def herdr(self, answers):
        fake = FakeHerdr(answers)
        self.patch(scan, "herdr", side_effect=fake)
        return fake


class AgentsIdsTest(BackendCase):
    def test_agents_carry_their_workspace_and_tab_ids(self):
        self.herdr({("agent", "list"): (0, {"result": {"agents": [
            {"workspace_id": "w96", "tab_id": "w96:t2", "name": "ap-x-1",
             "pane_id": "w96:p2", "agent_status": "idle"}]}}, "")})
        [agent] = scan.HERDR.agents()
        self.assertEqual(agent["ids"], ["w96", "w96:t2"])
        self.assertEqual(agent["session"], "w96")
        self.assertEqual(agent["agent"], "ap-x-1")


class TargetTest(BackendCase):
    def test_target_matches_the_tab_of_the_item(self):
        fake = self.herdr({
            ("agent", "list"): (0, {"result": {"agents": [
                {"workspace_id": "w96", "tab_id": "w96:t1", "name": "octopilot-code",
                 "pane_id": "w96:p1"},
                {"workspace_id": "w96", "tab_id": "w96:t2", "name": "ap-x-1",
                 "pane_id": "w96:p2"}]}}, ""),
            ("agent", "get"): refused("not_found"),
        })
        self.assertEqual(scan.HERDR.target({"session": "w96:t2", "agent": None}),
                         "ap-x-1")
        self.assertEqual(fake.argvs("agent", "get"), [])

    def test_target_falls_back_to_the_pane_of_an_unnamed_agent(self):
        self.herdr({("agent", "list"): (0, {"result": {"agents": [
            {"workspace_id": "w96", "tab_id": "w96:t2", "name": None,
             "pane_id": "w96:p2"}]}}, "")})
        self.assertEqual(scan.HERDR.target({"session": "w96:t2"}), "w96:p2")

    def test_target_is_none_without_an_agent_in_the_tab(self):
        self.herdr({("agent", "list"): (0, {"result": {"agents": []}}, "")})
        self.assertIsNone(scan.HERDR.target({"session": "w96:t2"}))


if __name__ == "__main__":
    unittest.main()
