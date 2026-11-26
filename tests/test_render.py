import unittest

from tests import fake  # noqa: F401  (imported first: sets the environment)
import scan


class RenderForTest(unittest.TestCase):
    def test_item_facts_fill_in_question_branch_and_stack(self):
        state = {"items": {"octo/app#1": {
            "mode": "fix", "pr": 1, "branch": "feat/x", "question": "why?",
            "stack": [{"pr": 1, "branch": "feat/x"}, {"pr": 2, "branch": "feat/y"}]}}}
        out = scan.render_for(state, "investigate", {"KEY": "octo/app#1", "REPO": "octo/app",
                                                     "CONTEXT": "none"})
        self.assertIn("> why?", out)
        self.assertIn("feat/x", out)
        self.assertNotIn("{{", out)

    def test_key_is_found_from_a_layer_pr(self):
        state = {"items": {"octo/app#1": {
            "mode": "fix", "pr": 1, "branch": "feat/x",
            "stack": [{"pr": 1, "branch": "feat/x"}, {"pr": 2, "branch": "feat/y"}]}}}
        subs = {"REPO": "octo/app", "PR": "2", "BRANCH": "feat/y", "BASE": "main",
                "CONTEXT": "none"}
        scan.render_for(state, "address-feedback", subs)
        self.assertEqual(subs["KEY"], "octo/app#1")

    def test_read_only_item_always_gets_the_read_only_prompt(self):
        state = {"items": {"octo/app?inv": {
            "mode": "investigate", "branch": "inv", "question": "what?", "read_only": True}}}
        out = scan.render_for(state, "investigate", {"KEY": "octo/app?inv", "REPO": "octo/app",
                                                     "BRANCH": "inv", "CONTEXT": "none"})
        self.assertIn("octo/app?inv", out)
        expected = scan.render_prompt("investigate-readonly", {
            "KEY": "octo/app?inv", "REPO": "octo/app", "BRANCH": "inv",
            "QUESTION": "what?", "CONTEXT": "none"})
        self.assertEqual(out, expected)

    def test_caller_values_win_over_the_items(self):
        state = {"items": {"octo/app#1": {"mode": "fix", "pr": 1, "branch": "feat/x",
                                          "question": "why?"}}}
        subs = {"KEY": "octo/app#1", "QUESTION": "mine", "CONTEXT": "none"}
        self.assertIn("> mine", scan.render_for(state, "investigate", subs))

    def test_unknown_item_renders_from_the_caller_alone(self):
        out = scan.render_for({"items": {}}, "rebase", {
            "REPO": "octo/app", "PR": "3", "BRANCH": "feat/z", "BASE": "main"})
        self.assertIn("feat/z", out)
        self.assertNotIn("{{", out)

    def test_rendering_never_asks_for_the_orchestrator(self):
        # review-fix carries {{LOOP}}, the template that used to name the
        # orchestrator; rendering must not touch the herdr panes to do it.
        subs = {"REPO": "octo/app", "PR": "1", "BRANCH": "x", "BASE": "main",
                "KEY": "octo/app#1", "CONTEXT": "none"}
        with fake.fake_world():
            scan.render_for({"items": {}}, "review-fix", subs)
        self.assertNotIn("ORCH", subs)


if __name__ == "__main__":
    unittest.main()
