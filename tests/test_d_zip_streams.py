"""Real malformed ZIP streams; these are not SQLite/product acceptance tests."""
import hashlib
import io
from pathlib import Path
import struct
import tempfile
import unittest
import zipfile
import zlib

from pmkb.infrastructure.file_io.zip_guard import (
    ArchivePolicy, ArchiveValidationError, inspect_zip,
)


class _NonSeekable(io.BytesIO):
    def seek(self, *args, **kwargs):
        raise io.UnsupportedOperation("Synthetic streaming ZIP writer")

    def seekable(self):
        return False


class ZipStreamIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "synthetic-stream.zip"

    def policy(self, **overrides):
        values = dict(allowed_entries={"payload.bin"}, required_entries={"payload.bin"},
                      max_archive_bytes=1_000_000, max_entries=1,
                      max_entry_bytes=200_000, max_total_uncompressed_bytes=200_000,
                      max_capture_bytes=200_000, chunk_size=17)
        values.update(overrides)
        return ArchivePolicy(**values)

    def make(self, payload, *, method=zipfile.ZIP_DEFLATED, zip64=False, streaming=False):
        output = _NonSeekable() if streaming else io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=method) as archive:
            with archive.open("payload.bin", "w", force_zip64=zip64) as member:
                member.write(payload)
        self.path.write_bytes(output.getvalue())

    def rewrite(self, *, compressed=None, declared_size=None, crc=None):
        # Mutate a real single-entry ZIP, keeping its central-directory/EOCD
        # offsets valid. Do not replace ZipExtFile with a synthetic reader.
        raw = bytearray(self.path.read_bytes())
        with zipfile.ZipFile(self.path) as archive:
            info = archive.getinfo("payload.bin")
            central = archive.start_dir
        local = info.header_offset
        name_size, extra_size = struct.unpack_from("<HH", raw, local + 26)
        start = local + 30 + name_size + extra_size
        if compressed is not None:
            old_size = info.compress_size
            raw[start:start + old_size] = compressed
            central += len(compressed) - old_size
            eocd = raw.rfind(b"PK\x05\x06")
            struct.pack_into("<I", raw, eocd + 16, central)
            for offset in (local + 18, central + 20):
                struct.pack_into("<I", raw, offset, len(compressed))
        if declared_size is not None:
            for offset in (local + 22, central + 24):
                struct.pack_into("<I", raw, offset, declared_size)
        if crc is not None:
            for offset in (local + 14, central + 16):
                struct.pack_into("<I", raw, offset, crc)
        self.path.write_bytes(raw)

    def rejected(self, code, policy=None):
        before = self.path.read_bytes()
        with self.assertRaises(ArchiveValidationError) as caught:
            inspect_zip(self.path, policy or self.policy(), capture_entries={"payload.bin"})
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_real_deflate_prefix_cannot_bypass_one_byte_budgets(self):
        self.make(b"A" * 100_000)
        original = self.path.read_bytes()
        with zipfile.ZipFile(self.path) as archive:
            info = archive.getinfo("payload.bin")
            start = info.header_offset + 30 + len(info.filename.encode())
            compressed = original[start:start + info.compress_size]
        self.assertEqual(zlib.decompress(compressed, -15), b"A" * 100_000)
        self.rewrite(declared_size=1, crc=zlib.crc32(b"A"))
        with zipfile.ZipFile(self.path) as archive:
            self.assertEqual(archive.getinfo("payload.bin").compress_size, len(compressed))
        self.rejected("resource_budget", self.policy(max_entry_bytes=1,
                      max_total_uncompressed_bytes=1, max_capture_bytes=1))

    def test_real_deflate_prefix_rejected_even_with_generous_budgets(self):
        self.make(b"AB" * 100)
        self.rewrite(declared_size=1, crc=zlib.crc32(b"A"))
        self.rejected("size_mismatch")

    def test_real_stored_prefix_is_rejected(self):
        self.make(b"AB" * 100, method=zipfile.ZIP_STORED)
        self.rewrite(declared_size=1, crc=zlib.crc32(b"A"))
        self.rejected("size_mismatch")

    def test_deflate_without_final_marker_is_rejected(self):
        self.make(b"A")
        with zipfile.ZipFile(self.path) as archive:
            info = archive.getinfo("payload.bin")
            start = info.header_offset + 30 + len(info.filename.encode())
            compressed = self.path.read_bytes()[start:start + info.compress_size]
        self.rewrite(compressed=compressed[:-1])
        self.rejected("invalid_zip")

    def test_bytes_after_deflate_end_are_rejected(self):
        self.make(b"A")
        with zipfile.ZipFile(self.path) as archive:
            info = archive.getinfo("payload.bin")
            start = info.header_offset + 30 + len(info.filename.encode())
            compressed = self.path.read_bytes()[start:start + info.compress_size]
        self.rewrite(compressed=compressed + b"trailing-garbage")
        self.rejected("invalid_zip")

    def test_second_deflate_stream_is_not_silently_ignored(self):
        self.make(b"A")
        with zipfile.ZipFile(self.path) as archive:
            info = archive.getinfo("payload.bin")
            start = info.header_offset + 30 + len(info.filename.encode())
            compressed = self.path.read_bytes()[start:start + info.compress_size]
        compressor = zlib.compressobj(wbits=-15)
        second = compressor.compress(b"B") + compressor.flush()
        self.rewrite(compressed=compressed + second)
        self.rejected("invalid_zip")

    def test_empty_deflate_still_requires_a_complete_stream(self):
        self.make(b"")
        self.rewrite(compressed=b"")
        self.rejected("invalid_zip")

    def test_complete_stream_crc_is_checked(self):
        for method in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            with self.subTest(method=method):
                self.make(b"AB" * 100, method=method)
                self.rewrite(crc=0)
                self.rejected("invalid_zip")

    def test_zip64_and_streaming_data_descriptors_remain_supported(self):
        payload = "Synthetic ZIP64 / descriptor / 中文".encode() * 4
        for method in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            for zip64 in (False, True):
                for streaming in (False, True):
                    with self.subTest(method=method, zip64=zip64, streaming=streaming):
                        self.make(payload, method=method, zip64=zip64, streaming=streaming)
                        report = inspect_zip(self.path, self.policy(), capture_entries={"payload.bin"})
                        self.assertEqual(report.entries[0].captured_bytes, payload)
                        self.assertEqual(report.entries[0].sha256, hashlib.sha256(payload).hexdigest())

    def test_exact_output_budgets_and_empty_members_are_supported(self):
        for method in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            for length in (0, 1, 17, 100_000):
                for chunk_size in (1, 17, 64 * 1024):
                    with self.subTest(method=method, length=length, chunk_size=chunk_size):
                        payload = b"A" * length
                        self.make(payload, method=method)
                        budget = max(length, 1)
                        report = inspect_zip(self.path, self.policy(max_entry_bytes=budget,
                            max_total_uncompressed_bytes=budget, max_capture_bytes=budget,
                            chunk_size=chunk_size), capture_entries={"payload.bin"})
                        self.assertEqual(report.total_uncompressed_bytes, length)
                        self.assertEqual(report.entries[0].captured_bytes, payload)


if __name__ == "__main__":
    unittest.main()
