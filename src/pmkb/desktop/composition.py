"""Explicit composition: no implicit editor, collection opening, or config writes."""
from dataclasses import dataclass
from pathlib import Path

from pmkb.application.collection.coordinator import CollectionCoordinatorService
from pmkb.application.collection.ports import CollectionSession
from pmkb.application.runtime import AsyncServiceRuntime
from pmkb.infrastructure.file_io.identity_registry import LocalIdentityRegistry
from pmkb.infrastructure.storage.collection_catalog import LocalCollectionBackend


@dataclass(frozen=True, slots=True)
class ApplicationServices:
    runtime: AsyncServiceRuntime
    collections: CollectionCoordinatorService
    config_file: Path

    @classmethod
    def compose(cls, session: CollectionSession, config_file: Path,
                runtime: AsyncServiceRuntime) -> 'ApplicationServices':
        if session is None:
            raise ValueError('A real editor session must be explicitly supplied; no headless fallback.')
        config_file = Path(config_file)
        if not config_file.is_absolute():
            raise ValueError('The application must supply an absolute configuration path.')
        registry = LocalIdentityRegistry(config_file)
        collections = CollectionCoordinatorService(LocalCollectionBackend(), registry, session)
        runtime.add_finalizer(collections.close)
        return cls(runtime, collections, config_file)
