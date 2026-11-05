"""Human-friendly duration and byte-size strings, used by the typed getters."""

from __future__ import annotations

import datetime as dt
import re

_DURATION_UNITS = {
    "ms": 0.001,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
    "w": 604800.0,
}
_DURATION_PART = re.compile(r"([0-9]+(?:\.[0-9]+)?)(ms|s|m|h|d|w)")

_SIZE_UNITS = {
    "": 1,
    "b": 1,
    "k": 1000,
    "kb": 1000,
    "m": 1000**2,
    "mb": 1000**2,
    "g": 1000**3,
    "gb": 1000**3,
    "t": 1000**4,
    "tb": 1000**4,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
}
_SIZE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]*)")


def parse_duration(text: str) -> dt.timedelta:
    """Parse strings like ``90s``, ``1h30m`` or ``250ms``. Raises ValueError."""
    stripped = text.strip()
    position = 0
    seconds = 0.0
    for match in _DURATION_PART.finditer(stripped):
        if match.start() != position:
            break
        seconds += float(match.group(1)) * _DURATION_UNITS[match.group(2)]
        position = match.end()
    if not stripped or position != len(stripped):
        raise ValueError(f"not a duration: {text!r}")
    return dt.timedelta(seconds=seconds)


def parse_size(text: str) -> int:
    """Parse strings like ``512``, ``10MB`` or ``1.5GiB`` into a byte count. Raises ValueError."""
    match = _SIZE.fullmatch(text.strip())
    if match is None:
        raise ValueError(f"not a size: {text!r}")
    factor = _SIZE_UNITS.get(match.group(2).lower())
    if factor is None:
        raise ValueError(f"unknown size unit in {text!r}")
    return int(float(match.group(1)) * factor)
