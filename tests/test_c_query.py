"""C query surface: saved-version search, tag filter, references and backlinks."""
from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from pmkb.application.parsing import ReferenceState, parse_markdown
from pmkb.application.query.service import SavedQueryService, SavedViewRequest, SearchRequest
from pmkb.contracts.collections import ActivationMode
from pmkb.contracts.common import CallContext, ResultStatus
from pmkb.domain.identity import format_reference_marker, new_identifier
from pmkb.domain.rules import RuleViolation
from pmkb.infrastructure.storage import reader as reader_module
from pmkb.infrastructure.storage.collection_catalog import LocalCollectionBackend
from pmkb.infrastructure.storage.store import CollectionStore


def new_collection(root: Path, name: str = "collection") -> tuple[Path, str]:
    directory = root / name
    directory.mkdir()
    backend = LocalCollectionBackend()
    prepared = backend.prepare(ActivationMode.CREATE, str(directory))
    backend.publish(prepared)
    return directory / "knowledge.sqlite", prepared.collection_id


class QueryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.db, self.collection_id = new_collection(self.root)
        self.store = CollectionStore(self.db, self.collection_id)
        self.service = SavedQueryService(self.store)
        self.context = CallContext(self.collection_id, 1)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def save(self, *, note_id: str | None = None, name: str = "Note", body: str = "Body",
             tags: tuple[str, ...] = (), base_revision: int | None = None):
        return self.store.save(
            command_id=new_identifier(),
            operation_id=new_identifier(),
            note_id=note_id or new_identifier(),
            name=name,
            body=body,
            tags=tags,
            base_revision=base_revision,
        )

    def search(self, keyword: str, tags: tuple[str, ...] = ()):
        return self.service.search_saved(SearchRequest(self.context, new_identifier(), keyword, tags))


class SearchTests(QueryTestCase):
    def test_literal_match_covers_saved_name_and_body(self) -> None:
        first = new_identifier()
        second = new_identifier()
        self.save(note_id=first, name="Markdown 笔记", body="知识管理与复习")
        self.save(note_id=second, name="Another", body="复习计划")

        by_body = self.search("知识管理")
        self.assertEqual([hit.note_id for hit in by_body.hits], [first])
        self.assertEqual(by_body.total, 1)

        by_name = self.search("markdown")
        self.assertEqual([hit.note_id for hit in by_name.hits], [first])

        by_substring = self.search("复习")
        self.assertEqual({hit.note_id for hit in by_substring.hits}, {first, second})

    def test_continuous_literal_match_does_not_split_characters(self) -> None:
        self.save(name="分离文字", body="知识与管理")
        self.assertEqual(self.search("知识管理").hits, ())
        self.assertEqual(self.search("知识与管理").total, 1)

    def test_ascii_case_insensitive_but_unicode_case_is_not_folded(self) -> None:
        self.save(name="Ärger", body="Markdown")
        self.assertEqual(self.search("MARKDOWN").total, 1)
        self.assertEqual(self.search("ärger").hits, ())
        self.assertEqual(self.search("Ärger").total, 1)

    def test_empty_or_whitespace_only_query_is_an_error_not_empty_success(self) -> None:
        for keyword in ("", "   ", "\t", "\u3000"):
            with self.subTest(keyword=repr(keyword)):
                with self.assertRaises(RuleViolation) as error:
                    self.search(keyword)
                self.assertEqual(error.exception.code, "INVALID_ARGUMENT")

    def test_no_match_is_a_distinct_empty_result(self) -> None:
        self.save(name="Alpha", body="正文")
        page = self.search("完全不存在")
        self.assertEqual(page.hits, ())
        self.assertEqual(page.total, 0)
        self.assertGreaterEqual(page.collection_revision, 1)

    def test_tag_filter_is_case_sensitive_and_all_tags_must_match(self) -> None:
        upper = new_identifier()
        lower = new_identifier()
        self.save(note_id=upper, name="Upper", tags=("AI", "课程"))
        self.save(note_id=lower, name="Lower", tags=("ai",))

        self.assertEqual([h.note_id for h in self.search("Upper", ("AI",)).hits], [upper])
        self.assertEqual([h.note_id for h in self.search("Lower", ("AI",)).hits], [])
        self.assertEqual([h.note_id for h in self.search("Upper", ("AI", "课程")).hits], [upper])
        self.assertEqual(self.search("Upper", ("AI", "missing")).hits, ())

    def test_search_is_scoped_to_one_collection(self) -> None:
        other_db, other_id = new_collection(self.root, "other")
        other_store = CollectionStore(other_db, other_id)
        other_store.save(command_id=new_identifier(), operation_id=new_identifier(),
                         note_id=new_identifier(), name="Shared", body="同一个关键词",
                         tags=(), base_revision=None)
        self.save(name="Shared", body="同一个关键词")

        ours = self.search("同一个关键词")
        self.assertEqual(ours.total, 1)
        self.assertNotEqual(ours.hits[0].note_id,
                            other_store.summaries()[0].note_id)

    def test_search_async_wraps_the_same_rules(self) -> None:
        self.save(name="Async", body="async body")
        ok = asyncio.run(self.service.search(SearchRequest(self.context, new_identifier(), "async")))
        self.assertIs(ok.status, ResultStatus.OK)
        self.assertEqual(ok.value.total, 1)

        error = asyncio.run(self.service.search(SearchRequest(self.context, new_identifier(), "  ")))
        self.assertIs(error.status, ResultStatus.ERROR)
        self.assertEqual(error.error.code, "INVALID_ARGUMENT")

    def test_stale_context_is_rejected(self) -> None:
        stale = CallContext(new_identifier(), 2)
        with self.assertRaises(Exception) as error:
            self.service.search_saved(SearchRequest(stale, new_identifier(), "x"))
        self.assertEqual(getattr(error.exception, "code", None), "STALE_CONTEXT")


class ReferenceTests(QueryTestCase):
    def test_backlinks_and_valid_classification(self) -> None:
        target = new_identifier()
        source = new_identifier()
        self.save(note_id=target, name="数据结构复习")
        marker = format_reference_marker(self.collection_id, target)
        self.save(note_id=source, name="索引", body=f"参见 {marker}")

        backlinks = self.store.backlinks(target)
        self.assertEqual([(item.note_id, item.name) for item in backlinks], [(source, "索引")])

        document = parse_markdown(self.store.load_note(source).body, self.collection_id)
        resolved = self.store.classify(document)
        self.assertEqual(len(resolved), 1)
        self.assertIs(resolved[0].state, ReferenceState.VALID)
        self.assertEqual(resolved[0].target_name, "数据结构复习")
        self.assertTrue(resolved[0].navigable)

    def test_deleted_target_keeps_historical_name_and_never_rebinds(self) -> None:
        target = new_identifier()
        source = new_identifier()
        self.save(note_id=target, name="已删目标")
        marker = format_reference_marker(self.collection_id, target)
        self.save(note_id=source, name="来源", body=marker)
        self.store.delete(command_id=new_identifier(), operation_id=new_identifier(),
                          note_id=target, expected_revision=1)

        resolved = self.store.classify(parse_markdown(marker, self.collection_id))
        self.assertIs(resolved[0].state, ReferenceState.DELETED)
        self.assertEqual(resolved[0].target_name, "已删目标")
        self.assertFalse(resolved[0].navigable)

        replacement = new_identifier()
        self.save(note_id=replacement, name="已删目标")
        again = self.store.classify(parse_markdown(marker, self.collection_id))
        self.assertIs(again[0].state, ReferenceState.DELETED)
        self.assertEqual(again[0].target_name, "已删目标")
        self.assertEqual(self.store.backlinks(replacement), ())

    def test_rename_keeps_the_relation_and_updates_the_displayed_name(self) -> None:
        target = new_identifier()
        source = new_identifier()
        self.save(note_id=target, name="旧名称")
        marker = format_reference_marker(self.collection_id, target)
        self.save(note_id=source, name="来源", body=marker)

        self.save(note_id=target, name="新名称", base_revision=1)
        resolved = self.store.classify(parse_markdown(marker, self.collection_id))
        self.assertIs(resolved[0].state, ReferenceState.VALID)
        self.assertEqual(resolved[0].target_name, "新名称")
        self.assertEqual(self.store.load_note(source).body, marker)
        self.assertEqual([item.note_id for item in self.store.backlinks(target)], [source])

    def test_missing_target_and_foreign_collection_never_fall_back_to_a_name(self) -> None:
        self.save(name="未知目标")
        missing_marker = format_reference_marker(self.collection_id, new_identifier())
        document = parse_markdown(f"{missing_marker} {format_reference_marker(new_identifier(), new_identifier())}",
                                  self.collection_id)
        resolved = self.store.classify(document)
        self.assertEqual([item.state for item in resolved],
                         [ReferenceState.MISSING, ReferenceState.FOREIGN_COLLECTION])
        self.assertIsNone(resolved[0].target_name)
        self.assertIsNone(resolved[1].target_name)

    def test_saved_view_returns_saved_note_references_and_backlinks(self) -> None:
        target = new_identifier()
        source = new_identifier()
        third = new_identifier()
        self.save(note_id=target, name="Target", tags=("x",))
        marker = format_reference_marker(self.collection_id, target)
        self.save(note_id=source, name="Source", body=f"link {marker}")
        self.save(note_id=third, name="Third", body=f"另一处 {marker}")

        view = self.service.saved_view(SavedViewRequest(self.context, new_identifier(), target))
        self.assertEqual(view.name, "Target")
        self.assertEqual(view.tags, ("x",))
        self.assertEqual(view.references, ())
        self.assertEqual({item.note_id for item in view.backlinks}, {source, third})

        view = self.service.saved_view(SavedViewRequest(self.context, new_identifier(), source))
        self.assertEqual(len(view.references), 1)
        self.assertEqual(view.references[0].target_name, "Target")
        self.assertTrue(view.references[0].navigable)
        self.assertEqual(view.backlinks, ())
        self.assertGreaterEqual(view.collection_revision, 4)

    def test_saved_view_missing_note_is_an_error(self) -> None:
        with self.assertRaises(RuleViolation) as error:
            self.service.saved_view(SavedViewRequest(self.context, new_identifier(), new_identifier()))
        self.assertEqual(error.exception.code, "NOTE_NOT_FOUND")

    def test_unsaved_draft_text_is_never_indexed(self) -> None:
        note_id = new_identifier()
        self.save(note_id=note_id, name="Saved name", body="旧正文")
        # A draft would change only B's memory; storage keeps the saved version.
        self.assertEqual(self.search("未保存关键词").total, 0)
        self.store.save(command_id=new_identifier(), operation_id=new_identifier(),
                        note_id=note_id, name="Saved name", body="新正文 未保存关键词",
                        tags=(), base_revision=1)
        self.assertEqual(self.search("未保存关键词").total, 1)


class LinkWhitespaceSaveTests(QueryTestCase):
    def test_trailing_blank_before_paren_creates_no_saved_reference(self) -> None:
        # C-REV-06 end-to-end: real SAVE of the review bodies must store no
        # occurrence and must not create a backlink on the target.
        target = new_identifier()
        self.save(note_id=target, name="Target")
        marker = format_reference_marker(self.collection_id, target)
        bodies = (
            f"[label {marker}](https://example.com )",
            f"[label {marker}](https://example.com\n)",
            f"![label {marker}](image.png )",
        )
        for index, body in enumerate(bodies):
            with self.subTest(body=body):
                result = self.save(name=f"来源{index}", body=body)
                self.assertEqual(result.state.value, "COMMITTED")
        self.assertEqual(self.store.backlinks(target), ())
        self.assertEqual(self.store.classify(parse_markdown(bodies[0], self.collection_id)), ())


class SavedViewSnapshotTests(QueryTestCase):
    def test_saved_view_uses_a_single_read_transaction(self) -> None:
        # C-REV-02: body, relations, backlinks and the collection revision must be
        # read in one transaction, not four independent ones.
        target = new_identifier()
        source = new_identifier()
        self.save(note_id=target, name="Target")
        marker = format_reference_marker(self.collection_id, target)
        self.save(note_id=source, name="Source", body=f"看 {marker}")

        opened: list = []
        statements: list[str] = []
        original = reader_module.connect_reader

        def recording(database):
            connection = original(database)
            connection.set_trace_callback(statements.append)
            opened.append(connection)
            return connection

        with mock.patch.object(reader_module, "connect_reader", recording):
            view = self.service.saved_view(
                SavedViewRequest(self.context, new_identifier(), source))

        self.assertEqual(len(opened), 1, "the saved view opened more than one connection")
        normalized = [statement.strip().upper() for statement in statements]
        self.assertEqual(normalized.count("BEGIN"), 1)
        self.assertEqual(normalized.count("COMMIT"), 1)
        self.assertEqual(len(view.references), 1)
        self.assertEqual(view.collection_revision, 3)

    def test_saved_view_never_mixes_two_saved_moments(self) -> None:
        # C-REV-02: a real save repeatedly interleaves with view construction. Each
        # response must belong to one saved moment: reference count and body agree
        # with the note revision, and the collection revision belongs to that same
        # moment (only this note is written, so collection revision = revision + 2).
        target = new_identifier()
        source = new_identifier()
        self.save(note_id=target, name="Target")
        marker = format_reference_marker(self.collection_id, target)
        with_reference = f"看 {marker}"
        without_reference = "没有引用"
        self.save(note_id=source, name="Source", body=with_reference)

        stop = threading.Event()
        errors: list[str] = []

        def writer() -> None:
            revision = 1
            for _ in range(60):
                if stop.is_set():
                    return
                revision += 1
                body = with_reference if revision % 2 == 1 else without_reference
                result = self.store.save(
                    command_id=new_identifier(),
                    operation_id=new_identifier(),
                    note_id=source,
                    name="Source",
                    body=body,
                    tags=(),
                    base_revision=revision - 1,
                )
                if result.state.value != "COMMITTED":
                    errors.append(f"writer save failed at revision {revision}: {result.error}")
                    return

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        try:
            for _ in range(120):
                view = self.service.saved_view(
                    SavedViewRequest(self.context, new_identifier(), source))
                expected_references = 1 if view.revision % 2 == 1 else 0
                with self.subTest(revision=view.revision):
                    self.assertEqual(len(view.references), expected_references)
                    self.assertEqual(
                        view.body,
                        with_reference if expected_references else without_reference,
                    )
                    self.assertEqual(view.collection_revision, view.revision + 2)
        finally:
            stop.set()
            thread.join(timeout=15)
        self.assertEqual(errors, [])
        self.assertFalse(thread.is_alive(), "the writer thread did not stop")


class TagFilterTests(QueryTestCase):
    def test_summaries_expose_saved_tags_only(self) -> None:
        tagged = new_identifier()
        untagged = new_identifier()
        self.save(note_id=tagged, name="有标签", tags=("AI", "课程"))
        self.save(note_id=untagged, name="无标签")

        rows = {item.note_id: item for item in self.store.summaries()}
        self.assertEqual(rows[tagged].tags, ("AI", "课程"))
        self.assertEqual(rows[untagged].tags, ())

    def test_summaries_tag_filter_is_and_and_case_sensitive(self) -> None:
        upper = new_identifier()
        lower = new_identifier()
        both = new_identifier()
        self.save(note_id=upper, name="大写标签篇", tags=("AI",))
        self.save(note_id=lower, name="小写标签篇", tags=("ai",))
        self.save(note_id=both, name="双标签", tags=("AI", "课程"))

        self.assertEqual({item.note_id for item in self.store.summaries(tags=("AI",))}, {upper, both})
        self.assertEqual({item.note_id for item in self.store.summaries(tags=("ai",))}, {lower})
        self.assertEqual({item.note_id for item in self.store.summaries(tags=("AI", "课程"))}, {both})
        self.assertEqual(self.store.summaries(tags=("不存在",)), ())

    def test_summaries_filter_has_no_implicit_trim_or_fold(self) -> None:
        self.save(name="AI 篇", tags=("AI",))
        with self.assertRaises(RuleViolation) as error:
            self.store.summaries(tags=(" AI ",))
        self.assertEqual(error.exception.code, "INVALID_TAG")
        # Case is not folded for tags: "ai" is a different tag, so no match.
        self.assertEqual(self.store.summaries(tags=("ai",)), ())
        self.assertEqual(len(self.store.summaries(tags=("AI", "AI"))), 1)

    def test_search_combines_keyword_and_all_tags(self) -> None:
        both = new_identifier()
        ai_only = new_identifier()
        course_only = new_identifier()
        self.save(note_id=both, name="组合", body="知识管理", tags=("AI", "课程"))
        self.save(note_id=ai_only, name="仅AI", body="知识管理", tags=("AI",))
        self.save(note_id=course_only, name="仅课程", body="其他内容", tags=("课程",))

        self.assertEqual({hit.note_id for hit in self.search("知识管理", ("AI", "课程")).hits}, {both})
        self.assertEqual({hit.note_id for hit in self.search("知识管理", ("AI",)).hits}, {both, ai_only})
        self.assertEqual(self.search("不存在", ("AI",)).hits, ())

    def test_search_hits_expose_saved_tags(self) -> None:
        note_id = new_identifier()
        self.save(note_id=note_id, name="带标签标题", tags=("AI", "课程"))
        reply = self.service.search_saved(SearchRequest(self.context, new_identifier(), "带标签标题"))
        self.assertEqual(reply.hits[0].tags, ("AI", "课程"))

    def test_tag_filter_is_collection_scoped(self) -> None:
        other_db, other_id = new_collection(self.root, "other")
        other_store = CollectionStore(other_db, other_id)
        other_store.save(command_id=new_identifier(), operation_id=new_identifier(),
                         note_id=new_identifier(), name="别的集合", body="同名标签",
                         tags=("AI",), base_revision=None)
        self.save(name="本集合", tags=("AI",))

        self.assertEqual([item.name for item in self.store.summaries(tags=("AI",))], ["本集合"])

    def test_tag_filter_uses_saved_state_after_update_and_delete(self) -> None:
        note_id = new_identifier()
        self.save(note_id=note_id, name="变更", tags=("AI",))
        self.save(note_id=note_id, name="变更", tags=("课程",), base_revision=1)
        self.assertEqual([item.name for item in self.store.summaries(tags=("课程",))], ["变更"])
        self.assertEqual(self.store.summaries(tags=("AI",)), ())

        self.store.delete(command_id=new_identifier(), operation_id=new_identifier(),
                          note_id=note_id, expected_revision=2)
        self.assertEqual(self.store.summaries(tags=("课程",)), ())


class BacklinkScopeTests(QueryTestCase):
    def test_foreign_marker_creates_no_backlink_in_this_collection(self) -> None:
        local = new_identifier()
        self.save(note_id=local, name="本地目标")
        foreign = format_reference_marker("f" * 32, new_identifier())
        self.save(name="来源", body=foreign)
        self.assertEqual(self.store.backlinks(local), ())

    def test_deleted_target_backlinks_still_list_saved_sources(self) -> None:
        target = new_identifier()
        source = new_identifier()
        self.save(note_id=target, name="目标")
        marker = format_reference_marker(self.collection_id, target)
        self.save(note_id=source, name="来源", body=marker)
        self.store.delete(command_id=new_identifier(), operation_id=new_identifier(),
                          note_id=target, expected_revision=1)

        self.assertEqual([item.note_id for item in self.store.backlinks(target)], [source])
        resolved = self.store.classify(parse_markdown(marker, self.collection_id))
        self.assertIs(resolved[0].state, ReferenceState.DELETED)

    def test_backlinks_ignore_markers_inside_code(self) -> None:
        target = new_identifier()
        self.save(note_id=target, name="目标")
        marker = format_reference_marker(self.collection_id, target)
        self.save(name="代码示例", body=f"```\n{marker}\n```")
        self.assertEqual(self.store.backlinks(target), ())


if __name__ == "__main__":
    unittest.main()
