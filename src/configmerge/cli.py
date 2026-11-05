"""The ``configmerge`` command line.

Layers are given as options and apply in the order written, so
``-f base.toml --env APP -f local.toml`` lets local.toml beat the environment.

Exit codes: 0 success; 1 the answer is "no" (validation failed, configs differ,
key not found); 2 usage errors or configuration that could not be loaded.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Mapping, Sequence, TextIO

from .config import Config
from .diff import changes_to_dicts, diff_configs, render_diff
from .errors import ConfigKeyError, ConfigMergeError, ConfigTypeError, MergeRuleError, SourceError, ValidationError
from .loader import ConfigLoader
from .merge import Strategy
from .paths import iter_leaves, parse_path, format_path
from .schema import Schema
from .schema_spec import schema_from_spec
from .sources import CliOverrides, Directory, EnvFile, EnvVars, Source, file_source
from .writers import to_env, to_json, to_toml

EXIT_OK = 0
EXIT_NEGATIVE = 1
EXIT_ERROR = 2


class _AddLayer(argparse.Action):
    """Collects every layer option into one ordered list."""

    def __call__(self, parser: Any, namespace: Any, values: Any, option_string: str | None = None) -> None:
        layers = list(getattr(namespace, "layers", None) or [])
        layers.append((self.const, values))
        namespace.layers = layers


def _common_options() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    group = parent.add_argument_group("layers (applied in the order given)")
    group.add_argument("-f", "--file", action=_AddLayer, const="file", dest="layers", metavar="PATH", help="TOML, JSON or .env file")
    group.add_argument("--optional-file", action=_AddLayer, const="optional", dest="layers", metavar="PATH", help="like --file, but it may be absent")
    group.add_argument("-d", "--dir", action=_AddLayer, const="dir", dest="layers", metavar="PATH", help="directory of fragments, in filename order")
    group.add_argument("--env", action=_AddLayer, const="env", dest="layers", metavar="PREFIX", help="environment variables starting with PREFIX_")
    group.add_argument("--env-file", action=_AddLayer, const="envfile", dest="layers", metavar="PATH", help="env file under any file name")
    group.add_argument("-s", "--set", action=_AddLayer, const="set", dest="layers", metavar="KEY=VALUE", help="single override")
    parent.add_argument("-p", "--profile", action="append", default=[], metavar="NAME", help="activate a profile (repeatable)")
    parent.add_argument("-m", "--merge", action="append", default=[], metavar="PATH=STRATEGY", help="merge rule: replace, append, unique or deep")
    parent.add_argument("--strict-types", action="store_true", help="refuse layers that change the shape of a value")
    parent.add_argument("--schema", metavar="PATH", help="schema file to validate against")
    parent.add_argument("--unknown", choices=["forbid", "allow", "strip"], default="forbid", help="undeclared keys under --schema (default: forbid)")
    parent.add_argument("--coerce", action="store_true", help="let --schema convert strings to the declared type")
    parent.add_argument("--no-interpolate", action="store_true", help="leave ${...} references as written")
    parent.add_argument("--secret", action="append", default=[], metavar="PATTERN", help="extra path pattern to mask (repeatable)")
    parent.add_argument("--show-secrets", action="store_true", help="print secret values in the clear")
    return parent


def _build_parser() -> argparse.ArgumentParser:
    common = _common_options()
    parser = argparse.ArgumentParser(prog="configmerge", description="Merge layered configuration and show where each value came from.")
    commands = parser.add_subparsers(dest="command", required=True)

    show = commands.add_parser("show", parents=[common], help="print the merged configuration")
    show.add_argument("--format", choices=["json", "toml", "env"], default="json")
    show.add_argument("--env-prefix", default="", help="variable prefix for --format env")
    show.add_argument("--section", metavar="PATH", help="print only this table")
    show.set_defaults(handler=_cmd_show)

    explain = commands.add_parser("explain", parents=[common], help="say which layer set each key")
    explain.add_argument("paths", nargs="*", metavar="PATH", help="keys to explain (default: every leaf)")
    explain.add_argument("--json", action="store_true", help="machine-readable output")
    explain.set_defaults(handler=_cmd_explain)

    validate = commands.add_parser("validate", parents=[common], help="check the configuration against --schema")
    validate.add_argument("--json", action="store_true", help="machine-readable output")
    validate.set_defaults(handler=_cmd_validate)

    diff = commands.add_parser("diff", parents=[common], help="compare two variations of the layers")
    diff.add_argument("--left", action="append", default=[], metavar="PATH", help="extra file on the left side")
    diff.add_argument("--right", action="append", default=[], metavar="PATH", help="extra file on the right side")
    diff.add_argument("--left-profile", action="append", default=[], metavar="NAME")
    diff.add_argument("--right-profile", action="append", default=[], metavar="NAME")
    diff.add_argument("--by-index", action="store_true", help="compare lists element by element")
    diff.add_argument("--ignore", action="append", default=[], metavar="PATTERN", help="path pattern to leave out")
    diff.add_argument("--json", action="store_true", help="machine-readable output")
    diff.set_defaults(handler=_cmd_diff)

    get = commands.add_parser("get", parents=[common], help="print one value")
    get.add_argument("path", metavar="PATH")
    get.add_argument("--default", help="printed when the key is absent, instead of failing")
    get.add_argument("--type", choices=["str", "int", "float", "bool", "list", "duration", "size"], help="insist on this type")
    get.set_defaults(handler=_cmd_get)
    return parser


def _make_source(kind: str, value: str, environ: Mapping[str, str]) -> Source:
    if kind == "file":
        return file_source(value)
    if kind == "optional":
        return file_source(value, optional=True)
    if kind == "dir":
        return Directory(value)
    if kind == "env":
        return EnvVars(value, environ=environ)
    if kind == "envfile":
        return EnvFile(value)
    return CliOverrides([value])


def _merge_rules(items: Sequence[str]) -> dict[str, Strategy | str]:
    rules: dict[str, Strategy | str] = {}
    for item in items:
        path, separator, strategy = item.rpartition("=")
        if not separator or not path:
            raise MergeRuleError(f"merge rule {item!r} is not of the form PATH=STRATEGY")
        rules[path] = strategy
    return rules


def _read_schema(args: argparse.Namespace) -> Schema | None:
    if not args.schema:
        return None
    layers = file_source(args.schema).load()
    return schema_from_spec(layers[0].data, unknown=args.unknown, coerce=args.coerce)


def _make_loader(
    args: argparse.Namespace,
    environ: Mapping[str, str],
    *,
    extra_files: Sequence[str] = (),
    extra_profiles: Sequence[str] = (),
) -> ConfigLoader:
    sources = [_make_source(kind, value, environ) for kind, value in args.layers or []]
    sources.extend(file_source(path) for path in extra_files)
    return ConfigLoader(
        *sources,
        rules=_merge_rules(args.merge),
        strict_types=args.strict_types,
        profiles=[*args.profile, *extra_profiles],
        schema=_read_schema(args),
        interpolate=not args.no_interpolate,
        environ=environ,
        secrets=args.secret,
    )


def _load(args: argparse.Namespace, environ: Mapping[str, str]) -> Config:
    return _make_loader(args, environ).load()


def _cmd_show(args: argparse.Namespace, environ: Mapping[str, str], out: TextIO) -> int:
    config = _load(args, environ)
    view = config.section(args.section) if args.section else config
    data = view.to_dict(redact=not args.show_secrets)
    if args.format == "json":
        text = to_json(data)
    elif args.format == "toml":
        text = to_toml(data)
    else:
        text = to_env(data, prefix=args.env_prefix)
    out.write(text if text.endswith("\n") or not text else text + "\n")
    return EXIT_OK


def _cmd_explain(args: argparse.Namespace, environ: Mapping[str, str], out: TextIO) -> int:
    config = _load(args, environ)
    mask = None if args.show_secrets else config.mask
    if not args.paths:
        return _explain_all(config, args, mask, out)
    missing = False
    documents = []
    for path in args.paths:
        explanation = config.explain(path)
        missing = missing or not explanation.exists
        if args.json:
            documents.append(explanation.to_dict(mask))
        else:
            out.write(explanation.render(mask) + "\n")
    if args.json:
        out.write(to_json({"explanations": documents}) + "\n")
    return EXIT_NEGATIVE if missing else EXIT_OK


def _explain_all(config: Config, args: argparse.Namespace, mask: Any, out: TextIO) -> int:
    rows = []
    for path, value in iter_leaves(config.to_dict()):
        shown = mask(path, value) if mask else value
        rows.append((format_path(path), shown, config.explain(path).source))
    if args.json:
        documents = [{"path": name, "value": value, "source": source} for name, value, source in rows]
        out.write(to_json({"keys": documents}) + "\n")
        return EXIT_OK
    width = max((len(name) for name, _, _ in rows), default=0)
    for name, value, source in rows:
        out.write(f"{name:<{width}}  {json.dumps(value, ensure_ascii=False, default=str)}  <- {source or 'unknown'}\n")
    return EXIT_OK


def _cmd_validate(args: argparse.Namespace, environ: Mapping[str, str], out: TextIO) -> int:
    if not args.schema:
        raise SourceError("validate needs --schema")
    try:
        _load(args, environ)
    except ValidationError as exc:
        if args.json:
            out.write(to_json({"valid": False, "issues": [issue.to_dict() for issue in exc.issues]}) + "\n")
        else:
            for issue in exc.issues:
                out.write(f"{issue}\n")
        return EXIT_NEGATIVE
    out.write(to_json({"valid": True, "issues": []}) + "\n" if args.json else "valid\n")
    return EXIT_OK


def _cmd_diff(args: argparse.Namespace, environ: Mapping[str, str], out: TextIO) -> int:
    left = _make_loader(args, environ, extra_files=args.left, extra_profiles=args.left_profile).load()
    right = _make_loader(args, environ, extra_files=args.right, extra_profiles=args.right_profile).load()
    changes = diff_configs(left.to_dict(), right.to_dict(), by_index=args.by_index, ignore=args.ignore)
    left_mask = None if args.show_secrets else left.mask
    right_mask = None if args.show_secrets else right.mask
    if args.json:
        rows = changes_to_dicts(changes, left_mask=left_mask, right_mask=right_mask)
        out.write(to_json({"changes": rows}) + "\n")
    elif changes:
        out.write(render_diff(changes, left_mask=left_mask, right_mask=right_mask) + "\n")
    return EXIT_NEGATIVE if changes else EXIT_OK


_GETTERS = {
    "str": Config.get_str,
    "int": Config.get_int,
    "float": Config.get_float,
    "bool": Config.get_bool,
    "list": Config.get_list,
    "duration": lambda config, path: config.get_duration(path).total_seconds(),
    "size": Config.get_size,
}


def _cmd_get(args: argparse.Namespace, environ: Mapping[str, str], out: TextIO) -> int:
    config = _load(args, environ)
    path = parse_path(args.path)
    if path not in config and args.default is not None:
        out.write(args.default + "\n")
        return EXIT_OK
    value = _GETTERS[args.type](config, path) if args.type else config[path]
    shown = value if args.show_secrets else config.mask(path, value)
    out.write(_plain(shown) + "\n")
    return EXIT_OK


def _plain(value: Any) -> str:
    """Scalars print bare, for use in shell scripts; containers print as JSON."""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return to_json(value, indent=None)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Run the command line and return its exit code."""
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    env = os.environ if environ is None else environ
    args = _build_parser().parse_args(argv)
    try:
        return args.handler(args, env, out)
    except (ConfigKeyError, ConfigTypeError, ValidationError) as exc:
        err.write(f"error: {exc}\n")
        return EXIT_NEGATIVE
    except ConfigMergeError as exc:
        err.write(f"error: {exc}\n")
        return EXIT_ERROR


def console_entry() -> None:
    sys.exit(main())
