"""Saved-only UI reads; request identities reject late callbacks."""
import asyncio
import secrets

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLineEdit, QPushButton, QListWidget, QListWidgetItem, QLabel
from pmkb.application.output_support import result
from pmkb.application.query.service import SavedQueryService, SearchRequest, SavedViewRequest
from pmkb.contracts.common import ResultStatus
from pmkb.contracts.notes import LoadNoteRequest


class SavedPanel:
    def build_saved_panel(self, layout):
        self._saved_query = None
        self._reference_query = None
        self._saved_revision = -1
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText('搜索保存版；留空并点击刷新显示全部')
        self.search_input.returnPressed.connect(self.refresh_saved)
        layout.addWidget(self.search_input)
        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText('标签筛选（逗号分隔）')
        layout.addWidget(self.filter_input)
        button = QPushButton('刷新保存版 / 搜索')
        button.clicked.connect(self.refresh_saved)
        layout.addWidget(button)
        self.saved_notes = QListWidget()
        self.saved_notes.itemDoubleClicked.connect(self.open_saved_item)
        layout.addWidget(self.saved_notes)
        layout.addWidget(QLabel('保存版引用 / 反向引用'))
        self.saved_relations = QListWidget()
        self.saved_relations.itemDoubleClicked.connect(self.open_saved_item)
        layout.addWidget(self.saved_relations)

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
        tags = tuple(t.strip() for t in self.filter_input.text().split(',') if t.strip())
        async def query(cancel):
            if keyword:
                reply = await SavedQueryService(store).search(SearchRequest(context, token, keyword, tags), cancel)
                if reply.status != ResultStatus.OK:
                    return reply
                return result(context, ResultStatus.OK, (reply.value.hits, reply.value.collection_revision))
            # Empty keyword is list mode; filter-only mode uses saved summaries.
            def read():
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
            for row in rows:
                item = QListWidgetItem(row.name)
                item.setData(Qt.ItemDataRole.UserRole, row.note_id)
                self.saved_notes.addItem(item)
        self._submit(query, ready)

    def open_saved_item(self, item):
        note_id = item.data(Qt.ItemDataRole.UserRole)
        if not note_id or self._switching or self._exit_waiting:
            return
        context = self.editing.context
        self._reference_query = token = secrets.token_hex(16)
        adapter = self._owned_storage
        if adapter is None:
            return
        async def load(cancel):
            loaded = await self.editing.load_note(LoadNoteRequest(context, note_id), cancel)
            if loaded.status != ResultStatus.OK:
                return loaded
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
            self._active_buffer = view.snapshot.buffer_id
            self._refresh(refresh_text=True)
            self.saved_relations.clear()
            if related.status != ResultStatus.OK:
                self._show_result(related)
                return
            if related.value.collection_revision < self._saved_revision:
                return
            self._saved_revision = related.value.collection_revision
            for ref in related.value.references:
                item = QListWidgetItem(f'{ref.state}: {ref.target_name or ref.marker_text}')
                if ref.navigable:
                    item.setData(Qt.ItemDataRole.UserRole, ref.target_note_id)
                self.saved_relations.addItem(item)
            for ref in related.value.backlinks:
                item = QListWidgetItem(f'反向引用：{ref.name}')
                item.setData(Qt.ItemDataRole.UserRole, ref.note_id)
                self.saved_relations.addItem(item)
        self._submit(load, ready)
