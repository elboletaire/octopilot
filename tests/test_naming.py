"""Heuristic branch and title names (naming.py): no model, no scan, no live state."""
import os
import subprocess
import tempfile
import unittest

import naming


class Slugify(unittest.TestCase):
    def test_words_and_symbols(self):
        self.assertEqual(naming.slugify("Hello, World!"), "hello-world")
        self.assertEqual(naming.slugify("  --a__b--  "), "a-b")
        self.assertEqual(naming.slugify("!!!"), "")

    def test_accents_and_unicode(self):
        self.assertEqual(naming.slugify("Crème brûlée"), "creme-brulee")
        self.assertEqual(naming.slugify("Añadir soporte para ñandú"), "anadir-soporte-para-nandu")
        self.assertEqual(naming.slugify("日本語 text"), "text")

    def test_cut_at_word_boundary(self):
        long = "Implement the new configuration loader for every supported environment"
        self.assertEqual(naming.slugify(long), "implement-the-new-configuration-loader")
        self.assertEqual(naming.slugify("aaa bbb", 5), "aaa")
        self.assertEqual(naming.slugify("aaaa-bbbb", 5), "aaaa")

    def test_hard_cut_without_a_boundary(self):
        self.assertEqual(naming.slugify("abcdefghij", 4), "abcd")


class BranchNames(unittest.TestCase):
    # (title, labels, issue number, expected branch)
    CASES = [
        ("Vertical login", [], 1756, "feat/vertical-login-1756"),
        ("Login button crashes on Safari", [], 7, "fix/login-button-crashes-on-safari-7"),
        ("fix(auth): refresh tokens", [], 8, "fix/refresh-tokens-8"),
        ("feat!: drop the v1 api", [], 9, "feat/drop-the-v1-api-9"),
        ("chore: bump deps", [], 10, "chore/bump-deps-10"),
        ("Docs: explain setup", [], 11, "docs/explain-setup-11"),
        ("Wip: thing", [], 12, "feat/wip-thing-12"),
        ("Fix typo in the readme", [], 13, "fix/fix-typo-in-the-readme-13"),
        ("Update README", [], 14, "docs/update-readme-14"),
        ("Document the engine", [], 15, "docs/document-the-engine-15"),
        ("Anything", ["Bug"], 16, "fix/anything-16"),
        ("feat: add x", ["bug"], 17, "fix/add-x-17"),
        ("Anything", ["documentation", "bug"], 18, "docs/anything-18"),
        ("Improve search", [{"name": "enhancement"}], 19, "feat/improve-search-19"),
        ("Cover the parser", [{"name": "tests"}], 20, "test/cover-the-parser-20"),
        ("Split the module", ["refactor"], 21, "refactor/split-the-module-21"),
        ("Bump lockfile", ["dependencies"], 22, "chore/bump-lockfile-22"),
        ("Fails to start on Windows", [], 23, "fix/fails-to-start-on-windows-23"),
        ("Support Windows 11", [], 24, "feat/support-windows-11-24"),
        ("Añadir soporte", [], 25, "feat/anadir-soporte-25"),
        ("!!!", [], 26, "feat/issue-26"),
        ("feat:", [], 27, "feat/issue-27"),
        ("a", [], 28, "feat/a-28"),
        ("Implement the new configuration loader for every supported environment", [], 29,
         "feat/implement-the-new-configuration-loader-29"),
    ]

    def test_issue_names_table(self):
        for title, labels, number, expected in self.CASES:
            with self.subTest(title=title, labels=labels):
                branch, _ = naming.issue_names(number, title, labels)
                self.assertEqual(branch, expected)

    def test_title_from_branch_round_trip(self):
        for title, expected in [("Vertical login", "Vertical Login"),
                                ("Support Windows 11", "Support Windows 11"),
                                ("Login button crashes", "Login Button Crashes")]:
            with self.subTest(title=title):
                branch, shown = naming.issue_names(1756, title)
                self.assertEqual(shown, expected)
                self.assertEqual(naming.title_from_branch(branch), expected)

    def test_title_from_branch_drops_only_the_trailing_number(self):
        self.assertEqual(naming.title_from_branch("feat/vertical-login-1756"), "Vertical Login")
        self.assertEqual(naming.title_from_branch("fix/x-1-1756"), "X 1")
        self.assertEqual(naming.title_from_branch("feat/issue-26"), "Issue")

    def test_branch_type_without_labels_or_prefix(self):
        self.assertEqual(naming.branch_type("Something new"), "feat")
        self.assertEqual(naming.branch_type("Fails on boot"), "fix")
        self.assertEqual(naming.branch_type("Explain the readme"), "docs")


class InvestigationNames(unittest.TestCase):
    def test_default_is_chore(self):
        name, branch, title = naming.investigation_names("How does the cache invalidate entries?")
        self.assertEqual(name, "how-does-the-cache-invalidate")
        self.assertEqual(branch, "chore/how-does-the-cache-invalidate")
        self.assertEqual(title, "How Does The Cache Invalidate")

    def test_fix_and_docs_keywords(self):
        self.assertEqual(naming.investigation_names("Why does sync crash?")[1],
                         "fix/why-does-sync-crash")
        self.assertEqual(naming.investigation_names("Audit the docs folder", True)[1],
                         "docs/audit-the-docs-folder")

    def test_empty_question_falls_back(self):
        self.assertEqual(naming.investigation_names("???")[0], "investigation")

    def test_choose_names_dispatches(self):
        self.assertEqual(naming.choose_names("issue", number=3, title="Vertical login"),
                         naming.issue_names(3, "Vertical login", ()))
        self.assertEqual(naming.choose_names("investigation", question="Why sync?"),
                         naming.investigation_names("Why sync?", False))
        with self.assertRaises(ValueError):
            naming.choose_names("other")


class UniqueBranch(unittest.TestCase):
    def test_fake_ref_checker(self):
        seen = []

        def taken_refs(repo, branch):
            seen.append((repo, branch))
            return branch in {"feat/x", "feat/x-2"}

        self.assertEqual(naming.unique_branch("/repo", "feat/x", ref_exists=taken_refs),
                         "feat/x-3")
        self.assertEqual(seen[0], ("/repo", "feat/x"))

    def test_names_in_taken_are_skipped(self):
        free = lambda repo, branch: False
        self.assertEqual(naming.unique_branch("/repo", "feat/x", taken={"feat/x"}, ref_exists=free),
                         "feat/x-2")
        self.assertEqual(naming.unique_branch("/repo", "feat/x", ref_exists=free), "feat/x")

    def test_worktree_directory_counts_as_taken(self):
        with tempfile.TemporaryDirectory() as repo:
            os.makedirs(os.path.join(repo, ".worktrees", "feat-x"))
            free = lambda r, b: False
            self.assertEqual(naming.unique_branch(repo, "feat/x", ref_exists=free), "feat/x-2")

    def test_real_git_refs(self):
        with tempfile.TemporaryDirectory() as repo:
            git = ["git", "-C", repo, "-c", "commit.gpgsign=false",
                   "-c", "user.name=test", "-c", "user.email=test@example.com"]
            subprocess.run(["git", "init", "-q", repo], check=True)
            subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "init"], check=True)
            subprocess.run(git + ["branch", "feat/x"], check=True)
            subprocess.run(git + ["update-ref", "refs/remotes/origin/feat/x-2", "HEAD"], check=True)
            self.assertEqual(naming.unique_branch(repo, "feat/x"), "feat/x-3")
            self.assertEqual(naming.unique_branch(repo, "feat/y"), "feat/y")


if __name__ == "__main__":
    unittest.main()
