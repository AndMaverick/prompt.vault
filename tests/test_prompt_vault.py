import unittest

from prompt_vault import PromptVault, RefError


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.vault = PromptVault()
        self.v1 = self.vault.commit("triage", "Classify the ticket.")
        self.v2 = self.vault.commit("triage", "Classify the ticket.\nReturn JSON.")

    def test_versions_increment_and_latest_is_default(self):
        self.assertEqual((self.v1.number, self.v2.number), (1, 2))
        self.assertEqual(self.vault.get("triage"), self.v2)

    def test_identical_commit_is_idempotent(self):
        again = self.vault.commit("triage", "Classify the ticket.\nReturn JSON.")
        self.assertEqual(again, self.v2)
        self.assertEqual(self.vault.get("triage", 2).number, 2)

    def test_lookup_by_number_digest_and_tag(self):
        self.vault.tag("triage", "prod", 1)
        self.assertEqual(self.vault.get("triage", "prod"), self.v1)
        self.assertEqual(self.vault.get("triage", self.v2.digest[:6]), self.v2)

    def test_unknown_refs_raise(self):
        with self.assertRaises(RefError):
            self.vault.get("triage", 9)
        with self.assertRaises(RefError):
            self.vault.get("nope")

    def test_diff_shows_added_line(self):
        self.assertIn("+Return JSON.", self.vault.diff("triage", 1, 2))

    def test_rollback_appends_instead_of_rewriting(self):
        restored = self.vault.rollback("triage", 1)
        self.assertEqual(restored.number, 3)
        self.assertEqual(restored.text, self.v1.text)
        self.assertEqual(self.vault.get("triage", 2), self.v2)

    def test_gate_blocks_deploy(self):
        self.assertFalse(self.vault.deploy("triage", "prod", lambda v: False))
        with self.assertRaises(RefError):
            self.vault.get("triage", "prod")
        self.assertTrue(self.vault.deploy("triage", "prod", lambda v: "JSON" in v.text))
        self.assertEqual(self.vault.get("triage", "prod"), self.v2)


if __name__ == "__main__":
    unittest.main()
