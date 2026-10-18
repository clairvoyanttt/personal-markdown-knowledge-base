import unittest
from pmkb.domain.text_spans import slice_utf16, to_utf16_offset

class Utf16SpanTests(unittest.TestCase):
    def test_emoji_prefix_uses_three_units_before_marker(self):
        self.assertEqual(to_utf16_offset("😀 [[note:x]]", 2), 3)
        self.assertEqual(slice_utf16("😀 [[note:x]]", 3, 10), "[[note:x]]")

    def test_ascii_chinese_roundtrip(self):
        self.assertEqual(to_utf16_offset("a中文", 3), 3)
        self.assertEqual(slice_utf16("a中文", 1, 2), "中文")

    def test_empty_slice_at_end_is_allowed(self):
        self.assertEqual(slice_utf16("😀", 2, 0), "")

    def test_split_surrogate_boundary_is_rejected(self):
        for start, length in [(1, 1), (0, 1), (1, 0)]:
            with self.assertRaises(ValueError):
                slice_utf16("😀x", start, length)

    def test_out_of_bounds_is_rejected(self):
        for start, length in [(-1, 1), (0, -1), (0, 9), (4, 0)]:
            with self.assertRaises(ValueError):
                slice_utf16("abc", start, length)
        for offset in [-1, 2]:
            with self.assertRaises(ValueError):
                to_utf16_offset("a", offset)

    def test_unpaired_surrogate_is_rejected_even_outside_requested_slice(self):
        with self.assertRaises(ValueError):
            slice_utf16("a\ud800", 0, 1)
        with self.assertRaises(ValueError):
            to_utf16_offset("a\ud800", 0)
