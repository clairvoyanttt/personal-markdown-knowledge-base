"""Development/CLI session with NO editor. Never use it for an actual dirty UI."""
from pmkb.application.collection.context_gate import ActiveContextGate
from pmkb.application.collection.ports import CollectionFailure


class HeadlessCollectionSession:
    def __init__(self) -> None:
        self.gate = ActiveContextGate()

    @property
    def context(self):
        return self.gate.current

    @property
    def has_unconfirmed_save(self):
        return False

    def buffers(self):
        return ()

    async def save_buffer(self, buffer_id, edit_version, cancel):
        raise CollectionFailure("STATE_BLOCKED", "No editor is attached to this development session.")

    async def activate(self, expected, target, expected_buffers, discard_ids, cancel_signals=()):
        if expected_buffers or discard_ids or any(signal.is_set() for signal in cancel_signals):
            return None
        accepted, actual = self.gate.try_activate(expected, target.collection_id)
        return actual if accepted else None
