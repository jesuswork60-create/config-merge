import unittest

from configmerge.merge import Merger, MergePolicy
from configmerge.paths import parse_path
from configmerge.provenance import Action, Provenance
from configmerge.sources import Layer


def run(*layers, rules=None):
    provenance = Provenance()
    merger = Merger(MergePolicy(rules), provenance)
    data = merger.merge_all(layers)
    return data, provenance


class HistoryTests(unittest.TestCase):
    def test_winner_and_overridden(self):
        data, prov = run(Layer("base", {"a": 1}), Layer("env", {"a": 2}, notes={("a",): "APP_A"}))
        explanation = prov.explain(("a",), data)
        self.assertEqual(explanation.value, 2)
        self.assertEqual(explanation.source, "env")
        self.assertEqual([e.layer for e in explanation.overridden], ["base"])
        last = explanation.events[-1]
        self.assertIs(last.action, Action.OVERRIDE)
        self.assertEqual(last.previous, 1)
        self.assertEqual(last.previous_layer, "base")
        self.assertEqual(last.note, "APP_A")

    def test_key_untouched_by_later_layer(self):
        data, prov = run(Layer("base", {"t": {"a": 1, "b": 2}}), Layer("over", {"t": {"b": 3}}))
        self.assertEqual(prov.explain(("t", "a"), data).source, "base")
        self.assertEqual(prov.explain(("t", "b"), data).source, "over")

    def test_unknown_key(self):
        data, prov = run(Layer("base", {"a": 1}))
        explanation = prov.explain(("zzz",), data)
        self.assertFalse(explanation.exists)
        self.assertIsNone(explanation.source)
        self.assertIn("not set by any layer", explanation.render())

    def test_layers_listed_in_order(self):
        _, prov = run(Layer("one", {}), Layer("two", {}))
        self.assertEqual(prov.layers, ["one", "two"])

    def test_table_explanation_lists_children(self):
        data, prov = run(Layer("base", {"t": {"a": 1}}), Layer("over", {"t": {"b": 2}}))
        explanation = prov.explain(("t",), data)
        self.assertEqual(dict(explanation.children), {("t", "a"): "base", ("t", "b"): "over"})
        rendered = explanation.render()
        self.assertIn("t.a  <- base", rendered)
        self.assertIn("t.b  <- over", rendered)


class ShapeChangeTests(unittest.TestCase):
    def test_scalar_over_table_removes_children(self):
        data, prov = run(Layer("base", {"a": {"b": 1}}), Layer("over", {"a": 5}))
        child = prov.explain(("a", "b"), data)
        self.assertFalse(child.exists)
        self.assertIs(child.events[-1].action, Action.REMOVE)
        self.assertEqual(child.events[-1].layer, "over")
        self.assertEqual(child.events[-1].previous, 1)
        self.assertIn("removed by over", child.render())
        self.assertEqual(prov.explain(("a",), data).source, "over")

    def test_table_over_scalar(self):
        data, prov = run(Layer("base", {"a": 5}), Layer("over", {"a": {"b": 1}}))
        self.assertEqual(prov.explain(("a", "b"), data).source, "over")
        self.assertIs(prov.events_at(("a",))[-1].action, Action.REMOVE)

    def test_replace_strategy_records_dropped_and_kept_keys(self):
        data, prov = run(
            Layer("base", {"t": {"x": 1, "y": 2}}),
            Layer("over", {"t": {"y": 3}}),
            rules={"t": "replace"},
        )
        self.assertIs(prov.events_at(("t", "x"))[-1].action, Action.REMOVE)
        self.assertIs(prov.events_at(("t", "y"))[-1].action, Action.OVERRIDE)
        self.assertEqual(prov.owner(("t", "x")), None)
        self.assertEqual(prov.owner(("t", "y")), "over")
        self.assertEqual(data, {"t": {"y": 3}})

    def test_key_can_return_after_removal(self):
        data, prov = run(Layer("a", {"t": {"x": 1}}), Layer("b", {"t": 0}), Layer("c", {"t": {"x": 2}}))
        explanation = prov.explain(("t", "x"), data)
        self.assertEqual(explanation.source, "c")
        self.assertEqual([e.action for e in explanation.events], [Action.SET, Action.REMOVE, Action.SET])


class ListElementTests(unittest.TestCase):
    def layers(self):
        return Layer("a", {"l": [1, 2]}), Layer("b", {"l": [2, 3]})

    def test_append_tracks_each_element(self):
        data, prov = run(*self.layers(), rules={"l": "append"})
        self.assertEqual(prov.elements(("l",)), ["a", "a", "b", "b"])
        self.assertEqual(prov.explain(parse_path("l[0]"), data).source, "a")
        self.assertEqual(prov.explain(parse_path("l[3]"), data).source, "b")

    def test_unique_credits_the_first_contributor(self):
        data, prov = run(*self.layers(), rules={"l": "unique"})
        self.assertEqual(data["l"], [1, 2, 3])
        self.assertEqual(prov.elements(("l",)), ["a", "a", "b"])

    def test_replace_resets_elements(self):
        _, prov = run(*self.layers())
        self.assertEqual(prov.elements(("l",)), ["b", "b"])

    def test_render_shows_mixed_elements(self):
        data, prov = run(*self.layers(), rules={"l": "append"})
        rendered = prov.explain(("l",), data).render()
        self.assertIn("[0] a", rendered)
        self.assertIn("[3] b", rendered)
        self.assertIn("append", rendered)

    def test_element_inside_list_of_tables(self):
        data, prov = run(Layer("a", {"l": [{"x": 1}]}), Layer("b", {"l": [{"x": 2}]}), rules={"l": "append"})
        self.assertEqual(prov.owner(parse_path("l[1].x")), "b")
        self.assertEqual(prov.owner(parse_path("l[0].x")), "a")
        self.assertEqual(data["l"][1]["x"], 2)

    def test_deep_merge_by_position_tracks_changes(self):
        data, prov = run(
            Layer("a", {"l": [{"x": 1, "y": 1}]}),
            Layer("b", {"l": [{"y": 2}]}),
            rules={"l": "deep"},
        )
        self.assertEqual(data["l"], [{"x": 1, "y": 2}])
        event = prov.events_at(parse_path("l[0].y"))[-1]
        self.assertEqual(event.layer, "b")
        self.assertEqual(event.previous, 1)
        self.assertEqual(event.previous_layer, "a")

    def test_out_of_range_index_is_not_set(self):
        data, prov = run(Layer("a", {"l": [1]}))
        self.assertFalse(prov.explain(parse_path("l[5]"), data).exists)


class OutputTests(unittest.TestCase):
    def test_mask_applies_to_value_and_history(self):
        data, prov = run(Layer("a", {"secret": "one"}), Layer("b", {"secret": "two"}))
        rendered = prov.explain(("secret",), data).render(lambda path, value: "***")
        self.assertNotIn("one", rendered)
        self.assertNotIn("two", rendered)
        self.assertIn("***", rendered)

    def test_to_dict_shape(self):
        data, prov = run(Layer("a", {"x": 1}), Layer("b", {"x": 2}))
        document = prov.explain(("x",), data).to_dict()
        self.assertEqual(document["path"], "x")
        self.assertEqual(document["value"], 2)
        self.assertEqual(document["source"], "b")
        self.assertEqual([row["action"] for row in document["history"]], ["set", "override"])
        self.assertIsNone(document["history"][0]["previous"])

    def test_to_dict_masks(self):
        data, prov = run(Layer("a", {"x": 1}), Layer("b", {"x": 2}))
        document = prov.explain(("x",), data).to_dict(lambda path, value: "***")
        self.assertEqual(document["value"], "***")
        self.assertEqual(document["history"][1]["previous"], "***")


if __name__ == "__main__":
    unittest.main()
