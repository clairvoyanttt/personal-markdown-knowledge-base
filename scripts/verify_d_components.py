"""Runnable synthetic D-component example; no SQLite or product restoration.

All files belong to a newly allocated temporary directory. No user source path
or output path is accepted. Classified spans below are explicit fixture inputs,
not a substitute Markdown parser or proof of a real saved-note capture.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile

from pmkb.application.backup_restore.candidate import prepare_restore_candidate
from pmkb.application.backup_restore.manifest import BackupManifest, ManifestPolicy, ReferenceCounts
from pmkb.application.backup_restore.package_components import inspect_package, publish_package
from pmkb.application.backup_restore.references import RestoreReference, restore_reference_ownership
from pmkb.application.export.naming import default_export_name
from pmkb.application.export.rendering import ExportReference, ReferenceKind, render_export_markdown
from pmkb.contracts.common import ResultStatus
from pmkb.infrastructure.file_io.publication import publish_bytes
from pmkb.infrastructure.file_io.zip_guard import ArchivePolicy


def verify() -> dict:
    source_id, note_id, new_id = "a" * 32, "b" * 32, "c" * 32
    marker = f"[[note:{source_id}:{note_id}]]"
    prefix = "课程📚\r\n"
    body = prefix + marker + "\r\n`" + marker + "`\r\n![图](relative.png)\r\n"
    span = len(prefix.encode("utf-16-le")) // 2
    # Intentionally not a valid SQLite database: never hand this to real restore.
    payload = b"SYNTHETIC FIXTURE - NOT SQLITE\n" + body.encode("utf-8")
    captured = datetime.now(timezone.utc)
    manifest = BackupManifest(1, 1, "snapshot.sqlite", source_id, r"Z:\synthetic\unavailable",
        captured, 1, 1, ReferenceCounts(1, 0, 0, 0), hashlib.sha256(payload).hexdigest(), len(payload))
    manifest_policy = ManifestPolicy(frozenset({1}), frozenset({1}), 8192)
    archive_policy = ArchivePolicy(frozenset({"manifest.json", "snapshot.sqlite"}),
        frozenset({"manifest.json", "snapshot.sqlite"}), 2097152, 2, 1048576, 2097152, 8192)
    with tempfile.TemporaryDirectory(prefix="pmkb-d-example-") as directory:
        root = Path(directory)
        exported = render_export_markdown(body,
            (ExportReference(span, 74, marker, ReferenceKind.VALID, "课程/笔记"),)).encode("utf-8")
        export_path = root / default_export_name("课程/笔记")
        if publish_bytes(export_path, exported).status is not ResultStatus.OK:
            raise RuntimeError("Synthetic export publication failed.")
        if export_path.read_bytes() != exported:
            raise RuntimeError("Synthetic exported bytes differ.")
        archive = root / "synthetic.zip"
        if publish_package(archive, payload, manifest, manifest_policy, archive_policy).status is not ResultStatus.OK:
            raise RuntimeError("Synthetic archive publication failed.")
        original_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        report = inspect_package(archive, manifest_policy, archive_policy)
        candidate = prepare_restore_candidate(archive, root, manifest_policy, archive_policy)
        try:
            candidate.verify_unchanged()
            if candidate.snapshot_path.read_bytes() != payload:
                raise RuntimeError("Candidate does not contain the actual synthetic payload.")
            transformed = restore_reference_ownership(body, source_id, new_id,
                (RestoreReference(span, 74, marker, ReferenceKind.VALID, "课程/笔记"),))
            if (transformed.changed_occurrences != 1
                    or f"`{marker}`" not in transformed.body
                    or "![图](relative.png)" not in transformed.body):
                raise RuntimeError("Authorized conversion altered protected fixture text.")
            if hashlib.sha256(archive.read_bytes()).hexdigest() != original_digest:
                raise RuntimeError("The source archive changed.")
        finally:
            warnings = candidate.cleanup()
        if warnings:
            raise RuntimeError("Synthetic candidate cleanup needs inspection.")
        return {
            "scope": "synthetic_d_components_only",
            "sqlite_validated": False,
            "product_restore_executed": False,
            "archive_entries": len(report.archive.entries),
            "checks": {"exact_export_bytes": True, "archive_manifest_binding": True,
                       "controlled_candidate_bytes": True, "authorized_conversion_only": True,
                       "source_archive_unchanged": True, "candidate_cleanup": True},
        }


if __name__ == "__main__":
    print(json.dumps(verify(), ensure_ascii=False, indent=2))
