"""Regressions from the official-QTS fan investigation; no device IO."""
import contextlib
import io
import json
import os
import tempfile
import types
import unittest
from unittest import mock

from helpers import load_fan, write_sysfs_tree

fan = load_fan()


class DriverAbiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.device = os.path.join(self.tmp.name, 'f71882fg.2592')
        write_sysfs_tree(self.device, {'fan2_input': '1400', 'fan2_full_speed': '1500',
                                    'pwm2': '160', 'pwm2_enable': '2'})
        self.controller = fan.FanController(self.device)
        self.addCleanup(fan.reset_handback_record)

    def test_safe_state_auto_does_not_write_rejected_duty(self):
        real_write = fan.write_sysfs
        calls = []

        def write(path, value):
            name = os.path.basename(path)
            calls.append((name, value))
            if name == 'pwm2' and self.controller.enable_mode(2) == 2:
                return False  # f71882fg returns EROFS in automatic mode.
            return real_write(path, value)

        with mock.patch.object(fan, 'write_sysfs', side_effect=write), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(self.controller.safe_state([2]))
        self.assertEqual(calls, [('pwm2_enable', 2)])
        self.assertEqual(self.controller.pwm(2), 160)

    def test_safe_state_manual_still_applies_full_duty_before_auto(self):
        fan.write_sysfs(os.path.join(self.device, 'pwm2_enable'), 1)
        with mock.patch.object(fan, 'write_sysfs', wraps=fan.write_sysfs) as write, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(self.controller.safe_state([2]))
        self.assertEqual([(os.path.basename(c.args[0]), c.args[1])
                          for c in write.call_args_list], [('pwm2', 255), ('pwm2_enable', 2)])

    def test_failed_mode_write_in_auto_is_not_hidden(self):
        with mock.patch.object(fan, 'write_sysfs', return_value=False), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(self.controller.safe_state([2]))

    def rpm_write(self, path, value):
        # Independent transcription of upstream's integer RPM target ABI.
        if os.path.basename(path) == 'pwm2':
            full = fan.read_int(os.path.join(self.device, 'fan2_full_speed'))
            target_rpm = int(value) * full // 255
            reg = 1500000 // target_rpm if target_rpm else 0
            actual_rpm = 1500000 // reg if reg else 0
            value = 255 * actual_rpm // full
        return self.real_write(path, value)

    def test_documented_rpm_roundtrip_is_accepted_without_generic_tolerance(self):
        self.real_write = fan.write_sysfs
        with mock.patch.object(fan, 'write_sysfs', side_effect=self.rpm_write), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(self.controller.set_pwm(2, 100))
            self.assertEqual(self.controller.pwm(2), 99)
            self.assertTrue(self.controller.set_pwm(2, 120))
            self.assertEqual(self.controller.pwm(2), 119)

    def test_arbitrary_nearby_value_is_still_rejected(self):
        real = fan.write_sysfs
        def wrong(path, value):
            return real(path, value - 2)
        with mock.patch.object(fan, 'write_sysfs', side_effect=wrong), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(self.controller.set_pwm(2, 100))

    def test_no_full_speed_does_not_enable_rounding_acceptance(self):
        os.unlink(os.path.join(self.device, 'fan2_full_speed'))
        real = fan.write_sysfs
        with mock.patch.object(fan, 'write_sysfs', side_effect=lambda p, v: real(p, v - 1)), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(self.controller.set_pwm(2, 100))

    def test_changing_full_speed_invalidates_rounding_acceptance(self):
        self.real_write = fan.write_sysfs
        def change(path, value):
            answer = self.rpm_write(path, value)
            self.real_write(os.path.join(self.device, 'fan2_full_speed'), 1800)
            return answer
        with mock.patch.object(fan, 'write_sysfs', side_effect=change), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(self.controller.set_pwm(2, 100))

    def test_manual_mode_register_has_no_rounding_exception(self):
        with mock.patch.object(fan, 'write_sysfs', return_value=True), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(self.controller.set_manual(2))

    def test_status_preserves_unknown_rpm_and_reports_full_speed(self):
        with open(os.path.join(self.device, 'fan2_input'), 'w') as f:
            f.write('unavailable')
        args = types.SimpleNamespace(device_glob=self.device, sensor='not-a-real-sensor',
                                     cache=os.path.join(self.tmp.name, 'absent.json'), json=True)
        out = io.StringIO()
        with mock.patch.object(fan, 'write_sysfs', side_effect=AssertionError('status wrote')), \
                mock.patch.object(fan, 'find_temp_input', return_value=None), \
                contextlib.redirect_stdout(out):
            self.assertEqual(fan.cmd_status(args), 0)
        report = json.loads(out.getvalue())
        self.assertIsNone(report['channels']['2']['rpm'])
        self.assertEqual(report['channels']['2']['full_speed_rpm'], 1500)
        self.assertEqual(report['active_channels'], [])

    def test_safe_state_log_does_not_claim_physical_verification(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertTrue(self.controller.safe_state([2]))
        self.assertIn('driver', out.getvalue())
        self.assertIn('physical effect untested', out.getvalue())

    def test_restore_of_already_auto_channel_does_not_write_duty(self):
        self.controller.remember(2)
        real_write = fan.write_sysfs
        def write(path, value):
            if os.path.basename(path) == 'pwm2':
                self.fail('duty write while already automatic')
            return real_write(path, value)
        with mock.patch.object(fan, 'write_sysfs', side_effect=write):
            self.assertTrue(self.controller.restore(2))


class RpmRoundtripTests(unittest.TestCase):
    def test_invalid_scale_or_input_is_not_a_match(self):
        for full in (None, 0, -1, True, 1.5, '1500', 1500001):
            self.assertIsNone(fan.rpm_target_readback(100, full))
        for duty in (-1, 256, True, 1.5, '100'):
            self.assertIsNone(fan.rpm_target_readback(duty, 1500))

    def test_overflowing_or_zero_rpm_target_is_rejected(self):
        self.assertIsNone(fan.rpm_target_readback(1, 1))
        self.assertIsNone(fan.rpm_target_readback(1, 1500))

    def test_endpoints_and_known_integer_roundtrips(self):
        self.assertEqual(fan.rpm_target_readback(0, 1500), 0)
        self.assertEqual(fan.rpm_target_readback(255, 1500), 255)
        self.assertEqual(fan.rpm_target_readback(100, 1500), 99)
        self.assertEqual(fan.rpm_target_readback(120, 1500), 119)


if __name__ == '__main__':
    unittest.main()
