"""Group 12: what the diagnostic redactor must and must not remove.

The old filter was one sed expression. It could not see a compressed IPv6
address, because the pattern that finds `fe80::1` also finds `22:54:01`.
"""

import subprocess
import unittest

from helpers import REPO_ROOT, load_script


class RedactorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.redact = load_script("scripts/redact.py", "qnap_tsx70_redact")

    def line(self, text, hostnames=()):
        return self.redact.redact_line(text, hostnames)

    def assertRedacted(self, text, placeholder):
        result = self.line(text)
        self.assertIn(placeholder, result,
                      "%r survived redaction as %r" % (text, result))

    def assertPreserved(self, text):
        self.assertEqual(self.line(text), text,
                         "%r was corrupted by redaction" % text)


class IPv6Tests(RedactorTests):
    CASES = (
        "::1",
        "fe80::1",
        "2001:db8::1",
        "2001:0db8:0000:0000:0000:0000:0000:0001",
        "2001:db8::/32",
        "fe80::1%eth0",
        "fe80::1%2",
        "::ffff:192.0.2.1",
        "fd00:1234:5678:9abc::42",
        "2001:db8:0:0:1:0:0:1",
    )

    def test_every_form_is_redacted(self):
        for address in self.CASES:
            with self.subTest(address=address):
                self.assertRedacted("addr %s end" % address, "[ipv6]")
                self.assertNotIn(address, self.line("addr %s end" % address))

    def test_an_address_followed_by_a_colon_keeps_its_punctuation(self):
        self.assertEqual(self.line("peer fe80::1: down"), "peer [ipv6]: down")

    def test_the_zone_id_goes_with_the_address(self):
        self.assertNotIn("eth0", self.line("link fe80::1%eth0 up"))


class IPv4AndFriendsTests(RedactorTests):
    def test_ipv4_and_cidr(self):
        self.assertEqual(self.line("ip 192.0.2.42 end"), "ip [ip] end")
        self.assertEqual(self.line("net 198.51.100.0/24 end"), "net [ip] end")

    def test_mac_addresses(self):
        self.assertEqual(self.line("mac 00:11:22:33:44:55 end"),
                         "mac [mac] end")

    def test_uuids(self):
        self.assertRedacted("uuid 123e4567-e89b-12d3-a456-426614174000", "[uuid]")

    def test_serial_lines_are_replaced_whole(self):
        self.assertEqual(self.line("Serial Number:    WD-WCC4N1234567"),
                         "[serial removed]")
        self.assertEqual(self.line("LU WWN Device Id: 5 000c50 0a1b2c3d4"),
                         "[serial removed]")

    def test_tagged_identifiers(self):
        self.assertEqual(self.line("PARTUUID=0a1b2c3d-01 rest"),
                         "PARTUUID=[redacted] rest")
        self.assertEqual(self.line('  "serial_number": "ABC123",'),
                         '  "serial_number": "[serial removed]",')

    def test_hostnames_are_word_anchored(self):
        self.assertEqual(self.line("host nas1 here", ["nas1"]),
                         "host [hostname] here")
        self.assertEqual(self.line("model nas1000 here", ["nas1"]),
                         "model nas1000 here")


class PreservationTests(RedactorTests):
    PRESERVED = (
        "Active: active (running) since Fri 2026-09-12 22:54:01 UTC; 1min ago",
        "===== Serial ports =====",
        "enabled: enabled",
        "kernel: 6.1.0-13-amd64",
        "stty: speed 1200 baud; line = 0;",
        "  fan1 rpm=1200   pwm=128   enable=2",
        "configured port: /dev/ttyS1",
        "12:34:56",
        "0:0:0:0",
        "python3: Python 3.11.2",
    )

    def test_ordinary_text_survives(self):
        for text in self.PRESERVED:
            with self.subTest(text=text):
                self.assertPreserved(text)


class FilterProcessTests(unittest.TestCase):
    """The filter is what diagnose.sh actually pipes through."""

    def test_it_reads_stdin_and_writes_stdout(self):
        result = subprocess.run(
            ["python3", "scripts/redact.py", "--hostname", "nas1"],
            input="nas1 has fe80::1 and 192.0.2.7\nplain line\n",
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout,
                         "[hostname] has [ipv6] and [ip]\nplain line\n")

    def test_undecodable_bytes_do_not_stop_it(self):
        result = subprocess.run(["python3", "scripts/redact.py"],
                                input=b"\xff\xfe binary 203.0.113.9\n",
                                cwd=REPO_ROOT, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"[ip]", result.stdout)


if __name__ == "__main__":
    unittest.main()
