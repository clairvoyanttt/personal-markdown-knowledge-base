"""Prepare the fixed V10 acceptance dataset in an empty directory.

This is a development-only input generator, not product acceptance. It creates
one real collection through A's bootstrap and writes 1000 notes through C's
``CollectionStore`` so the dataset matches the documented V10 specification.

The script refuses a non-empty target. It writes ``v10-manifest.json`` after the
collection is published; the manifest contains the collection ID, database hash,
per-note body hashes and aggregate counts needed by the acceptance record.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import platform
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pmkb.contracts.collections import ActivationMode  # noqa: E402
from pmkb.domain.identity import format_reference_marker, new_identifier  # noqa: E402
from pmkb.infrastructure.storage.collection_catalog import LocalCollectionBackend  # noqa: E402
from pmkb.infrastructure.storage.store import CollectionStore  # noqa: E402

DATASET_ID = "PMKB-V10-2026-11-03"
SPEC_VERSION = 1
NOTE_COUNT = 1000
BODY_BYTES = 10240
REFERENCE_NOTES = 50
REFERENCE_PREFIX_BYTES = 75
SPEC_SHA256 = "05632f8c3e132fee88021cdb0123984867c27d0b6cf8f0b7c8f8c2a86fe7667c"
FILLER = "课程笔记内容与复习要点，包含中文与 English mixed text 以及数字 2026。\n"
PREFIX = "笔记 {i:04d} 的正文。\nTOKEN-{i:04d} UniqueMarker。\n"


def note_id_for(index: int) -> str:
    return f"{index:032x}"


def tags_for(index: int) -> tuple[str, ...]:
    if index % 100 == 10:
        return ("课程", "复习")
    if index % 100 == 12:
        return ("AI", "ai")
    return {
        0: ("课程",),
        1: ("复习",),
        2: ("AI",),
        3: ("ai",),
        4: ("实验",),
        5: ("阅读",),
    }.get(index % 10, ())


def body_for(index: int, collection_id: str) -> str:
    target = BODY_BYTES - (REFERENCE_PREFIX_BYTES if index < REFERENCE_NOTES else 0)
    body = PREFIX.format(i=index)
    while len((body + FILLER).encode("utf-8")) <= target:
        body += FILLER
    body += " " * (target - len(body.encode("utf-8")))
    if index < REFERENCE_NOTES:
        body += "\n" + format_reference_marker(collection_id, note_id_for(index + 1))
    encoded = body.encode("utf-8")
    if len(encoded) != BODY_BYTES:
        raise RuntimeError(f"note {index} body is {len(encoded)} bytes, expected {BODY_BYTES}")
    return body


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="existing empty collection directory")
    parser.add_argument(
        "--manifest",
        default=None,
        help="manifest path (default: <root>/v10-manifest.json; written outside the collection database)",
    )
    arguments = parser.parse_args()

    root = Path(arguments.root).resolve()
    if not root.is_dir():
        raise SystemExit(f"target directory does not exist: {root}")
    if any(root.iterdir()):
        raise SystemExit(f"target directory must be empty: {root}")

    manifest_path = Path(arguments.manifest) if arguments.manifest else root / "v10-manifest.json"
    manifest_path = manifest_path.resolve()
    if manifest_path.parent != root and not manifest_path.parent.is_dir():
        raise SystemExit(f"manifest parent does not exist: {manifest_path.parent}")

    backend = LocalCollectionBackend()
    prepared = backend.prepare(ActivationMode.CREATE, str(root))
    outcome = backend.publish(prepared)
    database = root / "knowledge.sqlite"
    if not database.is_file():
        raise SystemExit(f"publish did not create {database}")

    collection_id = prepared.collection_id
    store = CollectionStore(database, collection_id)

    notes: list[dict] = []
    tag_counts: dict[str, int] = {}
    for index in range(NOTE_COUNT):
        identifier = note_id_for(index)
        name = f"笔记 {index:04d}"
        tags = tags_for(index)
        body = body_for(index, collection_id)

        first = store.save(
            command_id=new_identifier(),
            operation_id=new_identifier(),
            note_id=identifier,
            name=name,
            body=body,
            tags=tags,
            base_revision=None,
        )
        if first.state.value != "COMMITTED":
            raise SystemExit(
                f"save failed for index {index}: {first.state.value} "
                f"{first.error.code if first.error else ''}"
            )

        if index < REFERENCE_NOTES:
            second = store.save(
                command_id=new_identifier(),
                operation_id=new_identifier(),
                note_id=identifier,
                name=name,
                body=body,
                tags=tags,
                base_revision=1,
            )
            if second.state.value != "COMMITTED":
                raise SystemExit(
                    f"second save failed for index {index}: {second.state.value} "
                    f"{second.error.code if second.error else ''}"
                )

        notes.append({
            "index": index,
            "note_id": identifier,
            "name": name,
            "tags": list(tags),
            "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "body_utf8_bytes": len(body.encode("utf-8")),
            "revision": 2 if index < REFERENCE_NOTES else 1,
            "reference_target_index": index + 1 if index < REFERENCE_NOTES else None,
        })
        for tag in tags:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1

    summaries = store.summaries()
    if len(summaries) != NOTE_COUNT:
        raise SystemExit(f"collection has {len(summaries)} notes, expected {NOTE_COUNT}")
    for index, expected in enumerate(notes):
        saved = store.load_note(expected["note_id"])
        if saved is None:
            raise SystemExit(f"saved note missing: {expected['note_id']}")
        if (saved.name != expected["name"] or saved.revision != expected["revision"]
                or saved.body != body_for(index, collection_id)
                or tuple(sorted(saved.tags)) != tuple(sorted(expected["tags"]))):
            raise SystemExit(f"saved projection differs for index {index}")

    reference_count = 0
    for index in range(REFERENCE_NOTES):
        view = store.saved_view(note_id_for(index))
        reference_count += len(view.references) if view else 0
    if reference_count != REFERENCE_NOTES:
        raise SystemExit(f"reference count is {reference_count}, expected {REFERENCE_NOTES}")

    content_lines = [
        json.dumps(
            {
                "index": note["index"],
                "note_id": note["note_id"],
                "name": note["name"],
                "tags": note["tags"],
                "body_sha256": note["body_sha256"],
                "revision": note["revision"],
                "reference_target_index": note["reference_target_index"],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        for note in notes
    ]
    content_sha256 = hashlib.sha256(("\n".join(content_lines) + "\n").encode("utf-8")).hexdigest()

    manifest = {
        "dataset_id": DATASET_ID,
        "spec_version": SPEC_VERSION,
        "spec_sha256": SPEC_SHA256,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "generator": "scripts/prepare_v10_acceptance_data.py",
        "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(),
        "root": str(root),
        "database": str(database),
        "database_bytes": database.stat().st_size,
        "database_sha256": sha256_file(database),
        "collection_id": collection_id,
        "note_count": NOTE_COUNT,
        "body_utf8_bytes_min": min(note["body_utf8_bytes"] for note in notes),
        "body_utf8_bytes_max": max(note["body_utf8_bytes"] for note in notes),
        "reference_count": reference_count,
        "revision_counts": {"1": NOTE_COUNT - REFERENCE_NOTES, "2": REFERENCE_NOTES},
        "tag_counts": dict(sorted(tag_counts.items())),
        "content_sha256": content_sha256,
        "publish_warnings": list(outcome.warnings),
        "notes": notes,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")

    summary = {key: manifest[key] for key in (
        "dataset_id", "spec_version", "spec_sha256", "generated_at_utc", "root",
        "database", "database_bytes", "database_sha256", "collection_id", "note_count",
        "body_utf8_bytes_min", "body_utf8_bytes_max", "reference_count",
        "revision_counts", "tag_counts", "content_sha256", "publish_warnings",
    )}
    summary["manifest"] = str(manifest_path)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
