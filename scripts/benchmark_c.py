"""NFR03 saved-version scale check for C's storage and query path.

This is a real, fixed dataset run: 1000 synthetic notes of ~10 KB UTF-8 body,
30 fixed keyword/tag queries with results checked against expectations computed
from the generated data, and every query timed. It uses a temporary collection
created through A's real bootstrap and C's real SQLite transactions.

It is NOT product acceptance: one machine, one run, synthetic text, no Qt. The
full NFR03 record still needs the fixed dataset/order/prewarm/device record that
the design asks for.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pmkb.contracts.collections import ActivationMode  # noqa: E402
from pmkb.domain.identity import format_reference_marker, new_identifier  # noqa: E402
from pmkb.domain.naming import name_key as ascii_fold  # noqa: E402
from pmkb.infrastructure.storage.collection_catalog import LocalCollectionBackend  # noqa: E402
from pmkb.infrastructure.storage.store import CollectionStore  # noqa: E402

FILLER = "课程笔记内容与复习要点，包含中文与 English mixed text 以及数字 2026。"
TAG_POOL = ("课程", "复习", "AI", "ai", "实验", "阅读")


def build_body(index: int, target_bytes: int) -> str:
    """Deterministic body of roughly ``target_bytes`` UTF-8 bytes with a unique token."""
    parts = [f"笔记 {index:04d} 的正文。", f"TOKEN-{index:04d} UniqueMarker。"]
    while len("".join(parts).encode("utf-8")) < target_bytes:
        parts.append(FILLER)
    body = "".join(parts)
    while len(body.encode("utf-8")) > target_bytes:
        body = body[:-1]
    return body


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--body-bytes", type=int, default=10240)
    parser.add_argument("--query-limit-seconds", type=float, default=2.0)
    arguments = parser.parse_args()

    temporary = tempfile.TemporaryDirectory()
    root = Path(temporary.name)
    directory = root / "collection"
    directory.mkdir()
    backend = LocalCollectionBackend()
    prepared = backend.prepare(ActivationMode.CREATE, str(directory))
    backend.publish(prepared)
    database = directory / "knowledge.sqlite"
    store = CollectionStore(database, prepared.collection_id)

    note_ids: list[str] = []
    tags_by_note: dict[str, tuple[str, ...]] = {}
    bodies: dict[str, str] = {}
    names: dict[str, str] = {}
    save_seconds: list[float] = []

    started = time.perf_counter()
    for index in range(arguments.count):
        note_id = new_identifier()
        name = f"笔记 {index:04d}"
        body = build_body(index, arguments.body_bytes)
        tags = (TAG_POOL[index % 3],) if index % 4 == 0 else ()
        begin = time.perf_counter()
        result = store.save(
            command_id=new_identifier(),
            operation_id=new_identifier(),
            note_id=note_id,
            name=name,
            body=body,
            tags=tags,
            base_revision=None,
        )
        save_seconds.append(time.perf_counter() - begin)
        if result.state.value != "COMMITTED":
            print(json.dumps({"error": "save failed", "index": index,
                              "state": result.state.value,
                              "code": result.error.code if result.error else None}))
            return 1
        note_ids.append(note_id)
        tags_by_note[note_id] = tags
        bodies[note_id] = body
        names[note_id] = name
    save_total = time.perf_counter() - started

    # Add a few saved references so the reference tables are populated as well.
    for index in range(0, min(50, arguments.count)):
        source = note_ids[index]
        target = note_ids[(index + 1) % arguments.count]
        marker = format_reference_marker(prepared.collection_id, target)
        result = store.save(
            command_id=new_identifier(),
            operation_id=new_identifier(),
            note_id=source,
            name=names[source],
            body=bodies[source] + "\n" + marker,
            tags=tags_by_note[source],
            base_revision=1,
        )
        if result.state.value != "COMMITTED":
            print(json.dumps({"error": "reference save failed", "index": index}))
            return 1
        bodies[source] = bodies[source] + "\n" + marker

    def expected_ids(keyword: str, tags: tuple[str, ...] = ()) -> set[str]:
        folded = ascii_fold(keyword)
        matched = {
            note_id for note_id, body in bodies.items()
            if folded in ascii_fold(names[note_id]) or folded in ascii_fold(body)
        }
        if tags:
            matched &= {note_id for note_id, note_tags in tags_by_note.items()
                        if all(tag in note_tags for tag in tags)}
        return matched

    queries: list[tuple[str, tuple[str, ...]]] = [
        ("TOKEN-0007", ()), ("TOKEN-0999", ()),
        ("笔记 0001", ()), ("笔记 0777", ()), ("TOKEN-1234", ()),
        ("课程笔记内容", ()), ("复习要点", ()), ("knowledge", ()),
        ("Markdown", ()), ("markdown", ()), ("English", ()),
        ("2026", ()), ("数字", ()), ("笔记", ()),
        ("不存在的关键词", ()), ("token-0003", ()), ("TOKEN-0003", ()),
        ("笔记", ("课程",)), ("笔记", ("复习",)), ("笔记", ("AI",)),
        ("笔记", ("ai",)), ("笔记", ("课程", "复习")),
        ("TOKEN-0004", ("课程",)), ("TOKEN-0008", ("AI",)),
        ("实验", ()), ("阅读", ()), ("mixed text", ()),
        ("UniqueMarker", ()), ("笔记 0420", ()), ("笔记", ("不存在",)),
    ]

    durations: list[float] = []
    mismatches: list[dict] = []
    for keyword, tags in queries:
        begin = time.perf_counter()
        page = store.search(keyword=keyword, tags=tags)
        duration = time.perf_counter() - begin
        durations.append(duration)
        wanted = expected_ids(keyword, tags)
        got = {hit.note_id for hit in page.items}
        if got != wanted:
            mismatches.append({"keyword": keyword, "tags": list(tags),
                               "expected": len(wanted), "actual": len(got)})

    ordered = sorted(durations)
    p50 = statistics.median(ordered)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    under_limit = sum(1 for value in durations if value <= arguments.query_limit_seconds)

    database_bytes = database.stat().st_size
    report = {
        "captured_utc": datetime.now(timezone.utc).isoformat(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "processor": platform.processor(),
        "notes": arguments.count,
        "requested_body_bytes": arguments.body_bytes,
        "database_bytes": database_bytes,
        "save_total_seconds": round(save_total, 3),
        "save_average_ms": round(statistics.mean(save_seconds) * 1000, 1),
        "save_p95_ms": round(sorted(save_seconds)[int(len(save_seconds) * 0.95)] * 1000, 1),
        "queries": len(queries),
        "query_total_seconds": round(sum(durations), 3),
        "query_p50_ms": round(p50 * 1000, 2),
        "query_p95_ms": round(p95 * 1000, 2),
        "query_max_ms": round(max(durations) * 1000, 2),
        "queries_within_limit": f"{under_limit}/{len(queries)}",
        "limit_seconds": arguments.query_limit_seconds,
        "correct_results": not mismatches,
        "mismatches": mismatches,
        "note": "Single machine, single run, synthetic data, no Qt. Not product acceptance.",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    temporary.cleanup()
    return 0 if (not mismatches and under_limit == len(queries)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
