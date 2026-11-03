"""Run the fixed V10 query list against one prepared collection.

This is a development-only component self-check: it calls C's storage query
service directly and measures service latency, not Qt rendering. Its output is
explicitly marked ``UNSIGNED_TOOL_RUN`` and must not be recorded as formal V10
product acceptance.

Formal V10 execution uses the fixed order, warm-up, load and timing rules in
``docs/acceptance/v10-dataset-and-queries.md`` with a real executor and device.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
import platform
import sqlite3
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pmkb.infrastructure.storage.store import CollectionStore  # noqa: E402

DEFAULT_QUERIES = ROOT / "docs" / "acceptance" / "v10-queries.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ids_digest(note_ids: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(note_ids)).encode("utf-8")).hexdigest()


def percentile(sorted_values: list[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, int(len(sorted_values) * fraction))
    return sorted_values[index]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, help="path to knowledge.sqlite")
    parser.add_argument("--collection-id", required=True)
    parser.add_argument("--queries", default=str(DEFAULT_QUERIES))
    parser.add_argument("--out", default=None, help="write the full JSON record here")
    parser.add_argument("--label", default="", help="free-form tool-run label")
    parser.add_argument("--query-gap-seconds", type=float, default=1.0)
    parser.add_argument("--warmup-wait-seconds", type=float, default=5.0)
    parser.add_argument("--threshold-ms", type=float, default=2000.0)
    parser.add_argument("--skip-warmup", action="store_true")
    parser.add_argument("--allow-other-note-count", action="store_true",
                        help="skip the 1000-note precondition (never for a formal V10 run)")
    arguments = parser.parse_args()

    database = Path(arguments.database).resolve()
    if not database.is_file():
        raise SystemExit(f"database does not exist: {database}")
    query_path = Path(arguments.queries).resolve()
    specification = json.loads(query_path.read_text(encoding="utf-8"))
    queries = specification["queries"]
    if len(queries) != 30:
        raise SystemExit(f"expected 30 fixed queries, found {len(queries)}")

    store = CollectionStore(database, arguments.collection_id)
    metadata = store.metadata()
    if metadata.collection_id != arguments.collection_id:
        raise SystemExit("the database collection ID does not match --collection-id")
    if metadata.state != "READY":
        raise SystemExit(f"collection state is {metadata.state!r}, expected READY")
    summaries = store.summaries()
    if not arguments.allow_other_note_count and len(summaries) != specification["note_count"]:
        raise SystemExit(
            f"collection has {len(summaries)} notes, expected {specification['note_count']}"
        )

    started_at = datetime.now().astimezone()
    warmup_records: list[dict] = []
    if not arguments.skip_warmup:
        warmup = specification.get("warmup", {})
        for keyword in warmup.get("queries", []):
            for repetition in range(1, int(warmup.get("repetitions", 3)) + 1):
                begin = time.perf_counter()
                page = store.search(keyword=keyword)
                elapsed = (time.perf_counter() - begin) * 1000.0
                warmup_records.append({
                    "keyword": keyword,
                    "repetition": repetition,
                    "count": len(page.items),
                    "elapsed_ms": round(elapsed, 3),
                })
                time.sleep(0.2)
        time.sleep(arguments.warmup_wait_seconds)

    results: list[dict] = []
    for position, query in enumerate(queries):
        if position and arguments.query_gap_seconds > 0:
            time.sleep(arguments.query_gap_seconds)
        begin = time.perf_counter()
        error = None
        try:
            page = store.search(keyword=query["keyword"], tags=tuple(query["tags"]))
            note_ids = [item.note_id for item in page.items]
        except Exception as caught:  # noqa: BLE001 - record the actual failure
            elapsed = (time.perf_counter() - begin) * 1000.0
            results.append({
                "id": query["id"],
                "keyword": query["keyword"],
                "tags": query["tags"],
                "expected_count": query["expected_count"],
                "expected_ids_sha256": query["expected_ids_sha256"],
                "actual_count": None,
                "actual_ids_sha256": None,
                "elapsed_ms": round(elapsed, 3),
                "within_threshold": elapsed <= arguments.threshold_ms,
                "correct": False,
                "error": f"{type(caught).__name__}: {caught}",
            })
            continue
        elapsed = (time.perf_counter() - begin) * 1000.0
        actual_digest = ids_digest(note_ids)
        correct = (len(note_ids) == query["expected_count"]
                   and actual_digest == query["expected_ids_sha256"])
        expected_ids = query.get("expected_note_ids")
        if expected_ids is not None:
            correct = correct and sorted(note_ids) == sorted(expected_ids)
        record = {
            "id": query["id"],
            "keyword": query["keyword"],
            "tags": query["tags"],
            "expected_count": query["expected_count"],
            "expected_ids_sha256": query["expected_ids_sha256"],
            "actual_count": len(note_ids),
            "actual_ids_sha256": actual_digest,
            "elapsed_ms": round(elapsed, 3),
            "within_threshold": elapsed <= arguments.threshold_ms,
            "correct": correct,
        }
        if expected_ids is not None or len(note_ids) <= 20:
            record["actual_note_ids"] = sorted(note_ids)
        results.append(record)

    durations = sorted(item["elapsed_ms"] for item in results)
    correct_count = sum(1 for item in results if item["correct"])
    within_count = sum(1 for item in results if item["within_threshold"])
    criteria = specification["criteria"]
    all_correct = correct_count == int(criteria["all_queries_correct"])
    threshold_met = within_count >= int(criteria["queries_within_2000ms_required"])

    record = {
        "record_status": "UNSIGNED_TOOL_RUN",
        "not_product_acceptance": True,
        "label": arguments.label,
        "dataset_id": specification["dataset_id"],
        "spec_version": specification["spec_version"],
        "spec_sha256": specification["spec_sha256"],
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now().astimezone().isoformat(),
        "environment": {
            "python": sys.version.split()[0],
            "sqlite": sqlite3.sqlite_version,
            "platform": platform.platform(),
            "processor": platform.processor(),
            "machine": platform.machine(),
            "node": platform.node(),
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "cpu_count": os.cpu_count(),
        },
        "database": str(database),
        "database_bytes": database.stat().st_size,
        "database_sha256": sha256_file(database),
        "collection_id": metadata.collection_id,
        "collection_revision": metadata.collection_revision,
        "note_count": len(summaries),
        "warmup": warmup_records,
        "warmup_skipped": bool(arguments.skip_warmup),
        "query_gap_seconds": arguments.query_gap_seconds,
        "threshold_ms": arguments.threshold_ms,
        "queries": results,
        "summary": {
            "correct_count": correct_count,
            "within_threshold_count": within_count,
            "total_queries": len(results),
            "min_ms": durations[0] if durations else 0.0,
            "median_ms": round(statistics.median(durations), 3) if durations else 0.0,
            "p95_ms": round(percentile(durations, 0.95), 3),
            "max_ms": durations[-1] if durations else 0.0,
        },
        "tool_self_check": {
            "all_results_correct": all_correct,
            "performance_threshold_met": threshold_met,
            "criteria_met": all_correct and threshold_met,
        },
        "note": (
            "Direct storage-query call, no Qt. Not formal V10 acceptance. "
            "Formal execution still needs the fixed device, warm-up/load record, "
            "real UI timing and a human executor."
        ),
    }

    if arguments.out:
        output = Path(arguments.out)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
        print(json.dumps({
            "record_status": record["record_status"],
            "out": str(output),
            "correct_count": correct_count,
            "within_threshold_count": within_count,
            "total_queries": len(results),
            "tool_self_check": record["tool_self_check"],
        }, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0 if (all_correct and threshold_met) else 1


if __name__ == "__main__":
    raise SystemExit(main())
