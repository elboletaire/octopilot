import unittest

from engine import Request, parse_request

PAST = "(confirmed past wind-down)"
OVER = "(confirmed over cap)"


class ParseRequestTest(unittest.TestCase):
    def test_one_target_verbs(self):
        for verb in ("go", "no", "detach", "re-review", "ack"):
            self.assertEqual(parse_request(f"{verb} vocdoni/app#7"),
                             Request(verb, ("vocdoni/app#7",), False, False))

    def test_target_may_be_a_bare_number(self):
        self.assertEqual(parse_request("go 3"), Request("go", ("3",), False, False))

    def test_no_args_verbs(self):
        for verb in ("pause", "resume", "tick"):
            self.assertEqual(parse_request(verb), Request(verb, (), False, False))

    def test_extra_words_are_unknown(self):
        self.assertEqual(parse_request("go 3 4").verb, "unknown")
        self.assertEqual(parse_request("pause now").verb, "unknown")
        self.assertEqual(parse_request("go").verb, "unknown")

    def test_unknown_keeps_the_text(self):
        self.assertEqual(parse_request("hello there"),
                         Request("unknown", ("hello there",), False, False))

    def test_empty_is_unknown(self):
        self.assertEqual(parse_request("   ").verb, "unknown")

    def test_past_wind_down_suffix(self):
        self.assertEqual(parse_request(f"go vocdoni/x#1 {PAST}"),
                         Request("go", ("vocdoni/x#1",), True, False))

    def test_over_cap_suffix(self):
        self.assertEqual(parse_request(f"go vocdoni/x#1 {OVER}"),
                         Request("go", ("vocdoni/x#1",), False, True))

    def test_both_suffixes_in_either_order(self):
        for text in (f"go 3 {PAST} {OVER}", f"go 3 {OVER} {PAST}"):
            self.assertEqual(parse_request(text),
                             Request("go", ("3",), True, True))

    def test_suffix_alone_leaves_no_target(self):
        self.assertEqual(parse_request(f"go {PAST}").verb, "unknown")

    def test_suffix_in_the_middle_is_not_a_marker(self):
        self.assertEqual(parse_request(f"go {PAST} 3").verb, "unknown")

    def test_investigate(self):
        self.assertEqual(parse_request("/investigate acme/gizmo how does X work"),
                         Request("investigate", ("acme/gizmo", "how does X work"),
                                 False, False))

    def test_investigate_read_only(self):
        self.assertEqual(parse_request("/investigate --read-only gizmo audit it"),
                         Request("investigate-read-only", ("gizmo", "audit it"),
                                 False, False))

    def test_investigate_with_past_suffix(self):
        self.assertEqual(
            parse_request(f"/investigate --read-only acme/gizmo why? {PAST}"),
            Request("investigate-read-only", ("acme/gizmo", "why?"), True, False))

    def test_investigate_question_with_confirmed_text_is_kept(self):
        question = "why did the (confirmed by QA) build fail?"
        self.assertEqual(parse_request(f"/investigate acme/gizmo {question}"),
                         Request("investigate", ("acme/gizmo", question), False, False))
        self.assertEqual(parse_request(f"/investigate acme/gizmo {question} {PAST}"),
                         Request("investigate", ("acme/gizmo", question), True, False))

    def test_investigate_question_keeps_an_inner_over_cap_marker(self):
        question = f"is it {OVER} a flag?"
        self.assertEqual(parse_request(f"/investigate acme/gizmo {question} {PAST}"),
                         Request("investigate", ("acme/gizmo", question), True, False))

    def test_investigate_needs_a_question(self):
        self.assertEqual(parse_request("/investigate acme/gizmo").verb, "unknown")
        self.assertEqual(parse_request("/investigate").verb, "unknown")

    def test_code_is_verbatim(self):
        self.assertEqual(parse_request("/octopilot-code say (confirmed over cap) it"),
                         Request("code", ("say (confirmed over cap) it",), False, False))
        self.assertEqual(parse_request("/octopilot-code fix the scan\nsecond line"),
                         Request("code", ("fix the scan\nsecond line",), False, False))

    def test_code_keeps_trailing_markers(self):
        self.assertEqual(parse_request(f"/octopilot-code fix it {PAST}"),
                         Request("code", (f"fix it {PAST}",), False, False))
        self.assertEqual(parse_request(f"/octopilot-code fix it {OVER}"),
                         Request("code", (f"fix it {OVER}",), False, False))

    def test_code_needs_text(self):
        self.assertEqual(parse_request("/octopilot-code").verb, "unknown")

    def test_report_line(self):
        self.assertEqual(parse_request("OCTOPILOT vocdoni/app#7 ready 1a2b3c4 all green"),
                         Request("report", ("vocdoni/app#7", "ready", "1a2b3c4",
                                            "all green"), False, False))

    def test_report_without_message(self):
        self.assertEqual(parse_request("OCTOPILOT vocdoni/app#7 ready 1a2b3c4"),
                         Request("report", ("vocdoni/app#7", "ready", "1a2b3c4", ""),
                                 False, False))

    def test_report_keeps_its_markers(self):
        self.assertEqual(parse_request(f"OCTOPILOT a/b#1 blocked 0 waiting {PAST}"),
                         Request("report", ("a/b#1", "blocked", "0", f"waiting {PAST}"),
                                 False, False))

    def test_short_report_is_unknown(self):
        self.assertEqual(parse_request("OCTOPILOT a/b#1 ready").verb, "unknown")


if __name__ == "__main__":
    unittest.main()
