"""The result of a load: a read-only view with dotted lookups and typed getters."""

from __future__ import annotations

import copy
import datetime as dt
import difflib
import pathlib
from collections.abc import Mapping
from typing import Any, Iterator

from . import writers
from .coerce import coerce_to
from .errors import ConfigKeyError, ConfigTypeError, PathSyntaxError
from .paths import MISSING, Path, as_path, format_path, iter_leaves, lookup
from .provenance import Explanation, Provenance
from .redact import BoundRedactor, Redactor, tainted_paths
from .units import parse_duration, parse_size


class Config(Mapping):
    """Resolved configuration. Lookups hand out copies, so it cannot be changed by accident.

    ``config["server.port"]`` and ``config.get("server.port")`` both accept
    dotted paths. A Config also works as a plain mapping over its top-level keys.
    """

    def __init__(
        self,
        data: Mapping[str, Any],
        *,
        provenance: Provenance | None = None,
        redactor: Redactor | None = None,
        _prefix: Path = (),
        _root: Mapping[str, Any] | None = None,
    ) -> None:
        self._data = data
        self._provenance = provenance or Provenance()
        self._redactor = redactor or Redactor()
        self._prefix = _prefix
        self._root = data if _root is None else _root
        self._bound: BoundRedactor | None = None

    def _key(self, path: str | Path) -> Path:
        """Path for a lookup. A top-level key is taken literally, even with dots in it."""
        if isinstance(path, str) and path in self._data:
            return (path,)
        return as_path(path)

    def __getitem__(self, path: str | Path) -> Any:
        return copy.deepcopy(self._require(self._key(path)))

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, path: object) -> bool:
        if not isinstance(path, (str, tuple)):
            return False
        try:
            return lookup(self._data, self._key(path)) is not MISSING
        except PathSyntaxError:
            return False

    def __repr__(self) -> str:
        return f"Config({self.redacted()!r})"

    def get(self, path: str | Path, default: Any = None) -> Any:
        value = lookup(self._data, self._key(path))
        return default if value is MISSING else copy.deepcopy(value)

    def _require(self, path: Path) -> Any:
        value = lookup(self._data, path)
        if value is MISSING:
            raise ConfigKeyError(self._missing_message(path))
        return value

    def _missing_message(self, path: Path) -> str:
        name = format_path(self._prefix + path)
        known = [format_path(self._prefix + leaf) for leaf, _ in iter_leaves(self._data)]
        close = difflib.get_close_matches(name, known, n=1)
        return f"no such key: {name}" + (f" (did you mean {close[0]}?)" if close else "")

    def _typed(self, path: str | Path, default: Any, label: str, convert: Any) -> Any:
        parsed = self._key(path)
        value = lookup(self._data, parsed)
        if value is MISSING:
            if default is MISSING:
                raise ConfigKeyError(self._missing_message(parsed))
            return default
        try:
            return convert(value)
        except (TypeError, ValueError) as exc:
            name = format_path(self._prefix + parsed)
            shown = self._mask()(self._prefix + parsed, value)
            raise ConfigTypeError(f"{name}: expected {label}, got {shown!r}") from exc

    def get_str(self, path: str | Path, default: Any = MISSING) -> str:
        return self._typed(path, default, "a string", _to_str)

    def get_int(self, path: str | Path, default: Any = MISSING) -> int:
        return self._typed(path, default, "an integer", _to_int)

    def get_float(self, path: str | Path, default: Any = MISSING) -> float:
        return self._typed(path, default, "a number", _to_float)

    def get_bool(self, path: str | Path, default: Any = MISSING) -> bool:
        return self._typed(path, default, "a boolean", _to_bool)

    def get_list(self, path: str | Path, default: Any = MISSING, *, item_type: type | None = None) -> list[Any]:
        def convert(value: Any) -> list[Any]:
            if not isinstance(value, list):
                raise TypeError("not a list")
            if item_type is not None and not all(_is_instance(item, item_type) for item in value):
                raise TypeError(f"not all elements are {item_type.__name__}")
            return copy.deepcopy(value)

        label = f"a list of {item_type.__name__}" if item_type else "a list"
        return self._typed(path, default, label, convert)

    def get_path(self, path: str | Path, default: Any = MISSING) -> pathlib.Path:
        return self._typed(path, default, "a file path", lambda value: pathlib.Path(_to_str(value)))

    def get_duration(self, path: str | Path, default: Any = MISSING) -> dt.timedelta:
        """A timedelta from ``"1h30m"``-style text, or a plain number of seconds."""
        return self._typed(path, default, "a duration", _to_duration)

    def get_size(self, path: str | Path, default: Any = MISSING) -> int:
        """A byte count from ``"10MiB"``-style text, or a plain integer."""
        return self._typed(path, default, "a size", _to_size)

    def section(self, path: str | Path) -> "Config":
        """A view of one table. explain() and error messages keep using full paths."""
        parsed = self._key(path)
        value = self._require(parsed)
        if not isinstance(value, Mapping):
            raise ConfigTypeError(f"{format_path(self._prefix + parsed)}: expected a table")
        return Config(
            value,
            provenance=self._provenance,
            redactor=self._redactor,
            _prefix=self._prefix + parsed,
            _root=self._root,
        )

    def to_dict(self, *, redact: bool = False) -> dict[str, Any]:
        """A deep copy of the values, optionally with secrets masked."""
        return self.redacted() if redact else copy.deepcopy(dict(self._data))

    def redacted(self) -> dict[str, Any]:
        return self._mask().tree(self._data, self._prefix)

    def flatten(self, *, redact: bool = False) -> dict[str, Any]:
        """A single-level mapping from path text to leaf value."""
        source = self.redacted() if redact else self._data
        return {format_path(path): copy.deepcopy(value) for path, value in iter_leaves(source)}

    def explain(self, path: str | Path) -> Explanation:
        """Which layer set this key, and what it replaced."""
        return self._provenance.explain(self._prefix + self._key(path), self._root)

    def explain_text(self, path: str | Path) -> str:
        """explain() rendered for display, with secrets masked."""
        return self.explain(path).render(self._mask())

    @property
    def mask(self) -> BoundRedactor:
        """Callable hiding secrets in values taken from this config."""
        return self._mask()

    @property
    def layers(self) -> list[str]:
        """Names of the layers applied, lowest precedence first."""
        return list(self._provenance.layers)

    def _mask(self) -> BoundRedactor:
        if self._bound is None:
            tainted = tainted_paths(self._provenance.interpolations(), self._redactor, self._root)
            self._bound = self._redactor.bind(self._root, tainted)
        return self._bound

    def to_json(self, *, redact: bool = False, indent: int | None = 2, sort_keys: bool = False) -> str:
        return writers.to_json(self.to_dict(redact=redact), indent=indent, sort_keys=sort_keys)

    def to_toml(self, *, redact: bool = False) -> str:
        return writers.to_toml(self.to_dict(redact=redact))

    def to_env(self, *, redact: bool = False, prefix: str = "") -> str:
        return writers.to_env(self.to_dict(redact=redact), prefix=prefix)


def _is_instance(value: Any, kind: type) -> bool:
    if isinstance(value, bool) and kind is not bool:
        return False
    return isinstance(value, kind)


def _to_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    raise TypeError("not a string")


def _to_int(value: Any) -> int:
    if isinstance(value, bool):
        raise TypeError("boolean is not an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        return coerce_to(int, value)
    raise TypeError("not an integer")


def _to_float(value: Any) -> float:
    if isinstance(value, bool):
        raise TypeError("boolean is not a number")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return coerce_to(float, value)
    raise TypeError("not a number")


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return coerce_to(bool, value)
    raise TypeError("not a boolean")


def _to_duration(value: Any) -> dt.timedelta:
    if isinstance(value, bool):
        raise TypeError("boolean is not a duration")
    if isinstance(value, (int, float)):
        return dt.timedelta(seconds=value)
    if isinstance(value, str):
        return parse_duration(value)
    raise TypeError("not a duration")


def _to_size(value: Any) -> int:
    if isinstance(value, bool):
        raise TypeError("boolean is not a size")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return parse_size(value)
    raise TypeError("not a size")
