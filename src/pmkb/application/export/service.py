"""Executable D-internal API07 draft; not an agreed B/C public contract.

B must present FR09's no-internal-jump / no-attachment-packaging limitations
before calling, capture the user's current saved-only decision and supply its
exact dirty-buffer acknowledgements through OutputSession. C supplies an
immutable, context-bound saved projection with shared-parser-authorized spans;
this service never reads drafts, parses markers, saves notes or queries SQLite.

File work runs off the application event loop. Cancellation records intent but
waits for the actual in-flight operation: a known publication stays successful,
an unknown publication retains its artifacts and is never automatically retried.
All deliveries retain their originating context, even after a later UI switch.
"""
from dataclasses import dataclass
from pathlib import Path
import re
from threading import Event
from typing import Callable, Protocol

from pmkb.application.export.naming import default_export_name, selected_export_name
from pmkb.application.export.rendering import ExportReference, ReferenceKind, render_export_markdown
from pmkb.application.output_support import (
    OutputFailure, OutputSession, absolute_path, file_error, recheck_state,
    result, run_io, settle, validate_choice, validate_context, validate_state,
)
from pmkb.contracts.backup import FilePublicationState, UnsavedBackupChoice
from pmkb.contracts.common import AppError, CallContext, OperationResult, ResultStatus
from pmkb.infrastructure.file_io.publication import PublicationResult, publish_bytes


_ID = re.compile(r"[0-9a-f]{32}")
_MARKER = re.compile(r"\[\[note:([0-9a-f]{32}):[0-9a-f]{32}\]\]")
_MAX = 2 ** 63 - 1


@dataclass(frozen=True, slots=True)
class ExportRequest:
    context: CallContext
    note_id: str | None
    destination_directory: str
    file_name: str | None
    unsaved_choice: UnsavedBackupChoice


@dataclass(frozen=True, slots=True)
class SavedExportNote:
    """C's single consistent saved read, not a caller-built draft projection.

    references must be a frozen tuple of authorized occurrences, including
    target classifications/names from this same collection-revision capture.
    D validates structural consistency, not parser completeness or target truth.
    """
    context: CallContext
    note_id: str
    saved_revision: int
    collection_revision: int
    saved_name: str
    saved_body: str
    references: tuple[ExportReference, ...]


@dataclass(frozen=True, slots=True)
class ExportReply:
    destination_file: str
    source_collection_id: str
    note_id: str
    saved_revision: int
    source_collection_revision: int
    publication_state: FilePublicationState
    temporary_file: str | None = None


class ExportSourcePort(Protocol):
    async def read_saved(self, context: CallContext, note_id: str,
                         cancel: Event) -> SavedExportNote | None:
        """Read-only consistent saved projection. None means no saved version.

        No autosave or implicit draft promotion. Validate the requested context
        at the actual read boundary; classify only shared-parser-approved spans.
        """
        ...


def _projection(value: object, context: CallContext, note_id: str) -> SavedExportNote:
    if (not isinstance(value, SavedExportNote) or value.context != context or value.note_id != note_id
            or type(value.saved_revision) is not int or not 1 <= value.saved_revision <= _MAX
            or type(value.collection_revision) is not int or not 1 <= value.collection_revision <= _MAX
            or not isinstance(value.saved_name, str) or not 1 <= len(value.saved_name) <= 100
            or not isinstance(value.saved_body, str) or type(value.references) is not tuple):
        raise OutputFailure("STATE_BLOCKED", "The source did not supply a consistent saved-note projection.")
    try:
        # Dataclass equality alone equates 4.0 with 4; the C boundary still needs
        # the same strict context validation as a request or session capture.
        validate_context(value.context)
    except OutputFailure as error:
        raise OutputFailure("STATE_BLOCKED", "The saved projection contains an invalid source context.") from error
    try:
        # This validates saved-name Unicode/control shape even with a selected
        # export filename. It never changes the note's stored business name.
        default_export_name(value.saved_name)
        value.saved_body.encode("utf-8", errors="strict")
        for reference in value.references:
            if not isinstance(reference, ExportReference) or not isinstance(reference.marker_text, str):
                raise ValueError("Invalid authorized reference")
            marker = _MARKER.fullmatch(reference.marker_text)
            if marker is None or not isinstance(reference.kind, ReferenceKind):
                raise ValueError("Invalid classified marker")
            belongs = marker.group(1) == context.collection_id
            foreign = reference.kind is ReferenceKind.FOREIGN_COLLECTION
            if belongs == foreign:
                raise ValueError("Marker ownership disagrees with supplied classification")
    except (TypeError, ValueError, UnicodeError) as error:
        raise OutputFailure("STATE_BLOCKED", "The saved projection contains invalid text or reference ownership.") from error
    return value


def _directory(path: Path) -> Path:
    try:
        canonical = path.resolve(strict=True)
        if not canonical.is_dir():
            raise NotADirectoryError("Selected output parent is not a directory")
        return canonical
    except (OSError, ValueError, RuntimeError) as error:
        raise OutputFailure("IO_FAILURE", "The selected export directory is unavailable.",
                            "Choose an existing writable directory; no directory was created.") from error


def _render(note: SavedExportNote) -> bytes:
    try:
        return render_export_markdown(note.saved_body, note.references).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError) as error:
        raise OutputFailure("STATE_BLOCKED", "The authorized saved reference spans are inconsistent.",
                            "Refresh the saved projection; do not guess or reparse references here.") from error


class ExportService:
    """D's side of API07 using explicit, replaceable B/C/file adapters.

    Production must use the supplied safe publisher (or an equivalent reviewed
    port), not a direct overwrite writer. Each session adapter must bind its
    acknowledgements to this request, not a sticky global ignore-drafts flag.
    This implementation and synthetic tests do not claim Qt/C integration.
    """
    def __init__(self, source: ExportSourcePort, session: OutputSession,
                 *, publisher: Callable[..., PublicationResult] = publish_bytes):
        if not callable(getattr(source, "read_saved", None)):
            raise ValueError("A read-only saved projection source is required.")
        if not callable(getattr(session, "inspect", None)):
            raise ValueError("A captured output-session port is required.")
        if not callable(publisher):
            raise ValueError("A safe file publisher is required.")
        self.source, self.session, self.publisher = source, session, publisher

    async def export_note(self, request: ExportRequest,
                          cancel: Event | None = None) -> OperationResult[ExportReply]:
        context = request.context if isinstance(request, ExportRequest) else CallContext(None, 0)
        try:
            if not isinstance(request, ExportRequest):
                raise OutputFailure("INVALID_ARGUMENT", "Expected an export request.")
            validate_context(context)
            if cancel is not None and not isinstance(cancel, Event):
                raise OutputFailure("INVALID_ARGUMENT", "Expected a cancellation event.")
            signal = cancel if cancel is not None else Event()
            if not isinstance(request.unsaved_choice, UnsavedBackupChoice):
                raise OutputFailure("INVALID_ARGUMENT", "Choose how to handle unsaved edits.")
            directory = absolute_path(request.destination_directory)
            if request.file_name is not None:
                try:
                    selected_name = selected_export_name(request.file_name)
                except (TypeError, ValueError) as error:
                    raise OutputFailure("INVALID_NAME", "Choose a legal Markdown export filename.") from error
            else:
                selected_name = None
            if request.note_id is not None and (not isinstance(request.note_id, str)
                                               or _ID.fullmatch(request.note_id) is None):
                raise OutputFailure("INVALID_ARGUMENT", "Invalid selected note identity.")
            if signal.is_set() or request.unsaved_choice is UnsavedBackupChoice.RETURN_TO_SAVE:
                return result(context, ResultStatus.CANCELLED)
            if request.note_id is None:
                raise OutputFailure("NO_SAVED_VERSION", "A new draft has no saved version to export.",
                                    "Return to the editor and save explicitly before exporting.")

            before = await settle(self.session.inspect(), signal)
            validate_state(before, context)
            validate_choice(before, request.unsaved_choice, request.note_id)
            if signal.is_set():
                return result(context, ResultStatus.CANCELLED)
            directory = await run_io(lambda: _directory(directory), signal)
            if signal.is_set():
                return result(context, ResultStatus.CANCELLED)
            captured = await settle(self.source.read_saved(context, request.note_id, signal), signal)
            if signal.is_set():
                return result(context, ResultStatus.CANCELLED)
            if captured is None:
                raise OutputFailure("NO_SAVED_VERSION", "The selected note has no saved version.",
                                    "Refresh the selection or explicitly save before exporting.")
            note = await run_io(lambda: _projection(captured, context, request.note_id), signal)
            recheck_state(before, await settle(self.session.inspect(), signal), request.unsaved_choice, request.note_id)
            if signal.is_set():
                return result(context, ResultStatus.CANCELLED)
            destination = directory / (selected_name if selected_name is not None else default_export_name(note.saved_name))
            payload = await run_io(lambda: _render(note), signal)
            recheck_state(before, await settle(self.session.inspect(), signal), request.unsaved_choice, request.note_id)
            if signal.is_set():
                return result(context, ResultStatus.CANCELLED)
        except OutputFailure as error:
            # All operations so far are reads or pure rendering: no output file
            # has been attempted. A read-port lost result is not file publication.
            return result(context, ResultStatus.ERROR, error=error.error)
        except Exception:
            return result(context, ResultStatus.ERROR,
                          error=AppError("IO_FAILURE", "The saved export could not be prepared.",
                                         "Refresh the selection and review the source adapter."))

        def reply(state: FilePublicationState, temporary: Path | None = None) -> ExportReply:
            return ExportReply(str(destination), context.collection_id, note.note_id,
                               note.saved_revision, note.collection_revision, state,
                               str(temporary) if temporary is not None else None)

        reported_temporary = None
        try:
            receipt = await run_io(lambda: self.publisher(destination, payload, cancel=signal), signal)
            if (isinstance(receipt, PublicationResult) and isinstance(receipt.temporary_path, Path)
                    and receipt.temporary_path.is_absolute() and receipt.temporary_path != destination):
                # A reported location is an inspection hint only. Even a broken
                # acknowledgement does not grant deletion permission for it.
                reported_temporary = receipt.temporary_path
            if (not isinstance(receipt, PublicationResult) or not isinstance(receipt.status, ResultStatus)
                    or not isinstance(receipt.destination, Path) or not receipt.destination.is_absolute()
                    or receipt.destination != destination
                    or type(receipt.byte_count) is not int or not 0 <= receipt.byte_count <= len(payload)
                    or (receipt.status in {ResultStatus.OK, ResultStatus.CANCELLED} and receipt.error is not None)
                    or (receipt.status in {ResultStatus.ERROR, ResultStatus.UNCONFIRMED}
                        and not isinstance(receipt.error, AppError))
                    or type(receipt.warnings) is not tuple
                    or any(not isinstance(warning, AppError) for warning in receipt.warnings)
                    or (receipt.temporary_path is not None and
                        (not isinstance(receipt.temporary_path, Path) or not receipt.temporary_path.is_absolute()
                         or receipt.temporary_path == destination))):
                raise OutputFailure("IO_FAILURE", "The publisher did not return a valid receipt.", unknown=True)
            warnings = tuple(file_error(warning) for warning in receipt.warnings)
            if receipt.status is ResultStatus.OK:
                if type(receipt.byte_count) is not int or receipt.byte_count != len(payload) or receipt.error is not None:
                    raise OutputFailure("IO_FAILURE", "The publication receipt is inconsistent.", unknown=True)
                return result(context, ResultStatus.OK, reply(FilePublicationState.PUBLISHED, receipt.temporary_path),
                              warnings=warnings)
            if receipt.status in {ResultStatus.ERROR, ResultStatus.CANCELLED}:
                return result(context, receipt.status,
                              error=file_error(receipt.error) if receipt.status is ResultStatus.ERROR else None,
                              warnings=warnings)
            if receipt.status is ResultStatus.UNCONFIRMED:
                return result(context, ResultStatus.UNCONFIRMED, reply(FilePublicationState.UNKNOWN, receipt.temporary_path),
                              error=file_error(receipt.error), warnings=warnings)
            raise OutputFailure("IO_FAILURE", "Unsupported publication outcome.", unknown=True)
        except Exception:
            # The invocation crossed the publication boundary. Missing or broken
            # acknowledgement never proves rollback, even if cancellation arrived.
            return result(context, ResultStatus.UNCONFIRMED, reply(FilePublicationState.UNKNOWN, reported_temporary),
                          error=AppError("IO_FAILURE", "Export publication could not be confirmed.",
                                         "Inspect the output location; do not delete or automatically retry."))
