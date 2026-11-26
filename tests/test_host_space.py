"""octopilot_space and octopilot_workspace: where worker tabs open, and host-space
prints it. Nothing reaches herdr, hc or herdr-app: their calls are faked."""
import contextlib
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

from tests import SCRATCH
import scan

HEADLESS, CODE, PROMPTER = scan.HEADLESS_AGENT, scan.CODE_AGENT, scan.PROMPTER_AGENT


class HostSpaceTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.mkdtemp(prefix="host-", dir=SCRATCH)
        self.addCleanup(shutil.rmtree, folder, True)
        self.this = os.path.join(folder, "this")
        os.makedirs(self.this)
        self.terms = {}      # address -> (agent name, labels)
        self.spaces = []     # herdr-app ls rows
        self.workspaces = []
        self.agent_ws = {}   # agent name -> workspace id
        mock.patch.object(scan, "OCTOPILOT_DIR", self.this).start()
        mock.patch.object(scan, "hc", side_effect=self.fake_hc).start()
        mock.patch.object(scan, "cli", side_effect=self.fake_cli).start()
        mock.patch.object(scan, "herdr", side_effect=self.fake_herdr).start()
        mock.patch.object(scan.shutil, "which", return_value="/usr/bin/fake").start()
        self.addCleanup(mock.patch.stopall)

    def terminal(self, address, agent, **labels):
        self.terms[address] = (agent, {"octopilot.dir": self.this, **labels})

    def fake_hc(self, args, timeout=60):
        if args[:2] == ["ls", "--json"]:
            name = args[-1].split("=", 1)[1]
            return 0, {"terminals": [{"address": a} for a, (agent, _)
                                     in self.terms.items() if agent == name]}, ""
        if args[0] == "get":
            if args[1] not in self.terms:
                return 1, {"error": {"kind": "not_found"}}, "terminal not found"
            return 0, {"labels": self.terms[args[1]][1]}, ""
        raise AssertionError(f"unexpected hc call {args}")

    def fake_cli(self, args, timeout=60):
        if args[:2] == ["herdr-app", "ls"]:
            return 0, {"spaces": self.spaces}, ""
        raise AssertionError(f"unexpected cli call {args}")

    def fake_herdr(self, args, timeout=60):
        if args == ["workspace", "list"]:
            if self.workspaces is None:
                return 1, None, "herdr down"
            return 0, {"result": {"workspaces": self.workspaces}}, ""
        if args[:2] == ["agent", "get"] and args[2] in self.agent_ws:
            return 0, {"result": {"agent": {"workspace_id": self.agent_ws[args[2]]}}}, ""
        if args[:2] == ["agent", "get"]:
            return 1, {"error": {"code": "agent_not_found"}}, "no such agent"
        raise AssertionError(f"unexpected herdr call {args}")

    def space(self, sid, machine=None):
        row = {"id": sid, "name": sid, "folder": self.this}
        if machine:
            row["machine"] = machine
        self.spaces.append(row)


class OctopilotSpaceTest(HostSpaceTest):
    def test_the_headless_terminals_space(self):
        self.space("sp1")
        self.terminal("t1", HEADLESS, **{"herdr.space": "sp1"})
        self.assertEqual(scan.octopilot_space(), "sp1")

    def test_a_parked_headless_uses_octopilot_space(self):
        self.space("sp1")
        self.terminal("t1", HEADLESS, **{"octopilot.space": "sp1"})
        self.assertEqual(scan.octopilot_space(), "sp1")

    def test_herdr_space_wins_over_octopilot_space(self):
        self.space("sp1")
        self.space("sp2")
        self.terminal("t1", HEADLESS, **{"herdr.space": "sp1",
                                         "octopilot.space": "sp2"})
        self.assertEqual(scan.octopilot_space(), "sp1")

    def test_a_space_not_listed_falls_through_to_the_next_agent(self):
        self.space("sp2")
        self.terminal("t1", HEADLESS, **{"herdr.space": "sp9"})
        self.terminal("t2", CODE, **{"herdr.space": "sp2"})
        self.assertEqual(scan.octopilot_space(), "sp2")

    def test_none_when_no_listed_space_is_found(self):
        self.space("sp1")
        self.terminal("t1", HEADLESS, **{"herdr.space": "sp9"})
        self.assertIsNone(scan.octopilot_space())

    def test_a_remote_space_is_not_taken(self):
        self.space("sp1", machine="laptop")
        self.terminal("t1", HEADLESS, **{"herdr.space": "sp1"})
        self.assertIsNone(scan.octopilot_space())

    def test_the_prompter_is_the_fallback(self):
        self.space("sp3")
        self.terminal("t3", PROMPTER, **{"herdr.space": "sp3"})
        self.assertEqual(scan.octopilot_space(), "sp3")

    def test_the_code_agent_comes_before_the_prompter(self):
        self.space("sp2")
        self.space("sp3")
        self.terminal("t2", CODE, **{"herdr.space": "sp2"})
        self.terminal("t3", PROMPTER, **{"herdr.space": "sp3"})
        self.assertEqual(scan.octopilot_space(), "sp2")

    def test_none_when_herdr_app_lists_nothing(self):
        self.terminal("t1", HEADLESS, **{"herdr.space": "sp1"})
        self.assertIsNone(scan.octopilot_space())

    def test_none_when_no_agent_runs(self):
        self.space("sp1")
        self.assertIsNone(scan.octopilot_space())


class OctopilotWorkspaceTest(HostSpaceTest):
    def test_the_workspace_labelled_octopilot(self):
        self.workspaces = [{"workspace_id": "w1", "label": "Other"},
                           {"workspace_id": "w7", "label": scan.OCTOPILOT_LABEL}]
        self.agent_ws = {PROMPTER: "w3"}
        self.assertEqual(scan.octopilot_workspace(), "w7")

    def test_the_prompters_workspace_is_the_fallback(self):
        self.workspaces = [{"workspace_id": "w1", "label": "Other"}]
        self.agent_ws = {PROMPTER: "w3"}
        self.assertEqual(scan.octopilot_workspace(), "w3")

    def test_none_when_neither_is_found(self):
        self.workspaces = [{"workspace_id": "w1", "label": "Other"}]
        self.assertIsNone(scan.octopilot_workspace())

    def test_none_when_herdr_cannot_list_workspaces(self):
        self.workspaces = None
        self.assertIsNone(scan.octopilot_workspace())


class HostSpaceCommandTest(HostSpaceTest):
    def run_host_space(self, backend):
        out = io.StringIO()
        with mock.patch.object(scan, "OCTOPILOT_BACKEND", backend), \
                contextlib.redirect_stdout(out):
            code = scan.dispatch("host-space", ["host-space"], {})
        return code, out.getvalue()

    def test_prints_the_gui_space(self):
        self.space("sp1")
        self.terminal("t1", HEADLESS, **{"herdr.space": "sp1"})
        self.assertEqual(self.run_host_space("hc"), (0, "sp1\n"))

    def test_prints_the_tui_workspace(self):
        self.workspaces = [{"workspace_id": "w7", "label": scan.OCTOPILOT_LABEL}]
        self.assertEqual(self.run_host_space("herdr"), (0, "w7\n"))

    def test_prints_nothing_when_no_host_is_found(self):
        self.assertEqual(self.run_host_space("hc"), (0, ""))
        self.assertEqual(self.run_host_space("herdr"), (0, ""))

    def test_host_space_is_read_only(self):
        self.assertIn("host-space", scan.READ_ONLY)
