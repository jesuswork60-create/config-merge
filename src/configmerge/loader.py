"""Ties the stages together: sources, profiles, merge, defaults, interpolation, validation."""

from __future__ import annotations

import os
from typing import Any, Iterable, Mapping

from .config import Config
from .errors import ValidationError
from .interpolate import Interpolator, Resolver
from .merge import Merger, MergePolicy, Strategy
from .paths import Path
from .profiles import expand_profiles
from .provenance import SCHEMA_DEFAULT_LAYER, Action, Event, Provenance
from .redact import Redactor
from .schema import Schema, with_origins
from .sources import CliOverrides, DictSource, Directory, EnvFile, EnvVars, Layer, Source, file_source


class ConfigLoader:
    """Collects sources, lowest precedence first, and builds a Config.

    The ``add_*`` methods return the loader so calls can be chained::

        config = (
            ConfigLoader(profiles=["dev"], rules={"server.hosts": "unique"})
            .add_dict({"server": {"port": 8000}})
            .add_file("app.toml")
            .add_env("APP")
            .load()
        )

    A loader can be loaded more than once; each load re-reads its sources.
    """

    def __init__(
        self,
        *sources: Source,
        rules: Mapping[str, Strategy | str] | None = None,
        strict_types: bool = False,
        profiles: Iterable[str] = (),
        profile_key: str = "profile",
        profile_env: str | None = None,
        schema: Schema | Mapping[str, Any] | None = None,
        interpolate: bool = True,
        resolvers: Mapping[str, Resolver] | None = None,
        environ: Mapping[str, str] | None = None,
        redactor: Redactor | None = None,
        secrets: Iterable[str] = (),
    ) -> None:
        self.sources: list[Source] = list(sources)
        self.policy = MergePolicy(rules, strict_types=strict_types)
        self.profiles = list(profiles)
        self.profile_key = profile_key
        self.profile_env = profile_env
        self.schema = schema if isinstance(schema, Schema) or schema is None else Schema(schema)
        self.interpolate = interpolate
        self.resolvers = dict(resolvers or {})
        self.environ = os.environ if environ is None else environ
        base = redactor or Redactor()
        extra = self.schema.secret_patterns() if self.schema else []
        self.redactor = base.with_patterns([*secrets, *extra])

    def add(self, source: Source) -> "ConfigLoader":
        self.sources.append(source)
        return self

    def add_dict(self, data: Mapping[str, Any], *, name: str = "defaults") -> "ConfigLoader":
        return self.add(DictSource(data, name=name))

    def add_file(self, path: str | os.PathLike[str], *, optional: bool = False, name: str | None = None) -> "ConfigLoader":
        return self.add(file_source(path, optional=optional, name=name))

    def add_directory(self, path: str | os.PathLike[str], *, optional: bool = False, recursive: bool = False) -> "ConfigLoader":
        return self.add(Directory(path, optional=optional, recursive=recursive))

    def add_env(self, prefix: str, **options: Any) -> "ConfigLoader":
        options.setdefault("environ", self.environ)
        return self.add(EnvVars(prefix, **options))

    def add_env_file(self, path: str | os.PathLike[str], *, prefix: str = "", optional: bool = False) -> "ConfigLoader":
        return self.add(EnvFile(path, prefix=prefix, optional=optional))

    def add_overrides(self, items: Iterable[str], *, name: str = "cli") -> "ConfigLoader":
        return self.add(CliOverrides(items, name=name))

    def active_profiles(self) -> list[str]:
        """Profiles to apply: those named in the environment, then the configured ones."""
        names: list[str] = []
        if self.profile_env:
            raw = self.environ.get(self.profile_env, "")
            names.extend(part.strip() for part in raw.split(",") if part.strip())
        names.extend(name for name in self.profiles if name not in names)
        return names

    def layers(self) -> list[Layer]:
        """Read all sources and expand profile overlays."""
        loaded: list[Layer] = []
        for source in self.sources:
            loaded.extend(source.load())
        return expand_profiles(loaded, self.active_profiles(), key=self.profile_key)

    def load(self) -> Config:
        """Run the whole pipeline. Raises a ConfigMergeError subclass on any failure."""
        provenance = Provenance()
        data = Merger(self.policy, provenance).merge_all(self.layers())
        if self.schema is not None:
            data = self._fill_defaults(data, provenance)
        if self.interpolate:
            data = Interpolator(environ=self.environ, resolvers=self.resolvers, provenance=provenance).resolve(data)
        if self.schema is not None:
            result = self.schema.validate(data, is_secret=self.redactor.is_secret)
            if not result.ok:
                raise ValidationError(with_origins(result.issues, provenance.owner))
            data = result.value
        return Config(data, provenance=provenance, redactor=self.redactor)

    def _fill_defaults(self, data: dict[str, Any], provenance: Provenance) -> dict[str, Any]:
        def record(path: Path, value: Any) -> None:
            provenance.record(Event(path, Action.DEFAULT, SCHEMA_DEFAULT_LAYER, value=value))

        assert self.schema is not None
        return self.schema.apply_defaults(data, record)


def load_config(*sources: Source, **options: Any) -> Config:
    """One-call form of ``ConfigLoader(*sources, **options).load()``."""
    return ConfigLoader(*sources, **options).load()

