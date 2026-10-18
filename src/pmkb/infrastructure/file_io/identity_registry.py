"""Necessary identity/location registry only; no note bodies or recent-items UI."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile

from pmkb.application.collection.ports import CollectionFailure


class LocalIdentityRegistry:
    def __init__(self, config_file: Path) -> None:
        self.path = config_file.resolve()

    def _read(self) -> tuple[bytes | None, dict[str, str]]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return None, {}
        except OSError as error:
            raise CollectionFailure("IO_FAILURE", "Identity configuration is not readable.") from error
        try:
            data = json.loads(raw)
            locations = data["collections"]
            if data.get("configVersion") != 1 or not isinstance(locations, dict):
                raise ValueError("invalid registry shape")
            import re
            if any(not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{32}", key)
                   or not isinstance(value, str) or not Path(value).is_absolute()
                   for key, value in locations.items()):
                raise ValueError("invalid registry entry")
            return raw, locations
        except (ValueError, KeyError, TypeError) as error:
            raise CollectionFailure("IO_FAILURE", "Identity configuration is invalid; it was not replaced.") from error

    @staticmethod
    def _check(locations: dict[str, str], collection_id: str, directory: str) -> None:
        old = locations.get(collection_id)
        if old is None or os.path.normcase(old) == os.path.normcase(directory):
            return
        old_db, new_db = Path(old) / "knowledge.sqlite", Path(directory) / "knowledge.sqlite"
        if old_db.exists():
            try:
                if new_db.exists() and os.path.samefile(old_db, new_db):
                    return
            except OSError:
                pass
            raise CollectionFailure("INVALID_ARGUMENT", "The same identity is registered at another existing collection.", "Resolve the identity conflict; no collection was activated.")

    def check(self, collection_id: str, directory: str) -> None:
        _, locations = self._read()
        self._check(locations, collection_id, directory)

    @contextmanager
    def _lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A stable sidecar serializes independent instances/processes on Windows.
        with open(str(self.path) + ".lock", "a+b") as lock:
            lock.seek(0)
            if lock.read(1) == b"":
                lock.write(b"0"); lock.flush()
            lock.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                lock.seek(0)
                if os.name == "nt":
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def register(self, collection_id: str, directory: str) -> bool:
        temporary: str | None = None
        try:
            with self._lock():
                raw, locations = self._read()
                self._check(locations, collection_id, directory)
                if locations.get(collection_id) == directory:
                    return False
                locations[collection_id] = directory
                data = json.dumps({"configVersion": 1, "collections": locations}, ensure_ascii=False).encode("utf-8")
                fd, temporary = tempfile.mkstemp(prefix=".registry-", suffix=".tmp", dir=self.path.parent)
                with os.fdopen(fd, "wb") as output:
                    output.write(data); output.flush(); os.fsync(output.fileno())
                now = self.path.read_bytes() if self.path.exists() else None
                if now != raw:
                    raise CollectionFailure("IO_FAILURE", "Identity configuration changed concurrently; retry.")
                try:
                    os.replace(temporary, self.path)
                except OSError as error:
                    raise CollectionFailure("IO_FAILURE", "Identity registration result is unknown.", unknown=True) from error
                temporary = None
                return True
        except CollectionFailure:
            raise
        except OSError as error:
            raise CollectionFailure("IO_FAILURE", "Identity registration failed; collection was not activated.") from error
        finally:
            if temporary:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError:
                    pass  # Never remove or overwrite the actual configuration.
