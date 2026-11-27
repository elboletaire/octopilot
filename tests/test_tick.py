import contextlib
import fcntl
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from tests import fake
import scan
from tests.test_rerequest import FakeBackend, HEAD, KEY, new_state, pr_detail


def events():
    """The event log written so far, as dicts."""
    with open(scan.EVENTS_FILE) as fh:
        return [json.loads(line) for line in fh]


def run_tick(*args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = scan.tick(["tick", *args])
    return code, out.getvalue()


class TickTest(unittest.TestCase):
    def test_tick_saves_the_transitions_and_writes_the_snapshot(self):
        state = new_state()
        with fake.fake_world(pr_detail=pr_detail):
            scan.save(state)
            code, out = run_tick("--quiet")
            self.assertEqual(code, 0)
            self.assertEqual(out, "")
            with open(scan.STATE_FILE) as fh:
                saved = json.load(fh)
            with open(scan.SNAPSHOT_FILE) as fh:
                snap = json.load(fh)
            self.assertEqual(saved["items"][KEY]["pending_rerequest"], HEAD)
            self.assertEqual(saved["tick_counts"], {"REVIEW": 1})
            self.assertIsInstance(saved["last_tick"], int)
            self.assertNotIn("last_scan", saved)
            self.assertIsInstance(snap["last_tick"], int)
            self.assertNotIn("orch", saved)
            self.assertEqual([(r["kind"], r["key"]) for r in snap["rows"]],
                             [("REVIEW", KEY)])

    def test_tick_prints_the_table_unless_quiet(self):
        with fake.fake_world(pr_detail=pr_detail):
            scan.save(new_state())
            code, out = run_tick()
        self.assertEqual(code, 0)
        self.assertIn(KEY, out)

    def test_tick_logs_only_a_change_of_table(self):
        with fake.fake_world(pr_detail=pr_detail):
            scan.save(new_state())
            run_tick("--quiet")
            run_tick("--quiet")
            self.assertEqual([e["kind"] for e in events()], ["tick"])

    def test_tick_logs_a_gone_row_once_it_untracks_the_item(self):
        gone = {"items": {KEY: {"mode": "fix", "pr": 5, "session": "w1"}}}
        with fake.fake_world(all_sessions=([], {"herdr"}, set()),
                             backend_for=lambda s: FakeBackend(),
                             pr_detail=pr_detail):
            scan.save(dict(scan.EMPTY, **gone))
            run_tick("--quiet")
            with open(scan.STATE_FILE) as fh:
                self.assertEqual(json.load(fh)["items"], {})
            self.assertIn({"at": mock.ANY, "kind": "gone", "key": KEY}, events())

    def test_a_tick_that_finds_the_lock_held_returns_at_once(self):
        with fake.fake_world(), open(scan.TICK_LOCK, "w") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            code, out = run_tick("--quiet")
        self.assertEqual((code, out), (0, "tick already running\n"))
        self.assertFalse(os.path.exists(scan.STATE_FILE))

    def test_a_tick_that_finds_exec_lock_held_scans_but_does_not_wait_to_act(self):
        with fake.fake_world(), mock.patch.dict(os.environ, {"OCTOPILOT_ENGINE": "on"}), \
                open(scan.EXEC_LOCK, "w") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            code, out = run_tick()
        self.assertEqual(code, 0)
        self.assertIn("engine busy", out)

    def test_main_does_not_wrap_tick_in_the_state_lock(self):
        # tick takes the lock itself: main() wrapping it too would deadlock,
        # so count the entries instead of really locking.
        entries = []

        @contextlib.contextmanager
        def counting_lock():
            entries.append(1)
            yield

        with fake.fake_world(pr_detail=pr_detail), \
                mock.patch.object(scan, "locked", counting_lock), \
                mock.patch.object(sys, "argv", ["scan.py", "tick", "--quiet"]):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(scan.main(), 0)
            self.assertTrue(os.path.exists(scan.SNAPSHOT_FILE))
        self.assertEqual(len(entries), 1)

    def test_engine_runs_for_a_scratch_state_dir_or_the_installed_checkout(self):
        with tempfile.TemporaryDirectory() as live, \
                tempfile.TemporaryDirectory() as worktree, \
                mock.patch.object(scan, "installed_dir", return_value=live), \
                mock.patch.object(scan, "OCTOPILOT_DIR", live), \
                mock.patch.dict(os.environ):
            os.environ.pop("OCTOPILOT_STATE_DIR", None)
            self.assertTrue(scan.engine_allowed(live))
            # OCTOPILOT_DIR inherited from the live shell must not decide.
            self.assertFalse(scan.engine_allowed(worktree))
            os.environ["OCTOPILOT_STATE_DIR"] = fake.SCRATCH
            self.assertTrue(scan.engine_allowed(worktree))


SESSION = "1f2e3d4c-0000-4000-8000-000000000001"
RESUMED = "1f2e3d4c-0000-4000-8000-000000000002"


class LauncherSessionsTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.mkdtemp(prefix="launcher-", dir=fake.SCRATCH)
        self.addCleanup(shutil.rmtree, folder, True)
        self.projects = os.path.join(folder, "projects")
        self.sessions = os.path.join(folder, "sessions")
        os.makedirs(self.sessions)
        self.addresses, self.infos = {}, {}
        mock.patch.object(scan, "PROJECTS_DIR", self.projects).start()
        mock.patch.object(scan, "SESSIONS_DIR", self.sessions).start()
        mock.patch.object(scan, "agent_address",
                          side_effect=lambda agent: self.addresses.get(agent)).start()
        mock.patch.object(scan, "terminal_info",
                          side_effect=lambda address: self.infos.get(address)).start()
        self.addCleanup(mock.patch.stopall)

    def run_claude(self, agent, session=SESSION, pid=4242, cwd="/work/repo", fg_cwd=None):
        """A terminal running claude in the foreground, as claude's own session file says."""
        self.addresses[agent] = f"t-{agent}"
        fg = {"pid": pid, "name": "claude"}
        if fg_cwd:
            fg["cwd"] = fg_cwd
        self.infos[f"t-{agent}"] = {"foreground": fg, "cwd": cwd}
        with open(os.path.join(self.sessions, f"{pid}.json"), "w") as fh:
            json.dump({"pid": pid, "sessionId": session}, fh)

    def write_transcript(self, session=SESSION):
        folder = os.path.join(self.projects, "-work-repo")
        os.makedirs(folder, exist_ok=True)
        open(os.path.join(folder, f"{session}.jsonl"), "w").close()

    def test_a_running_launcher_stores_its_live_session_and_cwd(self):
        self.run_claude(scan.PROMPTER_AGENT)
        self.run_claude(scan.CODE_AGENT, session=RESUMED, pid=4343, cwd="/work/other")
        self.write_transcript(SESSION)
        self.write_transcript(RESUMED)
        state = {}
        scan.record_launcher_sessions(state)
        self.assertEqual(state["launcher_sessions"], {
            scan.PROMPTER_AGENT: {"session": SESSION, "cwd": "/work/repo"},
            scan.CODE_AGENT: {"session": RESUMED, "cwd": "/work/other"},
        })

    def test_a_clear_in_the_launcher_stores_the_new_session(self):
        self.run_claude(scan.PROMPTER_AGENT)
        self.write_transcript(SESSION)
        state = {}
        scan.record_launcher_sessions(state)
        self.run_claude(scan.PROMPTER_AGENT, session=RESUMED)
        self.write_transcript(RESUMED)
        scan.record_launcher_sessions(state)
        self.assertEqual(state["launcher_sessions"][scan.PROMPTER_AGENT]["session"], RESUMED)

    def test_a_fresh_clear_with_no_transcript_keeps_the_earlier_session(self):
        self.run_claude(scan.PROMPTER_AGENT)
        self.write_transcript(SESSION)
        state = {}
        scan.record_launcher_sessions(state)
        self.run_claude(scan.PROMPTER_AGENT, session=RESUMED)
        scan.record_launcher_sessions(state)
        self.assertEqual(state["launcher_sessions"][scan.PROMPTER_AGENT],
                         {"session": SESSION, "cwd": "/work/repo"})

    def test_the_cwd_is_the_foreground_claudes_folder_when_it_has_one(self):
        self.run_claude(scan.PROMPTER_AGENT, cwd="/work/terminal", fg_cwd="/work/claude")
        self.write_transcript(SESSION)
        state = {}
        scan.record_launcher_sessions(state)
        self.assertEqual(state["launcher_sessions"][scan.PROMPTER_AGENT]["cwd"], "/work/claude")

    def test_a_launcher_not_running_keeps_its_last_value(self):
        before = {scan.PROMPTER_AGENT: {"session": SESSION, "cwd": "/work/repo"}}
        state = {"launcher_sessions": dict(before)}
        scan.record_launcher_sessions(state)
        self.assertEqual(state["launcher_sessions"], before)

    def test_the_launcher_session_prints_id_and_cwd_when_the_transcript_exists(self):
        self.run_claude(scan.PROMPTER_AGENT)
        self.write_transcript()
        state = {}
        scan.record_launcher_sessions(state)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = scan.launcher_session(["launcher-session", scan.PROMPTER_AGENT], state)
        self.assertEqual((code, out.getvalue()), (0, f"{SESSION}\t/work/repo\n"))

    def test_the_launcher_session_prints_nothing_without_a_transcript(self):
        state = {"launcher_sessions": {scan.PROMPTER_AGENT: {"session": SESSION, "cwd": "/x"}}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = scan.launcher_session(["launcher-session", scan.PROMPTER_AGENT], state)
        self.assertEqual((code, out.getvalue()), (0, ""))

    def test_the_launcher_session_prints_nothing_for_an_agent_never_recorded(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = scan.launcher_session(["launcher-session", scan.CODE_AGENT], {})
        self.assertEqual((code, out.getvalue()), (0, ""))

    def test_tick_records_the_launcher_sessions(self):
        with fake.fake_world(pr_detail=pr_detail) as world:
            scan.save(new_state())
            run_tick("--quiet")
            self.assertEqual(len(world.named("record_launcher_sessions")), 1)
