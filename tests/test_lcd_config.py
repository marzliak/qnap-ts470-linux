"""Configuration parsing, defaults and page selection."""

import unittest

from helpers import load_lcd

lcd = load_lcd()


class ParseConfigTests(unittest.TestCase):
    def test_empty_body_yields_defaults(self):
        conf = lcd.parse_config("")
        self.assertEqual(conf, lcd.DEFAULTS)

    def test_simple_assignment(self):
        conf = lcd.parse_config("serial_port = /dev/ttyS2")
        self.assertEqual(conf["serial_port"], "/dev/ttyS2")

    def test_comments_and_blank_lines_ignored(self):
        conf = lcd.parse_config(
            "# a comment\n\n  \nserial_port = /dev/ttyS3  # trailing comment\n")
        self.assertEqual(conf["serial_port"], "/dev/ttyS3")

    def test_quotes_are_stripped(self):
        self.assertEqual(lcd.parse_config('serial_port = "/dev/ttyUSB0"')["serial_port"],
                         "/dev/ttyUSB0")

    def test_numeric_coercion_follows_the_default_type(self):
        conf = lcd.parse_config("page_interval = 2.5\n")
        self.assertIsInstance(conf["page_interval"], float)
        self.assertEqual(conf["page_interval"], 2.5)

    def test_boolean_forms(self):
        for value in ("true", "TRUE", "yes", "on", "1"):
            self.assertIs(lcd.parse_config("smart_enabled = %s" % value)["smart_enabled"],
                          True, value)
        for value in ("false", "No", "off", "0"):
            self.assertIs(lcd.parse_config("smart_enabled = %s" % value)["smart_enabled"],
                          False, value)

    def test_unparseable_value_falls_back_to_default(self):
        # A typo in one key must not take the service down on boot.
        conf = lcd.parse_config("page_interval = soon")
        self.assertEqual(conf["page_interval"], lcd.DEFAULTS["page_interval"])

    def test_unknown_keys_are_ignored(self):
        conf = lcd.parse_config("future_option = 1\nserial_port = /dev/ttyS1")
        self.assertNotIn("future_option", conf)
        self.assertEqual(conf["serial_port"], "/dev/ttyS1")

    def test_key_case_is_insensitive(self):
        self.assertEqual(lcd.parse_config("SERIAL_PORT = /dev/x")["serial_port"],
                         "/dev/x")

    def test_line_without_equals_is_skipped(self):
        self.assertEqual(lcd.parse_config("nonsense\n")["serial_port"],
                         lcd.DEFAULTS["serial_port"])

    def test_empty_value_is_allowed_for_optional_strings(self):
        self.assertEqual(lcd.parse_config("net_interface =")["net_interface"], "")

    def test_load_config_of_missing_file_returns_defaults(self):
        conf = lcd.load_config("/nonexistent/qnap-tsx70-lcd.conf")
        self.assertEqual(conf, lcd.DEFAULTS)


class PageSelectionTests(unittest.TestCase):
    def test_default_pages_all_resolve(self):
        conf = dict(lcd.DEFAULTS)
        self.assertEqual(lcd.enabled_pages(conf),
                         ["host", "cpu", "uptime", "cpuload", "fan", "net", "temps"])

    def test_unknown_page_names_are_dropped(self):
        conf = lcd.parse_config("pages = cpu,does_not_exist,net")
        self.assertEqual(lcd.enabled_pages(conf), ["cpu", "net"])

    def test_order_is_preserved(self):
        conf = lcd.parse_config("pages = net,cpu")
        self.assertEqual(lcd.enabled_pages(conf), ["net", "cpu"])

    def test_whitespace_and_case_tolerated(self):
        conf = lcd.parse_config("pages =  CPU , net ")
        self.assertEqual(lcd.enabled_pages(conf), ["cpu", "net"])

    def test_all_empty_selection(self):
        conf = lcd.parse_config("pages = nothing_valid")
        self.assertEqual(lcd.enabled_pages(conf), [])


class ExampleConfigTests(unittest.TestCase):
    """The shipped example must stay loadable and in sync with the defaults."""

    def setUp(self):
        import os
        from helpers import REPO_ROOT
        path = os.path.join(REPO_ROOT, "config/qnap-tsx70-lcd.conf.example")
        with open(path, encoding="utf-8") as handle:
            self.body = handle.read()

    def test_example_parses_to_the_built_in_defaults(self):
        self.assertEqual(lcd.parse_config(self.body), lcd.DEFAULTS)

    def test_every_default_key_is_documented(self):
        for key in lcd.DEFAULTS:
            self.assertIn(key, self.body, "%s missing from the example config" % key)


if __name__ == "__main__":
    unittest.main()


class TimingLimitTests(unittest.TestCase):
    """Timing below the protocol floor produces garbled text, so it is clamped."""

    def test_write_delay_is_clamped_up(self):
        conf = lcd.parse_config("write_delay = 0.01")
        self.assertEqual(conf["write_delay"], lcd.MIN_WRITE_DELAY)

    def test_page_interval_is_clamped_up(self):
        conf = lcd.parse_config("page_interval = 0.05")
        self.assertEqual(conf["page_interval"], lcd.MIN_PAGE_INTERVAL)

    def test_generous_values_are_left_alone(self):
        conf = lcd.parse_config("write_delay = 0.5\npage_interval = 30")
        self.assertEqual(conf["write_delay"], 0.5)
        self.assertEqual(conf["page_interval"], 30.0)

    def test_defaults_already_satisfy_the_floor(self):
        self.assertGreaterEqual(lcd.DEFAULTS["write_delay"], lcd.MIN_WRITE_DELAY)
        self.assertGreaterEqual(lcd.DEFAULTS["page_interval"], lcd.MIN_PAGE_INTERVAL)


class ConfigProblemTests(unittest.TestCase):
    """A silently ignored typo leaves the operator believing a change took."""

    def test_clean_config_has_no_problems(self):
        self.assertEqual(lcd.config_problems("serial_port = /dev/ttyS1\n"), [])

    def test_comments_and_blanks_are_not_problems(self):
        self.assertEqual(lcd.config_problems("# hello\n\n   \n"), [])

    def test_unknown_key_is_reported(self):
        problems = lcd.config_problems("serail_port = /dev/ttyS1")
        self.assertEqual(len(problems), 1)
        self.assertIn("unknown key", problems[0])

    def test_line_without_equals_is_reported(self):
        self.assertIn("not a key", lcd.config_problems("serial_port")[0])

    def test_non_numeric_number_is_reported(self):
        self.assertIn("not a number", lcd.config_problems("page_interval = soon")[0])

    def test_non_boolean_is_reported(self):
        self.assertIn("not a boolean",
                      lcd.config_problems("smart_enabled = maybe")[0])

    def test_unknown_page_name_is_reported(self):
        problems = lcd.config_problems("pages = cpu,bogus,net")
        self.assertEqual(len(problems), 1)
        self.assertIn("unknown page", problems[0])

    def test_the_shipped_example_is_clean(self):
        import os
        from helpers import REPO_ROOT
        path = os.path.join(REPO_ROOT, "config/qnap-tsx70-lcd.conf.example")
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(lcd.config_problems(handle.read()), [])


class DateFormatTests(unittest.TestCase):
    """The date shares a 16-column row; a format that overflows loses digits."""

    def test_default_format_fits_the_panel(self):
        import time
        rendered = time.strftime(lcd.DEFAULTS["date_format"])
        self.assertLessEqual(len(rendered), lcd.LCD_COLS,
                             "default date_format renders %r (%d chars)"
                             % (rendered, len(rendered)))

    def test_default_format_is_locale_independent(self):
        # %b and %a expand differently per locale and can overflow the row.
        self.assertNotIn("%b", lcd.DEFAULTS["date_format"])
        self.assertNotIn("%a", lcd.DEFAULTS["date_format"])

    def test_cpuload_page_row_two_fits(self):
        import time
        snapshot = {"cpu_pct": 100,
                    "datetime": time.strftime(lcd.DEFAULTS["date_format"])}
        line1, line2 = lcd.page_cpuload(snapshot)
        self.assertEqual(lcd.pad_line(line2).rstrip(b" ").decode(),
                         snapshot["datetime"])
