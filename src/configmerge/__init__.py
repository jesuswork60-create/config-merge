"""Layered configuration loading with deep merging, provenance and validation."""

from .config import Config
from .diff import Change, ChangeKind, diff_configs, render_diff
from .errors import (
    ConfigKeyError,
    ConfigMergeError,
    ConfigParseError,
    ConfigTypeError,
    InterpolationCycleError,
    InterpolationError,
    MergeConflictError,
    MergeRuleError,
    PathSyntaxError,
    ProfileError,
    ReferenceNotFoundError,
    SchemaDefinitionError,
    SourceError,
    ValidationError,
    WriterError,
)
from .interpolate import Interpolator
from .loader import ConfigLoader, load_config
from .merge import Merger, MergePolicy, Strategy
from .paths import MISSING, PathPattern, format_path, parse_path
from .provenance import Action, Event, Explanation, Provenance
from .redact import Redactor
from .schema import Field, Issue, Schema, ValidationResult
from .schema_spec import schema_from_spec
from .sources import (
    CliOverrides,
    DictSource,
    Directory,
    EnvFile,
    EnvVars,
    JsonFile,
    Layer,
    Source,
    TomlFile,
    file_source,
)
from .writers import to_env, to_json, to_toml

__version__ = "0.1.0"

__all__ = [
    "Action",
    "Change",
    "ChangeKind",
    "CliOverrides",
    "Config",
    "ConfigKeyError",
    "ConfigLoader",
    "ConfigMergeError",
    "ConfigParseError",
    "ConfigTypeError",
    "DictSource",
    "Directory",
    "EnvFile",
    "EnvVars",
    "Event",
    "Explanation",
    "Field",
    "Interpolator",
    "InterpolationCycleError",
    "InterpolationError",
    "Issue",
    "JsonFile",
    "Layer",
    "MISSING",
    "MergeConflictError",
    "MergeRuleError",
    "MergePolicy",
    "Merger",
    "PathPattern",
    "PathSyntaxError",
    "ProfileError",
    "Provenance",
    "Redactor",
    "ReferenceNotFoundError",
    "Schema",
    "SchemaDefinitionError",
    "Source",
    "SourceError",
    "Strategy",
    "TomlFile",
    "ValidationError",
    "ValidationResult",
    "WriterError",
    "diff_configs",
    "file_source",
    "format_path",
    "load_config",
    "parse_path",
    "render_diff",
    "schema_from_spec",
    "to_env",
    "to_json",
    "to_toml",
]
