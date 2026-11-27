import os
import subprocess
import tempfile
import unittest

os.environ.setdefault("STATE_FILE", os.path.join(tempfile.mkdtemp(), "state.json"))
import scan  # noqa: E402


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class ForkPrWorktreeTest(unittest.TestCase):
    def test_a_fork_branch_is_fetched_from_the_pull_ref(self):
        tmp = tempfile.mkdtemp()
        origin, clone = os.path.join(tmp, "origin"), os.path.join(tmp, "clone")
        os.mkdir(origin)
        git("init", "-q", "-b", "main", cwd=origin)
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
            "--allow-empty", "-m", "base", cwd=origin)
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
            "--allow-empty", "-m", "fork work", cwd=origin)
        # The fork's commit is only reachable through GitHub's pull ref.
        git("update-ref", "refs/pull/81/head", "HEAD", cwd=origin)
        git("reset", "-q", "--hard", "HEAD~1", cwd=origin)
        git("clone", "-q", origin, clone, cwd=tmp)
        wt = scan.add_worktree(clone, "main", "fork/topic", False, 81)
        self.assertEqual(wt, os.path.join(clone, ".worktrees", "fork-topic"))
        log = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=wt,
                             capture_output=True, text=True).stdout.strip()
        self.assertEqual(log, "fork work")


if __name__ == "__main__":
    unittest.main()
