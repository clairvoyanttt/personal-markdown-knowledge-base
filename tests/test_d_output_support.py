"""D-only session/choice rules and settled cancellation, not Qt integration."""
import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
from threading import Event
import unittest

from pmkb.contracts.backup import UnsavedBackupChoice as Choice
from pmkb.contracts.common import AppError, CallContext, ResultStatus
from pmkb.application.output_support import (
    OutputBufferState, OutputSessionState, OutputFailure, absolute_path,
    dirty_versions, file_error, recheck_state, result, run_io, settle,
    validate_choice, validate_context, validate_state,
)


CONTEXT = CallContext("a" * 32, 3)
NOTE = "b" * 32


class OutputSupportTests(unittest.TestCase):
    def state(self, *, consent=()):
        return OutputSessionState(CONTEXT, (OutputBufferState("draft", 2, True, NOTE),),
                                  acknowledged_dirty_versions=consent)

    def test_valid_context_and_restoration_without_active_collection(self):
        validate_context(CONTEXT)
        validate_context(CallContext(None, 0), require_active=False)
        with self.assertRaises(OutputFailure):
            validate_context(CallContext(None, 0))

    def test_context_rejects_bool_negative_overflow_and_invalid_identity(self):
        for context in (CallContext("a" * 32, True), CallContext("a" * 32, -1),
                        CallContext("a" * 32, 2 ** 63), CallContext("A" * 32, 0), None):
            with self.subTest(context=context), self.assertRaises(OutputFailure):
                validate_context(context)

    def test_state_rejects_mutable_duplicate_and_malformed_buffer_fields(self):
        good = self.state()
        for state in (replace(good, buffers=list(good.buffers)),
                      replace(good, buffers=good.buffers * 2),
                      replace(good, buffers=(replace(good.buffers[0], edit_version=True),)),
                      replace(good, buffers=(replace(good.buffers[0], is_dirty=1),)),
                      replace(good, buffers=(replace(good.buffers[0], note_id="x"),))):
            with self.subTest(state=state), self.assertRaises(OutputFailure):
                validate_state(state, CONTEXT)

    def test_unconfirmed_save_and_stale_context_block_output(self):
        for state in (replace(self.state(), has_unconfirmed_save=True),
                      replace(self.state(), context=CallContext("c" * 32, 4))):
            with self.assertRaises(OutputFailure):
                validate_state(state, CONTEXT)

    def test_no_unsaved_claim_is_checked_and_return_does_not_imply_saving(self):
        with self.assertRaises(OutputFailure):
            validate_choice(self.state(), Choice.NO_UNSAVED)
        validate_choice(self.state(), Choice.RETURN_TO_SAVE)

    def test_continue_requires_acknowledged_current_dirty_versions(self):
        for consent in ((), (("draft", 1),), (("other", 2),)):
            with self.subTest(consent=consent), self.assertRaises(OutputFailure):
                validate_choice(self.state(consent=consent), Choice.CONTINUE_SAVED_ONLY)
        validate_choice(self.state(consent=(("draft", 2),)), Choice.CONTINUE_SAVED_ONLY)

    def test_export_choice_only_covers_selected_note_but_backup_covers_all(self):
        state = OutputSessionState(CONTEXT, (OutputBufferState("new", 0, True),))
        validate_choice(state, Choice.NO_UNSAVED, NOTE)
        with self.assertRaises(OutputFailure):
            validate_choice(state, Choice.NO_UNSAVED)
        self.assertEqual(dirty_versions(state, NOTE), ())

    def test_new_draft_or_new_edit_requires_new_decision_during_capture(self):
        initial = self.state(consent=(("draft", 2),))
        for after in (replace(initial, buffers=(replace(initial.buffers[0], edit_version=3),),
                              acknowledged_dirty_versions=(("draft", 3),)),
                      replace(initial, buffers=initial.buffers + (OutputBufferState("new", 0, True),),
                              acknowledged_dirty_versions=(("draft", 2), ("new", 0)))):
            with self.subTest(after=after), self.assertRaises(OutputFailure):
                recheck_state(initial, after, Choice.CONTINUE_SAVED_ONLY)

    def test_malformed_acknowledgements_are_not_accepted(self):
        for consent in ((("draft", True),), (("draft", 2), ("draft", 2)), [ ("draft", 2) ]):
            with self.subTest(consent=consent), self.assertRaises(OutputFailure):
                validate_state(self.state(consent=consent), CONTEXT)

    def test_result_delivery_stays_bound_to_origin_and_status_shape_is_checked(self):
        reply = result(CONTEXT, ResultStatus.OK, value="synthetic")
        self.assertEqual(reply.delivery_context, CONTEXT)
        for status, value, error in ((ResultStatus.OK, None, None),
                                     (ResultStatus.ERROR, "not allowed", AppError("IO_FAILURE", "x", "y")),
                                     (ResultStatus.UNCONFIRMED, None, None)):
            with self.subTest(status=status), self.assertRaises(ValueError):
                result(CONTEXT, status, value=value, error=error)

    def test_file_errors_are_mapped_without_leaking_low_level_reasons(self):
        self.assertEqual(file_error(AppError("TARGET_EXISTS", "private", "private")).code, "TARGET_EXISTS")
        mapped = file_error(AppError("ARCHIVE_HASH_MISMATCH", "secret source path", "private"))
        self.assertEqual(mapped.code, "IO_FAILURE")
        self.assertNotIn("secret", mapped.reason)

    def test_absolute_path_validation_does_not_create_files(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(absolute_path(directory), Path(directory))
            for path in ("relative.zip", "", directory + "/../out", "bad\x00path", None):
                with self.subTest(path=path), self.assertRaises(OutputFailure):
                    absolute_path(path)


class SettledOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_outer_cancel_sets_intent_but_waits_for_actual_result(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        signal = Event()

        async def effect():
            entered.set()
            await release.wait()
            return "actual outcome"

        task = asyncio.create_task(settle(effect(), signal))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertTrue(signal.is_set())
        release.set()
        self.assertEqual(await task, "actual outcome")

    async def test_underlying_cancel_is_not_fabricated_as_rollback(self):
        async def lost():
            raise asyncio.CancelledError()

        with self.assertRaises(OutputFailure) as caught:
            await settle(lost(), Event())
        self.assertTrue(caught.exception.unknown)

    async def test_io_work_runs_off_the_service_event_loop(self):
        import threading
        owner = threading.get_ident()
        worker = await run_io(threading.get_ident, Event())
        self.assertNotEqual(worker, owner)
