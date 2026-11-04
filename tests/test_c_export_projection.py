"""C's saved projection for D's single-note export (FR09 contract).

`CExportSource` is D-owned on main; this test only pins the data C must supply:
one consistent saved view mapped to D's `SavedExportNote`, which D's validator and
renderer must accept. It does not modify `export_source.py` or the export service.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from threading import Event

from pmkb.application.export.rendering import ReferenceKind, render_export_markdown
from pmkb.application.export.service import _projection
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


class ExportProjectionTests(unittest.TestCase):
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

    def test_all_four_kinds_map_to_fr09_text(self) -> None:
        target = new_identifier()
        deleted = new_identifier()
        source = new_identifier()
        self.save(note_id=target, name="目标")
        self.save(note_id=deleted, name="已删目标")
        self.store.delete(command_id=new_identifier(), operation_id=new_identifier(),
                          note_id=deleted, expected_revision=1)
        markers = (
            format_reference_marker(self.collection_id, target),
            format_reference_marker(self.collection_id, deleted),
            format_reference_marker(self.collection_id, new_identifier()),
            format_reference_marker("f" * 32, new_identifier()),
        )
        self.save(note_id=source, name="来源", body="\n".join(markers))

        note = self.read(source)
        self.assertIsNotNone(note)
        self.assertIs(_projection(note, self.context, source), note)
        self.assertEqual([item.kind for item in note.references],
                         [ReferenceKind.VALID, ReferenceKind.DELETED,
                          ReferenceKind.MISSING, ReferenceKind.FOREIGN_COLLECTION])
        self.assertEqual(note.saved_name, "来源")
        self.assertEqual(note.saved_revision, 1)
        self.assertEqual(note.collection_revision, self.store.metadata().collection_revision)
        for item in note.references:
            self.assertEqual(
                slice_utf16(note.saved_body, item.start_utf16, item.length_utf16),
                item.marker_text,
            )

        rendered = render_export_markdown(note.saved_body, note.references)
        self.assertIn("内部引用：目标（仅供识别，导出后不支持应用内跳转）", rendered)
        self.assertIn("失效内部引用：已删目标（目标已删除，无法跳转）", rendered)
        self.assertIn("失效内部引用：未知目标（目标标识不存在或不可用，无法跳转）", rendered)
        self.assertIn("失效内部引用：未知目标（引用来自其他集合，无法跳转）", rendered)

    def test_missing_saved_version_returns_none(self) -> None:
        self.assertIsNone(self.read(new_identifier()))

    def test_stale_context_is_rejected(self) -> None:
        with self.assertRaises(StorageFailure) as error:
            asyncio.run(self.source.read_saved(CallContext(new_identifier(), 1),
                                               new_identifier(), Event()))
        self.assertEqual(error.exception.code, "STALE_CONTEXT")

    def test_code_marker_is_not_exported_as_a_reference(self) -> None:
        target = new_identifier()
        self.save(note_id=target, name="目标")
        marker = format_reference_marker(self.collection_id, target)
        source = new_identifier()
        self.save(note_id=source, name="代码", body=f"```\n{marker}\n```")

        note = self.read(source)
        self.assertEqual(note.references, ())
        rendered = render_export_markdown(note.saved_body, note.references)
        self.assertIn(marker, rendered)
        self.assertNotIn("内部引用：", rendered)


if __name__ == "__main__":
    unittest.main()
