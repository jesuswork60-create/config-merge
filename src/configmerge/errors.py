"""Exception hierarchy. Everything raised on purpose derives from ConfigMergeError."""

from __future__ import annotations

from typing import Any, Sequence


class ConfigMergeError(Exception):
    """Base class for all errors raised by this package."""


class PathSyntaxError(ConfigMergeError, ValueError):
    """A dotted path such as ``server.ports[1]`` could not be parsed."""

    def __init__(self, text: str, column: int, reason: str) -> None:
        super().__init__(f"invalid path {text!r} at column {column + 1}: {reason}")
        self.text = text
        self.column = column
        self.reason = reason


class SourceError(ConfigMergeError):
    """A configuration source could not be read."""


class ConfigParseError(SourceError):
    """A source file is malformed. Carries the file, line and column of the problem."""

    def __init__(
        self,
        file: str,
        line: int,
        column: int,
        detail: str,
        snippet: str | None = None,
    ) -> None:
        self.file = file
        self.line = line
        self.column = column
        self.detail = detail
        self.snippet = snippet
        super().__init__(self._format())

    def _format(self) -> str:
        head = f"{self.file}:{self.line}:{self.column}: {self.detail}"
        if self.snippet is None:
            return head
        gutter = f"{self.line:>4} | "
        pointer = " " * (len(gutter) + max(self.column - 1, 0)) + "^"
        return f"{head}\n{gutter}{self.snippet}\n{pointer}"


class MergeRuleError(ConfigMergeError, ValueError):
    """A merge rule names an unknown strategy or is malformed."""


class MergeConflictError(ConfigMergeError):
    """Two layers disagree about the shape of a value and strict types are on."""


class InterpolationError(ConfigMergeError):
    """A ``${...}`` expression could not be expanded."""

    def __init__(self, message: str, path: str | None = None) -> None:
        super().__init__(f"{path}: {message}" if path else message)
        self.path = path


class ReferenceNotFoundError(InterpolationError):
    """A reference names a key or variable that does not exist and has no default."""

    def __init__(self, reference: str, path: str | None = None) -> None:
        super().__init__(f"reference ${{{reference}}} not found", path)
        self.reference = reference


class InterpolationCycleError(InterpolationError):
    """References form a loop. ``cycle`` lists the paths in order, first repeated last."""

    def __init__(self, cycle: Sequence[str]) -> None:
        self.cycle = list(cycle)
        super().__init__("reference cycle: " + " -> ".join(self.cycle), self.cycle[0])


class ProfileError(ConfigMergeError):
    """A profile was requested that no layer declares, or a section is malformed."""


class SchemaDefinitionError(ConfigMergeError):
    """The schema itself is wrong, independent of any configuration values."""


class ValidationError(ConfigMergeError):
    """The configuration does not satisfy the schema. ``issues`` holds every problem."""

    def __init__(self, issues: Sequence[Any]) -> None:
        self.issues = list(issues)
        count = len(self.issues)
        noun = "error" if count == 1 else "errors"
        lines = [f"{count} validation {noun}:"]
        lines.extend(f"  {issue}" for issue in self.issues)
        super().__init__("\n".join(lines))


class WriterError(ConfigMergeError):
    """A value cannot be represented in the requested output format."""


class ConfigKeyError(ConfigMergeError, KeyError):
    """A requested key does not exist."""

    def __str__(self) -> str:
        # KeyError would otherwise repr() its argument and add stray quotes.
        return str(self.args[0]) if self.args else ""


class ConfigTypeError(ConfigMergeError, TypeError):
    """A value exists but cannot be returned as the requested type."""
