from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Protocol, runtime_checkable

_STORAGE_KEY = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


class ArtifactStoreError(RuntimeError):
    pass


@runtime_checkable
class ArtifactStore(Protocol):
    """Trusted byte store addressed only by platform-generated storage keys."""

    def put(self, storage_key: str, content: bytes) -> Path: ...

    def read(self, storage_key: str) -> bytes: ...

    def delete(self, storage_key: str) -> None: ...


class LocalArtifactStore:
    """Trusted local Artifact bytes addressed only by platform-generated UUIDs."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def path_for(self, storage_key: str) -> Path:
        if not _STORAGE_KEY.fullmatch(storage_key):
            raise ArtifactStoreError("invalid Artifact storage key")
        path = (self.root / Path(storage_key)).resolve()
        if self.root not in path.parents:
            raise ArtifactStoreError("Artifact storage key escapes the store")
        return path

    def put(self, storage_key: str, content: bytes) -> Path:
        destination = self.path_for(storage_key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("xb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return destination

    def read(self, storage_key: str) -> bytes:
        return self.path_for(storage_key).read_bytes()

    def delete(self, storage_key: str) -> None:
        path = self.path_for(storage_key)
        path.unlink(missing_ok=True)
        current = path.parent
        while current != self.root:
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent
