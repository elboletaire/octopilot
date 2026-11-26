"""HcBackend with worker tabs: agents carry their space and tab ids, target finds
an agent by either, and the sessions list shows the tabs that run in a worktree."""
import unittest
from unittest import mock

from tests import SCRATCH  # noqa: F401  (imports the scratch state before scan)
import scan


def stub_session_matches(agent, session):
    """Stands in for the shared helper: an agent matches a session by any id."""
    return session in agent.get("ids", [])


class HcAgentsTest(unittest.TestCase):
    def setUp(self):
        self.terminals = []
        self.labels = {}
        mock.patch.object(scan.shutil, "which", return_value="/bin/herdr-app").start()
        mock.patch.object(scan, "hc", side_effect=self.fake_hc).start()
        mock.patch.object(scan, "session_matches", stub_session_matches,
                          create=True).start()
        self.addCleanup(mock.patch.stopall)

    def fake_hc(self, args, timeout=60):
        if args[:2] == ["ls", "--json"]:
            return 0, {"terminals": self.terminals}, ""
        if args[0] == "get":
            return 0, {"name": None, "labels": self.labels.get(args[1], {})}, ""
        raise AssertionError(f"unexpected hc call {args}")

    def test_agents_carry_the_space_and_tab_ids(self):
        self.terminals = [{"address": "t1", "agent": "claude-code", "status": "idle"},
                          {"address": "t2", "agent": "claude-code", "status": "done"},
                          {"address": "t3", "agent": None}]
        self.labels = {"t1": {"herdr.space": "sp1", "herdr.tab": "tb1",
                              "octopilot.agent": "ap-x-1"},
                       "t2": {"herdr.space": "sp2"}}
        agents = {a["pane"]: a for a in scan.HC.agents()}
        self.assertEqual(set(agents), {"t1", "t2"})
        self.assertEqual(agents["t1"]["ids"], ["sp1", "tb1"])
        self.assertEqual(agents["t1"]["session"], "sp1")
        self.assertEqual(agents["t1"]["agent"], "ap-x-1")
        self.assertEqual(agents["t2"]["ids"], ["sp2"])

    def test_target_finds_a_worker_by_its_tab(self):
        self.terminals = [{"address": "t1", "agent": "claude-code", "status": "idle"}]
        self.labels = {"t1": {"herdr.space": "sp-oct", "herdr.tab": "tb9",
                              "octopilot.agent": "ap-x-1"}}
        item = {"session": "tb9", "agent": "ap-x-1"}
        self.assertEqual(scan.HC.target(item), "t1")

    def test_target_of_a_space_session_still_matches(self):
        self.terminals = [{"address": "t1", "agent": "claude-code", "status": "idle"}]
        self.labels = {"t1": {"herdr.space": "sp1", "herdr.tab": "tb1",
                              "octopilot.agent": "ap-x-1"}}
        self.assertEqual(scan.HC.target({"session": "sp1", "agent": "other"}), "t1")

    def test_target_is_none_for_another_tab(self):
        self.terminals = [{"address": "t1", "agent": "claude-code", "status": "idle"}]
        self.labels = {"t1": {"herdr.space": "sp1", "herdr.tab": "tb1"}}
        self.assertIsNone(scan.HC.target({"session": "tb2"}))


if __name__ == "__main__":
    unittest.main()
