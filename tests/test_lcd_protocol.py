"""Frame construction and button parsing - the bytes that reach the panel."""

import unittest

from helpers import load_lcd

lcd = load_lcd()


class PadLineTests(unittest.TestCase):
    def test_pads_short_text_to_16_bytes(self):
        self.assertEqual(lcd.pad_line("CPU"), b"CPU             ")
        self.assertEqual(len(lcd.pad_line("CPU")), 16)

    def test_empty_line_is_all_spaces(self):
        self.assertEqual(lcd.pad_line(""), b" " * 16)

    def test_truncates_long_text_to_16_bytes(self):
        self.assertEqual(lcd.pad_line("0123456789ABCDEFGHIJ"), b"0123456789ABCDEF")

    def test_exactly_16_is_unchanged(self):
        self.assertEqual(lcd.pad_line("0123456789ABCDEF"), b"0123456789ABCDEF")

    def test_non_ascii_is_replaced_not_expanded(self):
        # A multi-byte character must still cost exactly one column.
        frame = lcd.pad_line("café")
        self.assertEqual(len(frame), 16)
        self.assertEqual(frame[:4], b"caf?")

    def test_accepts_non_string_values(self):
        self.assertEqual(lcd.pad_line(42), b"42              ")


class FrameTests(unittest.TestCase):
    def test_line_one_frame(self):
        frame = lcd.frame_line(0, "Hello")
        self.assertEqual(frame[:4], b"\x4d\x0c\x00\x10")
        self.assertEqual(frame[4:], b"Hello           ")
        self.assertEqual(len(frame), 20)

    def test_line_two_frame(self):
        frame = lcd.frame_line(1, "World")
        self.assertEqual(frame[:4], b"\x4d\x0c\x01\x10")
        self.assertEqual(len(frame), 20)

    def test_invalid_row_is_rejected(self):
        with self.assertRaises(ValueError):
            lcd.frame_line(2, "x")

    def test_combined_frame_is_32_payload_bytes(self):
        frame = lcd.frame_both("a", "b")
        self.assertEqual(frame[:4], b"\x4d\x0c\x00\x20")
        self.assertEqual(len(frame), 36)

    def test_backlight_commands(self):
        self.assertEqual(lcd.frame_backlight(True), b"\x4d\x5e\x01")
        self.assertEqual(lcd.frame_backlight(False), b"\x4d\x5e\x00")

    def test_clear_and_stop_clock(self):
        self.assertEqual(lcd.frame_clear(), b"\x4d\x28")
        self.assertEqual(lcd.frame_stop_clock(), b"\x4d\x0d")


class ButtonParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = lcd.ButtonParser()

    def test_single_complete_frame(self):
        self.assertEqual(self.parser.feed(b"\x53\x05\x00\x01"), [lcd.BTN_ENTER])

    def test_multiple_frames_in_one_read(self):
        data = b"\x53\x05\x00\x01\x53\x05\x00\x02\x53\x05\x00\x00"
        self.assertEqual(self.parser.feed(data),
                         [lcd.BTN_ENTER, lcd.BTN_SELECT, lcd.BTN_RELEASE])

    def test_partial_frame_is_held_until_completed(self):
        # This is the regression the rewrite targets: a frame split across two
        # reads used to be dropped entirely.
        self.assertEqual(self.parser.feed(b"\x53\x05"), [])
        self.assertEqual(self.parser.feed(b"\x00\x02"), [lcd.BTN_SELECT])

    def test_frame_split_byte_by_byte(self):
        codes = []
        for byte in b"\x53\x05\x00\x01":
            codes.extend(self.parser.feed(bytes([byte])))
        self.assertEqual(codes, [lcd.BTN_ENTER])

    def test_leading_noise_is_discarded(self):
        self.assertEqual(self.parser.feed(b"\xff\x00\x53\x05\x00\x01"),
                         [lcd.BTN_ENTER])

    def test_trailing_partial_survives_across_feeds(self):
        self.assertEqual(self.parser.feed(b"\x53\x05\x00\x01\x53\x05"),
                         [lcd.BTN_ENTER])
        self.assertEqual(self.parser.pending, b"\x53\x05")
        self.assertEqual(self.parser.feed(b"\x00\x03"), [0x03])

    def test_buffer_does_not_grow_without_bound(self):
        for _ in range(100):
            self.parser.feed(b"\xaa" * 64)
        self.assertLessEqual(len(self.parser.pending), 2)

    def test_empty_read_is_harmless(self):
        self.assertEqual(self.parser.feed(b""), [])
        self.assertEqual(self.parser.pending, b"")

    def test_prefix_like_tail_is_retained(self):
        self.assertEqual(self.parser.feed(b"\x99\x53"), [])
        self.assertEqual(self.parser.pending, b"\x53")
        self.assertEqual(self.parser.feed(b"\x05\x00\x02"), [lcd.BTN_SELECT])


if __name__ == "__main__":
    unittest.main()
