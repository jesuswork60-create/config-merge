import math
import unittest

from configmerge.diff import ChangeKind, changes_to_dicts, diff_configs, render_diff, same_value
from configmerge.paths import MISSING
from configmerge.redact import Redactor


def summary(changes):
    return {(change.path, change.kind.value) for change in changes}


class DiffTests(unittest.TestCase):
    def test_identical(self):
        self.assertEqual(diff_configs({"a": {"b": [1]}}, {"a": {"b": [1]}}), [])

    def test_added_removed_changed(self):
        changes = diff_configs({"a": 1, "b": 2}, {"b": 3, "c": 4})
        self.assertEqual(
            summary(changes),
            {(("a",), "removed"), (("b",), "changed"), (("c",), "added")},
        )

    def test_values_recorded(self):
        added, changed, removed = diff_configs({"b": 1, "c": 2}, {"a": 9, "b": 5})
        self.assertIs(added.kind, ChangeKind.ADDED)
        self.assertIs(added.old, MISSING)
        self.assertEqual(added.new, 9)
        self.assertEqual((changed.old, changed.new), (1, 5))
        self.assertEqual(removed.old, 2)
        self.assertIs(removed.new, MISSING)

    def test_nested_paths_and_one_sided_tables(self):
        changes = diff_configs({"t": {"x": 1}}, {"t": {"x": 1, "new": {"p": 1, "q": 2}}})
        self.assertEqual(
            summary(changes),
            {(("t", "new", "p"), "added"), (("t", "new", "q"), "added")},
        )

    def test_empty_table_on_one_side_is_reported(self):
        self.assertEqual(summary(diff_configs({}, {"t": {}})), {(("t",), "added")})

    def test_lists_compare_whole_by_default(self):
        changes = diff_configs({"l": [1, 2]}, {"l": [1, 3]})
        self.assertEqual(summary(changes), {(("l",), "changed")})

    def test_lists_by_index(self):
        changes = diff_configs({"l": [1, {"x": 1}, 3]}, {"l": [1, {"x": 2}]}, by_index=True)
        self.assertEqual(
            summary(changes),
            {(("l", 1, "x"), "changed"), (("l", 2), "removed")},
        )
        changes = diff_configs({"l": []}, {"l": [1]}, by_index=True)
        self.assertEqual(summary(changes), {(("l", 0), "added")})

    def test_shape_change_is_a_change(self):
        changes = diff_configs({"a": {"x": 1}}, {"a": 5})
        self.assertEqual(summary(changes), {(("a",), "changed")})
        self.assertTrue(changes[0].type_changed)

    def test_type_sensitive(self):
        for left, right in [(1, 1.0), (1, True), ("1", 1)]:
            with self.subTest(left=left, right=right):
                changes = diff_configs({"a": left}, {"a": right})
                self.assertEqual(len(changes), 1)
                self.assertTrue(changes[0].type_changed)

    def test_type_sensitive_inside_lists(self):
        self.assertEqual(len(diff_configs({"a": [1]}, {"a": [True]})), 1)

    def test_nan_equals_itself(self):
        self.assertTrue(same_value(math.nan, math.nan))
        self.assertEqual(diff_configs({"a": math.nan}, {"a": math.nan}), [])

    def test_ignore_patterns(self):
        changes = diff_configs(
            {"build": {"stamp": 1, "id": 1}, "x": 1, "keep": 1},
            {"build": {"stamp": 2, "id": 2}, "x": 2, "keep": 2},
            ignore=["build", "x"],
        )
        self.assertEqual(summary(changes), {(("keep",), "changed")})

    def test_sorted_by_path(self):
        changes = diff_configs({}, {"z": 1, "a": 1, "m": {"b": 1}})
        self.assertEqual([c.path for c in changes], [("a",), ("m", "b"), ("z",)])


class RenderTests(unittest.TestCase):
    def test_text(self):
        changes = diff_configs({"b": 1, "c": "x"}, {"a": [1], "b": 2})
        self.assertEqual(
            render_diff(changes).splitlines(),
            ["+ a = [1]", "~ b: 1 -> 2", '- c = "x"'],
        )

    def test_dates_render(self):
        import datetime as dt

        text = render_diff(diff_configs({}, {"d": dt.date(2026, 1, 2)}))
        self.assertIn("2026-01-02", text)

    def test_masks_hide_secrets_but_show_the_change(self):
        left = {"password": "old-secret", "dsn": "pg://old-secret@h"}
        right = {"password": "new-secret", "dsn": "pg://new-secret@h"}
        text = render_diff(
            diff_configs(left, right),
            left_mask=Redactor().bind(left),
            right_mask=Redactor().bind(right),
        )
        self.assertNotIn("old-secret", text)
        self.assertNotIn("new-secret", text)
        self.assertIn('~ password: "***" -> "***"', text)
        self.assertIn('~ dsn: "pg://***@h" -> "pg://***@h"', text)

    def test_dicts(self):
        changes = diff_configs({"b": 1, "c": 1}, {"a": 1, "b": 2})
        rows = changes_to_dicts(changes)
        self.assertEqual(
            rows,
            [
                {"path": "a", "kind": "added", "new": 1},
                {"path": "b", "kind": "changed", "old": 1, "new": 2},
                {"path": "c", "kind": "removed", "old": 1},
            ],
        )

    def test_dicts_masked(self):
        rows = changes_to_dicts(
            diff_configs({"token": "a"}, {"token": "b"}),
            left_mask=lambda p, v: "L",
            right_mask=lambda p, v: "R",
        )
        self.assertEqual(rows[0]["old"], "L")
        self.assertEqual(rows[0]["new"], "R")


if __name__ == "__main__":
    unittest.main()
