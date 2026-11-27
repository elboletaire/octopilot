import unittest
from unittest import mock

from tests import fake  # noqa: F401  (imported first: sets the scratch environment)
from engine import stack_from, walk_stack


def pr(number, head, base):
    return {"number": number, "headRefName": head, "baseRefName": base}


CHAIN = [pr(704, "payg/0-prep", "main"),
         pr(705, "payg/1-foundation", "payg/0-prep"),
         pr(706, "payg/2-ui", "payg/1-foundation")]
BOTTOM_UP = [{"pr": 704, "branch": "payg/0-prep"},
             {"pr": 705, "branch": "payg/1-foundation"},
             {"pr": 706, "branch": "payg/2-ui"}]


class StackFromTest(unittest.TestCase):
    def test_plain_pr_on_default_branch(self):
        self.assertEqual(stack_from([pr(9, "fix/x", "main")], 9, "main"), (None, None))

    def test_not_an_open_pr(self):
        self.assertEqual(stack_from(CHAIN, 99, "main"), (None, "#99 is not an open PR"))

    def test_bottom_not_on_default_branch_is_plain(self):
        prs = [pr(8, "feat/a", "feat/root")]
        self.assertEqual(stack_from(prs, 8, "main"), (None, None))

    def test_two_layers_from_the_bottom(self):
        prs = CHAIN[:2]
        self.assertEqual(stack_from(prs, 704, "main"),
                         (BOTTOM_UP[:2], None))

    def test_middle_of_three_returns_the_whole_stack(self):
        self.assertEqual(stack_from(CHAIN, 705, "main"), (BOTTOM_UP, None))

    def test_top_of_three_returns_the_whole_stack(self):
        self.assertEqual(stack_from(CHAIN, 706, "main"), (BOTTOM_UP, None))

    def test_stack_on_a_non_default_base_is_checked_against_that_base(self):
        prs = [pr(1, "a", "develop"), pr(2, "b", "a")]
        self.assertEqual(stack_from(prs, 2, "develop"),
                         ([{"pr": 1, "branch": "a"}, {"pr": 2, "branch": "b"}], None))
        self.assertEqual(stack_from(prs, 2, "main"), (None, None))

    def test_fork_above_is_an_error_from_every_layer(self):
        # 706 and 707 both stand on 705's branch: a fork above 705.
        prs = CHAIN[:2] + [CHAIN[2], pr(707, "payg/2-api", "payg/1-foundation")]
        for num in (704, 705, 706, 707):
            self.assertEqual(stack_from(prs, num, "main"),
                             (None, "stack forks at #705"), num)

    def test_fork_at_the_top_is_an_error(self):
        prs = CHAIN + [pr(707, "x", "payg/2-ui"), pr(708, "y", "payg/2-ui")]
        self.assertEqual(stack_from(prs, 705, "main"),
                         (None, "stack forks at #706"))

    def test_fork_below_is_an_error(self):
        prs = [pr(1, "a", "main"), pr(2, "a", "main"), pr(3, "b", "a")]
        self.assertEqual(stack_from(prs, 3, "main"),
                         (None, "stack is ambiguous below #3"))

    def test_loop_is_an_error(self):
        prs = [pr(1, "a", "b"), pr(2, "b", "a")]
        self.assertEqual(stack_from(prs, 1, "main"), (None, "stack loops at #1"))


class WalkStackTest(unittest.TestCase):
    def test_one_gh_call_then_stack_from(self):
        with mock.patch("scan.gh_json", return_value=CHAIN) as gh:
            self.assertEqual(walk_stack("vocdoni/app", 705, "main"), (BOTTOM_UP, None))
        self.assertEqual(gh.call_args.args[0][:6],
                         ["pr", "list", "-R", "vocdoni/app", "--state", "open"])
        self.assertEqual(gh.call_count, 1)

    def test_gh_failure_is_an_error(self):
        with mock.patch("scan.gh_json", return_value=None):
            self.assertEqual(walk_stack("vocdoni/app", 705, "main"),
                             (None, "could not list the open PRs of vocdoni/app"))


if __name__ == "__main__":
    unittest.main()
