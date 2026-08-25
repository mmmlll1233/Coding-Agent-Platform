from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from mewcode.platform.domain import AttemptLease
from mewcode.platform.execution import SensitiveValueRedactor
from mewcode.platform.persistence import ArtifactMetadata, PlatformRepository

from .store import ArtifactStore


class ArtifactKind(StrEnum):
    AGENT_LOG = "agent_log"
    COMMAND_LOG = "command_log"
    DIFF = "diff"
    VERIFICATION_REPORT = "verification_report"


_TEXT_TYPES = {
    "application/json",
    "application/x-ndjson",
    "text/plain",
    "text/x-diff",
}


class ArtifactLimitError(RuntimeError):
    pass


class ArtifactIntegrityError(RuntimeError):
    pass


class ArtifactService:
    def __init__(
        self,
        repository: PlatformRepository,
        store: ArtifactStore,
        *,
        redactor: SensitiveValueRedactor,
        retention_days: int = 7,
        max_artifact_bytes: int = 64 * 1024 * 1024,
        max_attempt_bytes: int = 128 * 1024 * 1024,
    ) -> None:
        self.repository = repository
        self.store = store
        self.redactor = redactor
        self.retention_days = retention_days
        self.max_artifact_bytes = max_artifact_bytes
        self.max_attempt_bytes = max_attempt_bytes

    def _safe_content(self, content: bytes, content_type: str) -> bytes:
        if content_type not in _TEXT_TYPES:
            return content
        text = content.decode("utf-8", errors="replace")
        return self.redactor.redact(text).encode("utf-8")

    async def persist_bytes(
        self,
        lease: AttemptLease,
        *,
        kind: ArtifactKind,
        content: bytes,
        content_type: str,
    ) -> ArtifactMetadata:
        safe = self._safe_content(content, content_type)
        if len(safe) > self.max_artifact_bytes:
            raise ArtifactLimitError("Artifact exceeds the single Artifact limit")
        used = await self.repository.attempt_artifact_bytes(lease.attempt_id)
        if used + len(safe) > self.max_attempt_bytes:
            raise ArtifactLimitError("Attempt exceeds its total Artifact limit")
        artifact_id = uuid4()
        storage_key = f"{lease.job_id}/{lease.attempt_id}/{artifact_id}"
        digest = hashlib.sha256(safe).hexdigest()
        expires_at = datetime.now(UTC) + timedelta(days=self.retention_days)
        metadata = ArtifactMetadata(
            id=artifact_id,
            job_id=lease.job_id,
            attempt_id=lease.attempt_id,
            kind=kind.value,
            storage_key=storage_key,
            sha256=digest,
            size_bytes=len(safe),
            content_type=content_type,
            expires_at=expires_at,
        )
        await asyncio.to_thread(self.store.put, storage_key, safe)
        try:
            return await self.repository.add_artifact_fenced(
                artifact=metadata,
                worker_id=lease.worker_id,
                fencing_token=lease.fencing_token,
            )
        except Exception:
            await asyncio.to_thread(self.store.delete, storage_key)
            raise

    async def persist_file(
        self,
        lease: AttemptLease,
        *,
        kind: ArtifactKind,
        source: Path,
        content_type: str,
    ) -> ArtifactMetadata:
        content = await asyncio.to_thread(Path(source).read_bytes)
        return await self.persist_bytes(
            lease,
            kind=kind,
            content=content,
            content_type=content_type,
        )

    async def read_bytes(self, artifact: ArtifactMetadata) -> bytes:
        content = await asyncio.to_thread(self.store.read, artifact.storage_key)
        digest = hashlib.sha256(content).hexdigest()
        if len(content) != artifact.size_bytes or digest != artifact.sha256:
            raise ArtifactIntegrityError("Artifact bytes do not match trusted metadata")
        return content

    async def cleanup_expired(self, *, limit: int = 100) -> tuple[int, int]:
        artifacts = await self.repository.list_expired_artifacts(limit=limit)
        deleted_artifacts = 0
        for artifact in artifacts:
            await asyncio.to_thread(self.store.delete, artifact.storage_key)
            if await self.repository.delete_artifact_metadata(artifact.id):
                deleted_artifacts += 1
        deleted_jobs = await self.repository.delete_expired_terminal_jobs(limit=limit)
        return deleted_artifacts, deleted_jobs
