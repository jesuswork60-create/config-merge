"""Configuration sources. Each one turns some outside input into Layers.

A Layer is the unit the merge engine works on: a plain nested mapping, a name
to report in provenance, and optional per-key notes such as the environment
variable that produced a value. A source can yield several layers (a directory
yields one per file) or none (an optional file that is absent).
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path as FilePath
from typing import Any, Callable, Iterable, Mapping

from .coerce import coerce_scalar
from .errors import ConfigParseError, PathSyntaxError, SourceError
from .paths import Path, assign, format_path, parse_path

Coercer = Callable[[str], Any]


@dataclass(frozen=True)
class Layer:
    """One set of values with a name to attribute them to."""

    name: str
    data: Mapping[str, Any]
    kind: str = "dict"
    location: str | None = None
    notes: Mapping[Path, str] = field(default_factory=dict)


class Source(ABC):
    """Anything that can produce layers. Loading happens when the config is built."""

    @abstractmethod
    def load(self) -> list[Layer]:
        """Read the input and return its layers in precedence order, lowest first."""


class DictSource(Source):
    """Values supplied directly by the program, typically built-in defaults."""

    def __init__(self, data: Mapping[str, Any], *, name: str = "defaults") -> None:
        self.data = data
        self.name = name

    def load(self) -> list[Layer]:
        return [Layer(self.name, _normalize(self.data, (), self.name), kind="dict")]


def _normalize(value: Any, path: Path, name: str) -> Any:
    """Copy caller data into the plain dict/list model, rejecting non-string keys."""
    if isinstance(value, Mapping):
        out = {}
        for key, item in value.items():
            if not isinstance(key, str):
                where = format_path(path)
                raise SourceError(f"{name}: key {key!r} under {where} is not a string")
            out[key] = _normalize(item, path + (key,), name)
        return out
    if isinstance(value, (list, tuple)):
        return [_normalize(item, path + (i,), name) for i, item in enumerate(value)]
    return value


class FileSource(Source):
    """Base for sources backed by a single file."""

    kind = "file"

    def __init__(self, path: str | os.PathLike[str], *, optional: bool = False, name: str | None = None) -> None:
        self.path = FilePath(path)
        self.optional = optional
        self.name = name or str(self.path)

    def load(self) -> list[Layer]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            if self.optional:
                return []
            raise SourceError(f"{self.path}: file not found") from None
        except OSError as exc:
            raise SourceError(f"{self.path}: cannot read file: {exc.strerror or exc}") from exc
        text = self._decode(raw)
        data, notes = self.parse(text)
        return [Layer(self.name, data, kind=self.kind, location=str(self.path), notes=notes)]

    def _decode(self, raw: bytes) -> str:
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            before = raw[: exc.start]
            line = before.count(b"\n") + 1
            column = exc.start - (before.rfind(b"\n") + 1) + 1
            raise ConfigParseError(str(self.path), line, column, "file is not valid UTF-8") from exc

    @abstractmethod
    def parse(self, text: str) -> tuple[dict[str, Any], Mapping[Path, str]]:
        """Decode file contents into data and per-key notes."""

    def _parse_error(self, text: str, line: int, column: int, detail: str) -> ConfigParseError:
        lines = text.splitlines()
        snippet = lines[line - 1] if 0 < line <= len(lines) else None
        return ConfigParseError(str(self.path), line, column, detail, snippet)


_TOML_POSITION = re.compile(r"\s*\(at line (\d+), column (\d+)\)\s*$")
_TOML_AT_END = re.compile(r"\s*\(at end of document\)\s*$")


class TomlFile(FileSource):
    kind = "toml"

    def parse(self, text: str) -> tuple[dict[str, Any], Mapping[Path, str]]:
        try:
            return tomllib.loads(text), {}
        except tomllib.TOMLDecodeError as exc:
            raise self._toml_error(text, str(exc)) from exc

    def _toml_error(self, text: str, message: str) -> ConfigParseError:
        # tomllib only gained structured positions in 3.14, so read them from the message.
        position = _TOML_POSITION.search(message)
        if position:
            detail = message[: position.start()]
            return self._parse_error(text, int(position.group(1)), int(position.group(2)), detail)
        lines = text.splitlines() or [""]
        detail = _TOML_AT_END.sub("", message)
        return self._parse_error(text, len(lines), len(lines[-1]) + 1, detail)


class JsonFile(FileSource):
    kind = "json"

    def parse(self, text: str) -> tuple[dict[str, Any], Mapping[Path, str]]:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise self._parse_error(text, exc.lineno, exc.colno, exc.msg) from exc
        if not isinstance(data, dict):
            kind = type(data).__name__
            raise self._parse_error(text, 1, 1, f"top level must be an object, found {kind}")
        return data, {}


def env_items_to_tree(
    items: Iterable[tuple[str, str]],
    *,
    prefix: str = "",
    separator: str = "__",
    lowercase: bool = True,
    coerce: Coercer | None = coerce_scalar,
    origin: str = "environment",
) -> tuple[dict[str, Any], dict[Path, str]]:
    """Map NAME=value pairs to a nested table.

    With prefix ``APP``, ``APP_SERVER__PORT=80`` becomes ``server.port = 80``.
    A variable that is both a value and a parent of another variable is an
    error, since there is no sensible way to keep both.
    """
    lead = f"{prefix}_" if prefix else ""
    tree: dict[str, Any] = {}
    notes: dict[Path, str] = {}
    leaves: dict[Path, str] = {}
    branches: dict[Path, str] = {}
    for name, raw in sorted(items):
        if not name.startswith(lead) or len(name) == len(lead):
            continue
        key = name[len(lead) :]
        if lowercase:
            key = key.lower()
        path = tuple(key.split(separator))
        if not all(path):
            raise SourceError(f"{origin}: variable {name} has an empty key segment")
        clash = leaves.get(path) or branches.get(path)
        if clash is None:
            clash = next((leaves[path[:d]] for d in range(1, len(path)) if path[:d] in leaves), None)
        if clash is not None:
            raise SourceError(f"{origin}: variables {clash} and {name} conflict at {format_path(path)}")
        leaves[path] = name
        for depth in range(1, len(path)):
            branches.setdefault(path[:depth], name)
        assign(tree, path, coerce(raw) if coerce else raw)
        notes[path] = name
    return tree, notes


class EnvVars(Source):
    """Process environment variables selected by prefix."""

    def __init__(
        self,
        prefix: str,
        *,
        environ: Mapping[str, str] | None = None,
        separator: str = "__",
        lowercase: bool = True,
        coerce: Coercer | None = coerce_scalar,
        name: str | None = None,
    ) -> None:
        if not prefix:
            raise SourceError("EnvVars needs a non-empty prefix; reading every variable is never what you want")
        self.prefix = prefix
        self.environ = environ
        self.separator = separator
        self.lowercase = lowercase
        self.coerce = coerce
        self.name = name or f"env:{prefix}"

    def load(self) -> list[Layer]:
        environ = os.environ if self.environ is None else self.environ
        data, notes = env_items_to_tree(
            environ.items(),
            prefix=self.prefix,
            separator=self.separator,
            lowercase=self.lowercase,
            coerce=self.coerce,
            origin=self.name,
        )
        return [Layer(self.name, data, kind="env", notes=notes)]


class EnvFile(FileSource):
    """A dotenv-style file, read with the same mapping rules as real variables.

    Quoting follows POSIX shell rules, so a file that can be sourced by a shell
    gives the same values here. ``$VAR`` expansion is deliberately not done.
    """

    kind = "envfile"

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        prefix: str = "",
        optional: bool = False,
        name: str | None = None,
        separator: str = "__",
        lowercase: bool = True,
        coerce: Coercer | None = coerce_scalar,
    ) -> None:
        super().__init__(path, optional=optional, name=name)
        self.prefix = prefix
        self.separator = separator
        self.lowercase = lowercase
        self.coerce = coerce

    def parse(self, text: str) -> tuple[dict[str, Any], Mapping[Path, str]]:
        pairs: list[tuple[str, str]] = []
        lines: dict[str, int] = {}
        for number, line in enumerate(text.splitlines(), start=1):
            parsed = self._parse_line(text, line, number)
            if parsed is not None:
                pairs.append(parsed)
                lines[parsed[0]] = number
        # A later assignment of the same name wins, as it would in a shell.
        data, notes = env_items_to_tree(
            dict(pairs).items(),
            prefix=self.prefix,
            separator=self.separator,
            lowercase=self.lowercase,
            coerce=self.coerce,
            origin=str(self.path),
        )
        return data, {path: f"{var}, line {lines[var]}" for path, var in notes.items()}

    _ASSIGNMENT = re.compile(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=[ \t]*")

    def _parse_line(self, text: str, line: str, number: int) -> tuple[str, str] | None:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            return None
        match = self._ASSIGNMENT.match(line)
        if match is None:
            column = len(line) - len(line.lstrip()) + 1
            raise self._parse_error(text, number, column, "expected NAME=value")
        return match.group(1), self._read_value(text, line, match.end(), number)

    def _read_value(self, text: str, line: str, start: int, number: int) -> str:
        out: list[str] = []
        i = start
        while i < len(line):
            ch = line[i]
            if ch == "'":
                close = line.find("'", i + 1)
                if close < 0:
                    raise self._parse_error(text, number, i + 1, "unterminated single quote")
                out.append(line[i + 1 : close])
                i = close + 1
            elif ch == '"':
                chunk, i = self._read_double_quoted(text, line, i, number)
                out.append(chunk)
            elif ch == "\\" and i + 1 < len(line):
                out.append(line[i + 1])
                i += 2
            elif ch == "#" and (i == start or line[i - 1] in " \t"):
                break
            elif ch in " \t":
                # Blanks count only if something other than a comment follows.
                rest = line[i:].lstrip(" \t")
                if not rest or rest.startswith("#"):
                    break
                out.append(ch)
                i += 1
            else:
                out.append(ch)
                i += 1
        return "".join(out)

    def _read_double_quoted(self, text: str, line: str, start: int, number: int) -> tuple[str, int]:
        out: list[str] = []
        i = start + 1
        while i < len(line):
            ch = line[i]
            if ch == '"':
                return "".join(out), i + 1
            if ch == "\\" and i + 1 < len(line) and line[i + 1] in '"\\$`':
                out.append(line[i + 1])
                i += 2
            else:
                out.append(ch)
                i += 1
        raise self._parse_error(text, number, start + 1, "unterminated double quote")


class CliOverrides(Source):
    """Overrides of the form ``server.port=8080``, in the order given."""

    def __init__(self, items: Iterable[str], *, name: str = "cli", coerce: Coercer | None = coerce_scalar) -> None:
        self.items = list(items)
        self.name = name
        self.coerce = coerce

    def load(self) -> list[Layer]:
        data: dict[str, Any] = {}
        notes: dict[Path, str] = {}
        for item in self.items:
            path, raw = self._split(item)
            assign(data, path, self.coerce(raw) if self.coerce else raw)
            notes[path] = item
        return [Layer(self.name, data, kind="cli", notes=notes)]

    def _split(self, item: str) -> tuple[Path, str]:
        position = _find_equals(item)
        if position < 0:
            raise SourceError(f"{self.name}: override {item!r} is not of the form key=value")
        try:
            path = parse_path(item[:position])
        except PathSyntaxError as exc:
            raise SourceError(f"{self.name}: {exc}") from exc
        if any(isinstance(segment, int) for segment in path):
            raise SourceError(f"{self.name}: override {item!r} cannot target a list index; set the whole list")
        return path, item[position + 1 :]


def _find_equals(item: str) -> int:
    """Position of the first ``=`` outside a quoted key, or -1."""
    quoted = False
    i = 0
    while i < len(item):
        ch = item[i]
        if ch == "\\" and quoted:
            i += 2
            continue
        if ch == '"':
            quoted = not quoted
        elif ch == "=" and not quoted:
            return i
        i += 1
    return -1


class Directory(Source):
    """A directory of fragments, applied in sorted filename order, one layer each."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        optional: bool = False,
        suffixes: Iterable[str] = (".toml", ".json"),
        recursive: bool = False,
    ) -> None:
        self.path = FilePath(path)
        self.optional = optional
        self.suffixes = tuple(suffixes)
        self.recursive = recursive

    def load(self) -> list[Layer]:
        if not self.path.exists():
            if self.optional:
                return []
            raise SourceError(f"{self.path}: directory not found")
        if not self.path.is_dir():
            raise SourceError(f"{self.path}: not a directory")
        entries = self.path.rglob("*") if self.recursive else self.path.iterdir()
        fragments = sorted(
            entry
            for entry in entries
            if entry.is_file() and not entry.name.startswith(".") and entry.suffix in self.suffixes
        )
        layers: list[Layer] = []
        for fragment in fragments:
            layers.extend(file_source(fragment).load())
        return layers


def file_source(path: str | os.PathLike[str], *, optional: bool = False, name: str | None = None) -> FileSource:
    """Choose a file source from the file name."""
    file_path = FilePath(path)
    suffix = file_path.suffix.lower()
    if suffix == ".toml":
        return TomlFile(file_path, optional=optional, name=name)
    if suffix == ".json":
        return JsonFile(file_path, optional=optional, name=name)
    if suffix == ".env" or file_path.name == ".env":
        return EnvFile(file_path, optional=optional, name=name)
    raise SourceError(f"{file_path}: unsupported file type (expected .toml, .json or .env)")
