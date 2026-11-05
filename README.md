# config-merge

config-merge reads settings from several places, merges them in the order you
list them, and can tell you afterwards which layer set each value. It is a small
Python library with a command line tool on top and has no dependencies outside
the standard library.

What it covers:

- **Sources**: TOML and JSON files, environment variables (`APP_SERVER__PORT`
  becomes `server.port`), env files, `a.b.c=value` overrides, plain dicts,
  optional files and directories of numbered fragments.
- **Merging**: tables merge deeply. Other values are replaced, unless a rule
  for that path says `append`, `unique`, `deep` or `replace`. Rules can use
  wildcards (`services.*.hosts`, `**.tags`).
- **Provenance**: `explain` shows the winning layer for a key and every earlier
  value it replaced. List elements each remember the layer that added them.
- **Interpolation**: `${server.host}`, `${env:NAME}`, `${path:-default}` and `$$`
  for a literal dollar. A value that is only a reference keeps its type, and a
  cycle is reported with the full chain (`a -> b -> a`).
- **Profiles**: a `[profile.dev]` section is laid over its own layer when `dev`
  is active.
- **Validation**: a small schema language that reports every problem at once,
  with paths such as `server.ports[1]` and the layer that set the bad value.
- **Secrets**: values under names like `password` or `api_key` are masked in
  output, including where they were copied into other strings.
- **Output and comparison**: JSON, TOML and env writers, and a diff between two
  sets of layers.

## Install

Python 3.11 or newer is required. From a checkout:

    pip install .

That gives you the `configmerge` command. Without installing, set
`PYTHONPATH=src` and use `python3 -m configmerge` in its place, as the examples
below do.

## Command line

The `examples` folder holds settings for a made-up bookshop service. Layers
apply in the order they appear on the command line, so later options win.

    $ export PYTHONPATH=src
    $ python3 -m configmerge show -f examples/base.toml -f examples/local.json \
        -m server.allowed_hosts=unique -p dev \
        --schema examples/schema.toml --format toml
    log_level = "debug"

    [server]
    host = "127.0.0.1"
    port = 9000
    allowed_hosts = ["shop.example.com", "localhost", "staging.example.com"]
    url = "http://127.0.0.1:9000"

    [database]
    host = "localhost"
    port = 5432
    name = "bookshop"
    user = "bookshop"
    password = "***"
    dsn = "postgres://bookshop:***@localhost:5432/bookshop"
    pool_size = 5

    [cache]
    ttl = "15m"
    max_size = "64MiB"

To find out where a value came from:

    $ APP_SERVER__PORT=9100 python3 -m configmerge explain \
        -f examples/base.toml -f examples/local.json --env APP server.port
    server.port = 9100
      from env:APP
      history:
        set         8000 from examples/base.toml
        override    9000 from examples/local.json
        override    9100 from env:APP (APP_SERVER__PORT)

To check values against a schema:

    $ APP_SERVER__PORT=99999 python3 -m configmerge validate \
        -f examples/base.toml --env APP --schema examples/schema.toml
    server.port: must be at most 65535; got 99999 (set by env:APP)

The other commands are `diff` (for example `diff -f examples/base.toml
--right-profile dev`) and `get` (`get -f examples/base.toml cache.ttl --type
duration`). Secrets stay masked in all of them unless you pass `--show-secrets`.

Exit codes: 0 means fine, 1 means the answer was negative (validation failed,
differences found, key missing) and 2 means something went wrong, such as a parse
error or a reference cycle.

## Library

```python
from configmerge import ConfigLoader, Field, Schema

schema = Schema(
    {
        "server": {
            "host": str,
            "port": Field(int, min=1, max=65535),
            "url": str,
        },
        "workers": Field(int, default=2),
    }
)

config = (
    ConfigLoader(schema=schema, environ={"SHOP_SERVER__PORT": "9000"})
    .add_dict(
        {
            "server": {
                "host": "localhost",
                "port": 8000,
                "url": "http://${server.host}:${server.port}",
            }
        }
    )
    .add_env("SHOP")
    .load()
)

print(config["server.url"])                 # http://localhost:9000
print(config.get_int("workers"))            # 2
print(config.explain("server.port").source) # env:SHOP
```

`environ` is only there to keep the example self-contained. Leave it out and the
process environment is used.

## Running the tests

    PYTHONPATH=src python3 -m unittest discover -s tests

## Known limitations

- Parse errors have line and column, but `explain` cannot point at the line a
  TOML or JSON value came from, because the standard parsers do not report
  positions for values.
- A later layer can overwrite a key but cannot delete it. Setting it to null is
  the closest you can get.
- Overrides on the command line and environment variables cannot target a list
  index. You can only set the whole list.
- Environment variable names are case-folded, so mixed-case keys cannot be
  reached unless you pass `lowercase=False`. A key that itself contains `__`
  cannot be addressed at all.
- Duplicate keys in a JSON file are not reported. The last one silently wins.
- `${path:-default}` uses the default when the value is missing or null, but not
  when it is an empty string, which differs from the shell.
- Interpolation stops at the first error rather than collecting all of them, unlike
  schema validation.
- Env files do not expand `$VAR` and values cannot span several lines.
- Secret detection works from key names and the paths you configure. A password
  written inside an ordinary string under an ordinary key will not be noticed,
  and a harmless key can match by accident (`tokenizer` matches `token`).
- Secrets shorter than four characters are not searched for inside other strings.
  Values interpolated from them are masked whole instead.
- A `replace` rule copies the incoming subtree as it is, so rules for paths
  nested below it are not applied.
- Conversions done by the schema's `coerce` option are not shown in the `explain`
  history.
- Unquoted values from the environment or `-s` are guessed: `1.10` becomes the
  float `1.1`. Quote it as JSON (`'"1.10"'`) to keep the text.
- There is no include directive, and files are not watched for changes.
