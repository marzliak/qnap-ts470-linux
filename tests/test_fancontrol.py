"""Fan control: the pure curve, cache validation and sysfs behaviour.

The controller is exercised against fake sysfs trees, so the safe-state and
discovery contracts are checked without a fan in the room.
"""

import contextlib
import io
import json
import os
import tempfile
import unittest

from helpers import load_fan, write_sysfs_tree

fan = load_fan()


@contextlib.contextmanager
def quiet():
    """Swallow expected argparse errors and safe-state logging."""
    with contextlib.redirect_stderr(io.StringIO()), \
            contextlib.redirect_stdout(io.StringIO()):
        yield


def make_chip(root, channels, rpm=1000, pwm=160, enable=2):
    """Build a fake f71882fg platform device with the given channels."""
    spec = {}
    for channel in channels:
        spec["f71882fg.2592/fan%d_input" % channel] = "%d\n" % rpm
        spec["f71882fg.2592/pwm%d" % channel] = "%d\n" % pwm
        spec["f71882fg.2592/pwm%d_enable" % channel] = "%d\n" % enable
    write_sysfs_tree(root, spec)
    return os.path.join(root, "f71882fg.2592")


class CurveTests(unittest.TestCase):
    """Boundaries of the temperature -> duty mapping."""

    def test_below_and_at_lower_bound(self):
        for temp in (-40, 0, 29, 30):
            self.assertEqual(fan.calc_pwm(temp, 75, 255), 75, temp)

    def test_at_and_above_upper_bound(self):
        for temp in (70, 71, 200):
            self.assertEqual(fan.calc_pwm(temp, 75, 255), 255, temp)

    def test_midpoint_is_halfway(self):
        self.assertEqual(fan.calc_pwm(50, 75, 255), 165)

    def test_monotonic_across_the_range(self):
        previous = -1
        for temp in range(-10, 100):
            value = fan.calc_pwm(temp, 75, 255)
            self.assertGreaterEqual(value, previous)
            previous = value

    def test_result_always_within_pwm_range(self):
        for temp in range(-60, 160, 7):
            value = fan.calc_pwm(temp, 0, 255)
            self.assertGreaterEqual(value, fan.PWM_MIN)
            self.assertLessEqual(value, fan.PWM_MAX)

    def test_inverted_bounds_are_normalised(self):
        self.assertEqual(fan.calc_pwm(50, 255, 75), 165)

    def test_degenerate_temperature_window_yields_full_speed(self):
        # A config with temp_min == temp_max must not divide by zero, and must
        # err towards cooling rather than towards silence.
        self.assertEqual(fan.calc_pwm(40, 75, 255, 50, 50), 255)
        self.assertEqual(fan.calc_pwm(40, 75, 255, 60, 50), 255)

    def test_custom_window(self):
        self.assertEqual(fan.calc_pwm(40, 100, 200, 40, 60), 100)
        self.assertEqual(fan.calc_pwm(60, 100, 200, 40, 60), 200)
        self.assertEqual(fan.calc_pwm(50, 100, 200, 40, 60), 150)


class ClampTests(unittest.TestCase):
    def test_clamps_to_range(self):
        self.assertEqual(fan.clamp_pwm(-5), 0)
        self.assertEqual(fan.clamp_pwm(999), 255)
        self.assertEqual(fan.clamp_pwm(128), 128)

    def test_junk_becomes_the_safe_duty(self):
        # Failing towards full speed, never towards a stopped fan.
        self.assertEqual(fan.clamp_pwm(None), fan.SAFE_PWM)
        self.assertEqual(fan.clamp_pwm("fast"), fan.SAFE_PWM)


class NaturalSortTests(unittest.TestCase):
    def test_hwmon2_sorts_before_hwmon10(self):
        paths = ["/sys/class/hwmon/hwmon10", "/sys/class/hwmon/hwmon2"]
        self.assertEqual(sorted(paths, key=fan.natural_key),
                         ["/sys/class/hwmon/hwmon2", "/sys/class/hwmon/hwmon10"])


class CacheValidationTests(unittest.TestCase):
    def test_rejects_non_objects(self):
        for bad in ([], "x", 3, None):
            with self.assertRaises(fan.FanError):
                fan.validate_cache(bad)

    def test_rejects_missing_or_empty_channels(self):
        with self.assertRaises(fan.FanError):
            fan.validate_cache({"fans": {}})
        with self.assertRaises(fan.FanError):
            fan.validate_cache({"channels": []})

    def test_rejects_non_numeric_channels(self):
        with self.assertRaises(fan.FanError):
            fan.validate_cache({"channels": ["three"]})

    def test_duty_floor_is_enforced(self):
        # A cache claiming a zero floor would otherwise pin the fan off with
        # no kickstart path back.
        result = fan.validate_cache(
            {"channels": [3], "fans": {"3": {"minstop": 0, "minstart": 0}}})
        self.assertGreaterEqual(result["fans"][3]["minstop"], fan.PWM_FLOOR)
        self.assertGreaterEqual(result["fans"][3]["minstart"], fan.PWM_FLOOR)

    def test_minstart_never_below_minstop(self):
        result = fan.validate_cache(
            {"channels": [3], "fans": {"3": {"minstop": 200, "minstart": 90}}})
        self.assertGreaterEqual(result["fans"][3]["minstart"],
                                result["fans"][3]["minstop"])

    def test_missing_fan_entry_defaults_to_full_duty(self):
        result = fan.validate_cache({"channels": [3], "fans": {}})
        self.assertEqual(result["fans"][3]["minstop"], fan.SAFE_PWM)

    def test_out_of_range_values_are_clamped(self):
        result = fan.validate_cache(
            {"channels": [3], "fans": {"3": {"minstop": 9999, "minstart": -5}}})
        self.assertEqual(result["fans"][3]["minstop"], 255)

    def test_integer_keys_are_accepted(self):
        result = fan.validate_cache({"channels": [3], "fans": {3: {"minstop": 90}}})
        self.assertEqual(result["fans"][3]["minstop"], 90)

    def test_channels_are_deduplicated_and_sorted(self):
        result = fan.validate_cache({"channels": [3, 1, 3], "fans": {}})
        self.assertEqual(result["channels"], [1, 3])


class DegenerateCalibrationTests(unittest.TestCase):
    GOOD = {"max_rpm": 1500, "stall_duty": 60, "minstop": 80,
            "minstart": 90, "min_rpm": 400}

    def test_good_result_accepted(self):
        self.assertFalse(fan.calibration_is_degenerate(self.GOOD))

    def test_no_rotation_at_full_duty_rejected(self):
        self.assertTrue(fan.calibration_is_degenerate(dict(self.GOOD, max_rpm=0)))

    def test_never_stalled_but_pinned_at_max_rejected(self):
        self.assertTrue(fan.calibration_is_degenerate(dict(self.GOOD, minstop=255)))

    def test_missing_stall_measurement_rejected(self):
        self.assertTrue(fan.calibration_is_degenerate(dict(self.GOOD, stall_duty=None)))

    def test_no_minimum_rpm_rejected(self):
        self.assertTrue(fan.calibration_is_degenerate(dict(self.GOOD, min_rpm=0)))

    def test_empty_result_rejected(self):
        self.assertTrue(fan.calibration_is_degenerate({}))
        self.assertTrue(fan.calibration_is_degenerate(None))


class DeviceDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_single_device_is_resolved(self):
        make_chip(self.tmp.name, [3])
        found = fan.find_device(os.path.join(self.tmp.name, "f71882fg.*"))
        self.assertTrue(found.endswith("f71882fg.2592"))

    def test_no_device_raises_with_a_hint(self):
        with self.assertRaises(fan.FanError) as ctx:
            fan.find_device(os.path.join(self.tmp.name, "f71882fg.*"))
        self.assertIn("modprobe", str(ctx.exception))

    def test_ambiguous_devices_are_refused(self):
        write_sysfs_tree(self.tmp.name, {
            "f71882fg.2592/fan1_input": "0\n",
            "f71882fg.1024/fan1_input": "0\n",
        })
        with self.assertRaises(fan.FanError) as ctx:
            fan.find_device(os.path.join(self.tmp.name, "f71882fg.*"))
        self.assertIn("ambiguous", str(ctx.exception))


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Reference layout: three tachometers, only channel 3 turning.
        self.device = make_chip(self.tmp.name, [1, 2, 3], rpm=0, pwm=0, enable=2)
        self._write("fan3_input", "1022")
        self._write("pwm3", "68")
        self._write("pwm3_enable", "1")
        self.controller = fan.FanController(self.device)

    def _write(self, name, value):
        with open(os.path.join(self.device, name), "w", encoding="ascii") as fh:
            fh.write("%s\n" % value)

    def _read(self, name):
        with open(os.path.join(self.device, name), encoding="ascii") as fh:
            return fh.read().strip()

    def test_channels_are_discovered_from_the_chip(self):
        self.assertEqual(self.controller.channels(), [1, 2, 3])

    def test_passive_detection_finds_only_the_turning_channel(self):
        # Never hard-code channel 2: the reference unit uses channel 3.
        self.assertEqual(self.controller.detect_active(probe=False), [3])

    def test_passive_detection_writes_nothing(self):
        before = {n: self._read(n) for n in os.listdir(self.device)}
        self.controller.detect_active(probe=False)
        after = {n: self._read(n) for n in os.listdir(self.device)}
        self.assertEqual(before, after)

    def test_controllable_requires_both_registers(self):
        self.assertTrue(self.controller.controllable(3))
        os.unlink(os.path.join(self.device, "pwm2_enable"))
        self.assertFalse(self.controller.controllable(2))

    def test_set_pwm_clamps(self):
        self.controller.set_pwm(3, 9999)
        self.assertEqual(self._read("pwm3"), "255")

    def test_safe_state_forces_full_duty_and_automatic_mode(self):
        self.controller.set_manual(3)
        self.controller.set_pwm(3, 40)
        with quiet():
            self.controller.safe_state(reason="test")
        self.assertEqual(self._read("pwm3"), str(fan.SAFE_PWM))
        self.assertEqual(self._read("pwm3_enable"), str(fan.PWM_AUTOMATIC))

    def test_safe_state_without_any_touched_channel_is_a_no_op(self):
        with quiet():
            self.controller.safe_state(reason="test")
        self.assertEqual(self._read("pwm3"), "68")

    def test_restore_returns_the_previous_registers(self):
        self.controller.set_manual(3)
        self.controller.set_pwm(3, 250)
        self.controller.restore(3)
        self.assertEqual(self._read("pwm3"), "68")
        self.assertEqual(self._read("pwm3_enable"), "1")

    def test_borrowed_restores_on_exception(self):
        with self.assertRaises(ValueError):
            with self.controller.borrowed(3):
                self.controller.set_pwm(3, 10)
                raise ValueError("boom")
        self.assertEqual(self._read("pwm3"), "68")

    def test_borrowed_fails_safe_on_termination(self):
        # A stop signal during a probe must leave the fan cooling, not restored
        # to whatever low duty it happened to have.
        with quiet(), self.assertRaises(fan.Terminated):
            with self.controller.borrowed(3):
                self.controller.set_pwm(3, 10)
                raise fan.Terminated("signal SIGTERM")
        self.assertEqual(self._read("pwm3"), str(fan.SAFE_PWM))
        self.assertEqual(self._read("pwm3_enable"), str(fan.PWM_AUTOMATIC))

    def test_dry_run_never_writes(self):
        controller = fan.FanController(self.device, dry_run=True)
        controller.set_manual(3)
        controller.set_pwm(3, 255)
        with quiet():
            controller.safe_state(reason="test")
        self.assertEqual(self._read("pwm3"), "68")
        self.assertEqual(self._read("pwm3_enable"), "1")

    def test_write_failures_are_counted_per_channel_and_register(self):
        controller = fan.FanController(os.path.join(self.tmp.name, "gone"))
        with quiet():
            self.assertTrue(controller.set_pwm(3, 100) is False)
            self.assertEqual(controller.failure_count(3, "pwm3"), 1)
            controller.set_pwm(3, 100)
        self.assertEqual(controller.failure_count(3, "pwm3"), 2)
        # Not one number for the whole chip: a different channel, and a
        # different register on the same channel, each keep their own history.
        self.assertEqual(controller.failure_count(1, "pwm1"), 0)
        self.assertEqual(controller.failure_count(3, "pwm3_enable"), 0)

    def test_rpm_of_missing_channel_is_zero(self):
        self.assertEqual(self.controller.rpm(9), 0)


class TemperatureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _sensor(self, contents):
        path = os.path.join(self.tmp.name, "temp1_input")
        with open(path, "w", encoding="ascii") as fh:
            fh.write(contents)
        return path

    def test_normal_reading(self):
        self.assertEqual(fan.read_temp(self._sensor("41000\n")), 41)

    def test_missing_path_is_unknown_not_a_default(self):
        # Returning a comfortable default here is what previously let the fan
        # idle while the machine had no temperature signal at all.
        self.assertIsNone(fan.read_temp(None))
        self.assertIsNone(fan.read_temp("/nonexistent/temp1_input"))

    def test_empty_and_garbage_readings_are_unknown(self):
        self.assertIsNone(fan.read_temp(self._sensor("")))
        self.assertIsNone(fan.read_temp(self._sensor("warm\n")))

    def test_implausible_readings_are_rejected(self):
        self.assertIsNone(fan.read_temp(self._sensor("900000\n")))
        self.assertIsNone(fan.read_temp(self._sensor("-90000\n")))


class CacheFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "state", "fan-calibration.json")

    def test_round_trip(self):
        payload = {"version": 1, "channels": [3],
                   "fans": {"3": {"minstop": 90, "minstart": 100,
                                  "min_rpm": 400, "max_rpm": 1500}}}
        fan.save_cache(payload, self.path)
        loaded = fan.load_cache(self.path)
        self.assertEqual(loaded["channels"], [3])
        self.assertEqual(loaded["fans"][3]["minstop"], 90)

    def test_missing_file_is_none_not_an_error(self):
        self.assertIsNone(fan.load_cache(os.path.join(self.tmp.name, "absent")))

    def test_corrupt_file_raises(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        with self.assertRaises(fan.FanError):
            fan.load_cache(self.path)

    def test_save_leaves_no_temporary_files_behind(self):
        fan.save_cache({"version": 1, "channels": [3], "fans": {}}, self.path)
        leftovers = [n for n in os.listdir(os.path.dirname(self.path))
                     if n.startswith(".fan-calibration.")]
        self.assertEqual(leftovers, [])

    def test_saved_file_is_valid_json(self):
        fan.save_cache({"version": 1, "channels": [3], "fans": {}}, self.path)
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["channels"], [3])


class LockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "fancontrol.lock")

    def test_second_acquisition_is_refused(self):
        # Stops an interactive calibrate from fighting the service loop.
        first = fan.acquire_lock(self.path)
        self.addCleanup(first.close)
        with self.assertRaises(fan.FanError) as ctx:
            fan.acquire_lock(self.path)
        self.assertIn("another", str(ctx.exception))

    def test_lock_is_reusable_after_release(self):
        first = fan.acquire_lock(self.path)
        first.close()
        second = fan.acquire_lock(self.path)
        second.close()

    def test_the_lock_does_not_follow_the_cache_directory(self):
        # Reversed on purpose. The lock used to be placed beside the cache, so
        # `calibrate --cache /tmp/mine.json` took a lock in /tmp while the
        # service held one in /var/lib - two writers, one set of registers,
        # no contention. The lock names the chip now.
        device = "/sys/devices/platform/f71882fg.2592"
        paths = fan.writer_lock_paths(device, root="/run/qnap-tsx70")
        self.assertEqual(paths, ["/run/qnap-tsx70/fancontrol.lock",
                                 "/run/qnap-tsx70/fancontrol-f71882fg.2592.lock"])
        for cache in ("/var/lib/qnap-tsx70/fan-calibration.json",
                      "/tmp/mine.json", "/home/someone/cal.json"):
            self.assertEqual(
                fan.writer_lock_paths(device, root="/run/qnap-tsx70"), paths,
                "the lock moved with the cache %s" % cache)


class CliTests(unittest.TestCase):
    """Flags must be parsed, not substring-matched."""

    def test_subcommand_is_required(self):
        with quiet(), self.assertRaises(SystemExit):
            fan.build_parser().parse_args([])

    def test_unknown_flag_is_rejected(self):
        # The old parser silently accepted --calibrate-only and ignored it.
        with quiet(), self.assertRaises(SystemExit):
            fan.build_parser().parse_args(["calibrate", "--calibrate-only"])

    def test_all_subcommands_parse(self):
        parser = fan.build_parser()
        for argv in (["status"], ["status", "--json"], ["run"],
                     ["run", "--interval", "5"], ["calibrate", "--yes"],
                     ["calibrate", "--channels", "1", "3"], ["safe-state"]):
            self.assertTrue(hasattr(parser.parse_args(argv), "func"), argv)

    def test_calibrate_without_yes_does_not_set_the_flag(self):
        self.assertFalse(fan.build_parser().parse_args(["calibrate"]).yes)

    def test_dry_run_available_on_every_subcommand(self):
        parser = fan.build_parser()
        for command in ("status", "run", "calibrate", "safe-state"):
            self.assertTrue(parser.parse_args([command, "--dry-run"]).dry_run)


if __name__ == "__main__":
    unittest.main()
