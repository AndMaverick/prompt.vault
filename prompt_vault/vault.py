"""The vault: an append-only, hash-chained log of everything that happened to every prompt."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import diffing, template
from .gates import Gate, GateResult, gate_name, run_gate
from .lint import Finding
from .lint import lint as run_lint

GENESIS = "0" * 64
FORMAT = 1


class RefError(LookupError):
    """A version reference matched nothing, or matched ambiguously."""


class PolicyError(PermissionError):
    """An operation the vault's policy forbids, such as moving a protected tag by hand."""


class IntegrityError(ValueError):
    """The log has been altered, truncated or reordered since it was written."""


@dataclass(frozen=True)
class Version:
    name: str
    number: int
    digest: str
    text: str
    note: str = ""
    config: dict[str, Any] = field(default_factory=dict)  # model, temperature, tools: anything that changes behaviour with the text
    author: str = field(default="", compare=False)
    at: str = field(default="", compare=False)


@dataclass(frozen=True)
class Release:
    """What happened when a deploy was attempted."""

    ok: bool
    version: Version
    previous: Version | None  # what the tag pointed at before
    changed: bool  # False when the tag already pointed here
    reasons: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.ok


def digest_of(text: str, config: Mapping[str, Any] | None = None) -> str:
    """Short content digest. Plain text hashes as the text alone, so adding config support changed no existing digest."""
    if not config:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    payload = json.dumps({"text": text, "config": config}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _chain(prev: str, payload: Mapping[str, Any]) -> str:
    return hashlib.sha256((prev + _canonical(payload)).encode("utf-8")).hexdigest()


def _jsonable(obj: Any) -> Any:
    return json.loads(json.dumps(obj, default=str))


class PromptVault:
    def __init__(self, stages: Iterable[str] = (), protected: Iterable[str] = (), clock: Callable[[], datetime] | None = None) -> None:
        """``stages`` is the promotion path, for example ``("dev", "staging", "prod")``: a version can reach a stage only after it
        has been live at the one before. ``protected`` tags cannot be moved by hand, only by a deploy that passes its gate."""
        self._versions: dict[str, list[Version]] = {}
        self._tags: dict[tuple[str, str], Version] = {}
        self._events: list[dict[str, Any]] = []
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.stages: tuple[str, ...] = ()
        self.protected: frozenset[str] = frozenset()
        stages, protected = tuple(stages), tuple(protected)
        if stages or protected:
            self.configure(stages, protected)

    # ------------------------------------------------------------------------------------------------------- the log
    @property
    def head(self) -> str:
        """The hash of the newest event. Publish or sign it somewhere you control: any later change to history changes it."""
        return self._events[-1]["hash"] if self._events else GENESIS

    def _record(self, event: dict[str, Any]) -> dict[str, Any]:
        event = {**event, "seq": len(self._events) + 1, "at": self._clock().isoformat()}
        event["prev"] = self.head
        event["hash"] = _chain(event["prev"], {k: v for k, v in event.items() if k not in ("prev", "hash")})
        self._apply(event)
        self._events.append(event)
        return event

    def _apply(self, e: Mapping[str, Any]) -> None:
        kind = e["type"]
        if kind == "policy":
            self.stages, self.protected = tuple(e["stages"]), frozenset(e["protected"])
        elif kind == "commit":
            history = self._versions.setdefault(e["name"], [])
            if e["number"] != len(history) + 1:
                raise IntegrityError(f"{e['name']}: version {e['number']} does not follow version {len(history)}")
            history.append(Version(e["name"], e["number"], e["digest"], e["text"], e.get("note", ""), dict(e.get("config", {})), e.get("author", ""), e["at"]))
        elif kind in ("tag", "deploy") and (kind == "tag" or e["passed"]):
            history = self._versions.get(e["name"], [])
            if not 1 <= e["number"] <= len(history):
                raise IntegrityError(f"{e['name']}: no version {e['number']} to tag")
            self._tags[(e["name"], e["tag"])] = history[e["number"] - 1]

    def configure(self, stages: Iterable[str] = (), protected: Iterable[str] = ()) -> None:
        """Set the promotion path and protected tags. The change is itself recorded, so weakening a policy leaves a trace."""
        self._record({"type": "policy", "stages": list(stages), "protected": sorted(protected)})

    # ------------------------------------------------------------------------------------------------------ commits
    def commit(self, name: str, text: str, note: str = "", *, config: Mapping[str, Any] | None = None, author: str = "") -> Version:
        """Store a new version. Committing the same text and config as the newest version again is a no-op."""
        config = _jsonable(dict(config)) if config else {}
        history = self._versions.get(name, [])
        digest = digest_of(text, config)
        if history and history[-1].digest == digest:
            return history[-1]
        self._record({"type": "commit", "name": name, "number": len(history) + 1, "digest": digest, "text": text, "note": note,
                      "config": config, "author": author})
        return self._versions[name][-1]

    def get(self, name: str, ref: int | str | None = None) -> Version:
        """A version by number (``2`` or ``"v2"``), by tag, or by digest prefix. No ``ref`` means the newest."""
        history = self._versions.get(name)
        if not history:
            raise RefError(f"unknown prompt: {name}")
        if ref is None:
            return history[-1]
        if isinstance(ref, bool):
            raise RefError("a version reference must be a number, tag or digest")
        if isinstance(ref, int):
            if 1 <= ref <= len(history):
                return history[ref - 1]
            raise RefError(f"{name} has no version {ref}")
        if (name, ref) in self._tags:
            return self._tags[(name, ref)]
        m = re.fullmatch(r"v(\d+)", ref)
        if m:
            return self.get(name, int(m.group(1)))
        if re.fullmatch(r"[0-9a-f]+", ref):
            matches = [v for v in history if v.digest.startswith(ref)]
            if len({v.digest for v in matches}) == 1:
                return matches[-1]  # identical content committed again after a rollback: the newest copy
            raise RefError(f"ref '{ref}' matched {len({v.digest for v in matches})} different versions of {name}")
        raise RefError(f"ref '{ref}' matched nothing in {name}")

    def history(self, name: str) -> list[Version]:
        if name not in self._versions:
            raise RefError(f"unknown prompt: {name}")
        return list(self._versions[name])

    def names(self) -> list[str]:
        return sorted(self._versions)

    def tags(self, name: str) -> dict[str, Version]:
        return {tag: v for (n, tag), v in sorted(self._tags.items()) if n == name}

    # --------------------------------------------------------------------------------------------- tags and releases
    def tag(self, name: str, tag: str, ref: int | str | None = None, by: str = "") -> Version:
        """Point a tag at a version. Protected tags refuse this: they move only through :meth:`deploy`."""
        if tag in self.protected:
            raise PolicyError(f"{tag!r} is protected: use deploy() so the gate runs")
        version = self.get(name, ref)
        before = self._tags.get((name, tag))
        self._record({"type": "tag", "name": name, "tag": tag, "number": version.number, "from": before.number if before else None, "by": by})
        return version

    def _was_live(self, name: str, tag: str, number: int) -> bool:
        return any(e["name"] == name and e["tag"] == tag and e["number"] == number and (e["type"] == "tag" or e.get("passed"))
                   for e in self._events if e["type"] in ("tag", "deploy"))

    def release(self, name: str, tag: str, gate: Gate | None = None, ref: int | str | None = None, by: str = "") -> Release:
        """Try to move ``tag`` to a version. Passes only if the promotion path allows it and the gate passes, and either way the
        attempt is recorded, including why it was refused. A gate that raises counts as a failure."""
        version = self.get(name, ref)
        previous = self._tags.get((name, tag))
        if previous == version:
            return Release(True, version, previous, False)
        reasons: list[str] = []
        evidence: Mapping[str, Any] = {}
        if tag in self.stages and self.stages.index(tag) > 0:
            earlier = self.stages[self.stages.index(tag) - 1]
            if not self._was_live(name, earlier, version.number):
                reasons.append(f"v{version.number} has not been live at {earlier}; promote it there first")
        label = ""
        if gate is not None:
            label = gate_name(gate)
            result: GateResult = run_gate(gate, version)
            evidence = result.evidence
            if not result.ok:
                prefix = "" if label == "all_of" else f"{label}: "  # all_of's reason already names each gate that failed
                reasons.append(f"{prefix}{result.reason}" if result.reason else f"{label} failed")
        ok = not reasons
        self._record({"type": "deploy", "name": name, "tag": tag, "number": version.number, "from": previous.number if previous else None,
                      "passed": ok, "gate": label, "reasons": reasons, "evidence": _jsonable(dict(evidence)), "by": by})
        return Release(ok, version, previous, ok, tuple(reasons), evidence)

    def deploy(self, name: str, tag: str, gate: Gate | None = None, ref: int | str | None = None, by: str = "") -> bool:
        """Move a tag only if the promotion path allows it and ``gate(version)`` passes. Returns whether it moved."""
        return self.release(name, tag, gate, ref, by).ok

    def rollback(self, name: str, ref: int | str, by: str = "") -> Version:
        """Re-commit an old version as the newest one. History is never rewritten."""
        old = self.get(name, ref)
        return self.commit(name, old.text, note=f"rollback to v{old.number}", config=old.config, author=by)

    def deploy_log(self, name: str | None = None, tag: str | None = None, passed: bool | None = None) -> list[dict[str, Any]]:
        """Every deploy attempt, passed or refused, oldest first."""
        return [dict(e) for e in self._events if e["type"] == "deploy" and (name is None or e["name"] == name)
                and (tag is None or e["tag"] == tag) and (passed is None or e["passed"] == passed)]

    @property
    def size(self) -> int:
        """The number of events in the log."""
        return len(self._events)

    def events(self) -> list[dict[str, Any]]:
        return [dict(e) for e in self._events]

    # ------------------------------------------------------------------------------------------- reading a version
    def diff(self, name: str, a: int | str | None, b: int | str | None) -> str:
        left, right = self.get(name, a), self.get(name, b)
        lines = difflib.unified_diff(left.text.splitlines(), right.text.splitlines(), fromfile=f"{name}@v{left.number}",
                                     tofile=f"{name}@v{right.number}", lineterm="")
        return "\n".join(lines)

    def compare(self, name: str, a: int | str | None, b: int | str | None) -> diffing.Change:
        """What kind of change this is: identical, config, whitespace, edit, or breaking (the variables callers must supply changed)."""
        left, right = self.get(name, a), self.get(name, b)
        return diffing.compare(left.text, right.text, left.config, right.config)

    def blame(self, name: str, ref: int | str | None = None) -> list[tuple[str, int]]:
        """Each line of a version with the version number that wrote it."""
        version = self.get(name, ref)
        return diffing.blame([v.text for v in self._versions[name][: version.number]])

    def variables(self, name: str, ref: int | str | None = None) -> list[template.Variable]:
        return template.variables(self.get(name, ref).text)

    def render(self, name: str, ref: int | str | None = None, values: Mapping[str, Any] | None = None, /, *, strict: bool = True, **kwargs: Any) -> str:
        """Render a version. Pass variables as keywords or as a ``values`` mapping. The first three arguments are positional-only, so a
        variable called ``name`` or ``values`` works as a keyword."""
        merged = {**(values or {}), **kwargs}
        return template.render(self.get(name, ref).text, merged, strict)

    def lint(self, name: str, ref: int | str | None = None, declared: Collection[str] | None = None, max_tokens: int = 8000) -> list[Finding]:
        return run_lint(self.get(name, ref).text, declared, max_tokens)

    # ---------------------------------------------------------------------------------------------------- integrity
    def verify(self) -> list[str]:
        """Re-derive the whole chain. An empty list means every event is intact and in order."""
        problems: list[str] = []
        prev = GENESIS
        for n, e in enumerate(self._events, 1):
            if e.get("seq") != n:
                problems.append(f"event {n}: out of sequence (says {e.get('seq')})")
            if e.get("prev") != prev:
                problems.append(f"event {n}: does not follow the event before it")
            if _chain(prev, {k: v for k, v in e.items() if k not in ("prev", "hash")}) != e.get("hash"):
                problems.append(f"event {n}: content does not match its hash")
            if e["type"] == "commit" and digest_of(e["text"], e.get("config")) != e["digest"]:
                problems.append(f"event {n}: {e['name']} v{e['number']} text does not match its digest")
            prev = e.get("hash", prev)
        return problems

    def to_jsonl(self) -> str:
        return "".join(json.dumps(e, sort_keys=True, ensure_ascii=False) + "\n" for e in self._events)

    @classmethod
    def from_jsonl(cls, text: str, clock: Callable[[], datetime] | None = None, expected_head: str | None = None) -> PromptVault:
        """Rebuild a vault from its log, refusing one that does not verify. Pass ``expected_head`` (the hash you published) to
        also catch a log that was truncated, which a chain alone cannot show."""
        vault = cls(clock=clock)
        for n, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError:
                raise IntegrityError(f"line {n} is not valid JSON") from None
            vault._events.append(event)
        problems = vault.verify()
        if problems:
            raise IntegrityError("; ".join(problems[:3]))
        events, vault._events = vault._events, []
        for event in events:
            vault._apply(event)
            vault._events.append(event)
        if expected_head is not None and vault.head != expected_head:
            raise IntegrityError("the log's latest event does not match the expected head: events were removed or replaced")
        return vault

    def save(self, path: str) -> None:
        """Write the log atomically, so a crash never leaves half a file."""
        directory = os.path.dirname(os.path.abspath(path))
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".vault-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(self.to_jsonl())
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    @classmethod
    def load(cls, path: str, clock: Callable[[], datetime] | None = None, expected_head: str | None = None) -> PromptVault:
        with open(path, encoding="utf-8") as fh:
            return cls.from_jsonl(fh.read(), clock, expected_head)
