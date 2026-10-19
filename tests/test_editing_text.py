import unittest
from pmkb.application.editing.text_edit import apply_widget_text


class TextEditTests(unittest.TestCase):
    def test_unchanged_widget_text_keeps_exact_original_line_endings(self):
        original = 'a\r\nb\rc\u2028d\u2029e\u00a0f'
        self.assertEqual(apply_widget_text(original, 'a\u2029b\u2029c\u2028d\u2029e\u00a0f'), original)

    def test_middle_edit_keeps_other_lines_and_unicode(self):
        original = '中文😀\r\nsecond\r\nlast\r\n'
        self.assertEqual(apply_widget_text(original, '中文😀\u2029second!\u2029last\u2029'),
                         '中文😀\r\nsecond!\r\nlast\r\n')

    def test_newline_insertion_and_deletion_do_not_split_surrogate_pairs(self):
        self.assertEqual(apply_widget_text('😀\r\nx', '😀x'), '😀x')
        self.assertEqual(apply_widget_text('😀x', '😀\u2029x'), '😀\nx')
