"""Keeps secrets out of anything shown to a person.

A value counts as secret if its path matches a declared pattern, or if any key
along its path looks like a credential. Secrets also leak indirectly, because
interpolation can paste a password into a connection string. Two measures cover
that: known secret strings are scrubbed out of every other string, and values
built from a secret that scrubbing cannot find are masked as a whole.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from .paths import MISSING, Path, PathPattern, iter_leaves, lookup, parse_path
from .provenance import Event

DEFAULT_SECRET_KEYS = re.compile(
    r"passw(or)?d|passphrase|(^|[_-])pass$|secret|token|api[_-]?key|private[_-]?key|credential",
    re.IGNORECASE,
)

DEFAULT_MASK = "***"


class Redactor:
    """Decides what is secret. ``scrub_min_length`` keeps a short secret such as
    ``"1"`` from blanking out every digit in the output; such values are still
    masked at their own path."""

    def __init__(
        self,
        patterns: Iterable[str | PathPattern] = (),
        *,
        key_regex: re.Pattern[str] | None = DEFAULT_SECRET_KEYS,
        mask: str = DEFAULT_MASK,
        scrub_min_length: int = 4,
    ) -> None:
        self.patterns = [p if isinstance(p, PathPattern) else PathPattern(p) for p in patterns]
        self.key_regex = key_regex
        self.mask = mask
        self.scrub_min_length = scrub_min_length

    def with_patterns(self, patterns: Iterable[str | PathPattern]) -> "Redactor":
        return Redactor(
            [*self.patterns, *patterns],
            key_regex=self.key_regex,
            mask=self.mask,
            scrub_min_length=self.scrub_min_length,
        )

    def key_looks_secret(self, key: str) -> bool:
        return self.key_regex is not None and self.key_regex.search(key) is not None

    def is_secret(self, path: Path) -> bool:
        if any(pattern.matches_within(path) for pattern in self.patterns):
            return True
        return any(isinstance(segment, str) and self.key_looks_secret(segment) for segment in path)

    def bind(self, data: Mapping[str, Any], tainted: Iterable[Path] = ()) -> "BoundRedactor":
        """Prepare to mask values drawn from data, learning its secret strings."""
        return BoundRedactor(self, data, tainted)

    def redact(self, data: Mapping[str, Any], tainted: Iterable[Path] = ()) -> Any:
        """A copy of data with secrets masked."""
        return self.bind(data, tainted).tree(data)


class BoundRedactor:
    """A Redactor plus the secret strings of one particular config.

    Instances are callable as ``mask(path, value)``, the form Explanation expects.
    """

    def __init__(self, redactor: Redactor, data: Mapping[str, Any], tainted: Iterable[Path] = ()) -> None:
        self.redactor = redactor
        self.tainted = frozenset(tainted)
        found = {
            value
            for path, value in iter_leaves(data)
            if isinstance(value, str) and len(value) >= redactor.scrub_min_length and self._masked_path(path)
        }
        # Longest first, so one secret that contains another is removed whole.
        self.secret_strings = sorted(found, key=len, reverse=True)

    def _masked_path(self, path: Path) -> bool:
        if self.redactor.is_secret(path):
            return True
        return any(path[:depth] in self.tainted for depth in range(len(path), 0, -1))

    def __call__(self, path: Path, value: Any) -> Any:
        return self.tree(value, path)

    def tree(self, value: Any, path: Path = ()) -> Any:
        """Mask a value that lives at path, descending into containers."""
        if value is None:
            return None
        if path and self._masked_path(path):
            return self.redactor.mask
        if isinstance(value, Mapping):
            return {key: self.tree(item, path + (key,)) for key, item in value.items()}
        if isinstance(value, list):
            return [self.tree(item, path + (i,)) for i, item in enumerate(value)]
        if isinstance(value, str):
            return self.scrub(value)
        return value

    def scrub(self, text: str) -> str:
        """Remove every known secret string from text."""
        for secret in self.secret_strings:
            if secret in text:
                text = text.replace(secret, self.redactor.mask)
        return text


def tainted_paths(events: Iterable[Event], redactor: Redactor, data: Mapping[str, Any]) -> set[Path]:
    """Paths whose interpolated value was built from a secret scrubbing cannot catch.

    Events arrive in resolution order, so a reference is always seen after the
    value it points at, and one pass handles chains of references.
    """
    tainted: set[Path] = set()
    for event in events:
        if any(_taints(ref, redactor, data, tainted) for ref in event.references):
            tainted.add(event.path)
    return tainted


def _taints(ref: Any, redactor: Redactor, data: Mapping[str, Any], tainted: set[Path]) -> bool:
    if ref.scheme is not None:
        return redactor.key_looks_secret(ref.name)
    path = parse_path(ref.name)
    if any(path[:depth] in tainted for depth in range(len(path), 0, -1)):
        return True
    if not redactor.is_secret(path):
        return False
    value = lookup(data, path)
    scrubbable = isinstance(value, str) and len(value) >= redactor.scrub_min_length
    return value is not MISSING and not scrubbable
