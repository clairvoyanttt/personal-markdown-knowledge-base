"""API02/API11 in-memory orchestration; no SQL, Qt or pretend persistence.

The RLock protects snapshots read from A's worker and edits from the Qt thread.
It is never held across await. All persistence attempts remain tracked until a
settled result/evidence exists, including when an observer cancels its task.
"""
import asyncio
from dataclasses import dataclass, replace
import re
import secrets
from threading import Event, RLock
from types import MappingProxyType

from pmkb.application.collection.context_gate import ActiveContextGate
from pmkb.application.collection.ports import BufferState
from pmkb.application.editing.ports import (
    EditingStorage, SavedNote, SaveAttempt, SaveEvidence, SaveEvidenceState,
)
from pmkb.contracts.common import AppError, CallContext, OperationResult, ResultStatus
from pmkb.contracts.notes import (
    ApplyDraftEditRequest, CreateDraftRequest, DraftSnapshot, DraftView,
    LoadNoteRequest, SaveNoteRequest, SaveReply, SaveState,
)
from pmkb.contracts.commit_resolution import (
    CheckCommitRequest, CloseUnconfirmedRequest, CloseUnconfirmedReply,
    CommitCheckReply, CommitCheckState,
)

MAX_VERSION = (1 << 63) - 1
# Python str.strip's default set includes additional controls; use design §7.
TRIM = ' \u0085\u00a0\u1680' + ''.join(chr(i) for i in range(0x2000, 0x200b)) + '\u202f\u205f\u3000'


class EditingFailure(Exception):
    def __init__(self, code, reason, action='请检查输入及当前状态后重试。'):
        self.error = AppError(code, reason, action)
        super().__init__(reason)


def identifier(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{32}', value) is not None


def version(value, minimum=0):
    return type(value) is int and minimum <= value <= MAX_VERSION


def single_line(value, limit, code):
    """B input acceptance only. C must revalidate inside the transaction.

    Kept private pending C's shared validator; no name uniqueness lookup here.
    """
    if (not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127
            or c in '\u2028\u2029' or 0xd800 <= ord(c) <= 0xdfff for c in value)):
        raise EditingFailure(code, '输入包含控制字符、换行或无效 Unicode。')
    result = value.strip(TRIM)
    if not 1 <= len(result) <= limit:
        raise EditingFailure(code, f'去除首尾空白后须为 1—{limit} 个字符。')
    return result


def accepted_tags(tags):
    if not isinstance(tags, (tuple, list)):
        raise EditingFailure('INVALID_TAG', '标签必须是字符串列表。')
    result = tuple(single_line(t, 30, 'INVALID_TAG') for t in tags)
    if len(result) != len(set(result)):
        raise EditingFailure('INVALID_TAG', '标签已存在，请保留原标签列表。')
    if len(result) > 20:
        raise EditingFailure('TAG_LIMIT', '每篇笔记最多接受 20 个不同标签。')
    return result


@dataclass
class _Buffer:
    snapshot: DraftSnapshot
    confirmed_edit_version: int | None

    def view(self):
        return DraftView(self.snapshot, self.confirmed_edit_version != self.snapshot.edit_version)


class EditingService:
    def __init__(self, gate: ActiveContextGate, storage: EditingStorage | None = None):
        self.gate, self.storage = gate, storage
        self.lock = RLock()
        self._buffers: dict[str, _Buffer] = {}
        self._commands: set[str] = set()
        self._pending: SaveAttempt | None = None
        self._inflight = False
        self._checking = False
        self._exit_authorization: CloseUnconfirmedRequest | None = None

    @property
    def context(self):
        return self.gate.current

    @property
    def has_unconfirmed_save(self):
        with self.lock:
            return self._pending is not None and not self._inflight

    @property
    def busy(self):
        with self.lock:
            return self._inflight or self._checking

    @property
    def pending(self):
        with self.lock:
            return self._pending

    def views(self):
        with self.lock:
            return tuple(b.view() for b in self._buffers.values())

    def view(self, buffer_id):
        with self.lock:
            return self._buffer(buffer_id).view()

    def buffers(self):
        return tuple(sorted((BufferState(v.snapshot.buffer_id, v.snapshot.edit_version, v.is_dirty)
                             for v in self.views()), key=lambda b: b.buffer_id))

    def _context(self, context):
        if not ActiveContextGate._valid_context(context):
            raise EditingFailure('INVALID_ARGUMENT', '集合上下文无效。')
        if not self.gate.is_current(context):
            raise EditingFailure('STALE_CONTEXT', '集合已经切换，请重新发起操作。')
        if context.collection_id is None:
            raise EditingFailure('STATE_BLOCKED', '请先创建或打开集合。')

    def _buffer(self, buffer_id):
        if not identifier(buffer_id):
            raise EditingFailure('INVALID_ARGUMENT', '草稿标识无效。')
        if buffer_id not in self._buffers:
            raise EditingFailure('INVALID_ARGUMENT', '本会话没有该草稿。')
        return self._buffers[buffer_id]

    def _result(self, context, status, value=None, error=None):
        return OperationResult(status, context, context, value, error)

    def _failure(self, context, error):
        return self._result(context, ResultStatus.ERROR, error=error.error)

    def _validate_request(self, request, cls):
        if not isinstance(request, cls):
            raise EditingFailure('INVALID_ARGUMENT', '请求类型无效。')
        self._context(request.context)

    @staticmethod
    def _signal(cancel):
        if cancel is not None and not isinstance(cancel, Event):
            raise EditingFailure('INVALID_ARGUMENT', '取消信号类型无效。')
        return cancel if cancel is not None else Event()

    async def create_draft(self, request, cancel=None):
        return self.create(request, cancel)

    def create(self, request, cancel=None):
        context = getattr(request, 'context', self.context)
        try:
            with self.lock:
                self._validate_request(request, CreateDraftRequest)
                signal = self._signal(cancel)
                if not isinstance(request.initial_name, str):
                    raise EditingFailure('INVALID_ARGUMENT', '初始名称必须是字符串。')
                if signal.is_set():
                    return self._result(context, ResultStatus.CANCELLED)
                snapshot = DraftSnapshot(secrets.token_hex(16), None, None, 0, request.initial_name, '', ())
                self._buffers[snapshot.buffer_id] = _Buffer(snapshot, None)
                return self._result(context, ResultStatus.OK, self.view(snapshot.buffer_id))
        except EditingFailure as error:
            return self._failure(context, error)

    async def apply_draft_edit(self, request, cancel=None):
        return self.edit(request, cancel)

    def edit(self, request, cancel=None):
        context = getattr(request, 'context', self.context)
        try:
            with self.lock:
                self._validate_request(request, ApplyDraftEditRequest)
                signal = self._signal(cancel)
                if (not version(request.expected_edit_version) or not isinstance(request.name, str)
                        or not isinstance(request.body, str)):
                    raise EditingFailure('INVALID_ARGUMENT', '编辑版本、名称或正文类型无效。')
                buffer = self._buffer(request.buffer_id)
                if buffer.snapshot.edit_version != request.expected_edit_version:
                    raise EditingFailure('REVISION_CONFLICT', '草稿已变化，保留当前输入。')
                if buffer.snapshot.edit_version == MAX_VERSION:
                    raise EditingFailure('STATE_BLOCKED', '编辑版本已达到上限，不能回绕。')
                tags = accepted_tags(request.tags)
                if signal.is_set():
                    return self._result(context, ResultStatus.CANCELLED)
                buffer.snapshot = replace(buffer.snapshot, edit_version=buffer.snapshot.edit_version + 1,
                                          name=request.name, body=request.body, tags=tags)
                return self._result(context, ResultStatus.OK, buffer.view())
        except EditingFailure as error:
            return self._failure(context, error)

    async def load_note(self, request, cancel=None):
        context = getattr(request, 'context', self.context)
        try:
            with self.lock:
                self._validate_request(request, LoadNoteRequest)
                signal = self._signal(cancel)
                if not identifier(request.note_id):
                    raise EditingFailure('INVALID_ARGUMENT', '笔记标识无效。')
                for buffer in self._buffers.values():
                    if buffer.snapshot.note_id == request.note_id:
                        return self._result(context, ResultStatus.OK, buffer.view())
                self._readable()
                if signal.is_set():
                    return self._result(context, ResultStatus.CANCELLED)
            note = await self.storage.load_note(context, request.note_id, signal)
            with self.lock:
                self._context(context)
                self._readable()
                if signal.is_set():
                    return self._result(context, ResultStatus.CANCELLED)
                # A concurrent open must return the existing buffer, not reset it.
                for buffer in self._buffers.values():
                    if buffer.snapshot.note_id == request.note_id:
                        return self._result(context, ResultStatus.OK, buffer.view())
                if note is None:
                    raise EditingFailure('NOTE_NOT_FOUND', '保存版不存在或已删除。')
                if (not isinstance(note, SavedNote) or note.note_id != request.note_id
                        or not version(note.revision, 1) or not isinstance(note.body, str)):
                    raise EditingFailure('IO_FAILURE', '存储服务返回了无效的保存版。')
                if single_line(note.name, 100, 'INVALID_NAME') != note.name:
                    raise EditingFailure('IO_FAILURE', '保存版名称不符合规范，未改写原数据。')
                tags = accepted_tags(note.tags)
                if type(note.tags) is not tuple or tags != note.tags:
                    raise EditingFailure('IO_FAILURE', '保存版标签不符合规范，未改写原数据。')
                if any(0xd800 <= ord(c) <= 0xdfff for c in note.body):
                    raise EditingFailure('IO_FAILURE', '保存版正文包含无效 Unicode，未加载为已保存草稿。')
                snapshot = DraftSnapshot(secrets.token_hex(16), note.note_id, note.revision,
                                         0, note.name, note.body, tags)
                self._buffers[snapshot.buffer_id] = _Buffer(snapshot, 0)
                return self._result(context, ResultStatus.OK, self.view(snapshot.buffer_id))
        except EditingFailure as error:
            return self._failure(context, error)
        except Exception:
            return self._failure(context, EditingFailure('IO_FAILURE', '读取保存版失败，当前草稿未改变。'))

    def _readable(self):
        if self._pending is not None:
            raise EditingFailure('STATE_BLOCKED', '保存正在进行或待核实，请先等待或核实原命令。')
        if self.storage is None:
            raise EditingFailure('STATE_BLOCKED', 'C 的存储适配尚未接入，不能读取或持久化笔记。')

    @staticmethod
    async def _settle(awaitable, signal):
        task = asyncio.ensure_future(awaitable)
        while True:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                signal.set()
                if task.cancelled():
                    raise EditingFailure('COMMIT_UNCONFIRMED', '底层操作未提供确定结果。')

    def _matches_evidence(self, attempt, evidence):
        if (not isinstance(evidence, SaveEvidence) or evidence.context != attempt.context
                or not ActiveContextGate._valid_context(evidence.context)
                or evidence.command_id != attempt.command_id or evidence.note_id != attempt.note_id
                or not isinstance(evidence.snapshot, DraftSnapshot)
                or not version(evidence.snapshot.edit_version)
                or (evidence.snapshot.base_revision is not None and not version(evidence.snapshot.base_revision, 1))
                or evidence.snapshot != attempt.snapshot or not isinstance(evidence.state, SaveEvidenceState)
                or (evidence.error is not None and (not isinstance(evidence.error, AppError)
                    or not all(isinstance(v, str) and v.strip() for v in (
                        evidence.error.code, evidence.error.reason, evidence.error.suggested_action))))):
            return False
        if evidence.state == SaveEvidenceState.COMMITTED:
            return (version(evidence.saved_revision, 1) and version(evidence.collection_revision, 1)
                    and evidence.saved_revision == (attempt.snapshot.base_revision or 0) + 1
                    and evidence.error is None)
        return evidence.saved_revision is None and evidence.collection_revision is None

    def _confirm(self, attempt, evidence):
        buffer = self._buffer(attempt.snapshot.buffer_id)
        # Only saved identity/base advance. Later editor text and version survive.
        buffer.snapshot = replace(buffer.snapshot, note_id=attempt.note_id, base_revision=evidence.saved_revision)
        buffer.confirmed_edit_version = attempt.snapshot.edit_version

    def _unknown_reply(self, attempt):
        return SaveReply(attempt.command_id, attempt.note_id, attempt.snapshot.edit_version,
                         SaveState.PENDING, None, None, attempt.pending_save_id)

    async def save_note(self, request, cancel=None):
        context = getattr(request, 'context', self.context)
        try:
            with self.lock:
                self._validate_request(request, SaveNoteRequest)
                signal = self._signal(cancel)
                self._readable()
                if not identifier(request.command_id) or not isinstance(request.snapshot, DraftSnapshot):
                    raise EditingFailure('INVALID_ARGUMENT', '保存命令或快照无效。')
                snapshot = request.snapshot
                if (not version(snapshot.edit_version)
                        or ((snapshot.note_id is None) != (snapshot.base_revision is None))
                        or (snapshot.note_id is not None and (not identifier(snapshot.note_id)
                            or not version(snapshot.base_revision, 1)))
                        or not isinstance(snapshot.name, str) or not isinstance(snapshot.body, str)
                        or type(snapshot.tags) is not tuple):
                    raise EditingFailure('INVALID_ARGUMENT', '快照字段类型或身份/保存版本配对无效。')
                buffer = self._buffer(snapshot.buffer_id)
                if request.command_id in self._commands or snapshot != buffer.snapshot:
                    raise EditingFailure('REVISION_CONFLICT', '命令已使用或草稿快照已变化，请重新捕获。')
                single_line(snapshot.name, 100, 'INVALID_NAME')
                accepted_tags(snapshot.tags)
                if any(0xd800 <= ord(c) <= 0xdfff for c in snapshot.body):
                    raise EditingFailure('INVALID_ARGUMENT', '正文包含无配对代理项，请修正后保存。')
                if snapshot.base_revision == MAX_VERSION:
                    raise EditingFailure('STATE_BLOCKED', '保存版本达到上限，不能回绕。')
                if signal.is_set():
                    return self._result(context, ResultStatus.CANCELLED)
                attempt = SaveAttempt(context, request.command_id, secrets.token_hex(16),
                                      snapshot.note_id or secrets.token_hex(16), snapshot)
                self._pending, self._inflight = attempt, True
                self._commands.add(request.command_id)
        except EditingFailure as error:
            return self._failure(context, error)
        try:
            evidence = await self._settle(self.storage.save(attempt, signal), signal)
        except Exception:
            evidence = None  # Dispatch happened: failure to observe is NOT rollback proof.
        with self.lock:
            self._inflight = False
            if self._matches_evidence(attempt, evidence):
                if evidence.state == SaveEvidenceState.COMMITTED:
                    self._confirm(attempt, evidence)
                    self._pending = None
                    return self._result(context, ResultStatus.OK, SaveReply(attempt.command_id,
                        attempt.note_id, attempt.snapshot.edit_version, SaveState.COMMITTED,
                        evidence.saved_revision, evidence.collection_revision, None))
                if evidence.state == SaveEvidenceState.NOT_COMMITTED:
                    self._pending = None
                    return self._result(context, ResultStatus.ERROR, error=evidence.error or
                        AppError('IO_FAILURE', '存储服务已确认本次未提交；草稿保留。', '检查原因后重新保存。'))
            return self._result(context, ResultStatus.UNCONFIRMED, self._unknown_reply(attempt),
                AppError('COMMIT_UNCONFIRMED', '保存结果尚未核实；未把当前输入标记为已保存。', '核实原保存命令，不要重复写入。'))

    def _pending_request(self, request, cls):
        self._validate_request(request, cls)
        if (not identifier(request.pending_save_id) or not identifier(request.command_id)
                or self._pending is None or self._inflight or self._checking
                or request.pending_save_id != self._pending.pending_save_id
                or request.command_id != self._pending.command_id):
            raise EditingFailure('STATE_BLOCKED', '没有匹配且已结束底层调用的待核实保存。')
        return self._pending

    async def check_commit(self, request, cancel=None):
        context = getattr(request, 'context', self.context)
        try:
            with self.lock:
                attempt = self._pending_request(request, CheckCommitRequest)
                signal = self._signal(cancel)
                if signal.is_set():
                    return self._result(context, ResultStatus.CANCELLED)
                self._checking = True
        except EditingFailure as error:
            return self._failure(context, error)
        try:
            evidence = await self._settle(self.storage.check_save(attempt, signal), signal)
        except Exception:
            evidence = None
        with self.lock:
            self._checking = False
            state = CommitCheckState.UNRESOLVED
            if self._matches_evidence(attempt, evidence):
                state = CommitCheckState(evidence.state.value)
                if state == CommitCheckState.COMMITTED:
                    self._confirm(attempt, evidence)
                if state != CommitCheckState.UNRESOLVED:
                    self._pending = None
            resolved = state != CommitCheckState.UNRESOLVED
            if not resolved and signal.is_set():
                # Cancelling a read/check does not resolve or roll back SAVE.
                return self._result(context, ResultStatus.CANCELLED)
            reply = CommitCheckReply(attempt.command_id, attempt.note_id, attempt.snapshot.edit_version,
                state, evidence.saved_revision if resolved else None,
                evidence.collection_revision if resolved else None)
            return self._result(context, ResultStatus.OK if resolved else ResultStatus.UNCONFIRMED,
                reply, None if resolved else AppError('COMMIT_UNCONFIRMED', '现有证据不足，保存仍待核实。', '保留会话，稍后核实。'))

    def _exit_versions_current(self, request):
        with self.lock:
            try:
                self._pending_request(request, CloseUnconfirmedRequest)
                if request.acknowledge_commit_unknown is not True or request.discard_session_edits is not True:
                    return False
                versions = dict(request.confirmed_draft_edit_versions)
                return (all(identifier(k) and version(v) for k, v in versions.items())
                        and versions == {v.snapshot.buffer_id: v.snapshot.edit_version for v in self.views()})
            except (EditingFailure, TypeError, ValueError):
                return False

    def exit_permit_current(self, request):
        with self.lock:
            return (self._exit_authorization is not None and request == self._exit_authorization
                    and self._exit_versions_current(self._exit_authorization))

    async def close_unconfirmed_session(self, request, cancel=None):
        context = getattr(request, 'context', self.context)
        try:
            with self.lock:
                self._pending_request(request, CloseUnconfirmedRequest)
                signal = self._signal(cancel)
                if signal.is_set():
                    return self._result(context, ResultStatus.CANCELLED)
                if not self._exit_versions_current(request):
                    raise EditingFailure('STATE_BLOCKED', '须明确确认未知提交风险及全部最新草稿版本。',
                                         '重新确认；退出后不保证恢复内存草稿。')
                self._exit_authorization = replace(request, confirmed_draft_edit_versions=
                    MappingProxyType(dict(request.confirmed_draft_edit_versions)))
                return self._result(context, ResultStatus.OK, CloseUnconfirmedReply(True, True,
                    tuple(v.snapshot.buffer_id for v in self.views())))
        except EditingFailure as error:
            return self._failure(context, error)

    def install_collection(self, expected, target, expected_buffers, discard_ids, cancel_signals=()):
        """Invoke on Qt thread. Validate and mutate under one editor-state lock.

        No IO/UI user callbacks between the last version check and gate install.
        The caller refreshes widgets from this immutable new state afterwards.
        """
        with self.lock:
            if (self.busy or self._pending is not None or expected != self.context
                    or any(s.is_set() for s in cancel_signals)
                    or tuple(sorted(expected_buffers, key=lambda b: b.buffer_id)) != self.buffers()):
                return None
            dirty = {b.buffer_id for b in self.buffers() if b.is_dirty}
            if len(discard_ids) != len(set(discard_ids)) or set(discard_ids) != dirty:
                return None
            installed, context = self.gate.try_activate(expected, target.collection_id)
            if not installed:
                return None
            self._buffers.clear()
            return context
