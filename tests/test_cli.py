import contextlib
import io
import json
import tempfile
import tomllib
import unittest
from pathlib import Path

from configmerge.cli import main


class CliTestCase(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.dir = Path(holder.name)
        self.base = self.write(
            "base.toml",
            """
log = "info"
hosts = ["a"]

[server]
host = "h"
port = 80
url = "http://${server.host}:${server.port}"

[db]
password = "hunter22"
dsn = "pg://u:${db.password}@h"
timeout = "30s"

[profile.dev]
log = "debug"
[profile.dev.server]
port = 81

[profile.prod.server]
port = 443
""",
        )
        self.schema = self.write(
            "schema.toml",
            """
log = { type = "str", choices = ["debug", "info"] }
hosts = "list[str]"
[server]
host = "str"
port = { type = "int", min = 1, max = 1000 }
url = "str"
[db]
password = "str"
dsn = "str"
timeout = "str"
""",
        )

    def write(self, name, text):
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def run_cli(self, *argv, environ=None):
        out, err = io.StringIO(), io.StringIO()
        code = main(list(argv), stdout=out, stderr=err, environ=environ or {})
        return code, out.getvalue(), err.getvalue()


class ShowTests(CliTestCase):
    def test_json_is_default_and_masks_secrets(self):
        code, out, _ = self.run_cli("show", "-f", self.base)
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["server"]["url"], "http://h:80")
        self.assertEqual(data["db"]["password"], "***")
        self.assertEqual(data["db"]["dsn"], "pg://u:***@h")
        self.assertNotIn("hunter22", out)

    def test_show_secrets(self):
        _, out, _ = self.run_cli("show", "-f", self.base, "--show-secrets")
        self.assertEqual(json.loads(out)["db"]["password"], "hunter22")

    def test_toml_format(self):
        _, out, _ = self.run_cli("show", "-f", self.base, "--format", "toml")
        self.assertEqual(tomllib.loads(out)["server"]["port"], 80)

    def test_env_format(self):
        _, out, _ = self.run_cli("show", "-f", self.base, "--format", "env", "--env-prefix", "APP")
        self.assertIn("APP_SERVER__PORT=80\n", out)
        self.assertNotIn("hunter22", out)

    def test_section(self):
        _, out, _ = self.run_cli("show", "-f", self.base, "--section", "server")
        self.assertEqual(set(json.loads(out)), {"host", "port", "url"})

    def test_layer_options_apply_in_written_order(self):
        extra = self.write("extra.json", '{"log": "from-file"}')
        env = {"APP_LOG": "from-env"}
        _, out, _ = self.run_cli("show", "--env", "APP", "-f", extra, environ=env)
        self.assertEqual(json.loads(out)["log"], "from-file")
        _, out, _ = self.run_cli("show", "-f", extra, "--env", "APP", environ=env)
        self.assertEqual(json.loads(out)["log"], "from-env")
        _, out, _ = self.run_cli("show", "-f", extra, "--env", "APP", "-s", "log=from-cli", environ=env)
        self.assertEqual(json.loads(out)["log"], "from-cli")

    def test_profile_and_merge_rule(self):
        extra = self.write("extra.json", '{"hosts": ["b"]}')
        _, out, _ = self.run_cli("show", "-f", self.base, "-f", extra, "-p", "dev", "-m", "hosts=append")
        data = json.loads(out)
        self.assertEqual(data["log"], "debug")
        self.assertEqual(data["hosts"], ["a", "b"])
        self.assertEqual(data["server"]["url"], "http://h:81")

    def test_directory_and_optional_file(self):
        conf = self.dir / "conf"
        conf.mkdir()
        (conf / "a.toml").write_text("x = 1\n", encoding="utf-8")
        _, out, _ = self.run_cli("show", "-d", str(conf), "--optional-file", str(self.dir / "nope.toml"))
        self.assertEqual(json.loads(out), {"x": 1})

    def test_env_file_option(self):
        path = self.write("settings", "A=1\n")
        _, out, _ = self.run_cli("show", "--env-file", path)
        self.assertEqual(json.loads(out), {"a": 1})

    def test_no_interpolate(self):
        _, out, _ = self.run_cli("show", "-f", self.base, "--no-interpolate", "--section", "server")
        self.assertEqual(json.loads(out)["url"], "http://${server.host}:${server.port}")

    def test_extra_secret(self):
        _, out, _ = self.run_cli("show", "-f", self.base, "--secret", "server.host")
        self.assertEqual(json.loads(out)["server"]["host"], "***")

    def test_no_layers_is_an_empty_config(self):
        code, out, _ = self.run_cli("show")
        self.assertEqual((code, json.loads(out)), (0, {}))

    def test_interpolation_uses_given_environment(self):
        path = self.write("e.toml", 'a = "${env:ZZ}"\n')
        _, out, _ = self.run_cli("show", "-f", path, environ={"ZZ": "v"})
        self.assertEqual(json.loads(out), {"a": "v"})


class ExplainTests(CliTestCase):
    def test_single_key(self):
        code, out, _ = self.run_cli("explain", "-f", self.base, "--env", "APP", "server.port", environ={"APP_SERVER__PORT": "90"})
        self.assertEqual(code, 0)
        self.assertIn("server.port = 90", out)
        self.assertIn("env:APP (APP_SERVER__PORT)", out)
        self.assertIn(self.base, out)

    def test_masks_secrets(self):
        _, out, _ = self.run_cli("explain", "-f", self.base, "db.password", "db.dsn")
        self.assertNotIn("hunter22", out)

    def test_show_secrets(self):
        _, out, _ = self.run_cli("explain", "-f", self.base, "db.password", "--show-secrets")
        self.assertIn("hunter22", out)

    def test_missing_key_exit_code(self):
        code, out, _ = self.run_cli("explain", "-f", self.base, "nope")
        self.assertEqual(code, 1)
        self.assertIn("not set", out)

    def test_table_key(self):
        _, out, _ = self.run_cli("explain", "-f", self.base, "server")
        self.assertIn("server.port  <- ", out)

    def test_all_keys(self):
        code, out, _ = self.run_cli("explain", "-f", self.base)
        self.assertEqual(code, 0)
        self.assertIn("server.port", out)
        self.assertIn("hosts[0]", out)
        self.assertNotIn("hunter22", out)

    def test_json_single(self):
        _, out, _ = self.run_cli("explain", "-f", self.base, "-s", "log=x", "log", "--json")
        document = json.loads(out)["explanations"][0]
        self.assertEqual(document["source"], "cli")
        self.assertEqual(document["history"][-1]["note"], "log=x")

    def test_json_all(self):
        _, out, _ = self.run_cli("explain", "-f", self.base, "--json")
        rows = {row["path"]: row for row in json.loads(out)["keys"]}
        self.assertEqual(rows["db.password"]["value"], "***")
        self.assertEqual(rows["log"]["source"], self.base)


class ValidateTests(CliTestCase):
    def test_valid(self):
        code, out, _ = self.run_cli("validate", "-f", self.base, "--schema", self.schema)
        self.assertEqual((code, out), (0, "valid\n"))

    def test_invalid_lists_everything_with_origins(self):
        code, out, _ = self.run_cli(
            "validate",
            "-f",
            self.base,
            "-s",
            "server.port=5000",
            "-s",
            "log=loud",
            "-s",
            "extra=1",
            "--schema",
            self.schema,
        )
        self.assertEqual(code, 1)
        lines = out.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("server.port: must be at most 1000; got 5000 (set by cli)", lines[1] + lines[0] + lines[2])
        self.assertIn("extra: unknown key", out)

    def test_json_output(self):
        code, out, _ = self.run_cli("validate", "-f", self.base, "-s", "log=loud", "--schema", self.schema, "--json")
        self.assertEqual(code, 1)
        document = json.loads(out)
        self.assertFalse(document["valid"])
        self.assertEqual(document["issues"][0]["code"], "choice")
        self.assertEqual(document["issues"][0]["origin"], "cli")
        code, out, _ = self.run_cli("validate", "-f", self.base, "--schema", self.schema, "--json")
        self.assertEqual((code, json.loads(out)), (0, {"valid": True, "issues": []}))

    def test_unknown_policy_option(self):
        code, _, _ = self.run_cli("validate", "-f", self.base, "-s", "extra=1", "--schema", self.schema, "--unknown", "allow")
        self.assertEqual(code, 0)

    def test_coerce_option(self):
        path = self.write("s.toml", 'server = { type = "object", fields = { port = "int" }, unknown = "allow" }\n')
        data = self.write("d.toml", '[server]\nport = "80"\n')
        self.assertEqual(self.run_cli("validate", "-f", data, "--schema", path)[0], 1)
        self.assertEqual(self.run_cli("validate", "-f", data, "--schema", path, "--coerce")[0], 0)

    def test_requires_schema(self):
        code, _, err = self.run_cli("validate", "-f", self.base)
        self.assertEqual(code, 2)
        self.assertIn("--schema", err)

    def test_other_commands_validate_too(self):
        code, out, err = self.run_cli("show", "-f", self.base, "-s", "log=loud", "--schema", self.schema)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("log: must be one of", err)

    def test_broken_schema_file(self):
        path = self.write("bad.toml", 'a = "bogus"\n')
        code, _, err = self.run_cli("validate", "-f", self.base, "--schema", path)
        self.assertEqual(code, 2)
        self.assertIn("unknown type", err)


class DiffTests(CliTestCase):
    def test_profiles(self):
        code, out, _ = self.run_cli("diff", "-f", self.base, "--left-profile", "dev", "--right-profile", "prod")
        self.assertEqual(code, 1)
        self.assertIn("~ server.port: 81 -> 443", out)
        self.assertIn("~ server.url:", out)
        self.assertIn('~ log: "debug" -> "info"', out)

    def test_files(self):
        left = self.write("l.json", '{"a": 1, "b": 1}')
        right = self.write("r.json", '{"a": 2, "c": 1}')
        code, out, _ = self.run_cli("diff", "--left", left, "--right", right)
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines(), ["~ a: 1 -> 2", "- b = 1", "+ c = 1"])

    def test_no_difference(self):
        code, out, _ = self.run_cli("diff", "-f", self.base)
        self.assertEqual((code, out), (0, ""))

    def test_secret_changes_are_masked(self):
        right = self.write("r.toml", '[db]\npassword = "changed-secret"\n')
        code, out, _ = self.run_cli("diff", "-f", self.base, "--right", right)
        self.assertEqual(code, 1)
        self.assertNotIn("hunter22", out)
        self.assertNotIn("changed-secret", out)
        self.assertIn('~ db.password: "***" -> "***"', out)

    def test_show_secrets(self):
        right = self.write("r.toml", '[db]\npassword = "changed-secret"\n')
        _, out, _ = self.run_cli("diff", "-f", self.base, "--right", right, "--show-secrets")
        self.assertIn("changed-secret", out)

    def test_json_and_ignore(self):
        right = self.write("r.json", '{"a": 1, "b": 2}')
        code, out, _ = self.run_cli("diff", "--right", right, "--ignore", "a", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["changes"], [{"path": "b", "kind": "added", "new": 2}])

    def test_by_index(self):
        left = self.write("l.json", '{"l": [1, 2]}')
        right = self.write("r.json", '{"l": [1, 3]}')
        _, out, _ = self.run_cli("diff", "--left", left, "--right", right, "--by-index")
        self.assertEqual(out, "~ l[1]: 2 -> 3\n")


class GetTests(CliTestCase):
    def test_scalars_print_bare(self):
        self.assertEqual(self.run_cli("get", "-f", self.base, "server.host")[1], "h\n")
        self.assertEqual(self.run_cli("get", "-f", self.base, "server.port")[1], "80\n")
        self.assertEqual(self.run_cli("get", "-s", "a=true", "a")[1], "true\n")

    def test_containers_print_as_json(self):
        self.assertEqual(self.run_cli("get", "-f", self.base, "hosts")[1], '["a"]\n')
        self.assertEqual(self.run_cli("get", "-s", "t.a=1", "t")[1], '{"a": 1}\n')

    def test_list_element(self):
        self.assertEqual(self.run_cli("get", "-f", self.base, "hosts[0]")[1], "a\n")

    def test_missing(self):
        code, _, err = self.run_cli("get", "-f", self.base, "server.prot")
        self.assertEqual(code, 1)
        self.assertIn("did you mean server.port", err)

    def test_default(self):
        self.assertEqual(self.run_cli("get", "-f", self.base, "nope", "--default", "d")[:2], (0, "d\n"))

    def test_secret_masked_unless_asked(self):
        self.assertEqual(self.run_cli("get", "-f", self.base, "db.password")[1], "***\n")
        self.assertEqual(self.run_cli("get", "-f", self.base, "db.password", "--show-secrets")[1], "hunter22\n")

    def test_typed(self):
        self.assertEqual(self.run_cli("get", "-f", self.base, "db.timeout", "--type", "duration")[1], "30.0\n")
        self.assertEqual(self.run_cli("get", "-s", "a=5", "a", "--type", "int")[1], "5\n")
        self.assertEqual(self.run_cli("get", "-s", "a=2KiB", "a", "--type", "size")[1], "2048\n")

    def test_wrong_type(self):
        code, _, err = self.run_cli("get", "-s", "a=x", "a", "--type", "int")
        self.assertEqual(code, 1)
        self.assertIn("expected an integer", err)

    def test_bad_path_syntax(self):
        code, _, err = self.run_cli("get", "-s", "a=x", "a..b")
        self.assertEqual(code, 2)
        self.assertIn("invalid path", err)


class ErrorHandlingTests(CliTestCase):
    def test_parse_error_shows_location(self):
        path = self.write("bad.toml", "a = 1\nb = \n")
        code, out, err = self.run_cli("show", "-f", path)
        self.assertEqual((code, out), (2, ""))
        self.assertIn(f"{path}:2:", err)
        self.assertIn("^", err)

    def test_missing_file(self):
        code, _, err = self.run_cli("show", "-f", str(self.dir / "nope.toml"))
        self.assertEqual(code, 2)
        self.assertIn("file not found", err)

    def test_cycle_is_shown(self):
        path = self.write("c.toml", 'a = "${b}"\nb = "${a}"\n')
        code, _, err = self.run_cli("show", "-f", path)
        self.assertEqual(code, 2)
        self.assertIn("a -> b -> a", err)

    def test_unknown_profile(self):
        code, _, err = self.run_cli("show", "-f", self.base, "-p", "nope")
        self.assertEqual(code, 2)
        self.assertIn("available: dev, prod", err)

    def test_bad_merge_rule(self):
        for rule in ["noequals", "a=bogus", "=append"]:
            with self.subTest(rule=rule):
                code, _, err = self.run_cli("show", "-m", rule)
                self.assertEqual(code, 2)
                self.assertTrue(err.startswith("error: "))

    def test_strict_types(self):
        code, _, err = self.run_cli("show", "-f", self.base, "-s", "server.port=text", "--strict-types")
        self.assertEqual(code, 2)
        self.assertIn("server.port", err)

    def test_toml_output_of_unrepresentable_value(self):
        path = self.write("n.json", '{"a": [null]}')
        code, _, err = self.run_cli("show", "-f", path, "--format", "toml")
        self.assertEqual(code, 2)
        self.assertIn("TOML cannot represent null", err)

    def test_usage_error_exits_with_two(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                main(["show", "--bogus"])
        self.assertEqual(caught.exception.code, 2)

    def test_command_is_required(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                main([])
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
