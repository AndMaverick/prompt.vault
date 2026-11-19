"""Static checks for a prompt before it ships."""
from __future__ import annotations

import math
import re
from collections.abc import Collection
from dataclasses import dataclass

from . import template

SEVERITIES = ("error", "warning", "info")

_SECRETS = (
    ("an API key (sk- prefix)", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("an AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("a GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("a Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("a credential assignment", re.compile(r"(?i)\b(?:api[_-]?key|secret|token|password)\b\s*[:=]\s*[\"']?[A-Za-z0-9/+_\-]{16,}")),
)
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    message: str
    line: int | None = None

    def __str__(self) -> str:
        where = f"line {self.line}: " if self.line else ""
        return f"{self.code} {self.severity}: {where}{self.message}"


def estimate_tokens(text: str) -> int:
    """A rough token count with no tokenizer: about 4 characters per token for ASCII and 2 for everything else. Good to
    within a factor of about 1.5, enough to catch a prompt that is ten times too long and no more."""
    ascii_chars = sum(c.isascii() for c in text)
    return math.ceil(ascii_chars / 4 + (len(text) - ascii_chars) / 2)


def lint(text: str, declared: Collection[str] | None = None, max_tokens: int = 8000) -> list[Finding]:
    """Check a prompt. ``declared`` is the list of variables the calling code supplies: with it, placeholders nobody fills and
    variables nobody uses are reported."""
    out: list[Finding] = []
    if not text.strip():
        return [Finding("PV001", "error", "the prompt is empty")]
    for line in template.malformed(text):
        out.append(Finding("PV002", "error", "a '{{' that is not a valid placeholder (it would be sent as literal text)", line))
    for lineno, line_text in enumerate(text.splitlines(), 1):
        for what, pattern in _SECRETS:
            if pattern.search(line_text):
                out.append(Finding("PV003", "error", f"looks like {what}: a prompt is stored in history and logs, so never put a secret in one", lineno))
                break
        if _EMAIL.search(line_text):
            out.append(Finding("PV010", "warning", "contains an email address", lineno))
        if line_text != line_text.rstrip():
            out.append(Finding("PV006", "info", "trailing whitespace", lineno))
    used = template.variables(text)
    if declared is not None:
        known = set(declared)
        for v in used:
            if v.required and v.name.split(".")[0] not in known:
                out.append(Finding("PV004", "error", f"placeholder {{{{{v.name}}}}} is not supplied by the calling code"))
        used_roots = {v.name.split(".")[0] for v in used}
        for name in sorted(known - used_roots):
            out.append(Finding("PV005", "warning", f"variable {name!r} is supplied but the prompt never uses it"))
    lowered: dict[str, str] = {}
    for v in used:
        key = v.name.lower()
        if key in lowered and lowered[key] != v.name:
            out.append(Finding("PV008", "warning", f"variables {lowered[key]!r} and {v.name!r} differ only by case"))
        lowered.setdefault(key, v.name)
    tokens = estimate_tokens(text)
    if tokens > max_tokens:
        out.append(Finding("PV007", "warning", f"about {tokens} tokens (estimate), over the {max_tokens} limit"))
    if "\r\n" in text and re.search(r"(?<!\r)\n", text):
        out.append(Finding("PV009", "warning", "mixed line endings"))
    return sorted(out, key=lambda f: (SEVERITIES.index(f.severity), f.line or 0, f.code))
