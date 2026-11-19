import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from prompt_vault import (
    GateResult,
    IntegrityError,
    PolicyError,
    PromptVault,
    RefError,
    all_of,
    command_gate,
    digest_of,
    lint_gate,
    named,
    threshold_gate,
)


def clock():
    state = {"t": datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)}

    def now():
        state["t"] += timedelta(minutes=1)
        return state["t"]
    return now


def new(**kw):
    return PromptVault(clock=clock(), **kw)


class CommitAndRefTests(unittest.TestCase):
    def setUp(self):
        self.v = new()
        self.v1 = self.v.commit("triage", "Classify the ticket.", author="maria")
        self.v2 = self.v.commit("triage", "Classify the ticket.\nReturn JSON.", "add format")

    def test_original_behaviour(self):
        self.assertEqual((self.v1.number, self.v2.number), (1, 2))
        self.assertEqual(self.v.commit("triage", self.v2.text), self.v2)
        self.assertEqual(self.v.get("triage"), self.v2)
        self.assertEqual(self.v.get("triage", self.v2.digest[:6]), self.v2)
        with self.assertRaises(RefError):
            self.v.get("triage", 9)
        with self.assertRaises(RefError):
            self.v.get("nope")
        self.assertIn("+Return JSON.", self.v.diff("triage", 1, 2))

    def test_plain_text_digest_is_unchanged_from_0_1(self):
        import hashlib
        self.assertEqual(self.v1.digest, hashlib.sha256(b"Classify the ticket.").hexdigest()[:12])

    def test_refs(self):
        self.assertEqual(self.v.get("triage", "v1"), self.v1)
        self.v.tag("triage", "prod", 1)
        self.assertEqual(self.v.get("triage", "prod"), self.v1)
        with self.assertRaises(RefError):
            self.v.get("triage", "nothing")
        with self.assertRaises(RefError):
            self.v.get("triage", True)

    def test_author_and_time_are_recorded(self):
        self.assertEqual(self.v1.author, "maria")
        self.assertTrue(self.v1.at.startswith("2026-03-01T09:01"))

    def test_config_is_part_of_the_version(self):
        a = self.v.commit("triage", self.v2.text, config={"temperature": 0.2})
        self.assertEqual(a.number, 3)  # same text, different settings: a new version
        self.assertNotEqual(a.digest, self.v2.digest)
        self.assertEqual(self.v.commit("triage", self.v2.text, config={"temperature": 0.2}), a)
        self.assertEqual(self.v.compare("triage", 2, 3).kind, "config")
        self.assertEqual(a.digest, digest_of(self.v2.text, {"temperature": 0.2}))

    def test_rollback_appends_and_keeps_config(self):
        self.v.commit("triage", "other", config={"model": "m"})
        back = self.v.rollback("triage", 1)
        self.assertEqual((back.number, back.text, back.note), (4, self.v1.text, "rollback to v1"))
        self.assertEqual(len(self.v.history("triage")), 4)
        again = self.v.rollback("triage", 3)  # v3 holds config {"model": "m"}
        self.assertEqual(again.config, {"model": "m"})

    def test_digest_of_a_rolled_back_text_resolves_to_the_newest_copy(self):
        back = self.v.rollback("triage", 1)
        self.assertEqual(self.v.get("triage", self.v1.digest), back)  # not ambiguous: same content

    def test_ambiguous_prefix_is_an_error(self):
        v = new()
        seen = {}
        for i in range(400):
            ver = v.commit("p", f"text {i}")
            seen.setdefault(ver.digest[0], []).append(ver)
        prefix = next(k for k, vs in seen.items() if len(vs) > 1)
        with self.assertRaises(RefError):
            v.get("p", prefix)

    def test_names_history_tags(self):
        self.v.commit("other", "x")
        self.v.tag("triage", "prod", 2)
        self.assertEqual(self.v.names(), ["other", "triage"])
        self.assertEqual([x.number for x in self.v.history("triage")], [1, 2])
        self.assertEqual({k: x.number for k, x in self.v.tags("triage").items()}, {"prod": 2})
        with self.assertRaises(RefError):
            self.v.history("nope")


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.v = new()
        self.v.commit("t", "Classify the ticket.")
        self.v.commit("t", "Classify the ticket.\nReturn JSON.")

    def test_gate_blocks_and_the_refusal_is_recorded(self):
        self.assertFalse(self.v.deploy("t", "prod", lambda ver: False))
        with self.assertRaises(RefError):
            self.v.get("t", "prod")
        log = self.v.deploy_log()
        self.assertEqual([(e["passed"], e["reasons"]) for e in log], [(False, ["<lambda>: gate returned false"])])
        self.assertTrue(self.v.deploy("t", "prod", lambda ver: "JSON" in ver.text))
        self.assertEqual(self.v.get("t", "prod").number, 2)
        self.assertEqual([e["passed"] for e in self.v.deploy_log("t", "prod")], [False, True])
        self.assertEqual(len(self.v.deploy_log(passed=False)), 1)

    def test_no_gate_means_the_deploy_just_happens(self):
        self.assertTrue(self.v.deploy("t", "dev"))

    def test_gate_that_raises_fails_closed(self):
        def broken(ver):
            raise RuntimeError("eval service down")
        rel = self.v.release("t", "prod", broken)
        self.assertFalse(rel.ok)
        self.assertIn("RuntimeError: eval service down", rel.reasons[0])
        with self.assertRaises(RefError):
            self.v.get("t", "prod")

    def test_release_reports_previous_and_unchanged(self):
        self.v.deploy("t", "prod", ref=1)
        rel = self.v.release("t", "prod", ref=2)
        self.assertEqual((rel.ok, rel.changed, rel.previous.number, rel.version.number), (True, True, 1, 2))
        again = self.v.release("t", "prod", ref=2)
        self.assertEqual((again.ok, again.changed), (True, False))
        self.assertEqual(len(self.v.deploy_log()), 2)  # the no-op was not logged

    def test_gate_evidence_is_recorded_with_the_deploy(self):
        gate = threshold_gate(lambda ver: 0.91, 0.9, name="accuracy")
        self.assertTrue(self.v.deploy("t", "prod", gate))
        e = self.v.deploy_log()[0]
        self.assertEqual((e["gate"], e["evidence"]), ("accuracy", {"accuracy": 0.91, "minimum": 0.9}))
        self.assertFalse(self.v.deploy("t", "staging", threshold_gate(lambda ver: 0.5, 0.9, name="accuracy")))
        self.assertIn("accuracy 0.5 is below 0.9", self.v.deploy_log(tag="staging")[0]["reasons"][0])

    def test_all_of_reports_every_failure(self):
        gate = all_of(named("a", lambda ver: GateResult(False, "bad a")), named("b", lambda ver: GateResult(False, "bad b")), named("c", lambda ver: True))
        rel = self.v.release("t", "prod", gate)
        self.assertIn("a: bad a", rel.reasons[0])
        self.assertIn("b: bad b", rel.reasons[0])
        self.assertNotIn("c:", rel.reasons[0])

    def test_lint_gate(self):
        self.v.commit("t", "Contact {{ oops")
        self.assertFalse(self.v.deploy("t", "prod", lint_gate()))
        self.assertTrue(self.v.deploy("t", "prod", lint_gate(), ref=2))
        self.v.commit("t", "has trailing space \nok")
        self.assertTrue(self.v.deploy("t", "dev", lint_gate("error")))
        self.assertFalse(self.v.deploy("t", "dev2", lint_gate("info")))


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.v = new(stages=("dev", "staging", "prod"), protected=("prod",))
        self.v.commit("t", "one")
        self.v.commit("t", "two")

    def test_cannot_skip_a_stage(self):
        rel = self.v.release("t", "prod")
        self.assertFalse(rel.ok)
        self.assertIn("has not been live at staging", rel.reasons[0])
        self.assertFalse(self.v.deploy("t", "staging"))
        self.assertTrue(self.v.deploy("t", "dev"))
        self.assertTrue(self.v.deploy("t", "staging"))
        self.assertTrue(self.v.deploy("t", "prod"))

    def test_promotion_is_per_version(self):
        self.v.deploy("t", "dev", ref=1)
        self.v.deploy("t", "staging", ref=1)
        self.assertFalse(self.v.deploy("t", "prod", ref=2))  # v2 never went through staging
        self.assertTrue(self.v.deploy("t", "prod", ref=1))

    def test_having_been_live_earlier_counts_even_after_the_tag_moved(self):
        self.v.deploy("t", "dev", ref=1)
        self.v.deploy("t", "dev", ref=2)  # dev moved on to v2
        self.assertTrue(self.v.deploy("t", "staging", ref=1))

    def test_protected_tags_cannot_be_moved_by_hand(self):
        with self.assertRaises(PolicyError):
            self.v.tag("t", "prod", 1)
        self.v.tag("t", "scratch", 1)  # unprotected tags are free

    def test_policy_changes_are_logged(self):
        self.v.configure(stages=("dev", "prod"), protected=())
        self.assertEqual([e["type"] for e in self.v.events()][-1], "policy")
        self.assertTrue(self.v.deploy("t", "dev"))
        self.assertTrue(self.v.deploy("t", "prod"))

    def test_policy_survives_save_and_load(self):
        loaded = PromptVault.from_jsonl(self.v.to_jsonl())
        self.assertEqual((loaded.stages, loaded.protected), (("dev", "staging", "prod"), frozenset({"prod"})))
        with self.assertRaises(PolicyError):
            loaded.tag("t", "prod", 1)


class IntegrityTests(unittest.TestCase):
    def build(self):
        v = new(stages=("dev", "prod"))
        v.commit("t", "one", author="maria")
        v.commit("t", "two")
        v.deploy("t", "dev", ref=1)
        v.deploy("t", "prod", ref=1)
        v.tag("t", "scratch", 2)
        return v

    def test_clean_vault_verifies_and_round_trips(self):
        v = self.build()
        self.assertEqual(v.verify(), [])
        back = PromptVault.from_jsonl(v.to_jsonl())
        self.assertEqual(back.head, v.head)
        self.assertEqual({k: x.number for k, x in back.tags("t").items()}, {"dev": 1, "prod": 1, "scratch": 2})
        self.assertEqual(back.get("t", 1).author, "maria")

    def test_editing_a_prompt_in_history_is_detected(self):
        lines = self.build().to_jsonl().splitlines()
        e = json.loads(lines[1])
        e["text"] = "one but quietly changed"
        lines[1] = json.dumps(e)
        with self.assertRaises(IntegrityError):
            PromptVault.from_jsonl("\n".join(lines))

    def test_editing_a_prompt_and_its_digest_is_still_detected_by_the_chain(self):
        lines = self.build().to_jsonl().splitlines()
        e = json.loads(lines[1])
        e["text"] = "changed"
        e["digest"] = digest_of("changed")
        lines[1] = json.dumps(e)
        with self.assertRaises(IntegrityError) as cm:
            PromptVault.from_jsonl("\n".join(lines))
        self.assertIn("hash", str(cm.exception))

    def test_removing_or_reordering_events_is_detected(self):
        lines = self.build().to_jsonl().splitlines()
        with self.assertRaises(IntegrityError):
            PromptVault.from_jsonl("\n".join(lines[:1] + lines[2:]))
        with self.assertRaises(IntegrityError):
            PromptVault.from_jsonl("\n".join([lines[1], lines[0], *lines[2:]]))

    def test_truncation_needs_the_published_head(self):
        v = self.build()
        lines = v.to_jsonl().splitlines()
        truncated = "\n".join(lines[:-2])
        PromptVault.from_jsonl(truncated)  # a chain alone cannot see a missing tail
        with self.assertRaises(IntegrityError):
            PromptVault.from_jsonl(truncated, expected_head=v.head)
        PromptVault.from_jsonl("\n".join(lines), expected_head=v.head)

    def test_garbage_line(self):
        with self.assertRaises(IntegrityError):
            PromptVault.from_jsonl("not json")

    def test_a_refused_deploy_cannot_be_erased(self):
        v = new()
        v.commit("t", "x")
        v.deploy("t", "prod", lambda ver: False)
        lines = v.to_jsonl().splitlines()
        with self.assertRaises(IntegrityError):
            PromptVault.from_jsonl("\n".join(lines[:-1]), expected_head=v.head)

    def test_save_and_load_file(self):
        v = self.build()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "v.jsonl")
            v.save(path)
            self.assertEqual(os.listdir(d), ["v.jsonl"])
            self.assertEqual(PromptVault.load(path, expected_head=v.head).head, v.head)

    def test_empty_vault(self):
        self.assertEqual(PromptVault().verify(), [])
        self.assertEqual(PromptVault.from_jsonl("").names(), [])


class ReadingTests(unittest.TestCase):
    def test_render_variables_lint_blame(self):
        v = new()
        v.commit("t", "Hello {{name}}.\nBe {{tone|brief}}.")
        v.commit("t", "Hello {{name}}.\nBe {{tone|brief}}.\nAnswer in {{lang}}.")
        self.assertEqual(v.render("t", 1, name="Maria"), "Hello Maria.\nBe brief.")
        self.assertEqual(v.render("t", None, {"name": "M", "lang": "Arabic", "tone": "warm"}), "Hello M.\nBe warm.\nAnswer in Arabic.")
        self.assertEqual([(x.name, x.required) for x in v.variables("t")], [("name", True), ("tone", False), ("lang", True)])
        self.assertEqual([f.code for f in v.lint("t", declared=["name"])], ["PV004"])
        self.assertEqual([n for _, n in v.blame("t")], [1, 1, 2])
        self.assertEqual([n for _, n in v.blame("t", 1)], [1, 1])
        self.assertTrue(v.compare("t", 1, 2).breaking)


class CommandGateTests(unittest.TestCase):
    def test_exit_code_decides_and_the_prompt_is_passed_in_a_file(self):
        v = new()
        v.commit("t", "Return JSON.")
        script = "import os,sys; sys.exit(0 if 'JSON' in open(os.environ['PROMPT_VAULT_FILE']).read() and os.environ['PROMPT_VAULT_NAME']=='t' else 3)"
        self.assertTrue(v.deploy("t", "prod", command_gate(f"{sys.executable} -c \"{script}\"")))
        v.commit("t", "Return text.")
        self.assertFalse(v.deploy("t", "prod", command_gate(f"{sys.executable} -c \"{script}\"")))
        self.assertIn("exit 3", v.deploy_log()[-1]["reasons"][0])

    def test_missing_command_fails_closed(self):
        v = new()
        v.commit("t", "x")
        self.assertFalse(v.deploy("t", "prod", command_gate("definitely-not-a-real-command-xyz")))
        with self.assertRaises(ValueError):
            command_gate("  ")


if __name__ == "__main__":
    unittest.main()
