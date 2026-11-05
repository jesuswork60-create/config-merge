import datetime as dt
import pathlib
import tomllib
import unittest

from configmerge.config import Config
from configmerge.errors import ConfigKeyError, ConfigTypeError, PathSyntaxError
from configmerge.loader import ConfigLoader


def make(data):
    return Config(data)


class LookupTests(unittest.TestCase):
    def setUp(self):
        self.config = make({"server": {"port": 80, "hosts": ["a", "b"]}, "name": "x", "nothing": None})

    def test_dotted_and_index(self):
        self.assertEqual(self.config["server.port"], 80)
        self.assertEqual(self.config["server.hosts[1]"], "b")
        self.assertEqual(self.config[("server", "hosts", 0)], "a")

    def test_get_default(self):
        self.assertEqual(self.config.get("server.port"), 80)
        self.assertIsNone(self.config.get("server.nope"))
        self.assertEqual(self.config.get("server.nope", 5), 5)

    def test_stored_none_is_returned_not_default(self):
        self.assertIsNone(self.config.get("nothing", "default"))

    def test_missing_key_suggests(self):
        with self.assertRaises(ConfigKeyError) as caught:
            self.config["server.prot"]
        self.assertEqual(str(caught.exception), "no such key: server.prot (did you mean server.port?)")

    def test_missing_key_is_a_key_error(self):
        with self.assertRaises(KeyError):
            self.config["zzzzzz"]

    def test_contains(self):
        self.assertIn("server.port", self.config)
        self.assertIn("nothing", self.config)
        self.assertNotIn("server.nope", self.config)
        self.assertNotIn(5, self.config)

    def test_contains_tolerates_bad_syntax(self):
        self.assertNotIn("a..b", self.config)

    def test_bad_syntax_raises_on_read(self):
        with self.assertRaises(PathSyntaxError):
            self.config["a..b"]

    def test_mapping_protocol_over_top_level(self):
        self.assertEqual(set(self.config), {"server", "name", "nothing"})
        self.assertEqual(len(self.config), 3)
        self.assertEqual(dict(self.config.items())["name"], "x")
        self.assertEqual(self.config, {"server": {"port": 80, "hosts": ["a", "b"]}, "name": "x", "nothing": None})

    def test_top_level_key_with_a_dot_works_through_the_mapping_protocol(self):
        config = make({"a.b": 1, "c": 2})
        self.assertEqual(dict(config.items()), {"a.b": 1, "c": 2})
        self.assertEqual(config["a.b"], 1)
        self.assertIn("a.b", config)

    def test_quoted_path_reaches_a_nested_dotted_key(self):
        self.assertEqual(make({"t": {"a.b": 1}})['t."a.b"'], 1)

    def test_results_are_copies(self):
        hosts = self.config["server.hosts"]
        hosts.append("c")
        self.assertEqual(self.config["server.hosts"], ["a", "b"])
        table = self.config.get("server")
        table["port"] = 1
        self.assertEqual(self.config["server.port"], 80)
        dumped = self.config.to_dict()
        dumped["server"]["port"] = 1
        self.assertEqual(self.config["server.port"], 80)


class TypedGetterTests(unittest.TestCase):
    def test_get_str(self):
        self.assertEqual(make({"a": "x"}).get_str("a"), "x")
        with self.assertRaises(ConfigTypeError):
            make({"a": 1}).get_str("a")

    def test_get_int(self):
        config = make({"i": 5, "s": "6", "f": 7.0, "x": 7.5, "b": True, "t": "x"})
        self.assertEqual(config.get_int("i"), 5)
        self.assertEqual(config.get_int("s"), 6)
        self.assertEqual(config.get_int("f"), 7)
        for key in ("x", "b", "t"):
            with self.subTest(key=key):
                with self.assertRaises(ConfigTypeError):
                    config.get_int(key)

    def test_get_float(self):
        config = make({"i": 5, "s": "1.5", "b": True})
        self.assertEqual(config.get_float("i"), 5.0)
        self.assertIsInstance(config.get_float("i"), float)
        self.assertEqual(config.get_float("s"), 1.5)
        with self.assertRaises(ConfigTypeError):
            config.get_float("b")

    def test_get_bool(self):
        config = make({"b": False, "s": "yes", "i": 1, "t": "maybe"})
        self.assertIs(config.get_bool("b"), False)
        self.assertIs(config.get_bool("s"), True)
        for key in ("i", "t"):
            with self.assertRaises(ConfigTypeError):
                config.get_bool(key)

    def test_get_list(self):
        config = make({"l": [1, 2], "m": [1, "x"], "b": [True], "s": "x"})
        self.assertEqual(config.get_list("l", item_type=int), [1, 2])
        with self.assertRaisesRegex(ConfigTypeError, "a list of int"):
            config.get_list("m", item_type=int)
        with self.assertRaises(ConfigTypeError):
            config.get_list("b", item_type=int)
        with self.assertRaises(ConfigTypeError):
            config.get_list("s")

    def test_get_path(self):
        self.assertEqual(make({"p": "/var/lib"}).get_path("p"), pathlib.Path("/var/lib"))

    def test_get_duration(self):
        config = make({"s": "1h30m", "n": 90, "f": 1.5, "b": True, "x": "soon"})
        self.assertEqual(config.get_duration("s"), dt.timedelta(minutes=90))
        self.assertEqual(config.get_duration("n"), dt.timedelta(seconds=90))
        self.assertEqual(config.get_duration("f"), dt.timedelta(seconds=1.5))
        for key in ("b", "x"):
            with self.assertRaises(ConfigTypeError):
                config.get_duration(key)

    def test_get_size(self):
        config = make({"s": "2KiB", "n": 512, "x": "lots"})
        self.assertEqual(config.get_size("s"), 2048)
        self.assertEqual(config.get_size("n"), 512)
        with self.assertRaises(ConfigTypeError):
            config.get_size("x")

    def test_defaults_are_returned_unconverted(self):
        config = make({})
        self.assertEqual(config.get_int("a", 5), 5)
        self.assertIsNone(config.get_str("a", None))
        with self.assertRaises(ConfigKeyError):
            config.get_int("a")

    def test_type_error_names_path_and_value(self):
        with self.assertRaisesRegex(ConfigTypeError, "t.a: expected an integer, got 'x'"):
            make({"t": {"a": "x"}}).get_int("t.a")

    def test_type_error_hides_secrets(self):
        with self.assertRaises(ConfigTypeError) as caught:
            make({"password": "hunter22"}).get_int("password")
        self.assertNotIn("hunter22", str(caught.exception))


class SectionTests(unittest.TestCase):
    def setUp(self):
        self.config = (
            ConfigLoader()
            .add_dict({"db": {"primary": {"host": "h", "password": "hunter22"}, "note": "x"}}, name="base")
            .load()
        )

    def test_reads_relative(self):
        section = self.config.section("db.primary")
        self.assertEqual(section["host"], "h")
        self.assertEqual(set(section), {"host", "password"})

    def test_section_of_section(self):
        self.assertEqual(self.config.section("db").section("primary")["host"], "h")

    def test_explain_uses_full_path(self):
        explanation = self.config.section("db.primary").explain("host")
        self.assertEqual(explanation.path, ("db", "primary", "host"))
        self.assertEqual(explanation.source, "base")

    def test_error_message_uses_full_path(self):
        with self.assertRaisesRegex(ConfigKeyError, "no such key: db.primary.nope"):
            self.config.section("db.primary")["nope"]

    def test_redaction_applies(self):
        self.assertEqual(self.config.section("db.primary").redacted()["password"], "***")

    def test_not_a_table(self):
        with self.assertRaises(ConfigTypeError):
            self.config.section("db.note")
        with self.assertRaises(ConfigKeyError):
            self.config.section("nope")


class OutputTests(unittest.TestCase):
    DATA = {"a": 1, "t": {"password": "hunter22", "dsn": "x:hunter22"}, "l": [{"k": 1}]}

    def test_flatten(self):
        flat = make({"a": {"b": 1}, "l": [1, {"x": 2}]}).flatten()
        self.assertEqual(flat, {"a.b": 1, "l[0]": 1, "l[1].x": 2})

    def test_flatten_redacted(self):
        self.assertEqual(make(self.DATA).flatten(redact=True)["t.password"], "***")

    def test_repr_never_shows_secrets(self):
        text = repr(make(self.DATA))
        self.assertNotIn("hunter22", text)
        self.assertIn("***", text)

    def test_writers_show_values_unless_asked(self):
        config = make(self.DATA)
        self.assertIn("hunter22", config.to_json())
        for text in (config.to_json(redact=True), config.to_toml(redact=True)):
            self.assertNotIn("hunter22", text)

    def test_toml_output_parses(self):
        self.assertEqual(tomllib.loads(make(self.DATA).to_toml()), self.DATA)

    def test_env_output(self):
        text = make({"a": {"b": 1}, "password": "hunter22"}).to_env(prefix="P", redact=True)
        self.assertIn("P_A__B=1", text)
        self.assertNotIn("hunter22", text)

    def test_explain_text_is_masked(self):
        config = ConfigLoader().add_dict({"password": "first-one"}, name="a").add_dict({"password": "second-one"}, name="b").load()
        text = config.explain_text("password")
        self.assertNotIn("first-one", text)
        self.assertNotIn("second-one", text)
        self.assertIn("from b", text)

    def test_layers(self):
        config = ConfigLoader().add_dict({}, name="a").add_dict({}, name="b").load()
        self.assertEqual(config.layers, ["a", "b"])


if __name__ == "__main__":
    unittest.main()
