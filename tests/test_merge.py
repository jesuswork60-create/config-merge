import copy
import unittest

from configmerge.errors import MergeConflictError, MergeRuleError
from configmerge.merge import Merger, MergePolicy, Strategy
from configmerge.sources import Layer


def merge(*tables, rules=None, strict_types=False):
    merger = Merger(MergePolicy(rules, strict_types=strict_types))
    layers = [Layer(f"layer{i}", table) for i, table in enumerate(tables)]
    return merger.merge_all(layers)


class DefaultBehaviourTests(unittest.TestCase):
    def test_tables_merge_deeply(self):
        result = merge({"a": {"x": 1, "y": 2}}, {"a": {"y": 3, "z": 4}})
        self.assertEqual(result, {"a": {"x": 1, "y": 3, "z": 4}})

    def test_scalars_and_lists_are_replaced(self):
        result = merge({"a": 1, "b": [1, 2]}, {"a": 2, "b": [3]})
        self.assertEqual(result, {"a": 2, "b": [3]})

    def test_type_change_replaces(self):
        self.assertEqual(merge({"a": {"x": 1}}, {"a": 5}), {"a": 5})
        self.assertEqual(merge({"a": 5}, {"a": {"x": 1}}), {"a": {"x": 1}})

    def test_empty_table_keeps_existing(self):
        self.assertEqual(merge({"a": {"x": 1}}, {"a": {}}), {"a": {"x": 1}})

    def test_empty_table_is_created_when_absent(self):
        self.assertEqual(merge({}, {"a": {}}), {"a": {}})

    def test_none_is_an_ordinary_value(self):
        self.assertEqual(merge({"a": 1}, {"a": None}), {"a": None})

    def test_inputs_are_not_mutated(self):
        first = {"a": {"x": [1]}, "keep": {"k": 1}}
        second = {"a": {"x": [2], "y": {"z": 1}}}
        first_copy, second_copy = copy.deepcopy(first), copy.deepcopy(second)
        result = merge(first, second)
        result["a"]["y"]["z"] = 99
        result["a"]["x"].append(3)
        self.assertEqual(first, first_copy)
        self.assertEqual(second, second_copy)

    def test_no_layers(self):
        self.assertEqual(merge(), {})


class StrategyTests(unittest.TestCase):
    def test_append(self):
        result = merge({"a": [1, 2]}, {"a": [2, 3]}, rules={"a": "append"})
        self.assertEqual(result, {"a": [1, 2, 2, 3]})

    def test_append_scalar_counts_as_element(self):
        self.assertEqual(merge({"a": [1]}, {"a": 2}, rules={"a": "append"}), {"a": [1, 2]})

    def test_append_first_layer_makes_a_list(self):
        self.assertEqual(merge({"a": "x"}, rules={"a": "append"}), {"a": ["x"]})

    def test_append_table_as_element(self):
        result = merge({"a": [{"x": 1}]}, {"a": {"y": 2}}, rules={"a": "append"})
        self.assertEqual(result, {"a": [{"x": 1}, {"y": 2}]})

    def test_append_keeps_an_earlier_scalar_as_first_element(self):
        self.assertEqual(merge({"a": 1}, {"a": [2]}, rules={"a": "append"}), {"a": [1, 2]})

    def test_unique_keeps_first_position(self):
        result = merge({"a": [3, 1]}, {"a": [2, 3, 4, 2]}, rules={"a": "unique"})
        self.assertEqual(result, {"a": [3, 1, 2, 4]})

    def test_unique_dedupes_within_first_layer(self):
        self.assertEqual(merge({"a": [1, 1, 2]}, rules={"a": "unique"}), {"a": [1, 2]})

    def test_unique_handles_unhashable_elements(self):
        result = merge(
            {"a": [{"x": [1, 2]}, [1]]},
            {"a": [{"x": [1, 2]}, [1], {"x": [2, 1]}]},
            rules={"a": "unique"},
        )
        self.assertEqual(result, {"a": [{"x": [1, 2]}, [1], {"x": [2, 1]}]})

    def test_unique_keeps_true_and_one_apart(self):
        self.assertEqual(merge({"a": [1, True]}, {"a": [True, 1]}, rules={"a": "unique"}), {"a": [1, True]})

    def test_replace_on_a_table_drops_old_keys(self):
        result = merge({"a": {"x": 1}}, {"a": {"y": 2}}, rules={"a": "replace"})
        self.assertEqual(result, {"a": {"y": 2}})

    def test_replace_with_empty_table_empties_it(self):
        self.assertEqual(merge({"a": {"x": 1}}, {"a": {}}, rules={"a": "replace"}), {"a": {}})

    def test_deep_merges_lists_by_position(self):
        result = merge(
            {"a": [{"x": 1, "y": 1}, {"x": 2}]},
            {"a": [{"y": 9}]},
            rules={"a": "deep"},
        )
        self.assertEqual(result, {"a": [{"x": 1, "y": 9}, {"x": 2}]})

    def test_deep_list_extends_past_the_end(self):
        self.assertEqual(merge({"a": [1]}, {"a": [2, 3]}, rules={"a": "deep"}), {"a": [2, 3]})

    def test_nested_rule_applies_during_first_introduction(self):
        result = merge({"s": {"web": {"hosts": [1, 1]}}}, rules={"s.*.hosts": "unique"})
        self.assertEqual(result, {"s": {"web": {"hosts": [1]}}})

    def test_wildcard_rule_and_specific_rule(self):
        rules = {"s.*.hosts": "append", "s.db.hosts": "replace"}
        result = merge(
            {"s": {"web": {"hosts": [1]}, "db": {"hosts": [1]}}},
            {"s": {"web": {"hosts": [2]}, "db": {"hosts": [2]}}},
            rules=rules,
        )
        self.assertEqual(result["s"]["web"]["hosts"], [1, 2])
        self.assertEqual(result["s"]["db"]["hosts"], [2])

    def test_rule_order_breaks_ties(self):
        policy = MergePolicy({"a.*": "append", "*.b": "unique"})
        self.assertIs(policy.rule_for(("a", "b")), Strategy.UNIQUE)

    def test_strategy_accepts_enum(self):
        self.assertEqual(merge({"a": [1]}, {"a": [2]}, rules={"a": Strategy.APPEND}), {"a": [1, 2]})

    def test_unknown_strategy(self):
        with self.assertRaisesRegex(MergeRuleError, "unknown merge strategy 'bogus'"):
            MergePolicy({"a": "bogus"})


class StrictTypesTests(unittest.TestCase):
    def test_shape_change_refused_and_names_both_layers(self):
        with self.assertRaises(MergeConflictError) as caught:
            merge({"a": {"b": 1}}, {"a": {"b": "x"}}, strict_types=True)
        message = str(caught.exception)
        self.assertIn("a.b", message)
        self.assertIn("layer1", message)
        self.assertIn("layer0", message)

    def test_table_over_scalar_refused(self):
        with self.assertRaises(MergeConflictError):
            merge({"a": 1}, {"a": {"b": 1}}, strict_types=True)

    def test_int_and_float_are_compatible(self):
        self.assertEqual(merge({"a": 1}, {"a": 1.5}, strict_types=True), {"a": 1.5})

    def test_bool_and_int_are_not(self):
        with self.assertRaises(MergeConflictError):
            merge({"a": 1}, {"a": True}, strict_types=True)

    def test_none_is_allowed_either_way(self):
        self.assertEqual(merge({"a": None}, {"a": 1}, {"a": None}, strict_types=True), {"a": None})

    def test_append_of_scalar_is_not_a_conflict(self):
        self.assertEqual(merge({"a": [1]}, {"a": 2}, rules={"a": "append"}, strict_types=True), {"a": [1, 2]})


if __name__ == "__main__":
    unittest.main()
