"""Page rendering, formatting helpers and graceful degradation.

Every page must render two lines that fit the panel even when every metric
behind it is missing - that is the failure mode that used to take the daemon
down on a machine with no disks or no sensors.
"""

import os
import tempfile
import unittest

from helpers import load_lcd, write_sysfs_tree

lcd = load_lcd()

EMPTY_SNAPSHOT = {}

FULL_SNAPSHOT = {
    "hostname": "NAS", "ip": "192.0.2.10", "cpu_temp": 41, "board_temp": 38,
    "nic_temp": 52, "load": "0.31", "mem_used": 3204, "mem_total": 16384,
    "uptime": 93784.0, "cpu_pct": 7, "datetime": "12:30 12/Sep/2026",
    "fan_channels": {1: {"rpm": 0, "pwm": 0, "percent": 0},
                     3: {"rpm": 1022, "pwm": 68, "percent": 27}},
    "rx_rate": 1500000, "tx_rate": 900, "disk_count": 4,
}


class FormatTests(unittest.TestCase):
    def test_fmt_bytes_scales(self):
        self.assertEqual(lcd.fmt_bytes(0), "0B")
        self.assertEqual(lcd.fmt_bytes(999), "999B")
        self.assertEqual(lcd.fmt_bytes(1000), "1K")
        self.assertEqual(lcd.fmt_bytes(1_500_000), "1.5M")
        self.assertEqual(lcd.fmt_bytes(2_000_000_000), "2.0G")

    def test_fmt_bytes_handles_junk(self):
        self.assertEqual(lcd.fmt_bytes(None), "N/A")
        self.assertEqual(lcd.fmt_bytes("abc"), "N/A")

    def test_fmt_gib(self):
        self.assertEqual(lcd.fmt_gib(16384), "16.0")
        self.assertEqual(lcd.fmt_gib(3204), "3.1")
        self.assertEqual(lcd.fmt_gib(131072), "128")
        self.assertIsNone(lcd.fmt_gib(None))
        self.assertIsNone(lcd.fmt_gib("lots"))

    def test_fmt_gib_never_exceeds_five_characters(self):
        for mib in (0, 512, 16384, 131072, 1048576, 16777216):
            self.assertLessEqual(len(lcd.fmt_gib(mib)), 5, mib)

    def test_fmt_load_trades_precision_for_width(self):
        self.assertEqual(lcd.fmt_load("0.31"), "0.31")
        self.assertEqual(lcd.fmt_load("9.99"), "9.99")
        self.assertEqual(lcd.fmt_load("12.34"), "12.3")
        self.assertEqual(lcd.fmt_load("123.4"), "123")
        self.assertEqual(lcd.fmt_load("4321"), "4321")
        self.assertEqual(lcd.fmt_load("99999"), "999+")
        self.assertEqual(lcd.fmt_load(None), "N/A")
        self.assertEqual(lcd.fmt_load("busy"), "N/A")

    def test_fmt_load_never_exceeds_four_characters(self):
        for value in ("0.00", "9.99", "10.5", "99.99", "100", "999", "1000",
                      "4321", "99999", "1234567"):
            self.assertLessEqual(len(lcd.fmt_load(value)), 4, value)

    def test_fmt_uptime_units(self):
        self.assertEqual(lcd.fmt_uptime(59), "0m")
        self.assertEqual(lcd.fmt_uptime(600), "10m")
        self.assertEqual(lcd.fmt_uptime(3660), "1h1m")
        self.assertEqual(lcd.fmt_uptime(93784), "1d2h")
        self.assertEqual(lcd.fmt_uptime(None), "N/A")
        self.assertEqual(lcd.fmt_uptime(-5), "N/A")

    def test_millidegrees_conversion(self):
        self.assertEqual(lcd.millidegrees_to_c("41000"), 41)
        self.assertIsNone(lcd.millidegrees_to_c(None))
        self.assertIsNone(lcd.millidegrees_to_c("warm"))

    def test_pwm_to_percent(self):
        self.assertEqual(lcd.pwm_to_percent(255), 100)
        self.assertEqual(lcd.pwm_to_percent(0), 0)
        self.assertEqual(lcd.pwm_to_percent(68), 27)
        self.assertIsNone(lcd.pwm_to_percent(None))


class PageRenderTests(unittest.TestCase):
    def assert_fits(self, lines):
        for line in lines:
            self.assertLessEqual(len(lcd.pad_line(line)), 16)
            self.assertEqual(len(lcd.pad_line(line)), 16)

    def test_every_page_renders_with_full_data(self):
        for name, func in lcd.PAGES.items():
            with self.subTest(page=name):
                line1, line2 = func(FULL_SNAPSHOT)
                self.assert_fits([line1, line2])

    def test_every_page_renders_with_no_data_at_all(self):
        # The real regression: absent sensors must produce N/A, not a traceback.
        for name, func in lcd.PAGES.items():
            with self.subTest(page=name):
                line1, line2 = func(EMPTY_SNAPSHOT)
                self.assert_fits([line1, line2])

    def test_missing_sensor_renders_na(self):
        line1, line2 = lcd.page_temps({"cpu_temp": None, "board_temp": None,
                                       "nic_temp": None})
        self.assertIn("N/A", line1)
        self.assertIn("N/A", line2)

    def test_ram_line_fits_on_a_large_memory_machine(self):
        # 16 GiB rendered as dotted mebibytes was 18 characters, so the row was
        # silently truncated on the reference machine itself.
        _, line2 = lcd.page_cpu({"mem_used": 16000, "mem_total": 16384})
        self.assertLessEqual(len(line2), 16)
        self.assertEqual(line2, "RAM 15.6/16.0G")

    def test_ram_line_when_memory_is_unreadable(self):
        _, line2 = lcd.page_cpu({"mem_used": None, "mem_total": None})
        self.assertEqual(line2, "RAM N/A")

    def test_fan_page_reports_the_active_channel_not_a_fixed_one(self):
        # The reference unit drives channel 3, so nothing may hard-code 2.
        line1, _ = lcd.page_fan(FULL_SNAPSHOT)
        self.assertIn("Fan3", line1)
        self.assertIn("1022", line1)

    def test_fan_page_with_no_active_channel(self):
        snapshot = {"fan_channels": {1: {"rpm": 0, "pwm": 0, "percent": 0}}}
        line1, line2 = lcd.page_fan(snapshot)
        self.assertIn("N/A", line1)
        self.assert_fits([line1, line2])

    def test_fan_page_with_no_controller(self):
        line1, _ = lcd.page_fan({"fan_channels": {}})
        self.assertIn("N/A", line1)

    def test_uptime_page_with_zero_disks(self):
        line1, line2 = lcd.page_uptime({"uptime": 120.0, "disk_count": 0})
        self.assertEqual(line2, "0 disks online")
        self.assert_fits([line1, line2])

    def test_active_fan_channels_ordering(self):
        channels = {3: {"rpm": 900}, 1: {"rpm": 700}, 2: {"rpm": 0}}
        self.assertEqual(lcd.active_fan_channels(channels), [1, 3])
        self.assertEqual(lcd.active_fan_channels({}), [])


class DetailPageTests(unittest.TestCase):
    FULL_DISK = {"name": "sda", "size": "3.6T", "model": "EXAMPLEDISK 4TB",
                 "temp": 34, "hours": 15000, "cycles": 42, "realloc": 0,
                 "pending": 0, "health": "OK"}

    def test_pages_are_produced(self):
        self.assertEqual(len(lcd.detail_pages(self.FULL_DISK)), 5)

    def test_pages_fit_the_panel(self):
        for page in lcd.detail_pages(self.FULL_DISK):
            self.assertEqual(len(lcd.pad_line(page)), 16)

    def test_reallocated_sectors_raise_a_warning_marker(self):
        disk = dict(self.FULL_DISK, realloc=7)
        pages = lcd.detail_pages(disk)
        self.assertTrue(pages[0].endswith("!"), pages[0])
        self.assertIn("7", pages[2])

    def test_warning_marker_is_dropped_rather_than_overflowing(self):
        # At the widest plausible summary there is no column left for it, and
        # the count is still reported on its own page.
        disk = {"size": "999.9T", "temp": 100, "health": "FAIL", "realloc": 9}
        pages = lcd.detail_pages(disk)
        self.assertEqual(len(pages[0]), 16)
        self.assertFalse(pages[0].endswith("!"))
        self.assertIn("9", pages[2])

    def test_no_warning_when_clean(self):
        self.assertNotIn("!", lcd.detail_pages(self.FULL_DISK)[0])

    def test_disk_without_any_smart_data(self):
        # USB bridges and some NVMe devices report nothing useful.
        disk = {"name": "sdb", "size": "1.8T"}
        pages = lcd.detail_pages(disk)
        self.assertEqual(len(pages), 5)
        self.assertIn("N/A", pages[1])

    def test_completely_empty_disk_record(self):
        pages = lcd.detail_pages({})
        self.assertEqual(len(pages), 5)
        for page in pages:
            self.assertEqual(len(lcd.pad_line(page)), 16)

    def test_non_numeric_realloc_does_not_raise(self):
        disk = dict(self.FULL_DISK, realloc="unknown")
        self.assertEqual(len(lcd.detail_pages(disk)), 5)


class HwmonDiscoveryTests(unittest.TestCase):
    """Sensor discovery must be name-based; hwmonN indices are not stable."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = write_sysfs_tree(self.tmp.name, {
            "hwmon0/name": "acpitz\n",
            "hwmon0/temp1_input": "38000\n",
            "hwmon2/name": "coretemp\n",
            "hwmon2/temp1_input": "41000\n",
            "hwmon10/name": "r8169_0_100:00\n",
            "hwmon10/temp1_input": "52000\n",
        })

    def test_hwmon_temp_reads_first_available_input(self):
        self.assertEqual(lcd.hwmon_temp(os.path.join(self.root, "hwmon2")), 41)

    def test_hwmon_temp_of_none_is_none(self):
        self.assertIsNone(lcd.hwmon_temp(None))

    def test_hwmon_temp_of_directory_without_inputs(self):
        empty = os.path.join(self.root, "hwmon0", "missing")
        self.assertIsNone(lcd.hwmon_temp(empty))

    def test_find_hwmon_with_empty_name_returns_none(self):
        self.assertIsNone(lcd.find_hwmon(""))
        self.assertIsNone(lcd.find_hwmon(None))

    def test_find_hwmon_prefix_with_empty_prefix(self):
        self.assertIsNone(lcd.find_hwmon_prefix(""))


class FanDeviceDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_single_match_is_resolved(self):
        root = write_sysfs_tree(self.tmp.name, {
            "f71882fg.2592/fan3_input": "1022\n",
            "f71882fg.2592/pwm3": "68\n",
        })
        found = lcd.find_fan_device(os.path.join(root, "f71882fg.*"))
        self.assertTrue(found.endswith("f71882fg.2592"))

    def test_ambiguous_match_is_refused(self):
        root = write_sysfs_tree(self.tmp.name, {
            "f71882fg.2592/fan1_input": "0\n",
            "f71882fg.1024/fan1_input": "0\n",
        })
        self.assertIsNone(lcd.find_fan_device(os.path.join(root, "f71882fg.*")))

    def test_no_match_is_none(self):
        self.assertIsNone(lcd.find_fan_device(
            os.path.join(self.tmp.name, "nothing.*")))

    def test_channels_are_read_from_whatever_the_chip_exposes(self):
        root = write_sysfs_tree(self.tmp.name, {
            "f71882fg.2592/fan1_input": "0\n",
            "f71882fg.2592/pwm1": "0\n",
            "f71882fg.2592/fan3_input": "1022\n",
            "f71882fg.2592/pwm3": "68\n",
        })
        device = lcd.find_fan_device(os.path.join(root, "f71882fg.*"))
        channels = lcd.read_fan_channels(device)
        self.assertEqual(sorted(channels), [1, 3])
        self.assertEqual(channels[3]["rpm"], 1022)
        self.assertEqual(channels[3]["percent"], 27)
        self.assertEqual(lcd.active_fan_channels(channels), [3])

    def test_missing_pwm_file_is_tolerated(self):
        root = write_sysfs_tree(self.tmp.name, {
            "f71882fg.2592/fan2_input": "800\n",
        })
        device = lcd.find_fan_device(os.path.join(root, "f71882fg.*"))
        channels = lcd.read_fan_channels(device)
        self.assertIsNone(channels[2]["pwm"])
        self.assertIsNone(channels[2]["percent"])

    def test_read_fan_channels_of_none(self):
        self.assertEqual(lcd.read_fan_channels(None), {})


class SmartParsingTests(unittest.TestCase):
    def test_attribute_lookup(self):
        data = {"ata_smart_attributes": {"table": [
            {"id": 5, "raw": {"value": 0}},
            {"id": 197, "raw": {"value": 3}},
        ]}}
        self.assertEqual(lcd.smart_attribute(data, 5), 0)
        self.assertEqual(lcd.smart_attribute(data, 197), 3)
        self.assertIsNone(lcd.smart_attribute(data, 9))

    def test_attribute_lookup_without_a_table(self):
        self.assertIsNone(lcd.smart_attribute({}, 5))
        self.assertIsNone(lcd.smart_attribute({"ata_smart_attributes": None}, 5))

    def test_human_size(self):
        self.assertEqual(lcd.human_size(0), "0B")
        self.assertIsNone(lcd.human_size(None))
        self.assertIsNone(lcd.human_size("big"))


class CpuAndNetTests(unittest.TestCase):
    def test_cpu_percent_between_samples(self):
        self.assertEqual(lcd.cpu_percent((100, 200), (100, 300)), 100)
        self.assertEqual(lcd.cpu_percent((100, 200), (200, 300)), 0)

    def test_cpu_percent_with_missing_or_stale_samples(self):
        self.assertIsNone(lcd.cpu_percent(None, (1, 2)))
        self.assertIsNone(lcd.cpu_percent((1, 2), None))
        self.assertIsNone(lcd.cpu_percent((1, 200), (1, 200)))

    def test_net_rates(self):
        rx, tx = lcd.net_rates((0, 0), (1000, 2000), 2.0)
        self.assertEqual((rx, tx), (500, 1000))

    def test_net_rates_reject_counter_reset(self):
        self.assertEqual(lcd.net_rates((5000, 5000), (10, 10), 2.0), (None, None))

    def test_net_rates_reject_zero_elapsed(self):
        self.assertEqual(lcd.net_rates((0, 0), (10, 10), 0), (None, None))

    def test_net_rates_with_missing_samples(self):
        self.assertEqual(lcd.net_rates(None, (1, 1), 1.0), (None, None))


if __name__ == "__main__":
    unittest.main()


class SmartNullFieldTests(unittest.TestCase):
    """smartctl emits explicit nulls; dict.get's default does not cover those.

    Regression test: `data.get("device", {}).get("name")` raised AttributeError
    when smartctl reported `"device": null`, which killed the daemon.
    """

    def _with_payload(self, payload):
        """Run read_smart against a canned smartctl JSON document."""
        original = lcd.run_json
        lcd.run_json = lambda argv, timeout=20: payload
        self.addCleanup(setattr, lcd, "run_json", original)
        return lcd.read_smart("/dev/sda")

    def test_null_device_object_does_not_raise(self):
        info = self._with_payload({"device": None})
        self.assertIsNone(info["model"])

    def test_every_container_null_is_survivable(self):
        info = self._with_payload({"device": None, "temperature": None,
                                   "power_on_time": None, "smart_status": None,
                                   "ata_smart_attributes": None})
        self.assertEqual(set(info), {"model", "temp", "hours", "cycles",
                                     "realloc", "pending", "health"})
        self.assertTrue(all(value is None for value in info.values()))

    def test_model_falls_back_to_the_device_name(self):
        info = self._with_payload({"device": {"name": "/dev/sda"}})
        self.assertEqual(info["model"], "/dev/sda")

    def test_full_payload_is_parsed(self):
        info = self._with_payload({
            "model_name": "EXAMPLEDISK 4TB",
            "temperature": {"current": 34},
            "power_on_time": {"hours": 15000},
            "power_cycle_count": 42,
            "smart_status": {"passed": True},
            "ata_smart_attributes": {"table": [{"id": 5, "raw": {"value": 0}}]},
        })
        self.assertEqual(info["model"], "EXAMPLEDISK 4TB")
        self.assertEqual(info["temp"], 34)
        self.assertEqual(info["hours"], 15000)
        self.assertEqual(info["cycles"], 42)
        self.assertEqual(info["health"], "OK")
        self.assertEqual(info["realloc"], 0)

    def test_failed_health_is_reported(self):
        info = self._with_payload({"smart_status": {"passed": False}})
        self.assertEqual(info["health"], "FAIL")

    def test_smartctl_absent_yields_all_none(self):
        info = self._with_payload(None)
        self.assertTrue(all(value is None for value in info.values()))

    def test_smart_attribute_with_null_raw(self):
        data = {"ata_smart_attributes": {"table": [{"id": 5, "raw": {}}]}}
        self.assertIsNone(lcd.smart_attribute(data, 5))

    def test_smart_attribute_falls_back_to_string(self):
        data = {"ata_smart_attributes": {"table": [
            {"id": 5, "raw": {"string": "0"}}]}}
        self.assertEqual(lcd.smart_attribute(data, 5), "0")


class PreflightTests(unittest.TestCase):
    """--check must never write to the display and must never raise."""

    def test_preflight_on_a_machine_without_the_panel(self):
        conf = lcd.parse_config("serial_port = /dev/does-not-exist")
        ok, lines = lcd.preflight(conf, verbose=False)
        self.assertFalse(ok)
        self.assertTrue(any("does not exist" in line for line in lines))

    def test_preflight_reports_config_problems(self):
        import os
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as fh:
            fh.write("bogus_key = 1\n")
            path = fh.name
        self.addCleanup(os.unlink, path)
        conf = lcd.parse_config("serial_port = /dev/does-not-exist")
        _, lines = lcd.preflight(conf, verbose=False, config_path=path)
        self.assertTrue(any("unknown key" in line for line in lines))

    def test_preflight_flags_an_oversized_date_format(self):
        conf = lcd.parse_config(
            "serial_port = /dev/does-not-exist\n"
            "date_format = %Y-%m-%d %H:%M:%S %Z\n")
        _, lines = lcd.preflight(conf, verbose=False)
        self.assertTrue(any("date_format" in line for line in lines))


class CliTests(unittest.TestCase):
    def test_list_pages_and_check_are_mutually_exclusive(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            lcd.build_parser().parse_args(["--check", "--once"])

    def test_test_message_has_a_default_payload(self):
        args = lcd.build_parser().parse_args(["--test-message"])
        self.assertIn("|", args.test_message)

    def test_test_message_accepts_custom_text(self):
        args = lcd.build_parser().parse_args(["--test-message", "a|b"])
        self.assertEqual(args.test_message, "a|b")

    def test_port_override(self):
        self.assertEqual(lcd.build_parser().parse_args(["--port", "/dev/x"]).port,
                         "/dev/x")

    def test_default_config_path(self):
        self.assertEqual(lcd.build_parser().parse_args([]).config,
                         lcd.DEFAULT_CONFIG_PATH)


class RowWidthTests(unittest.TestCase):
    """No page may overflow 16 columns at any plausible value.

    Truncation is silent, so an overflow removes information without any
    visible sign that it happened - which is how the year lost a digit from the
    clock and the RAM row lost its unit.
    """

    WORST = {
        "hostname": "A" * 16,
        "ip": "255.255.255.255",
        "cpu_temp": 100, "board_temp": 100, "nic_temp": 100,
        "load": "999.99",
        "mem_used": 16777215, "mem_total": 16777216,   # 16 TiB
        "uptime": 9999 * 86400,
        "cpu_pct": 100,
        "datetime": "31/12/2026 23:59",
        "fan_channels": {10: {"rpm": 99999, "pwm": 255, "percent": 100},
                         11: {"rpm": 99999, "pwm": 255, "percent": 100}},
        "rx_rate": 999_999_999_999, "tx_rate": 999_999_999_999,
        "disk_count": 9999,
    }

    def test_every_page_fits_at_worst_case_values(self):
        for name, func in lcd.PAGES.items():
            for row, line in enumerate(func(self.WORST)):
                with self.subTest(page=name, row=row):
                    self.assertLessEqual(
                        len(line), 16,
                        "page %r row %d renders %d chars: %r"
                        % (name, row, len(line), line))

    def test_every_page_fits_with_a_single_active_fan(self):
        snapshot = dict(self.WORST,
                        fan_channels={3: {"rpm": 99999, "pwm": 255,
                                          "percent": 100}})
        for name, func in lcd.PAGES.items():
            for line in func(snapshot):
                self.assertLessEqual(len(line), 16, "%s: %r" % (name, line))

    def test_detail_pages_fit_at_worst_case_values(self):
        disk = {"name": "nvme0n1", "size": "999.9T", "temp": 100,
                "health": "FAIL", "model": "M" * 40, "hours": 999999,
                "cycles": 99999, "realloc": 65535, "pending": 65535}
        pages = lcd.detail_pages(disk)
        # The model is free text and is allowed to truncate; the rest is not.
        for index, page in enumerate(pages):
            if index == 1:
                continue
            self.assertLessEqual(len(page), 16,
                                 "detail page %d renders %d chars: %r"
                                 % (index + 1, len(page), page))

    def test_detail_header_row_fits(self):
        for name in ("sda", "nvme0n1", "mmcblk0"):
            header = "[%s] %d/%d" % (name, 5, 5)
            self.assertLessEqual(len(header), 16, header)


class DeviceOpenErrorTests(unittest.TestCase):
    """A wrong port is the likeliest beginner mistake and the first documented
    step; it must produce advice rather than a traceback."""

    def _error_for(self, port):
        conf = dict(lcd.DEFAULTS, serial_port=port)
        with self.assertRaises(lcd.LcdError) as ctx:
            lcd.open_device(conf)
        return str(ctx.exception)

    def test_missing_port(self):
        message = self._error_for("/dev/definitely-not-here")
        self.assertIn("does not exist", message)
        self.assertIn("--check", message)

    def test_directory_instead_of_port(self):
        self.assertIn("directory", self._error_for(tempfile.gettempdir()))

    def test_regular_file_instead_of_port(self):
        with tempfile.NamedTemporaryFile(suffix=".notatty") as handle:
            message = self._error_for(handle.name)
        self.assertIn("not a serial port", message)
        self.assertIn("--check", message)

    def test_cli_reports_the_error_without_a_traceback(self):
        import contextlib
        import io
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = lcd.cli(["--port", "/dev/definitely-not-here",
                            "--config", "/nonexistent", "--once"])
        self.assertEqual(code, 1)
        self.assertTrue(stderr.getvalue().startswith("error: "))
        self.assertNotIn("Traceback", stderr.getvalue())
