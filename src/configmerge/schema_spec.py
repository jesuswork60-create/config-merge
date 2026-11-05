"""Schemas written as data, so the command line can read them from a TOML or JSON file.

Each entry is either a nested table of further entries, a type string, or a
table with a ``type`` key and constraints::

    log_level = { type = "str", choices = ["debug", "info"], default = "info" }
    workers = "int?"            # optional
    hosts = "list[str]"

    [server]
    host = "str"
    port = { type = "int", min = 1, max = 65535 }

A key that happens to be called ``type`` can be declared through the explicit
form ``{ type = "object", fields = { type = "str" } }``.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Mapping

from .errors import SchemaDefinitionError
from .paths import MISSING
from .schema import Field, Schema

_TYPES: dict[str, Any] = {
    "str": str,
    "string": str,
    "int": int,
    "integer": int,
    "float": float,
    "number": (int, float),
    "bool": bool,
    "boolean": bool,
    "list": list,
    "object": dict,
    "table": dict,
    "map": dict,
    "any": None,
    "date": dt.date,
    "datetime": dt.datetime,
    "time": dt.time,
}

_FIELD_OPTIONS = {
    "type",
    "required",
    "default",
    "choices",
    "min",
    "max",
    "pattern",
    "items",
    "fields",
    "values",
    "unknown",
    "nullable",
    "secret",
    "doc",
}

_CONTAINER = re.compile(r"(list|map)\[(.*)\]\Z")


def schema_from_spec(spec: Mapping[str, Any], *, unknown: str = "forbid", coerce: bool = False) -> Schema:
    """Build a Schema from a parsed spec mapping."""
    if not isinstance(spec, Mapping):
        raise SchemaDefinitionError("a schema spec must be a table")
    fields = {name: _read_entry(entry, name) for name, entry in spec.items()}
    return Schema(fields, unknown=unknown, coerce=coerce)


def _read_entry(entry: Any, where: str) -> Field:
    if isinstance(entry, str):
        return _read_shorthand(entry, where)
    if isinstance(entry, list):
        if len(entry) != 1:
            raise SchemaDefinitionError(f"{where}: list shorthand needs exactly one element")
        return Field(list, items=_read_entry(entry[0], f"{where}[]"))
    if not isinstance(entry, Mapping):
        raise SchemaDefinitionError(f"{where}: cannot read schema entry {entry!r}")
    if isinstance(entry.get("type"), str):
        return _read_descriptor(entry, where)
    return Field(dict, fields={name: _read_entry(item, f"{where}.{name}") for name, item in entry.items()})


def _read_shorthand(text: str, where: str) -> Field:
    spelled = text.strip()
    optional = spelled.endswith("?")
    if optional:
        spelled = spelled[:-1].strip()
    required = False if optional else None
    container = _CONTAINER.match(spelled)
    if container:
        inner = _read_shorthand(container.group(2), f"{where}[]")
        if container.group(1) == "list":
            return Field(list, items=inner, required=required)
        return Field(dict, values=inner, required=required)
    kinds = [_type_named(part.strip(), where) for part in spelled.split("|")]
    return Field(_flatten_union(kinds), required=required)


def _type_named(name: str, where: str) -> Any:
    if name not in _TYPES:
        known = ", ".join(sorted(_TYPES))
        raise SchemaDefinitionError(f"{where}: unknown type {name!r} (known: {known})")
    return _TYPES[name]


def _flatten_union(kinds: list[Any]) -> Any:
    if len(kinds) == 1:
        return kinds[0]
    if any(kind is None for kind in kinds):
        return None
    flat: list[type] = []
    for kind in kinds:
        flat.extend(kind if isinstance(kind, tuple) else (kind,))
    return tuple(flat)


def _read_descriptor(entry: Mapping[str, Any], where: str) -> Field:
    extra = set(entry) - _FIELD_OPTIONS
    if extra:
        raise SchemaDefinitionError(f"{where}: unknown field options: {', '.join(sorted(extra))}")
    base = _read_shorthand(entry["type"], where)
    options: dict[str, Any] = {k: entry[k] for k in ("choices", "min", "max", "pattern", "unknown", "doc") if k in entry}
    if "fields" in entry:
        fields = entry["fields"]
        if not isinstance(fields, Mapping):
            raise SchemaDefinitionError(f"{where}: fields must be a table")
        options["fields"] = {name: _read_entry(item, f"{where}.{name}") for name, item in fields.items()}
    else:
        options["fields"] = base.fields or None
    if "items" in entry:
        options["items"] = _read_entry(entry["items"], f"{where}[]")
    else:
        options["items"] = base.items
    if "values" in entry:
        options["values"] = _read_entry(entry["values"], f"{where}.*")
    else:
        options["values"] = base.values
    return Field(
        base.types,
        required=entry.get("required", base.required),
        default=entry.get("default", MISSING),
        nullable=entry.get("nullable", False),
        secret=entry.get("secret", False),
        **options,
    )
