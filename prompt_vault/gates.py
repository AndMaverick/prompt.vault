"""Gates decide whether a version may move to a tag. A gate that raises is a gate that failed."""
from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .lint import SEVERITIES, lint

if TYPE_CHECKING:
    from .vault import Version


@dataclass(frozen=True)
class GateResult:
    ok: bool
    reason: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)  # recorded with the deploy, e.g. the score and the suite digest

    def __bool__(self) -> bool:
        return self.ok


Gate = Callable[["Version"], "bool | GateResult"]


def run_gate(gate: Gate, version: Version) -> GateResult:
    """Run any gate and normalise the answer. A crash is a failure, so a broken gate can never wave a prompt through."""
    try:
        out = gate(version)
    except Exception as exc:
        return GateResult(False, f"gate raised {type(exc).__name__}: {exc}")
    if isinstance(out, GateResult):
        return out
    return GateResult(bool(out), "" if out else "gate returned false")


def gate_name(gate: Gate) -> str:
    return str(getattr(gate, "name", None) or getattr(gate, "__name__", None) or type(gate).__name__)


def named(name: str, gate: Gate) -> Gate:
    def wrapper(version: Version) -> bool | GateResult:
        return gate(version)

    wrapper.name = name  # type: ignore[attr-defined]
    return wrapper


def lint_gate(fail_on: str = "error", declared: list[str] | None = None) -> Gate:
    """Pass only if the prompt has no findings at ``fail_on`` severity or worse."""
    limit = SEVERITIES.index(fail_on)

    def gate(version: Version) -> GateResult:
        bad = [f for f in lint(version.text, declared) if SEVERITIES.index(f.severity) <= limit]
        return GateResult(not bad, "; ".join(str(f) for f in bad[:3]), {"findings": len(bad)})

    gate.name = "lint"  # type: ignore[attr-defined]
    return gate


def threshold_gate(score: Callable[[Version], float], minimum: float, name: str = "score") -> Gate:
    """Pass if ``score(version)`` is at least ``minimum``. Wire ``score`` to an eval run on the version's rendered text."""

    def gate(version: Version) -> GateResult:
        value = float(score(version))
        return GateResult(value >= minimum, f"{name} {value:.4g} is below {minimum:.4g}" if value < minimum else "", {name: value, "minimum": minimum})

    gate.name = name  # type: ignore[attr-defined]
    return gate


def all_of(*gates: Gate) -> Gate:
    """Pass only if every gate passes. Every gate runs, so the record shows everything that failed, not only the first."""

    def gate(version: Version) -> GateResult:
        results = [(gate_name(g), run_gate(g, version)) for g in gates]
        failed = [f"{n}: {r.reason}" for n, r in results if not r.ok]
        return GateResult(not failed, "; ".join(failed), {n: dict(r.evidence) for n, r in results})

    gate.name = "all_of"  # type: ignore[attr-defined]
    return gate


def command_gate(command: str, timeout: float = 600, env: Mapping[str, str] | None = None) -> Gate:
    """Run a command, with the prompt text in a temp file. Exit 0 passes. This is how to gate on any eval tool, for example
    ``eval-forge compare baseline.json candidate.json --strict``. The command sees PROMPT_VAULT_NAME, PROMPT_VAULT_VERSION,
    PROMPT_VAULT_DIGEST and PROMPT_VAULT_FILE."""
    argv = shlex.split(command)
    if not argv:
        raise ValueError("empty command")

    def gate(version: Version) -> GateResult:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "prompt.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(version.text)
            environment = {**os.environ, **(env or {}), "PROMPT_VAULT_NAME": version.name, "PROMPT_VAULT_VERSION": str(version.number),
                           "PROMPT_VAULT_DIGEST": version.digest, "PROMPT_VAULT_FILE": path}
            try:
                done = subprocess.run(argv, env=environment, capture_output=True, text=True, timeout=timeout, check=False)
            except (OSError, subprocess.TimeoutExpired) as exc:
                return GateResult(False, f"could not run {argv[0]}: {exc}")
        tail = (done.stdout.strip().splitlines() or done.stderr.strip().splitlines() or [""])[-1][:200]
        reason = "" if done.returncode == 0 else f"exit {done.returncode}" + (f": {tail}" if tail else "")
        return GateResult(done.returncode == 0, reason, {"exit_code": done.returncode})

    gate.name = "command:" + os.path.basename(argv[0])  # type: ignore[attr-defined]
    return gate
