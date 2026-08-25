from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mewcode.platform.domain import (
    AttemptLease,
    AttemptOutcome,
    AttemptOutcomeStatus,
    AttemptStage,
    AttemptStatus,
    JobStatus,
    RepositoryTarget,
    ensure_attempt_transition,
    ensure_job_transition,
)

from .database import Database
from .orm import (
    ApiKeyRow,
    ArtifactRow,
    AttemptRow,
    JobEventRow,
    JobInputRow,
    JobRow,
    RequesterRow,
    TenantRow,
    WorkerNodeRow,
)
from .ports import ArtifactMetadata

CLAIM_ADVISORY_LOCK = 5_251_903_303


class PersistenceError(RuntimeError):
    pass


class NotFound(PersistenceError):
    pass


class StateConflict(PersistenceError):
    def __init__(self, message: str, *, code: str = "STATE_CONFLICT") -> None:
        super().__init__(message)
        self.code = code


class IdempotencyConflict(StateConflict):
    def __init__(self) -> None:
        super().__init__(
            "Idempotency-Key was already used with a different request",
            code="IDEMPOTENCY_CONFLICT",
        )


class LeaseLost(StateConflict):
    def __init__(self) -> None:
        super().__init__(
            "The Worker Lease is missing, expired, or owned by another Worker",
            code="LEASE_LOST",
        )


@dataclass(frozen=True)
class ApiKeyPrincipal:
    tenant_id: UUID
    requester_id: UUID
    key_id: UUID
    requester_name: str


@dataclass(frozen=True)
class ClaimedAttempt:
    lease: AttemptLease


@dataclass(frozen=True)
class StoredEvent:
    id: UUID
    job_id: UUID
    attempt_id: UUID | None
    sequence: int
    attempt_sequence: int | None
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


class PlatformRepository:
    def __init__(
        self, database: Database, *, metadata_retention_days: int = 30
    ) -> None:
        self.database = database
        self.metadata_retention_days = metadata_retention_days

    @staticmethod
    async def _database_now(session: AsyncSession) -> datetime:
        value = await session.scalar(select(func.now()))
        assert isinstance(value, datetime)
        return value

    @staticmethod
    async def _append_event(
        session: AsyncSession,
        *,
        job_id: UUID,
        attempt_id: UUID | None,
        event_type: str,
        payload: dict[str, Any] | None = None,
        attempt_sequence: int | None = None,
    ) -> JobEventRow:
        sequence = await session.scalar(
            update(JobRow)
            .where(JobRow.id == job_id)
            .values(
                next_event_sequence=JobRow.next_event_sequence + 1,
                updated_at=func.now(),
            )
            .returning(JobRow.next_event_sequence)
        )
        if sequence is None:
            raise NotFound("Job does not exist")
        event = JobEventRow(
            id=uuid4(),
            job_id=job_id,
            attempt_id=attempt_id,
            sequence=int(sequence),
            attempt_sequence=attempt_sequence,
            event_type=event_type,
            payload=payload or {},
        )
        session.add(event)
        return event

    async def ensure_requester(
        self, *, tenant_name: str, requester_name: str
    ) -> ApiKeyPrincipal:
        async with self.database.sessions.begin() as session:
            tenant = await session.scalar(
                select(TenantRow).where(TenantRow.name == tenant_name)
            )
            if tenant is None:
                tenant = TenantRow(id=uuid4(), name=tenant_name)
                session.add(tenant)
                await session.flush()
            requester = await session.scalar(
                select(RequesterRow).where(
                    RequesterRow.tenant_id == tenant.id,
                    RequesterRow.name == requester_name,
                )
            )
            if requester is None:
                requester = RequesterRow(
                    id=uuid4(), tenant_id=tenant.id, name=requester_name, active=True
                )
                session.add(requester)
                await session.flush()
            return ApiKeyPrincipal(
                tenant_id=tenant.id,
                requester_id=requester.id,
                key_id=UUID(int=0),
                requester_name=requester.name,
            )

    async def create_api_key(
        self, *, tenant_name: str, requester_name: str
    ) -> tuple[str, ApiKeyPrincipal]:
        principal = await self.ensure_requester(
            tenant_name=tenant_name, requester_name=requester_name
        )
        key_id = uuid4()
        prefix = f"mew_live_{key_id.hex}"
        secret = secrets.token_urlsafe(32)
        digest = hashlib.sha256(secret.encode("utf-8")).digest()
        async with self.database.sessions.begin() as session:
            session.add(
                ApiKeyRow(
                    id=key_id,
                    requester_id=principal.requester_id,
                    key_prefix=prefix,
                    secret_hash=digest,
                )
            )
        return (
            f"{prefix}.{secret}",
            ApiKeyPrincipal(
                tenant_id=principal.tenant_id,
                requester_id=principal.requester_id,
                key_id=key_id,
                requester_name=principal.requester_name,
            ),
        )

    async def revoke_api_key(self, key_id: UUID) -> bool:
        async with self.database.sessions.begin() as session:
            result = await session.execute(
                update(ApiKeyRow)
                .where(ApiKeyRow.id == key_id, ApiKeyRow.revoked_at.is_(None))
                .values(revoked_at=func.now())
            )
            return result.rowcount == 1

    async def authenticate_api_key(self, token: str) -> ApiKeyPrincipal | None:
        prefix, separator, secret = token.partition(".")
        if not separator or not prefix.startswith("mew_live_") or not secret:
            return None
        async with self.database.sessions() as session:
            row = (
                await session.execute(
                    select(ApiKeyRow, RequesterRow)
                    .join(RequesterRow, RequesterRow.id == ApiKeyRow.requester_id)
                    .where(
                        ApiKeyRow.key_prefix == prefix,
                        ApiKeyRow.revoked_at.is_(None),
                        RequesterRow.active.is_(True),
                    )
                )
            ).one_or_none()
        if row is None:
            return None
        api_key, requester = row
        candidate = hashlib.sha256(secret.encode("utf-8")).digest()
        if not hmac.compare_digest(candidate, api_key.secret_hash):
            return None
        return ApiKeyPrincipal(
            tenant_id=requester.tenant_id,
            requester_id=requester.id,
            key_id=api_key.id,
            requester_name=requester.name,
        )

    async def lookup_idempotent_job(
        self,
        *,
        principal: ApiKeyPrincipal,
        idempotency_key: str,
        request_hash: str,
    ) -> JobRow | None:
        async with self.database.sessions() as session:
            job = await session.scalar(
                select(JobRow).where(
                    JobRow.tenant_id == principal.tenant_id,
                    JobRow.requester_id == principal.requester_id,
                    JobRow.idempotency_key == idempotency_key,
                )
            )
            if job is not None and job.request_hash != request_hash:
                raise IdempotencyConflict()
            return job

    async def create_job(
        self,
        *,
        principal: ApiKeyPrincipal,
        idempotency_key: str,
        request_hash: str,
        target: RepositoryTarget,
        work_request: dict[str, Any],
        execution_contract: dict[str, Any],
    ) -> tuple[JobRow, bool]:
        existing = await self.lookup_idempotent_job(
            principal=principal,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if existing is not None:
            return existing, True

        job_id = uuid4()
        attempt_id = uuid4()
        try:
            async with self.database.sessions.begin() as session:
                job = JobRow(
                    id=job_id,
                    tenant_id=principal.tenant_id,
                    requester_id=principal.requester_id,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    status=JobStatus.QUEUED.value,
                    stage=None,
                    current_attempt_no=1,
                    installation_id=target.installation_id,
                    repo_owner=target.owner,
                    repo_name=target.name,
                    base_ref=target.base_ref,
                    base_sha=target.base_sha,
                    work_request=work_request,
                    execution_contract=execution_contract,
                    attachment_ids=[],
                )
                attempt = AttemptRow(
                    id=attempt_id,
                    job_id=job_id,
                    attempt_no=1,
                    status=AttemptStatus.QUEUED.value,
                )
                session.add(job)
                await session.flush()
                session.add(attempt)
                await session.flush()
                await self._append_event(
                    session,
                    job_id=job_id,
                    attempt_id=attempt_id,
                    event_type="job_received",
                    payload={"status": JobStatus.RECEIVED.value},
                )
                await self._append_event(
                    session,
                    job_id=job_id,
                    attempt_id=attempt_id,
                    event_type="job_queued",
                    payload={"status": JobStatus.QUEUED.value, "attempt_no": 1},
                )
        except IntegrityError:
            existing = await self.lookup_idempotent_job(
                principal=principal,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if existing is None:
                raise
            return existing, True
        created = await self.get_job(principal=principal, job_id=job_id)
        return created, False

    async def get_job(self, *, principal: ApiKeyPrincipal, job_id: UUID) -> JobRow:
        async with self.database.sessions() as session:
            job = await session.scalar(
                select(JobRow).where(
                    JobRow.id == job_id,
                    JobRow.tenant_id == principal.tenant_id,
                    JobRow.requester_id == principal.requester_id,
                )
            )
            if job is None:
                raise NotFound("Job does not exist")
            return job

    async def get_current_attempt(
        self, *, principal: ApiKeyPrincipal, job_id: UUID
    ) -> AttemptRow | None:
        async with self.database.sessions() as session:
            row = await session.scalar(
                select(AttemptRow)
                .join(JobRow, JobRow.id == AttemptRow.job_id)
                .where(
                    JobRow.id == job_id,
                    JobRow.tenant_id == principal.tenant_id,
                    JobRow.requester_id == principal.requester_id,
                    AttemptRow.attempt_no == JobRow.current_attempt_no,
                )
            )
            return row

    async def list_events(
        self,
        *,
        principal: ApiKeyPrincipal,
        job_id: UUID,
        after: int = 0,
        limit: int = 100,
    ) -> list[StoredEvent]:
        async with self.database.sessions() as session:
            exists = await session.scalar(
                select(JobRow.id).where(
                    JobRow.id == job_id,
                    JobRow.tenant_id == principal.tenant_id,
                    JobRow.requester_id == principal.requester_id,
                )
            )
            if exists is None:
                raise NotFound("Job does not exist")
            rows = (
                await session.scalars(
                    select(JobEventRow)
                    .where(JobEventRow.job_id == job_id, JobEventRow.sequence > after)
                    .order_by(JobEventRow.sequence)
                    .limit(limit)
                )
            ).all()
            return [
                StoredEvent(
                    id=row.id,
                    job_id=row.job_id,
                    attempt_id=row.attempt_id,
                    sequence=row.sequence,
                    attempt_sequence=row.attempt_sequence,
                    event_type=row.event_type,
                    payload=row.payload,
                    created_at=row.created_at,
                )
                for row in rows
            ]

    @staticmethod
    def _artifact_metadata(row: ArtifactRow) -> ArtifactMetadata:
        return ArtifactMetadata(
            id=row.id,
            job_id=row.job_id,
            attempt_id=row.attempt_id,
            kind=row.kind,
            storage_key=row.storage_key,
            sha256=row.sha256,
            size_bytes=row.size_bytes,
            content_type=row.content_type,
            expires_at=row.expires_at,
            created_at=row.created_at,
        )

    async def attempt_artifact_bytes(self, attempt_id: UUID) -> int:
        async with self.database.sessions() as session:
            value = await session.scalar(
                select(func.coalesce(func.sum(ArtifactRow.size_bytes), 0)).where(
                    ArtifactRow.attempt_id == attempt_id
                )
            )
            return int(value or 0)

    async def add_artifact_fenced(
        self,
        *,
        artifact: ArtifactMetadata,
        worker_id: str,
        fencing_token: UUID,
    ) -> ArtifactMetadata:
        if artifact.attempt_id is None:
            raise ValueError("Attempt Artifact requires attempt_id")
        async with self.database.sessions.begin() as session:
            attempt, job, _ = await self._locked_lease(
                session,
                attempt_id=artifact.attempt_id,
                worker_id=worker_id,
                fencing_token=fencing_token,
            )
            if artifact.job_id != job.id or artifact.attempt_id != attempt.id:
                raise LeaseLost()
            row = ArtifactRow(
                id=artifact.id,
                job_id=artifact.job_id,
                attempt_id=artifact.attempt_id,
                kind=artifact.kind,
                storage_key=artifact.storage_key,
                sha256=artifact.sha256,
                size_bytes=artifact.size_bytes,
                content_type=artifact.content_type,
                expires_at=artifact.expires_at,
            )
            session.add(row)
            await session.flush()
            await self._append_event(
                session,
                job_id=job.id,
                attempt_id=attempt.id,
                event_type="artifact_created",
                payload={
                    "artifact_id": str(row.id),
                    "kind": row.kind,
                    "sha256": row.sha256,
                    "size_bytes": row.size_bytes,
                    "expires_at": row.expires_at.isoformat(),
                },
            )
            await session.refresh(row)
            return self._artifact_metadata(row)

    async def list_artifacts(
        self, *, principal: ApiKeyPrincipal, job_id: UUID
    ) -> list[ArtifactMetadata]:
        async with self.database.sessions() as session:
            job = await session.scalar(
                select(JobRow.id).where(
                    JobRow.id == job_id,
                    JobRow.tenant_id == principal.tenant_id,
                    JobRow.requester_id == principal.requester_id,
                )
            )
            if job is None:
                raise NotFound("Job does not exist")
            now = await self._database_now(session)
            rows = (
                await session.scalars(
                    select(ArtifactRow)
                    .where(
                        ArtifactRow.job_id == job_id,
                        ArtifactRow.expires_at > now,
                    )
                    .order_by(ArtifactRow.created_at, ArtifactRow.id)
                )
            ).all()
            return [self._artifact_metadata(row) for row in rows]

    async def get_artifact(
        self,
        *,
        principal: ApiKeyPrincipal,
        job_id: UUID,
        artifact_id: UUID,
    ) -> ArtifactMetadata:
        async with self.database.sessions() as session:
            now = await self._database_now(session)
            row = await session.scalar(
                select(ArtifactRow)
                .join(JobRow, JobRow.id == ArtifactRow.job_id)
                .where(
                    ArtifactRow.id == artifact_id,
                    ArtifactRow.job_id == job_id,
                    ArtifactRow.expires_at > now,
                    JobRow.tenant_id == principal.tenant_id,
                    JobRow.requester_id == principal.requester_id,
                )
            )
            if row is None:
                raise NotFound("Artifact does not exist")
            return self._artifact_metadata(row)

    async def list_expired_artifacts(
        self, *, limit: int = 100
    ) -> list[ArtifactMetadata]:
        async with self.database.sessions() as session:
            now = await self._database_now(session)
            rows = (
                await session.scalars(
                    select(ArtifactRow)
                    .where(ArtifactRow.expires_at <= now)
                    .order_by(ArtifactRow.expires_at, ArtifactRow.id)
                    .limit(limit)
                )
            ).all()
            return [self._artifact_metadata(row) for row in rows]

    async def delete_artifact_metadata(self, artifact_id: UUID) -> bool:
        async with self.database.sessions.begin() as session:
            result = await session.execute(
                delete(ArtifactRow).where(ArtifactRow.id == artifact_id)
            )
            return result.rowcount == 1

    async def delete_expired_terminal_jobs(self, *, limit: int = 100) -> int:
        terminal = (
            JobStatus.SUCCEEDED.value,
            JobStatus.FAILED.value,
            JobStatus.CANCELLED.value,
        )
        async with self.database.sessions.begin() as session:
            now = await self._database_now(session)
            ids = (
                await session.scalars(
                    select(JobRow.id)
                    .where(
                        JobRow.status.in_(terminal),
                        JobRow.retention_until.is_not(None),
                        JobRow.retention_until <= now,
                    )
                    .order_by(JobRow.retention_until, JobRow.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            if not ids:
                return 0
            result = await session.execute(delete(JobRow).where(JobRow.id.in_(ids)))
            return int(result.rowcount or 0)

    async def add_input(
        self,
        *,
        principal: ApiKeyPrincipal,
        job_id: UUID,
        content: str,
    ) -> JobRow:
        async with self.database.sessions.begin() as session:
            job = await session.scalar(
                select(JobRow)
                .where(
                    JobRow.id == job_id,
                    JobRow.tenant_id == principal.tenant_id,
                    JobRow.requester_id == principal.requester_id,
                )
                .with_for_update()
            )
            if job is None:
                raise NotFound("Job does not exist")
            current = JobStatus(job.status)
            if current != JobStatus.NEEDS_INPUT:
                raise StateConflict(
                    "Input is accepted only while the Job Needs Input",
                    code="JOB_NOT_NEEDS_INPUT",
                )
            ensure_job_transition(current, JobStatus.QUEUED)
            previous_attempt = await session.scalar(
                select(AttemptRow).where(
                    AttemptRow.job_id == job.id,
                    AttemptRow.attempt_no == job.current_attempt_no,
                )
            )
            session.add(
                JobInputRow(
                    id=uuid4(),
                    job_id=job.id,
                    attempt_id=previous_attempt.id if previous_attempt else None,
                    content=content,
                    attachment_ids=[],
                )
            )
            await self._queue_new_attempt(session, job, event_type="input_received")
        return await self.get_job(principal=principal, job_id=job_id)

    async def retry_job(self, *, principal: ApiKeyPrincipal, job_id: UUID) -> JobRow:
        async with self.database.sessions.begin() as session:
            job = await session.scalar(
                select(JobRow)
                .where(
                    JobRow.id == job_id,
                    JobRow.tenant_id == principal.tenant_id,
                    JobRow.requester_id == principal.requester_id,
                )
                .with_for_update()
            )
            if job is None:
                raise NotFound("Job does not exist")
            current = JobStatus(job.status)
            if current != JobStatus.FAILED:
                raise StateConflict(
                    "Only a Failed Job can be retried", code="JOB_NOT_FAILED"
                )
            ensure_job_transition(current, JobStatus.QUEUED)
            await self._queue_new_attempt(session, job, event_type="job_retried")
        return await self.get_job(principal=principal, job_id=job_id)

    async def _queue_new_attempt(
        self, session: AsyncSession, job: JobRow, *, event_type: str
    ) -> AttemptRow:
        attempt_no = job.current_attempt_no + 1
        attempt = AttemptRow(
            id=uuid4(),
            job_id=job.id,
            attempt_no=attempt_no,
            status=AttemptStatus.QUEUED.value,
        )
        session.add(attempt)
        job.current_attempt_no = attempt_no
        job.status = JobStatus.QUEUED.value
        job.stage = None
        job.error_code = None
        job.error_message = None
        job.finished_at = None
        job.retention_until = None
        job.updated_at = await self._database_now(session)
        await session.flush()
        await self._append_event(
            session,
            job_id=job.id,
            attempt_id=attempt.id,
            event_type=event_type,
            payload={"status": JobStatus.QUEUED.value, "attempt_no": attempt_no},
        )
        return attempt

    async def cancel_job(self, *, principal: ApiKeyPrincipal, job_id: UUID) -> JobRow:
        async with self.database.sessions.begin() as session:
            job = await session.scalar(
                select(JobRow)
                .where(
                    JobRow.id == job_id,
                    JobRow.tenant_id == principal.tenant_id,
                    JobRow.requester_id == principal.requester_id,
                )
                .with_for_update()
            )
            if job is None:
                raise NotFound("Job does not exist")
            current = JobStatus(job.status)
            if current in {JobStatus.CANCELLED, JobStatus.CANCEL_REQUESTED}:
                return job
            attempt = await session.scalar(
                select(AttemptRow).where(
                    AttemptRow.job_id == job.id,
                    AttemptRow.attempt_no == job.current_attempt_no,
                )
            )
            now = await self._database_now(session)
            if current in {JobStatus.QUEUED, JobStatus.NEEDS_INPUT}:
                ensure_job_transition(current, JobStatus.CANCELLED)
                job.status = JobStatus.CANCELLED.value
                job.finished_at = now
                job.retention_until = now + timedelta(days=self.metadata_retention_days)
                if attempt is not None and attempt.status == AttemptStatus.QUEUED.value:
                    ensure_attempt_transition(
                        AttemptStatus.QUEUED, AttemptStatus.CANCELLED
                    )
                    attempt.status = AttemptStatus.CANCELLED.value
                    attempt.finished_at = now
                event_type = "job_cancelled"
            elif current == JobStatus.RUNNING:
                if job.stage == AttemptStage.PUBLISHING.value:
                    raise StateConflict(
                        "Job cannot be cancelled after publication begins",
                        code="JOB_NOT_CANCELLABLE",
                    )
                ensure_job_transition(current, JobStatus.CANCEL_REQUESTED)
                job.status = JobStatus.CANCEL_REQUESTED.value
                event_type = "job_cancel_requested"
            else:
                raise StateConflict(
                    f"Job in {current.value} cannot be cancelled",
                    code="JOB_NOT_CANCELLABLE",
                )
            job.updated_at = now
            await self._append_event(
                session,
                job_id=job.id,
                attempt_id=attempt.id if attempt else None,
                event_type=event_type,
                payload={"status": job.status},
            )
        return await self.get_job(principal=principal, job_id=job_id)

    async def register_worker(
        self, worker_id: str, metadata: dict[str, Any] | None = None
    ) -> None:
        async with self.database.sessions.begin() as session:
            existing = await session.get(WorkerNodeRow, worker_id)
            if existing is None:
                session.add(
                    WorkerNodeRow(
                        id=worker_id,
                        metadata_json=metadata or {},
                    )
                )
            else:
                existing.started_at = await self._database_now(session)
                existing.heartbeat_at = existing.started_at
                existing.metadata_json = metadata or {}

    async def heartbeat_worker(self, worker_id: str) -> bool:
        async with self.database.sessions.begin() as session:
            result = await session.execute(
                update(WorkerNodeRow)
                .where(WorkerNodeRow.id == worker_id)
                .values(heartbeat_at=func.now())
            )
            return result.rowcount == 1

    async def has_fresh_worker(self, stale_seconds: int) -> bool:
        async with self.database.sessions() as session:
            now = await self._database_now(session)
            worker = await session.scalar(
                select(WorkerNodeRow.id)
                .where(
                    WorkerNodeRow.heartbeat_at >= now - timedelta(seconds=stale_seconds)
                )
                .limit(1)
            )
            return worker is not None

    async def claim_attempt(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        max_concurrent_jobs: int,
    ) -> ClaimedAttempt | None:
        async with self.database.sessions.begin() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": CLAIM_ADVISORY_LOCK},
            )
            now = await self._database_now(session)
            active = await session.scalar(
                select(func.count(AttemptRow.id)).where(
                    AttemptRow.status == AttemptStatus.RUNNING.value,
                    AttemptRow.lease_expires_at > now,
                )
            )
            if int(active or 0) >= max_concurrent_jobs:
                return None

            attempt = await session.scalar(
                select(AttemptRow)
                .join(JobRow, JobRow.id == AttemptRow.job_id)
                .where(
                    AttemptRow.status == AttemptStatus.QUEUED.value,
                    JobRow.status == JobStatus.QUEUED.value,
                    AttemptRow.attempt_no == JobRow.current_attempt_no,
                )
                .order_by(AttemptRow.queued_at, AttemptRow.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if attempt is None:
                return None
            job = await session.scalar(
                select(JobRow).where(JobRow.id == attempt.job_id).with_for_update()
            )
            assert job is not None
            ensure_attempt_transition(AttemptStatus.QUEUED, AttemptStatus.RUNNING)
            ensure_job_transition(JobStatus.QUEUED, JobStatus.RUNNING)
            token = uuid4()
            expires = now + timedelta(seconds=lease_seconds)
            attempt.status = AttemptStatus.RUNNING.value
            attempt.stage = AttemptStage.PREPARING.value
            attempt.worker_id = worker_id
            attempt.fencing_token = token
            attempt.heartbeat_at = now
            attempt.lease_expires_at = expires
            attempt.started_at = attempt.started_at or now
            job.status = JobStatus.RUNNING.value
            job.stage = AttemptStage.PREPARING.value
            job.updated_at = now
            await self._append_event(
                session,
                job_id=job.id,
                attempt_id=attempt.id,
                event_type="attempt_started",
                payload={
                    "status": JobStatus.RUNNING.value,
                    "attempt_no": attempt.attempt_no,
                    "stage": AttemptStage.PREPARING.value,
                },
            )
            inputs = (
                await session.scalars(
                    select(JobInputRow)
                    .where(JobInputRow.job_id == job.id)
                    .order_by(JobInputRow.created_at, JobInputRow.id)
                )
            ).all()
            lease = AttemptLease(
                job_id=job.id,
                attempt_id=attempt.id,
                attempt_no=attempt.attempt_no,
                tenant_id=job.tenant_id,
                requester_id=job.requester_id,
                worker_id=worker_id,
                fencing_token=token,
                lease_expires_at=expires,
                repository_target=RepositoryTarget(
                    installation_id=job.installation_id,
                    owner=job.repo_owner,
                    name=job.repo_name,
                    base_ref=job.base_ref,
                    base_sha=job.base_sha or "",
                ),
                work_request=job.work_request,
                execution_contract=job.execution_contract,
                inputs=tuple(
                    {
                        "content": item.content,
                        "attachment_ids": item.attachment_ids,
                        "created_at": item.created_at.isoformat(),
                    }
                    for item in inputs
                ),
            )
            return ClaimedAttempt(lease=lease)

    @staticmethod
    async def _locked_lease(
        session: AsyncSession,
        *,
        attempt_id: UUID,
        worker_id: str,
        fencing_token: UUID,
        require_live: bool = True,
    ) -> tuple[AttemptRow, JobRow, datetime]:
        now = await PlatformRepository._database_now(session)
        conditions = [
            AttemptRow.id == attempt_id,
            AttemptRow.status == AttemptStatus.RUNNING.value,
            AttemptRow.worker_id == worker_id,
            AttemptRow.fencing_token == fencing_token,
        ]
        if require_live:
            conditions.append(AttemptRow.lease_expires_at > now)
        attempt = await session.scalar(
            select(AttemptRow).where(*conditions).with_for_update()
        )
        if attempt is None:
            raise LeaseLost()
        job = await session.scalar(
            select(JobRow).where(JobRow.id == attempt.job_id).with_for_update()
        )
        if job is None or attempt.attempt_no != job.current_attempt_no:
            raise LeaseLost()
        return attempt, job, now

    async def heartbeat_lease(
        self,
        *,
        attempt_id: UUID,
        worker_id: str,
        fencing_token: UUID,
        lease_seconds: int,
    ) -> datetime:
        async with self.database.sessions.begin() as session:
            attempt, _, now = await self._locked_lease(
                session,
                attempt_id=attempt_id,
                worker_id=worker_id,
                fencing_token=fencing_token,
            )
            attempt.heartbeat_at = now
            attempt.lease_expires_at = now + timedelta(seconds=lease_seconds)
            return attempt.lease_expires_at

    async def is_cancel_requested(
        self,
        *,
        attempt_id: UUID,
        worker_id: str,
        fencing_token: UUID,
    ) -> bool:
        async with self.database.sessions() as session:
            now = await self._database_now(session)
            status = await session.scalar(
                select(JobRow.status)
                .join(AttemptRow, AttemptRow.job_id == JobRow.id)
                .where(
                    AttemptRow.id == attempt_id,
                    AttemptRow.status == AttemptStatus.RUNNING.value,
                    AttemptRow.worker_id == worker_id,
                    AttemptRow.fencing_token == fencing_token,
                    AttemptRow.lease_expires_at > now,
                    AttemptRow.attempt_no == JobRow.current_attempt_no,
                )
            )
            if status is None:
                raise LeaseLost()
            return status == JobStatus.CANCEL_REQUESTED.value

    async def report_stage(
        self,
        *,
        attempt_id: UUID,
        worker_id: str,
        fencing_token: UUID,
        stage: AttemptStage,
    ) -> None:
        async with self.database.sessions.begin() as session:
            attempt, job, _ = await self._locked_lease(
                session,
                attempt_id=attempt_id,
                worker_id=worker_id,
                fencing_token=fencing_token,
            )
            if job.status != JobStatus.RUNNING.value:
                raise StateConflict(
                    "A cancelling Job cannot advance stages",
                    code="JOB_CANCEL_REQUESTED",
                )
            attempt.stage = stage.value
            job.stage = stage.value
            await self._append_event(
                session,
                job_id=job.id,
                attempt_id=attempt.id,
                event_type="stage_changed",
                payload={"stage": stage.value},
            )

    async def append_attempt_event(
        self,
        *,
        attempt_id: UUID,
        worker_id: str,
        fencing_token: UUID,
        attempt_sequence: int,
        event_type: str,
        payload: dict[str, Any],
    ) -> int:
        async with self.database.sessions.begin() as session:
            attempt, job, _ = await self._locked_lease(
                session,
                attempt_id=attempt_id,
                worker_id=worker_id,
                fencing_token=fencing_token,
            )
            event = await self._append_event(
                session,
                job_id=job.id,
                attempt_id=attempt.id,
                event_type=event_type,
                payload=payload,
                attempt_sequence=attempt_sequence,
            )
            await session.flush()
            return event.sequence

    async def finish_attempt(
        self,
        *,
        attempt_id: UUID,
        worker_id: str,
        fencing_token: UUID,
        outcome: AttemptOutcome,
    ) -> None:
        async with self.database.sessions.begin() as session:
            attempt, job, now = await self._locked_lease(
                session,
                attempt_id=attempt_id,
                worker_id=worker_id,
                fencing_token=fencing_token,
            )
            if job.status == JobStatus.CANCEL_REQUESTED.value:
                outcome = AttemptOutcome(status=AttemptOutcomeStatus.CANCELLED)

            if outcome.status == AttemptOutcomeStatus.COMPLETED:
                try:
                    ensure_job_transition(
                        JobStatus(job.status),
                        JobStatus.SUCCEEDED,
                        pr_number=outcome.pr_number,
                        pr_url=outcome.pr_url,
                        head_branch=outcome.head_branch,
                        head_sha=outcome.head_sha,
                        verification_succeeded=outcome.verification_succeeded,
                    )
                except ValueError:
                    outcome = AttemptOutcome(
                        status=AttemptOutcomeStatus.FAILED,
                        error_code="INCOMPLETE_DELIVERY",
                        error_message=(
                            "Attempt completed without a verified Draft Pull Request"
                        ),
                    )

            attempt.lease_expires_at = None
            attempt.heartbeat_at = now
            attempt.finished_at = now
            attempt.stage = AttemptStage.CLEANING_UP.value
            attempt.usage = outcome.usage
            job.updated_at = now

            if outcome.status == AttemptOutcomeStatus.COMPLETED:
                ensure_attempt_transition(
                    AttemptStatus.RUNNING, AttemptStatus.COMPLETED
                )
                attempt.status = AttemptStatus.COMPLETED.value
                job.status = JobStatus.SUCCEEDED.value
                job.pr_number = outcome.pr_number
                job.pr_url = outcome.pr_url
                job.head_branch = outcome.head_branch
                job.head_sha = outcome.head_sha
                job.verification_succeeded = outcome.verification_succeeded
                job.finished_at = now
                job.retention_until = now + timedelta(days=self.metadata_retention_days)
                event_type = "job_succeeded"
            elif outcome.status == AttemptOutcomeStatus.NEEDS_INPUT:
                ensure_attempt_transition(
                    AttemptStatus.RUNNING, AttemptStatus.NEEDS_INPUT
                )
                ensure_job_transition(JobStatus(job.status), JobStatus.NEEDS_INPUT)
                attempt.status = AttemptStatus.NEEDS_INPUT.value
                job.status = JobStatus.NEEDS_INPUT.value
                job.finished_at = None
                event_type = "job_needs_input"
            elif outcome.status == AttemptOutcomeStatus.CANCELLED:
                ensure_attempt_transition(
                    AttemptStatus.RUNNING, AttemptStatus.CANCELLED
                )
                if job.status == JobStatus.RUNNING.value:
                    ensure_job_transition(JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED)
                    job.status = JobStatus.CANCEL_REQUESTED.value
                ensure_job_transition(JobStatus(job.status), JobStatus.CANCELLED)
                attempt.status = AttemptStatus.CANCELLED.value
                job.status = JobStatus.CANCELLED.value
                job.finished_at = now
                job.retention_until = now + timedelta(days=self.metadata_retention_days)
                event_type = "job_cancelled"
            else:
                ensure_attempt_transition(AttemptStatus.RUNNING, AttemptStatus.FAILED)
                ensure_job_transition(JobStatus(job.status), JobStatus.FAILED)
                attempt.status = AttemptStatus.FAILED.value
                attempt.failure_code = outcome.error_code or "ATTEMPT_FAILED"
                attempt.failure_message = outcome.error_message
                job.status = JobStatus.FAILED.value
                job.finished_at = now
                job.retention_until = now + timedelta(days=self.metadata_retention_days)
                event_type = "job_failed"

            job.stage = AttemptStage.CLEANING_UP.value
            job.error_code = outcome.error_code or (
                "ATTEMPT_FAILED"
                if outcome.status == AttemptOutcomeStatus.FAILED
                else None
            )
            job.error_message = outcome.error_message
            await self._append_event(
                session,
                job_id=job.id,
                attempt_id=attempt.id,
                event_type=event_type,
                payload={
                    "status": job.status,
                    "error_code": outcome.error_code,
                    "error_message": outcome.error_message,
                    "pr_number": outcome.pr_number,
                    "pr_url": outcome.pr_url,
                    "head_branch": outcome.head_branch,
                    "head_sha": outcome.head_sha,
                    "verification_succeeded": outcome.verification_succeeded,
                },
            )

    async def recover_expired_leases(self, *, automatic_retry_limit: int = 1) -> int:
        recovered = 0
        async with self.database.sessions.begin() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": CLAIM_ADVISORY_LOCK},
            )
            now = await self._database_now(session)
            attempts = (
                await session.scalars(
                    select(AttemptRow)
                    .where(
                        AttemptRow.status == AttemptStatus.RUNNING.value,
                        AttemptRow.lease_expires_at <= now,
                    )
                    .order_by(AttemptRow.lease_expires_at, AttemptRow.id)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for attempt in attempts:
                job = await session.scalar(
                    select(JobRow).where(JobRow.id == attempt.job_id).with_for_update()
                )
                if job is None or attempt.attempt_no != job.current_attempt_no:
                    continue
                attempt.heartbeat_at = now
                attempt.lease_expires_at = None
                attempt.finished_at = now
                if job.status == JobStatus.CANCEL_REQUESTED.value:
                    attempt.status = AttemptStatus.CANCELLED.value
                    job.status = JobStatus.CANCELLED.value
                    job.finished_at = now
                    job.retention_until = now + timedelta(
                        days=self.metadata_retention_days
                    )
                    event_type = "job_cancelled"
                    payload = {"status": JobStatus.CANCELLED.value}
                else:
                    attempt.status = AttemptStatus.FAILED.value
                    attempt.failure_code = "WORKER_LEASE_EXPIRED"
                    attempt.failure_message = "Worker Lease expired"
                    if job.automatic_retry_count < automatic_retry_limit:
                        job.automatic_retry_count += 1
                        await self._append_event(
                            session,
                            job_id=job.id,
                            attempt_id=attempt.id,
                            event_type="attempt_lease_expired",
                            payload={
                                "status": AttemptStatus.FAILED.value,
                                "error_code": "WORKER_LEASE_EXPIRED",
                            },
                        )
                        await self._queue_new_attempt(
                            session, job, event_type="worker_lease_recovered"
                        )
                        recovered += 1
                        continue
                    else:
                        job.status = JobStatus.FAILED.value
                        job.error_code = "WORKER_LEASE_EXPIRED"
                        job.error_message = "Worker Lease expired"
                        job.finished_at = now
                        job.retention_until = now + timedelta(
                            days=self.metadata_retention_days
                        )
                        event_type = "job_failed"
                        payload = {
                            "status": JobStatus.FAILED.value,
                            "error_code": "WORKER_LEASE_EXPIRED",
                        }
                await self._append_event(
                    session,
                    job_id=job.id,
                    attempt_id=attempt.id,
                    event_type=event_type,
                    payload=payload,
                )
                recovered += 1
        return recovered
