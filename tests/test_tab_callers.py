"""The scan.py callers of worker tabs: detach, cleanup, track and the re-home of
an item whose tab was moved, with the herdr and herdr-app calls faked."""
import contextlib
import io
import json
import unittest
from unittest import mock

from tests import fake
import scan
from tests.test_rerequest import KEY, new_state, pr_detail
from tests.test_returns import FakeHost
from tests.test_tick import events, run_tick

REAL_PARKED_AGENT = scan.parked_agent  # fake_world stubs it; some tests want the real one
TAB = "tb1"        # a worker's tab that was closed
MOVED = "tb2"      # the same agent, now in another tab of the Octopilot space
SPACE = "sp1"


def tab_row(tab, space=SPACE):
    return {"id": tab, "title": "fix", "path": "/wt/app-5", "backend": "hc",
            "space": space, "worktree": {"branch": "feat/x", "main_repo_path": "/p"}}


def agent_in(sess, name="ap-app-5"):
    return {"session": sess, "agent": name, "pane": "t1", "backend": "hc",
            "state": "idle"}


def tab_item(session=TAB, **extra):
    state = new_state()
    state["items"][KEY]["session"] = session
    state["items"][KEY]["agent"] = "ap-app-5"
    state["items"][KEY].update(extra)
    return state


class DetachTabTest(unittest.TestCase):
    def test_detach_says_tab_closed_for_a_tab(self):
        state = tab_item()
        host = FakeHost(close=(0, ""))
        with fake.fake_world(backend_for=host):
            r = scan.detach_item(state, KEY)
        self.assertEqual(r["msg"], f"detached {KEY} session={TAB} branch=feat/x "
                                   "-> tab closed, worktree kept, untracked")
        self.assertEqual(host.closed, [TAB])

    def test_detach_names_the_tab_host_when_its_close_fails(self):
        state = tab_item()
        with fake.fake_world(backend_for=FakeHost(close=(1, "busy"))):
            r = scan.detach_item(state, KEY)
        self.assertEqual(r, {"ok": False, "error": "herdr-app tab close failed: busy",
                             "msg": None})


class CleanupTabTest(unittest.TestCase):
    def test_the_discard_is_logged_under_the_tab_label(self):
        state = tab_item()
        with fake.fake_world(backend_for=FakeHost(discard=(0, ""))):
            r = scan.cleanup_item(state, KEY)
        self.assertEqual(r["msg"], f"herdr-app tab remove {TAB}: ok\ncleaned up octo/app#5")


class TrackTabTest(unittest.TestCase):
    def track(self, sessions, open_ids):
        state = new_state()
        err = io.StringIO()
        out = io.StringIO()
        with fake.fake_world(all_sessions=(sessions, {"hc"}, open_ids),
                             backend_for=scan.HC), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            code = scan.dispatch("track", ["track", KEY, "comment", "feat/x", MOVED],
                                 state)
        return code, state, err.getvalue(), out.getvalue()

    def test_a_tab_id_is_an_open_session(self):
        code, state, _, out = self.track([tab_row(MOVED)], {SPACE, MOVED})
        self.assertEqual(code, 0)
        self.assertEqual(out, f"tracked {KEY} mode=comment branch=feat/x session={MOVED}\n")
        self.assertEqual(state["items"][KEY]["session"], MOVED)
        self.assertEqual(state["items"][KEY]["space"], SPACE)

    def test_a_closed_tab_is_refused_with_its_host_name(self):
        code, state, err, _ = self.track([], {SPACE})
        self.assertEqual(code, 1)
        self.assertEqual(err, f"{MOVED} is not an open herdr-app tab\n")
        self.assertEqual(state["items"][KEY]["session"], "w1")


class RehomeTest(unittest.TestCase):
    def test_a_missing_tab_with_no_agent_left_is_gone(self):
        state = tab_item()
        with fake.fake_world(all_sessions=([], {"hc"}, {SPACE}), backend_for=scan.HC,
                             pr_detail=pr_detail):
            rows = scan.scan(state)[0]
        self.assertEqual([r[0] for r in rows], ["GONE"])
        self.assertNotIn(KEY, state["items"])

    def test_the_item_follows_its_agent_to_another_tab(self):
        state = tab_item()
        info = {}
        live = {MOVED: agent_in(MOVED), SPACE: agent_in(SPACE)}
        with fake.fake_world(all_sessions=([tab_row(MOVED)], {"hc"}, {SPACE, MOVED}),
                             all_live=live, pr_detail=pr_detail, backend_for=scan.HC):
            rows = scan.scan(state, info=info)[0]
        self.assertNotIn("GONE", [r[0] for r in rows])
        it = state["items"][KEY]
        self.assertEqual((it["session"], it["space"]), (MOVED, SPACE))
        self.assertEqual(info["rehomed"], [(KEY, MOVED)])

    def test_a_tab_is_preferred_to_the_space_its_agent_also_lives_in(self):
        state = tab_item()
        info = {}
        live = {SPACE: agent_in(SPACE), MOVED: agent_in(MOVED)}
        rows = [tab_row(MOVED), tab_row(SPACE, space=None)]
        with fake.fake_world(all_sessions=(rows, {"hc"}, {SPACE, MOVED}),
                             all_live=live, pr_detail=pr_detail, backend_for=scan.HC):
            scan.scan(state, info=info)
        self.assertEqual(info["rehomed"], [(KEY, MOVED)])

    def test_a_session_another_item_holds_is_not_taken(self):
        state = tab_item()
        state["items"]["octo/app#6"] = {"mode": "comment", "pr": 6, "branch": "feat/y",
                                        "session": MOVED, "agent": "ap-app-6",
                                        "phase": "working"}
        live = {MOVED: agent_in(MOVED)}
        info = {}
        with fake.fake_world(all_sessions=([tab_row(MOVED)], {"hc"}, {SPACE, MOVED}),
                             all_live=live, pr_detail=pr_detail, backend_for=scan.HC):
            rows = scan.scan(state, info=info)[0]
        self.assertEqual([r[0] for r in rows if r[1] == KEY], ["GONE"])
        self.assertNotIn(KEY, state["items"])
        self.assertEqual(state["items"]["octo/app#6"]["session"], MOVED)
        self.assertEqual(info["rehomed"], [])

    def test_two_items_with_the_same_agent_take_one_home(self):
        state = tab_item()
        state["items"]["octo/app#6"] = {"mode": "comment", "pr": 6, "branch": "feat/y",
                                        "session": "tb3", "agent": "ap-app-5",
                                        "phase": "working"}
        live = {MOVED: agent_in(MOVED)}
        info = {}
        with fake.fake_world(all_sessions=([tab_row(MOVED)], {"hc"}, {SPACE, MOVED}),
                             all_live=live, pr_detail=pr_detail, backend_for=scan.HC):
            rows = scan.scan(state, info=info)[0]
        self.assertEqual(info["rehomed"], [(KEY, MOVED)])
        self.assertEqual([r[0] for r in rows if r[0] == "GONE"], ["GONE"])
        self.assertNotIn("octo/app#6", state["items"])

    def test_another_agent_does_not_rehome_the_item(self):
        state = tab_item()
        live = {MOVED: agent_in(MOVED, name="ap-app-9")}
        with fake.fake_world(all_sessions=([tab_row(MOVED)], {"hc"}, {SPACE, MOVED}),
                             all_live=live, pr_detail=pr_detail, backend_for=scan.HC):
            rows = scan.scan(state)[0]
        self.assertEqual([r[0] for r in rows], ["GONE"])

    def test_a_parked_agent_keeps_the_item_tracked(self):
        state = tab_item()
        parked = {"session": None, "ids": [], "agent": "ap-app-5", "pane": "t9"}
        with fake.fake_world(all_sessions=([], {"hc"}, {SPACE}), backend_for=scan.HC,
                             parked_agent=parked, pr_detail=pr_detail):
            rows = scan.scan(state)[0]
        self.assertEqual([r[0] for r in rows], ["WORKING"])
        self.assertIn("terminal.keep --terminal t9", rows[0][2])
        self.assertIn("feedback waits", rows[0][2])
        self.assertEqual(state["items"][KEY]["session"], TAB)

    def test_the_real_check_keeps_a_parked_agents_item_tracked(self):
        state = tab_item()
        parked = {"session": None, "ids": [], "agent": "ap-app-5", "pane": "t9"}
        with mock.patch.object(scan.HC, "agents", return_value=[parked]), \
                fake.fake_world(all_sessions=([], {"hc"}, {SPACE}), backend_for=scan.HC,
                                parked_agent=REAL_PARKED_AGENT, pr_detail=pr_detail):
            rows = scan.scan(state)[0]
        self.assertEqual([r[0] for r in rows], ["WORKING"])
        self.assertIn(KEY, state["items"])

    def test_herdr_app_is_listed_once_per_scan_for_gone_items(self):
        state = tab_item()
        state["items"]["octo/app#6"] = {"mode": "comment", "pr": 6, "branch": "feat/y",
                                        "session": "tb3", "agent": "ap-app-6",
                                        "phase": "working"}
        with mock.patch.object(scan.HC, "agents", return_value=[]) as agents, \
                fake.fake_world(all_sessions=([], {"hc"}, {SPACE}), backend_for=scan.HC,
                                parked_agent=REAL_PARKED_AGENT, pr_detail=pr_detail):
            scan.scan(state)
        self.assertEqual(agents.call_count, 1)

    def test_a_parked_closed_pr_is_still_closed(self):
        state = tab_item(pr=5)
        parked = {"session": None, "ids": [], "agent": "ap-app-5", "pane": "t9"}
        closed = {"headRefOid": "abc1234", "state": "CLOSED"}
        with fake.fake_world(all_sessions=([], {"hc"}, {SPACE}), backend_for=scan.HC,
                             parked_agent=parked, pr_detail=lambda slug, pr: closed):
            rows = scan.scan(state)[0]
        self.assertEqual([r[0] for r in rows], ["CLOSED"])
        self.assertIn(KEY, state["items"])

    def test_a_parked_item_you_parked_is_done_and_holds_no_slot(self):
        state = tab_item(parked={"at": 1, "requested": False, "head": ""})
        parked = {"session": None, "ids": [], "agent": "ap-app-5", "pane": "t9"}
        with fake.fake_world(all_sessions=([], {"hc"}, {SPACE}), backend_for=scan.HC,
                             parked_agent=parked, pr_detail=pr_detail):
            rows = scan.scan(state)[0]
        self.assertEqual([r[0] for r in rows], ["DONE"])
        self.assertIn(KEY, state["items"])

    def test_a_parked_agent_shown_in_a_tab_is_rehomed(self):
        state = tab_item()
        info = {}
        parked = {"session": None, "ids": [], "agent": "ap-app-5", "pane": "t9"}
        live = {MOVED: agent_in(MOVED)}
        with fake.fake_world(all_sessions=([tab_row(MOVED)], {"hc"}, {SPACE, MOVED}),
                             all_live=live, parked_agent=parked, pr_detail=pr_detail,
                             backend_for=scan.HC):
            rows = scan.scan(state, info=info)[0]
        self.assertNotIn("GONE", [r[0] for r in rows])
        self.assertEqual(info["rehomed"], [(KEY, MOVED)])
        self.assertEqual(state["items"][KEY]["session"], MOVED)


class ParkedAgentTest(unittest.TestCase):
    def parked(self, agents, session=TAB, open_ws=frozenset({SPACE}), backend=scan.HC):
        it = tab_item()["items"][KEY]
        with mock.patch.object(scan, "backend_for", return_value=backend):
            return scan.parked_agent(it, session, set(open_ws), lambda: agents)

    def test_a_terminal_with_no_space_or_tab_is_parked(self):
        agents = [{"session": None, "ids": [], "agent": "ap-app-5", "pane": "t9"}]
        self.assertEqual(self.parked(agents)["pane"], "t9")

    def test_a_terminal_whose_tab_is_closed_is_parked(self):
        agents = [{"session": None, "ids": [MOVED], "agent": "ap-app-5", "pane": "t9"}]
        self.assertEqual(self.parked(agents, open_ws={SPACE})["pane"], "t9")

    def test_a_terminal_whose_space_is_still_open_is_not_parked(self):
        agents = [{"session": SPACE, "ids": [SPACE, MOVED], "agent": "ap-app-5",
                   "pane": "t9"}]
        self.assertIsNone(self.parked(agents, open_ws={SPACE}))

    def test_a_terminal_in_an_open_tab_is_not_parked(self):
        agents = [{"session": SPACE, "ids": [SPACE, MOVED], "agent": "ap-app-5",
                   "pane": "t9"}]
        self.assertIsNone(self.parked(agents, open_ws={SPACE, MOVED}))

    def test_another_agents_parked_terminal_is_not_this_items(self):
        agents = [{"session": None, "ids": [], "agent": "ap-app-9", "pane": "t9"}]
        self.assertIsNone(self.parked(agents))

    def test_a_session_that_is_not_herdr_app_is_never_parked(self):
        agents = [{"session": None, "ids": [], "agent": "ap-app-5", "pane": "t9"}]
        self.assertIsNone(self.parked(agents, session="w1", backend=None))


class ParkedTargetAndCleanupTest(unittest.TestCase):
    PARKED = {"session": None, "ids": [], "agent": "ap-app-5", "pane": "t9"}

    def test_a_parked_terminal_is_the_items_target(self):
        it = tab_item()["items"][KEY]
        with mock.patch.object(scan.HC, "agents", return_value=[self.PARKED]):
            self.assertEqual(scan.HC.target(it), "t9")

    def test_a_parked_cleanup_ends_the_terminal_before_the_discard(self):
        state = tab_item()
        with mock.patch.object(scan, "hc", return_value=(0, {}, "")) as hc_call, \
                mock.patch.object(scan.HC, "discard", return_value=(0, "")) as discard, \
                fake.fake_world(all_sessions=([], {"hc"}, {SPACE}), backend_for=scan.HC,
                                parked_agent=self.PARKED):
            r = scan.cleanup_item(state, KEY)
        hc_call.assert_called_once_with(["kill", "t9"])
        discard.assert_called_once()
        self.assertIn("hc kill t9: ok", r["msg"])
        self.assertNotIn(KEY, state["items"])

    def test_a_parked_cleanup_keeps_the_item_when_the_kill_fails(self):
        state = tab_item()
        with mock.patch.object(scan, "hc", return_value=(1, None, "busy")), \
                mock.patch.object(scan.HC, "discard", return_value=(0, "")) as discard, \
                fake.fake_world(all_sessions=([], {"hc"}, {SPACE}), backend_for=scan.HC,
                                parked_agent=self.PARKED):
            r = scan.cleanup_item(state, KEY)
        discard.assert_not_called()
        self.assertFalse(r["ok"])
        self.assertIn(KEY, state["items"])


class RehomeEventsTest(unittest.TestCase):
    def test_the_tick_logs_the_rehome_and_no_gone_row(self):
        state = tab_item()
        live = {MOVED: agent_in(MOVED)}
        with fake.fake_world(all_sessions=([tab_row(MOVED)], {"hc"}, {SPACE, MOVED}),
                             all_live=live, pr_detail=pr_detail, backend_for=scan.HC):
            scan.save(state)
            code, out = run_tick("--quiet")
            self.assertIn({"at": mock.ANY, "kind": "rehome", "key": KEY,
                           "session": MOVED}, events())
            self.assertNotIn("gone", [e["kind"] for e in events()])
            with open(scan.STATE_FILE) as fh:
                self.assertEqual(json.load(fh)["items"][KEY]["session"], MOVED)
        self.assertEqual((code, out), (0, ""))


if __name__ == "__main__":
    unittest.main()
