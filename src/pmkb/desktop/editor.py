"""Qt editor consuming application services. Unavailable providers stay explicit."""
from pathlib import Path
import secrets

from PySide6.QtCore import Qt, QSignalBlocker, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QTextBrowser,
    QVBoxLayout, QWidget,
)

from pmkb.application.collection.context_gate import ActiveContextGate
from pmkb.application.editing.service import EditingService, EditingFailure, single_line
from pmkb.application.editing.preview import PreviewService
from pmkb.application.editing.text_edit import apply_widget_text
from pmkb.contracts.collections import (
    ActivationMode, CancelActivationRequest, CompleteActivationRequest,
    DraftLeaveDecision, LeaveChoice, PrepareActivationRequest,
)
from pmkb.contracts.common import ResultStatus
from pmkb.contracts.notes import ApplyDraftEditRequest, CreateDraftRequest, SaveNoteRequest
from pmkb.contracts.commit_resolution import (
    CheckCommitRequest, CloseUnconfirmedRequest, CommitCheckReply, CommitCheckState,
)
from pmkb.desktop.app import StartupWindow
from pmkb.desktop.composition import ApplicationServices
from pmkb.desktop.editor_session import EditorSession


class LocalPreview(QTextBrowser):
    """Rendering never fetches remote or local embedded resources implicitly."""
    def loadResource(self, resource_type, name):
        return None


class EditorWindow(StartupWindow):
    def __init__(self, config_file: Path, storage=None, preview_renderer=None):
        config_file = Path(config_file)
        if not config_file.is_absolute():
            raise ValueError('The application must supply an absolute configuration path.')
        super().__init__()
        self.editing = EditingService(ActiveContextGate(), storage)
        self.session = EditorSession(self.editing, self.dispatcher,
                                     install_view=self._install_collection_view)
        self.services = ApplicationServices.compose(self.session, config_file, self.runtime)
        self.previews = PreviewService(self.editing, preview_renderer)
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(120)
        self._preview_timer.timeout.connect(self._request_preview)
        self._preview_key = None
        self._active_buffer: str | None = None
        self._switching = False
        self._exit_waiting = False
        self._exit_polling = False
        self._unknown_exit = None
        self._draft_text_inputs: dict[str, str] = {}
        self.setWindowTitle('个人 Markdown 知识库 · B 编辑开发版')
        self.resize(1180, 760)
        self._build_ui()
        self.cancel_exit_button = QPushButton('取消退出', self)
        self.cancel_exit_button.clicked.connect(self._cancel_exit)
        self.statusBar().addPermanentWidget(self.cancel_exit_button)
        self.cancel_exit_button.hide()
        self._refresh()

    def _build_ui(self):
        root = QWidget()
        layout = QVBoxLayout(root)
        row = QHBoxLayout()
        self.location = QLabel('尚未打开集合')
        self.location.setTextFormat(Qt.TextFormat.PlainText)
        self.location.setWordWrap(True)
        for title, callback in [('新建集合', lambda: self.choose_collection(ActivationMode.CREATE)),
                                ('打开 / 切换集合', lambda: self.choose_collection(ActivationMode.OPEN))]:
            button = QPushButton(title)
            button.clicked.connect(callback)
            row.addWidget(button)
        row.addWidget(self.location, 1)
        layout.addLayout(row)
        self.notice = QLabel('开发状态：内存草稿可编辑；真实保存、查询与预览须接入 C 服务。')
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        splitter = QSplitter()
        left = QWidget()
        left_layout = QVBoxLayout(left)
        self.new_button = QPushButton('新建草稿')
        self.new_button.clicked.connect(self.new_draft)
        left_layout.addWidget(self.new_button)
        left_layout.addWidget(QLabel('会话草稿（含未保存输入）'))
        self.drafts = QListWidget()
        self.drafts.currentItemChanged.connect(self._select_draft)
        left_layout.addWidget(self.drafts)
        left_layout.addWidget(QLabel('保存版列表 / 搜索 / 标签筛选'))
        unavailable = QLabel('等待 C 的保存版查询接口；不使用草稿冒充查询结果。')
        unavailable.setWordWrap(True)
        left_layout.addWidget(unavailable)
        splitter.addWidget(left)
        center = QWidget()
        form = QVBoxLayout(center)
        form.addWidget(QLabel('名称（改名需手动保存）'))
        self.name = QLineEdit()
        self.name.textEdited.connect(self._apply_name)
        form.addWidget(self.name)
        self.body = QPlainTextEdit()
        self.body.setPlaceholderText('Markdown 原文；当前输入仅存在于内存中。')
        self.body.textChanged.connect(self._apply_text)
        form.addWidget(self.body, 1)
        tags_row = QHBoxLayout()
        self.tag_input = QLineEdit()
        self.tag_input.setPlaceholderText('输入单个标签，回车添加；大小写区分')
        self.tag_input.returnPressed.connect(self.add_tag)
        self.tag_input.textEdited.connect(self._remember_tag_input)
        tags_row.addWidget(self.tag_input)
        add = QPushButton('添加标签')
        add.clicked.connect(self.add_tag)
        tags_row.addWidget(add)
        remove = QPushButton('移除选中标签')
        remove.clicked.connect(self.remove_tag)
        tags_row.addWidget(remove)
        form.addLayout(tags_row)
        self.tags = QListWidget()
        self.tags.setMaximumHeight(90)
        form.addWidget(self.tags)
        commands = QHBoxLayout()
        self.save_button = QPushButton('手动保存（Ctrl+S）')
        self.save_button.clicked.connect(self.save_current)
        commands.addWidget(self.save_button)
        self.check_button = QPushButton('核实原保存命令')
        self.check_button.clicked.connect(self.check_pending)
        commands.addWidget(self.check_button)
        self.save_shortcut = QShortcut(QKeySequence.StandardKey.Save, self)
        self.save_shortcut.activated.connect(self.save_current)
        form.addLayout(commands)
        self.state_label = QLabel()
        self.state_label.setTextFormat(Qt.TextFormat.PlainText)
        self.state_label.setWordWrap(True)
        form.addWidget(self.state_label)
        splitter.addWidget(center)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(QLabel('当前草稿预览'))
        self.preview = LocalPreview()
        self.preview.setOpenLinks(False)
        self.preview.setOpenExternalLinks(False)
        self.preview.setPlainText('等待 C 的统一解析接口；尚未实现六类 Markdown 预览验收。')
        right_layout.addWidget(self.preview)
        right_layout.addWidget(QLabel('引用 / 反向引用 / 删除 / 导出 / 备份 / 恢复'))
        pending = QLabel('相应服务及精确契约尚待接入。未保存草稿不会作为保存版输出。')
        pending.setWordWrap(True)
        right_layout.addWidget(pending)
        splitter.addWidget(right)
        splitter.setSizes([240, 560, 360])
        layout.addWidget(splitter, 1)
        self.setCentralWidget(root)

    def _remember_tag_input(self, text):
        if self._active_buffer:
            self._draft_text_inputs[self._active_buffer] = text

    def _install_collection_view(self):
        """Final Qt activation boundary, not a later outcome presentation.

        The old buffers have just been discarded by a successful model install.
        Clear their selection/input state and install the new location and
        disabled empty editors before returning to Qt's event queue. Rejected
        or cancelled activation never calls this method.
        """
        self._active_buffer = None
        self._draft_text_inputs.clear()
        self._preview_timer.stop()
        self._preview_key = None
        # None is also the empty collection's key, so _refresh cannot detect
        # this transition. Clear completed render output at the same boundary.
        self.preview.setPlainText('请选择草稿。' if self.previews.renderer else '等待 C 的统一解析接口。')
        self._refresh(refresh_text=True)

    def _current(self):
        return self.editing.view(self._active_buffer) if self._active_buffer else None

    def _refresh(self, refresh_text=False):
        current = self._active_buffer
        with QSignalBlocker(self.drafts):
            self.drafts.clear()
            selected = None
            for view in self.editing.views():
                s = view.snapshot
                item = QListWidgetItem(f'{"● " if view.is_dirty else ""}{s.name or "（未命名）"}')
                item.setData(Qt.ItemDataRole.UserRole, s.buffer_id)
                self.drafts.addItem(item)
                if s.buffer_id == current:
                    selected = item
            self.drafts.setCurrentItem(selected)
        if selected is None:
            self._active_buffer = None
        active = self.editing.context.collection_id is not None
        self.location.setText(self.session.directory or '尚未打开集合')
        self.new_button.setEnabled(active and not self._exit_waiting and not self._switching)
        self.save_button.setEnabled(self._active_buffer is not None and not self.editing.busy
            and not self.editing.has_unconfirmed_save and not self._switching)
        self.check_button.setEnabled(self.editing.has_unconfirmed_save and not self.editing.busy)
        for widget in (self.name, self.body, self.tag_input, self.tags):
            widget.setEnabled(self._active_buffer is not None and not self._exit_waiting)
        view = self._current()
        if refresh_text:
            with QSignalBlocker(self.name), QSignalBlocker(self.body), QSignalBlocker(self.tag_input):
                self.name.setText(view.snapshot.name if view else '')
                self.body.setPlainText(view.snapshot.body if view else '')
                self.tag_input.setText(self._draft_text_inputs.get(self._active_buffer, ''))
        with QSignalBlocker(self.tags):
            self.tags.clear()
            if view:
                self.tags.addItems(view.snapshot.tags)
        if self.editing.has_unconfirmed_save:
            state = '保存结果待核实；显示最后确认事实，仍可保留内存编辑，禁止重复保存。'
        elif self.editing.busy:
            state = '正在保存 / 核实；当前输入仍可编辑。'
        elif view:
            s = view.snapshot
            state = f'{"未保存" if view.is_dirty else "已确认保存"} · 编辑版本 {s.edit_version} · 最后确认保存版本 {s.base_revision or "无"}'
        else:
            state = '请先创建或打开集合，再新建草稿。'
        self.state_label.setText(state)
        key = (self.editing.context, view.snapshot) if view else None
        if key != self._preview_key:
            self._preview_key = key
            if view and self.previews.renderer is not None and not self._exit_waiting:
                self.preview.setPlainText('正在生成当前草稿预览……')
                self._preview_timer.start()
            elif not view:
                self._preview_timer.stop()
                self.preview.setPlainText('请选择草稿。' if self.previews.renderer else '等待 C 的统一解析接口。')

    def _select_draft(self, item, previous=None):
        self._active_buffer = item.data(Qt.ItemDataRole.UserRole) if item else None
        self._refresh(refresh_text=True)

    def new_draft(self):
        if self._exit_waiting or self._switching:
            return
        result = self.editing.create(CreateDraftRequest(self.editing.context, ''))
        if result.status == ResultStatus.OK:
            self._active_buffer = result.value.snapshot.buffer_id
        self._show_result(result)
        self._refresh(refresh_text=True)

    def _apply_text(self, *unused):
        view = self._current()
        if view is None or self._exit_waiting:
            return
        s = view.snapshot
        result = self.editing.edit(ApplyDraftEditRequest(self.editing.context, s.buffer_id,
            s.edit_version, self.name.text(), apply_widget_text(s.body, self.body.document().toRawText()), s.tags))
        self._show_result(result, quiet=True)
        self._refresh()

    def _apply_name(self, *unused):
        # QPlainTextEdit normalizes CRLF for display. Renaming must not rewrite
        # untouched Markdown (including original line endings) from the widget.
        view = self._current()
        if view is None or self._exit_waiting:
            return
        s = view.snapshot
        result = self.editing.edit(ApplyDraftEditRequest(self.editing.context, s.buffer_id,
            s.edit_version, self.name.text(), s.body, s.tags))
        self._show_result(result, quiet=True)
        self._refresh()

    def _apply_tags(self, tags):
        view = self._current()
        if view is None or self._exit_waiting:
            return False
        s = view.snapshot
        result = self.editing.edit(ApplyDraftEditRequest(self.editing.context, s.buffer_id,
            s.edit_version, s.name, s.body, tags))
        self._show_result(result, quiet=True)
        self._refresh()
        return result.status == ResultStatus.OK

    def add_tag(self):
        view = self._current()
        if view is None:
            return
        try:
            tag = single_line(self.tag_input.text(), 30, 'INVALID_TAG')
            if tag in view.snapshot.tags:
                self.statusBar().showMessage('该标签已存在；原标签及输入保留。')
                return
            if self._apply_tags(view.snapshot.tags + (tag,)):
                self.tag_input.clear()
                self._draft_text_inputs[self._active_buffer] = ''
        except EditingFailure as error:
            self.statusBar().showMessage(error.error.reason)

    def remove_tag(self):
        view = self._current()
        item = self.tags.currentItem()
        if view and item:
            self._apply_tags(tuple(t for t in view.snapshot.tags if t != item.text()))

    def _submit(self, factory, callback, on_error=None):
        """Observe exceptions and deliver only to the active collection on Qt."""
        handle = self.runtime.submit(factory)
        def done(future):
            try:
                result = future.result()  # Already done; no blocking on Qt.
            except BaseException:
                # Runtime exposes CancelledError too; consume it without claiming
                # rollback or letting a Future callback strand the UI state.
                self.dispatcher.post(on_error or self._operation_failed)
            else:
                self.dispatcher.deliver(result, lambda: self.editing.context, callback)
        handle.outcome.add_done_callback(done)
        self._refresh()
        return handle

    def _operation_failed(self):
        self.statusBar().showMessage('操作异常；请核查实际状态，未声称保存或切换成功。')
        self._refresh()

    def _transition_failed(self):
        self._switching = False
        self._operation_failed()

    def _exit_failed(self):
        self._resume_exit()
        self._operation_failed()

    def _show_result(self, result, quiet=False):
        if result.error:
            self.statusBar().showMessage(f'{result.error.code}：{result.error.reason} {result.error.suggested_action}')
        elif not quiet:
            self.statusBar().showMessage('操作完成。' if result.status == ResultStatus.OK else '操作已取消。')

    def save_current(self):
        if self.editing.busy or self._switching or self._exit_waiting or self._closing:
            return  # Includes shortcuts queued during the activation handoff.
        view = self._current()
        if view is None:
            return
        request = SaveNoteRequest(self.editing.context, secrets.token_hex(16), view.snapshot)
        self._submit(lambda cancel: self.editing.save_note(request, cancel), self._saved)

    def _saved(self, result):
        self._show_result(result)
        if result.status == ResultStatus.OK and isinstance(result.value, CommitCheckReply):
            if result.value.state == CommitCheckState.NOT_COMMITTED:
                self.statusBar().showMessage('核实完成：原保存已确认未提交；草稿保留，可手动重新保存。')
            elif result.value.state == CommitCheckState.COMMITTED:
                self.statusBar().showMessage('核实完成：原保存的捕获版本已提交；后续编辑仍须另行保存。')
        self._refresh()

    def _request_preview(self):
        view = self._current()
        if view is None or self._exit_waiting or self._closing:
            return
        s, context = view.snapshot, self.editing.context
        self._submit(lambda cancel: self.previews.preview(context, s.buffer_id, s.edit_version, cancel),
                     lambda result: self._preview_ready(result, (s.buffer_id, s.edit_version)))

    def _preview_ready(self, result, expected=None):
        view = self._current()
        if expected is not None and (view is None or expected != (
                view.snapshot.buffer_id, view.snapshot.edit_version)):
            return
        if result.status != ResultStatus.OK:
            if result.status != ResultStatus.CANCELLED:
                self._show_result(result)
            return
        content = result.value
        if (view is not None and content.buffer_id == view.snapshot.buffer_id
                and type(content.edit_version) is int and content.edit_version == view.snapshot.edit_version):
            self.preview.setHtml(content.html)

    def check_pending(self):
        pending = self.editing.pending
        if pending is None or self.editing.busy:
            return
        request = CheckCommitRequest(self.editing.context, pending.pending_save_id, pending.command_id)
        self._submit(lambda cancel: self.editing.check_commit(request, cancel), self._saved)

    def choose_collection(self, mode):
        if self._exit_waiting or self._switching or self.editing.busy:
            return
        directory = QFileDialog.getExistingDirectory(self, '新建空位置' if mode == ActivationMode.CREATE else '选择集合目录')
        if directory:
            self.open_collection(mode, directory)

    def open_collection(self, mode, directory):
        if self._exit_waiting or self._switching or self.editing.busy:
            return
        self._switching = True
        request = PrepareActivationRequest(self.editing.context, mode, str(Path(directory).resolve()))
        self._submit(lambda cancel: self.services.collections.prepare_activation(request, cancel),
                     self._prepared, self._transition_failed)

    def _ask_leave(self, view):
        dialog = QMessageBox(self)
        dialog.setWindowTitle('未保存草稿')
        dialog.setIcon(QMessageBox.Icon.Question)
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setText(f'草稿“{view.snapshot.name or "未命名"}”有未保存修改。保存、放弃还是取消？')
        dialog.setStandardButtons(QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                                  | QMessageBox.StandardButton.Cancel)
        dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
        answer = dialog.exec()
        dialog.deleteLater()
        return {QMessageBox.StandardButton.Save: LeaveChoice.SAVE,
                QMessageBox.StandardButton.Discard: LeaveChoice.DISCARD}.get(answer, LeaveChoice.CANCEL)

    def _prepared(self, result):
        if result.status != ResultStatus.OK:
            self._switching = False
            self._show_result(result)
            self._refresh()
            return
        ticket = result.value
        decisions = () if self._exit_waiting else tuple(
            DraftLeaveDecision(v.snapshot.buffer_id, v.snapshot.edit_version, self._ask_leave(v))
            for v in self.editing.views() if v.is_dirty)
        if any(d.choice == LeaveChoice.CANCEL for d in decisions) or self._exit_waiting:
            request = CancelActivationRequest(result.delivery_context, ticket.ticket_id)
            self._submit(lambda cancel: self.services.collections.cancel_activation(request, cancel),
                         self._activated, self._transition_failed)
        else:
            request = CompleteActivationRequest(result.delivery_context, ticket.ticket_id, decisions)
            self._submit(lambda cancel: self.services.collections.complete_activation(request, cancel),
                         self._activated, self._transition_failed)

    def _activated(self, result):
        self._switching = False
        self._show_result(result)
        # Model + text/selection/location were already installed together on Qt.
        # A late outcome only changes operation status; it must not replace text.
        self._refresh()

    def _resume_exit(self):
        self._stop_exit_poll()
        self._exit_waiting = False
        self._unknown_exit = None
        self.cancel_exit_button.hide()
        self._preview_key = None
        if self.centralWidget():
            self.centralWidget().setEnabled(True)
        self._refresh()

    def _cancel_exit(self):
        if self._exit_waiting and not self._closing:
            self._resume_exit()
            self.statusBar().showMessage('已取消退出；后台操作仍按真实结果收尾，草稿保留。')

    def _stop_exit_poll(self):
        if self._exit_polling:
            self._poll.stop()
            self._poll.timeout.disconnect(self._settle_exit)
            self._poll.timeout.connect(self._check_shutdown)
            self._exit_polling = False

    def closeEvent(self, event):
        if self._closed_runtime or self._closing:
            super().closeEvent(event)
            return
        event.ignore()
        if self._exit_waiting:
            return
        self._exit_waiting = True
        self.cancel_exit_button.show()
        self.statusBar().showMessage('正在等待后台操作及结果收尾；可取消退出，取消不代表已回滚。')
        self._preview_timer.stop()
        self.centralWidget().setEnabled(False)
        self.runtime.request_cancel_all()
        self._poll.timeout.disconnect(self._check_shutdown)
        self._poll.timeout.connect(self._settle_exit)
        self._exit_polling = True
        self._poll.start()

    def _settle_exit(self):
        if not self._exit_waiting:
            self._stop_exit_poll()
            return
        if self.runtime.pending_count or self.dispatcher.pending_count or self.editing.busy or self._switching:
            return
        self._stop_exit_poll()
        pending = self.editing.pending
        if pending is not None:
            confirmed_versions = {v.snapshot.buffer_id: v.snapshot.edit_version for v in self.editing.views()}
            # Both risks must be explicit; cancellation is the default.
            answer = QMessageBox.warning(self, '保存提交未知',
                '原保存可能已经提交，也可能没有提交。退出会放弃全部会话草稿，关闭后不保证恢复内存输入。\n'
                '建议取消退出并核实原命令。是否仍明确确认未知提交风险并放弃全部会话草稿？',
                QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if not self._exit_waiting or answer != QMessageBox.StandardButton.Discard:
                self._resume_exit()
                return
            request = CloseUnconfirmedRequest(self.editing.context, pending.pending_save_id,
                pending.command_id, True, True,
                confirmed_versions)
            self._unknown_exit = request
            self._submit(lambda cancel: self.editing.close_unconfirmed_session(request, cancel),
                         self._exit_permission, self._exit_failed)
            return
        versions = self.editing.buffers()
        for view in self.editing.views():
            if not view.is_dirty:
                continue
            choice = self._ask_leave(view)
            if not self._exit_waiting:
                return
            if choice == LeaveChoice.CANCEL:
                self._resume_exit()
                return
            if choice == LeaveChoice.SAVE:
                self._submit(lambda cancel, v=view: self.session.save_buffer(v.snapshot.buffer_id,
                    v.snapshot.edit_version, cancel), self._exit_saved, self._exit_failed)
                return
        # Modal dialogs run a nested event loop: recheck all versions afterwards.
        if versions != self.editing.buffers() or self.editing.pending is not None:
            self._resume_exit()
            self.statusBar().showMessage('确认期间草稿发生变化，请重新关闭并确认。')
            return
        self._begin_final_close()

    def _exit_saved(self, result):
        continue_exit = self._exit_waiting
        self._resume_exit()
        self._show_result(result)
        if continue_exit and result.status == ResultStatus.OK:
            self.close()  # Recheck all buffers; later input is never silently discarded.

    def _exit_permission(self, result):
        if not self._exit_waiting:
            return  # A late permission cannot revive a cancelled exit intent.
        if (result.status == ResultStatus.OK and result.value.ready_to_close
                and self.editing.exit_permit_current(self._unknown_exit)):
            self._begin_final_close()
        else:
            self._resume_exit()
            self._show_result(result)

    def _begin_final_close(self):
        self._closing = True
        self.cancel_exit_button.hide()
        self.statusBar().showMessage('退出许可已确认，正在等待后台资源收尾。')
        self.runtime.begin_shutdown()
        self._poll.start()
