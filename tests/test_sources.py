import os
import tempfile
import unittest
from pathlib import Path

from configmerge.errors import ConfigParseError, SourceError
from configmerge.sources import (
    CliOverrides,
    DictSource,
    Directory,
    EnvFile,
    EnvVars,
    JsonFile,
    TomlFile,
    env_items_to_tree,
    file_source,
)


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.dir = Path(holder.name)

    def write(self, name, text, *, binary=False):
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if binary:
            path.write_bytes(text)
        else:
            path.write_text(text, encoding="utf-8")
        return path


class TomlFileTests(TempDirTestCase):
    def test_reads_values_and_names_layer_after_file(self):
        path = self.write("a.toml", 'x = 1\n[t]\ny = "z"\n')
        layer = TomlFile(path).load()[0]
        self.assertEqual(layer.data, {"x": 1, "t": {"y": "z"}})
        self.assertEqual(layer.name, str(path))
        self.assertEqual(layer.kind, "toml")

    def test_custom_name(self):
        path = self.write("a.toml", "x = 1\n")
        self.assertEqual(TomlFile(path, name="base").load()[0].name, "base")

    def test_missing_required_file(self):
        with self.assertRaisesRegex(SourceError, "file not found"):
            TomlFile(self.dir / "nope.toml").load()

    def test_missing_optional_file_yields_no_layers(self):
        self.assertEqual(TomlFile(self.dir / "nope.toml", optional=True).load(), [])

    def test_parse_error_points_at_line_and_column(self):
        path = self.write("bad.toml", 'ok = 1\nbroken = \nmore = 2\n')
        with self.assertRaises(ConfigParseError) as caught:
            TomlFile(path).load()
        error = caught.exception
        self.assertEqual(error.file, str(path))
        self.assertEqual(error.line, 2)
        self.assertGreater(error.column, 1)
        self.assertEqual(error.snippet, "broken = ")
        self.assertIn(f"{path}:2:", str(error))
        self.assertIn("broken = ", str(error))
        self.assertIn("^", str(error))

    def test_error_at_end_of_document(self):
        path = self.write("bad.toml", "a = 1\nb = [1,")
        with self.assertRaises(ConfigParseError) as caught:
            TomlFile(path).load()
        self.assertEqual(caught.exception.line, 2)

    def test_invalid_utf8_reports_position(self):
        path = self.write("bad.toml", b"a = 1\nb = \"\xff\"\n", binary=True)
        with self.assertRaises(ConfigParseError) as caught:
            TomlFile(path).load()
        self.assertEqual(caught.exception.line, 2)
        self.assertEqual(caught.exception.column, 6)
        self.assertIn("UTF-8", str(caught.exception))

    def test_byte_order_mark_is_tolerated(self):
        path = self.write("bom.toml", b"\xef\xbb\xbfa = 1\n", binary=True)
        self.assertEqual(TomlFile(path).load()[0].data, {"a": 1})

    def test_directory_given_as_file(self):
        with self.assertRaisesRegex(SourceError, "cannot read"):
            TomlFile(self.dir).load()


class JsonFileTests(TempDirTestCase):
    def test_reads_object(self):
        path = self.write("a.json", '{"a": {"b": [1, null]}}')
        self.assertEqual(JsonFile(path).load()[0].data, {"a": {"b": [1, None]}})

    def test_parse_error_position(self):
        path = self.write("bad.json", '{\n  "a": 1,\n  "b": oops\n}\n')
        with self.assertRaises(ConfigParseError) as caught:
            JsonFile(path).load()
        self.assertEqual(caught.exception.line, 3)
        self.assertEqual(caught.exception.column, 8)
        self.assertIn('"b": oops', str(caught.exception))

    def test_non_object_root_rejected(self):
        path = self.write("list.json", "[1, 2]")
        with self.assertRaisesRegex(ConfigParseError, "must be an object"):
            JsonFile(path).load()


class EnvMappingTests(unittest.TestCase):
    def tree(self, env, **options):
        return env_items_to_tree(env.items(), **options)[0]

    def test_prefix_and_nesting(self):
        env = {"APP_SERVER__PORT": "80", "APP_NAME": "shop", "OTHER_X": "1", "APPLE": "2"}
        self.assertEqual(self.tree(env, prefix="APP"), {"server": {"port": 80}, "name": "shop"})

    def test_single_underscore_stays_in_key(self):
        self.assertEqual(self.tree({"APP_DB_POOL_SIZE": "5"}, prefix="APP"), {"db_pool_size": 5})

    def test_coercion_can_be_turned_off(self):
        self.assertEqual(self.tree({"APP_X": "5"}, prefix="APP", coerce=None), {"x": "5"})

    def test_case_can_be_preserved(self):
        self.assertEqual(self.tree({"APP_Mixed": "5"}, prefix="APP", lowercase=False), {"Mixed": 5})

    def test_custom_separator(self):
        self.assertEqual(self.tree({"APP_A_B": "1"}, prefix="APP", separator="_"), {"a": {"b": 1}})

    def test_notes_record_variable(self):
        _, notes = env_items_to_tree([("APP_A__B", "1")], prefix="APP")
        self.assertEqual(notes, {("a", "b"): "APP_A__B"})

    def test_value_and_parent_conflict(self):
        with self.assertRaisesRegex(SourceError, "APP_A and APP_A__B conflict"):
            self.tree({"APP_A": "1", "APP_A__B": "2"}, prefix="APP")

    def test_case_folding_conflict(self):
        with self.assertRaisesRegex(SourceError, "conflict"):
            self.tree({"APP_A": "1", "APP_a": "2"}, prefix="APP")

    def test_branch_then_value_conflict_after_case_folding(self):
        with self.assertRaisesRegex(SourceError, "conflict"):
            self.tree({"APP_A__B": "1", "APP_a": "2"}, prefix="APP")

    def test_empty_segment_rejected(self):
        with self.assertRaisesRegex(SourceError, "empty key segment"):
            self.tree({"APP_A____B": "1"}, prefix="APP")

    def test_bare_prefix_is_skipped(self):
        self.assertEqual(self.tree({"APP_": "1"}, prefix="APP"), {})


class EnvVarsTests(unittest.TestCase):
    def test_uses_injected_environ(self):
        layer = EnvVars("APP", environ={"APP_A": "1"}).load()[0]
        self.assertEqual(layer.data, {"a": 1})
        self.assertEqual(layer.name, "env:APP")
        self.assertEqual(layer.notes[("a",)], "APP_A")

    def test_reads_process_environment_by_default(self):
        os.environ["CMTEST_VALUE"] = "9"
        self.addCleanup(os.environ.pop, "CMTEST_VALUE")
        self.assertEqual(EnvVars("CMTEST").load()[0].data, {"value": 9})

    def test_empty_prefix_refused(self):
        with self.assertRaises(SourceError):
            EnvVars("")


class EnvFileTests(TempDirTestCase):
    def load(self, text, **options):
        path = self.write("x.env", text)
        return EnvFile(path, **options).load()[0]

    def test_basics(self):
        layer = self.load("# comment\n\nA=1\nexport B__C=hello\n  D = spaced\n")
        self.assertEqual(layer.data, {"a": 1, "b": {"c": "hello"}, "d": "spaced"})

    def test_quoting_follows_the_shell(self):
        layer = self.load(
            "A='single $X # kept'\n"
            'B="double \\"q\\" \\$ \\\\ \\n"\n'
            "C='it'\\''s'\n"
            "D=escaped\\ space\n"
            'E=""\n'
        )
        self.assertEqual(
            layer.data,
            {
                "a": "single $X # kept",
                "b": 'double "q" $ \\ \\n',
                "c": "it's",
                "d": "escaped space",
                "e": "",
            },
        )

    def test_comments(self):
        layer = self.load("A=1 # trailing\nB=a#b\nC= # only comment\n")
        self.assertEqual(layer.data, {"a": 1, "b": "a#b", "c": ""})

    def test_later_assignment_wins_and_notes_have_line(self):
        layer = self.load("A=1\nA=2\n")
        self.assertEqual(layer.data, {"a": 2})
        self.assertEqual(layer.notes[("a",)], "A, line 2")

    def test_prefix_filters(self):
        layer = self.load("APP_X=1\nOTHER=2\n", prefix="APP")
        self.assertEqual(layer.data, {"x": 1})

    def test_bad_line_reports_position(self):
        path = self.write("x.env", "A=1\n  not an assignment\n")
        with self.assertRaises(ConfigParseError) as caught:
            EnvFile(path).load()
        self.assertEqual(caught.exception.line, 2)
        self.assertEqual(caught.exception.column, 3)

    def test_unterminated_quotes(self):
        for text in ["A='open\n", 'A="open\n']:
            path = self.write("x.env", text)
            with self.assertRaisesRegex(ConfigParseError, "unterminated"):
                EnvFile(path).load()


class CliOverridesTests(unittest.TestCase):
    def data(self, *items):
        return CliOverrides(items).load()[0].data

    def test_paths_and_coercion(self):
        self.assertEqual(self.data("a.b=1", "a.c=true", "d=x"), {"a": {"b": 1, "c": True}, "d": "x"})

    def test_value_may_contain_equals(self):
        self.assertEqual(self.data("q=a=b"), {"q": "a=b"})

    def test_quoted_key_may_contain_equals_and_dots(self):
        self.assertEqual(self.data('"a=b.c"=1'), {"a=b.c": 1})

    def test_json_value(self):
        self.assertEqual(self.data("a=[1,2]"), {"a": [1, 2]})

    def test_empty_value(self):
        self.assertEqual(self.data("a="), {"a": ""})

    def test_order_decides_duplicates(self):
        self.assertEqual(self.data("a=1", "a=2"), {"a": 2})
        self.assertEqual(self.data("a=1", "a.b=2"), {"a": {"b": 2}})

    def test_note_is_original_argument(self):
        layer = CliOverrides(["a.b=1"]).load()[0]
        self.assertEqual(layer.notes[("a", "b")], "a.b=1")

    def test_errors(self):
        for bad in ["noequals", "=value", "a..b=1", "a[0]=1"]:
            with self.subTest(bad=bad):
                with self.assertRaises(SourceError):
                    self.data(bad)


class DictSourceTests(unittest.TestCase):
    def test_copies_and_normalizes(self):
        original = {"a": ({"b": 1},)}
        layer = DictSource(original).load()[0]
        self.assertEqual(layer.data, {"a": [{"b": 1}]})
        self.assertEqual(layer.name, "defaults")
        layer.data["a"][0]["b"] = 2
        self.assertEqual(original["a"][0]["b"], 1)

    def test_non_string_key_rejected(self):
        with self.assertRaisesRegex(SourceError, "not a string"):
            DictSource({"a": {1: "x"}}).load()


class DirectoryTests(TempDirTestCase):
    def test_sorted_one_layer_per_file(self):
        self.write("conf/20-b.toml", "x = 2\n")
        self.write("conf/10-a.json", '{"x": 1}')
        self.write("conf/ignored.txt", "not config")
        self.write("conf/.hidden.toml", "x = 99\n")
        layers = Directory(self.dir / "conf").load()
        self.assertEqual([Path(layer.name).name for layer in layers], ["10-a.json", "20-b.toml"])

    def test_not_recursive_by_default(self):
        self.write("conf/a.toml", "x = 1\n")
        self.write("conf/sub/b.toml", "y = 2\n")
        self.assertEqual(len(Directory(self.dir / "conf").load()), 1)
        self.assertEqual(len(Directory(self.dir / "conf", recursive=True).load()), 2)

    def test_missing(self):
        with self.assertRaisesRegex(SourceError, "not found"):
            Directory(self.dir / "absent").load()
        self.assertEqual(Directory(self.dir / "absent", optional=True).load(), [])

    def test_file_instead_of_directory(self):
        path = self.write("file.toml", "")
        with self.assertRaisesRegex(SourceError, "not a directory"):
            Directory(path).load()

    def test_parse_error_names_the_fragment(self):
        path = self.write("conf/bad.toml", "x = \n")
        with self.assertRaises(ConfigParseError) as caught:
            Directory(self.dir / "conf").load()
        self.assertEqual(caught.exception.file, str(path))


class FileSourceTests(unittest.TestCase):
    def test_dispatch_by_suffix(self):
        self.assertIsInstance(file_source("a.toml"), TomlFile)
        self.assertIsInstance(file_source("a.JSON"), JsonFile)
        self.assertIsInstance(file_source("a.env"), EnvFile)
        self.assertIsInstance(file_source(".env"), EnvFile)

    def test_unknown_suffix(self):
        with self.assertRaisesRegex(SourceError, "unsupported file type"):
            file_source("a.yaml")


if __name__ == "__main__":
    unittest.main()
