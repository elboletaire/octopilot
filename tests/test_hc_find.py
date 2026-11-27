"""HC.find picks a terminal of this checkout, or an unlabelled one, never another
checkout's (open-gui.sh labels each agent with its octopilot.dir)."""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from tests import SCRATCH
import scan


class HcFindTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.mkdtemp(prefix="find-", dir=SCRATCH)
        self.addCleanup(shutil.rmtree, folder, True)
        self.this = os.path.join(folder, "this")
        self.other = os.path.join(folder, "other")
        os.makedirs(self.this)
        os.makedirs(self.other)
        # A symlink to this checkout's dir is still this checkout.
        self.link = os.path.join(folder, "link")
        os.symlink(self.this, self.link)
        self.labels = {}
        self.foreground = {}   # terminal -> the program in its foreground (claude)
        self.listed = []
        mock.patch.object(scan, "OCTOPILOT_DIR", self.this).start()
        mock.patch.object(scan, "hc", side_effect=self.fake_hc).start()
        self.addCleanup(mock.patch.stopall)

    def fake_hc(self, args, timeout=60):
        if args[:2] == ["ls", "--json"]:
            return 0, {"terminals": [{"address": a} for a in self.listed]}, ""
        if args[0] == "get":
            info = self.labels.get(args[1])
            if info is None:
                return 1, {"error": {"kind": "not_found"}}, "terminal not found"
            name = self.foreground.get(args[1], "claude")
            return 0, {"labels": info, "foreground": {"name": name}}, ""
        raise AssertionError(f"unexpected hc call {args}")

    def test_prefers_this_checkouts_terminal_over_an_unlabelled_one(self):
        self.listed = ["t3", "t2", "t1"]
        self.labels = {"t1": {"octopilot.dir": self.this},
                       "t2": {"octopilot.dir": self.other},
                       "t3": {}}
        self.assertEqual(scan.HC.find("octopilot"), "hc:t1")

    def test_a_symlinked_dir_label_is_this_checkout(self):
        self.listed = ["t1"]
        self.labels = {"t1": {"octopilot.dir": self.link}}
        self.assertEqual(scan.HC.find("octopilot"), "hc:t1")

    def test_takes_an_unlabelled_terminal_when_no_checkout_matches(self):
        self.listed = ["t2", "t3"]
        self.labels = {"t2": {"octopilot.dir": self.other},
                       "t3": {"octopilot.dir": ""}}
        self.assertEqual(scan.HC.find("octopilot"), "hc:t3")

    def test_never_picks_another_checkouts_terminal(self):
        self.listed = ["t2"]
        self.labels = {"t2": {"octopilot.dir": self.other}}
        self.assertIsNone(scan.HC.find("octopilot"))

    def test_a_failed_get_is_skipped_not_taken_as_unlabelled(self):
        # t1's error reply has no labels: it must not pass for a worker's terminal.
        self.listed = ["t1", "t2"]
        self.labels = {"t2": {}}
        self.assertEqual(scan.HC.find("octopilot"), "hc:t2")

    def test_none_when_every_get_fails(self):
        self.listed = ["t1"]
        self.assertIsNone(scan.HC.find("octopilot"))

    def test_an_agent_beats_a_bare_shell_with_the_same_label(self):
        # A shell left by a failed end keeps the label: the agent is the worker.
        self.listed = ["t1", "t2"]
        self.labels = {"t1": {"octopilot.dir": self.this},
                       "t2": {"octopilot.dir": self.this}}
        self.foreground = {"t1": "zsh"}
        self.assertEqual(scan.HC.find("octopilot"), "hc:t2")

    def test_an_unlabelled_agent_beats_this_checkouts_bare_shell(self):
        self.listed = ["t1", "t2"]
        self.labels = {"t1": {"octopilot.dir": self.this}, "t2": {}}
        self.foreground = {"t1": "bash"}
        self.assertEqual(scan.HC.find("octopilot"), "hc:t2")

    def test_a_bare_shell_is_found_when_nothing_else_is(self):
        self.listed = ["t1", "t2"]
        self.labels = {"t1": {"octopilot.dir": self.other},
                       "t2": {"octopilot.dir": self.this}}
        self.foreground = {"t1": "zsh", "t2": "zsh"}
        self.assertEqual(scan.HC.find("octopilot"), "hc:t2")

    def test_none_when_nothing_is_listed(self):
        self.assertIsNone(scan.HC.find("octopilot"))
