"""API08 D-only orchestration with synthetic C/B ports, not product acceptance."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from pmkb.application.backup_restore.backup_service import BackupServiceImpl, SnapshotLease
from pmkb.application.backup_restore.manifest import (
    BackupManifest, ManifestPolicy, ReferenceCounts, SnapshotFacts,
)
from pmkb.application.backup_restore.package_components import inspect_package
from pmkb.application.output_support import OutputBufferState, OutputFailure, OutputSessionState
from pmkb.contracts.backup import BackupRequest, FilePublicationState, UnsavedBackupChoice as Choice
from pmkb.contracts.common import AppError, CallContext, ResultStatus
from pmkb.infrastructure.file_io.publication import PublicationResult
from pmkb.infrastructure.file_io.zip_guard import ArchivePolicy


CONTEXT = CallContext("a" * 32, 3)
OTHER = CallContext("b" * 32, 4)
PAYLOAD = "合成保存快照，不是实际SQLite数据库。".encode()
MANIFEST_POLICY = ManifestPolicy(frozenset({1}), frozenset({1}), 8192)
ARCHIVE_POLICY = ArchivePolicy(frozenset({"manifest.json", "snapshot.sqlite"}),
                               frozenset({"manifest.json", "snapshot.sqlite"}),
                               1024 * 1024, 2, 256 * 1024, 512 * 1024, 8192, 31)
CAPTURE_TIME = datetime(2026, 10, 18, 6, 2, 3, 123456, timezone.utc)


def lease(payload=PAYLOAD):
    digest = hashlib.sha256(payload).hexdigest()
    counts = ReferenceCounts(2, 1, 3, 4)
    manifest = BackupManifest(1, 1, "snapshot.sqlite", CONTEXT.collection_id,
                              "Z:\\unreachable-historical-only\\source", CAPTURE_TIME,
                              17, 2, counts, digest, len(payload))
    facts = SnapshotFacts(1, CONTEXT.collection_id, 17, 2, counts, digest, len(payload))
    return SnapshotLease(CONTEXT, payload, manifest, facts)


class Session:
    def __init__(self, states=None):
        self.states = list(states or [OutputSessionState(CONTEXT, ())])
        self.calls = 0
        self.hook = None

    async def inspect(self):
        self.calls += 1
        if self.hook is not None:
            await self.hook(self.calls)
        return self.states[min(self.calls - 1, len(self.states) - 1)]


class Source:
    def __init__(self, captured=None):
        self.captured = captured or lease()
        self.calls = []
        self.released = []
        self.capture_hook = None
        self.release_hook = None
        self.warnings = ()

    async def capture_saved(self, context, cancel):
        self.calls.append(context)
        if self.capture_hook is not None:
            await self.capture_hook(cancel)
        return self.captured

    async def release(self, captured):
        self.released.append(captured)
        if self.release_hook is not None:
            await self.release_hook()
        return self.warnings


class BackupServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.target = Path(self.temporary.name).resolve(strict=True) / "备份.zip"
        self.session, self.source = Session(), Source()

    def service(self):
        return BackupServiceImpl(self.session, self.source, MANIFEST_POLICY, ARCHIVE_POLICY)

    def request(self, choice=Choice.NO_UNSAVED):
        return BackupRequest(CONTEXT, str(self.target), choice)

    async def test_real_archive_uses_capture_facts_and_microsecond_timestamp(self):
        reply = await self.service().backup_collection(self.request())
        self.assertEqual(reply.status, ResultStatus.OK)
        self.assertEqual(reply.value.publication_state, FilePublicationState.PUBLISHED)
        self.assertEqual(reply.value.backup_captured_at_utc, CAPTURE_TIME)
        self.assertEqual(reply.value.source_collection_revision, 17)
        self.assertEqual(reply.value.saved_note_count, 2)
        checked = inspect_package(self.target, MANIFEST_POLICY, ARCHIVE_POLICY,
                                  facts=self.source.captured.facts)
        self.assertEqual(checked.manifest, self.source.captured.manifest)
        self.assertEqual(self.source.released, [self.source.captured])
        self.assertEqual(reply.request_context, CONTEXT)
        self.assertEqual(reply.delivery_context, CONTEXT)

    async def test_empty_saved_collection_is_valid_not_an_empty_draft(self):
        captured = lease(b"")
        self.source.captured = replace(captured,
            manifest=replace(captured.manifest, saved_note_count=0, reference_counts=ReferenceCounts(0, 0, 0, 0)),
            facts=replace(captured.facts, saved_note_count=0, reference_counts=ReferenceCounts(0, 0, 0, 0)))
        reply = await self.service().backup_collection(self.request())
        self.assertEqual(reply.status, ResultStatus.OK)
        self.assertEqual(reply.value.saved_note_count, 0)

    async def test_parent_alias_is_canonicalized_before_publication(self):
        # Model a Windows 8.3 TEMP alias without needing short-name support or
        # symlink privileges on this machine. The real ZIP writer runs below.
        alias = self.target.parent / "synthetic-short-parent-alias"
        original_resolve = Path.resolve

        def resolve(path, *args, **kwargs):
            if path == alias:
                return self.target.parent
            return original_resolve(path, *args, **kwargs)

        with patch.object(Path, "resolve", autospec=True, side_effect=resolve):
            request = replace(self.request(), destination_file=str(alias / self.target.name))
            reply = await self.service().backup_collection(request)
        self.assertEqual(reply.status, ResultStatus.OK)
        self.assertEqual(reply.value.destination_file, str(self.target))
        self.assertEqual(inspect_package(self.target, MANIFEST_POLICY, ARCHIVE_POLICY).manifest,
                         self.source.captured.manifest)

    async def test_parent_alias_preserves_known_error_outcomes(self):
        alias = self.target.parent / "synthetic-short-parent-alias"
        original_resolve = Path.resolve

        def resolve(path, *args, **kwargs):
            if path == alias:
                return self.target.parent
            return original_resolve(path, *args, **kwargs)

        for failure in ("target_exists", "snapshot_mismatch"):
            with self.subTest(failure=failure):
                if failure == "target_exists":
                    self.target.write_bytes(b"previous synthetic data")
                else:
                    self.target.unlink()
                    captured = lease()
                    self.source.captured = replace(captured,
                        manifest=replace(captured.manifest, snapshot_sha256="0" * 64),
                        facts=replace(captured.facts, snapshot_sha256="0" * 64))
                with patch.object(Path, "resolve", autospec=True, side_effect=resolve):
                    request = replace(self.request(), destination_file=str(alias / self.target.name))
                    reply = await self.service().backup_collection(request)
                self.assertEqual(reply.status, ResultStatus.ERROR)
                self.assertIsNone(reply.value)
                if failure == "target_exists":
                    self.assertEqual(reply.error.code, "TARGET_EXISTS")
                    self.assertEqual(self.target.read_bytes(), b"previous synthetic data")
                else:
                    self.assertFalse(self.target.exists())

    async def test_same_basename_in_another_parent_receipt_stays_unknown(self):
        wrong = self.target.parent / "another-parent" / self.target.name
        with patch("pmkb.application.backup_restore.backup_service.publish_package",
                   return_value=PublicationResult(ResultStatus.OK, wrong, byte_count=25)):
            reply = await self.service().backup_collection(self.request())
        self.assertEqual(reply.status, ResultStatus.UNCONFIRMED)
        self.assertEqual(reply.value.publication_state, FilePublicationState.UNKNOWN)
        self.assertFalse(self.target.exists())

    async def test_parent_resolution_failure_precedes_capture_and_publication(self):
        with patch.object(Path, "resolve", side_effect=OSError("synthetic parent failure")), patch(
                "pmkb.application.backup_restore.backup_service.publish_package") as publisher:
            reply = await self.service().backup_collection(self.request())
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertIsNone(reply.value)
        self.assertEqual(self.source.calls, [])
        publisher.assert_not_called()
        self.assertFalse(self.target.exists())

    async def test_parent_resolution_runs_off_loop_and_cancellation_prevents_capture(self):
        import threading
        owner = threading.get_ident()
        cancel = Event()
        original_resolve = Path.resolve

        def resolve(path, *args, **kwargs):
            self.assertNotEqual(threading.get_ident(), owner)
            resolved = original_resolve(path, *args, **kwargs)
            cancel.set()
            return resolved

        with patch.object(Path, "resolve", autospec=True, side_effect=resolve), patch(
                "pmkb.application.backup_restore.backup_service.publish_package") as publisher:
            reply = await self.service().backup_collection(self.request(), cancel)
        self.assertEqual(reply.status, ResultStatus.CANCELLED)
        self.assertEqual(self.source.calls, [])
        publisher.assert_not_called()
        self.assertFalse(self.target.exists())

    async def test_return_to_save_does_not_capture_publish_or_save(self):
        reply = await self.service().backup_collection(self.request(Choice.RETURN_TO_SAVE))
        self.assertEqual(reply.status, ResultStatus.CANCELLED)
        self.assertIsNone(reply.value)
        self.assertEqual(self.source.calls, [])
        self.assertFalse(self.target.exists())

    async def test_all_draft_categories_block_untrue_no_unsaved_claim(self):
        for category in ("name", "body", "tags", "references", "new-note"):
            with self.subTest(category=category):
                self.session = Session([OutputSessionState(CONTEXT,
                    (OutputBufferState(category, 1, True),))])
                reply = await self.service().backup_collection(self.request())
                self.assertEqual(reply.status, ResultStatus.ERROR)
                self.assertEqual(reply.error.code, "STATE_BLOCKED")
                self.assertEqual(self.source.calls, [])

    async def test_continue_only_excludes_explicitly_acknowledged_versions(self):
        buffer = OutputBufferState("new-note", 2, True)
        for acknowledgements, expected in (((), ResultStatus.ERROR),
                ((("new-note", 1),), ResultStatus.ERROR),
                ((("new-note", 2),), ResultStatus.OK)):
            with self.subTest(acknowledgements=acknowledgements):
                self.session = Session([OutputSessionState(CONTEXT, (buffer,),
                    acknowledged_dirty_versions=acknowledgements)])
                reply = await self.service().backup_collection(self.request(Choice.CONTINUE_SAVED_ONLY))
                self.assertEqual(reply.status, expected)

    async def test_new_draft_between_initial_checks_requires_new_choice(self):
        self.session = Session([OutputSessionState(CONTEXT, ()),
            OutputSessionState(CONTEXT, (OutputBufferState("new", 0, True),))])
        reply = await self.service().backup_collection(self.request())
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertEqual(self.source.calls, [])

    async def test_edited_confirmed_draft_during_capture_is_not_silently_excluded(self):
        initial = OutputSessionState(CONTEXT, (OutputBufferState("draft", 1, True),),
                                     acknowledged_dirty_versions=(("draft", 1),))
        changed = OutputSessionState(CONTEXT, (OutputBufferState("draft", 2, True),),
                                     acknowledged_dirty_versions=(("draft", 2),))
        self.session = Session([initial, initial, changed])
        reply = await self.service().backup_collection(self.request(Choice.CONTINUE_SAVED_ONLY))
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertEqual(self.source.released, [self.source.captured])
        self.assertFalse(self.target.exists())

    async def test_context_change_after_capture_prevents_publication(self):
        self.session = Session([OutputSessionState(CONTEXT, ()),
            OutputSessionState(CONTEXT, ()), OutputSessionState(OTHER, ())])
        reply = await self.service().backup_collection(self.request())
        self.assertEqual(reply.error.code, "STALE_CONTEXT")
        self.assertEqual(reply.delivery_context, CONTEXT)
        self.assertFalse(self.target.exists())

    async def test_unconfirmed_save_blocks_at_entry_or_after_capture(self):
        clean = OutputSessionState(CONTEXT, ())
        blocked = replace(clean, has_unconfirmed_save=True)
        for states in ([blocked], [clean, clean, blocked]):
            with self.subTest(states=states):
                self.session = Session(states)
                reply = await self.service().backup_collection(self.request())
                self.assertEqual(reply.error.code, "STATE_BLOCKED")
                self.assertFalse(self.target.exists())

    async def test_capture_context_or_collection_mismatch_is_rejected(self):
        valid = lease()
        for captured in (replace(valid, context=OTHER),
                replace(valid, manifest=replace(valid.manifest, source_collection_id=OTHER.collection_id),
                        facts=replace(valid.facts, source_collection_id=OTHER.collection_id))):
            with self.subTest(captured=captured):
                self.source = Source(captured)
                reply = await self.service().backup_collection(self.request())
                self.assertEqual(reply.status, ResultStatus.ERROR)
                self.assertFalse(self.target.exists())
                self.assertEqual(self.source.released, [captured])

    async def test_independent_c_facts_mismatch_never_reaches_publisher(self):
        captured = lease()
        self.source.captured = replace(captured, facts=replace(captured.facts, saved_note_count=99))
        with patch("pmkb.application.backup_restore.backup_service.publish_package") as publisher:
            reply = await self.service().backup_collection(self.request())
        self.assertEqual(reply.status, ResultStatus.ERROR)
        publisher.assert_not_called()

    async def test_actual_snapshot_hash_or_size_mismatch_fails_before_publication(self):
        for field, wrong in (("snapshot_sha256", "0" * 64), ("snapshot_size_bytes", 999)):
            with self.subTest(field=field):
                captured = lease()
                self.source.captured = replace(captured,
                    manifest=replace(captured.manifest, **{field: wrong}),
                    facts=replace(captured.facts, **{field: wrong}))
                reply = await self.service().backup_collection(self.request())
                self.assertEqual(reply.status, ResultStatus.ERROR)
                self.assertFalse(self.target.exists())

    async def test_snapshot_path_is_read_but_never_deleted_by_service(self):
        source_path = Path(self.temporary.name) / "C-owned-frozen.bin"
        source_path.write_bytes(PAYLOAD)
        self.source.captured = replace(lease(), frozen_snapshot=source_path)
        reply = await self.service().backup_collection(self.request())
        self.assertEqual(reply.status, ResultStatus.OK)
        self.assertEqual(source_path.read_bytes(), PAYLOAD)

    async def test_capture_error_has_no_fake_reply_and_does_not_publish(self):
        async def broken(cancel):
            raise OutputFailure("IO_FAILURE", "synthetic capture failure")
        self.source.capture_hook = broken
        reply = await self.service().backup_collection(self.request())
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertIsNone(reply.value)
        self.assertEqual(self.source.released, [])
        self.assertFalse(self.target.exists())

    async def test_capture_unknown_has_no_fake_time_or_publication(self):
        async def broken(cancel):
            raise OutputFailure("IO_FAILURE", "capture outcome unknown", unknown=True)
        self.source.capture_hook = broken
        reply = await self.service().backup_collection(self.request())
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertEqual(reply.error.code, "STATE_BLOCKED")
        self.assertIsNone(reply.value)
        self.assertFalse(self.target.exists())

    async def test_existing_destination_preserves_bytes_and_releases_snapshot(self):
        self.target.write_bytes(b"previous user data")
        reply = await self.service().backup_collection(self.request())
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertEqual(reply.error.code, "TARGET_EXISTS")
        self.assertEqual(self.target.read_bytes(), b"previous user data")
        self.assertEqual(len(self.source.released), 1)

    async def test_pre_cancelled_request_never_captures(self):
        cancel = Event()
        cancel.set()
        reply = await self.service().backup_collection(self.request(), cancel)
        self.assertEqual(reply.status, ResultStatus.CANCELLED)
        self.assertEqual(self.source.calls, [])

    async def test_cancel_after_capture_releases_owned_source_without_publishing(self):
        async def cancel_capture(cancel):
            cancel.set()
        self.source.capture_hook = cancel_capture
        reply = await self.service().backup_collection(self.request())
        self.assertEqual(reply.status, ResultStatus.CANCELLED)
        self.assertFalse(self.target.exists())
        self.assertEqual(len(self.source.released), 1)

    async def test_known_publication_success_survives_late_cancellation(self):
        cancel = Event()
        def published(*args):
            payload = b"synthetic publisher acknowledgement fixture"
            self.target.write_bytes(payload)
            cancel.set()
            return PublicationResult(ResultStatus.OK, self.target, byte_count=len(payload))
        with patch("pmkb.application.backup_restore.backup_service.publish_package", published):
            reply = await self.service().backup_collection(self.request(), cancel)
        self.assertEqual(reply.status, ResultStatus.OK)
        self.assertEqual(reply.value.publication_state, FilePublicationState.PUBLISHED)

    async def test_unknown_publisher_outcome_is_not_replayed_or_deleted(self):
        def uncertain(*args):
            self.target.write_bytes(b"possibly formal")
            return PublicationResult(ResultStatus.UNCONFIRMED, self.target,
                error=AppError("PUBLICATION_UNCONFIRMED", "unknown", "inspect"))
        with patch("pmkb.application.backup_restore.backup_service.publish_package", side_effect=uncertain) as publisher:
            reply = await self.service().backup_collection(self.request())
        self.assertEqual(reply.status, ResultStatus.UNCONFIRMED)
        self.assertEqual(reply.error.code, "IO_FAILURE")
        self.assertEqual(reply.value.publication_state, FilePublicationState.UNKNOWN)
        self.assertEqual(publisher.call_count, 1)
        self.assertEqual(self.target.read_bytes(), b"possibly formal")

    async def test_publisher_acknowledgement_exception_is_conservatively_unknown(self):
        def lost(*args):
            self.target.write_bytes(b"formal with lost reply")
            raise OSError("lost acknowledgement")
        with patch("pmkb.application.backup_restore.backup_service.publish_package", lost):
            reply = await self.service().backup_collection(self.request())
        self.assertEqual(reply.status, ResultStatus.UNCONFIRMED)
        self.assertEqual(self.target.read_bytes(), b"formal with lost reply")

    async def test_release_failure_is_warning_not_false_publication_failure(self):
        async def broken():
            raise OSError("source cleanup failed")
        self.source.release_hook = broken
        reply = await self.service().backup_collection(self.request())
        self.assertEqual(reply.status, ResultStatus.OK)
        self.assertTrue(reply.warnings)
        self.assertTrue(self.target.is_file())

    async def test_source_cleanup_warnings_are_preserved(self):
        warning = AppError("SNAPSHOT_RETAINED", "owned freeze retained", "inspect")
        self.source.warnings = (warning,)
        reply = await self.service().backup_collection(self.request())
        self.assertEqual(reply.warnings, (warning,))

    async def test_outer_task_cancellation_waits_for_capture_and_then_releases(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def capture(cancel):
            entered.set()
            await release.wait()
        self.source.capture_hook = capture
        task = asyncio.create_task(self.service().backup_collection(self.request()))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        reply = await task
        self.assertEqual(reply.status, ResultStatus.CANCELLED)
        self.assertEqual(len(self.source.released), 1)
        self.assertFalse(self.target.exists())

    async def test_outer_task_cancel_during_publish_waits_for_known_success(self):
        import threading
        entered, release = threading.Event(), threading.Event()
        def delayed(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            payload = b"synthetic delayed publisher fixture"
            self.target.write_bytes(payload)
            return PublicationResult(ResultStatus.OK, self.target, byte_count=len(payload))
        with patch("pmkb.application.backup_restore.backup_service.publish_package", delayed):
            task = asyncio.create_task(self.service().backup_collection(self.request()))
            await asyncio.wait_for(asyncio.to_thread(entered.wait), 5)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            reply = await task
        self.assertEqual(reply.status, ResultStatus.OK)
        self.assertEqual(len(self.source.released), 1)

    async def test_publish_runs_off_event_loop_and_old_context_is_not_relabelled(self):
        import threading
        owner = threading.get_ident()
        def published(*args):
            self.assertNotEqual(threading.get_ident(), owner)
            self.session.states = [OutputSessionState(OTHER, ())]
            payload = b"synthetic old-context publisher fixture"
            self.target.write_bytes(payload)
            return PublicationResult(ResultStatus.OK, self.target, byte_count=len(payload))
        with patch("pmkb.application.backup_restore.backup_service.publish_package", published):
            reply = await self.service().backup_collection(self.request())
        self.assertEqual(reply.status, ResultStatus.OK)
        self.assertEqual(reply.delivery_context, CONTEXT)
        self.assertEqual(reply.value.source_collection_id, CONTEXT.collection_id)

    async def test_invalid_requests_and_cancel_type_are_rejected_before_capture(self):
        requests = (None, replace(self.request(), context=CallContext(None, 0)),
                    replace(self.request(), context=CallContext("a" * 32, True)),
                    replace(self.request(), destination_file="relative.zip"),
                    replace(self.request(), destination_file=str(self.target.parent) + "/"),
                    replace(self.request(), destination_file=str(self.target.parent / "CON.zip")),
                    replace(self.request(), unsaved_choice="NO_UNSAVED"))
        for request in requests:
            with self.subTest(request=request):
                reply = await self.service().backup_collection(request)
                self.assertEqual(reply.status, ResultStatus.ERROR)
        reply = await self.service().backup_collection(self.request(), True)
        self.assertEqual(reply.error.code, "INVALID_ARGUMENT")
        self.assertEqual(self.source.calls, [])

    async def test_malformed_or_wrong_target_publication_receipt_is_unknown(self):
        for acknowledgement in (None,
                PublicationResult(ResultStatus.ERROR, self.target),
                PublicationResult(ResultStatus.OK, self.target.parent / "other.zip", byte_count=25),
                PublicationResult(ResultStatus.OK, self.target, byte_count=0),
                PublicationResult(ResultStatus.OK, self.target,
                                  byte_count=ARCHIVE_POLICY.max_archive_bytes + 1),
                PublicationResult("OK", self.target),
                PublicationResult(ResultStatus.CANCELLED, self.target,
                                  error=AppError("IO_FAILURE", "contradiction", "inspect"))):
            with self.subTest(acknowledgement=acknowledgement), patch(
                    "pmkb.application.backup_restore.backup_service.publish_package", return_value=acknowledgement):
                reply = await self.service().backup_collection(self.request())
                self.assertEqual(reply.status, ResultStatus.UNCONFIRMED)
                self.assertEqual(reply.value.destination_file, str(self.target))

    async def test_bad_or_mutable_capture_is_rejected_without_fake_backup(self):
        captured = lease()
        for wrong in (object(), replace(captured, frozen_snapshot=bytearray(PAYLOAD)),
                      replace(captured, manifest=replace(captured.manifest,
                          backup_captured_at_utc=CAPTURE_TIME.replace(tzinfo=None)))):
            with self.subTest(wrong=wrong):
                self.source = Source(wrong)
                reply = await self.service().backup_collection(self.request())
                self.assertEqual(reply.status, ResultStatus.ERROR)
                self.assertIsNone(reply.value)
                self.assertFalse(self.target.exists())

    async def test_session_failure_before_capture_does_not_start_source(self):
        async def broken(calls):
            raise OSError("session not available")
        self.session.hook = broken
        reply = await self.service().backup_collection(self.request())
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertEqual(self.source.calls, [])

    async def test_context_changes_before_capture_are_rejected(self):
        self.session = Session([OutputSessionState(CONTEXT, ()), OutputSessionState(OTHER, ())])
        reply = await self.service().backup_collection(self.request())
        self.assertEqual(reply.error.code, "STALE_CONTEXT")
        self.assertEqual(self.source.calls, [])

    async def test_new_draft_after_capture_does_not_publish(self):
        clean = OutputSessionState(CONTEXT, ())
        self.session = Session([clean, clean,
            OutputSessionState(CONTEXT, (OutputBufferState("new", 0, True),))])
        reply = await self.service().backup_collection(self.request())
        self.assertEqual(reply.error.code, "STATE_BLOCKED")
        self.assertFalse(self.target.exists())
        self.assertEqual(len(self.source.released), 1)

    async def test_definite_publish_error_or_cancellation_has_no_success_fields(self):
        warning = AppError("TEMPORARY_RETAINED", "owned file retained", "inspect")
        for acknowledgement in (
                PublicationResult(ResultStatus.ERROR, self.target, error=AppError("IO_FAILURE", "failed", "inspect"), warnings=(warning,)),
                PublicationResult(ResultStatus.CANCELLED, self.target, warnings=(warning,))):
            with self.subTest(acknowledgement=acknowledgement), patch(
                    "pmkb.application.backup_restore.backup_service.publish_package", return_value=acknowledgement):
                reply = await self.service().backup_collection(self.request())
                self.assertEqual(reply.status, acknowledgement.status)
                self.assertIsNone(reply.value)
                self.assertEqual(reply.warnings, (warning,))

    async def test_malformed_release_reply_does_not_erase_success(self):
        self.source.warnings = [AppError("SNAPSHOT_RETAINED", "retained", "inspect")]
        reply = await self.service().backup_collection(self.request())
        self.assertEqual(reply.status, ResultStatus.OK)
        self.assertEqual(reply.warnings[0].code, "SNAPSHOT_RETAINED")

    async def test_outer_cancel_during_source_release_waits_without_retracting_backup(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def release_source():
            entered.set()
            await release.wait()
        self.source.release_hook = release_source
        task = asyncio.create_task(self.service().backup_collection(self.request()))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        reply = await task
        self.assertEqual(reply.status, ResultStatus.OK)
        self.assertTrue(self.target.is_file())

    async def test_child_capture_task_loss_is_not_faked_as_known_cancellation(self):
        async def lost(cancel):
            raise asyncio.CancelledError()
        self.source.capture_hook = lost
        reply = await self.service().backup_collection(self.request())
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertEqual(reply.error.code, "STATE_BLOCKED")
        self.assertIsNone(reply.value)
        self.assertFalse(self.target.exists())

    async def test_archive_resource_budget_is_explicit_and_failure_preserves_source(self):
        source_path = Path(self.temporary.name) / "frozen-source.bin"
        source_path.write_bytes(PAYLOAD)
        self.source.captured = replace(lease(), frozen_snapshot=source_path)
        tiny = replace(ARCHIVE_POLICY, max_entry_bytes=2)
        service = BackupServiceImpl(self.session, self.source, MANIFEST_POLICY, tiny)
        reply = await service.backup_collection(self.request())
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertFalse(self.target.exists())
        self.assertEqual(source_path.read_bytes(), PAYLOAD)

    async def test_cancel_between_session_checks_does_not_capture(self):
        cancel = Event()
        async def update(calls):
            if calls == 2:
                cancel.set()
        self.session.hook = update
        reply = await self.service().backup_collection(self.request(), cancel)
        self.assertEqual(reply.status, ResultStatus.CANCELLED)
        self.assertEqual(self.source.calls, [])

    def test_constructor_requires_explicit_exact_profile_and_controlled_ports(self):
        cases = ((self.session, self.source, None, ARCHIVE_POLICY),
                 (self.session, self.source, MANIFEST_POLICY, None),
                 (self.session, self.source, MANIFEST_POLICY,
                  replace(ARCHIVE_POLICY, required_entries=frozenset({"snapshot.sqlite"}))),
                 (object(), self.source, MANIFEST_POLICY, ARCHIVE_POLICY),
                 (self.session, object(), MANIFEST_POLICY, ARCHIVE_POLICY))
        for arguments in cases:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                BackupServiceImpl(*arguments)


if __name__ == "__main__":
    unittest.main()
