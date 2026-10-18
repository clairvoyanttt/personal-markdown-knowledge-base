"""D-internal API07 orchestration, using synthetic C/B ports and real files."""
import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
from threading import Event
import unittest

from pmkb.application.export.rendering import ExportReference, ReferenceKind
from pmkb.application.export.service import ExportRequest, ExportService, SavedExportNote
from pmkb.application.output_support import OutputBufferState, OutputSessionState, OutputFailure
from pmkb.contracts.backup import FilePublicationState, UnsavedBackupChoice
from pmkb.contracts.common import AppError, CallContext, ResultStatus
from pmkb.infrastructure.file_io.publication import PublicationResult, publish_bytes


CONTEXT = CallContext("a" * 32, 4)
NOTE = "b" * 32
TARGET = "c" * 32
MARKER = f"[[note:{CONTEXT.collection_id}:{TARGET}]]"


class Session:
    def __init__(self, states=None):
        self.states = list(states or [OutputSessionState(CONTEXT, ())])
        self.calls = 0

    async def inspect(self):
        self.calls += 1
        return self.states[min(self.calls - 1, len(self.states) - 1)]


class Source:
    def __init__(self, note=None):
        self.note = note
        self.calls = []
        self.action = None

    async def read_saved(self, context, note_id, cancel):
        self.calls.append((context, note_id, cancel))
        if self.action:
            await self.action(cancel)
        return self.note


class ExportServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pmkb-export-test-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        self.saved = SavedExportNote(CONTEXT, NOTE, 3, 9, "保存/名称", "保存正文", ())
        self.source = Source(self.saved)
        self.session = Session()
        self.service = ExportService(self.source, self.session)
        self.request = ExportRequest(CONTEXT, NOTE, str(self.directory), None,
                                     UnsavedBackupChoice.NO_UNSAVED)

    async def test_default_name_and_utf8_saved_projection(self):
        reply = await self.service.export_note(self.request)
        self.assertEqual(reply.status, ResultStatus.OK)
        self.assertEqual(Path(reply.value.destination_file).name, "note-保存_名称.md")
        self.assertEqual(Path(reply.value.destination_file).read_bytes(), "保存正文".encode())
        self.assertEqual((reply.value.saved_revision, reply.value.source_collection_revision), (3, 9))
        self.assertEqual(reply.value.publication_state, FilePublicationState.PUBLISHED)
        self.assertEqual(reply.request_context, CONTEXT)
        self.assertEqual(reply.delivery_context, CONTEXT)
        self.assertGreaterEqual(self.session.calls, 3)

    async def test_selected_extension(self):
        for selected, expected in (("输出", "输出.md"), ("大写.MD", "大写.MD")):
            with self.subTest(selected=selected):
                reply = await self.service.export_note(replace(self.request, file_name=selected))
                self.assertEqual(reply.status, ResultStatus.OK)
                self.assertEqual(Path(reply.value.destination_file).name, expected)

    async def test_dirty_selected_note_uses_saved_name_body_and_relations(self):
        self.session.states = [OutputSessionState(CONTEXT, (OutputBufferState("draft", 7, True, NOTE),),
                                                acknowledged_dirty_versions=(("draft", 7),))]
        reply = await self.service.export_note(replace(self.request, unsaved_choice=UnsavedBackupChoice.CONTINUE_SAVED_ONLY))
        self.assertEqual(reply.status, ResultStatus.OK)
        self.assertEqual(Path(reply.value.destination_file).read_text(encoding="utf-8"), "保存正文")
        self.assertEqual(self.session.states[0].buffers[0].edit_version, 7)

    async def test_unrelated_dirty_and_new_draft_do_not_block_selected_export(self):
        self.session.states = [OutputSessionState(CONTEXT, (OutputBufferState("new", 1, True),
                                                         OutputBufferState("other", 8, True, TARGET)))]
        self.assertEqual((await self.service.export_note(self.request)).status, ResultStatus.OK)

    async def test_all_four_authorized_reference_kinds_and_code_unchanged(self):
        foreign = f"[[note:{'d' * 32}:{TARGET}]]"
        markers = [MARKER, MARKER, MARKER, foreign]
        body = "\n".join(markers) + f"\n`{MARKER}`\n[网页](https://example.invalid) ![图](a.png)"
        refs = tuple(ExportReference(index * 75, 74, marker, kind,
                                     "名称[*]" if kind is ReferenceKind.VALID else None)
                     for index, (marker, kind) in enumerate(zip(markers, ReferenceKind)))
        self.source.note = replace(self.saved, saved_body=body, references=refs)
        reply = await self.service.export_note(self.request)
        self.assertEqual(reply.status, ResultStatus.OK)
        text = Path(reply.value.destination_file).read_text(encoding="utf-8")
        self.assertIn("内部引用：名称\\[\\*\\]", text)
        self.assertIn("目标已删除", text)
        self.assertIn("目标标识不存在或不可用", text)
        self.assertIn("引用来自其他集合", text)
        self.assertIn(f"`{MARKER}`", text)
        self.assertIn("[网页](https://example.invalid) ![图](a.png)", text)

    async def test_new_draft_rejected_without_source_read(self):
        reply = await self.service.export_note(replace(self.request, note_id=None))
        self.assertEqual((reply.status, reply.error.code), (ResultStatus.ERROR, "NO_SAVED_VERSION"))
        self.assertIsNone(reply.value)
        self.assertEqual(self.source.calls, [])
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_missing_saved_note(self):
        self.source.note = None
        reply = await self.service.export_note(self.request)
        self.assertEqual(reply.error.code, "NO_SAVED_VERSION")
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_return_to_save_neither_saves_nor_reads(self):
        reply = await self.service.export_note(replace(self.request, unsaved_choice=UnsavedBackupChoice.RETURN_TO_SAVE))
        self.assertEqual(reply.status, ResultStatus.CANCELLED)
        self.assertIsNone(reply.value)
        self.assertEqual(self.source.calls, [])
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_no_unsaved_must_recheck_actual_selected_state(self):
        self.session.states = [OutputSessionState(CONTEXT, (OutputBufferState("draft", 1, True, NOTE),))]
        reply = await self.service.export_note(self.request)
        self.assertEqual(reply.error.code, "STATE_BLOCKED")
        self.assertEqual(self.source.calls, [])

    async def test_continue_requires_exact_current_dirty_acknowledgement(self):
        for acknowledgements in ((), (("draft", 0),), (("different", 1),)):
            with self.subTest(acknowledgements=acknowledgements):
                self.session.states = [OutputSessionState(CONTEXT, (OutputBufferState("draft", 1, True, NOTE),),
                                                         acknowledged_dirty_versions=acknowledgements)]
                reply = await self.service.export_note(replace(self.request, unsaved_choice=UnsavedBackupChoice.CONTINUE_SAVED_ONLY))
                self.assertEqual(reply.error.code, "STATE_BLOCKED")
        self.assertEqual(self.source.calls, [])

    async def test_dirty_or_context_changes_during_capture_abort(self):
        clean = OutputSessionState(CONTEXT, ())
        for after, code in ((OutputSessionState(CONTEXT, (OutputBufferState("late", 1, True, NOTE),)), "STATE_BLOCKED"),
                            (OutputSessionState(CallContext("d" * 32, 5), ()), "STALE_CONTEXT")):
            with self.subTest(code=code):
                self.session.calls = 0
                self.session.states = [clean, after]
                reply = await self.service.export_note(self.request)
                self.assertEqual(reply.error.code, code)
                self.assertEqual(list(self.directory.iterdir()), [])

    async def test_acknowledged_new_edit_during_capture_still_requires_restart(self):
        before = OutputSessionState(CONTEXT, (OutputBufferState("draft", 1, True, NOTE),),
                                    acknowledged_dirty_versions=(("draft", 1),))
        after = OutputSessionState(CONTEXT, (OutputBufferState("draft", 2, True, NOTE),),
                                   acknowledged_dirty_versions=(("draft", 2),))
        self.session.states = [before, after]
        reply = await self.service.export_note(replace(self.request, unsaved_choice=UnsavedBackupChoice.CONTINUE_SAVED_ONLY))
        self.assertEqual(reply.error.code, "STATE_BLOCKED")
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_dirty_changes_after_render_before_publish_abort(self):
        clean = OutputSessionState(CONTEXT, ())
        self.session.states = [clean, clean, OutputSessionState(CONTEXT, (OutputBufferState("late", 1, True, NOTE),))]
        reply = await self.service.export_note(self.request)
        self.assertEqual(reply.error.code, "STATE_BLOCKED")
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_global_unknown_save_blocks_even_unrelated_note(self):
        self.session.states = [OutputSessionState(CONTEXT, (), has_unconfirmed_save=True)]
        reply = await self.service.export_note(self.request)
        self.assertEqual(reply.error.code, "STATE_BLOCKED")
        self.assertEqual(self.source.calls, [])

    async def test_invalid_request_fields_fail_before_io(self):
        for changes in ({"note_id": "../bad"}, {"note_id": True}, {"file_name": "../evil.md"},
                        {"file_name": "CON.md"}, {"file_name": "export.txt"},
                        {"destination_directory": "relative"}, {"destination_directory": str(self.directory / "..")},
                        {"unsaved_choice": "NO_UNSAVED"}, {"context": CallContext("A" * 32, 1)},
                        {"context": CallContext(None, 1)}):
            with self.subTest(changes=changes):
                reply = await self.service.export_note(replace(self.request, **changes))
                self.assertEqual(reply.status, ResultStatus.ERROR)
                self.assertIsNone(reply.value)
        self.assertEqual(self.source.calls, [])

    async def test_invalid_cancel_type(self):
        reply = await self.service.export_note(self.request, cancel=True)
        self.assertEqual(reply.error.code, "INVALID_ARGUMENT")
        self.assertEqual(self.source.calls, [])

    async def test_directory_must_already_exist_and_be_directory(self):
        file = self.directory / "file"
        file.write_bytes(b"existing")
        for target in (self.directory / "missing", file):
            with self.subTest(target=target):
                reply = await self.service.export_note(replace(self.request, destination_directory=str(target)))
                self.assertEqual(reply.status, ResultStatus.ERROR)
                self.assertFalse((self.directory / "missing").exists())
                self.assertEqual(file.read_bytes(), b"existing")

    async def test_saved_projection_identity_and_revision_fail_closed(self):
        for changes in ({"context": CallContext("d" * 32, 4)}, {"context": CallContext(CONTEXT.collection_id, 4.0)},
                        {"note_id": TARGET},
                        {"saved_revision": 0}, {"saved_revision": True}, {"saved_revision": 2 ** 63},
                        {"collection_revision": 0}, {"collection_revision": True}, {"references": []},
                        {"saved_name": ""}, {"saved_name": "x" * 101}, {"saved_body": "\ud800"}):
            with self.subTest(changes=changes):
                self.source.note = replace(self.saved, **changes)
                reply = await self.service.export_note(self.request)
                self.assertEqual(reply.status, ResultStatus.ERROR)
                self.assertEqual(reply.error.code, "STATE_BLOCKED")
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_source_wrong_object_rejected(self):
        self.source.note = {"saved_body": "pretend"}
        reply = await self.service.export_note(self.request)
        self.assertEqual(reply.error.code, "STATE_BLOCKED")

    async def test_reference_projection_inconsistency_rejected(self):
        for kind, marker, offset in ((ReferenceKind.VALID, MARKER, 1),
                                     (ReferenceKind.FOREIGN_COLLECTION, MARKER, 0),
                                     (ReferenceKind.VALID, f"[[note:{'d' * 32}:{TARGET}]]", 0)):
            with self.subTest(kind=kind, offset=offset):
                self.source.note = replace(self.saved, saved_body=marker,
                    references=(ExportReference(offset, 74, marker, kind, "目标"),))
                reply = await self.service.export_note(self.request)
                self.assertEqual(reply.error.code, "STATE_BLOCKED")
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_target_collision_never_overwrites(self):
        target = self.directory / "note-保存_名称.md"
        target.write_bytes(b"existing")
        reply = await self.service.export_note(self.request)
        self.assertEqual((reply.status, reply.error.code), (ResultStatus.ERROR, "TARGET_EXISTS"))
        self.assertEqual(target.read_bytes(), b"existing")
        self.assertIsNone(reply.value)

    async def test_two_exports_competing_for_target_only_one_publishes(self):
        replies = await asyncio.gather(self.service.export_note(self.request), self.service.export_note(self.request))
        self.assertEqual(sorted(reply.status.value for reply in replies), ["ERROR", "OK"])
        self.assertEqual(Path(next(reply.value.destination_file for reply in replies if reply.value)).read_bytes(), "保存正文".encode())

    async def test_early_cancel_and_capture_cancel_publish_nothing(self):
        cancel = Event()
        cancel.set()
        reply = await self.service.export_note(self.request, cancel)
        self.assertEqual(reply.status, ResultStatus.CANCELLED)
        self.assertEqual(self.source.calls, [])
        cancel.clear()
        async def cancel_capture(signal):
            signal.set()
        self.source.action = cancel_capture
        reply = await self.service.export_note(self.request, cancel)
        self.assertEqual(reply.status, ResultStatus.CANCELLED)
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_cancel_outer_task_waits_for_c_read_no_publish(self):
        started = asyncio.Event()
        release = asyncio.Event()
        finished = []
        async def read(signal):
            started.set()
            await release.wait()
            finished.append(True)
        self.source.action = read
        task = asyncio.create_task(self.service.export_note(self.request))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        reply = await task
        self.assertEqual(reply.status, ResultStatus.CANCELLED)
        self.assertEqual(finished, [True])
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_worker_file_write_does_not_block_event_loop(self):
        started, release = Event(), Event()
        def publisher(destination, payload, cancel=None):
            started.set()
            if not release.wait(5):
                raise TimeoutError("test worker did not release")
            return publish_bytes(destination, payload, cancel=cancel)
        service = ExportService(self.source, self.session, publisher=publisher)
        task = asyncio.create_task(service.export_note(self.request))
        self.assertTrue(await asyncio.to_thread(started.wait, 5))
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        self.assertEqual((await task).status, ResultStatus.OK)

    async def test_outer_cancel_during_publication_waits_for_real_success(self):
        started, release = Event(), Event()
        def publisher(destination, payload, cancel=None):
            reply = publish_bytes(destination, payload)
            started.set()
            if not release.wait(5):
                raise TimeoutError("test worker did not release")
            return reply
        service = ExportService(self.source, self.session, publisher=publisher)
        task = asyncio.create_task(service.export_note(self.request))
        self.assertTrue(await asyncio.to_thread(started.wait, 5))
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        reply = await task
        self.assertEqual(reply.status, ResultStatus.OK)
        self.assertTrue(Path(reply.value.destination_file).is_file())

    async def test_context_changes_after_known_publish_keep_success_on_original_context(self):
        def publisher(destination, payload, cancel=None):
            reply = publish_bytes(destination, payload, cancel=cancel)
            self.session.states = [OutputSessionState(CallContext("d" * 32, 6), ())]
            return reply
        reply = await ExportService(self.source, self.session, publisher=publisher).export_note(self.request)
        self.assertEqual(reply.status, ResultStatus.OK)
        self.assertEqual(reply.delivery_context, CONTEXT)
        self.assertTrue(Path(reply.value.destination_file).is_file())

    async def test_publisher_unknown_preserves_paths_and_does_not_retry(self):
        calls = []
        temporary = self.directory / ".unknown.tmp"
        def publisher(destination, payload, cancel=None):
            calls.append(True)
            Path(destination).write_bytes(payload)
            temporary.write_bytes(payload)
            return PublicationResult(ResultStatus.UNCONFIRMED, Path(destination), len(payload), temporary,
                                     AppError("PUBLICATION_UNCONFIRMED", "unknown", "inspect"))
        reply = await ExportService(self.source, self.session, publisher=publisher).export_note(self.request)
        self.assertEqual(reply.status, ResultStatus.UNCONFIRMED)
        self.assertEqual(reply.value.publication_state, FilePublicationState.UNKNOWN)
        self.assertEqual(reply.value.temporary_file, str(temporary))
        self.assertEqual(calls, [True])
        self.assertTrue(temporary.is_file())
        self.assertTrue(Path(reply.value.destination_file).is_file())

    async def test_exception_losing_publisher_receipt_is_unknown_not_error(self):
        def publisher(destination, payload, cancel=None):
            Path(destination).write_bytes(payload)
            raise RuntimeError("ack lost")
        reply = await ExportService(self.source, self.session, publisher=publisher).export_note(self.request)
        self.assertEqual(reply.status, ResultStatus.UNCONFIRMED)
        self.assertEqual(reply.error.code, "IO_FAILURE")
        self.assertTrue(Path(reply.value.destination_file).is_file())

    async def test_publisher_failure_and_cancel_have_no_value(self):
        for status in (ResultStatus.ERROR, ResultStatus.CANCELLED):
            def publisher(destination, payload, cancel=None):
                return PublicationResult(status, Path(destination), error=AppError("IO_FAILURE", "failed", "retry") if status is ResultStatus.ERROR else None)
            with self.subTest(status=status):
                reply = await ExportService(self.source, self.session, publisher=publisher).export_note(self.request)
                self.assertEqual(reply.status, status)
                self.assertIsNone(reply.value)

    async def test_source_failure_never_claims_file_publication(self):
        async def fail(signal):
            raise OutputFailure("IO_FAILURE", "saved read failed")
        self.source.action = fail
        reply = await self.service.export_note(self.request)
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertIsNone(reply.value)
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_utf16_reference_after_non_bmp_text(self):
        self.source.note = replace(self.saved, saved_body="😀" + MARKER + "尾",
            references=(ExportReference(2, 74, MARKER, ReferenceKind.VALID, "目标"),))
        reply = await self.service.export_note(self.request)
        self.assertEqual(reply.status, ResultStatus.OK)
        self.assertEqual(Path(reply.value.destination_file).read_text(encoding="utf-8"),
                         "😀内部引用：目标（仅供识别，导出后不支持应用内跳转）尾")

    async def test_sanitized_name_collision_does_not_change_either_note(self):
        self.source.note = replace(self.saved, saved_name="a/b")
        first = await self.service.export_note(self.request)
        self.source.note = replace(self.saved, note_id=TARGET, saved_name="a?b", saved_body="另一篇")
        second = await self.service.export_note(replace(self.request, note_id=TARGET))
        self.assertEqual(first.status, ResultStatus.OK)
        self.assertEqual((second.status, second.error.code), (ResultStatus.ERROR, "TARGET_EXISTS"))
        self.assertEqual(Path(first.value.destination_file).read_bytes(), "保存正文".encode())
        self.assertEqual(self.source.note.saved_body, "另一篇")

    async def test_pre_publish_source_cancellation_does_not_return_unknown_file(self):
        async def cancelled(signal):
            raise asyncio.CancelledError()
        self.source.action = cancelled
        reply = await self.service.export_note(self.request)
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertIsNone(reply.value)
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_broken_publication_receipts_stay_unknown(self):
        for receipt in (None, PublicationResult(ResultStatus.OK, self.directory / "wrong.md", 12),
                        PublicationResult(ResultStatus.OK, self.directory / "note-保存_名称.md", True),
                        PublicationResult(ResultStatus.PARTIAL, self.directory / "note-保存_名称.md")):
            with self.subTest(receipt=receipt):
                def publisher(destination, payload, cancel=None):
                    return receipt
                reply = await ExportService(self.source, self.session, publisher=publisher).export_note(self.request)
                self.assertEqual(reply.status, ResultStatus.UNCONFIRMED)
                self.assertEqual(reply.value.publication_state, FilePublicationState.UNKNOWN)

    async def test_cleanup_warning_does_not_erase_known_publication(self):
        temporary = self.directory / ".retained.tmp"
        def publisher(destination, payload, cancel=None):
            receipt = publish_bytes(destination, payload, cancel=cancel)
            temporary.write_bytes(payload)
            return replace(receipt, temporary_path=temporary,
                           warnings=(AppError("TEMPORARY_RETAINED", "retained", "inspect"),))
        reply = await ExportService(self.source, self.session, publisher=publisher).export_note(self.request)
        self.assertEqual(reply.status, ResultStatus.OK)
        self.assertEqual(reply.value.temporary_file, str(temporary))
        self.assertEqual(reply.warnings[0].code, "IO_FAILURE")
        self.assertTrue(temporary.is_file())

    async def test_invalid_request_object_is_structured_error(self):
        reply = await self.service.export_note(None)
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertEqual(reply.error.code, "INVALID_ARGUMENT")
        self.assertEqual(self.source.calls, [])

    async def test_malformed_failure_and_unknown_receipts_are_not_known_failures(self):
        target = self.directory / "note-保存_名称.md"
        bad = AppError("IO_FAILURE", "inconsistent", "inspect")
        for receipt in (PublicationResult(ResultStatus.ERROR, target),
                        PublicationResult(ResultStatus.UNCONFIRMED, target),
                        PublicationResult(ResultStatus.CANCELLED, target, error=bad),
                        PublicationResult(ResultStatus.ERROR, target, -1, error=bad),
                        PublicationResult(ResultStatus.CANCELLED, target, True),
                        PublicationResult(ResultStatus.ERROR, target, 999999, error=bad),
                        PublicationResult(ResultStatus.UNCONFIRMED, target, error="not an AppError"),
                        PublicationResult(ResultStatus.CANCELLED, target, temporary_path=Path("relative.tmp")),
                        PublicationResult(ResultStatus.ERROR, target, temporary_path=target, error=bad)):
            with self.subTest(receipt=receipt):
                calls = []
                def publisher(destination, payload, cancel=None):
                    calls.append(True)
                    return receipt
                reply = await ExportService(self.source, self.session, publisher=publisher).export_note(self.request)
                self.assertEqual(reply.status, ResultStatus.UNCONFIRMED)
                self.assertEqual(reply.value.publication_state, FilePublicationState.UNKNOWN)
                self.assertEqual(calls, [True])

    async def test_cancelled_partial_staging_count_is_not_publication(self):
        def publisher(destination, payload, cancel=None):
            return PublicationResult(ResultStatus.CANCELLED, Path(destination), 3)
        reply = await ExportService(self.source, self.session, publisher=publisher).export_note(self.request)
        self.assertEqual(reply.status, ResultStatus.CANCELLED)
        self.assertIsNone(reply.value)

    async def test_broken_receipt_keeps_known_temporary_location_without_cleanup(self):
        temporary = self.directory / ".unknown.tmp"
        def publisher(destination, payload, cancel=None):
            temporary.write_bytes(payload)
            Path(destination).write_bytes(payload)
            return PublicationResult(ResultStatus.ERROR, Path(destination), len(payload), temporary)
        reply = await ExportService(self.source, self.session, publisher=publisher).export_note(self.request)
        self.assertEqual(reply.status, ResultStatus.UNCONFIRMED)
        self.assertEqual(reply.value.temporary_file, str(temporary))
        self.assertTrue(temporary.is_file())
        self.assertTrue(Path(reply.value.destination_file).is_file())

    async def test_invalid_service_ports_fail_before_an_operation(self):
        for source, session, publisher in ((self.source, self.session, None),
                                           (None, self.session, publish_bytes),
                                           (self.source, None, publish_bytes)):
            with self.subTest(source=source, session=session, publisher=publisher):
                with self.assertRaises(ValueError):
                    ExportService(source, session, publisher=publisher)


if __name__ == "__main__":
    unittest.main()
