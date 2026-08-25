from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID


@dataclass(frozen=True)
class ArtifactMetadata:
    id: UUID
    job_id: UUID
    attempt_id: UUID | None
    kind: str
    storage_key: str
    sha256: str
    size_bytes: int
    content_type: str
    expires_at: datetime
    created_at: datetime | None = None


class ArtifactMetadataRepository(Protocol):
    async def add(self, artifact: ArtifactMetadata) -> None: ...

    async def get(self, artifact_id: UUID) -> ArtifactMetadata | None: ...


@dataclass(frozen=True)
class NotificationMessage:
    job_id: UUID
    event_type: str
    destination: str
    payload: dict[str, Any]


class NotificationOutboxRepository(Protocol):
    async def enqueue(self, message: NotificationMessage) -> None: ...
