import contextlib
import copy
import io
import json
import os
import unittest

from tests import fake
import scan
from tests.test_rerequest import KEY, new_state, pr_detail


class SnapshotTest(unittest.TestCase):
    def test_snapshot_publishes_the_rows_and_saves_no_state(self):
        state = new_state()
        before = copy.deepcopy(state)
        with fake.fake_world(pr_detail=pr_detail):
            snap = scan.snapshot(state)
            with open(scan.SNAPSHOT_FILE) as fh:
                published = json.load(fh)
            self.assertEqual(state, before)
        self.assertEqual(snap, published)
        self.assertEqual([(r["kind"], r["key"]) for r in snap["rows"]],
                         [("REVIEW", KEY)])
        self.assertEqual(snap["rows"][0]["item"]["mode"], "comment")
        self.assertEqual(snap["rows"][0]["title"], "Fix it")
        self.assertFalse(os.path.exists(scan.STATE_FILE))

    def test_snapshot_doc_takes_the_header_from_state_and_leaves_rows_alone(self):
        state = {"paused": True, "last_tick": 1794315600, "items": {}}
        rows = [("WORKING", "o/r!1", "branch=x"), ("CAPPED", "o/r#2", "rounds=3/3")]
        info = {"details": {}, "meta": {}}
        with fake.fake_world():
            doc = scan.snapshot_doc(state, state, rows, 4, 3, 1, info)
        self.assertTrue(doc["paused"])
        self.assertEqual((doc["active"], doc["repos"], doc["untracked"]), (4, 3, 1))
        self.assertEqual(doc["last_tick"], 1794315600)
        self.assertEqual([r["kind"] for r in doc["rows"]], ["WORKING", "CAPPED"])
        self.assertEqual(rows[0][0], "WORKING")

    def test_write_snapshot_replaces_the_file_in_place(self):
        with fake.fake_world():
            scan.write_snapshot({"rows": [], "generated_at": 1})
            scan.write_snapshot({"rows": [], "generated_at": 2})
            with open(scan.SNAPSHOT_FILE) as fh:
                self.assertEqual(json.load(fh)["generated_at"], 2)
            leftovers = [n for n in os.listdir(os.path.dirname(scan.SNAPSHOT_FILE))
                         if ".tmp." in n]
            self.assertEqual(leftovers, [])
