"""Combine D-only helpers, not application export/backup/restore acceptance."""
import hashlib
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import zipfile

from pmkb.application.export.naming import default_export_name
from pmkb.application.export.rendering import ExportReference, ReferenceKind, render_export_markdown
from pmkb.contracts.common import ResultStatus
from pmkb.domain.text_spans import to_utf16_offset
from pmkb.infrastructure.file_io.publication import publish_bytes
from pmkb.infrastructure.file_io.zip_guard import ArchivePolicy, ArchiveValidationError, inspect_zip


class IndependentFileWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.area = TemporaryDirectory()
        self.addCleanup(self.area.cleanup)
        self.directory = Path(self.area.name)

    def policy(self):
        # Explicit synthetic test budget/allowlist, NOT a confirmed backup format.
        names = frozenset({"manifest.json", "snapshot.sqlite"})
        return ArchivePolicy(names, names, max_archive_bytes=16384, max_entries=2,
                             max_entry_bytes=4096, max_total_uncompressed_bytes=8192,
                             max_capture_bytes=4096)

    def archive_bytes(self, entries):
        output = BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in entries:
                archive.writestr(name, data)
        return output.getvalue()

    def test_render_name_publish_preserves_saved_input_and_exact_other_bytes(self):
        marker = f"[[note:{'a' * 32}:{'b' * 32}]]"
        source = f"😀说明\r\n{marker}\r\n`{marker}`\r\n![图](../图片.png)\n"
        offset = source.index(marker)
        item = ExportReference(to_utf16_offset(source, offset), len(marker), marker,
                               ReferenceKind.VALID, "数据结构复习")
        transformed = render_export_markdown(source, (item,))
        expected = source.replace(marker, "内部引用：数据结构复习（仅供识别，导出后不支持应用内跳转）", 1)
        target = self.directory / default_export_name("课程/笔记")
        reply = publish_bytes(target, transformed.encode("utf-8"))
        self.assertEqual(reply.status, ResultStatus.OK)
        self.assertEqual(target.name, "note-课程_笔记.md")
        self.assertEqual(target.read_bytes(), expected.encode("utf-8"))
        self.assertEqual(source.count(marker), 2)  # Input was not rewritten.

    def test_different_titles_colliding_after_sanitization_do_not_overwrite(self):
        first = self.directory / default_export_name("课程/笔记")
        second = self.directory / default_export_name("课程\\笔记")
        self.assertEqual(first, second)
        self.assertEqual(publish_bytes(first, b"first").status, ResultStatus.OK)
        reply = publish_bytes(second, b"second")
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertEqual(reply.error.code, "TARGET_EXISTS")
        self.assertEqual(first.read_bytes(), b"first")
        self.assertEqual(list(self.directory.iterdir()), [first])

    def test_publish_and_inspect_synthetic_container_without_claiming_database_validity(self):
        manifest = b'{"purpose":"synthetic container, not a valid backup"}'
        fake_snapshot = b"synthetic bytes; deliberately NOT a SQLite database"
        payload = self.archive_bytes((("manifest.json", manifest), ("snapshot.sqlite", fake_snapshot)))
        target = self.directory / "synthetic.zip"
        self.assertEqual(publish_bytes(target, payload).status, ResultStatus.OK)
        report = inspect_zip(target, self.policy(), capture_entries=frozenset({"manifest.json"}),
                             expected_sha256={"snapshot.sqlite": hashlib.sha256(fake_snapshot).hexdigest()})
        entries = {entry.name: entry for entry in report.entries}
        self.assertEqual(entries["manifest.json"].captured_bytes, manifest)
        self.assertIsNone(entries["snapshot.sqlite"].captured_bytes)
        self.assertEqual(report.total_uncompressed_bytes, len(manifest) + len(fake_snapshot))
        self.assertEqual(target.read_bytes(), payload)
        self.assertEqual(list(self.directory.iterdir()), [target])

    def test_rejected_container_does_not_extract_or_mutate_any_file(self):
        payload = self.archive_bytes((("manifest.json", b"{}"), ("../snapshot.sqlite", b"unsafe")))
        target = self.directory / "unsafe.zip"
        self.assertEqual(publish_bytes(target, payload).status, ResultStatus.OK)
        before = hashlib.sha256(target.read_bytes()).hexdigest()
        with self.assertRaises(ArchiveValidationError):
            inspect_zip(target, self.policy())
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), before)
        self.assertEqual(list(self.directory.iterdir()), [target])


if __name__ == "__main__":
    unittest.main()
