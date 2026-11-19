"""Prompt templates: ``{{name}}`` placeholders, ``{{name|default}}`` for optional ones, dotted names for nested data, ``\\{{`` for a literal.

There is deliberately no logic here. A prompt that needs conditionals or loops should build its text in code and commit the
result, so the thing being versioned is the thing the model sees.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_NAME = r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
PLACEHOLDER = re.compile(rf"(?<!\\)\{{\{{\s*({_NAME})\s*(?:\|([^{{}}]*))?\}}\}}")
_OPENER = re.compile(r"(?<!\\)\{\{")


class TemplateError(ValueError):
    """Base class for rendering problems."""


class MissingVariable(TemplateError):
    def __init__(self, names: list[str]) -> None:
        super().__init__("missing variable" + ("s" if len(names) > 1 else "") + ": " + ", ".join(names))
        self.names = names


class UnknownVariable(TemplateError):
    def __init__(self, names: list[str]) -> None:
        super().__init__("not used by this prompt: " + ", ".join(names))
        self.names = names


@dataclass(frozen=True)
class Variable:
    name: str
    default: str | None = None

    @property
    def required(self) -> bool:
        return self.default is None


def variables(text: str) -> list[Variable]:
    """The placeholders in ``text``, in order of first appearance. A variable is optional only if every use gives a default."""
    found: dict[str, str | None] = {}
    for m in PLACEHOLDER.finditer(text):
        name = m.group(1)
        default = m.group(2).strip() if m.group(2) is not None else None
        if name not in found:
            found[name] = default
        elif default is None:
            found[name] = None
    return [Variable(n, d) for n, d in found.items()]


def malformed(text: str) -> list[int]:
    """Line numbers of an opening ``{{`` that is not part of a valid placeholder (a typo that would ship as literal text)."""
    valid = {m.start() for m in PLACEHOLDER.finditer(text)}
    out = []
    for m in _OPENER.finditer(text):
        if m.start() not in valid:
            out.append(text.count("\n", 0, m.start()) + 1)
    return out


def _lookup(values: Mapping[str, Any], dotted: str) -> tuple[bool, Any]:
    cur: Any = values
    for part in dotted.split("."):
        if isinstance(cur, Mapping) and part in cur:
            cur = cur[part]
        else:
            return False, None
    return True, cur


def render(text: str, values: Mapping[str, Any], strict: bool = True) -> str:
    """Fill the placeholders. With ``strict`` (the default) a missing required variable, or a supplied value the prompt never
    uses, raises: a misspelt variable should fail in a test, not quietly produce a prompt with a hole in it."""
    missing: list[str] = []

    def fill(m: re.Match[str]) -> str:
        name, default = m.group(1), m.group(2)
        ok, value = _lookup(values, name)
        if ok and value is None:
            raise TemplateError(f"variable {name!r} is None")
        if ok:
            return str(value)
        if default is not None:
            return default.strip()
        missing.append(name)
        return m.group(0)

    out = PLACEHOLDER.sub(fill, text)
    if missing and strict:
        raise MissingVariable(sorted(set(missing)))
    if strict:
        used = {v.name.split(".")[0] for v in variables(text)}
        extra = sorted(set(values) - used)
        if extra:
            raise UnknownVariable(extra)
    return out.replace("\\{{", "{{")
