"""The kitchen's speech: the engine's last lines, read from the headless
dashboard's view.json (CONTRACT.md, "Speech and chat")."""
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from tests import SCRATCH
import scan


class SpeechTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.mkdtemp(prefix="speech-", dir=SCRATCH)
        self.addCleanup(shutil.rmtree, folder, True)
        mock.patch.object(scan, "VIEW_FILE", os.path.join(folder, "view.json")).start()
        self.find = mock.patch.object(scan.HC, "find", return_value=None).start()
        self.addCleanup(mock.patch.stopall)

    def write_view(self, generated_at=None, mode="on", state="idle", last=None):
        doc = {"v": 1, "generated_at": time.time() if generated_at is None else generated_at,
               "header": {"engine": {"mode": mode, "state": state, "since_s": None,
                                     "last": last}}}
        with open(scan.VIEW_FILE, "w") as fh:
            json.dump(doc, fh)

    def test_offline_when_the_view_is_missing(self):
        out = scan.speech_cmd({"paused": True})
        self.assertEqual((out["status"], out["paused"], out["text"]),
                         ("offline", True, ""))

    def test_offline_when_the_view_is_stale(self):
        self.write_view(generated_at=time.time() - 16, state="acting")
        self.assertEqual(scan.speech_cmd({})["status"], "offline")

    def test_offline_when_the_engine_is_off(self):
        self.write_view(mode="off", state="off")
        self.assertEqual(scan.speech_cmd({})["status"], "offline")

    def test_idle_and_working_follow_the_engine_state(self):
        self.write_view(state="idle")
        self.assertEqual(scan.speech_cmd({})["status"], "idle")
        self.write_view(state="scanning")
        self.assertEqual(scan.speech_cmd({})["status"], "idle")
        self.write_view(state="acting")
        self.assertEqual(scan.speech_cmd({})["status"], "working")

    def test_a_stale_engine_is_blocked(self):
        self.write_view(state="stale")
        self.assertEqual(scan.speech_cmd({})["status"], "blocked")

    def test_no_last_gives_no_text(self):
        self.write_view(state="idle")
        out = scan.speech_cmd({})
        self.assertEqual((out["text"], out["at"], out["seq"]), ("", None, 0))

    def test_text_and_at_come_from_the_last_lines(self):
        last = {"at": 1794315600.0, "verb": "tick",
                "lines": ["ready repo#12", "go repo#13"]}
        self.write_view(state="idle", last=last)
        out = scan.speech_cmd({})
        self.assertEqual(out["text"], "- ready repo#12\n- go repo#13")
        self.assertEqual(out["at"], "2026-11-10T13:00:00Z")

    def test_seq_changes_with_the_text_only(self):
        def seq_of(lines):
            self.write_view(last={"at": 1794315600.0, "verb": "tick", "lines": lines})
            return scan.speech_cmd({})["seq"]

        first = seq_of(["ready repo#12"])
        self.assertEqual(seq_of(["ready repo#12"]), first)
        self.assertNotEqual(seq_of(["ready repo#13"]), first)

    def test_terminal_is_the_headless_dashboard_terminal(self):
        self.find.return_value = "hc:t9"
        self.write_view(state="idle")
        out = scan.speech_cmd({})
        self.find.assert_called_with(scan.HEADLESS_AGENT)
        self.assertEqual((out["agent"], out["terminal"]), ("hc:t9", "t9"))
