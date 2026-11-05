"""Profile overlays such as ``[profile.dev]``.

A profile section belongs to the layer that declares it and acts as a layer of
its own directly above that one. So a file's dev profile beats that file's base
values, while a later file or an environment variable still beats the profile.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .errors import ProfileError
from .paths import Path
from .sources import Layer


def split_profiles(layer: Layer, key: str) -> tuple[Layer, dict[str, Mapping[str, Any]]]:
    """Separate a layer's profile section from its ordinary values."""
    section = layer.data.get(key)
    if section is None:
        return layer, {}
    if not isinstance(section, Mapping):
        raise ProfileError(f"{layer.name}: {key!r} must be a table of profiles")
    for name, overlay in section.items():
        if not isinstance(overlay, Mapping):
            raise ProfileError(f"{layer.name}: profile {name!r} must be a table")
    base = {k: v for k, v in layer.data.items() if k != key}
    notes = {path: note for path, note in layer.notes.items() if not path or path[0] != key}
    return Layer(layer.name, base, layer.kind, layer.location, notes), dict(section)


def _overlay_notes(layer: Layer, key: str, profile: str) -> dict[Path, str]:
    prefix = (key, profile)
    return {path[2:]: note for path, note in layer.notes.items() if path[:2] == prefix}


def expand_profiles(layers: Iterable[Layer], active: Iterable[str], *, key: str = "profile") -> list[Layer]:
    """Replace each layer by itself plus its active overlays, in the order requested.

    Raises ProfileError if a requested profile is declared by no layer at all,
    since that is almost always a typo that would otherwise pass unnoticed.
    """
    requested = list(active)
    declared: set[str] = set()
    expanded: list[Layer] = []
    for layer in layers:
        base, overlays = split_profiles(layer, key)
        declared.update(overlays)
        expanded.append(base)
        for name in requested:
            if name in overlays:
                expanded.append(
                    Layer(
                        f"{layer.name}#{key}.{name}",
                        overlays[name],
                        kind="profile",
                        location=layer.location,
                        notes=_overlay_notes(layer, key, name),
                    )
                )
    unknown = [name for name in requested if name not in declared]
    if unknown:
        available = ", ".join(sorted(declared)) or "none"
        raise ProfileError(f"unknown profile {', '.join(unknown)} (available: {available})")
    return expanded
