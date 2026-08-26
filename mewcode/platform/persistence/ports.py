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


@dataclass(frozen=True)
class ClaimedNotification:
    id: UUID
    job_id: UUID
    source_event_sequence: int
    event_type: str
    destination: str
    payload: dict[str, Any]
    attempt_count: int
    notifier_id: str
    fencing_token: UUID


@dataclass(frozen=True)
class NotificationOutboxStats:
    pending: int
    in_flight: int
    delivered: int
    oldest_pending_seconds: float


class NotificationOutboxRepository(Protocol):
    async def enqueue(self, message: NotificationMessage) -> None: ...

    async def claim_notification(
        self, *, notifier_id: str, lease_seconds: int
    ) -> ClaimedNotification | None: ...

    async def mark_notification_delivered(
        self,
        *,
        notification_id: UUID,
        notifier_id: str,
        fencing_token: UUID,
    ) -> bool: ...

    async def retry_notification(
        self,
        *,
        notification_id: UUID,
        notifier_id: str,
        fencing_token: UUID,
        next_attempt_at: datetime,
        error: str,
    ) -> bool: ...

    async def notification_outbox_stats(self) -> NotificationOutboxStats: ...
