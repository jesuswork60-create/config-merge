import datetime as dt
import unittest

from configmerge.coerce import coerce_scalar, coerce_to
from configmerge.units import parse_duration, parse_size


class CoerceScalarTests(unittest.TestCase):
    def test_booleans(self):
        self.assertIs(coerce_scalar("true"), True)
        self.assertIs(coerce_scalar("FALSE"), False)

    def test_yes_and_no_stay_strings(self):
        self.assertEqual(coerce_scalar("yes"), "yes")
        self.assertEqual(coerce_scalar("no"), "no")

    def test_integers(self):
        self.assertEqual(coerce_scalar("0"), 0)
        self.assertEqual(coerce_scalar("-42"), -42)
        self.assertEqual(coerce_scalar("+7"), 7)

    def test_leading_zero_stays_string(self):
        self.assertEqual(coerce_scalar("0123"), "0123")

    def test_floats_need_a_dot(self):
        self.assertEqual(coerce_scalar("1.5"), 1.5)
        self.assertEqual(coerce_scalar("-2.5e3"), -2500.0)
        self.assertEqual(coerce_scalar("1e5"), "1e5")
        self.assertEqual(coerce_scalar("1."), "1.")
        self.assertEqual(coerce_scalar(".5"), ".5")

    def test_version_with_two_dots_stays_string(self):
        self.assertEqual(coerce_scalar("1.2.3"), "1.2.3")

    def test_json_containers(self):
        self.assertEqual(coerce_scalar("[1, 2]"), [1, 2])
        self.assertEqual(coerce_scalar('{"a": 1}'), {"a": 1})

    def test_json_string_forces_string(self):
        self.assertEqual(coerce_scalar('"8080"'), "8080")

    def test_invalid_json_stays_text(self):
        self.assertEqual(coerce_scalar("[oops"), "[oops")
        self.assertEqual(coerce_scalar("{x}"), "{x}")

    def test_plain_text_and_empty(self):
        self.assertEqual(coerce_scalar("hello world"), "hello world")
        self.assertEqual(coerce_scalar(""), "")

    def test_inf_and_nan_stay_strings(self):
        self.assertEqual(coerce_scalar("inf"), "inf")
        self.assertEqual(coerce_scalar("nan"), "nan")


class CoerceToTests(unittest.TestCase):
    def test_bool_words(self):
        for word in ["true", "YES", "on", "1"]:
            self.assertIs(coerce_to(bool, word), True)
        for word in ["false", "No", "off", "0"]:
            self.assertIs(coerce_to(bool, word), False)
        with self.assertRaises(ValueError):
            coerce_to(bool, "maybe")

    def test_int_accepts_leading_zero_when_asked_for(self):
        self.assertEqual(coerce_to(int, " 007 "), 7)
        with self.assertRaises(ValueError):
            coerce_to(int, "7.5")

    def test_float(self):
        self.assertEqual(coerce_to(float, "1e3"), 1000.0)
        self.assertEqual(coerce_to(float, "2"), 2.0)
        for bad in ["nan", "inf", "abc", ""]:
            with self.assertRaises(ValueError):
                coerce_to(float, bad)

    def test_dates(self):
        self.assertEqual(coerce_to(dt.date, "2026-01-02"), dt.date(2026, 1, 2))
        with self.assertRaises(ValueError):
            coerce_to(dt.date, "soon")

    def test_unsupported_target(self):
        with self.assertRaises(ValueError):
            coerce_to(list, "[1]")


class DurationTests(unittest.TestCase):
    def test_single_and_compound(self):
        self.assertEqual(parse_duration("90s"), dt.timedelta(seconds=90))
        self.assertEqual(parse_duration("1h30m"), dt.timedelta(minutes=90))
        self.assertEqual(parse_duration("250ms"), dt.timedelta(milliseconds=250))
        self.assertEqual(parse_duration("1w2d"), dt.timedelta(days=9))
        self.assertEqual(parse_duration("1.5h"), dt.timedelta(minutes=90))

    def test_ms_is_not_minutes_then_junk(self):
        self.assertEqual(parse_duration("5m10ms"), dt.timedelta(minutes=5, milliseconds=10))

    def test_invalid(self):
        for bad in ["", "10", "s", "1h 30m", "10x", "h1", "-5s"]:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_duration(bad)


class SizeTests(unittest.TestCase):
    def test_units(self):
        self.assertEqual(parse_size("512"), 512)
        self.assertEqual(parse_size("10MB"), 10_000_000)
        self.assertEqual(parse_size("10MiB"), 10 * 1024**2)
        self.assertEqual(parse_size("1.5 GiB"), int(1.5 * 1024**3))
        self.assertEqual(parse_size("2k"), 2000)

    def test_invalid(self):
        for bad in ["", "MB", "10XB", "-1MB"]:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_size(bad)


if __name__ == "__main__":
    unittest.main()
