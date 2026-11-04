"""C core loop: saved list order, reopen consistency and storage close semantics.

These are the guarantees the minimal delivery depends on: a saved note can be
found in the list, read back after a full reopen, and searched by its saved
title/body. They use real temporary collections and real SQLite files.
"""
from __future__ import annotations

import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path
from threading import Event
from unittest import mock

from pmkb.application.editing.ports import SaveAttempt
from pmkb.application.parsing import ReferenceState, parse_markdown
from pmkb.contracts.collections import ActivationMode
from pmkb.contracts.common import CallContext
from pmkb.contracts.notes import DraftSnapshot
from pmkb.domain.identity import format_reference_marker, new_identifier
from pmkb.infrastructure.storage.collection_catalog import LocalCollectionBackend
from pmkb.infrastructure.storage.editing_storage import SqliteEditingStorage
from pmkb.infrastructure.storage.store import CollectionStore


def new_collection(root: Path, name: str = "collection") -> tuple[Path, str]:
    directory = root / name
    directory.mkdir()
    backend = LocalCollectionBackend()
    prepared = backend.prepare(ActivationMode.CREATE, str(directory))
    backend.publish(prepared)
    return directory / "knowledge.sqlite", prepared.collection_id


class CoreLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.db, self.collection_id = new_collection(self.root)
        self.store = CollectionStore(self.db, self.collection_id)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def save(self, *, note_id=None, name="Note", body="Body", tags=(), base_revision=None):
        return self.store.save(command_id=new_identifier(), operation_id=new_identifier(),
                               note_id=note_id or new_identifier(), name=name, body=body,
                               tags=tags, base_revision=base_revision)

    def reopen(self) -> CollectionStore:
        return CollectionStore(self.db, self.collection_id)

    def attempt(self, *, name="N", body="b") -> SaveAttempt:
        snapshot = DraftSnapshot(new_identifier(), None, None, 0, name, body, ())
        return SaveAttempt(CallContext(self.collection_id, 1), new_identifier(),
                           new_identifier(), new_identifier(), snapshot)

    def test_empty_collection_is_empty_not_an_error(self) -> None:
        self.assertEqual(self.store.summaries(), ())
        self.assertEqual(self.store.search(keyword="anything").items, ())
        self.assertIsNone(self.store.load_note(new_identifier()))
        self.assertIsNone(self.store.saved_view(new_identifier()))

    def test_saved_list_order_is_ascii_folded_then_identity(self) -> None:
        alpha = "0" * 31 + "1"
        alpha2 = "0" * 31 + "3"
        beta = "0" * 31 + "2"
        cjk = "0" * 31 + "4"
        self.save(note_id=beta, name="beta")
        self.save(note_id=cjk, name="笔记 1")
        self.save(note_id=alpha2, name="Alpha2")
        self.save(note_id=alpha, name="Alpha")

        # lower(name) folds ASCII only; noteId is the deterministic tie-break, but
        # names equal after folding are already rejected by the unique name key.
        self.assertEqual([item.note_id for item in self.store.summaries()],
                         [alpha, alpha2, beta, cjk])

    def test_reopen_keeps_saved_notes_and_search_results(self) -> None:
        first = new_identifier()
        second = new_identifier()
        self.save(note_id=first, name="数据结构", body="知识管理与复习")
        self.save(note_id=second, name="算法", body="排序与查找")

        reopened = self.reopen()
        self.assertEqual([item.note_id for item in reopened.summaries()],
                         [item.note_id for item in self.store.summaries()])
        before = self.store.search(keyword="知识管理")
        after = reopened.search(keyword="知识管理")
        self.assertEqual([item.note_id for item in after.items],
                         [item.note_id for item in before.items])
        self.assertEqual(after.collection_revision, before.collection_revision)

    def test_load_note_returns_exact_saved_text_after_reopen(self) -> None:
        note_id = new_identifier()
        body = "第一行\r\n第二行 😀 \u00a0 空格\n结尾"
        self.save(note_id=note_id, name="  原文  ", body=body, tags=("AI", "ai"))

        reopened = self.reopen()
        loaded = reopened.load_note(note_id)
        self.assertEqual(loaded.name, "原文")
        self.assertEqual(loaded.body, body)
        self.assertEqual(loaded.tags, ("AI", "ai"))
        self.assertEqual(loaded.revision, 1)

    def test_deleted_note_leaves_list_search_and_load(self) -> None:
        note_id = new_identifier()
        self.save(note_id=note_id, name="待删除", body="唯一关键词")
        self.store.delete(command_id=new_identifier(), operation_id=new_identifier(),
                          note_id=note_id, expected_revision=1)

        reopened = self.reopen()
        self.assertEqual(reopened.summaries(), ())
        self.assertEqual(reopened.search(keyword="唯一关键词").items, ())
        self.assertIsNone(reopened.load_note(note_id))
        self.assertIsNone(reopened.saved_view(note_id))

    def test_saved_view_relations_survive_reopen(self) -> None:
        target = new_identifier()
        source = new_identifier()
        self.save(note_id=target, name="目标")
        marker = format_reference_marker(self.collection_id, target)
        self.save(note_id=source, name="来源", body=f"看 {marker}")

        view = self.reopen().saved_view(source)
        self.assertEqual(view.revision, 1)
        self.assertEqual(len(view.references), 1)
        self.assertIs(view.references[0].state, ReferenceState.VALID)
        self.assertEqual(view.references[0].target_name, "目标")
        self.assertEqual([item.note_id for item in view.backlinks], [])
        self.assertEqual(len(self.reopen().saved_view(target).backlinks), 1)

    def test_close_waits_for_inflight_save_and_then_refuses_new_work(self) -> None:
        adapter = SqliteEditingStorage(self.store)
        real_save = self.store.save
        results = []

        def slow_save(**kwargs):
            time.sleep(0.4)
            return real_save(**kwargs)

        def run_save() -> None:
            results.append(asyncio.run(adapter.save(self.attempt(name="关闭中"), Event())))

        with mock.patch.object(self.store, "save", slow_save):
            worker = threading.Thread(target=run_save)
            worker.start()
            time.sleep(0.1)
            started = time.monotonic()
            adapter.close()
            elapsed = time.monotonic() - started
            worker.join(timeout=10)

        self.assertFalse(worker.is_alive())
        self.assertGreaterEqual(elapsed, 0.2, "close returned before in-flight work finished")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].state.value, "COMMITTED")
        self.assertEqual(len(self.store.summaries()), 1)
        with self.assertRaises(RuntimeError):
            asyncio.run(adapter.save(self.attempt(), Event()))

    def test_aclose_is_awaitable_and_idempotent(self) -> None:
        adapter = SqliteEditingStorage(self.store)
        asyncio.run(adapter.aclose())
        asyncio.run(adapter.aclose())
        with self.assertRaises(RuntimeError):
            asyncio.run(adapter.load_note(CallContext(self.collection_id, 1),
                                          new_identifier(), Event()))


if __name__ == "__main__":
    unittest.main()
