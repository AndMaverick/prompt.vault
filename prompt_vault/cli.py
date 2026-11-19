"""``python -m prompt_vault``: version, lint, gate and deploy prompts from a terminal or a pipeline."""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from . import gates
from .lint import SEVERITIES
from .template import TemplateError
from .vault import IntegrityError, PolicyError, PromptVault, RefError


def _config(pairs: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep or not key:
            raise ValueError(f"--config expects key=value, got {pair!r}")
        try:
            out[key] = json.loads(raw)
        except ValueError:
            out[key] = raw
    return out


def _values(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep or not key:
            raise ValueError(f"--var expects name=value, got {pair!r}")
        out[key] = raw
    return out


def _ref(text: str | None) -> int | str | None:
    return int(text) if text and text.isdigit() else text


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="prompt_vault", description="Version control for prompts, with an audit trail you can verify.")
    ap.add_argument("--file", default=os.environ.get("PROMPT_VAULT_FILE", "prompts.vault.jsonl"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("commit")
    c.add_argument("name")
    src = c.add_mutually_exclusive_group(required=True)
    src.add_argument("--text")
    src.add_argument("--from", dest="path", help="read the prompt from a file, or - for stdin")
    c.add_argument("--note", default="")
    c.add_argument("--author", default="")
    c.add_argument("--config", action="append", default=[], help="key=value, repeatable (model, temperature, ...)")
    sub.add_parser("ls")
    lg = sub.add_parser("log")
    lg.add_argument("name")
    sh = sub.add_parser("show")
    sh.add_argument("name")
    sh.add_argument("ref", nargs="?")
    df = sub.add_parser("diff")
    df.add_argument("name")
    df.add_argument("a")
    df.add_argument("b")
    tg = sub.add_parser("tag")
    tg.add_argument("name")
    tg.add_argument("tag")
    tg.add_argument("--ref")
    tg.add_argument("--by", default="")
    dp = sub.add_parser("deploy", help="move a tag if the gates pass; exit 1 if refused")
    dp.add_argument("name")
    dp.add_argument("tag")
    dp.add_argument("--ref")
    dp.add_argument("--by", default="")
    dp.add_argument("--lint", action="store_true", help="gate on no lint errors")
    dp.add_argument("--gate-cmd", action="append", default=[], help="a command that must exit 0, e.g. an eval run (repeatable)")
    rb = sub.add_parser("rollback")
    rb.add_argument("name")
    rb.add_argument("ref")
    rb.add_argument("--by", default="")
    bl = sub.add_parser("blame")
    bl.add_argument("name")
    bl.add_argument("ref", nargs="?")
    li = sub.add_parser("lint", help="exit 1 if there are findings at --fail-on or worse")
    li.add_argument("name")
    li.add_argument("ref", nargs="?")
    li.add_argument("--var", action="append", default=None, help="a variable the calling code supplies (repeatable)")
    li.add_argument("--fail-on", choices=SEVERITIES, default="error")
    rn = sub.add_parser("render")
    rn.add_argument("name")
    rn.add_argument("ref", nargs="?")
    rn.add_argument("--var", action="append", default=[], help="name=value (repeatable)")
    rn.add_argument("--lax", action="store_true", help="leave missing variables in place instead of failing")
    vr = sub.add_parser("vars")
    vr.add_argument("name")
    vr.add_argument("ref", nargs="?")
    vf = sub.add_parser("verify", help="check the log has not been altered; exit 1 if it has")
    vf.add_argument("--head", help="the head hash you published, to also catch truncation")
    pl = sub.add_parser("policy")
    pl.add_argument("--stages", default="", help="comma-separated promotion path, e.g. dev,staging,prod")
    pl.add_argument("--protected", default="", help="comma-separated tags that only deploy may move")
    dl = sub.add_parser("deploys")
    dl.add_argument("name", nargs="?")
    sub.add_parser("head")
    args = ap.parse_args(argv)
    try:
        vault = PromptVault.load(args.file) if os.path.exists(args.file) else PromptVault()
        code = 0
        size = vault.size
        if args.cmd == "commit":
            text = args.text if args.text is not None else (sys.stdin.read() if args.path == "-" else open(args.path, encoding="utf-8").read())  # noqa: SIM115
            before = len(vault.history(args.name)) if args.name in vault.names() else 0
            v = vault.commit(args.name, text, args.note, config=_config(args.config), author=args.author)
            print(f"{v.name} v{v.number} {v.digest}" + ("" if v.number > before else " (unchanged)"))
        elif args.cmd == "ls":
            for name in vault.names():
                latest = vault.get(name)
                tags = ", ".join(f"{t}=v{v.number}" for t, v in vault.tags(name).items())
                print(f"{name:<24}v{latest.number:<4}{latest.digest}  {tags}")
        elif args.cmd == "log":
            for v in reversed(vault.history(args.name)):
                print(f"v{v.number:<4}{v.digest}  {v.at[:19]}  {v.author or '-':<12}{v.note}")
        elif args.cmd == "show":
            print(vault.get(args.name, _ref(args.ref)).text)
        elif args.cmd == "diff":
            print(vault.diff(args.name, _ref(args.a), _ref(args.b)))
            ch = vault.compare(args.name, _ref(args.a), _ref(args.b))
            print(f"\n{ch.kind}: +{ch.lines_added} -{ch.lines_removed} lines, {ch.similarity:.0%} of words unchanged"
                  + (f"; callers must now supply {', '.join(ch.variables_added)}" if ch.variables_added else "")
                  + (f"; no longer used: {', '.join(ch.variables_removed)}" if ch.variables_removed else ""))
        elif args.cmd == "tag":
            v = vault.tag(args.name, args.tag, _ref(args.ref), args.by)
            print(f"{args.tag} -> {v.name} v{v.number}")
        elif args.cmd == "deploy":
            checks = []
            if args.lint:
                checks.append(gates.lint_gate())
            checks += [gates.command_gate(cmd) for cmd in args.gate_cmd]
            rel = vault.release(args.name, args.tag, gates.all_of(*checks) if checks else None, _ref(args.ref), args.by)
            if rel.ok:
                print(f"{args.tag} -> {rel.version.name} v{rel.version.number}" + ("" if rel.changed else " (already there)"))
            else:
                print(f"refused: {'; '.join(rel.reasons)}", file=sys.stderr)
                code = 1
        elif args.cmd == "rollback":
            before = len(vault.history(args.name))
            target = _ref(args.ref)
            assert target is not None
            v = vault.rollback(args.name, target, args.by)
            print(f"{v.name} v{v.number} {v.digest}" + ("" if v.number > before else " (already the newest)"))
        elif args.cmd == "blame":
            for line, number in vault.blame(args.name, _ref(args.ref)):
                print(f"v{number:<3} {line}")
        elif args.cmd == "lint":
            findings = vault.lint(args.name, _ref(args.ref), args.var)
            for f in findings:
                print(f)
            limit = SEVERITIES.index(args.fail_on)
            code = 1 if any(SEVERITIES.index(f.severity) <= limit for f in findings) else 0
            if not findings:
                print("no findings")
        elif args.cmd == "render":
            print(vault.render(args.name, _ref(args.ref), _values(args.var), strict=not args.lax))
        elif args.cmd == "vars":
            for var in vault.variables(args.name, _ref(args.ref)):
                print(f"{var.name}" + (f"  (optional, default {var.default!r})" if var.default is not None else "  (required)"))
        elif args.cmd == "verify":
            problems = vault.verify()
            if args.head and vault.head != args.head:
                problems.append("the latest event does not match the expected head: events were removed or replaced")
            print("\n".join(problems) if problems else f"ok: {len(vault.events())} events, head {vault.head}")
            code = 1 if problems else 0
        elif args.cmd == "policy":
            vault.configure([s for s in args.stages.split(",") if s], [s for s in args.protected.split(",") if s])
            print(f"stages: {', '.join(vault.stages) or '-'}; protected: {', '.join(sorted(vault.protected)) or '-'}")
        elif args.cmd == "deploys":
            for e in vault.deploy_log(args.name):
                print(f"{e['at'][:19]}  {e['name']} {e['tag']} v{e['number']}  {'moved' if e['passed'] else 'REFUSED'}"
                      + (f"  {'; '.join(e['reasons'])}" if e["reasons"] else ""))
        elif args.cmd == "head":
            print(vault.head)
        if vault.size != size:
            vault.save(args.file)
        return code
    except (OSError, ValueError, KeyError, LookupError, PermissionError) as exc:
        kind = "integrity" if isinstance(exc, IntegrityError) else "refused" if isinstance(exc, PolicyError) else "error"
        print(f"{kind}: {exc.args[0] if isinstance(exc, KeyError) and exc.args else exc}", file=sys.stderr)
        return 2


__all__ = ["main", "RefError", "TemplateError"]

if __name__ == "__main__":
    raise SystemExit(main())
