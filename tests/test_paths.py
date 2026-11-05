import unittest

from configmerge.errors import PathSyntaxError
from configmerge.paths import (
    MISSING,
    PathPattern,
    assign,
    format_path,
    iter_leaves,
    lookup,
    parse_path,
)


class ParsePathTests(unittest.TestCase):
    def test_simple_and_nested(self):
        self.assertEqual(parse_path("a"), ("a",))
        self.assertEqual(parse_path("a.b.c"), ("a", "b", "c"))

    def test_indices(self):
        self.assertEqual(parse_path("a[0]"), ("a", 0))
        self.assertEqual(parse_path("a[1][2].b"), ("a", 1, 2, "b"))
        self.assertEqual(parse_path("[3].x"), (3, "x"))

    def test_quoted_keys(self):
        self.assertEqual(parse_path('labels."app.name".x'), ("labels", "app.name", "x"))
        self.assertEqual(parse_path(r'"with \"quote\""'), ('with "quote"',))

    def test_bare_keys_allow_dashes_and_digits(self):
        self.assertEqual(parse_path("a-b.0.c_d"), ("a-b", "0", "c_d"))

    def test_rejects_malformed(self):
        for text in ["", ".a", "a.", "a..b", "a[", "a[x]", "a[-1]", "a.[0]", "a[0]b", '"open', "a b"]:
            with self.subTest(text=text):
                with self.assertRaises(PathSyntaxError):
                    parse_path(text)

    def test_error_reports_column(self):
        with self.assertRaises(PathSyntaxError) as caught:
            parse_path("abc..d")
        self.assertEqual(caught.exception.column, 4)
        self.assertIn("column 5", str(caught.exception))

    def test_wildcard_index_only_in_pattern_mode(self):
        self.assertEqual(parse_path("a[*]", wildcards=True), ("a", "*"))
        with self.assertRaises(PathSyntaxError):
            parse_path("a[*]")


class FormatPathTests(unittest.TestCase):
    def test_round_trip(self):
        for path in [("a",), ("a", 0, "b"), ("a.b", "c d"), ('q"uote',), (0, 1), ("a-b_c",)]:
            with self.subTest(path=path):
                self.assertEqual(parse_path(format_path(path)), path)

    def test_root(self):
        self.assertEqual(format_path(()), "<root>")

    def test_list_index_form(self):
        self.assertEqual(format_path(("server", "ports", 1)), "server.ports[1]")


class LookupTests(unittest.TestCase):
    DATA = {"a": {"b": [10, {"c": None}]}, "s": "text"}

    def test_found(self):
        self.assertEqual(lookup(self.DATA, ("a", "b", 0)), 10)
        self.assertIsNone(lookup(self.DATA, ("a", "b", 1, "c")))

    def test_missing_is_distinct_from_none(self):
        self.assertIs(lookup(self.DATA, ("a", "x")), MISSING)
        self.assertIs(lookup(self.DATA, ("a", "b", 2)), MISSING)

    def test_wrong_shape_is_missing_not_an_error(self):
        self.assertIs(lookup(self.DATA, ("s", "x")), MISSING)
        self.assertIs(lookup(self.DATA, ("a", 0)), MISSING)
        self.assertIs(lookup(self.DATA, ("a", "b", "c")), MISSING)


class AssignTests(unittest.TestCase):
    def test_creates_tables(self):
        data = {}
        assign(data, ("a", "b", "c"), 1)
        self.assertEqual(data, {"a": {"b": {"c": 1}}})

    def test_replaces_scalar_on_the_way(self):
        data = {"a": 1}
        assign(data, ("a", "b"), 2)
        self.assertEqual(data, {"a": {"b": 2}})


class IterLeavesTests(unittest.TestCase):
    def test_into_lists(self):
        leaves = dict(iter_leaves({"a": {"b": [1, {"c": 2}]}, "d": 3}))
        self.assertEqual(leaves, {("a", "b", 0): 1, ("a", "b", 1, "c"): 2, ("d",): 3})

    def test_lists_atomic(self):
        leaves = dict(iter_leaves({"a": [1, 2]}, into_lists=False))
        self.assertEqual(leaves, {("a",): [1, 2]})

    def test_empty_containers_are_leaves(self):
        leaves = dict(iter_leaves({"a": {}, "b": []}))
        self.assertEqual(leaves, {("a",): {}, ("b",): []})


class PathPatternTests(unittest.TestCase):
    def test_literal(self):
        self.assertTrue(PathPattern("a.b").matches(("a", "b")))
        self.assertFalse(PathPattern("a.b").matches(("a", "b", "c")))
        self.assertFalse(PathPattern("a.b").matches(("a",)))

    def test_single_star(self):
        pattern = PathPattern("services.*.hosts")
        self.assertTrue(pattern.matches(("services", "web", "hosts")))
        self.assertFalse(pattern.matches(("services", "hosts")))
        self.assertFalse(pattern.matches(("services", "a", "b", "hosts")))

    def test_star_matches_list_index(self):
        self.assertTrue(PathPattern("a[*].b").matches(("a", 3, "b")))
        self.assertTrue(PathPattern("a.*.b").matches(("a", 3, "b")))

    def test_double_star(self):
        pattern = PathPattern("**.password")
        self.assertTrue(pattern.matches(("password",)))
        self.assertTrue(pattern.matches(("a", "b", "password")))
        self.assertFalse(pattern.matches(("password", "x")))
        self.assertTrue(PathPattern("a.**").matches(("a",)))
        self.assertTrue(PathPattern("a.**").matches(("a", "b", "c")))

    def test_matches_within_covers_descendants(self):
        pattern = PathPattern("db.credentials")
        self.assertTrue(pattern.matches_within(("db", "credentials", "user")))
        self.assertFalse(pattern.matches_within(("db",)))

    def test_specificity_prefers_literals(self):
        literal = PathPattern("a.b.c").specificity
        star = PathPattern("a.*.c").specificity
        glob = PathPattern("a.**.c").specificity
        self.assertGreater(literal, star)
        self.assertGreater(star, glob)

    def test_from_segments(self):
        pattern = PathPattern.from_segments(("a", "*", "b"))
        self.assertTrue(pattern.matches(("a", 0, "b")))
        self.assertEqual(pattern.text, "a.*.b")


if __name__ == "__main__":
    unittest.main()
