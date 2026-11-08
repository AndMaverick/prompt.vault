# prompt.vault

Version control for prompts, built for the way prompts actually change: small edits that quietly change behavior. Content-addressed versions, tags, diffs, rollback that never rewrites history, and deploys that only move a tag when a gate passes. Standard library only.

## Use

```python
from prompt_vault import PromptVault

vault = PromptVault()
vault.commit("triage", "Classify the ticket.")
vault.commit("triage", "Classify the ticket.\nReturn JSON.")

print(vault.diff("triage", 1, 2))
vault.deploy("triage", "prod", gate=lambda v: "JSON" in v.text)   # True, tag moved
vault.rollback("triage", 1)                                       # new version 3, history intact
```

## Design

- **Content addressed.** Each version carries a short SHA-256 digest. Committing identical text is a no-op, so history never fills with duplicates.
- **Flexible refs.** Look a version up by number, by tag, or by digest prefix. Ambiguous or unknown refs raise `RefError`.
- **Append only.** `rollback` re-commits old text as the newest version. Nothing is rewritten, so the audit trail stays honest.
- **Gated deploys.** `deploy` moves a tag only if your gate returns true. Wire the gate to an eval run, for example [eval.forge](https://github.com/AndMaverick/eval.forge), and a prompt cannot reach prod without passing.

## Tests

```
python -m unittest discover -s tests -t . -v
```

MIT licensed. Built by [AndMaverick LLC](https://www.andmaverick.com).
