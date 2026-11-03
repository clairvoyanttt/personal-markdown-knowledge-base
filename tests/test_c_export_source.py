"""C's export source adapter: one consistent saved projection for D's exporter."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from threading import Event

from pmkb.application.export.rendering import ReferenceKind, render_export_markdown
from pmkb.contracts.collections import ActivationMode
from pmkb.contracts.common import CallContext
from pmkb.domain.identity import format_reference_marker, new_identifier
from pmkb.domain.text_spans import slice_utf16
from pmkb.infrastructure.storage.collection_catalog import LocalCollectionBackend
from pmkb.infrastructure.storage.connection import StorageFailure
from pmkb.infrastructure.storage.export_source import CExportSource
from pmkb.infrastructure.storage.store import CollectionStore


def new_collection(root: Path, name: str = "collection") -> tuple[Path, str]:
    directory = root / name
    directory.mkdir()
    backend = LocalCollectionBackend()
    prepared = backend.prepare(ActivationMode.CREATE, str(directory))
    backend.publish(prepared)
    return directory / "knowledge.sqlite", prepared.collection_id


class ExportSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.db, self.collection_id = new_collection(self.root)
        self.store = CollectionStore(self.db, self.collection_id)
        self.source = CExportSource(self.store)
        self.context = CallContext(self.collection_id, 1)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def save(self, *, note_id=None, name="Note", body="Body", tags=(), base_revision=None):
        return self.store.save(command_id=new_identifier(), operation_id=new_identifier(),
                               note_id=note_id or new_identifier(), name=name, body=body,
                               tags=tags, base_revision=base_revision)

    def read(self, note_id: str):
        return asyncio.run(self.source.read_saved(self.context, note_id, Event()))

    def test_valid_reference_projection_feeds_d_renderer(self) -> None:
        target = new_identifier()
        source = new_identifier()
        self.save(note_id=target, name="目标笔记", tags=("x",))
        marker = format_reference_marker(self.collection_id, target)
        saved = self.save(note_id=source, name="来源", body=f"看 {marker} 完")

        note = self.read(source)
        self.assertIsNotNone(note)
        self.assertEqual(note.context, self.context)
        self.assertEqual(note.note_id, source)
        self.assertEqual(note.saved_revision, 1)
        self.assertEqual(note.collection_revision, 3)
        self.assertEqual(note.saved_name, "来源")
        self.assertEqual(note.saved_body, f"看 {marker} 完")
        self.assertEqual(len(note.references), 1)
        reference = note.references[0]
        self.assertIs(reference.kind, ReferenceKind.VALID)
        self.assertEqual(reference.target_name, "目标笔记")
        self.assertEqual(reference.marker_text, marker)
        self.assertEqual(slice_utf16(note.saved_body, reference.start_utf16,
                                     reference.length_utf16), marker)
        rendered = render_export_markdown(note.saved_body, note.references)
        self.assertIn("内部引用：目标笔记（仅供识别，导出后不支持应用内跳转）", rendered)
        # D's own boundary validator must accept the projection unchanged.
        from pmkb.application.export.service import _projection

        self.assertIs(_projection(note, self.context, source), note)
        self.assertEqual(saved.committed_note.revision, 1)

    def test_deleted_missing_and_foreign_classifications(self) -> None:
        target = new_identifier()
        self.save(note_id=target, name="已删目标")
        self.store.delete(command_id=new_identifier(), operation_id=new_identifier(),
                          note_id=target, expected_revision=1)

        missing = format_reference_marker(self.collection_id, new_identifier())
        foreign = format_reference_marker("f" * 32, new_identifier())
        source = new_identifier()
        body = f"{missing}\n{foreign}"
        self.save(note_id=source, name="来源", body=body)

        note = self.read(source)
        self.assertEqual([item.kind for item in note.references],
                         [ReferenceKind.MISSING, ReferenceKind.FOREIGN_COLLECTION])
        self.assertIsNone(note.references[0].target_name)
        self.assertIsNone(note.references[1].target_name)
        rendered = render_export_markdown(note.saved_body, note.references)
        self.assertIn("目标标识不存在或不可用", rendered)
        self.assertIn("引用来自其他集合", rendered)

    def test_deleted_target_uses_historical_name(self) -> None:
        target = new_identifier()
        self.save(note_id=target, name="历史名称")
        marker = format_reference_marker(self.collection_id, target)
        source = new_identifier()
        self.save(note_id=source, name="来源", body=marker)
        self.store.delete(command_id=new_identifier(), operation_id=new_identifier(),
                          note_id=target, expected_revision=1)

        note = self.read(source)
        self.assertIs(note.references[0].kind, ReferenceKind.DELETED)
        self.assertEqual(note.references[0].target_name, "历史名称")
        self.assertIn("目标已删除", render_export_markdown(note.saved_body, note.references))

    def test_missing_saved_version_returns_none(self) -> None:
        self.assertIsNone(self.read(new_identifier()))

    def test_stale_context_is_rejected(self) -> None:
        other = CallContext(new_identifier(), 1)
        with self.assertRaises(StorageFailure) as error:
            asyncio.run(self.source.read_saved(other, new_identifier(), Event()))
        self.assertEqual(error.exception.code, "STALE_CONTEXT")


if __name__ == "__main__":
    unittest.main()
