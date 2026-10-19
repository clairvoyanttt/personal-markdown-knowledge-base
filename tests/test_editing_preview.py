import asyncio
import unittest
from pmkb.application.collection.context_gate import ActiveContextGate
from pmkb.application.editing.service import EditingService
from pmkb.application.editing.preview import PreviewService
from pmkb.contracts.notes import CreateDraftRequest, ApplyDraftEditRequest
from pmkb.contracts.common import ResultStatus


class PreviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        gate = ActiveContextGate()
        gate.try_activate(gate.current, 'a' * 32)
        self.editing = EditingService(gate)
        self.ctx = gate.current
        self.view = self.editing.create(CreateDraftRequest(self.ctx, 'name')).value

    async def test_missing_shared_parser_is_error_not_fake_render(self):
        s = self.view.snapshot
        r = await PreviewService(self.editing).preview(self.ctx, s.buffer_id, s.edit_version)
        self.assertEqual(r.status, ResultStatus.ERROR)
        self.assertEqual(r.error.code, 'STATE_BLOCKED')
        self.assertTrue(self.editing.view(s.buffer_id).is_dirty)

    async def test_renderer_receives_frozen_current_draft_and_does_not_save(self):
        seen = []
        class Renderer:
            async def render(self, context, snapshot, cancel):
                seen.append(snapshot)
                return '<p>test adapter output</p>'
        s = self.view.snapshot
        r = await PreviewService(self.editing, Renderer()).preview(self.ctx, s.buffer_id, s.edit_version)
        self.assertEqual(r.value.edit_version, 0)
        self.assertEqual(seen, [s])
        self.assertTrue(self.editing.view(s.buffer_id).is_dirty)
        self.assertIsNone(self.editing.pending)

    async def test_stale_preview_version_is_rejected_before_renderer(self):
        s = self.view.snapshot
        self.editing.edit(ApplyDraftEditRequest(self.ctx, s.buffer_id, 0, s.name, '# newer', ()))
        r = await PreviewService(self.editing).preview(self.ctx, s.buffer_id, 0)
        self.assertEqual(r.error.code, 'REVISION_CONFLICT')
