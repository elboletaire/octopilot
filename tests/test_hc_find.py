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
        self.listed = []
        mock.patch.object(scan, "OCTOPILOT_DIR", self.this).start()
        mock.patch.object(scan, "hc", side_effect=self.fake_hc).start()
        self.addCleanup(mock.patch.stopall)

    def fake_hc(self, args, timeout=60):
        if args[:2] == ["ls", "--json"]:
            return 0, {"terminals": [{"address": a} for a in self.listed]}, ""
        if args[0] == "get":
            info = self.labels.get(args[1])
            return (0, {"labels": info}, "") if info is not None else (1, None, "gone")
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

    def test_skips_a_terminal_whose_labels_cannot_be_read(self):
        self.listed = ["t1", "t2"]
        self.labels = {"t2": {}}
        self.assertEqual(scan.HC.find("octopilot"), "hc:t2")

    def test_none_when_nothing_is_listed(self):
        self.assertIsNone(scan.HC.find("octopilot"))
