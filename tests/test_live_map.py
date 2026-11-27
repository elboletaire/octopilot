"""live_map indexes an agent under each of its ids, and session_matches agrees."""
import unittest

from tests import SCRATCH  # noqa: F401  (imports the scratch state before scan)
import scan

STATES = {"working": "running", "idle": "idle"}


class LiveMapTest(unittest.TestCase):
    def test_an_agent_with_ids_is_indexed_under_each(self):
        agents = [{"session": "sp1f", "ids": ["sp1f", "tb2"], "agent": "ap-x-1",
                   "pane": "t9", "status": "working"}]
        live = scan.live_map(agents, STATES, "hc")
        self.assertEqual(set(live), {"sp1f", "tb2"})
        self.assertEqual(live["tb2"]["agent"], "ap-x-1")
        self.assertEqual(live["tb2"]["state"], "running")
        self.assertEqual(live["tb2"]["backend"], "hc")

    def test_without_ids_the_session_is_the_key(self):
        agents = [{"session": "w12", "agent": "ap-x-1", "pane": "p1",
                   "status": "idle"}]
        self.assertEqual(set(scan.live_map(agents, STATES, "herdr")), {"w12"})

    def test_an_empty_ids_list_falls_back_to_the_session(self):
        agents = [{"session": "w12", "ids": [], "agent": "ap-x-1"}]
        self.assertEqual(set(scan.live_map(agents, STATES, "herdr")), {"w12"})

    def test_an_ap_agent_wins_over_another_on_a_shared_id(self):
        agents = [{"session": "w12", "ids": ["w12", "w12:t1"], "agent": "claude"},
                  {"session": "w12", "ids": ["w12", "w12:t2"], "agent": "ap-x-1"}]
        live = scan.live_map(agents, STATES, "herdr")
        self.assertEqual(live["w12"]["agent"], "ap-x-1")
        self.assertEqual(live["w12:t1"]["agent"], "claude")
        self.assertEqual(live["w12:t2"]["agent"], "ap-x-1")

    def test_a_non_ap_agent_does_not_replace_an_ap_one(self):
        agents = [{"session": "w12", "ids": ["w12", "w12:t1"], "agent": "ap-x-1"},
                  {"session": "w12", "ids": ["w12", "w12:t1"], "agent": "claude"}]
        live = scan.live_map(agents, STATES, "herdr")
        self.assertEqual(live["w12:t1"]["agent"], "ap-x-1")

    def test_an_agent_without_any_id_is_skipped(self):
        agents = [{"session": None, "ids": [None, ""], "agent": "x"}]
        self.assertEqual(scan.live_map(agents, STATES, "hc"), {})


class SessionMatchesTest(unittest.TestCase):
    def test_matches_by_any_id(self):
        agent = {"session": "sp1f", "ids": ["sp1f", "tb2"]}
        self.assertTrue(scan.session_matches(agent, "sp1f"))
        self.assertTrue(scan.session_matches(agent, "tb2"))
        self.assertFalse(scan.session_matches(agent, "tb3"))

    def test_without_ids_compares_the_session(self):
        agent = {"session": "w12:t3"}
        self.assertTrue(scan.session_matches(agent, "w12:t3"))
        self.assertFalse(scan.session_matches(agent, "w12"))

    def test_an_empty_ids_list_compares_the_session(self):
        agent = {"session": "w12", "ids": []}
        self.assertTrue(scan.session_matches(agent, "w12"))
        self.assertFalse(scan.session_matches(agent, "w12:t1"))
