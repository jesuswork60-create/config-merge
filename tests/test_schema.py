import datetime as dt
import math
import unittest

from configmerge.errors import SchemaDefinitionError, ValidationError
from configmerge.schema import Field, Schema
from configmerge.schema_spec import schema_from_spec


def issues_of(schema, data, **options):
    result = schema.validate(data, **options)
    return {str_path(issue.path): issue for issue in result.issues}


def str_path(path):
    from configmerge.paths import format_path

    return format_path(path)


class TypeTests(unittest.TestCase):
    def assertRejected(self, kind, value):
        issues = issues_of(Schema({"a": Field(kind)}), {"a": value})
        self.assertEqual(issues["a"].code, "type", (kind, value))

    def assertAccepted(self, kind, value):
        self.assertTrue(Schema({"a": Field(kind)}).validate({"a": value}).ok, (kind, value))

    def test_scalars(self):
        self.assertAccepted(str, "x")
        self.assertAccepted(int, 1)
        self.assertAccepted(float, 1.5)
        self.assertAccepted(bool, True)
        self.assertRejected(str, 1)
        self.assertRejected(int, "1")
        self.assertRejected(int, 1.5)
        self.assertRejected(float, "1.5")

    def test_bool_is_not_a_number(self):
        self.assertRejected(int, True)
        self.assertRejected(float, False)
        self.assertRejected(bool, 1)

    def test_int_given_for_float_is_converted(self):
        value = Schema({"a": Field(float)}).check({"a": 1})
        self.assertIsInstance(value["a"], float)

    def test_int_in_number_union_stays_int(self):
        value = Schema({"a": Field((int, float))}).check({"a": 1})
        self.assertIsInstance(value["a"], int)

    def test_dates(self):
        self.assertAccepted(dt.date, dt.date(2026, 1, 1))
        self.assertAccepted(dt.datetime, dt.datetime(2026, 1, 1))
        self.assertRejected(dt.date, dt.datetime(2026, 1, 1))

    def test_union(self):
        schema = Schema({"a": Field((int, str))})
        self.assertTrue(schema.validate({"a": 1}).ok)
        self.assertTrue(schema.validate({"a": "x"}).ok)
        self.assertIn("integer or string", str(schema.validate({"a": 1.5}).issues[0]))

    def test_any(self):
        schema = Schema({"a": Field()})
        for value in [1, "x", [1], {"k": 1}]:
            self.assertTrue(schema.validate({"a": value}).ok)

    def test_null_needs_nullable(self):
        self.assertEqual(issues_of(Schema({"a": Field(int)}), {"a": None})["a"].code, "type")
        self.assertTrue(Schema({"a": Field(int, nullable=True)}).validate({"a": None}).ok)
        self.assertEqual(issues_of(Schema({"a": Field()}), {"a": None})["a"].code, "type")


class ConstraintTests(unittest.TestCase):
    def test_number_bounds(self):
        schema = Schema({"a": Field(int, min=1, max=10)})
        self.assertTrue(schema.validate({"a": 1}).ok)
        self.assertTrue(schema.validate({"a": 10}).ok)
        self.assertEqual(issues_of(schema, {"a": 0})["a"].code, "min")
        self.assertEqual(issues_of(schema, {"a": 11})["a"].code, "max")

    def test_nan_never_passes_bounds(self):
        schema = Schema({"a": Field(float, min=0.0)})
        self.assertFalse(schema.validate({"a": math.nan}).ok)

    def test_length_bounds(self):
        schema = Schema({"s": Field(str, min=2), "l": Field(list, max=1), "t": Field(dict, min=1)})
        issues = issues_of(schema, {"s": "x", "l": [1, 2], "t": {}})
        self.assertEqual(set(issues), {"s", "l", "t"})
        self.assertIn("length 1", issues["s"].message)

    def test_choices(self):
        schema = Schema({"a": Field(str, choices=["x", "y"])})
        self.assertTrue(schema.validate({"a": "x"}).ok)
        issue = issues_of(schema, {"a": "z"})["a"]
        self.assertEqual(issue.code, "choice")
        self.assertIn("'x', 'y'", issue.message)

    def test_choices_keep_true_and_one_apart(self):
        schema = Schema({"a": Field(choices=[1])})
        self.assertFalse(schema.validate({"a": True}).ok)

    def test_pattern_matches_whole_string(self):
        schema = Schema({"a": Field(str, pattern="[a-z]+")})
        self.assertTrue(schema.validate({"a": "abc"}).ok)
        self.assertEqual(issues_of(schema, {"a": "abc1"})["a"].code, "pattern")

    def test_custom_check_forms(self):
        def raises(value):
            raise ValueError("nope from raise")

        cases = [
            (lambda v: False, "failed custom check"),
            (lambda v: "nope from string", "nope from string"),
            (raises, "nope from raise"),
        ]
        for check, expected in cases:
            issues = issues_of(Schema({"a": Field(check=check)}), {"a": 1})
            self.assertEqual(issues["a"].message, expected)
        self.assertTrue(Schema({"a": Field(check=lambda v: True)}).validate({"a": 1}).ok)
        self.assertTrue(Schema({"a": Field(check=lambda v: None)}).validate({"a": 1}).ok)

    def test_several_constraints_all_reported(self):
        schema = Schema({"a": Field(str, min=5, pattern="[0-9]+")})
        codes = [issue.code for issue in schema.validate({"a": "x"}).issues]
        self.assertEqual(codes, ["min", "pattern"])


class StructureTests(unittest.TestCase):
    def test_nested_shorthand(self):
        schema = Schema({"server": {"ports": [int], "host": str}})
        self.assertTrue(schema.validate({"server": {"ports": [1, 2], "host": "h"}}).ok)

    def test_every_error_is_reported_with_its_path(self):
        schema = Schema(
            {
                "server": {
                    "host": str,
                    "ports": Field(list, items=Field(int, min=1)),
                },
                "workers": Field(int, min=1),
                "name": str,
            }
        )
        issues = issues_of(schema, {"server": {"host": 5, "ports": [80, 0, "x"]}, "workers": 0})
        self.assertEqual(
            set(issues),
            {"server.host", "server.ports[1]", "server.ports[2]", "workers", "name"},
        )
        self.assertEqual(issues["server.ports[1]"].code, "min")
        self.assertEqual(issues["server.ports[2]"].code, "type")
        self.assertEqual(issues["name"].code, "missing")

    def test_list_of_tables(self):
        schema = Schema({"backends": [{"host": str, "weight": Field(int, default=1)}]})
        value = schema.check({"backends": [{"host": "a"}, {"host": "b", "weight": 2}]})
        self.assertEqual(value["backends"], [{"host": "a", "weight": 1}, {"host": "b", "weight": 2}])
        issues = issues_of(schema, {"backends": [{"host": "a"}, {}]})
        self.assertIn("backends[1].host", issues)

    def test_wrong_container_does_not_descend(self):
        schema = Schema({"t": {"a": int}, "l": [int]})
        issues = issues_of(schema, {"t": 5, "l": "x"})
        self.assertEqual(set(issues), {"t", "l"})

    def test_values_map(self):
        schema = Schema({"limits": Field(values=Field(int, min=0))})
        issues = issues_of(schema, {"limits": {"a": 1, "b": -1, "c": "x"}})
        self.assertEqual(set(issues), {"limits.b", "limits.c"})

    def test_fields_and_values_together(self):
        schema = Schema({"t": Field(fields={"fixed": str}, values=Field(int))})
        self.assertTrue(schema.validate({"t": {"fixed": "x", "other": 1}}).ok)
        self.assertFalse(schema.validate({"t": {"fixed": 1, "other": 1}}).ok)

    def test_plain_table_holds_anything(self):
        schema = Schema({"t": dict})
        self.assertTrue(schema.validate({"t": {"any": {"thing": 1}}}).ok)


class MissingAndDefaultTests(unittest.TestCase):
    def test_required_by_default(self):
        self.assertEqual(issues_of(Schema({"a": int}), {})["a"].code, "missing")

    def test_optional(self):
        schema = Schema({"a": Field(int, required=False)})
        self.assertEqual(schema.check({}), {})

    def test_default_fills(self):
        self.assertEqual(Schema({"a": Field(int, default=5)}).check({}), {"a": 5})

    def test_default_is_copied(self):
        schema = Schema({"a": Field(list, default=[])})
        first = schema.check({})
        first["a"].append(1)
        self.assertEqual(schema.check({}), {"a": []})

    def test_default_is_validated(self):
        self.assertEqual(issues_of(Schema({"a": Field(int, default="x")}), {})["a"].code, "type")

    def test_absent_table_reports_required_children_by_path(self):
        issues = issues_of(Schema({"t": {"a": int, "b": Field(int, default=1)}}), {})
        self.assertEqual(set(issues), {"t.a"})

    def test_absent_table_of_defaults_is_created(self):
        self.assertEqual(Schema({"t": {"a": Field(int, default=1)}}).check({}), {"t": {"a": 1}})

    def test_absent_table_with_nothing_to_fill_stays_absent(self):
        self.assertEqual(Schema({"t": {"a": Field(int, required=False)}}).check({}), {})

    def test_optional_table_with_required_children_may_be_absent(self):
        schema = Schema({"t": Field(fields={"a": int}, required=False)})
        self.assertEqual(schema.check({}), {})
        self.assertFalse(schema.validate({"t": {}}).ok)

    def test_required_table(self):
        schema = Schema({"t": Field(fields={"a": Field(int, required=False)}, required=True)})
        self.assertEqual(issues_of(schema, {})["t"].code, "missing")

    def test_apply_defaults_reports_each_fill_and_does_not_check(self):
        filled = []
        schema = Schema({"t": {"a": Field(int, default=1), "b": str}, "c": Field(int, default=2), "d": int})
        result = schema.apply_defaults({"d": "not an int"}, lambda path, value: filled.append((path, value)))
        self.assertEqual(result, {"d": "not an int", "t": {"a": 1}, "c": 2})
        self.assertEqual(filled, [(("t", "a"), 1), (("c",), 2)])

    def test_input_not_mutated(self):
        data = {"t": {}}
        Schema({"t": {"a": Field(int, default=1)}}).check(data)
        self.assertEqual(data, {"t": {}})


class UnknownKeyTests(unittest.TestCase):
    DATA = {"host": "h", "prot": 1}

    def test_forbid_is_default_and_suggests(self):
        schema = Schema({"host": str, "port": Field(int, default=1)})
        issue = issues_of(schema, self.DATA)["prot"]
        self.assertEqual(issue.code, "unknown")
        self.assertIn("did you mean 'port'", issue.message)

    def test_allow_keeps(self):
        schema = Schema({"host": str}, unknown="allow")
        self.assertEqual(schema.check(self.DATA), self.DATA)

    def test_strip_removes(self):
        schema = Schema({"host": str}, unknown="strip")
        self.assertEqual(schema.check(self.DATA), {"host": "h"})

    def test_field_policy_overrides_and_inherits(self):
        schema = Schema(
            {
                "open": Field(fields={"a": Field(int, default=1), "inner": {"b": Field(int, default=1)}}, unknown="allow"),
                "closed": {"a": Field(int, default=1)},
            }
        )
        data = {"open": {"x": 1, "inner": {"y": 1}}, "closed": {"z": 1}}
        issues = issues_of(schema, data)
        self.assertEqual(set(issues), {"closed.z"})

    def test_bad_policy(self):
        with self.assertRaises(SchemaDefinitionError):
            Schema({"a": int}, unknown="sometimes")
        with self.assertRaises(SchemaDefinitionError):
            Field(dict, unknown="sometimes")


class CoercionTests(unittest.TestCase):
    def test_off_by_default(self):
        self.assertFalse(Schema({"a": int}).validate({"a": "5"}).ok)

    def test_converts_strings(self):
        schema = Schema(
            {"i": int, "f": float, "b": bool, "d": dt.date, "l": [int]},
            coerce=True,
        )
        value = schema.check({"i": "5", "f": "1.5", "b": "yes", "d": "2026-01-02", "l": ["1", "2"]})
        self.assertEqual(value, {"i": 5, "f": 1.5, "b": True, "d": dt.date(2026, 1, 2), "l": [1, 2]})

    def test_constraints_see_the_converted_value(self):
        schema = Schema({"a": Field(int, max=10)}, coerce=True)
        self.assertEqual(issues_of(schema, {"a": "99"})["a"].code, "max")

    def test_failed_conversion_is_a_type_issue(self):
        self.assertEqual(issues_of(Schema({"a": int}, coerce=True), {"a": "x"})["a"].code, "type")

    def test_string_in_union_is_left_alone(self):
        self.assertEqual(Schema({"a": Field((int, str))}, coerce=True).check({"a": "5"}), {"a": "5"})


class SecretTests(unittest.TestCase):
    def test_secret_value_stays_out_of_messages(self):
        schema = Schema(
            {
                "a": Field(int, secret=True),
                "b": Field(str, secret=True, choices=["x"]),
                "c": Field(str, secret=True, pattern="[0-9]+"),
                "d": Field(fields={"inner": int}, secret=True),
            }
        )
        data = {"a": "hunter2", "b": "hunter2", "c": "hunter2", "d": {"inner": "hunter2"}}
        for issue in schema.validate(data).issues:
            self.assertNotIn("hunter2", str(issue))

    def test_external_secret_predicate(self):
        schema = Schema({"token": int})
        issues = schema.validate({"token": "hunter2"}, is_secret=lambda path: path == ("token",)).issues
        self.assertNotIn("hunter2", str(issues[0]))
        self.assertIn("value hidden", str(issues[0]))

    def test_long_values_are_shortened(self):
        issue = Schema({"a": int}).validate({"a": "x" * 500}).issues[0]
        self.assertLess(len(issue.message), 120)

    def test_secret_patterns(self):
        schema = Schema(
            {
                "db": {"password": Field(str, secret=True), "host": str},
                "tokens": [Field(str, secret=True)],
                "keys": Field(values=Field(str, secret=True)),
                "whole": Field(fields={"a": str}, secret=True),
            }
        )
        patterns = schema.secret_patterns()
        for path in [("db", "password"), ("tokens", 0), ("keys", "x"), ("whole",)]:
            self.assertTrue(any(p.matches(path) for p in patterns), path)
        self.assertFalse(any(p.matches(("db", "host")) for p in patterns))


class DefinitionErrorTests(unittest.TestCase):
    def test_bad_definitions(self):
        cases = [
            lambda: Field(int, min=5, max=1),
            lambda: Field(str, choices=[]),
            lambda: Field(str, pattern="("),
            lambda: Field(int, items=int),
            lambda: Field(str, fields={"a": int}),
            lambda: Field(int, pattern="x"),
            lambda: Field(int, required=True, default=1),
            lambda: Field("int"),
            lambda: Schema({"a": [int, str]}),
            lambda: Schema({"a": 5}),
            lambda: Schema(Field(int)),
        ]
        for i, case in enumerate(cases):
            with self.subTest(i=i):
                with self.assertRaises(SchemaDefinitionError):
                    case()

    def test_check_raises_with_all_issues(self):
        with self.assertRaises(ValidationError) as caught:
            Schema({"a": int, "b": int}).check({})
        self.assertEqual(len(caught.exception.issues), 2)
        self.assertIn("2 validation errors", str(caught.exception))
        self.assertIn("a: required integer is missing", str(caught.exception))

    def test_describe_lists_leaves(self):
        names = [name for name, _ in Schema({"a": int, "t": {"b": str}}).describe()]
        self.assertEqual(names, ["a", "t.b"])


class SpecTests(unittest.TestCase):
    def test_shorthand_strings(self):
        schema = schema_from_spec({"a": "int", "b": "str?", "c": "list[int]", "d": "map[float]", "e": "int|str"})
        self.assertTrue(schema.validate({"a": 1, "c": [1], "d": {"k": 1.5}, "e": "x"}).ok)
        issues = issues_of(schema, {"a": "x", "c": ["x"], "d": {"k": "x"}, "e": 1.5})
        self.assertEqual(set(issues), {"a", "c[0]", "d.k", "e"})

    def test_optional_container_shorthand(self):
        self.assertEqual(schema_from_spec({"a": "list[str]?"}).check({}), {})

    def test_nested_tables(self):
        schema = schema_from_spec({"server": {"port": {"type": "int", "min": 1, "max": 10}}})
        self.assertEqual(issues_of(schema, {"server": {"port": 11}})["server.port"].code, "max")

    def test_descriptor_options(self):
        schema = schema_from_spec(
            {
                "level": {"type": "str", "choices": ["a"], "default": "a"},
                "name": {"type": "str", "pattern": "[a-z]+"},
                "pw": {"type": "str", "secret": True},
                "n": {"type": "int", "nullable": True, "required": False},
                "items": {"type": "list", "items": {"type": "int", "min": 0}},
                "tbl": {"type": "object", "fields": {"type": "str"}, "unknown": "allow"},
                "m": {"type": "map", "values": "int"},
            }
        )
        data = {"name": "x", "pw": "p", "items": [1], "tbl": {"type": "t", "other": 1}, "m": {"k": 1}}
        self.assertEqual(schema.check(data)["level"], "a")
        self.assertFalse(schema.validate(dict(data, items=[-1])).ok)
        self.assertTrue(any(p.matches(("pw",)) for p in schema.secret_patterns()))

    def test_list_shorthand_as_data(self):
        schema = schema_from_spec({"a": [{"x": "int"}]})
        self.assertIn("a[0].x", issues_of(schema, {"a": [{"x": "y"}]}))

    def test_key_called_type_via_nested_table(self):
        schema = schema_from_spec({"t": {"type": {"type": "str"}}})
        self.assertTrue(schema.validate({"t": {"type": "x"}}).ok)

    def test_policy_and_coerce_options(self):
        self.assertEqual(schema_from_spec({"a": "int"}, coerce=True).check({"a": "5"}), {"a": 5})
        self.assertEqual(schema_from_spec({"a": "int"}, unknown="strip").check({"a": 1, "b": 2}), {"a": 1})

    def test_errors_name_the_location(self):
        cases = [
            ({"t": {"a": "bogus"}}, "t.a: unknown type 'bogus'"),
            ({"a": {"type": "int", "bogus": 1}}, "a: unknown field options: bogus"),
            ({"a": ["int", "str"]}, "a: list shorthand"),
            ({"a": 5}, "a: cannot read"),
            ({"a": {"type": "object", "fields": 5}}, "a: fields must be a table"),
        ]
        for spec, expected in cases:
            with self.subTest(spec=spec):
                with self.assertRaisesRegex(SchemaDefinitionError, expected):
                    schema_from_spec(spec)


if __name__ == "__main__":
    unittest.main()
