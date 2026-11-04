"""B's reference/backlink panel contract against C's saved-version queries.

These tests exercise exactly the fields B's `saved_panel` renders
(`state`, `target_name`, `marker_text`, `reason`, `navigable`, `backlinks`) and
the "insert saved reference -> save -> backlink" loop. They do not modify B's or
D's modules; they pin the data C must supply.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pmkb.application.parsing import ReferenceState
from pmkb.application.query.service import SavedQueryService, SavedViewRequest
from pmkb.contracts.collections import ActivationMode
from pmkb.contracts.common import CallContext
from pmkb.domain.identity import format_reference_marker, new_identifier
from pmkb.infrastructure.storage.collection_catalog import LocalCollectionBackend
from pmkb.infrastructure.storage.store import CollectionStore


def new_collection(root: Path, name: str = "collection") -> tuple[Path, str]:
    directory = root / name
    directory.mkdir()
    backend = LocalCollectionBackend()
    prepared = backend.prepare(ActivationMode.CREATE, str(directory))
    backend.publish(prepared)
    return directory / "knowledge.sqlite", prepared.collection_id


class ReferenceUiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.db, self.collection_id = new_collection(self.root)
        self.store = CollectionStore(self.db, self.collection_id)
        self.service = SavedQueryService(self.store)
        self.context = CallContext(self.collection_id, 1)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def save(self, *, note_id=None, name="Note", body="Body", tags=(), base_revision=None):
        return self.store.save(command_id=new_identifier(), operation_id=new_identifier(),
                               note_id=note_id or new_identifier(), name=name, body=body,
                               tags=tags, base_revision=base_revision)

    def view(self, note_id: str):
        return self.service.saved_view(
            SavedViewRequest(self.context, new_identifier(), note_id))

    def test_panel_fields_for_all_reference_states(self) -> None:
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

        view = self.view(source)
        self.assertEqual([item.state for item in view.references],
                         [ReferenceState.VALID, ReferenceState.DELETED,
                          ReferenceState.MISSING, ReferenceState.FOREIGN_COLLECTION])
        self.assertEqual(view.references[0].target_name, "目标")
        self.assertEqual(view.references[1].target_name, "已删目标")
        self.assertIsNone(view.references[2].target_name)
        self.assertIsNone(view.references[3].target_name)
        self.assertEqual([item.navigable for item in view.references],
                         [True, False, False, False])
        for item in view.references:
            # Exactly how B's panel renders one row; must never raise.
            label = f"{item.state}: {item.target_name or item.marker_text}（{item.reason}）"
            self.assertTrue(label.strip())
            self.assertTrue(item.marker_text.startswith("[[note:"))

        target_view = self.view(target)
        self.assertEqual([(item.note_id, item.name) for item in target_view.backlinks],
                         [(source, "来源")])

    def test_inserted_marker_round_trips_to_a_navigable_reference(self) -> None:
        target = new_identifier()
        source = new_identifier()
        self.save(note_id=target, name="目标笔记")
        marker = format_reference_marker(self.collection_id, target)
        # B inserts via format_reference_marker, then the user saves manually.
        self.save(note_id=source, name="来源", body=f"前 {marker} 后")

        view = self.view(source)
        self.assertEqual(len(view.references), 1)
        self.assertIs(view.references[0].state, ReferenceState.VALID)
        self.assertTrue(view.references[0].navigable)
        self.assertEqual(view.references[0].target_note_id, target)
        self.assertEqual([item.note_id for item in self.store.backlinks(target)], [source])

    def test_cross_collection_marker_is_not_navigable_and_has_no_backlink(self) -> None:
        local = new_identifier()
        self.save(note_id=local, name="本地目标")
        foreign = format_reference_marker("f" * 32, new_identifier())
        self.save(name="来源", body=foreign)

        local_view = self.view(local)
        self.assertEqual(local_view.backlinks, ())
        source = [item for item in self.store.summaries() if item.name == "来源"][0]
        view = self.view(source.note_id)
        self.assertIs(view.references[0].state, ReferenceState.FOREIGN_COLLECTION)
        self.assertFalse(view.references[0].navigable)
        self.assertIsNone(view.references[0].target_name)

    def test_deleted_target_never_rebinds_after_same_name_recreate(self) -> None:
        target = new_identifier()
        source = new_identifier()
        self.save(note_id=target, name="历史名称")
        marker = format_reference_marker(self.collection_id, target)
        self.save(note_id=source, name="来源", body=marker)
        self.store.delete(command_id=new_identifier(), operation_id=new_identifier(),
                          note_id=target, expected_revision=1)
        replacement = new_identifier()
        self.save(note_id=replacement, name="历史名称")

        view = self.view(source)
        self.assertIs(view.references[0].state, ReferenceState.DELETED)
        self.assertEqual(view.references[0].target_name, "历史名称")
        self.assertFalse(view.references[0].navigable)
        self.assertEqual(self.view(replacement).backlinks, ())

    def test_unsaved_marker_is_not_visible_until_the_note_is_saved(self) -> None:
        target = new_identifier()
        source = new_identifier()
        self.save(note_id=target, name="目标")
        self.save(note_id=source, name="来源", body="还没有引用")

        # B's editor holds the inserted marker only in memory before save.
        self.assertEqual(self.view(source).references, ())
        self.assertEqual(self.store.backlinks(target), ())

        marker = format_reference_marker(self.collection_id, target)
        self.save(note_id=source, name="来源", body=f"现在有 {marker}", base_revision=1)
        self.assertEqual(len(self.view(source).references), 1)
        self.assertEqual([item.note_id for item in self.store.backlinks(target)], [source])

    def test_marker_inside_code_is_not_a_relationship(self) -> None:
        target = new_identifier()
        self.save(note_id=target, name="目标")
        marker = format_reference_marker(self.collection_id, target)
        source = new_identifier()
        self.save(note_id=source, name="代码", body=f"```\n{marker}\n```")
        self.assertEqual(self.view(source).references, ())
        self.assertEqual(self.store.backlinks(target), ())


if __name__ == "__main__":
    unittest.main()
