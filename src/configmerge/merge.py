"""The deep merge engine and its per-path strategies.

Tables merge key by key and everything else is replaced, unless the caller
declares otherwise for a path. Inputs are never mutated: merging returns new
containers along the changed branches and shares the untouched ones.
"""

from __future__ import annotations

import copy
import enum
from typing import Any, Mapping

from .errors import MergeConflictError, MergeRuleError
from .paths import MISSING, Path, PathPattern, format_path
from .provenance import Action, Event, Provenance
from .sources import Layer


class Strategy(str, enum.Enum):
    """How a layer's value combines with the one already present.

    ``deep`` merges tables by key and lists by position. ``append`` concatenates
    lists, ``unique`` does the same while dropping repeats (the first occurrence
    keeps its place). ``replace`` discards the previous value whatever its shape.
    A non-list value given to ``append`` or ``unique`` counts as one element.
    """

    REPLACE = "replace"
    APPEND = "append"
    UNIQUE = "unique"
    DEEP = "deep"


class MergePolicy:
    """Maps paths to strategies. Rules are path patterns; the most specific match wins."""

    def __init__(self, rules: Mapping[str, Strategy | str] | None = None, *, strict_types: bool = False) -> None:
        self.strict_types = strict_types
        self._rules: list[tuple[PathPattern, Strategy]] = []
        for pattern, strategy in (rules or {}).items():
            self.add_rule(pattern, strategy)

    def add_rule(self, pattern: str, strategy: Strategy | str) -> None:
        try:
            resolved = Strategy(strategy)
        except ValueError:
            names = ", ".join(s.value for s in Strategy)
            raise MergeRuleError(f"unknown merge strategy {strategy!r} for {pattern} (use one of: {names})") from None
        self._rules.append((PathPattern(pattern), resolved))

    def rule_for(self, path: Path) -> Strategy | None:
        best: tuple[tuple[int, int, int], int] | None = None
        found: Strategy | None = None
        for order, (pattern, strategy) in enumerate(self._rules):
            if pattern.matches(path):
                rank = (pattern.specificity, order)
                if best is None or rank > best:
                    best, found = rank, strategy
        return found

    def strategy_for(self, path: Path, base: Any, incoming: Any) -> Strategy:
        rule = self.rule_for(path)
        if rule is not None:
            return rule
        if isinstance(incoming, dict) and (base is MISSING or isinstance(base, dict)):
            return Strategy.DEEP
        return Strategy.REPLACE


def _kind(value: Any) -> str:
    if isinstance(value, dict):
        return "table"
    if isinstance(value, list):
        return "list"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__


def _freeze(value: Any) -> Any:
    """A hashable stand-in for equality in ``unique``. True and 1 stay distinct."""
    if isinstance(value, dict):
        return ("table", frozenset((key, _freeze(item)) for key, item in value.items()))
    if isinstance(value, list):
        return ("list", tuple(_freeze(item) for item in value))
    if isinstance(value, bool):
        return ("boolean", value)
    return value


class Merger:
    """Applies layers one after another, reporting every change to a Provenance."""

    def __init__(self, policy: MergePolicy | None = None, provenance: Provenance | None = None) -> None:
        self.policy = policy or MergePolicy()
        self.provenance = provenance or Provenance()

    def apply(self, base: Mapping[str, Any], layer: Layer) -> dict[str, Any]:
        """Return base with layer merged over it."""
        self.provenance.note_layer(layer.name)
        return self._merge_table(base, layer.data, (), layer)

    def merge_all(self, layers: list[Layer]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for layer in layers:
            result = self.apply(result, layer)
        return result

    def _merge_table(self, base: Mapping[str, Any], incoming: Mapping[str, Any], path: Path, layer: Layer) -> dict[str, Any]:
        result = dict(base)
        for key, value in incoming.items():
            result[key] = self._merge_value(base.get(key, MISSING), value, path + (key,), layer)
        return result

    def _merge_value(self, base: Any, incoming: Any, path: Path, layer: Layer) -> Any:
        strategy = self.policy.strategy_for(path, base, incoming)
        self._check_shape(base, incoming, path, layer, strategy)
        if strategy in (Strategy.APPEND, Strategy.UNIQUE):
            return self._merge_list(base, incoming, path, layer, strategy)
        if strategy is Strategy.DEEP:
            if isinstance(incoming, dict) and incoming and (base is MISSING or isinstance(base, dict)):
                return self._merge_table({} if base is MISSING else base, incoming, path, layer)
            if isinstance(base, list) and isinstance(incoming, list):
                return self._merge_by_position(base, incoming, path, layer)
            if isinstance(base, dict) and isinstance(incoming, dict):
                return base  # An empty table adds nothing to an existing one.
        return self._replace(base, incoming, path, layer)

    def _replace(self, base: Any, incoming: Any, path: Path, layer: Layer) -> Any:
        value = copy.deepcopy(incoming)
        self.provenance.record_assign(path, base, value, layer.name, layer.notes)
        return value

    def _merge_list(self, base: Any, incoming: Any, path: Path, layer: Layer, strategy: Strategy) -> list[Any]:
        extra = copy.deepcopy(incoming) if isinstance(incoming, list) else [copy.deepcopy(incoming)]
        if isinstance(base, list):
            items = base + extra
            owners = self._list_owners(path, base) + [layer.name] * len(extra)
        else:
            items, owners = extra, [layer.name] * len(extra)
        if strategy is Strategy.UNIQUE:
            items, owners = _dedupe(items, owners)
        action = Action.UNIQUE if strategy is Strategy.UNIQUE else Action.APPEND
        if isinstance(base, list):
            self.provenance.record(
                Event(
                    path,
                    action,
                    layer.name,
                    value=items,
                    previous=base,
                    previous_layer=self.provenance.owner(path),
                    note=layer.notes.get(path),
                )
            )
        else:
            self.provenance.record_assign(path, base, items, layer.name, layer.notes)
        self.provenance.set_elements(path, owners)
        return items

    def _list_owners(self, path: Path, base: list[Any]) -> list[str]:
        owners = self.provenance.elements(path)
        if len(owners) == len(base):
            return owners
        return [self.provenance.owner(path) or "unknown"] * len(base)

    def _merge_by_position(self, base: list[Any], incoming: list[Any], path: Path, layer: Layer) -> list[Any]:
        result = list(base)
        owners = self._list_owners(path, base)
        for index, item in enumerate(incoming):
            child = path + (index,)
            if index < len(base):
                result[index] = self._merge_value(base[index], item, child, layer)
                owners[index] = layer.name
            else:
                result.append(self._merge_value(MISSING, item, child, layer))
                owners.append(layer.name)
        self.provenance.set_elements(path, owners)
        return result

    def _check_shape(self, base: Any, incoming: Any, path: Path, layer: Layer, strategy: Strategy) -> None:
        if not self.policy.strict_types or base is MISSING or base is None or incoming is None:
            return
        if strategy in (Strategy.APPEND, Strategy.UNIQUE) and isinstance(base, list):
            return
        if _kind(base) != _kind(incoming):
            owner = self.provenance.owner(path) or "an earlier layer"
            raise MergeConflictError(
                f"{format_path(path)}: layer {layer.name!r} sets a {_kind(incoming)} "
                f"over a {_kind(base)} from {owner!r}"
            )


def _dedupe(items: list[Any], owners: list[str]) -> tuple[list[Any], list[str]]:
    seen: set[Any] = set()
    kept: list[Any] = []
    kept_owners: list[str] = []
    for item, owner in zip(items, owners):
        marker = _freeze(item)
        if marker in seen:
            continue
        seen.add(marker)
        kept.append(item)
        kept_owners.append(owner)
    return kept, kept_owners
