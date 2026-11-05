import re
import unittest

from configmerge.interpolate import Interpolator
from configmerge.paths import PathPattern
from configmerge.provenance import Provenance
from configmerge.redact import DEFAULT_SECRET_KEYS, Redactor, tainted_paths


class KeyDetectionTests(unittest.TestCase):
    def test_secret_looking_names(self):
        for key in ["password", "PASSWORD", "db_passwd", "passphrase", "smtp_pass", "client_secret", "secrets", "auth_token", "api_key", "apikey", "API-KEY", "private_key", "credentials"]:
            with self.subTest(key=key):
                self.assertIsNotNone(DEFAULT_SECRET_KEYS.search(key))

    def test_innocent_names(self):
        for key in ["host", "port", "bypass", "passenger", "username", "compass", "key", "pass_through"]:
            with self.subTest(key=key):
                self.assertIsNone(DEFAULT_SECRET_KEYS.search(key))


class RedactorTests(unittest.TestCase):
    def test_by_key_name_anywhere_in_the_tree(self):
        result = Redactor().redact({"db": {"password": "hunter22", "host": "h"}, "l": [{"token": "t"}]})
        self.assertEqual(result, {"db": {"password": "***", "host": "h"}, "l": [{"token": "***"}]})

    def test_table_under_secret_key_is_masked_whole(self):
        self.assertEqual(Redactor().redact({"secrets": {"a": 1, "b": 2}}), {"secrets": "***"})

    def test_by_pattern(self):
        redactor = Redactor(["services.*.dsn", PathPattern("plain")])
        result = redactor.redact({"services": {"a": {"dsn": "x", "o": 1}}, "plain": 5})
        self.assertEqual(result, {"services": {"a": {"dsn": "***", "o": 1}}, "plain": "***"})

    def test_non_string_secret_masked(self):
        self.assertEqual(Redactor().redact({"token": 12345}), {"token": "***"})

    def test_null_stays_visible(self):
        self.assertEqual(Redactor().redact({"token": None}), {"token": None})

    def test_custom_mask_and_key_regex(self):
        redactor = Redactor(mask="<hidden>", key_regex=re.compile("^pin$"))
        self.assertEqual(redactor.redact({"pin": 1, "password": "p"}), {"pin": "<hidden>", "password": "p"})

    def test_key_detection_can_be_disabled(self):
        self.assertEqual(Redactor(key_regex=None).redact({"password": "p"}), {"password": "p"})

    def test_input_not_mutated(self):
        data = {"password": "p", "t": {"token": "x"}}
        Redactor().redact(data)
        self.assertEqual(data, {"password": "p", "t": {"token": "x"}})

    def test_with_patterns_keeps_settings(self):
        redactor = Redactor(mask="#").with_patterns(["a"])
        self.assertEqual(redactor.redact({"a": 1}), {"a": "#"})


class ScrubTests(unittest.TestCase):
    def test_secret_inside_other_strings(self):
        data = {"db": {"password": "hunter22"}, "dsn": "pg://u:hunter22@h/db", "list": ["x-hunter22"]}
        result = Redactor().redact(data)
        self.assertEqual(result["dsn"], "pg://u:***@h/db")
        self.assertEqual(result["list"], ["x-***"])

    def test_short_secrets_are_not_scrubbed_elsewhere(self):
        result = Redactor().redact({"password": "a", "text": "banana"})
        self.assertEqual(result, {"password": "***", "text": "banana"})

    def test_longest_secret_first(self):
        data = {"token_a": "abcd", "token_b": "abcdef", "text": "abcdef abcd"}
        self.assertEqual(Redactor().redact(data)["text"], "*** ***")

    def test_masking_a_value_from_outside_the_tree(self):
        bound = Redactor().bind({"password": "hunter22"})
        self.assertEqual(bound(("other",), "see hunter22"), "see ***")
        self.assertEqual(bound(("password",), "older value"), "***")


class TaintTests(unittest.TestCase):
    def resolve(self, data, environ=None, redactor=None):
        provenance = Provenance()
        resolved = Interpolator(environ=environ or {}, provenance=provenance).resolve(data)
        redactor = redactor or Redactor()
        tainted = tainted_paths(provenance.interpolations(), redactor, resolved)
        return redactor.redact(resolved, tainted)

    def test_env_secret_masks_whole_value(self):
        result = self.resolve({"dsn": "pg://${env:DB_PASSWORD}@h", "ok": "${env:HOST}"}, {"DB_PASSWORD": "s", "HOST": "h"})
        self.assertEqual(result, {"dsn": "***", "ok": "h"})

    def test_long_secret_path_is_scrubbed_in_place(self):
        result = self.resolve({"password": "hunter22", "dsn": "pg://${password}@h"})
        self.assertEqual(result["dsn"], "pg://***@h")

    def test_short_secret_path_taints(self):
        result = self.resolve({"password": "ab", "dsn": "pg://${password}@h"})
        self.assertEqual(result["dsn"], "***")

    def test_non_string_secret_taints(self):
        result = self.resolve({"token": 42, "x": "n=${token}"})
        self.assertEqual(result["x"], "***")

    def test_taint_follows_chains(self):
        result = self.resolve({"token": 42, "a": "${token}", "b": "v=${a}", "c": "plain"})
        self.assertEqual(result, {"token": "***", "a": "***", "b": "***", "c": "plain"})

    def test_chain_through_table_alias(self):
        result = self.resolve({"token": 42, "t": {"k": "${token}"}, "copy": "${t.k}"})
        self.assertEqual(result["copy"], "***")

    def test_references_to_ordinary_values_do_not_taint(self):
        self.assertEqual(self.resolve({"host": "h", "u": "x${host}"})["u"], "xh")


if __name__ == "__main__":
    unittest.main()
