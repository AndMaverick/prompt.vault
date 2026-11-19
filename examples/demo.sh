#!/bin/sh
# A prompt's life: commit, lint, promote through stages, get refused, then someone edits history. Run from the repository root.
set -e
F=/tmp/prompt-vault-demo.jsonl
rm -f "$F"
PV="python -m prompt_vault --file $F"
$PV policy --stages dev,staging,prod --protected prod
$PV commit triage --text "Classify the ticket as billing, refund or other.
Reply with JSON only." --author maria --note "first version" --config model=small --config temperature=0
$PV commit triage --text "Classify the ticket as billing, refund or other.
Reply with JSON only.
Escalate if {{ticket}} mentions a regulator." --author maria --note "add escalation" --config model=small --config temperature=0
echo
echo "--- what changed, and does it break callers ---"
$PV diff triage 1 2
echo
echo "--- lint: the new placeholder is not supplied by the caller ---"
$PV lint triage --var language || true
echo
echo "--- straight to prod is refused, and the refusal is recorded ---"
$PV deploy triage prod --lint --by maria || true
$PV deploy triage dev --by maria
$PV deploy triage staging --by maria
$PV deploy triage prod --by maria --gate-cmd "python -c 'import sys; sys.exit(3)'" || true
$PV deploy triage prod --by maria
echo
$PV deploys triage | cut -c22-
echo
$PV ls
$PV verify | sed 's/, head.*//'
echo
echo "--- someone quietly edits v1 in the file ---"
sed -i 's/billing, refund or other/billing or other/' "$F"
$PV verify || true
