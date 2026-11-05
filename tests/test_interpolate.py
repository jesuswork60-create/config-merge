import unittest

from configmerge.errors import (
    InterpolationCycleError,
    InterpolationError,
    ReferenceNotFoundError,
)
from configmerge.interpolate import Interpolator, Literal, Reference, parse_template
from configmerge.provenance import Action, Provenance


def resolve(data, environ=None, resolvers=None, provenance=None):
    return Interpolator(environ=environ or {}, resolvers=resolvers, provenance=provenance).resolve(data)


class TemplateParserTests(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(parse_template("abc"), (Literal("abc"),))
        self.assertEqual(parse_template(""), ())

    def test_escape(self):
        self.assertEqual(parse_template("a$$b"), (Literal("a$b"),))
        self.assertEqual(parse_template("$${x}"), (Literal("${x}"),))

    def test_lone_dollar_is_literal(self):
        self.assertEqual(parse_template("cost: 5$ or $x"), (Literal("cost: 5$ or $x"),))
        self.assertEqual(parse_template("$"), (Literal("$"),))

    def test_reference_parts(self):
        parts = parse_template("x${a.b}y")
        self.assertEqual(parts[0], Literal("x"))
        self.assertEqual((parts[1].scheme, parts[1].name, parts[1].default), (None, "a.b", None))
        self.assertEqual(parts[2], Literal("y"))

    def test_scheme_and_default(self):
        reference = parse_template("${env:HOME:-/tmp}")[0]
        self.assertIsInstance(reference, Reference)
        self.assertEqual((reference.scheme, reference.name), ("env", "HOME"))
        self.assertEqual(reference.default, (Literal("/tmp"),))

    def test_nested_default(self):
        reference = parse_template("${a:-${b:-c}}")[0]
        inner = reference.default[0]
        self.assertEqual(inner.name, "b")
        self.assertEqual(inner.default, (Literal("c"),))

    def test_empty_default(self):
        self.assertEqual(parse_template("${a:-}")[0].default, ())

    def test_escaped_dollar_in_default(self):
        self.assertEqual(parse_template("${a:-$$5}")[0].default, (Literal("$5"),))

    def test_errors(self):
        for text in ["${", "${a", "x${a:-${b}", "${}", "${:-x}", "${env:}"]:
            with self.subTest(text=text):
                with self.assertRaises(InterpolationError):
                    parse_template(text)

    def test_unterminated_reports_column(self):
        with self.assertRaisesRegex(InterpolationError, "column 4"):
            parse_template("abc${x")


class ResolutionTests(unittest.TestCase):
    def test_string_without_references_untouched(self):
        self.assertEqual(resolve({"a": "plain", "b": 5, "c": None}), {"a": "plain", "b": 5, "c": None})

    def test_embedded_reference(self):
        data = {"host": "h", "port": 80, "url": "http://${host}:${port}/"}
        self.assertEqual(resolve(data)["url"], "http://h:80/")

    def test_nested_path_and_list_index(self):
        data = {"a": {"b": ["x", "y"]}, "c": "${a.b[1]}"}
        self.assertEqual(resolve(data)["c"], "y")

    def test_whole_value_keeps_type(self):
        data = {"n": 5, "f": 1.5, "t": True, "l": [1], "m": {"k": 1}, "z": None}
        for key in data:
            data_with_ref = dict(data, ref="${%s}" % key)
            result = resolve(data_with_ref)["ref"]
            with self.subTest(key=key):
                self.assertEqual(result, data[key])
                self.assertIs(type(result), type(data[key]))

    def test_aliased_containers_are_independent_copies(self):
        result = resolve({"a": {"k": [1]}, "b": "${a}"})
        result["b"]["k"].append(2)
        self.assertEqual(result["a"], {"k": [1]})

    def test_chains(self):
        data = {"a": "${b}", "b": "${c}", "c": "end"}
        self.assertEqual(resolve(data), {"a": "end", "b": "end", "c": "end"})

    def test_order_independent(self):
        self.assertEqual(resolve({"z": "last", "a": "${z}!"})["a"], "last!")

    def test_references_inside_containers(self):
        data = {"x": "v", "l": ["${x}", {"k": "${x}"}], "t": {"k": "${x}"}}
        self.assertEqual(resolve(data), {"x": "v", "l": ["v", {"k": "v"}], "t": {"k": "v"}})

    def test_referenced_table_is_itself_resolved(self):
        data = {"x": "v", "a": {"k": "${x}"}, "b": "${a}"}
        self.assertEqual(resolve(data)["b"], {"k": "v"})

    def test_path_through_an_alias(self):
        data = {"real": {"k": "v"}, "alias": "${real}", "use": "${alias.k}"}
        self.assertEqual(resolve(data)["use"], "v")

    def test_path_through_an_alias_does_not_expand_twice(self):
        data = {"real": {"k": "$${literal}"}, "alias": "${real}", "use": "${alias.k}"}
        result = resolve(data)
        self.assertEqual(result["use"], "${literal}")
        self.assertEqual(result["real"]["k"], "${literal}")

    def test_escape_produces_literal(self):
        self.assertEqual(resolve({"a": "$${x}", "b": "a$$b"}), {"a": "${x}", "b": "a$b"})

    def test_embedding_scalars(self):
        data = {"t": True, "n": None, "f": 1.5, "s": "[${t}|${n}|${f}]"}
        self.assertEqual(resolve(data)["s"], "[true|null|1.5]")

    def test_embedding_table_or_list_refused(self):
        for target in ({"k": 1}, [1]):
            with self.assertRaisesRegex(InterpolationError, "cannot be embedded"):
                resolve({"a": target, "b": "x${a}"})

    def test_keys_are_not_expanded(self):
        self.assertEqual(resolve({"${a}": 1}), {"${a}": 1})

    def test_input_not_mutated(self):
        data = {"a": "${b}", "b": 1, "t": {"k": "${b}"}}
        resolve(data)
        self.assertEqual(data, {"a": "${b}", "b": 1, "t": {"k": "${b}"}})


class DefaultTests(unittest.TestCase):
    def test_default_used_when_missing(self):
        self.assertEqual(resolve({"a": "${nope:-fallback}"})["a"], "fallback")

    def test_default_ignored_when_present(self):
        self.assertEqual(resolve({"a": "${b:-fallback}", "b": 1})["a"], 1)

    def test_default_used_for_null(self):
        self.assertEqual(resolve({"a": "${b:-x}", "b": None})["a"], "x")

    def test_null_without_default_stays_null(self):
        self.assertIsNone(resolve({"a": "${b}", "b": None})["a"])

    def test_empty_string_counts_as_present(self):
        self.assertEqual(resolve({"a": "${b:-x}", "b": ""})["a"], "")

    def test_default_text_stays_a_string(self):
        self.assertEqual(resolve({"a": "${nope:-8080}"})["a"], "8080")

    def test_empty_default(self):
        self.assertEqual(resolve({"a": "${nope:-}"})["a"], "")

    def test_default_can_contain_references_and_keeps_type(self):
        self.assertEqual(resolve({"a": "${nope:-${b}}", "b": 7})["a"], 7)
        self.assertEqual(resolve({"a": "${nope:-x${b}y}", "b": 7})["a"], "x7y")

    def test_nested_defaults(self):
        self.assertEqual(resolve({"a": "${p:-${q:-${r:-deep}}}"})["a"], "deep")

    def test_default_not_followed_when_unneeded(self):
        # The default would be a dangling reference, but it is never needed.
        self.assertEqual(resolve({"a": "${b:-${nope}}", "b": 1})["a"], 1)


class ResolverTests(unittest.TestCase):
    def test_env(self):
        self.assertEqual(resolve({"a": "${env:HOST}"}, environ={"HOST": "h"})["a"], "h")

    def test_env_values_stay_strings(self):
        self.assertEqual(resolve({"a": "${env:PORT}"}, environ={"PORT": "80"})["a"], "80")

    def test_env_default(self):
        self.assertEqual(resolve({"a": "${env:NOPE:-d}"})["a"], "d")

    def test_custom_resolver(self):
        result = resolve({"a": "${upper:x}"}, resolvers={"upper": str.upper})
        self.assertEqual(result["a"], "X")

    def test_custom_resolver_keeps_type(self):
        result = resolve({"a": "${num:x}"}, resolvers={"num": lambda name: 5})
        self.assertEqual(result["a"], 5)

    def test_resolver_returning_none_means_missing(self):
        result = resolve({"a": "${none:x:-d}"}, resolvers={"none": lambda name: None})
        self.assertEqual(result["a"], "d")

    def test_unknown_resolver(self):
        with self.assertRaisesRegex(InterpolationError, "unknown resolver 'vault'.*known: env"):
            resolve({"a": "${vault:key}"})

    def test_quoted_key_with_colon_is_a_path(self):
        self.assertEqual(resolve({"a:b": 1, "c": '${"a:b"}'})["c"], 1)


class ErrorTests(unittest.TestCase):
    def test_missing_reference_names_where_and_what(self):
        with self.assertRaises(ReferenceNotFoundError) as caught:
            resolve({"t": {"a": "x${nope}"}})
        self.assertEqual(caught.exception.reference, "nope")
        self.assertEqual(caught.exception.path, "t.a")
        self.assertIn("t.a", str(caught.exception))

    def test_bad_path_inside_reference(self):
        with self.assertRaisesRegex(InterpolationError, "invalid path"):
            resolve({"a": "${b..c}"})

    def test_malformed_template_names_the_key(self):
        with self.assertRaisesRegex(InterpolationError, "^t.a: unterminated"):
            resolve({"t": {"a": "${b"}})

    def test_deep_chain_works(self):
        data = {f"k{i}": "${k%d}" % (i + 1) for i in range(100)}
        data["k100"] = "end"
        self.assertEqual(resolve(data)["k0"], "end")

    def test_absurdly_deep_chain_fails_cleanly(self):
        data = {f"k{i}": "${k%d}" % (i + 1) for i in range(100000)}
        data["k100000"] = "end"
        with self.assertRaisesRegex(InterpolationError, "too deeply"):
            resolve(data)


class CycleTests(unittest.TestCase):
    def test_self_reference(self):
        with self.assertRaises(InterpolationCycleError) as caught:
            resolve({"a": "${a}"})
        self.assertEqual(caught.exception.cycle, ["a", "a"])

    def test_cycle_is_shown(self):
        with self.assertRaises(InterpolationCycleError) as caught:
            resolve({"a": "${b}", "b": "x${c}", "c": "${a}y"})
        self.assertEqual(caught.exception.cycle, ["a", "b", "c", "a"])
        self.assertIn("a -> b -> c -> a", str(caught.exception))

    def test_cycle_excludes_the_lead_in(self):
        with self.assertRaises(InterpolationCycleError) as caught:
            resolve({"start": "${a}", "a": "${b}", "b": "${a}"})
        self.assertEqual(caught.exception.cycle, ["a", "b", "a"])

    def test_child_referring_to_parent(self):
        with self.assertRaises(InterpolationCycleError) as caught:
            resolve({"a": {"b": "${a}"}})
        self.assertEqual(caught.exception.cycle, ["a", "a.b", "a"])

    def test_cycle_through_an_alias_path(self):
        with self.assertRaises(InterpolationCycleError):
            resolve({"a": "${b.k}", "b": "${a}"})

    def test_cycle_in_a_default(self):
        with self.assertRaises(InterpolationCycleError):
            resolve({"a": "${nope:-${a}}"})

    def test_shared_dependency_is_not_a_cycle(self):
        data = {"base": "b", "x": "${base}", "y": "${base}", "z": "${x}${y}"}
        self.assertEqual(resolve(data)["z"], "bb")


class ProvenanceTests(unittest.TestCase):
    def test_records_raw_value_and_references(self):
        provenance = Provenance()
        resolve({"a": "${b}-${env:E}", "b": 1}, environ={"E": "e"}, provenance=provenance)
        event = provenance.events_at(("a",))[0]
        self.assertIs(event.action, Action.INTERPOLATE)
        self.assertEqual(event.previous, "${b}-${env:E}")
        self.assertEqual(event.value, "1-e")
        self.assertEqual([str(ref) for ref in event.references], ["b", "env:E"])

    def test_plain_strings_record_nothing(self):
        provenance = Provenance()
        resolve({"a": "plain"}, provenance=provenance)
        self.assertEqual(provenance.events_at(("a",)), [])

    def test_only_used_default_branch_is_listed(self):
        provenance = Provenance()
        resolve({"a": "${nope:-${b}}", "b": 1}, provenance=provenance)
        self.assertEqual([str(r) for r in provenance.events_at(("a",))[0].references], ["b"])

    def test_dependencies_are_recorded_before_dependents(self):
        provenance = Provenance()
        resolve({"z": "${a}", "a": "${b}", "b": 1}, provenance=provenance)
        order = [event.path[0] for event in provenance.interpolations()]
        self.assertEqual(order, ["a", "z"])


if __name__ == "__main__":
    unittest.main()
