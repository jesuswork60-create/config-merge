"""Expansion of ``${...}`` references inside string values.

Syntax:
    ``${server.host}``          value at a config path
    ``${env:HOME}``             value from a named resolver (``env`` is built in)
    ``${server.port:-8080}``    default when the reference is missing or null
    ``${a:-${b:-c}}``           defaults may contain further references
    ``$$``                      a literal dollar sign

A string that consists of exactly one reference takes the type of whatever it
refers to, so ``port = "${defaults.port}"`` stays an integer and a reference to
a table copies the table. Inside longer strings the target must be a scalar.
"""

from __future__ import annotations

import copy
import datetime as dt
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Union

from .errors import (
    InterpolationCycleError,
    InterpolationError,
    PathSyntaxError,
    ReferenceNotFoundError,
)
from .paths import MISSING, Path, format_path, parse_path, step
from .provenance import INTERPOLATION_LAYER, Action, Event, Provenance, Ref

Resolver = Callable[[str], Any]

_SCHEME = re.compile(r"([A-Za-z][A-Za-z0-9_-]*):(.*)\Z", re.DOTALL)


@dataclass(frozen=True)
class Literal:
    text: str


@dataclass(frozen=True)
class Reference:
    text: str  # the source between ${ and }, for messages
    scheme: str | None
    name: str
    default: tuple["Part", ...] | None = None


Part = Union[Literal, Reference]


def parse_template(text: str, *, offset: int = 0) -> tuple[Part, ...]:
    """Split a string into literal text and references."""
    parts: list[Part] = []
    buffer: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        following = text[i + 1] if i + 1 < len(text) else ""
        if ch == "$" and following == "$":
            buffer.append("$")
            i += 2
        elif ch == "$" and following == "{":
            if buffer:
                parts.append(Literal("".join(buffer)))
                buffer = []
            reference, i = _parse_reference(text, i, offset)
            parts.append(reference)
        else:
            buffer.append(ch)
            i += 1
    if buffer:
        parts.append(Literal("".join(buffer)))
    return tuple(parts)


def _parse_reference(text: str, start: int, offset: int) -> tuple[Reference, int]:
    body_start = start + 2
    end, separator = _scan_reference(text, body_start)
    if end < 0:
        raise InterpolationError(f"unterminated reference starting at column {offset + start + 1} in {text!r}")
    if separator >= 0:
        target = text[body_start:separator]
        default = parse_template(text[separator + 2 : end], offset=offset + separator + 2)
    else:
        target = text[body_start:end]
        default = None
    if not target:
        raise InterpolationError(f"empty reference at column {offset + start + 1} in {text!r}")
    match = _SCHEME.match(target) if not target.startswith('"') else None
    scheme, name = (match.group(1), match.group(2)) if match else (None, target)
    if scheme is not None and not name:
        raise InterpolationError(f"reference ${{{target}}} names no key")
    return Reference(text[body_start:end], scheme, name, default), end + 1


def _scan_reference(text: str, start: int) -> tuple[int, int]:
    """Find the closing brace and the first ``:-`` at this nesting level. -1 if absent."""
    depth = 0
    separator = -1
    i = start
    while i < len(text):
        pair = text[i : i + 2]
        if pair == "$$":
            i += 2
        elif pair == "${":
            depth += 1
            i += 2
        elif text[i] == "}":
            if depth == 0:
                return i, separator
            depth -= 1
            i += 1
        elif pair == ":-" and depth == 0 and separator < 0:
            separator = i
            i += 2
        else:
            i += 1
    return -1, separator


def _embed(value: Any, reference: Reference, where: str) -> str:
    """Text form of a scalar placed inside a longer string."""
    if isinstance(value, str):
        return value
    if value is True or value is False:
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        kind = "table" if isinstance(value, dict) else "list"
        raise InterpolationError(f"${{{reference.text}}} is a {kind} and cannot be embedded in a longer string", where)
    return str(value)


class Interpolator:
    """Resolves all references in a config tree. Instances are reusable and stateless."""

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        resolvers: Mapping[str, Resolver] | None = None,
        provenance: Provenance | None = None,
    ) -> None:
        env = os.environ if environ is None else environ
        self.resolvers: dict[str, Resolver] = {"env": env.get}
        self.resolvers.update(resolvers or {})
        self.provenance = provenance

    def resolve(self, data: Mapping[str, Any]) -> dict[str, Any]:
        """Return a new tree with every reference expanded."""
        run = _Run(data, self.resolvers, self.provenance)
        try:
            return {key: run.resolve_path((key,), []) for key in data}
        except RecursionError:
            raise InterpolationError("references nested too deeply to resolve") from None


class _Run:
    """State of one resolve() call: the memo of finished paths."""

    def __init__(self, root: Mapping[str, Any], resolvers: Mapping[str, Resolver], provenance: Provenance | None) -> None:
        self.root = root
        self.resolvers = resolvers
        self.provenance = provenance
        self.done: dict[Path, Any] = {}

    def resolve_path(self, path: Path, stack: list[Path]) -> Any:
        """Fully resolved value at path, or MISSING. ``stack`` is the chain being followed."""
        if path in self.done:
            return self.done[path]
        if path in stack:
            start = stack.index(path)
            raise InterpolationCycleError([format_path(p) for p in stack[start:]] + [format_path(path)])
        stack.append(path)
        try:
            value = self._walk(path, stack)
        finally:
            stack.pop()
        if value is not MISSING:
            self.done[path] = value
        return value

    def _walk(self, path: Path, stack: list[Path]) -> Any:
        node: Any = self.root
        resolved = False
        last = len(path) - 1
        for depth, segment in enumerate(path):
            node = step(node, segment)
            if node is MISSING:
                return MISSING
            # A string part-way down the path may be a reference to a table, so
            # look through it. What comes back is final and must not be re-parsed.
            if not resolved and depth < last and isinstance(node, str):
                node = self.resolve_path(path[: depth + 1], stack)
                resolved = True
                if node is MISSING:
                    return MISSING
        return node if resolved else self._resolve_node(node, path, stack)

    def _resolve_node(self, node: Any, path: Path, stack: list[Path]) -> Any:
        if isinstance(node, str):
            return self._resolve_string(node, path, stack)
        if isinstance(node, dict):
            return {key: self.resolve_path(path + (key,), stack) for key in node}
        if isinstance(node, list):
            return [self.resolve_path(path + (i,), stack) for i in range(len(node))]
        return node

    def _resolve_string(self, raw: str, path: Path, stack: list[Path]) -> Any:
        if "$" not in raw:
            return raw
        where = format_path(path)
        try:
            parts = parse_template(raw)
        except InterpolationError as exc:
            raise InterpolationError(str(exc), where) from None
        used: list[Ref] = []
        value = self._render(parts, path, stack, used)
        if self.provenance is not None and value != raw:
            self.provenance.record(
                Event(path, Action.INTERPOLATE, INTERPOLATION_LAYER, value=value, previous=raw, references=tuple(used))
            )
        return value

    def _render(self, parts: tuple[Part, ...], path: Path, stack: list[Path], used: list[Ref]) -> Any:
        if not parts:
            return ""
        if len(parts) == 1:
            part = parts[0]
            if isinstance(part, Literal):
                return part.text
            return copy.deepcopy(self._lookup(part, path, stack, used))
        out: list[str] = []
        for part in parts:
            if isinstance(part, Literal):
                out.append(part.text)
            else:
                out.append(_embed(self._lookup(part, path, stack, used), part, format_path(path)))
        return "".join(out)

    def _lookup(self, reference: Reference, path: Path, stack: list[Path], used: list[Ref]) -> Any:
        value = self._fetch(reference, path, stack)
        if value is not MISSING and value is not None:
            used.append(Ref(reference.scheme, reference.name))
            return value
        if reference.default is not None:
            return self._render(reference.default, path, stack, used)
        if value is None:
            used.append(Ref(reference.scheme, reference.name))
            return None
        raise ReferenceNotFoundError(reference.text, format_path(path))

    def _fetch(self, reference: Reference, path: Path, stack: list[Path]) -> Any:
        if reference.scheme is not None:
            resolver = self.resolvers.get(reference.scheme)
            if resolver is None:
                known = ", ".join(sorted(self.resolvers))
                raise InterpolationError(
                    f"unknown resolver {reference.scheme!r} in ${{{reference.text}}} (known: {known})",
                    format_path(path),
                )
            value = resolver(reference.name)
            return MISSING if value is None else value
        try:
            target = parse_path(reference.name)
        except PathSyntaxError as exc:
            raise InterpolationError(str(exc), format_path(path)) from None
        return self.resolve_path(target, stack)
