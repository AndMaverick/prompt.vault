import unittest

from prompt_vault import (
    MissingVariable,
    UnknownVariable,
    compare,
    estimate_tokens,
    render,
    variables,
)
from prompt_vault import lint as run_lint
from prompt_vault.diffing import blame
from prompt_vault.template import TemplateError, malformed


class TemplateTests(unittest.TestCase):
    def test_variables_in_order_with_defaults(self):
        vs = variables("Hi {{name}}, you are {{ role | a helper }}. Bye {{name}} {{user.email}}")
        self.assertEqual([(v.name, v.default, v.required) for v in vs],
                         [("name", None, True), ("role", "a helper", False), ("user.email", None, True)])

    def test_a_variable_is_optional_only_if_every_use_has_a_default(self):
        self.assertTrue(variables("{{a|x}} and {{a}}")[0].required)
        self.assertFalse(variables("{{a|x}} and {{a|y}}")[0].required)

    def test_render_fills_and_uses_defaults(self):
        self.assertEqual(render("Hi {{name}}, role: {{role|helper}}.", {"name": "Maria"}), "Hi Maria, role: helper.")
        self.assertEqual(render("{{n}}+{{n}}", {"n": 3}), "3+3")

    def test_dotted_names_read_nested_data(self):
        self.assertEqual(render("{{user.name}} <{{user.contact.email}}>", {"user": {"name": "M", "contact": {"email": "m@x.io"}}}), "M <m@x.io>")
        with self.assertRaises(MissingVariable):
            render("{{user.age}}", {"user": {"name": "M"}})

    def test_strict_rendering_catches_missing_and_misspelt(self):
        with self.assertRaises(MissingVariable) as cm:
            render("{{a}} {{b}} {{c}}", {"a": 1})
        self.assertEqual(cm.exception.names, ["b", "c"])
        with self.assertRaises(UnknownVariable) as cm2:
            render("{{name}}", {"name": "x", "nmae": "typo"})
        self.assertEqual(cm2.exception.names, ["nmae"])

    def test_lax_mode_leaves_holes_visible(self):
        self.assertEqual(render("Hi {{name}}", {}, strict=False), "Hi {{name}}")

    def test_none_is_an_error_not_the_word_none(self):
        with self.assertRaises(TemplateError):
            render("{{a}}", {"a": None})

    def test_escaped_braces_are_literal(self):
        self.assertEqual(render("Use \\{{braces}} like {{x}}", {"x": "this"}), "Use {{braces}} like this")
        self.assertEqual(variables("\\{{not_a_var}}"), [])

    def test_values_are_inserted_verbatim_and_not_reinterpreted(self):
        self.assertEqual(render("{{a}}", {"a": "{{b}}"}), "{{b}}")

    def test_malformed_openers_report_their_line(self):
        self.assertEqual(malformed("ok {{a}}\nbad {{ a b }}\nalso {{"), [2, 3])
        self.assertEqual(malformed("escaped \\{{ is fine"), [])


class LintTests(unittest.TestCase):
    def codes(self, text, **kw):
        return [f.code for f in run_lint(text, **kw)]

    def test_empty_prompt(self):
        self.assertEqual(self.codes("  \n"), ["PV001"])

    def test_clean_prompt(self):
        self.assertEqual(self.codes("Classify the ticket as {{label}}.", declared=["label"]), [])

    def test_malformed_placeholder(self):
        self.assertIn("PV002", self.codes("Hello {{ name"))

    def test_secrets_are_errors_and_never_echoed(self):
        samples = ["sk-" + "a1B2c3D4e5" * 3, "AKIA" + "ABCDEFGHIJKLMNOP", "ghp_" + "x" * 36, "-----BEGIN " + "RSA PRIVATE KEY-----",
                   "pass" + "word = " + "hunter2hunter2hunter2"]
        for s in samples:
            findings = run_lint("Use this: " + s)
            self.assertEqual([f.code for f in findings], ["PV003"], s)
            self.assertNotIn(s, str(findings[0]))

    def test_declared_variables(self):
        findings = run_lint("{{a}} and {{b}}", declared=["a", "c"])
        self.assertEqual([(f.code, f.severity) for f in findings], [("PV004", "error"), ("PV005", "warning")])
        self.assertEqual(run_lint("{{b|fallback}}", declared=["b"]), [])
        self.assertEqual(run_lint("{{user.name}}", declared=["user"]), [])

    def test_optional_placeholders_need_no_declaration(self):
        self.assertEqual(run_lint("{{tone|polite}}", declared=[]), [])

    def test_case_only_variable_names(self):
        self.assertIn("PV008", self.codes("{{userName}} {{username}}"))

    def test_trailing_whitespace_email_length_and_line_endings(self):
        self.assertIn("PV006", self.codes("hello \nworld"))
        self.assertIn("PV010", self.codes("Contact ops@example.com"))
        self.assertIn("PV007", self.codes("word " * 400, max_tokens=100))
        self.assertIn("PV009", self.codes("a\r\nb\nc"))

    def test_findings_sort_errors_first_with_lines(self):
        findings = run_lint("trailing \nContact ops@example.com\n{{ bad")
        self.assertEqual([f.severity for f in findings], ["error", "warning", "info"])
        self.assertIn("line 3", str(findings[0]))

    def test_token_estimate(self):
        self.assertEqual(estimate_tokens("abcd" * 10), 10)
        self.assertGreater(estimate_tokens("مرحبا" * 10), estimate_tokens("hello" * 10) // 2)


class CompareTests(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(compare("a b", "a b").kind, "identical")
        self.assertEqual(compare("a b", "a b", {"t": 0}, {"t": 1}).kind, "config")
        self.assertEqual(compare("a b", "a b", {"t": 0}, {"t": 1}).config_changed, ("t",))
        self.assertEqual(compare("a  b\n", "a b").kind, "whitespace")
        self.assertEqual(compare("Be brief.", "Be very brief.").kind, "edit")

    def test_new_required_variable_is_breaking(self):
        c = compare("Hi {{name}}", "Hi {{name}} from {{city}}")
        self.assertEqual((c.kind, c.variables_added, c.breaking), ("breaking", ("city",), True))

    def test_removed_variable_is_breaking(self):
        c = compare("Hi {{name}} {{city}}", "Hi {{name}}")
        self.assertEqual((c.kind, c.variables_removed), ("breaking", ("city",)))

    def test_new_optional_variable_is_not_breaking(self):
        self.assertEqual(compare("Hi", "Hi {{tone|polite}}").kind, "edit")

    def test_counts_and_similarity(self):
        c = compare("one\ntwo\nthree", "one\nTWO\nthree\nfour")
        self.assertEqual((c.lines_added, c.lines_removed), (2, 1))
        self.assertEqual(compare("a b c d", "a b c d").similarity, 1.0)
        self.assertAlmostEqual(compare("a b c d", "a b c x").similarity, 0.75)


class BlameTests(unittest.TestCase):
    def test_each_line_credits_the_version_that_wrote_it(self):
        texts = ["one\ntwo", "one\ntwo\nthree", "one\nTWO\nthree", "one\nTWO"]
        self.assertEqual(blame(texts), [("one", 1), ("TWO", 3)])
        self.assertEqual(blame(texts[:3]), [("one", 1), ("TWO", 3), ("three", 2)])

    def test_a_line_deleted_and_written_again_belongs_to_the_later_version(self):
        self.assertEqual(blame(["a\nb", "a", "a\nb"]), [("a", 1), ("b", 3)])

    def test_single_version(self):
        self.assertEqual(blame(["x\ny"]), [("x", 1), ("y", 1)])
        self.assertEqual(blame([]), [])


if __name__ == "__main__":
    unittest.main()
