import datetime as dt
import json
import math
import tempfile
import tomllib
import unittest
from pathlib import Path

from configmerge.errors import WriterError
from configmerge.sources import EnvFile
from configmerge.writers import to_env, to_json, to_toml


class JsonWriterTests(unittest.TestCase):
    def test_round_trip(self):
        data = {"a": {"b": [1, 2.5, None, True, "é"]}}
        self.assertEqual(json.loads(to_json(data)), data)

    def test_non_ascii_is_kept_readable(self):
        self.assertIn("é", to_json({"a": "é"}))

    def test_dates_become_iso(self):
        data = {"d": dt.date(2026, 1, 2), "t": dt.time(3, 4), "dt": dt.datetime(2026, 1, 2, 3, 4)}
        self.assertEqual(
            json.loads(to_json(data)),
            {"d": "2026-01-02", "t": "03:04:00", "dt": "2026-01-02T03:04:00"},
        )

    def test_non_finite_refused_with_path(self):
        with self.assertRaisesRegex(WriterError, r"a\[0\]"):
            to_json({"a": [math.inf]})

    def test_unknown_type_refused(self):
        with self.assertRaisesRegex(WriterError, "a: cannot write"):
            to_json({"a": object()})

    def test_compact_and_sorted(self):
        self.assertEqual(to_json({"b": 1, "a": 2}, indent=None, sort_keys=True), '{"a": 2, "b": 1}')


class TomlWriterTests(unittest.TestCase):
    def assertRoundTrips(self, data):
        text = to_toml(data)
        self.assertEqual(tomllib.loads(text), data, text)

    def test_scalars(self):
        self.assertRoundTrips({"s": "x", "i": -1, "f": 1.5, "b": True, "e": 1e20})

    def test_float_stays_float(self):
        parsed = tomllib.loads(to_toml({"f": 2.0}))
        self.assertIsInstance(parsed["f"], float)

    def test_nonfinite_floats(self):
        parsed = tomllib.loads(to_toml({"a": math.inf, "b": -math.inf, "c": math.nan}))
        self.assertEqual((parsed["a"], parsed["b"]), (math.inf, -math.inf))
        self.assertTrue(math.isnan(parsed["c"]))

    def test_string_escapes(self):
        self.assertRoundTrips({"s": 'q"uo\\te\n\t\r\b\f\x01\x7f é'})

    def test_dates(self):
        self.assertRoundTrips(
            {
                "d": dt.date(2026, 1, 2),
                "t": dt.time(3, 4, 5),
                "n": dt.datetime(2026, 1, 2, 3, 4, 5),
                "z": dt.datetime(2026, 1, 2, 3, 4, tzinfo=dt.timezone.utc),
            }
        )

    def test_nested_tables_come_after_values(self):
        self.assertRoundTrips({"t": {"sub": {"x": 1}, "v": 2}, "top": 3})

    def test_table_with_only_subtables_has_no_empty_header(self):
        text = to_toml({"a": {"b": {"c": 1}}})
        self.assertNotIn("[a]\n", text)
        self.assertIn("[a.b]", text)

    def test_empty_table_and_list(self):
        self.assertRoundTrips({"t": {}, "l": [], "n": {"inner": {}}})

    def test_quoted_keys(self):
        self.assertRoundTrips({"a.b": 1, "with space": {"x y": 2}, "": 3, 'q"': {"é": [1]}})

    def test_array_of_tables(self):
        self.assertRoundTrips({"srv": [{"h": "a", "sub": {"x": 1}}, {"h": "b", "more": [{"y": 2}]}]})
        self.assertIn("[[srv]]", to_toml({"srv": [{"h": "a"}]}))

    def test_mixed_lists_use_inline_forms(self):
        self.assertRoundTrips({"m": [1, "x", {"k": [1, {"z": 2}]}, [2, [3]]], "n": [{}, 1]})

    def test_array_of_tables_inside_table(self):
        self.assertRoundTrips({"t": {"v": 1, "l": [{"a": 1}, {"a": 2}], "after": {"b": 1}}})

    def test_null_dropped_from_tables(self):
        self.assertEqual(tomllib.loads(to_toml({"a": None, "b": {"c": None, "d": 1}})), {"b": {"d": 1}})

    def test_null_in_inline_table_dropped(self):
        self.assertEqual(tomllib.loads(to_toml({"l": [1, {"a": None, "b": 2}]})), {"l": [1, {"b": 2}]})

    def test_null_refused_when_asked(self):
        with self.assertRaisesRegex(WriterError, "t.a: TOML cannot represent null"):
            to_toml({"t": {"a": None}}, drop_null=False)
        with self.assertRaises(WriterError):
            to_toml({"l": [1, {"a": None}]}, drop_null=False)

    def test_null_in_list_always_refused(self):
        with self.assertRaisesRegex(WriterError, r"l\[1\]"):
            to_toml({"l": [1, None]})

    def test_unknown_type_refused(self):
        with self.assertRaisesRegex(WriterError, "cannot write"):
            to_toml({"a": object()})

    def test_empty_document(self):
        self.assertEqual(to_toml({}), "")


class EnvWriterTests(unittest.TestCase):
    def test_nesting_and_prefix(self):
        text = to_env({"server": {"port": 80}, "name": "shop"}, prefix="APP")
        self.assertEqual(text, "APP_SERVER__PORT=80\nAPP_NAME=shop\n")

    def test_null_skipped(self):
        self.assertEqual(to_env({"a": None, "b": 1}), "B=1\n")

    def test_invalid_key_refused(self):
        for key in ["a-b", "a.b", "1a"]:
            with self.assertRaisesRegex(WriterError, "cannot be part of a variable name"):
                to_env({key: 1})

    def test_nonfinite_refused(self):
        with self.assertRaises(WriterError):
            to_env({"a": math.nan})

    def test_empty(self):
        self.assertEqual(to_env({}), "")

    def test_round_trip_through_env_file(self):
        data = {
            "plain": "word",
            "spaced": "two words # not a comment",
            "quote": "it's \"quoted\"",
            "newline": "line1\nline2",
            "dollar": "$HOME and `x`",
            "numeric_string": "8080",
            "bool_string": "true",
            "float_string": "1.5",
            "json_string": "[1, 2]",
            "quoted_string": '"already"',
            "empty": "",
            "unicode": "é",
            "number": 80,
            "negative": -5,
            "real": 1.5,
            "big": 1e16,
            "small": 1e-07,
            "yes": True,
            "no": False,
            "items": [1, "a b", {"k": None}],
            "none": [],
            "nothing": {},
            "nested": {"deep": {"er": "value"}},
        }
        text = to_env(data, prefix="APP")
        with tempfile.TemporaryDirectory() as holder:
            path = Path(holder) / "out.env"
            path.write_text(text, encoding="utf-8")
            loaded = EnvFile(path, prefix="APP").load()[0].data
        self.assertEqual(loaded, data, text)
        for key in ("numeric_string", "bool_string", "float_string"):
            self.assertIsInstance(loaded[key], str)


if __name__ == "__main__":
    unittest.main()
