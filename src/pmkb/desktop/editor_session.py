"""B's real in-memory editor session for A's collection coordinator."""
import secrets

from pmkb.application.editing.service import EditingService, EditingFailure
from pmkb.contracts.common import ResultStatus
from pmkb.contracts.notes import SaveNoteRequest


class EditorSession:
    def __init__(self, editing: EditingService, dispatcher):
        self.editing, self.dispatcher = editing, dispatcher
        self.directory: str | None = None

    @property
    def context(self):
        return self.editing.context

    @property
    def has_unconfirmed_save(self):
        # A must not prepare a switch during a dispatched write either.
        return self.editing.has_unconfirmed_save or self.editing.busy

    def buffers(self):
        return self.editing.buffers()

    async def save_buffer(self, buffer_id, edit_version, cancel=None):
        with self.editing.lock:
            context = self.context
            try:
                snapshot = self.editing.view(buffer_id).snapshot
                if type(edit_version) is not int or snapshot.edit_version != edit_version:
                    raise EditingFailure('REVISION_CONFLICT', '离开选择之后草稿已变化，请重新确认。')
            except EditingFailure as error:
                return self.editing._result(context, ResultStatus.ERROR, error=error.error)
        return await self.editing.save_note(SaveNoteRequest(context, secrets.token_hex(16), snapshot), cancel)

    async def activate(self, expected, target, expected_buffers, discard_ids, cancel_signals=()):
        def install():
            context = self.editing.install_collection(expected, target, expected_buffers,
                                                      discard_ids, cancel_signals)
            if context is not None:
                self.directory = target.directory
            return context
        return await self.dispatcher.invoke(install)
