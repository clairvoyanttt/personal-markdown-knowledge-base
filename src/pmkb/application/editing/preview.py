"""B's versioned API06 consumer seam; renderer must use C's shared parser.

No default parser is supplied. These internal objects are a proposal for the
C/B adapter, not a claim that a common ParsedDocument contract is approved.
"""
from dataclasses import dataclass
from threading import Event
from typing import Protocol

from pmkb.application.editing.service import EditingFailure, EditingService
from pmkb.contracts.common import AppError, CallContext, OperationResult, ResultStatus
from pmkb.contracts.notes import DraftSnapshot


@dataclass(frozen=True, slots=True)
class PreviewContent:
    buffer_id: str
    edit_version: int
    html: str


class PreviewRenderer(Protocol):
    # Adapter consumes C's unified parse/reference result; no separate parser.
    # HTML must be escaped at the shared AST presentation boundary. No scripts,
    # remote loads or implicit saving. Core rendering is entirely offline.
    async def render(self, context: CallContext, snapshot: DraftSnapshot, cancel: Event) -> str: ...


class PreviewService:
    def __init__(self, editing: EditingService, renderer: PreviewRenderer | None = None):
        self.editing, self.renderer = editing, renderer

    async def preview(self, context, buffer_id, expected_edit_version, cancel=None):
        try:
            signal = self.editing._signal(cancel)
            with self.editing.lock:
                self.editing._context(context)
                snapshot = self.editing.view(buffer_id).snapshot
                if type(expected_edit_version) is not int or snapshot.edit_version != expected_edit_version:
                    raise EditingFailure('REVISION_CONFLICT', '预览请求的草稿已变化。')
            if self.renderer is None:
                raise EditingFailure('STATE_BLOCKED', 'C 的统一解析/预览适配尚未接入。')
            if signal.is_set():
                return OperationResult(ResultStatus.CANCELLED, context, context, None, None)
            html = await self.renderer.render(context, snapshot, signal)
            if not isinstance(html, str):
                raise EditingFailure('IO_FAILURE', '预览服务返回形状无效。')
            if signal.is_set():
                return OperationResult(ResultStatus.CANCELLED, context, context, None, None)
            content = PreviewContent(buffer_id, snapshot.edit_version, html)
            return OperationResult(ResultStatus.OK, context, context, content, None)
        except EditingFailure as error:
            return self.editing._failure(context, error)
        except Exception:
            return self.editing._failure(context, EditingFailure('IO_FAILURE', '预览失败，草稿未改变。'))
