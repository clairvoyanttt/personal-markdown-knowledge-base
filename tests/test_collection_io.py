from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from pmkb.contracts.collections import ActivationMode, TargetStage
from pmkb.application.collection.ports import CollectionFailure
from pmkb.infrastructure.storage.collection_catalog import LocalCollectionBackend
from pmkb.infrastructure.file_io.identity_registry import LocalIdentityRegistry

class CollectionIOTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / "集合"; self.target.mkdir()
        self.backend = LocalCollectionBackend()
        self.registry = LocalIdentityRegistry(self.root / "config/config.json")

    def test_prepare_is_private_and_publish_creates_real_valid_empty_collection(self):
        target = self.backend.prepare(ActivationMode.CREATE, str(self.target))
        self.assertFalse((self.target / "knowledge.sqlite").exists())
        self.assertEqual(self.backend.publish(target).stage, TargetStage.PUBLISHED_NEW)
        self.backend.validate(target, published=True)
        with closing(sqlite3.connect(self.target / "knowledge.sqlite")) as db:
            self.assertEqual(db.execute("SELECT collectionId, state FROM CollectionMeta").fetchone(), (target.collection_id, "READY"))
            self.assertEqual(db.execute("SELECT count(*) FROM Notes").fetchone()[0], 0)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertFalse(Path(target.temporary_file).exists())

    def test_cancel_cleanup_removes_only_owned_candidate(self):
        target = self.backend.prepare(ActivationMode.CREATE, str(self.target))
        other = self.target / "user.txt"; other.write_text("keep")
        self.backend.cleanup(target)
        self.assertEqual(other.read_text(), "keep")
        self.assertFalse(Path(target.temporary_file).exists())
        self.assertTrue(self.target.exists())

    def test_create_rejects_nonempty_target_without_modifying_file(self):
        f = self.target / "user.txt"; f.write_text("keep")
        with self.assertRaises(CollectionFailure) as caught:
            self.backend.prepare(ActivationMode.CREATE, str(self.target))
        self.assertEqual(caught.exception.code, "TARGET_NOT_EMPTY")
        self.assertEqual(f.read_text(), "keep")

    def test_publication_cannot_overwrite_file_created_after_prepare(self):
        target = self.backend.prepare(ActivationMode.CREATE, str(self.target))
        formal = self.target / "knowledge.sqlite"; formal.write_bytes(b"external")
        with self.assertRaises(CollectionFailure):
            self.backend.publish(target)
        self.backend.cleanup(target)
        self.assertEqual(formal.read_bytes(), b"external")

    def test_open_is_read_only_and_does_not_generate_another_collection(self):
        target = self.backend.prepare(ActivationMode.CREATE, str(self.target)); self.backend.publish(target)
        formal = self.target / "knowledge.sqlite"; before = formal.read_bytes()
        opened = self.backend.prepare(ActivationMode.OPEN, str(self.target))
        self.assertEqual(opened.collection_id, target.collection_id)
        self.assertIsNone(opened.temporary_file)
        self.assertEqual(formal.read_bytes(), before)

    def test_missing_and_future_schema_are_rejected_without_writes(self):
        with self.assertRaises(CollectionFailure):
            self.backend.prepare(ActivationMode.OPEN, str(self.target))
        f = self.target / "knowledge.sqlite"
        with closing(sqlite3.connect(f, autocommit=True)) as db:
            db.execute("CREATE TABLE CollectionMeta (singletonId, collectionId, schemaVersion, collectionRevision, state)")
            db.execute("INSERT INTO CollectionMeta VALUES (1, ?, 2, 1, 'READY')", ("a" * 32,))
        before = f.read_bytes()
        with self.assertRaises(CollectionFailure) as caught:
            self.backend.prepare(ActivationMode.OPEN, str(self.target))
        self.assertEqual(caught.exception.code, "SCHEMA_UNSUPPORTED")
        self.assertEqual(f.read_bytes(), before)

    def test_supported_version_with_missing_tables_is_invalid(self):
        f = self.target / "knowledge.sqlite"
        with closing(sqlite3.connect(f, autocommit=True)) as db:
            db.execute("CREATE TABLE CollectionMeta (singletonId, collectionId, schemaVersion, collectionRevision, state)")
            db.execute("INSERT INTO CollectionMeta VALUES (1, ?, 1, 1, 'READY')", ("a" * 32,))
        with self.assertRaises(CollectionFailure):
            self.backend.prepare(ActivationMode.OPEN, str(self.target))

    def test_registry_persists_and_same_location_is_idempotent(self):
        target = self.backend.prepare(ActivationMode.CREATE, str(self.target)); self.backend.publish(target)
        self.assertTrue(self.registry.register(target.collection_id, target.directory))
        self.assertFalse(self.registry.register(target.collection_id, target.directory))
        other_instance = LocalIdentityRegistry(self.root / "config/config.json")
        other_instance.check(target.collection_id, target.directory)
        data = json.loads((self.root / "config/config.json").read_text(encoding="utf-8"))
        self.assertEqual(data["collections"][target.collection_id], target.directory)
        self.assertNotIn("body", data)

    def test_corrupt_registry_is_not_replaced(self):
        config = self.root / "config/config.json"; config.parent.mkdir(); config.write_text("broken")
        with self.assertRaises(CollectionFailure):
            self.registry.register("a" * 32, str(self.target))
        self.assertEqual(config.read_text(), "broken")

    def test_known_independent_location_with_same_identity_is_rejected(self):
        target = self.backend.prepare(ActivationMode.CREATE, str(self.target)); self.backend.publish(target)
        self.registry.register(target.collection_id, target.directory)
        other = self.root / "other"; other.mkdir()
        (other / "knowledge.sqlite").write_bytes((self.target / "knowledge.sqlite").read_bytes())
        with self.assertRaises(CollectionFailure):
            self.registry.register(target.collection_id, str(other))
        self.registry.check(target.collection_id, target.directory)

    def test_matching_columns_without_required_relationships_are_rejected(self):
        target = self.backend.prepare(ActivationMode.CREATE, str(self.target)); self.backend.publish(target)
        database = self.target / "knowledge.sqlite"
        with closing(sqlite3.connect(database, autocommit=True)) as db:
            db.execute("DROP TABLE NoteTags")
            db.execute("CREATE TABLE NoteTags(noteId TEXT, tag TEXT)")
        before = database.read_bytes()
        with self.assertRaises(CollectionFailure):
            self.backend.prepare(ActivationMode.OPEN, str(self.target))
        self.assertEqual(database.read_bytes(), before)
