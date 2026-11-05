"""A small schema language for checking a resolved configuration.

Schemas are written as plain Python structures, with Field for anything beyond
a bare type::

    Schema({
        "server": {
            "host": Field(str, default="localhost"),
            "port": Field(int, min=1, max=65535),
            "tags": [str],
        },
        "log_level": Field(str, choices=["debug", "info", "warn"], default="info"),
        "db": {"password": Field(str, secret=True)},
    })

Validation never stops at the first problem: every issue is collected and
reported with its full path, for example ``server.ports[1]``.
"""

from __future__ import annotations

import copy
import datetime as dt
import difflib
import math
import re
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from .coerce import coerce_to
from .errors import SchemaDefinitionError, ValidationError
from .paths import MISSING, Path, PathPattern, format_path

UNKNOWN_POLICIES = ("forbid", "allow", "strip")

_TYPE_LABELS = {
    str: "string",
    int: "integer",
    float: "float",
    bool: "boolean",
    list: "list",
    dict: "table",
    dt.datetime: "datetime",
    dt.date: "date",
    dt.time: "time",
}


@dataclass(frozen=True)
class Issue:
    """One validation problem. ``origin`` names the layer that set the value, if known."""

    path: Path
    code: str
    message: str
    origin: str | None = None

    def __str__(self) -> str:
        text = f"{format_path(self.path)}: {self.message}"
        return f"{text} (set by {self.origin})" if self.origin else text

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": format_path(self.path),
            "code": self.code,
            "message": self.message,
            "origin": self.origin,
        }


class Field:
    """Constraints for one value.

    ``type`` is a Python type, a tuple of types, or omitted for any type.
    ``min``/``max`` bound numbers, and the length of strings, lists and tables.
    ``pattern`` must match the whole string. ``check`` is a callable returning
    False or an error message on failure; a ValueError it raises counts too.
    A field is required unless it has a default or ``required=False`` is given.
    """

    def __init__(
        self,
        type: Any = None,
        *,
        required: bool | None = None,
        default: Any = MISSING,
        choices: Iterable[Any] | None = None,
        min: float | None = None,
        max: float | None = None,
        pattern: str | None = None,
        items: Any = None,
        fields: Mapping[str, Any] | None = None,
        values: Any = None,
        unknown: str | None = None,
        nullable: bool = False,
        secret: bool = False,
        check: Callable[[Any], Any] | None = None,
        doc: str | None = None,
    ) -> None:
        if type is None:
            type = dict if fields is not None or values is not None else list if items is not None else None
        self.types = _normalize_types(type)
        self.required = required
        self.default = default
        self.choices = list(choices) if choices is not None else None
        self.min = min
        self.max = max
        self.items = as_field(items) if items is not None else None
        self.fields = {name: as_field(spec) for name, spec in (fields or {}).items()}
        self.values = as_field(values) if values is not None else None
        self.unknown = unknown
        self.nullable = nullable
        self.secret = secret
        self.check = check
        self.doc = doc
        self.pattern_text = pattern
        self.pattern = self._compile(pattern)
        self._validate_definition()

    @staticmethod
    def _compile(pattern: str | None) -> re.Pattern[str] | None:
        if pattern is None:
            return None
        try:
            return re.compile(pattern)
        except re.error as exc:
            raise SchemaDefinitionError(f"invalid pattern {pattern!r}: {exc}") from exc

    def _validate_definition(self) -> None:
        if self.unknown is not None and self.unknown not in UNKNOWN_POLICIES:
            raise SchemaDefinitionError(f"unknown-key policy must be one of {UNKNOWN_POLICIES}, got {self.unknown!r}")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise SchemaDefinitionError(f"min {self.min} is greater than max {self.max}")
        if self.choices is not None and not self.choices:
            raise SchemaDefinitionError("choices must not be empty")
        if self.required is True and self.has_default:
            raise SchemaDefinitionError("a field cannot be required and have a default")
        if self.items is not None and not self.allows(list):
            raise SchemaDefinitionError("items only applies to list fields")
        if (self.fields or self.values is not None) and not self.allows(dict):
            raise SchemaDefinitionError("fields and values only apply to table fields")
        if self.pattern is not None and not self.allows(str):
            raise SchemaDefinitionError("pattern only applies to string fields")

    def allows(self, kind: type) -> bool:
        return self.types is None or kind in self.types

    @property
    def has_default(self) -> bool:
        return self.default is not MISSING

    @property
    def is_table(self) -> bool:
        return self.types is not None and dict in self.types and bool(self.fields)

    @property
    def is_required(self) -> bool:
        return self.required if self.required is not None else not self.has_default

    @property
    def type_label(self) -> str:
        if self.types is None:
            return "any"
        return " or ".join(_TYPE_LABELS.get(t, t.__name__) for t in self.types)


def _normalize_types(spec: Any) -> tuple[type, ...] | None:
    if spec is None or spec is Any or spec is object:
        return None
    candidates = spec if isinstance(spec, tuple) else (spec,)
    for candidate in candidates:
        if not isinstance(candidate, type):
            raise SchemaDefinitionError(f"not a type: {candidate!r}")
    return tuple(candidates)


def as_field(spec: Any) -> Field:
    """Expand the shorthand forms: a type, a tuple of types, ``[item]``, or a nested dict."""
    if isinstance(spec, Field):
        return spec
    if isinstance(spec, Mapping):
        return Field(dict, fields=spec)
    if isinstance(spec, list):
        if len(spec) != 1:
            raise SchemaDefinitionError(f"list shorthand needs exactly one item spec, got {spec!r}")
        return Field(list, items=spec[0])
    if isinstance(spec, (type, tuple)) or spec is Any:
        return Field(spec)
    raise SchemaDefinitionError(f"cannot read schema entry {spec!r}")


def _matches_type(value: Any, kind: type) -> bool:
    if kind is bool:
        return isinstance(value, bool)
    if kind is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if kind is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind is dt.date:
        return isinstance(value, dt.date) and not isinstance(value, dt.datetime)
    return isinstance(value, kind)


def _same_choice(value: Any, choice: Any) -> bool:
    return isinstance(value, bool) == isinstance(choice, bool) and value == choice


def _summarize(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def _describe_kind(value: Any) -> str:
    for kind, label in _TYPE_LABELS.items():
        if _matches_type(value, kind) and kind is not float:
            return label
    if isinstance(value, float):
        return "float"
    return "null" if value is None else type(value).__name__


@dataclass(frozen=True)
class ValidationResult:
    value: dict[str, Any]
    issues: tuple[Issue, ...]

    @property
    def ok(self) -> bool:
        return not self.issues


class _Validator:
    def __init__(self, *, coerce: bool, is_secret: Callable[[Path], bool] | None) -> None:
        self.coerce = coerce
        self.is_secret = is_secret or (lambda path: False)
        self.issues: list[Issue] = []

    def issue(self, path: Path, code: str, message: str) -> None:
        self.issues.append(Issue(path, code, message))

    def show(self, value: Any, path: Path, secret: bool) -> str:
        """A value for use in a message. Secrets show only what kind of thing they are."""
        if secret or self.is_secret(path):
            return f"a {_describe_kind(value)} (value hidden)"
        return _summarize(value)

    def check(self, field: Field, value: Any, path: Path, unknown: str, secret: bool) -> Any:
        secret = secret or field.secret
        if value is None:
            if not field.nullable:
                self.issue(path, "type", f"expected {field.type_label}, got null")
            return None
        value, ok = self._check_type(field, value, path, secret)
        if not ok:
            return value
        self._check_constraints(field, value, path, secret)
        if isinstance(value, dict):
            return self._check_table(field, value, path, unknown, secret)
        if isinstance(value, list) and field.items is not None:
            return [self.check(field.items, item, path + (i,), unknown, secret) for i, item in enumerate(value)]
        return value

    def _check_type(self, field: Field, value: Any, path: Path, secret: bool) -> tuple[Any, bool]:
        if field.types is None:
            return value, True
        for kind in field.types:
            if _matches_type(value, kind):
                # An int given for a pure float field is a float from here on.
                if field.types == (float,) and not isinstance(value, float):
                    return float(value), True
                return value, True
        if self.coerce and isinstance(value, str):
            for kind in field.types:
                try:
                    return coerce_to(kind, value), True
                except ValueError:
                    continue
        shown = self.show(value, path, secret)
        self.issue(path, "type", f"expected {field.type_label}, got {shown}")
        return value, False

    def _check_constraints(self, field: Field, value: Any, path: Path, secret: bool) -> None:
        if field.choices is not None and not any(_same_choice(value, choice) for choice in field.choices):
            options = ", ".join(repr(choice) for choice in field.choices)
            self.issue(path, "choice", f"must be one of {options}; got {self.show(value, path, secret)}")
        self._check_bounds(field, value, path, secret)
        if field.pattern is not None and isinstance(value, str) and not field.pattern.fullmatch(value):
            self.issue(path, "pattern", f"does not match pattern {field.pattern_text!r}; got {self.show(value, path, secret)}")
        if field.check is not None:
            self._run_check(field, value, path)

    def _check_bounds(self, field: Field, value: Any, path: Path, secret: bool) -> None:
        if field.min is None and field.max is None:
            return
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            measure, label = value, self.show(value, path, secret)
            if isinstance(value, float) and math.isnan(value):
                self.issue(path, "range", "must be a number within bounds; got nan")
                return
        elif isinstance(value, (str, list, dict)):
            measure, label = len(value), f"length {len(value)}"
        else:
            return
        if field.min is not None and measure < field.min:
            self.issue(path, "min", f"must be at least {field.min}; got {label}")
        if field.max is not None and measure > field.max:
            self.issue(path, "max", f"must be at most {field.max}; got {label}")

    def _run_check(self, field: Field, value: Any, path: Path) -> None:
        try:
            outcome = field.check(value)  # type: ignore[misc]
        except ValueError as exc:
            self.issue(path, "check", str(exc))
            return
        if outcome is False:
            self.issue(path, "check", "failed custom check")
        elif isinstance(outcome, str):
            self.issue(path, "check", outcome)

    def _check_table(self, field: Field, table: dict[str, Any], path: Path, unknown: str, secret: bool) -> dict[str, Any]:
        policy = field.unknown or unknown
        out: dict[str, Any] = {}
        for name, child in field.fields.items():
            child_path = path + (name,)
            if name in table:
                out[name] = self.check(child, table[name], child_path, policy, secret)
            else:
                self._check_missing(child, name, child_path, out, policy, secret)
        for name, value in table.items():
            if name in field.fields:
                continue
            child_path = path + (name,)
            if field.values is not None:
                out[name] = self.check(field.values, value, child_path, policy, secret)
            elif not field.fields or policy == "allow":
                # A plain table with no declared keys holds whatever it likes.
                out[name] = value
            elif policy == "forbid":
                self.issue(child_path, "unknown", self._unknown_message(name, field))
        return out

    def _check_missing(
        self, child: Field, name: str, path: Path, out: dict[str, Any], unknown: str, secret: bool
    ) -> None:
        if child.required is False:
            return
        if child.is_table and child.required is None:
            # Absent table with no opinion of its own: its children decide.
            nested = self._check_table(child, {}, path, unknown, secret or child.secret)
            if nested:
                out[name] = nested
            return
        if child.is_required:
            self.issue(path, "missing", f"required {child.type_label} is missing")

    @staticmethod
    def _unknown_message(name: str, field: Field) -> str:
        close = difflib.get_close_matches(name, list(field.fields), n=1)
        return f"unknown key (did you mean {close[0]!r}?)" if close else "unknown key"


class Schema:
    """A compiled schema. ``unknown`` sets what happens to undeclared keys by default."""

    def __init__(self, spec: Mapping[str, Any] | Field, *, unknown: str = "forbid", coerce: bool = False) -> None:
        if unknown not in UNKNOWN_POLICIES:
            raise SchemaDefinitionError(f"unknown-key policy must be one of {UNKNOWN_POLICIES}, got {unknown!r}")
        self.root = as_field(spec)
        if self.root.types != (dict,):
            raise SchemaDefinitionError("the top level of a schema must be a table")
        self.unknown = unknown
        self.coerce = coerce

    def apply_defaults(
        self, data: Mapping[str, Any], on_default: Callable[[Path, Any], None] | None = None
    ) -> dict[str, Any]:
        """Return data with declared defaults filled in. Performs no checking."""
        return _fill_table(self.root, data, (), on_default)

    def validate(self, data: Mapping[str, Any], *, is_secret: Callable[[Path], bool] | None = None) -> ValidationResult:
        """Check data and collect all issues.

        The returned value has defaults filled, values coerced if the schema asks
        for that, and keys removed under the ``strip`` policy. ``is_secret``
        names extra paths whose values must stay out of messages.
        """
        filled = self.apply_defaults(data)
        validator = _Validator(coerce=self.coerce, is_secret=is_secret)
        value = validator.check(self.root, filled, (), self.unknown, False)
        return ValidationResult(value, tuple(validator.issues))

    def check(self, data: Mapping[str, Any], *, is_secret: Callable[[Path], bool] | None = None) -> dict[str, Any]:
        """Like validate, but raises ValidationError when there are issues."""
        result = self.validate(data, is_secret=is_secret)
        if not result.ok:
            raise ValidationError(result.issues)
        return result.value

    def secret_patterns(self) -> list[PathPattern]:
        """Patterns for every field marked secret, for the redactor."""
        return [PathPattern.from_segments(segments) for segments in _secret_paths(self.root, ())]

    def describe(self) -> Iterator[tuple[str, Field]]:
        """Yield (path pattern, field) for each declared leaf, in declaration order."""
        yield from _describe(self.root, ())


def _fill_table(
    field: Field, table: Mapping[str, Any], path: Path, on_default: Callable[[Path, Any], None] | None
) -> dict[str, Any]:
    out = dict(table)
    for name, child in field.fields.items():
        child_path = path + (name,)
        if name in out:
            out[name] = _fill_value(child, out[name], child_path, on_default)
        elif child.has_default:
            out[name] = copy.deepcopy(child.default)
            if on_default is not None:
                on_default(child_path, out[name])
        elif child.is_table and child.required is not False:
            nested = _fill_table(child, {}, child_path, on_default)
            if nested:
                out[name] = nested
    if field.values is not None:
        for name, value in out.items():
            if name not in field.fields:
                out[name] = _fill_value(field.values, value, path + (name,), on_default)
    return out


def _fill_value(field: Field, value: Any, path: Path, on_default: Callable[[Path, Any], None] | None) -> Any:
    if isinstance(value, dict):
        return _fill_table(field, value, path, on_default)
    if isinstance(value, list) and field.items is not None:
        return [_fill_value(field.items, item, path + (i,), on_default) for i, item in enumerate(value)]
    return value


def _secret_paths(field: Field, path: Path) -> Iterator[Path]:
    if field.secret:
        yield path
        return
    for name, child in field.fields.items():
        yield from _secret_paths(child, path + (name,))
    if field.values is not None:
        yield from _secret_paths(field.values, path + ("*",))
    if field.items is not None:
        yield from _secret_paths(field.items, path + ("*",))


def _describe(field: Field, path: Path) -> Iterator[tuple[str, Field]]:
    if field.fields:
        for name, child in field.fields.items():
            yield from _describe(child, path + (name,))
    elif path:
        yield format_path(path), field


def with_origins(issues: Sequence[Issue], origin_of: Callable[[Path], str | None]) -> list[Issue]:
    """Attach the layer responsible for each issue's path."""
    return [replace(issue, origin=origin_of(issue.path)) for issue in issues]
