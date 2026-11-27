import unittest

from tests import fake
import scan


class FakeWorldTest(unittest.TestCase):
    def test_answers_and_records_calls(self):
        with fake.fake_world(whoami="ada") as world:
            self.assertEqual(scan.whoami({}), "ada")
            self.assertEqual(scan.gh_json(["api", "x"], default=[]), [])
        self.assertEqual(world.named("whoami"), [(({},), {})])
        self.assertEqual(len(world.named("gh_json")), 1)

    def test_state_files_are_scratch(self):
        with fake.fake_world():
            self.assertTrue(scan.STATE_FILE.startswith(fake.SCRATCH))
            self.assertTrue(scan.SNAPSHOT_FILE.startswith(fake.SCRATCH))

    def test_unknown_fake_is_refused(self):
        with self.assertRaises(TypeError):
            with fake.fake_world(nope=1):
                pass
