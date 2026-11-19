import contextlib
import io
import os
import sys
import tempfile
import unittest

from prompt_vault.cli import main


class CliTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.file = os.path.join(self.dir.name, "v.jsonl")

    def run_cli(self, *argv, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        old = sys.stdin
        if stdin is not None:
            sys.stdin = io.StringIO(stdin)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(["--file", self.file, *argv])
        finally:
            sys.stdin = old
        return code, out.getvalue(), err.getvalue()

    def test_commit_show_log_ls_diff(self):
        code, out, _ = self.run_cli("commit", "t", "--text", "Hello {{name}}.", "--author", "maria", "--note", "first", "--config", "temperature=0.2")
        self.assertEqual(code, 0)
        self.assertIn("t v1", out)
        self.assertIn("(unchanged)", self.run_cli("commit", "t", "--text", "Hello {{name}}.", "--config", "temperature=0.2")[1])
        self.run_cli("commit", "t", "--text", "Hello {{name}}!")
        self.assertEqual(self.run_cli("show", "t", "1")[1].strip(), "Hello {{name}}.")
        self.assertIn("first", self.run_cli("log", "t")[1])
        self.assertIn("t", self.run_cli("ls")[1])
        diff = self.run_cli("diff", "t", "1", "2")[1]
        self.assertIn("-Hello {{name}}.", diff)
        self.assertIn("+Hello {{name}}!", diff)

    def test_commit_from_stdin_and_file(self):
        self.run_cli("commit", "t", "--from", "-", stdin="From stdin")
        path = os.path.join(self.dir.name, "p.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("From file")
        self.assertIn("v2", self.run_cli("commit", "t", "--from", path)[1])
        self.assertEqual(self.run_cli("show", "t")[1].strip(), "From file")

    def test_render_and_vars(self):
        self.run_cli("commit", "t", "--text", "Hi {{name}}, be {{tone|brief}}.")
        self.assertEqual(self.run_cli("render", "t", "--var", "name=Maria")[1].strip(), "Hi Maria, be brief.")
        code, _, err = self.run_cli("render", "t")
        self.assertEqual(code, 2)
        self.assertIn("missing variable: name", err)
        code, out, _ = self.run_cli("render", "t", "--lax")
        self.assertIn("{{name}}", out)
        self.assertIn("name  (required)", self.run_cli("vars", "t")[1])
        self.assertEqual(self.run_cli("render", "t", "--var", "oops")[0], 2)

    def test_lint_exit_codes(self):
        self.run_cli("commit", "t", "--text", "Hello {{ name")
        self.assertEqual(self.run_cli("lint", "t")[0], 1)
        self.run_cli("commit", "t", "--text", "Hello {{name}}.\n")
        self.assertEqual(self.run_cli("lint", "t", "--var", "name")[0], 0)
        self.assertEqual(self.run_cli("lint", "t", "--var", "name", "--var", "extra", "--fail-on", "warning")[0], 1)
        self.run_cli("commit", "t", "--text", "trailing \n")
        self.assertEqual(self.run_cli("lint", "t")[0], 0)
        self.assertEqual(self.run_cli("lint", "t", "--fail-on", "info")[0], 1)

    def test_policy_deploy_and_refusals(self):
        self.run_cli("policy", "--stages", "dev,prod", "--protected", "prod")
        self.run_cli("commit", "t", "--text", "one")
        code, _, err = self.run_cli("deploy", "t", "prod")
        self.assertEqual(code, 1)
        self.assertIn("has not been live at dev", err)
        self.assertEqual(self.run_cli("deploy", "t", "dev")[0], 0)
        self.assertEqual(self.run_cli("deploy", "t", "prod", "--gate-cmd", f"{sys.executable} -c \"raise SystemExit(5)\"")[0], 1)
        code, out, _ = self.run_cli("deploy", "t", "prod", "--lint", "--gate-cmd", f"{sys.executable} -c \"pass\"")
        self.assertEqual(code, 0)
        self.assertIn("prod -> t v1", out)
        self.assertIn("(already there)", self.run_cli("deploy", "t", "prod")[1])
        log = self.run_cli("deploys")[1]
        self.assertEqual(log.count("REFUSED"), 2)
        code, _, err = self.run_cli("tag", "t", "prod")
        self.assertEqual(code, 2)
        self.assertIn("protected", err)

    def test_rollback_blame_verify_head(self):
        self.run_cli("commit", "t", "--text", "a\nb")
        self.run_cli("commit", "t", "--text", "a\nB")
        out = self.run_cli("blame", "t")[1]
        self.assertIn("v1   a", out)
        self.assertIn("v2   B", out)
        self.assertIn("t v3", self.run_cli("rollback", "t", "1")[1])
        self.assertIn("(already the newest)", self.run_cli("rollback", "t", "3")[1])
        code, out, _ = self.run_cli("verify")
        self.assertEqual(code, 0)
        head = self.run_cli("head")[1].strip()
        self.assertEqual(self.run_cli("verify", "--head", head)[0], 0)
        self.assertEqual(self.run_cli("verify", "--head", "0" * 64)[0], 1)

    def test_tampering_is_reported_as_an_integrity_error(self):
        self.run_cli("commit", "t", "--text", "original")
        with open(self.file, encoding="utf-8") as fh:
            text = fh.read()
        with open(self.file, "w", encoding="utf-8") as fh:
            fh.write(text.replace("original", "tampered"))
        code, _, err = self.run_cli("show", "t")
        self.assertEqual(code, 2)
        self.assertIn("integrity:", err)

    def test_errors_are_clean(self):
        self.assertEqual(self.run_cli("show", "nope")[0], 2)
        self.run_cli("commit", "t", "--text", "x")
        self.assertIn("matched nothing", self.run_cli("show", "t", "zzz")[2])
        self.assertEqual(self.run_cli("commit", "t", "--text", "y", "--config", "bad")[0], 2)

    def test_a_failed_command_leaves_the_file_unchanged(self):
        self.run_cli("commit", "t", "--text", "x")
        with open(self.file, encoding="utf-8") as fh:
            before = fh.read()
        self.run_cli("deploy", "t", "prod", "--ref", "9")
        with open(self.file, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before)


if __name__ == "__main__":
    unittest.main()
