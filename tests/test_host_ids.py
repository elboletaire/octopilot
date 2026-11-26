"""The session id shapes: whole workspaces and spaces, and their tabs, per backend."""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

from tests import SCRATCH  # noqa: F401  (imports the scratch state before scan)
import scan

TABS = {"tb1f": scan.HC, "tb0": scan.HC, "w1:t2": scan.HERDR, "w12:t3": scan.HERDR}
WHOLE = {"sp1f": scan.HC, "w12": scan.HERDR}
JUNK = [None, "", "junk", "tb", "tbZZ", "sp", "sp1F", "w", "w1:", "w1:t", "w1:t2:t3",
        "w:t2", "w1 :t2", "tb1f:t2", "W12"]


class HostIdsTest(unittest.TestCase):
    def test_is_tab(self):
        for session in TABS:
            self.assertTrue(scan.is_tab(session), session)
        for session in list(WHOLE) + JUNK:
            self.assertFalse(scan.is_tab(session), session)

    def test_backend_for(self):
        for session, backend in {**TABS, **WHOLE}.items():
            self.assertIs(scan.backend_for(session), backend, session)
        for session in JUNK:
            self.assertIsNone(scan.backend_for(session), session)

    def test_host_noun(self):
        self.assertEqual(scan.host_noun(scan.HC, "tb1f"), "herdr-app tab")
        self.assertEqual(scan.host_noun(scan.HC, "tb0"), "herdr-app tab")
        self.assertEqual(scan.host_noun(scan.HERDR, "w1:t2"), "herdr tab")
        self.assertEqual(scan.host_noun(scan.HERDR, "w12:t3"), "herdr tab")
        self.assertEqual(scan.host_noun(scan.HC, "sp1f"), scan.HC.noun)
        self.assertEqual(scan.host_noun(scan.HERDR, "w12"), scan.HERDR.noun)
        for session in JUNK:
            self.assertEqual(scan.host_noun(scan.HERDR, session), scan.HERDR.noun,
                             session)

    def test_remove_label_for(self):
        self.assertEqual(scan.remove_label_for(scan.HC, "tb1f"), "herdr-app tab remove")
        self.assertEqual(scan.remove_label_for(scan.HC, "tb0"), "herdr-app tab remove")
        self.assertEqual(scan.remove_label_for(scan.HERDR, "w1:t2"), "herdr tab remove")
        self.assertEqual(scan.remove_label_for(scan.HERDR, "w12:t3"), "herdr tab remove")
        self.assertEqual(scan.remove_label_for(scan.HC, "sp1f"), scan.HC.remove_label)
        self.assertEqual(scan.remove_label_for(scan.HERDR, "w12"),
                         scan.HERDR.remove_label)
        for session in JUNK:
            self.assertEqual(scan.remove_label_for(scan.HERDR, session),
                             scan.HERDR.remove_label, session)

    def test_every_remove_label_is_listed(self):
        for session, backend in {**TABS, **WHOLE}.items():
            self.assertIn(scan.remove_label_for(backend, session), scan.REMOVE_LABELS)

    def test_worker_host_reads_at_use(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("WORKER_HOST", None)
            self.assertEqual(scan.worker_host(), "tab")
            os.environ["WORKER_HOST"] = "space"
            self.assertEqual(scan.worker_host(), "space")
            os.environ["WORKER_HOST"] = "Tab"
            with self.assertRaises(SystemExit) as ctx:
                scan.worker_host()
            self.assertEqual(str(ctx.exception),
                             "WORKER_HOST must be tab or space, not 'Tab'")

    def test_track_names_the_tab_shapes(self):
        argv = ["scan.py", "track", "k", "comment", "b", "junk"]
        err = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(scan, "all_sessions", side_effect=AssertionError), \
                contextlib.redirect_stderr(err):
            self.assertEqual(scan.main(), 1)
        self.assertIn("herdr tab (w12:t1)", err.getvalue())
        self.assertIn("herdr-app tab (tb<hex>)", err.getvalue())


if __name__ == "__main__":
    unittest.main()
