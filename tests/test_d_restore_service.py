"""D restore orchestration with explicit C/A substitutes, NOT SQLite acceptance."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from pmkb.application.output_support import OutputFailure, OutputSessionState
from pmkb.application.backup_restore.manifest import BackupManifest, ManifestPolicy, ReferenceCounts, SnapshotFacts
from pmkb.application.backup_restore.package_components import publish_package
from pmkb.application.backup_restore.restore_service import (
    InitializedRestore, RestorePublication, RestoreReply, RestoreRequest,
    RestoreService, RestoreStage, RestoreTarget,
)
from pmkb.contracts.common import AppError, CallContext, ResultStatus
from pmkb.infrastructure.file_io.publication import publish_bytes
from pmkb.infrastructure.file_io.zip_guard import ArchivePolicy


SOURCE = "a" * 32
NEW = "b" * 32
CONTEXT = CallContext("c" * 32, 2)


class Session:
    def __init__(self, context=CONTEXT):
        self.state = OutputSessionState(context, ())

    async def inspect(self):
        return self.state


class StorageSubstitute:
    """Supplies configured synthetic assertions; never validates SQLite."""
    def __init__(self, manifest):
        self.manifest = manifest
        self.facts_override = self.init_override = self.failure = None
        self.validations = self.initializations = self.releases = 0
        self.on_init = None

    async def validate_snapshot(self, candidate, cancel):
        self.validations += 1
        m = self.manifest
        return self.facts_override or SnapshotFacts(m.schema_version, m.source_collection_id,
            m.source_collection_revision, m.saved_note_count, m.reference_counts,
            m.snapshot_sha256, m.snapshot_size_bytes)

    async def initialize_restore(self, candidate, target, cancel):
        self.initializations += 1
        if self.on_init:
            self.on_init(candidate)
        if self.failure:
            raise self.failure
        return self.init_override or InitializedRestore("init-token", target.token, target.collection_id,
            self.manifest.saved_note_count, self.manifest.saved_note_count,
            self.manifest.snapshot_sha256, 1)

    async def release(self, initialized):
        self.releases += 1
        return ()


class TargetSubstitute:
    """Publishes labelled synthetic bytes, not an initialized collection DB."""
    def __init__(self):
        self.prepares = self.publications = 0
        self.releases = []
        self.new_id = NEW
        self.failure = self.reply_override = None
        self.on_publish = None
        self.release_warning = False

    async def prepare(self, destination, source_id, cancel):
        self.prepares += 1
        return RestoreTarget("target-token", str(destination), self.new_id)

    async def publish(self, target, initialized, cancel):
        self.publications += 1
        if self.on_publish:
            self.on_publish()
        if self.failure:
            raise self.failure
        if self.reply_override:
            return self.reply_override
        receipt = publish_bytes(Path(target.directory) / "knowledge.sqlite",
                                b"SYNTHETIC TEST OUTPUT - NOT SQLITE", cancel)
        return RestorePublication(receipt.status, target.directory, target.collection_id,
                                  receipt.error, receipt.warnings)

    async def release(self, target, published):
        self.releases.append(published)
        if self.release_warning:
            raise OSError("injected cleanup failure")
        return ()


class RestoreServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.destination = self.root / "restored"
        self.destination.mkdir()
        self.source = self.root / "source.zip"
        body = "仅用于编排测试，不是SQLite数据库".encode()
        self.manifest = BackupManifest(1, 1, "snapshot.sqlite", SOURCE, r"Z:\unavailable\source",
            datetime(2026, 10, 18, 4, 5, 6, 123456, timezone.utc), 7, 2,
            ReferenceCounts(1, 1, 1, 1), hashlib.sha256(body).hexdigest(), len(body))
        self.mp = ManifestPolicy(frozenset({1}), frozenset({1}), 8192)
        self.ap = ArchivePolicy(frozenset({"manifest.json", "snapshot.sqlite"}),
            frozenset({"manifest.json", "snapshot.sqlite"}), 1048576, 2, 524288, 1048576, 8192)
        self.assertEqual(publish_package(self.source, body, self.manifest, self.mp, self.ap).status,
                         ResultStatus.OK)
        self.original = self.source.read_bytes()
        self.session = Session()
        self.storage = StorageSubstitute(self.manifest)
        self.targets = TargetSubstitute()
        self.service = RestoreService(self.session, self.storage, self.targets, self.work, self.mp, self.ap)
        self.request = RestoreRequest(CONTEXT, str(self.source), str(self.destination))

    async def asyncTearDown(self):
        self.temp.cleanup()

    def check_original(self):
        self.assertEqual(self.source.read_bytes(), self.original)

    async def test_complete_d_flow_reports_only_substitute_publication_and_never_activates(self):
        reply = await self.service.restore_backup(self.request)
        self.assertEqual(reply.status, ResultStatus.OK)
        self.assertIsInstance(reply.value, RestoreReply)
        self.assertEqual(reply.value.stage, RestoreStage.PUBLISHED)
        self.assertEqual(reply.value.collection_id, NEW)
        self.assertEqual(reply.value.saved_note_count, 2)
        self.assertEqual(reply.delivery_context, CONTEXT)
        self.assertEqual(self.session.state.context, CONTEXT)
        self.assertEqual(self.targets.releases, [True])
        self.assertEqual(self.storage.releases, 1)
        self.assertEqual(list(self.work.iterdir()), [])
        self.check_original()

    async def test_restore_is_allowed_without_an_active_collection(self):
        context = CallContext(None, 0)
        self.session.state = OutputSessionState(context, ())
        reply = await self.service.restore_backup(replace(self.request, context=context))
        self.assertEqual(reply.status, ResultStatus.OK)
        self.assertEqual(reply.delivery_context, context)

    async def test_nonempty_target_is_rejected_without_changing_existing_content(self):
        existing = self.destination / "keep.txt"
        existing.write_text("keep", encoding="utf-8")
        reply = await self.service.restore_backup(self.request)
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertEqual(reply.error.code, "TARGET_NOT_EMPTY")
        self.assertEqual(existing.read_text(encoding="utf-8"), "keep")
        self.assertEqual(self.storage.initializations, 0)
        self.assertEqual(self.targets.publications, 0)

    async def test_work_directory_cannot_be_inside_restore_target(self):
        self.service.work_parent = self.destination
        reply = await self.service.restore_backup(self.request)
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertEqual(list(self.destination.iterdir()), [])

    async def test_relative_paths_and_invalid_context_are_rejected_before_ports(self):
        for request in (replace(self.request, source_file="relative.zip"),
                        replace(self.request, destination_directory="relative"),
                        replace(self.request, context=CallContext(SOURCE, True))):
            with self.subTest(request=request):
                reply = await self.service.restore_backup(request)
                self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertEqual(self.targets.prepares, 0)

    async def test_pending_save_blocks_without_discarding_session(self):
        self.session.state = replace(self.session.state, has_unconfirmed_save=True)
        reply = await self.service.restore_backup(self.request)
        self.assertEqual(reply.error.code, "STATE_BLOCKED")
        self.assertEqual(self.storage.validations, 0)

    async def test_invalid_zip_is_not_given_to_storage_or_targets(self):
        self.source.write_bytes(b"not a zip")
        reply = await self.service.restore_backup(self.request)
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertEqual(reply.error.code, "INVALID_BACKUP")
        self.assertEqual(self.storage.validations, 0)
        self.assertEqual(self.targets.prepares, 0)
        self.assertEqual(list(self.work.iterdir()), [])

    async def test_independent_facts_mismatch_stops_before_initialization(self):
        m = self.manifest
        self.storage.facts_override = SnapshotFacts(1, SOURCE, 7, 3, m.reference_counts,
                                                    m.snapshot_sha256, m.snapshot_size_bytes)
        reply = await self.service.restore_backup(self.request)
        self.assertEqual(reply.error.code, "INVALID_BACKUP")
        self.assertEqual(self.targets.prepares, 0)
        self.assertEqual(list(self.work.iterdir()), [])

    async def test_target_identity_must_be_new_and_destination_bound(self):
        self.targets.new_id = SOURCE
        reply = await self.service.restore_backup(self.request)
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertEqual(self.storage.initializations, 0)
        self.assertEqual(self.targets.releases, [False])

    async def test_invalid_initialization_receipt_count_or_binding_never_publishes(self):
        good = InitializedRestore("init-token", "target-token", NEW, 2, 2,
                                  self.manifest.snapshot_sha256, 1)
        for initialized in (replace(good, initialized_note_count=1),
                            replace(good, target_token="other"),
                            replace(good, collection_revision=2),
                            replace(good, saved_note_count=True),
                            replace(good, source_snapshot_sha256="d" * 64)):
            with self.subTest(initialized=initialized):
                self.storage.init_override = initialized
                reply = await self.service.restore_backup(self.request)
                self.assertEqual(reply.status, ResultStatus.ERROR)
                self.assertEqual(self.targets.publications, 0)
        self.check_original()

    async def test_snapshot_mutation_by_initializer_is_detected_before_publication(self):
        self.storage.on_init = lambda candidate: candidate.snapshot_path.write_bytes(b"changed")
        reply = await self.service.restore_backup(self.request)
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertEqual(self.targets.publications, 0)
        self.check_original()

    async def test_known_initialization_failure_cleans_candidates_and_preserves_target(self):
        self.storage.failure = OutputFailure("INVALID_BACKUP", "rejected synthetic snapshot")
        reply = await self.service.restore_backup(self.request)
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertEqual(reply.error.code, "INVALID_BACKUP")
        self.assertEqual(list(self.work.iterdir()), [])
        self.assertEqual(list(self.destination.iterdir()), [])

    async def test_unknown_initialization_retains_candidates_without_claiming_publication(self):
        self.storage.failure = OutputFailure("IO_FAILURE", "lost initialization receipt", unknown=True)
        reply = await self.service.restore_backup(self.request)
        self.assertEqual(reply.status, ResultStatus.UNCONFIRMED)
        self.assertEqual(reply.value.stage, RestoreStage.INITIALIZATION_UNKNOWN)
        self.assertEqual(self.targets.publications, 0)
        self.assertTrue(Path(reply.value.candidate_path).is_file())
        self.assertEqual(self.targets.releases, [])
        self.check_original()

    async def test_lost_publication_receipt_is_unknown_and_never_retried_or_cleaned(self):
        self.targets.failure = OSError("receipt lost")
        reply = await self.service.restore_backup(self.request)
        self.assertEqual(reply.status, ResultStatus.UNCONFIRMED)
        self.assertEqual(reply.value.stage, RestoreStage.PUBLICATION_UNKNOWN)
        self.assertEqual(self.targets.publications, 1)
        self.assertEqual(self.targets.releases, [])
        self.assertEqual(self.storage.releases, 0)
        self.assertTrue(Path(reply.value.candidate_path).is_file())

    async def test_explicit_unknown_publication_receipt_preserves_candidate(self):
        self.targets.reply_override = RestorePublication(ResultStatus.UNCONFIRMED,
            str(self.destination), NEW, AppError("IO_FAILURE", "unknown", "inspect"))
        reply = await self.service.restore_backup(self.request)
        self.assertEqual(reply.status, ResultStatus.UNCONFIRMED)
        self.assertEqual(self.targets.releases, [])

    async def test_preparation_reply_loss_retains_input_without_fabricating_new_id(self):
        async def lost(destination, source_id, cancel):
            raise OSError("target preparation lost its reply")

        self.targets.prepare = lost
        reply = await self.service.restore_backup(self.request)
        self.assertEqual(reply.status, ResultStatus.UNCONFIRMED)
        self.assertEqual(reply.value.stage, RestoreStage.PREPARATION_UNKNOWN)
        self.assertIsNone(reply.value.collection_id)
        self.assertEqual(self.storage.initializations, 0)
        self.assertTrue(Path(reply.value.candidate_path).is_file())

    async def test_definite_publish_error_preserves_concurrently_created_target_file(self):
        existing = self.destination / "keep.txt"
        self.storage.on_init = lambda candidate: existing.write_text("keep", encoding="utf-8")
        self.targets.reply_override = RestorePublication(ResultStatus.ERROR, str(self.destination), NEW,
            AppError("TARGET_NOT_EMPTY", "target changed", "choose empty"))
        reply = await self.service.restore_backup(self.request)
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertEqual(reply.error.code, "TARGET_NOT_EMPTY")
        self.assertEqual(existing.read_text(encoding="utf-8"), "keep")
        self.assertEqual(self.targets.releases, [False])
        self.assertEqual(self.storage.releases, 1)
        self.assertEqual(list(self.work.iterdir()), [])

    async def test_malformed_publication_reply_is_unknown_not_cleaned_as_failed(self):
        self.targets.reply_override = RestorePublication(ResultStatus.OK, str(self.destination), SOURCE)
        reply = await self.service.restore_backup(self.request)
        self.assertEqual(reply.status, ResultStatus.UNCONFIRMED)
        self.assertEqual(self.targets.releases, [])

    async def test_string_publication_status_is_never_mistaken_for_known_failure(self):
        for status in ("OK", "ERROR", "CANCELLED", "UNCONFIRMED"):
            with self.subTest(status=status):
                self.targets.reply_override = RestorePublication(status, str(self.destination), NEW,
                    AppError("IO_FAILURE", "untrusted receipt", "inspect") if status in {"ERROR", "UNCONFIRMED"} else None)
                reply = await self.service.restore_backup(self.request)
                self.assertEqual(reply.status, ResultStatus.UNCONFIRMED)
                self.assertEqual(reply.value.stage, RestoreStage.PUBLICATION_UNKNOWN)
                self.assertEqual(self.targets.releases, [])
                self.assertEqual(self.storage.releases, 0)
                self.assertTrue(Path(reply.value.candidate_path).is_file())

    async def test_cancelled_receipt_with_error_is_not_accepted_as_no_effect(self):
        self.targets.reply_override = RestorePublication(ResultStatus.CANCELLED, str(self.destination), NEW,
            AppError("IO_FAILURE", "unknown effect", "inspect"))
        reply = await self.service.restore_backup(self.request)
        self.assertEqual(reply.status, ResultStatus.UNCONFIRMED)
        self.assertEqual(self.targets.releases, [])

    async def test_context_change_after_capture_stops_before_target_preparation(self):
        original = self.storage.validate_snapshot

        async def changed(candidate, cancel):
            facts = await original(candidate, cancel)
            self.session.state = OutputSessionState(CallContext("d" * 32, 3), ())
            return facts

        self.storage.validate_snapshot = changed
        reply = await self.service.restore_backup(self.request)
        self.assertEqual(reply.error.code, "STALE_CONTEXT")
        self.assertEqual(self.targets.prepares, 0)

    async def test_context_change_after_publication_does_not_undo_output(self):
        self.targets.on_publish = lambda: setattr(self.session, "state",
            OutputSessionState(CallContext("d" * 32, 3), ()))
        reply = await self.service.restore_backup(self.request)
        self.assertEqual(reply.status, ResultStatus.OK)
        self.assertEqual(reply.delivery_context, CONTEXT)
        self.assertTrue((self.destination / "knowledge.sqlite").is_file())

    async def test_early_cancel_has_no_effect(self):
        signal = Event()
        signal.set()
        reply = await self.service.restore_backup(self.request, signal)
        self.assertEqual(reply.status, ResultStatus.CANCELLED)
        self.assertEqual(self.targets.prepares, 0)

    async def test_cancel_after_initialization_cleans_unpublished_only(self):
        signal = Event()
        self.storage.on_init = lambda candidate: signal.set()
        reply = await self.service.restore_backup(self.request, signal)
        self.assertEqual(reply.status, ResultStatus.CANCELLED)
        self.assertEqual(self.targets.publications, 0)
        self.assertEqual(self.storage.releases, 1)

    async def test_late_cancel_after_known_success_remains_success(self):
        signal = Event()
        original = self.targets.publish

        async def late(target, initialized, cancel):
            reply = await original(target, initialized, cancel)
            signal.set()
            return reply

        self.targets.publish = late
        reply = await self.service.restore_backup(self.request, signal)
        self.assertEqual(reply.status, ResultStatus.OK)

    async def test_outer_task_cancellation_waits_for_publication_receipt(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.targets.publish

        async def delayed(target, initialized, cancel):
            entered.set()
            await release.wait()
            # Simulate a publisher already committed before observing intent.
            return await original(target, initialized, Event())

        self.targets.publish = delayed
        signal = Event()
        task = asyncio.create_task(self.service.restore_backup(self.request, signal))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        reply = await asyncio.wait_for(task, 5)
        self.assertEqual(reply.status, ResultStatus.OK)
        self.assertTrue(signal.is_set())

    async def test_cleanup_failure_after_success_is_warning_not_republication(self):
        self.targets.release_warning = True
        reply = await self.service.restore_backup(self.request)
        self.assertEqual(reply.status, ResultStatus.OK)
        self.assertTrue(reply.warnings)
        self.assertEqual(self.targets.publications, 1)
        self.assertTrue((self.destination / "knowledge.sqlite").is_file())

    async def test_no_silent_default_ports_or_database_implementation_are_installed(self):
        with self.assertRaises(TypeError):
            RestoreService()

    async def test_invalid_ports_or_profile_are_rejected_during_construction(self):
        for session, storage, targets, policy in (
            (None, self.storage, self.targets, self.ap),
            (self.session, None, self.targets, self.ap),
            (self.session, self.storage, None, self.ap),
            (self.session, self.storage, self.targets,
             replace(self.ap, allowed_entries=frozenset({"manifest.json", "snapshot.sqlite", "extra"}))),
        ):
            with self.subTest(session=session, storage=storage, targets=targets, policy=policy):
                with self.assertRaises(ValueError):
                    RestoreService(session, storage, targets, self.work, self.mp, policy)
