"""prompt.vault: content-addressed prompt versioning with tags, diffs, rollback, and gated deploys."""

from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass

__version__ = "0.1.0"


class RefError(LookupError):
    """A version reference matched nothing, or matched ambiguously."""


@dataclass(frozen=True)
class Version:
    name: str
    number: int
    digest: str
    text: str
    note: str = ""


class PromptVault:
    def __init__(self) -> None:
        self._versions: dict = {}
        self._tags: dict = {}

    def commit(self, name: str, text: str, note: str = "") -> Version:
        """Store a new version. Committing identical text again is a no-op."""
        history = self._versions.setdefault(name, [])
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        if history and history[-1].digest == digest:
            return history[-1]
        version = Version(name, len(history) + 1, digest, text, note)
        history.append(version)
        return version

    def get(self, name: str, ref=None) -> Version:
        history = self._versions.get(name)
        if not history:
            raise RefError(f"unknown prompt: {name}")
        if ref is None:
            return history[-1]
        if isinstance(ref, int):
            if 1 <= ref <= len(history):
                return history[ref - 1]
            raise RefError(f"{name} has no version {ref}")
        if (name, ref) in self._tags:
            return self._tags[(name, ref)]
        matches = [v for v in history if v.digest.startswith(ref)]
        if len(matches) == 1:
            return matches[0]
        raise RefError(f"ref '{ref}' matched {len(matches)} versions of {name}")

    def tag(self, name: str, tag: str, ref=None) -> Version:
        version = self.get(name, ref)
        self._tags[(name, tag)] = version
        return version

    def diff(self, name: str, a, b) -> str:
        left, right = self.get(name, a), self.get(name, b)
        lines = difflib.unified_diff(
            left.text.splitlines(), right.text.splitlines(),
            fromfile=f"{name}@v{left.number}", tofile=f"{name}@v{right.number}", lineterm="")
        return "\n".join(lines)

    def rollback(self, name: str, ref) -> Version:
        """Re-commit an old version as the newest one. History is never rewritten."""
        old = self.get(name, ref)
        return self.commit(name, old.text, note=f"rollback to v{old.number}")

    def deploy(self, name: str, tag: str, gate, ref=None) -> bool:
        """Move a tag only if gate(version) passes."""
        version = self.get(name, ref)
        if not gate(version):
            return False
        self._tags[(name, tag)] = version
        return True
