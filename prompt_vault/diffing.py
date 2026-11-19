"""Understanding a change: what kind it is, who it will break, and which version wrote each line."""
from __future__ import annotations

import difflib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from . import template


@dataclass(frozen=True)
class Change:
    kind: str  # identical, config, whitespace, edit or breaking
    similarity: float  # 0 to 1, by words
    lines_added: int
    lines_removed: int
    variables_added: tuple[str, ...] = ()  # new required placeholders: callers must now supply them
    variables_removed: tuple[str, ...] = ()  # placeholders that went away: callers still supplying them will now fail in strict mode
    config_changed: tuple[str, ...] = field(default=())

    @property
    def breaking(self) -> bool:
        return self.kind == "breaking"


def _words(text: str) -> list[str]:
    return text.split()


def compare(old_text: str, new_text: str, old_config: dict[str, object] | None = None, new_config: dict[str, object] | None = None) -> Change:
    """Classify the change between two versions. ``breaking`` means the set of required variables changed, which is the one
    kind of prompt edit that breaks callers instead of changing behaviour."""
    old_config, new_config = old_config or {}, new_config or {}
    config_keys = tuple(sorted(k for k in set(old_config) | set(new_config) if old_config.get(k) != new_config.get(k)))
    old_lines, new_lines = old_text.splitlines(), new_text.splitlines()
    added = removed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    sim = difflib.SequenceMatcher(a=_words(old_text), b=_words(new_text), autojunk=False).ratio() if (old_text or new_text) else 1.0
    old_req = {v.name for v in template.variables(old_text) if v.required}
    new_req = {v.name for v in template.variables(new_text) if v.required}
    all_old = {v.name for v in template.variables(old_text)}
    all_new = {v.name for v in template.variables(new_text)}
    gained, lost = tuple(sorted(new_req - all_old)), tuple(sorted(all_old - all_new))
    if old_text == new_text:
        kind = "config" if config_keys else "identical"
    elif re.sub(r"\s+", " ", old_text).strip() == re.sub(r"\s+", " ", new_text).strip():
        kind = "whitespace"
    elif gained or lost or old_req != new_req:
        kind = "breaking"
    else:
        kind = "edit"
    return Change(kind, sim, added, removed, gained, lost, config_keys)


def blame(texts: Sequence[str]) -> list[tuple[str, int]]:
    """For the last text in a history, the version number (1-based) that introduced each line. A line that was deleted and
    later written again is credited to the version that wrote it again."""
    origin: list[tuple[str, int]] = []
    previous: list[str] = []
    for number, text in enumerate(texts, 1):
        lines = text.splitlines()
        nxt: list[tuple[str, int]] = []
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=previous, b=lines, autojunk=False).get_opcodes():
            if tag == "equal":
                nxt.extend(origin[i1:i2])
            elif tag in ("replace", "insert"):
                nxt.extend((line, number) for line in lines[j1:j2])
        origin, previous = nxt, lines
    return origin
