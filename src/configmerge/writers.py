"""Output formats: JSON, TOML and env-file.

The TOML and env-file writers aim to round-trip through the matching sources:
what they produce reads back as the same data, apart from the limits of each
format (TOML has no null; env-file keys are folded to one case).
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
from typing import Any, Iterator, Mapping

from .coerce import coerce_scalar
from .errors import WriterError
from .paths import Path, format_path

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SHELL_SAFE = re.compile(r"[A-Za-z0-9_@%+=:,./-]+")

_TOML_ESCAPES = {
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
    '"': '\\"',
    "\\": "\\\\",
}


def to_json(data: Any, *, indent: int | None = 2, sort_keys: bool = False) -> str:
    """Serialize to JSON. Dates become ISO 8601 strings; nan and inf are refused."""
    return json.dumps(
        _json_ready(data, ()),
        indent=indent,
        sort_keys=sort_keys,
        ensure_ascii=False,
        allow_nan=False,
    )


def _json_ready(value: Any, path: Path) -> Any:
    if isinstance(value, Mapping):
        return {key: _json_ready(item, path + (key,)) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item, path + (i,)) for i, item in enumerate(value)]
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        raise WriterError(f"{format_path(path)}: JSON cannot represent {value}")
    if value is None or isinstance(value, (str, int, float)):
        return value
    raise WriterError(f"{format_path(path)}: cannot write {type(value).__name__} as JSON")


def to_toml(data: Mapping[str, Any], *, drop_null: bool = True) -> str:
    """Serialize to TOML.

    TOML has no null. Null table entries are dropped, or raise WriterError if
    ``drop_null`` is false. A null inside a list always raises, as dropping it
    would shift the other elements.
    """
    lines = list(_table_lines(data, (), drop_null))
    return "\n".join(lines).strip("\n") + "\n" if lines else ""


def _is_table_array(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(item, Mapping) for item in value)


def _table_lines(table: Mapping[str, Any], path: Path, drop_null: bool) -> Iterator[str]:
    subtables = []
    for key, value in table.items():
        if value is None:
            if not drop_null:
                raise WriterError(f"{format_path(path + (key,))}: TOML cannot represent null")
        elif isinstance(value, Mapping) or _is_table_array(value):
            subtables.append((key, value))
        else:
            yield f"{_toml_key(key)} = {_toml_inline(value, path + (key,), drop_null)}"
    for key, value in subtables:
        child = path + (key,)
        header = ".".join(_toml_key(str(segment)) for segment in child)
        if isinstance(value, Mapping):
            has_own_values = not value or any(
                not isinstance(item, Mapping) and not _is_table_array(item) for item in value.values()
            )
            if has_own_values:
                yield ""
                yield f"[{header}]"
            yield from _table_lines(value, child, drop_null)
        else:
            for item in value:
                yield ""
                yield f"[[{header}]]"
                yield from _table_lines(item, child, drop_null)


def _toml_key(key: str) -> str:
    return key if _BARE_KEY.fullmatch(key) else _toml_string(key)


def _toml_string(text: str) -> str:
    out = ['"']
    for ch in text:
        if ch in _TOML_ESCAPES:
            out.append(_TOML_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _toml_inline(value: Any, path: Path, drop_null: bool) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _toml_float(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, list):
        items = (_toml_inline(item, path + (i,), drop_null) for i, item in enumerate(value))
        return "[" + ", ".join(items) + "]"
    if isinstance(value, Mapping):
        pairs = []
        for key, item in value.items():
            if item is None and drop_null:
                continue
            pairs.append(f"{_toml_key(key)} = {_toml_inline(item, path + (key,), drop_null)}")
        return "{" + ", ".join(pairs) + "}" if pairs else "{}"
    if value is None:
        raise WriterError(f"{format_path(path)}: TOML cannot represent null")
    raise WriterError(f"{format_path(path)}: cannot write {type(value).__name__} as TOML")


def _toml_float(value: float) -> str:
    if math.isnan(value):
        return "nan"
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    return repr(value)


def to_env(data: Mapping[str, Any], *, prefix: str = "", separator: str = "__") -> str:
    """Serialize to env-file lines, nesting tables with the separator.

    Output can be sourced by a POSIX shell. Lists are written as JSON. Null values
    are skipped. Keys that are not valid in a variable name raise WriterError.
    """
    lead = f"{prefix}_" if prefix else ""
    lines = [f"{name}={value}" for name, value in _env_pairs(data, (), lead, separator)]
    return "\n".join(lines) + "\n" if lines else ""


def _env_pairs(table: Mapping[str, Any], path: Path, lead: str, separator: str) -> Iterator[tuple[str, str]]:
    for key, value in table.items():
        child = path + (key,)
        if not _ENV_KEY.fullmatch(key):
            raise WriterError(f"{format_path(child)}: key {key!r} cannot be part of a variable name")
        if value is None:
            continue
        if isinstance(value, Mapping) and value:
            yield from _env_pairs(value, child, lead, separator)
        else:
            name = lead + separator.join(str(segment) for segment in child).upper()
            yield name, _shell_quote(_env_text(value, child))


def _env_text(value: Any, path: Path) -> str:
    """Text which, once the shell has removed quoting, coerces back to value."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        unchanged = coerce_scalar(value) == value and type(coerce_scalar(value)) is str
        if unchanged and not re.search(r"[\x00-\x1f\x7f]", value):
            return value
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise WriterError(f"{format_path(path)}: env-file output cannot represent {value}")
        return _env_number(value)
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (list, Mapping)):
        return json.dumps(_json_ready(value, path), ensure_ascii=False)
    raise WriterError(f"{format_path(path)}: cannot write {type(value).__name__} to an env file")


def _env_number(value: int | float) -> str:
    text = repr(value)
    # coerce_scalar only reads floats that have a dot, and repr(1e16) has none.
    if isinstance(value, float) and "e" in text and "." not in text:
        return text.replace("e", ".0e")
    return text


def _shell_quote(text: str) -> str:
    if _SHELL_SAFE.fullmatch(text):
        return text
    return "'" + text.replace("'", "'\\''") + "'"
