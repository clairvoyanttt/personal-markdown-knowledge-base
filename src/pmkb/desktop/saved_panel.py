"""Saved-only UI reads; request identities reject late callbacks."""
import asyncio
import secrets

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLineEdit, QPushButton, QListWidget, QListWidgetItem, QLabel, QMessageBox, QInputDialog
from pmkb.application.output_support import result
from pmkb.application.query.service import SavedQueryService, SearchRequest, SavedViewRequest
from pmkb.contracts.common import ResultStatus
from pmkb.contracts.notes import LoadNoteRequest


class SavedPanel:
    def build_saved_panel(self, layout):
        self._delete_attempt = None
        self._delete_running = False
        self._saved_query = None
        self._reference_query = None
        self._saved_revision = -1
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText('搜索保存版；留空并点击刷新显示全部')
        self.search_input.returnPressed.connect(self.run_search)
        layout.addWidget(self.search_input)
        search = QPushButton('搜索保存版')
        search.clicked.connect(self.run_search)
        layout.addWidget(search)
        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText('标签筛选（逗号分隔）')
        layout.addWidget(self.filter_input)
        button = QPushButton('刷新保存版 / 应用标签')
        button.clicked.connect(self.refresh_saved)
        layout.addWidget(button)
        self.saved_notes = QListWidget()
        self.saved_notes.itemClicked.connect(self.open_saved_item)
        if not self.mvp_mode:
            self.saved_notes.itemDoubleClicked.connect(self.open_saved_item)
        layout.addWidget(self.saved_notes)
        self.delete_button = QPushButton('删除选中保存版')
        self.delete_button.clicked.connect(self.delete_saved)
        layout.addWidget(self.delete_button)
        insert = QPushButton('选择保存版插入引用')
        self.insert_reference_button = insert
        insert.clicked.connect(self.insert_saved_reference)
        layout.addWidget(insert)
        self.check_delete_button = QPushButton('核实删除结果')
        self.check_delete_button.clicked.connect(self.check_saved_delete)
        self.check_delete_button.hide()
        layout.addWidget(self.check_delete_button)
        relations_label = QLabel('保存版引用 / 反向引用（仅展示，不跳转）' if self.mvp_mode
                                else '保存版引用 / 反向引用')
        layout.addWidget(relations_label)
        self.saved_relations = QListWidget()
        if not self.mvp_mode:
            self.saved_relations.itemDoubleClicked.connect(self.open_saved_item)
        layout.addWidget(self.saved_relations)
        self.saved_state = QLabel('尚未打开集合')
        self.saved_state.setWordWrap(True)
        layout.addWidget(self.saved_state)
        if self.mvp_mode:
            for widget in (self.delete_button, self.check_delete_button):
                widget.hide()
            if not self.enable_references:
                for widget in (insert, relations_label, self.saved_relations):
                    widget.hide()
            button.setText('刷新保存版 / 应用标签')

    def run_search(self):
        if not self.search_input.text().strip():
            self._saved_query = self._reference_query = None
            self.saved_notes.clear()
            self.saved_relations.clear()
            self.statusBar().showMessage('搜索内容不能为空或全为空白；旧搜索结果已清除。')
            self.saved_state.setText('请输入搜索内容。')
            return
        self.refresh_saved()

    def refresh_saved(self):
        self._saved_query = token = secrets.token_hex(16)
        self._reference_query = None
        self.saved_notes.clear()
        self.saved_relations.clear()
        adapter = self._owned_storage
        if adapter is None or self._closing:
            return
        context = self.editing.context
        store = adapter.store
        keyword = self.search_input.text()
        from pmkb.domain.naming import normalize_tags
        from pmkb.domain.rules import RuleViolation
        try:
            tags = normalize_tags(self.filter_input.text().split(',')) if self.filter_input.text() else ()
        except RuleViolation as error:
            self.statusBar().showMessage(f'{error.code}：{error.reason}')
            return
        async def query(cancel):
            if keyword:
                reply = await SavedQueryService(store).search(SearchRequest(context, token, keyword, tags), cancel)
                if reply.status != ResultStatus.OK:
                    return reply
                return result(context, ResultStatus.OK, (reply.value.hits, reply.value.collection_revision))
            # Empty keyword is list mode; filter-only mode uses saved summaries.
            def read():
                if self.mvp_mode and not tags:
                    return store.summaries(), None
                captured = store.snapshot()
                rows = tuple(row for row in captured.notes if set(tags) <= set(row.tags))
                return rows, captured.collection_revision
            return result(context, ResultStatus.OK, await asyncio.to_thread(read))

        def ready(reply):
            if token != self._saved_query:
                return
            if reply.status != ResultStatus.OK:
                self._show_result(reply)
                return
            rows, revision = reply.value
            if revision is not None and revision < self._saved_revision:
                return
            if revision is not None:
                self._saved_revision = revision
            self.saved_state.setText(f'已找到 {len(rows)} 篇保存版笔记' if rows else '没有找到保存版笔记。')
            for row in rows:
                item = QListWidgetItem(row.name)
                item.setToolTip(f"已保存 · 修订 {row.revision}")
                item.setData(Qt.ItemDataRole.UserRole, row.note_id)
                item.setData(Qt.ItemDataRole.UserRole + 1, row.revision)
                self.saved_notes.addItem(item)
        self.saved_state.setText("正在读取保存版……")
        self._submit(query, ready)

    def open_saved_item(self, item):
        note_id = item.data(Qt.ItemDataRole.UserRole)
        if not note_id:
            self.statusBar().showMessage(item.text())
            return
        if self._switching or self._exit_waiting or self.editing.external_busy:
            return
        context = self.editing.context
        discard = None
        current = self._current()
        if (self.mvp_mode and current is not None and current.is_dirty
                and current.snapshot.note_id != note_id):
            from pmkb.contracts.collections import LeaveChoice
            captured = current.snapshot
            choice = self._ask_leave(current)
            if context != self.editing.context or self._current() != current:
                self.statusBar().showMessage('确认期间草稿已变化，请重新选择。')
                return
            if choice == LeaveChoice.CANCEL:
                return
            if choice == LeaveChoice.SAVE:
                def saved(reply):
                    self._show_result(reply)
                    if reply.status == ResultStatus.OK:
                        self.refresh_saved()
                        selected = QListWidgetItem()
                        selected.setData(Qt.ItemDataRole.UserRole, note_id)
                        self.open_saved_item(selected)
                self._submit(lambda cancel: self.session.save_buffer(captured.buffer_id,
                             captured.edit_version, cancel), saved)
                return
            discard = captured
        self._reference_query = token = secrets.token_hex(16)
        adapter = self._owned_storage
        if adapter is None:
            return
        async def load(cancel):
            loaded = await self.editing.load_note(LoadNoteRequest(context, note_id), cancel)
            if loaded.status != ResultStatus.OK:
                return loaded
            if self.mvp_mode and not self.enable_references:
                return result(context, ResultStatus.OK, (loaded.value, None))
            related = await SavedQueryService(adapter.store).load_saved_view(
                SavedViewRequest(context, token, note_id), cancel)
            return result(context, ResultStatus.OK, (loaded.value, related))
        def ready(reply):
            if token != self._reference_query:
                return
            if reply.status != ResultStatus.OK:
                self._show_result(reply)
                return
            view, related = reply.value
            if discard is not None:
                if not self.editing.discard_draft(context, discard.buffer_id, discard.edit_version):
                    self.statusBar().showMessage('加载期间出现新输入，原草稿保留，请重新选择。')
                    return
            self._active_buffer = view.snapshot.buffer_id
            self._refresh(refresh_text=True)
            self.saved_relations.clear()
            if related is None:
                return
            if related.status != ResultStatus.OK:
                self._show_result(related)
                return
            if related.value.collection_revision < self._saved_revision:
                return
            self._saved_revision = related.value.collection_revision
            self._show_relations(related.value)
        self._submit(load, ready)

    def refresh_relations(self):
        if self.mvp_mode and not self.enable_references:
            return
        self._reference_query = token = secrets.token_hex(16)
        self.saved_relations.clear()
        view, adapter = self._current(), self._owned_storage
        if view is None or view.snapshot.note_id is None or adapter is None or self._closing:
            return
        context, note_id = self.editing.context, view.snapshot.note_id
        async def query(cancel):
            return await SavedQueryService(adapter.store).load_saved_view(
                SavedViewRequest(context, token, note_id), cancel)
        def ready(reply):
            current = self._current()
            if (token != self._reference_query or current is None
                    or current.snapshot.note_id != note_id):
                return
            if reply.status != ResultStatus.OK:
                self._show_result(reply)
                return
            if reply.value.collection_revision < self._saved_revision:
                return
            self._saved_revision = reply.value.collection_revision
            self._show_relations(reply.value)
        self._submit(query, ready)

    def _show_relations(self, saved):
        self.saved_relations.clear()
        for ref in saved.references:
            labels = {
                'VALID': f'有效引用：{ref.target_name}',
                'DELETED': f'失效引用：{ref.target_name or "未知目标"}（目标已删除）',
                'MISSING': '失效引用：未知目标（标识不存在或不可用）',
                'FOREIGN_COLLECTION': '引用来自其他集合（无法跳转）',
            }
            item = QListWidgetItem(labels.get(ref.state, '失效引用：目标不可用'))
            item.setToolTip(ref.marker_text + '\n' + ref.reason)
            if ref.navigable and not self.mvp_mode:
                item.setData(Qt.ItemDataRole.UserRole, ref.target_note_id)
            self.saved_relations.addItem(item)
        for ref in saved.backlinks:
            item = QListWidgetItem(f'反向引用：{ref.name}')
            if not self.mvp_mode:
                item.setData(Qt.ItemDataRole.UserRole, ref.note_id)
            self.saved_relations.addItem(item)

    def insert_saved_reference(self):
        if (self.editing.busy or self._switching or self._exit_waiting
                or (self.mvp_mode and not self.enable_references)):
            return
        captured = self._current()
        if captured is None or self.saved_notes.count() == 0:
            return
        context = self.editing.context
        targets = [(self.saved_notes.item(i).text(),
                    self.saved_notes.item(i).data(Qt.ItemDataRole.UserRole))
                   for i in range(self.saved_notes.count())]
        label, accepted = QInputDialog.getItem(self, '插入引用', '选择当前列表中的保存版目标：',
                                               [name for name, _ in targets], 0, False)
        if not accepted:
            return
        if (self._current() != captured or self.editing.context != context
                or self.editing.busy or self._switching or self._exit_waiting):
            self.statusBar().showMessage('选择期间草稿或集合已变化，请重新插入。')
            return
        note_id = next((nid for name, nid in targets if name == label), None)
        if note_id is None:
            return
        from pmkb.domain.identity import format_reference_marker
        self.body.textCursor().insertText(format_reference_marker(context.collection_id, note_id))

    def delete_saved(self):
        if (self._owned_storage is None or self.editing.busy or self.editing.has_unconfirmed_save
                or self._switching or self._exit_waiting or self._delete_attempt):
            return
        item = self.saved_notes.currentItem()
        if item is None:
            return
        note_id = item.data(Qt.ItemDataRole.UserRole)
        revision = item.data(Qt.ItemDataRole.UserRole + 1)
        context = self.editing.context
        buffers = self.editing.buffers()
        if any(v.is_dirty and v.snapshot.note_id == note_id for v in self.editing.views()):
            self.statusBar().showMessage('该笔记有未保存修改，请先保存后再删除；输入已保留。')
            return
        answer = QMessageBox.question(self, '删除保存版',
            '确认删除选中保存版？已有引用会保留并显示为目标已删除。',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if (answer != QMessageBox.StandardButton.Yes or context != self.editing.context
                or buffers != self.editing.buffers() or self.editing.busy
                or self.editing.has_unconfirmed_save or self._switching or self._exit_waiting):
            return
        store = self._owned_storage.store
        # Never take a late revision as permission to delete a newer save.
        if revision is None:
            return
        self._delete_attempt = (store, context, note_id, revision,
                                secrets.token_hex(16), secrets.token_hex(16))
        self._run_saved_delete(False)

    def check_saved_delete(self):
        if self._delete_attempt and not self._delete_running and not self._exit_waiting:
            self._run_saved_delete(True)

    def _run_saved_delete(self, checking):
        from pmkb.infrastructure.storage.writing import CommitState
        store, context, note_id, revision, command, operation = self._delete_attempt
        self._reference_query = self._saved_query = None
        self._delete_running = self.editing.external_busy = True
        self.editing.external_pending = True
        self.centralWidget().setEnabled(False)
        def work():
            if not checking:
                return store.delete(command_id=command, operation_id=operation,
                                    note_id=note_id, expected_revision=revision).state
            snapshot = store.snapshot()
            # Absence of a receipt alone never proves rollback.
            receipt = next((r for r in snapshot.receipts if r.command_id == command
                            and r.operation_id == operation and r.note_id == note_id
                            and r.operation_kind == 'DELETE' and r.before_revision == revision), None)
            deleted = any(k.note_id == note_id and k.state == 'DELETED' for k in snapshot.identities)
            if receipt and deleted and not any(n.note_id == note_id for n in snapshot.notes):
                return CommitState.COMMITTED
            return CommitState.UNRESOLVED
        async def execute(cancel):
            try:
                state = await asyncio.to_thread(work)
            except Exception:
                state = CommitState.UNRESOLVED
            return result(context, ResultStatus.OK, state)
        def ready(reply):
            self._delete_running = self.editing.external_busy = False
            state = reply.value
            if state != CommitState.UNRESOLVED:
                self.editing.external_pending = False
                self._delete_attempt = None
                if state == CommitState.COMMITTED:
                    self.editing.finish_saved_delete(note_id)
                    if self._active_buffer and not any(v.snapshot.buffer_id == self._active_buffer
                                                      for v in self.editing.views()):
                        self._active_buffer = None
            self.check_delete_button.setVisible(self._delete_attempt is not None)
            if not self._exit_waiting:
                self.centralWidget().setEnabled(True)
            self._refresh(refresh_text=True)
            self.refresh_saved()
            self.statusBar().showMessage(
                '已删除保存版。' if state == CommitState.COMMITTED else
                '删除已确认未提交；原保存版保留，请刷新后重新确认。' if state == CommitState.NOT_COMMITTED else
                '删除结果待核实，禁止重复删除、保存或切换集合；请核实原命令。')
        self._submit(execute, ready)
