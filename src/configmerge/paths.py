"""Path syntax shared by every subsystem.

A path is a tuple of segments: strings index tables and integers index lists.
The text form is ``server.ports[1].name``; keys that are not bare words are
written in double quotes, as in ``labels."app.kubernetes.io/name"``.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator, Mapping, Union

from .errors import PathSyntaxError

Segment = Union[str, int]
Path = tuple[Segment, ...]


class _Missing:
    """Sentinel for "no value", distinct from a stored None."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        return False


MISSING = _Missing()

_BARE_DISPLAY = re.compile(r"[A-Za-z0-9_-]+")
_BARE_INPUT = re.compile(r"[^.\[\]\"\s]+")


def parse_path(text: str, *, wildcards: bool = False) -> Path:
    """Parse text into segments. ``wildcards`` also admits ``[*]`` for patterns."""
    if not text:
        raise PathSyntaxError(text, 0, "empty path")
    segments: list[Segment] = []
    i = 0
    expect_key = True
    while i < len(text):
        ch = text[i]
        if ch == "[":
            if expect_key and segments:
                raise PathSyntaxError(text, i, "expected a key after '.'")
            index, i = _read_index(text, i, wildcards)
            segments.append(index)
            expect_key = False
        elif ch == ".":
            if expect_key:
                raise PathSyntaxError(text, i, "empty key")
            i += 1
            expect_key = True
        elif expect_key:
            key, i = _read_key(text, i)
            segments.append(key)
            expect_key = False
        else:
            raise PathSyntaxError(text, i, f"unexpected character {ch!r}")
    if expect_key:
        raise PathSyntaxError(text, len(text), "path ends with '.'")
    return tuple(segments)


def _read_index(text: str, start: int, wildcards: bool) -> tuple[Segment, int]:
    close = text.find("]", start)
    if close < 0:
        raise PathSyntaxError(text, start, "unclosed '['")
    body = text[start + 1 : close]
    if wildcards and body == "*":
        return "*", close + 1
    if body.isascii() and body.isdigit():
        return int(body), close + 1
    raise PathSyntaxError(text, start + 1, "list index must be a non-negative integer")


def _read_key(text: str, start: int) -> tuple[str, int]:
    if text[start] == '"':
        return _read_quoted(text, start)
    match = _BARE_INPUT.match(text, start)
    if match is None:
        raise PathSyntaxError(text, start, f"unexpected character {text[start]!r}")
    return match.group(), match.end()


def _read_quoted(text: str, start: int) -> tuple[str, int]:
    out: list[str] = []
    i = start + 1
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            out.append(text[i + 1])
            i += 2
        elif ch == '"':
            return "".join(out), i + 1
        else:
            out.append(ch)
            i += 1
    raise PathSyntaxError(text, start, "unterminated quoted key")


def format_path(path: Path) -> str:
    """Render segments back to text; parse_path(format_path(p)) == p for non-empty p."""
    if not path:
        return "<root>"
    out: list[str] = []
    for segment in path:
        if isinstance(segment, int):
            out.append(f"[{segment}]")
            continue
        key = segment if _BARE_DISPLAY.fullmatch(segment) else json.dumps(segment, ensure_ascii=False)
        out.append(f".{key}" if out else key)
    return "".join(out)


def as_path(path: str | Path) -> Path:
    """Accept either form of a path."""
    return parse_path(path) if isinstance(path, str) else path


def lookup(data: Any, path: Path) -> Any:
    """Return the value at path, or MISSING. Never raises on a wrong shape."""
    node = data
    for segment in path:
        node = step(node, segment)
        if node is MISSING:
            return MISSING
    return node


def step(node: Any, segment: Segment) -> Any:
    """Descend one segment, or return MISSING when the shape does not allow it."""
    if isinstance(segment, int):
        if isinstance(node, list) and segment < len(node):
            return node[segment]
        return MISSING
    if isinstance(node, Mapping):
        return node.get(segment, MISSING)
    return MISSING


def assign(data: dict[str, Any], path: Path, value: Any) -> None:
    """Store value at a path of string keys, creating tables along the way.

    An intermediate value that is not a table is replaced.
    """
    node = data
    for segment in path[:-1]:
        child = node.get(segment)  # type: ignore[call-overload]
        if not isinstance(child, dict):
            child = {}
            node[segment] = child  # type: ignore[index]
        node = child
    node[path[-1]] = value  # type: ignore[index]


def iter_leaves(data: Any, root: Path = (), *, into_lists: bool = True) -> Iterator[tuple[Path, Any]]:
    """Yield (path, value) for each leaf. Empty containers count as leaves."""
    if isinstance(data, Mapping) and data:
        for key, value in data.items():
            yield from iter_leaves(value, root + (key,), into_lists=into_lists)
    elif into_lists and isinstance(data, list) and data:
        for index, value in enumerate(data):
            yield from iter_leaves(value, root + (index,), into_lists=into_lists)
    else:
        yield root, data


class PathPattern:
    """A path with wildcards: ``*`` matches one segment, ``**`` any number of them."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.segments = parse_path(text, wildcards=True)

    @classmethod
    def from_segments(cls, segments: Path) -> "PathPattern":
        self = cls.__new__(cls)
        self.segments = segments
        self.text = ".".join("*" if s == "*" else format_path((s,)) for s in segments)
        return self

    def matches(self, path: Path) -> bool:
        return _match(self.segments, path)

    def matches_within(self, path: Path) -> bool:
        """True if the pattern matches path or any of its ancestors."""
        return any(_match(self.segments, path[:depth]) for depth in range(len(path), 0, -1))

    @property
    def specificity(self) -> tuple[int, int, int]:
        """Sort key: more literal segments first, then fewer ``**``, then fewer ``*``."""
        stars = sum(1 for s in self.segments if s == "*")
        globs = sum(1 for s in self.segments if s == "**")
        return (len(self.segments) - stars - globs, -globs, -stars)

    def __repr__(self) -> str:
        return f"PathPattern({self.text!r})"


def _match(pattern: Path, path: Path) -> bool:
    if not pattern:
        return not path
    head = pattern[0]
    if head == "**":
        return any(_match(pattern[1:], path[skip:]) for skip in range(len(path) + 1))
    if not path:
        return False
    if head == "*" or head == path[0]:
        return _match(pattern[1:], path[1:])
    return False
