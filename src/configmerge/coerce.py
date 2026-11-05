"""Turning strings into typed values.

Two different jobs live here. ``coerce_scalar`` guesses a type for text that
arrives without one (environment variables, CLI overrides). ``coerce_to``
converts text to a type the caller already asked for, as schemas and typed
getters do, so it can afford to accept more spellings.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

_INT = re.compile(r"[+-]?(0|[1-9][0-9]*)\Z")
# A dot is required: "1e5" looks like a short commit hash far more often than a float.
_FLOAT = re.compile(r"[+-]?(0|[1-9][0-9]*)\.[0-9]+([eE][+-]?[0-9]+)?\Z")

_TRUE_WORDS = frozenset({"true", "yes", "on", "1"})
_FALSE_WORDS = frozenset({"false", "no", "off", "0"})


def coerce_scalar(text: str) -> Any:
    """Guess the type of untyped text.

    ``true``/``false`` become booleans and plain decimal numbers become int or
    float. Numbers with leading zeros stay strings, since they tend to be
    identifiers. Text starting with ``[``, ``{`` or ``"`` is decoded as JSON if
    it is valid, which also gives a way to force a string: ``'"8080"'``.
    Everything else is returned unchanged.
    """
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if _INT.match(text):
        return int(text)
    if _FLOAT.match(text):
        return float(text)
    if text[:1] in ('[', '{', '"'):
        try:
            return json.loads(text)
        except ValueError:
            return text
    return text


def coerce_to(target: type, text: str) -> Any:
    """Convert text to target or raise ValueError."""
    stripped = text.strip()
    if target is bool:
        lowered = stripped.lower()
        if lowered in _TRUE_WORDS:
            return True
        if lowered in _FALSE_WORDS:
            return False
        raise ValueError(f"not a boolean: {text!r}")
    if target is int:
        if re.fullmatch(r"[+-]?[0-9]+", stripped):
            return int(stripped)
        raise ValueError(f"not an integer: {text!r}")
    if target is float:
        if re.fullmatch(r"[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?", stripped):
            return float(stripped)
        raise ValueError(f"not a number: {text!r}")
    if target in (dt.datetime, dt.date, dt.time):
        return target.fromisoformat(stripped)
    raise ValueError(f"cannot convert text to {target.__name__}")
