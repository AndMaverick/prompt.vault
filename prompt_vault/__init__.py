"""prompt.vault: version control for prompts, with an audit trail you can verify.

Content-addressed versions (text plus the model settings that change behaviour), tags, a promotion path, deploys that move a
tag only when a gate passes and that record every refusal, templates with strict rendering, a linter, line-level blame, and an
append-only hash-chained log so history cannot be rewritten without it showing.
"""
from __future__ import annotations

from . import gates
from .diffing import Change, blame, compare
from .gates import GateResult, all_of, command_gate, lint_gate, named, threshold_gate
from .lint import Finding, estimate_tokens, lint
from .template import MissingVariable, TemplateError, UnknownVariable, Variable, render, variables
from .vault import GENESIS, IntegrityError, PolicyError, PromptVault, RefError, Release, Version, digest_of

__version__ = "0.2.0"

__all__ = [
    "PromptVault", "Version", "Release", "RefError", "PolicyError", "IntegrityError", "GENESIS", "digest_of",
    "Change", "compare", "blame", "Finding", "lint", "estimate_tokens", "Variable", "variables", "render",
    "TemplateError", "MissingVariable", "UnknownVariable", "GateResult", "gates", "lint_gate", "threshold_gate", "all_of",
    "command_gate", "named", "__version__",
]
