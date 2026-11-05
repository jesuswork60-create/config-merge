import tempfile
import unittest
from pathlib import Path

from configmerge import (
    ConfigLoader,
    DictSource,
    EnvVars,
    Field,
    InterpolationCycleError,
    MergeConflictError,
    ProfileError,
    Redactor,
    Schema,
    ValidationError,
    load_config,
    schema_from_spec,
)
from configmerge.provenance import Action
from configmerge.sources import Layer


class PipelineTests(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.dir = Path(holder.name)

    def write(self, name, text):
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_precedence_follows_order_added(self):
        base = self.write("base.toml", 'a = "toml"\nb = "toml"\nc = "toml"\nd = "toml"\n')
        extra = self.write("extra.json", '{"b": "json", "c": "json", "d": "json"}')
        config = (
            ConfigLoader(environ={"APP_C": "env", "APP_D": "env"})
            .add_dict({"a": "dict", "z": "dict"})
            .add_file(base)
            .add_file(extra)
            .add_env("APP")
            .add_overrides(["d=cli"])
            .load()
        )
        self.assertEqual(config.to_dict(), {"a": "toml", "z": "dict", "b": "json", "c": "env", "d": "cli"})
        self.assertEqual(config.explain("d").source, "cli")
        self.assertEqual(config.explain("c").source, "env:APP")
        self.assertEqual(config.explain("c").events[-1].note, "APP_C")
        self.assertEqual(config.explain("a").source, str(base))

    def test_optional_file_and_directory_layers(self):
        self.write("conf/10.toml", "x = 1\ny = 1\n")
        self.write("conf/20.toml", "y = 2\n")
        config = (
            ConfigLoader()
            .add_directory(self.dir / "conf")
            .add_file(self.dir / "absent.toml", optional=True)
            .add_directory(self.dir / "none", optional=True)
            .load()
        )
        self.assertEqual(config.to_dict(), {"x": 1, "y": 2})
        self.assertEqual(len(config.layers), 2)

    def test_env_file(self):
        path = self.write("x.env", "APP_A=1\nOTHER=2\n")
        self.assertEqual(ConfigLoader().add_env_file(path, prefix="APP").load().to_dict(), {"a": 1})

    def test_rules_apply_across_sources(self):
        config = load_config(
            DictSource({"hosts": ["a"]}),
            EnvVars("APP", environ={"APP_HOSTS": '["a","b"]'}),
            rules={"hosts": "unique"},
        )
        self.assertEqual(config["hosts"], ["a", "b"])
        self.assertEqual(config.explain("hosts[1]").source, "env:APP")

    def test_strict_types_option(self):
        loader = ConfigLoader(DictSource({"a": 1}), DictSource({"a": "x"}), strict_types=True)
        with self.assertRaises(MergeConflictError):
            loader.load()

    def test_load_twice_rereads_sources(self):
        path = self.write("a.toml", "x = 1\n")
        loader = ConfigLoader().add_file(path)
        self.assertEqual(loader.load()["x"], 1)
        path.write_text("x = 2\n", encoding="utf-8")
        self.assertEqual(loader.load()["x"], 2)

    def test_custom_source(self):
        class Fixed(DictSource):
            def load(self):
                return [Layer("one", {"a": 1}), Layer("two", {"a": 2})]

        config = load_config(Fixed({}))
        self.assertEqual(config["a"], 2)
        self.assertEqual(config.layers, ["one", "two"])


class ProfileTests(unittest.TestCase):
    BASE = {
        "level": "info",
        "server": {"host": "h", "port": 1},
        "profile": {
            "dev": {"level": "debug", "server": {"port": 2}},
            "local": {"server": {"port": 3}},
        },
    }

    def load(self, *profiles, **options):
        return load_config(DictSource(self.BASE, name="base"), profiles=profiles, **options)

    def test_section_removed_when_inactive(self):
        self.assertNotIn("profile", self.load().to_dict())

    def test_overlay(self):
        config = self.load("dev")
        self.assertEqual(config["level"], "debug")
        self.assertEqual(config["server"], {"host": "h", "port": 2})

    def test_provenance_names_the_overlay(self):
        explanation = self.load("dev").explain("server.port")
        self.assertEqual(explanation.source, "base#profile.dev")
        self.assertEqual(explanation.overridden[0].layer, "base")

    def test_requested_order_decides(self):
        self.assertEqual(self.load("dev", "local")["server.port"], 3)
        self.assertEqual(self.load("local", "dev")["server.port"], 2)

    def test_later_layer_beats_earlier_profile(self):
        config = load_config(
            DictSource(self.BASE, name="base"),
            DictSource({"level": "warn"}, name="after"),
            profiles=["dev"],
        )
        self.assertEqual(config["level"], "warn")

    def test_later_layers_profile_beats_everything_before(self):
        config = load_config(
            DictSource(self.BASE),
            DictSource({"profile": {"dev": {"level": "trace"}}}),
            profiles=["dev"],
        )
        self.assertEqual(config["level"], "trace")

    def test_profile_declared_only_by_one_layer_is_enough(self):
        config = load_config(DictSource({"a": 1}), DictSource({"profile": {"x": {"a": 2}}}), profiles=["x"])
        self.assertEqual(config["a"], 2)

    def test_unknown_profile(self):
        with self.assertRaisesRegex(ProfileError, r"unknown profile prod \(available: dev, local\)"):
            self.load("prod")

    def test_no_profiles_declared(self):
        with self.assertRaisesRegex(ProfileError, "available: none"):
            load_config(DictSource({"a": 1}), profiles=["x"])

    def test_malformed_sections(self):
        with self.assertRaisesRegex(ProfileError, "must be a table of profiles"):
            load_config(DictSource({"profile": "dev"}))
        with self.assertRaisesRegex(ProfileError, "profile 'dev' must be a table"):
            load_config(DictSource({"profile": {"dev": 1}}))

    def test_profile_from_environment(self):
        config = self.load(profile_env="APP_PROFILE", environ={"APP_PROFILE": "dev, local"})
        self.assertEqual(config["server.port"], 3)

    def test_environment_profiles_come_before_configured_ones(self):
        config = load_config(
            DictSource(self.BASE),
            profiles=["dev"],
            profile_env="P",
            environ={"P": "local"},
        )
        self.assertEqual(config["server.port"], 2)

    def test_profile_env_unset_is_fine(self):
        self.assertEqual(self.load(profile_env="P", environ={})["level"], "info")

    def test_custom_key(self):
        config = load_config(
            DictSource({"a": 1, "env": {"x": {"a": 2}}, "profile": {"kept": True}}),
            profiles=["x"],
            profile_key="env",
        )
        self.assertEqual(config.to_dict(), {"a": 2, "profile": {"kept": True}})

    def test_env_vars_can_declare_overlays_and_keep_notes(self):
        config = load_config(
            EnvVars("APP", environ={"APP_PROFILE__DEV__A": "5"}),
            profiles=["dev"],
        )
        self.assertEqual(config["a"], 5)
        self.assertEqual(config.explain("a").events[-1].note, "APP_PROFILE__DEV__A")

    def test_rules_apply_to_overlays(self):
        config = load_config(
            DictSource({"l": [1], "profile": {"dev": {"l": [2]}}}),
            profiles=["dev"],
            rules={"l": "append"},
        )
        self.assertEqual(config["l"], [1, 2])

    def test_interpolation_sees_profile_values(self):
        config = load_config(
            DictSource({"host": "prod", "url": "http://${host}", "profile": {"dev": {"host": "dev"}}}),
            profiles=["dev"],
        )
        self.assertEqual(config["url"], "http://dev")


class InterpolationStageTests(unittest.TestCase):
    def test_runs_on_the_merged_result(self):
        config = load_config(DictSource({"h": "a", "u": "${h}"}), DictSource({"h": "b"}))
        self.assertEqual(config["u"], "b")

    def test_can_be_disabled(self):
        config = load_config(DictSource({"u": "${h}"}), interpolate=False)
        self.assertEqual(config["u"], "${h}")

    def test_uses_injected_environment_and_resolvers(self):
        config = load_config(
            DictSource({"a": "${env:X}", "b": "${r:k}"}),
            environ={"X": "1"},
            resolvers={"r": lambda name: name * 2},
        )
        self.assertEqual(config.to_dict(), {"a": "1", "b": "kk"})

    def test_cycle_surfaces(self):
        with self.assertRaises(InterpolationCycleError):
            load_config(DictSource({"a": "${b}", "b": "${a}"}))

    def test_explain_shows_raw_and_references(self):
        config = load_config(DictSource({"h": "a", "u": "x${h}"}, name="base"))
        explanation = config.explain("u")
        self.assertEqual(explanation.source, "base")
        self.assertEqual(explanation.value, "xa")
        self.assertEqual([str(r) for r in explanation.references], ["h"])
        self.assertIn("x${h}", explanation.render())
        self.assertIs(explanation.events[-1].action, Action.INTERPOLATE)

    def test_explain_inside_aliased_table(self):
        config = load_config(DictSource({"real": {"k": 1}, "alias": "${real}"}, name="base"))
        explanation = config.explain("alias.k")
        self.assertTrue(explanation.exists)
        self.assertEqual(explanation.source, "base")

    def test_interpolated_list_element(self):
        config = load_config(DictSource({"h": "a", "l": ["${h}", "b"]}, name="base"))
        self.assertEqual(config.explain("l[0]").source, "base")
        self.assertEqual(config.explain("l[0]").value, "a")


class SchemaStageTests(unittest.TestCase):
    def test_defaults_recorded_in_provenance(self):
        config = load_config(DictSource({}), schema={"a": Field(int, default=1)})
        self.assertEqual(config["a"], 1)
        self.assertEqual(config.explain("a").source, "<schema default>")

    def test_defaults_can_interpolate(self):
        schema = {"host": str, "url": Field(str, default="http://${host}")}
        self.assertEqual(load_config(DictSource({"host": "h"}), schema=schema)["url"], "http://h")

    def test_validation_sees_resolved_values(self):
        schema = {"base": int, "port": Field(int, max=10)}
        with self.assertRaises(ValidationError):
            load_config(DictSource({"base": 99, "port": "${base}"}), schema=schema)

    def test_issue_names_the_layer_responsible(self):
        schema = Schema({"t": {"port": Field(int, max=10), "name": str}})
        with self.assertRaises(ValidationError) as caught:
            load_config(
                DictSource({"t": {"port": 1}}, name="base"),
                EnvVars("APP", environ={"APP_T__PORT": "99"}),
                schema=schema,
            )
        issues = {issue.path: issue for issue in caught.exception.issues}
        self.assertEqual(issues[("t", "port")].origin, "env:APP")
        self.assertIn("(set by env:APP)", str(issues[("t", "port")]))
        self.assertIsNone(issues[("t", "name")].origin)

    def test_issue_for_list_element_names_its_layer(self):
        schema = {"l": [Field(int)]}
        with self.assertRaises(ValidationError) as caught:
            load_config(
                DictSource({"l": [1]}, name="a"),
                DictSource({"l": ["x"]}, name="b"),
                schema=schema,
                rules={"l": "append"},
            )
        self.assertEqual(caught.exception.issues[0].origin, "b")

    def test_coercion_of_interpolated_env_value(self):
        schema = Schema({"port": int}, coerce=True)
        config = load_config(DictSource({"port": "${env:PORT}"}), schema=schema, environ={"PORT": "80"})
        self.assertEqual(config["port"], 80)
        self.assertEqual(config.explain("port").value, 80)

    def test_profile_section_is_invisible_to_the_schema(self):
        config = load_config(DictSource({"a": 1, "profile": {"d": {"a": 2}}}), schema={"a": int})
        self.assertEqual(config["a"], 1)

    def test_strip_policy_removes_from_config(self):
        schema = Schema({"a": int}, unknown="strip")
        self.assertNotIn("b", load_config(DictSource({"a": 1, "b": 2}), schema=schema))

    def test_schema_secret_flag_reaches_redaction(self):
        schema = {"dsn": Field(str, secret=True), "note": str}
        config = load_config(DictSource({"dsn": "abc-secret-xyz", "note": "see abc-secret-xyz"}), schema=schema)
        self.assertEqual(config.redacted(), {"dsn": "***", "note": "see ***"})

    def test_name_based_secret_hidden_in_validation_errors(self):
        with self.assertRaises(ValidationError) as caught:
            load_config(DictSource({"api_key": "hunter22"}), schema={"api_key": int})
        self.assertNotIn("hunter22", str(caught.exception))

    def test_secrets_option_and_custom_redactor(self):
        config = load_config(
            DictSource({"odd": "v", "password": "visible", "pin": "1"}),
            secrets=["pin"],
            redactor=Redactor(key_regex=None, mask="#"),
        )
        self.assertEqual(config.redacted(), {"odd": "v", "password": "visible", "pin": "#"})

    def test_schema_from_spec_end_to_end(self):
        schema = schema_from_spec({"server": {"port": {"type": "int", "default": 8000}}})
        self.assertEqual(load_config(DictSource({}), schema=schema)["server.port"], 8000)

    def test_secret_built_from_env_is_masked_end_to_end(self):
        config = load_config(DictSource({"dsn": "pg://${env:DB_TOKEN}@h"}), environ={"DB_TOKEN": "ab"})
        self.assertEqual(config.redacted(), {"dsn": "***"})
        self.assertNotIn("pg://ab", config.explain_text("dsn"))


if __name__ == "__main__":
    unittest.main()
