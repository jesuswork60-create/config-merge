"""Records where every value came from and answers "why is this set to that?".

The merge engine, the schema default filler and the interpolator all report
what they do here as a chronological list of events per path. Lists are treated
as single leaves by default, so they carry an extra per-element record of which
layer contributed each element.
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator, Mapping

from .paths import MISSING, Path, format_path, iter_leaves, lookup

Mask = Callable[[Path, Any], Any]

SCHEMA_DEFAULT_LAYER = "<schema default>"
INTERPOLATION_LAYER = "<interpolation>"


class Action(str, enum.Enum):
    SET = "set"
    OVERRIDE = "override"
    APPEND = "append"
    UNIQUE = "unique"
    REMOVE = "remove"
    DEFAULT = "default"
    INTERPOLATE = "interpolate"


@dataclass(frozen=True)
class Ref:
    """One reference used by an interpolated value. ``scheme`` is None for config paths."""

    scheme: str | None
    name: str

    def __str__(self) -> str:
        return f"{self.scheme}:{self.name}" if self.scheme else self.name


@dataclass(frozen=True)
class Event:
    path: Path
    action: Action
    layer: str
    value: Any = MISSING
    previous: Any = MISSING
    previous_layer: str | None = None
    note: str | None = None
    references: tuple[Ref, ...] = ()

    @property
    def origin(self) -> str:
        return f"{self.layer} ({self.note})" if self.note else self.layer


def _display(path: Path, value: Any, mask: Mask | None) -> str:
    shown = mask(path, value) if mask else value
    return json.dumps(shown, ensure_ascii=False, default=str)


@dataclass(frozen=True)
class Explanation:
    """The answer to explain(path): current value plus the history behind it."""

    path: Path
    exists: bool
    value: Any
    events: tuple[Event, ...] = ()
    element_layer: str | None = None
    children: tuple[tuple[Path, str | None], ...] = ()
    element_layers: tuple[str, ...] = ()

    @property
    def source(self) -> str | None:
        """Layer that decided the current value, or None if it is unset."""
        if self.element_layer is not None:
            return self.element_layer
        for event in reversed(self.events):
            if event.action is Action.REMOVE:
                return None
            if event.action is not Action.INTERPOLATE:
                return event.layer
        return None

    @property
    def overridden(self) -> list[Event]:
        """Events whose values were replaced by the winning one, oldest first."""
        values = [e for e in self.events if e.action not in (Action.INTERPOLATE, Action.REMOVE)]
        return values[:-1]

    @property
    def references(self) -> tuple[Ref, ...]:
        return tuple(ref for event in self.events for ref in event.references)

    def render(self, mask: Mask | None = None) -> str:
        name = format_path(self.path)
        if self.children:
            lines = [f"{name}: table"]
            lines.extend(f"  {format_path(path)}  <- {layer or 'unknown'}" for path, layer in self.children)
            return "\n".join(lines)
        if not self.exists:
            if not self.events:
                return f"{name}: not set by any layer"
            last = self.events[-1]
            return f"{name}: not set (removed by {last.origin})\n" + self._history(mask)
        head = f"{name} = {_display(self.path, self.value, mask)}"
        if self.source is None:
            return head
        lines = [head, f"  from {self.source}"]
        if self.element_layer is not None:
            lines[-1] += " (list element)"
        if len(set(self.element_layers)) > 1:
            lines.append("  elements:")
            lines.extend(f"    [{i}] {layer}" for i, layer in enumerate(self.element_layers))
        if self.events:
            lines.append(self._history(mask))
        return "\n".join(lines)

    def _history(self, mask: Mask | None) -> str:
        lines = ["  history:"]
        for event in self.events:
            lines.append(f"    {event.action.value:<11} {self._describe(event, mask)}")
        return "\n".join(lines)

    def _describe(self, event: Event, mask: Mask | None) -> str:
        path = event.path
        if event.action is Action.REMOVE:
            return f"{_display(path, event.previous, mask)} dropped by {event.origin}"
        if event.action is Action.INTERPOLATE:
            refs = ", ".join(str(ref) for ref in event.references)
            before = _display(path, event.previous, mask)
            after = _display(path, event.value, mask)
            return f"{before} -> {after}" + (f" (uses {refs})" if refs else "")
        return f"{_display(path, event.value, mask)} from {event.origin}"

    def to_dict(self, mask: Mask | None = None) -> dict[str, Any]:
        def show(path: Path, value: Any) -> Any:
            if value is MISSING:
                return None
            return mask(path, value) if mask else value

        return {
            "path": format_path(self.path),
            "exists": self.exists,
            "value": show(self.path, self.value),
            "source": self.source,
            "elements": list(self.element_layers),
            "history": [
                {
                    "path": format_path(event.path),
                    "action": event.action.value,
                    "layer": event.layer,
                    "note": event.note,
                    "value": show(event.path, event.value),
                    "previous": show(event.path, event.previous),
                    "previous_layer": event.previous_layer,
                    "references": [str(ref) for ref in event.references],
                }
                for event in self.events
            ],
            "children": [
                {"path": format_path(path), "source": layer} for path, layer in self.children
            ],
        }


def _tree_leaves(path: Path, value: Any) -> Iterator[tuple[Path, Any]]:
    """Leaves as provenance sees them: tables are descended, lists are single leaves."""
    return iter_leaves(value, path, into_lists=False)


class Provenance:
    """The event log for one load."""

    def __init__(self) -> None:
        self._events: dict[Path, list[Event]] = {}
        self._elements: dict[Path, list[str]] = {}
        self._order: list[Event] = []
        self.layers: list[str] = []

    def note_layer(self, name: str) -> None:
        self.layers.append(name)

    def record(self, event: Event) -> None:
        self._events.setdefault(event.path, []).append(event)
        self._order.append(event)

    def events_at(self, path: Path) -> list[Event]:
        return list(self._events.get(path, ()))

    def all_events(self) -> list[Event]:
        """Every event in the order it happened."""
        return list(self._order)

    def interpolations(self) -> Iterator[Event]:
        return (event for event in self._order if event.action is Action.INTERPOLATE)

    def elements(self, path: Path) -> list[str]:
        return list(self._elements.get(path, ()))

    def set_elements(self, path: Path, owners: Iterable[str]) -> None:
        self._elements[path] = list(owners)

    def owner(self, path: Path) -> str | None:
        """Name of the layer that last decided the value at path, or None."""
        for depth in range(len(path), 0, -1):
            prefix = path[:depth]
            if depth < len(path):
                owners = self._elements.get(prefix)
                index = path[depth]
                if owners and isinstance(index, int) and index < len(owners):
                    return owners[index]
            for event in reversed(self._events.get(prefix, ())):
                if event.action is Action.INTERPOLATE:
                    continue
                return None if event.action is Action.REMOVE else event.layer
        return None

    def record_assign(self, path: Path, old: Any, new: Any, layer_name: str, notes: Mapping[Path, str]) -> None:
        """Log new replacing old at path, covering every leaf either side touches."""
        old_leaves = dict(_tree_leaves(path, old)) if old is not MISSING else {}
        new_leaves = dict(_tree_leaves(path, new))
        for leaf, previous in old_leaves.items():
            if leaf not in new_leaves:
                self.record(
                    Event(
                        leaf,
                        Action.REMOVE,
                        layer_name,
                        previous=previous,
                        previous_layer=self.owner(leaf),
                    )
                )
                self._elements.pop(leaf, None)
        for leaf, value in new_leaves.items():
            previous = old_leaves.get(leaf, old if leaf == path else MISSING)
            self.record(
                Event(
                    leaf,
                    Action.SET if previous is MISSING else Action.OVERRIDE,
                    layer_name,
                    value=value,
                    previous=previous,
                    previous_layer=None if previous is MISSING else self.owner(leaf),
                    note=notes.get(leaf),
                )
            )
            if isinstance(value, list):
                self._elements[leaf] = [layer_name] * len(value)
            else:
                self._elements.pop(leaf, None)

    def explain(self, path: Path, data: Mapping[str, Any]) -> Explanation:
        """Build the explanation of path against the final resolved data."""
        value = lookup(data, path)
        exists = value is not MISSING
        if exists and isinstance(value, Mapping) and value:
            children = tuple((leaf, self.owner(leaf)) for leaf, _ in iter_leaves(value, path))
            return Explanation(path, True, value, children=children)
        events = self._chain(path)
        element_layer = self._element_layer(path) if exists else None
        element_layers = tuple(self._elements.get(path, ())) if isinstance(value, list) else ()
        return Explanation(path, exists, value, tuple(events), element_layer, element_layers=element_layers)

    def _chain(self, path: Path) -> list[Event]:
        """Events relevant to path, borrowing from the nearest ancestor when needed.

        A value inside a list or inside an aliased table has no merge events of
        its own, so the events of the container explain where it came from.
        """
        own = self.events_at(path)
        if any(event.action is not Action.INTERPOLATE for event in own):
            return own
        for depth in range(len(path) - 1, 0, -1):
            inherited = self.events_at(path[:depth])
            if inherited:
                return inherited + own
        return own

    def _element_layer(self, path: Path) -> str | None:
        if path and isinstance(path[-1], int):
            owners = self._elements.get(path[:-1])
            if owners and path[-1] < len(owners):
                return owners[path[-1]]
        return None
