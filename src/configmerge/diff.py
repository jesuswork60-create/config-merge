"""Comparison of two resolved configurations."""

from __future__ import annotations

import enum
import json
import math
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator, Mapping

from .paths import MISSING, Path, PathPattern, format_path, iter_leaves

Mask = Callable[[Path, Any], Any]


class ChangeKind(str, enum.Enum):
    ADDED = "added"
    REMOVED = "removed"
    CHANGED = "changed"


@dataclass(frozen=True)
class Change:
    path: Path
    kind: ChangeKind
    old: Any = MISSING
    new: Any = MISSING

    @property
    def type_changed(self) -> bool:
        return self.kind is ChangeKind.CHANGED and type(self.old) is not type(self.new)


def same_value(left: Any, right: Any) -> bool:
    """Equality that notices type: 1, 1.0 and True are all different settings."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(same_value(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(same_value(a, b) for a, b in zip(left, right))
    if isinstance(left, float) and math.isnan(left) and math.isnan(right):
        return True
    return left == right


def diff_configs(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    *,
    by_index: bool = False,
    ignore: Iterable[str | PathPattern] = (),
) -> list[Change]:
    """List the differences between two trees, sorted by path text.

    Lists compare as whole values unless ``by_index`` is set. ``ignore`` patterns
    drop matching paths and everything under them.
    """
    patterns = [p if isinstance(p, PathPattern) else PathPattern(p) for p in ignore]
    changes = _walk(left, right, (), by_index)
    kept = [c for c in changes if not any(p.matches_within(c.path) for p in patterns)]
    return sorted(kept, key=lambda change: format_path(change.path))


def _walk(left: Any, right: Any, path: Path, by_index: bool) -> Iterator[Change]:
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        for key in left.keys() | right.keys():
            yield from _walk_key(left, right, key, path + (key,), by_index)
    elif by_index and isinstance(left, list) and isinstance(right, list):
        for index in range(max(len(left), len(right))):
            child = path + (index,)
            if index >= len(right):
                yield from _one_sided(left[index], child, ChangeKind.REMOVED)
            elif index >= len(left):
                yield from _one_sided(right[index], child, ChangeKind.ADDED)
            else:
                yield from _walk(left[index], right[index], child, by_index)
    elif not same_value(left, right):
        yield Change(path, ChangeKind.CHANGED, left, right)


def _walk_key(left: Mapping[str, Any], right: Mapping[str, Any], key: str, path: Path, by_index: bool) -> Iterator[Change]:
    if key not in right:
        yield from _one_sided(left[key], path, ChangeKind.REMOVED)
    elif key not in left:
        yield from _one_sided(right[key], path, ChangeKind.ADDED)
    else:
        yield from _walk(left[key], right[key], path, by_index)


def _one_sided(value: Any, path: Path, kind: ChangeKind) -> Iterator[Change]:
    for leaf, item in iter_leaves(value, path, into_lists=False):
        if kind is ChangeKind.ADDED:
            yield Change(leaf, kind, new=item)
        else:
            yield Change(leaf, kind, old=item)


def _text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def render_diff(
    changes: Iterable[Change],
    *,
    left_mask: Mask | None = None,
    right_mask: Mask | None = None,
) -> str:
    """Text form: ``+`` added, ``-`` removed, ``~`` changed. Masks hide secrets."""
    lines: list[str] = []
    for change in changes:
        name = format_path(change.path)
        old = left_mask(change.path, change.old) if left_mask and change.old is not MISSING else change.old
        new = right_mask(change.path, change.new) if right_mask and change.new is not MISSING else change.new
        if change.kind is ChangeKind.ADDED:
            lines.append(f"+ {name} = {_text(new)}")
        elif change.kind is ChangeKind.REMOVED:
            lines.append(f"- {name} = {_text(old)}")
        else:
            lines.append(f"~ {name}: {_text(old)} -> {_text(new)}")
    return "\n".join(lines)


def changes_to_dicts(
    changes: Iterable[Change],
    *,
    left_mask: Mask | None = None,
    right_mask: Mask | None = None,
) -> list[dict[str, Any]]:
    """JSON-ready form of a diff. Absent sides are left out rather than null."""
    out: list[dict[str, Any]] = []
    for change in changes:
        row: dict[str, Any] = {"path": format_path(change.path), "kind": change.kind.value}
        if change.old is not MISSING:
            row["old"] = left_mask(change.path, change.old) if left_mask else change.old
        if change.new is not MISSING:
            row["new"] = right_mask(change.path, change.new) if right_mask else change.new
        out.append(row)
    return out
