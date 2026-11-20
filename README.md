# prompt.vault

Version control for prompts, with an audit trail you can verify.

A prompt change is a production change that looks like an edit to a string. It can break callers (a new placeholder nobody fills), change behaviour in ways no test notices, or reach production without passing anything. And when something goes wrong, the history that should explain it is a file anyone with access can quietly rewrite. prompt.vault treats prompts the way a release process treats code: every version is content addressed, a version reaches production only through a promotion path and a gate, every refusal is recorded, and the whole history is an append only hash chain, so changing the past shows. Standard library only.

```
pip install git+https://github.com/AndMaverick/prompt.vault
```

## What it does

```python
from prompt_vault import PromptVault, lint_gate, threshold_gate, all_of

vault = PromptVault(stages=("dev", "staging", "prod"), protected=("prod",))
vault.commit("triage", "Classify the ticket.\nReturn JSON for {{ticket}}.", author="maria",
             config={"model": "small", "temperature": 0})

vault.compare("triage", 1, 2).kind               # after a second commit: identical | config | whitespace | edit | breaking
vault.render("triage", ticket="refund not received")   # strict: missing or misspelt variables raise
vault.deploy("triage", "dev")
vault.deploy("triage", "staging")                 # the promotion path: prod is refused until staging has had it
vault.deploy("triage", "prod", all_of(lint_gate(), threshold_gate(run_eval, 0.9, "accuracy")), by="maria")
vault.verify()                                    # [] unless history was altered
```

- **Versions are text plus the settings that change behaviour.** The model, temperature and tools are part of the version and its digest, so changing only the temperature is a new version and shows as a `config` change. Plain text prompts keep the digest they had in 0.1.
- **Changes are classified.** `compare` says whether an edit is identical, config only, whitespace only, an ordinary edit, or **breaking**: the set of required `{{variables}}` changed, the one kind of prompt edit that breaks callers rather than changing behaviour. `blame` credits every line to the version that wrote it.
- **A promotion path.** With `stages=("dev", "staging", "prod")` a version reaches a stage only after it has been live at the one before, per version. `protected` tags cannot be moved by hand, only by a deploy, so there is no side door.
- **Gates that fail closed.** A deploy moves a tag only if its gate passes. A gate that raises, times out or cannot run counts as a failure. Built in: `lint_gate`, `threshold_gate` (score at least a minimum, with the evidence recorded), `all_of`, and `command_gate`, which runs any command and passes on exit 0, so any eval tool can gate a release, for example `eval-forge compare baseline.json candidate.json --strict` from [eval.forge](https://github.com/AndMaverick/eval.forge).
- **Every attempt is evidence.** `deploy_log` lists every deploy, passed or refused, with who, when, the gate, the reasons and its evidence. A refusal is as much part of the record as a success.
- **Strict templates.** `{{name}}`, `{{name|default}}`, dotted names for nested data, `\{{` for a literal. A missing variable, or a value the prompt never uses (usually a typo), raises instead of producing a prompt with a hole in it.
- **A linter.** Empty prompts, malformed placeholders that would ship as literal text, secrets (API keys, cloud and GitHub tokens, private keys, credential assignments, never echoed back), placeholders the calling code does not supply, variables nobody uses, case only duplicates, emails, trailing whitespace, mixed line endings, and a prompt far over a length budget.
- **Tamper evident.** The log is JSON Lines, each event hashed with the one before it. Editing, reordering or deleting an event breaks the chain and the vault refuses to load. A chain alone cannot show that the *last* events were cut off, so publish `vault.head` (the newest hash) somewhere you control and load with `expected_head` to catch that too.

## Command line

```
stages: dev, staging, prod; protected: prod
triage v1 91bedfaf224b
triage v2 c827722fdb9b

--- what changed, and does it break callers ---
--- triage@v1
+++ triage@v2
@@ -1,2 +1,3 @@
 Classify the ticket as billing, refund or other.
 Reply with JSON only.
+Escalate if {{ticket}} mentions a regulator.

breaking: +1 -0 lines, 80% of words unchanged; callers must now supply ticket

--- lint: the new placeholder is not supplied by the caller ---
PV004 error: placeholder {{ticket}} is not supplied by the calling code
PV005 warning: variable 'language' is supplied but the prompt never uses it

--- straight to prod is refused, and the refusal is recorded ---
refused: v2 has not been live at staging; promote it there first
dev -> triage v2
staging -> triage v2
refused: command:python: exit 3
prod -> triage v2

triage prod v2  REFUSED  v2 has not been live at staging; promote it there first
triage dev v2  moved
triage staging v2  moved
triage prod v2  REFUSED  command:python: exit 3
triage prod v2  moved

triage                  v2   c827722fdb9b  dev=v2, prod=v2, staging=v2
ok: 8 events

--- someone quietly edits v1 in the file ---
integrity: event 2: content does not match its hash; event 2: triage v1 text does not match its digest; event 3: content does not match its hash
```

`examples/demo.sh` reproduces this. The vault lives in `prompts.vault.jsonl` (or `--file`, or `PROMPT_VAULT_FILE`). Commands: `commit`, `ls`, `log`, `show`, `diff`, `tag`, `deploy` (`--lint`, `--gate-cmd`), `rollback`, `blame`, `lint`, `render`, `vars`, `policy`, `deploys`, `verify` (`--head`), `head`. Exit codes: `0` fine, `1` refused or findings, `2` error.

## Honest limits

- **It proves history was not altered, not that history was wise.** The chain shows the log is exactly what was written. It does not stop someone with write access from rebuilding the whole file with a fresh chain, which is why `head` is meant to be published or signed outside the file.
- **The gate is only as good as the check behind it.** A `threshold_gate` over a thin eval suite passes thin prompts. Use a gate that knows how much a score can be trusted, such as eval.forge's noninferiority gate.
- **The linter is static.** It sees text, not behaviour. It will not catch a prompt that is well formed and wrong. The token count is an estimate (about 4 characters per token for ASCII, 2 otherwise), good to a factor of about 1.5.
- **Breaking means callers' variables.** It does not mean the model's behaviour changed. That is what evals are for.
- **One file, one writer.** Commands read the file, change it and rewrite it atomically. Two people deploying at the same moment need a lock outside this library.

## Tests

```
python -m unittest discover -s tests -t . -v
```

78 tests, including tampering in several forms (edited text, edited text with a recomputed digest, removed and reordered events, truncation) and gates that crash. Ruff and mypy strict run in CI on Python 3.10 to 3.13.

**Related repositories:** [eval.forge](https://github.com/AndMaverick/eval.forge) supplies the check a promotion gate should rest on. [mao-framework](https://github.com/AndMaverick/mao-framework) has a smaller module, `mao.vault`, that versions prompts behind gated deploys inside a governed run.

MIT licensed. Built by [AndMaverick LLC](https://www.andmaverick.com).

---

**If this is your problem.** If a system you run can fail in ways that matter, the controls belong in the architecture, not in the postmortem. I am happy to talk through yours. [Begin a conversation](https://calendly.com/andmaverick?utm_source=github&utm_medium=repo&utm_campaign=prompt.vault).
