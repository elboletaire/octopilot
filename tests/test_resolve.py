import unittest

from engine import resolve_target

REGISTRY = {"vocdoni/app": {"path": "/c/app", "base": "main"},
            "vocdoni/sdk": {"path": "/c/sdk", "base": "main"},
            "other/app": {"path": "/c/other-app", "base": "main"},
            "solo/tool": {"path": "/c/tool", "base": "main"}}


def row(kind, key):
    return {"kind": kind, "key": key, "fields": {}}


class ResolveTargetTest(unittest.TestCase):
    def setUp(self):
        self.state = {"registry": REGISTRY, "items": {
            "vocdoni/sdk#12": {"prev_keys": ["vocdoni/sdk!4"]},
            "solo/tool#2": {}}}
        self.rows = [row("PROPOSE", "vocdoni/app!9"),
                     row("WORKING", "vocdoni/sdk#12"),
                     row("PROPOSE", "solo/tool#5"),
                     row("GONE", "solo/tool#8")]

    def resolve(self, token):
        return resolve_target(token, self.state, self.rows)

    def test_exact_tracked_key(self):
        self.assertEqual(self.resolve("solo/tool#2"), ("solo/tool#2", None))

    def test_exact_row_key(self):
        self.assertEqual(self.resolve("vocdoni/app!9"), ("vocdoni/app!9", None))

    def test_re_keyed_item_by_prev_key(self):
        self.assertEqual(self.resolve("vocdoni/sdk!4"), ("vocdoni/sdk#12", None))

    def test_repo_suffix_and_number(self):
        self.assertEqual(self.resolve("sdk#12"), ("vocdoni/sdk#12", None))
        self.assertEqual(self.resolve("tool#5"), ("solo/tool#5", None))

    def test_full_slug_with_number(self):
        self.assertEqual(self.resolve("vocdoni/app#3"), ("vocdoni/app#3", None))

    def test_full_slug_wins_over_suffix_matches(self):
        state = {"registry": {"app": {}, "vocdoni/app": {}}, "items": {}}
        self.assertEqual(resolve_target("app!4", state, []), ("app!4", None))

    def test_ambiguous_suffix_names_the_candidates(self):
        key, err = self.resolve("app#3")
        self.assertIsNone(key)
        self.assertEqual(err, "app is ambiguous: other/app vocdoni/app")

    def test_unknown_repo(self):
        self.assertEqual(self.resolve("nope#1"), (None, "nope is not in the registry"))

    def test_bare_number_of_a_proposal(self):
        self.assertEqual(self.resolve("9"), ("vocdoni/app!9", None))

    def test_bare_number_of_a_tracked_row(self):
        self.assertEqual(self.resolve("12"), ("vocdoni/sdk#12", None))

    def test_bare_number_must_be_unique(self):
        self.rows.append(row("PROPOSE", "other/app#5"))
        self.assertEqual(self.resolve("5"),
                         (None, "#5 is ambiguous: other/app#5 solo/tool#5"))

    def test_bare_number_with_no_row(self):
        self.assertEqual(self.resolve("77"), (None, "no proposed or tracked item #77"))

    def test_gone_rows_do_not_count_for_a_bare_number(self):
        self.assertEqual(self.resolve("8"), (None, "no proposed or tracked item #8"))

    def test_unknown_token(self):
        self.assertEqual(self.resolve("banana"), (None, "unknown target 'banana'"))

    def test_unregistered_full_slug_is_refused(self):
        self.assertEqual(resolve_target("x/y#1", {"items": {"a/b#1": {}}}, []),
                         (None, "x/y is not in the registry"))


if __name__ == "__main__":
    unittest.main()
