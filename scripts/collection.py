"""Developer entry for API01 with real files and explicitly NO editor session."""
import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path

from pmkb.application.collection.coordinator import CollectionCoordinatorService
from pmkb.application.collection.headless_session import HeadlessCollectionSession
from pmkb.contracts.collections import ActivationMode, CompleteActivationRequest, PrepareActivationRequest
from pmkb.contracts.common import ResultStatus
from pmkb.infrastructure.file_io.identity_registry import LocalIdentityRegistry
from pmkb.infrastructure.storage.collection_catalog import LocalCollectionBackend


async def run(action: str, directory: Path, config: Path):
    session = HeadlessCollectionSession()
    service = CollectionCoordinatorService(LocalCollectionBackend(), LocalIdentityRegistry(config), session)
    prepared = await service.prepare_activation(PrepareActivationRequest(session.context, ActivationMode[action.upper()], str(directory)))
    if prepared.status != ResultStatus.OK:
        return prepared
    return await service.complete_activation(CompleteActivationRequest(session.context, prepared.value.ticket_id, ()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["create", "open"])
    parser.add_argument("directory", type=Path)
    parser.add_argument("--config", required=True, type=Path, help="Explicit identity-registry file outside the collection.")
    args = parser.parse_args()
    directory, config = args.directory.resolve(), args.config.resolve()
    if config.is_relative_to(directory):
        print(json.dumps({"status": "ERROR", "error": {"code": "INVALID_ARGUMENT", "reason": "Keep configuration outside the collection directory."}}))
        return 1
    result = asyncio.run(run(args.action, directory, config))
    print(json.dumps(asdict(result), ensure_ascii=True))
    return 0 if result.status == ResultStatus.OK else 1


if __name__ == "__main__":
    raise SystemExit(main())
