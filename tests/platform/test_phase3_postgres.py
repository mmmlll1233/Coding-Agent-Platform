from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, replace
from datetime import timedelta
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from alembic import command
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from mewcode.platform.api import PlatformComponents, create_app
from mewcode.platform.cli import _alembic_config, _grant_runtime_roles
from mewcode.platform.domain import (
    AttemptControls,
    AttemptLease,
    AttemptOutcome,
    AttemptOutcomeStatus,
    AttemptStage,
    JobStatus,
    RepositoryTarget,
)
from mewcode.platform.execution import SensitiveValueRedactor
from mewcode.platform.persistence import (
    ArtifactMetadata,
    IdempotencyConflict,
    LeaseLost,
    NotFound,
    PlatformRepository,
    PostgresJobEventSink,
    StateConflict,
    create_database,
)
from mewcode.platform.persistence.orm import AttemptRow, JobRow, NotificationOutboxRow
from mewcode.platform.runtime import JobEvent
from mewcode.platform.settings import PlatformSettings
from mewcode.platform.workers import WorkerService

pytestmark = pytest.mark.platform_postgres


@pytest.fixture(scope="session")
def postgres_settings() -> PlatformSettings:
    url = os.environ.get("MEWCODE_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("PHASE3-CAPABILITY-POSTGRES: test database is not configured")
    settings = PlatformSettings.from_env(
        {
            "MEWCODE_PLATFORM_DATABASE_URL": url,
            "MEWCODE_PLATFORM_WORKER_STALE_SECONDS": "30",
        }
    )
    command.upgrade(_alembic_config(settings), "head")
    return settings


@pytest_asyncio.fixture
async def database(postgres_settings: PlatformSettings):
    database = create_database(postgres_settings)
    async with database.engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE notification_outbox, artifacts, job_events, job_inputs, "
                "attempts, jobs, api_keys, requesters, tenants, worker_nodes CASCADE"
            )
        )
    try:
        yield database
    finally:
        await database.aclose()


async def _principal(repository: PlatformRepository, suffix: str = "one"):
    token, principal = await repository.create_api_key(
        tenant_name=f"tenant-{suffix}", requester_name=f"requester-{suffix}"
    )
    return token, principal


def _target() -> RepositoryTarget:
    return RepositoryTarget(123, "company", "service", "main", "a" * 40)


def _work(title: str = "Fix it") -> dict:
    return {"kind": "bugfix", "title": title, "description": "broken"}


def _execution() -> dict:
    return {
        "setup_commands": [],
        "verification_commands": [
            {"name": "tests", "command": "pytest", "timeout_seconds": 600}
        ],
    }


async def _create(
    repository: PlatformRepository,
    principal,
    key: str,
    *,
    request_hash: str | None = None,
):
    return await repository.create_job(
        principal=principal,
        idempotency_key=key,
        request_hash=request_hash or key.ljust(64, "0")[:64],
        target=_target(),
        work_request=_work(key),
        execution_contract=_execution(),
    )


async def _migration_sql(settings: PlatformSettings, *statements: str) -> None:
    database = create_database(settings)
    try:
        async with database.engine.begin() as connection:
            for statement in statements:
                await connection.execute(text(statement))
    finally:
        await database.aclose()


def test_phase4_migration_fails_closed_on_incomplete_historical_success(
    postgres_settings: PlatformSettings,
) -> None:
    config = _alembic_config(postgres_settings)
    tenant_id = "00000000-0000-0000-0000-000000000401"
    requester_id = "00000000-0000-0000-0000-000000000402"
    job_id = "00000000-0000-0000-0000-000000000403"
    command.downgrade(config, "0001_phase3_control_plane")
    asyncio.run(
        _migration_sql(
            postgres_settings,
            f"INSERT INTO tenants (id, name) VALUES ('{tenant_id}', 'phase4-migration')",
            f"INSERT INTO requesters (id, tenant_id, name) "
            f"VALUES ('{requester_id}', '{tenant_id}', 'phase4-migration')",
            f"""
            INSERT INTO jobs (
              id, tenant_id, requester_id, idempotency_key, request_hash,
              status, installation_id, repo_owner, repo_name, base_ref, base_sha,
              work_request, execution_contract, pr_url, head_sha,
              verification_succeeded
            ) VALUES (
              '{job_id}', '{tenant_id}', '{requester_id}', 'phase4-migration',
              '{"4" * 64}', 'SUCCEEDED', 7, 'Acme', 'Repo', 'main',
              '{"a" * 40}', '{{}}'::jsonb, '{{}}'::jsonb,
              'https://github.com/Acme/Repo/pull/4', '{"b" * 40}', true
            )
            """,
        )
    )
    try:
        with pytest.raises(Exception, match="incomplete successful Delivery"):
            command.upgrade(config, "head")
    finally:
        asyncio.run(
            _migration_sql(
                postgres_settings,
                f"DELETE FROM jobs WHERE id = '{job_id}'",
                f"DELETE FROM requesters WHERE id = '{requester_id}'",
                f"DELETE FROM tenants WHERE id = '{tenant_id}'",
            )
        )
        command.upgrade(config, "head")


def test_phase6_migration_rejects_historical_outbox_rows(
    postgres_settings: PlatformSettings,
) -> None:
    config = _alembic_config(postgres_settings)
    tenant_id = "00000000-0000-0000-0000-000000000611"
    requester_id = "00000000-0000-0000-0000-000000000612"
    job_id = "00000000-0000-0000-0000-000000000613"
    outbox_id = "00000000-0000-0000-0000-000000000614"
    command.downgrade(config, "0003_phase5_artifacts")
    asyncio.run(
        _migration_sql(
            postgres_settings,
            f"INSERT INTO tenants (id, name) VALUES ('{tenant_id}', 'phase6-migration')",
            f"INSERT INTO requesters (id, tenant_id, name) "
            f"VALUES ('{requester_id}', '{tenant_id}', 'phase6-migration')",
            f"""
            INSERT INTO jobs (
              id, tenant_id, requester_id, idempotency_key, request_hash,
              status, installation_id, repo_owner, repo_name, base_ref, base_sha,
              work_request, execution_contract
            ) VALUES (
              '{job_id}', '{tenant_id}', '{requester_id}', 'phase6-migration',
              '{"6" * 64}', 'QUEUED', 7, 'Acme', 'Repo', 'main',
              '{"a" * 40}', '{{}}'::jsonb, '{{}}'::jsonb
            )
            """,
            f"""
            INSERT INTO notification_outbox (
              id, job_id, event_type, destination, payload
            ) VALUES (
              '{outbox_id}', '{job_id}', 'FAILED', 'feishu:platform', '{{}}'::jsonb
            )
            """,
        )
    )
    try:
        with pytest.raises(Exception, match="requires an empty notification_outbox"):
            command.upgrade(config, "head")
    finally:
        asyncio.run(
            _migration_sql(
                postgres_settings,
                f"DELETE FROM notification_outbox WHERE id = '{outbox_id}'",
                f"DELETE FROM jobs WHERE id = '{job_id}'",
                f"DELETE FROM requesters WHERE id = '{requester_id}'",
                f"DELETE FROM tenants WHERE id = '{tenant_id}'",
            )
        )
        command.upgrade(config, "head")


@pytest.mark.asyncio
async def test_concurrent_idempotent_submission_creates_one_job(database) -> None:
    repository = PlatformRepository(database)
    _, principal = await _principal(repository)
    results = await asyncio.gather(
        *[
            _create(repository, principal, "same-key", request_hash="b" * 64)
            for _ in range(8)
        ]
    )
    assert len({job.id for job, _ in results}) == 1
    assert sum(1 for _, replayed in results if not replayed) == 1
    async with database.sessions() as session:
        assert await session.scalar(select(func.count(JobRow.id))) == 1
        assert await session.scalar(select(func.count(AttemptRow.id))) == 1
    with pytest.raises(IdempotencyConflict):
        await repository.lookup_idempotent_job(
            principal=principal,
            idempotency_key="same-key",
            request_hash="c" * 64,
        )


@pytest.mark.asyncio
async def test_database_capacity_and_fencing_are_enforced(database) -> None:
    repository = PlatformRepository(database)
    _, principal = await _principal(repository)
    for key in ("first", "second", "third"):
        await _create(repository, principal, key)

    first, second = await asyncio.gather(
        repository.claim_attempt(
            worker_id="worker-a", lease_seconds=60, max_concurrent_jobs=1
        ),
        repository.claim_attempt(
            worker_id="worker-b", lease_seconds=60, max_concurrent_jobs=1
        ),
    )
    claimed = first or second
    assert claimed is not None
    assert (first is None) != (second is None)
    assert (
        await repository.claim_attempt(
            worker_id="worker-c", lease_seconds=60, max_concurrent_jobs=1
        )
        is None
    )

    lease = claimed.lease
    assert await repository.live_attempt_ids() == frozenset({lease.attempt_id})
    await repository.heartbeat_lease(
        attempt_id=lease.attempt_id,
        worker_id=lease.worker_id,
        fencing_token=lease.fencing_token,
        lease_seconds=60,
    )
    with pytest.raises(LeaseLost):
        await repository.heartbeat_lease(
            attempt_id=lease.attempt_id,
            worker_id="stale-worker",
            fencing_token=lease.fencing_token,
            lease_seconds=60,
        )


@pytest.mark.asyncio
async def test_worker_registration_rejects_global_capacity_drift_and_draining(
    database,
) -> None:
    repository = PlatformRepository(database)
    await repository.register_worker(
        "worker-capacity-a",
        metadata={"global_capacity": 1, "local_slots": 1, "draining": False},
        stale_seconds=30,
    )
    assert await repository.has_fresh_worker(30)
    with pytest.raises(StateConflict) as same_id:
        await repository.register_worker(
            "worker-capacity-a",
            metadata={"global_capacity": 5, "local_slots": 1, "draining": False},
            stale_seconds=30,
        )
    assert same_id.value.code == "CAPACITY_CONFIGURATION_MISMATCH"
    with pytest.raises(StateConflict) as caught:
        await repository.register_worker(
            "worker-capacity-b",
            metadata={"global_capacity": 5, "local_slots": 1, "draining": False},
            stale_seconds=30,
        )
    assert caught.value.code == "CAPACITY_CONFIGURATION_MISMATCH"
    assert await repository.set_worker_draining("worker-capacity-a", True)
    assert not await repository.has_fresh_worker(30)


@pytest.mark.asyncio
async def test_global_capacity_allows_multiple_workers_without_oversubscription(
    database,
) -> None:
    repository = PlatformRepository(database)
    _, principal = await _principal(repository, "phase7-capacity")
    for key in ("capacity-one", "capacity-two", "capacity-three"):
        await _create(repository, principal, key)
    first = await repository.claim_attempt(
        worker_id="worker-a", lease_seconds=60, max_concurrent_jobs=2
    )
    second = await repository.claim_attempt(
        worker_id="worker-b", lease_seconds=60, max_concurrent_jobs=2
    )
    blocked = await repository.claim_attempt(
        worker_id="worker-c", lease_seconds=60, max_concurrent_jobs=2
    )
    assert first is not None and second is not None
    assert blocked is None
    stats = await repository.job_queue_stats()
    assert (stats.queued, stats.running) == (1, 2)


@pytest.mark.asyncio
async def test_expired_lease_retries_once_and_rejects_stale_events(database) -> None:
    repository = PlatformRepository(database)
    _, principal = await _principal(repository)
    job, _ = await _create(repository, principal, "lease")
    claimed = await repository.claim_attempt(
        worker_id="worker-a", lease_seconds=60, max_concurrent_jobs=1
    )
    assert claimed is not None
    lease = claimed.lease
    sink = PostgresJobEventSink(
        repository,
        job_id=job.id,
        attempt_id=lease.attempt_id,
        worker_id=lease.worker_id,
        fencing_token=lease.fencing_token,
        redactor=SensitiveValueRedactor(("phase3-secret-canary",)),
    )
    await sink.emit(
        JobEvent(
            job_id=str(job.id),
            attempt_id=str(lease.attempt_id),
            attempt_sequence=1,
            timestamp=lease.lease_expires_at,
            event_type="text_delta",
            payload={"text": "hello phase3-secret-canary"},
        )
    )
    async with database.sessions.begin() as session:
        await session.execute(
            update(AttemptRow)
            .where(AttemptRow.id == lease.attempt_id)
            .values(lease_expires_at=func.now() - text("interval '1 second'"))
        )
    assert await repository.recover_expired_leases() == 1
    assert await repository.live_attempt_ids() == frozenset()
    with pytest.raises(LeaseLost):
        await sink.emit(
            JobEvent(
                job_id=str(job.id),
                attempt_id=str(lease.attempt_id),
                attempt_sequence=2,
                timestamp=lease.lease_expires_at,
                event_type="text_delta",
            )
        )
    current = await repository.get_job(principal=principal, job_id=job.id)
    assert current.status == JobStatus.QUEUED.value
    assert current.current_attempt_no == 2
    assert current.automatic_retry_count == 1
    events = await repository.list_events(principal=principal, job_id=job.id, limit=100)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert "phase3-secret-canary" not in str([event.payload for event in events])
    assert "[REDACTED]" in str([event.payload for event in events])

    retried = await repository.claim_attempt(
        worker_id="worker-b", lease_seconds=60, max_concurrent_jobs=1
    )
    assert retried is not None
    async with database.sessions.begin() as session:
        await session.execute(
            update(AttemptRow)
            .where(AttemptRow.id == retried.lease.attempt_id)
            .values(lease_expires_at=func.now() - text("interval '1 second'"))
        )
    assert await repository.recover_expired_leases() == 1
    failed = await repository.get_job(principal=principal, job_id=job.id)
    assert failed.status == JobStatus.FAILED.value
    assert failed.error_code == "WORKER_LEASE_EXPIRED"


@pytest.mark.asyncio
async def test_input_retry_and_cancel_create_only_expected_attempts(database) -> None:
    repository = PlatformRepository(database)
    _, principal = await _principal(repository)
    needs_job, _ = await _create(repository, principal, "needs")
    needs = await repository.claim_attempt(
        worker_id="worker-a", lease_seconds=60, max_concurrent_jobs=1
    )
    assert needs is not None
    await repository.finish_attempt(
        attempt_id=needs.lease.attempt_id,
        worker_id=needs.lease.worker_id,
        fencing_token=needs.lease.fencing_token,
        outcome=AttemptOutcome(status=AttemptOutcomeStatus.NEEDS_INPUT),
    )
    await repository.add_input(
        principal=principal, job_id=needs_job.id, content="Use the safe default"
    )
    with pytest.raises(StateConflict):
        await repository.add_input(
            principal=principal, job_id=needs_job.id, content="duplicate"
        )

    await repository.cancel_job(principal=principal, job_id=needs_job.id)
    cancelled = await repository.cancel_job(principal=principal, job_id=needs_job.id)
    assert cancelled.status == JobStatus.CANCELLED.value
    async with database.sessions() as session:
        attempts = await session.scalar(
            select(func.count(AttemptRow.id)).where(AttemptRow.job_id == needs_job.id)
        )
    assert attempts == 2

    failed_job, _ = await _create(repository, principal, "manual-retry")
    failed_attempt = await repository.claim_attempt(
        worker_id="worker-b", lease_seconds=60, max_concurrent_jobs=1
    )
    assert failed_attempt is not None
    await repository.finish_attempt(
        attempt_id=failed_attempt.lease.attempt_id,
        worker_id=failed_attempt.lease.worker_id,
        fencing_token=failed_attempt.lease.fencing_token,
        outcome=AttemptOutcome(
            status=AttemptOutcomeStatus.FAILED,
            error_code="TEST_FAILURE",
        ),
    )
    retried = await repository.retry_job(principal=principal, job_id=failed_job.id)
    assert retried.status == JobStatus.QUEUED.value
    assert retried.current_attempt_no == 2
    with pytest.raises(StateConflict):
        await repository.retry_job(principal=principal, job_id=failed_job.id)


@pytest.mark.asyncio
async def test_completed_processor_cannot_create_unverified_success(database) -> None:
    repository = PlatformRepository(database)
    _, principal = await _principal(repository, "delivery")
    job, _ = await _create(repository, principal, "delivery")
    claimed = await repository.claim_attempt(
        worker_id="worker-delivery", lease_seconds=60, max_concurrent_jobs=1
    )
    assert claimed is not None
    await repository.finish_attempt(
        attempt_id=claimed.lease.attempt_id,
        worker_id=claimed.lease.worker_id,
        fencing_token=claimed.lease.fencing_token,
        outcome=AttemptOutcome(status=AttemptOutcomeStatus.COMPLETED),
    )
    failed = await repository.get_job(principal=principal, job_id=job.id)
    assert failed.status == JobStatus.FAILED.value
    assert failed.error_code == "INCOMPLETE_DELIVERY"
    assert failed.pr_url is None


@pytest.mark.asyncio
async def test_phase4_delivery_evidence_is_persisted_atomically(database) -> None:
    repository = PlatformRepository(database)
    _, principal = await _principal(repository, "phase4-delivery")
    job, _ = await _create(repository, principal, "phase4-delivery")
    claimed = await repository.claim_attempt(
        worker_id="worker-phase4", lease_seconds=60, max_concurrent_jobs=1
    )
    assert claimed is not None
    branch = f"mewcode/{job.id}"
    await repository.finish_attempt(
        attempt_id=claimed.lease.attempt_id,
        worker_id=claimed.lease.worker_id,
        fencing_token=claimed.lease.fencing_token,
        outcome=AttemptOutcome(
            status=AttemptOutcomeStatus.COMPLETED,
            pr_number=17,
            pr_url="https://github.com/example/repository/pull/17",
            head_branch=branch,
            head_sha="e" * 40,
            verification_succeeded=True,
        ),
    )
    completed = await repository.get_job(principal=principal, job_id=job.id)
    assert completed.status == JobStatus.SUCCEEDED.value
    assert completed.pr_number == 17
    assert completed.head_branch == branch
    events = await repository.list_events(
        principal=principal, job_id=job.id, after=0, limit=100
    )
    succeeded = next(event for event in events if event.event_type == "job_succeeded")
    assert succeeded.payload["pr_number"] == 17
    assert succeeded.payload["head_branch"] == branch
    assert succeeded.payload["verification_succeeded"] is True
    invalid_delivery_updates = (
        {"pr_url": None},
        {"pr_number": 18},
        {"pr_url": "https://example.com/example/repository/pull/17"},
        {"head_branch": "mewcode/not-a-job-id"},
        {"head_sha": "not-a-sha"},
        {"verification_succeeded": False},
    )
    for invalid in invalid_delivery_updates:
        with pytest.raises(IntegrityError):
            async with database.sessions.begin() as session:
                await session.execute(
                    update(JobRow).where(JobRow.id == job.id).values(**invalid)
                )


@pytest.mark.asyncio
async def test_phase5_artifacts_are_fenced_and_requester_isolated(database) -> None:
    repository = PlatformRepository(database)
    _, principal = await _principal(repository, "artifact-owner")
    _, other = await _principal(repository, "artifact-other")
    job, _ = await _create(repository, principal, "artifact-fencing")
    claimed = await repository.claim_attempt(
        worker_id="artifact-worker", lease_seconds=60, max_concurrent_jobs=1
    )
    assert claimed is not None
    lease = claimed.lease
    now = lease.lease_expires_at
    artifact = ArtifactMetadata(
        id=UUID("00000000-0000-0000-0000-0000000005a1"),
        job_id=job.id,
        attempt_id=lease.attempt_id,
        kind="verification_report",
        storage_key=f"{job.id}/{lease.attempt_id}/00000000-0000-0000-0000-0000000005a1",
        sha256="a" * 64,
        size_bytes=2,
        content_type="application/json",
        expires_at=now + timedelta(days=7),
    )
    stored = await repository.add_artifact_fenced(
        artifact=artifact,
        worker_id=lease.worker_id,
        fencing_token=lease.fencing_token,
    )
    assert stored.created_at is not None
    assert (await repository.list_artifacts(principal=principal, job_id=job.id))[
        0
    ].id == artifact.id
    with pytest.raises(NotFound):
        await repository.list_artifacts(principal=other, job_id=job.id)
    with pytest.raises(LeaseLost):
        await repository.add_artifact_fenced(
            artifact=replace(artifact, id=UUID(int=0x5A2)),
            worker_id="stale-worker",
            fencing_token=lease.fencing_token,
        )


@pytest.mark.asyncio
async def test_phase5_publishing_stage_rejects_cancellation(database) -> None:
    repository = PlatformRepository(database)
    _, principal = await _principal(repository, "publishing")
    job, _ = await _create(repository, principal, "publishing-cancel")
    claimed = await repository.claim_attempt(
        worker_id="publishing-worker", lease_seconds=60, max_concurrent_jobs=1
    )
    assert claimed is not None
    await repository.report_stage(
        attempt_id=claimed.lease.attempt_id,
        worker_id=claimed.lease.worker_id,
        fencing_token=claimed.lease.fencing_token,
        stage=AttemptStage.PUBLISHING,
    )
    with pytest.raises(StateConflict) as caught:
        await repository.cancel_job(principal=principal, job_id=job.id)
    assert caught.value.code == "JOB_NOT_CANCELLABLE"


@pytest.mark.asyncio
async def test_phase5_retention_deletes_only_expired_terminal_jobs(database) -> None:
    repository = PlatformRepository(database)
    _, principal = await _principal(repository, "retention")
    failed_job, _ = await _create(repository, principal, "expired-terminal")
    failed_attempt = await repository.claim_attempt(
        worker_id="retention-worker-a", lease_seconds=60, max_concurrent_jobs=1
    )
    assert failed_attempt is not None
    await repository.finish_attempt(
        attempt_id=failed_attempt.lease.attempt_id,
        worker_id=failed_attempt.lease.worker_id,
        fencing_token=failed_attempt.lease.fencing_token,
        outcome=AttemptOutcome(
            status=AttemptOutcomeStatus.FAILED, error_code="TEST_FAILURE"
        ),
    )
    needs_job, _ = await _create(repository, principal, "needs-input-retention")
    needs_attempt = await repository.claim_attempt(
        worker_id="retention-worker-b", lease_seconds=60, max_concurrent_jobs=1
    )
    assert needs_attempt is not None
    await repository.finish_attempt(
        attempt_id=needs_attempt.lease.attempt_id,
        worker_id=needs_attempt.lease.worker_id,
        fencing_token=needs_attempt.lease.fencing_token,
        outcome=AttemptOutcome(status=AttemptOutcomeStatus.NEEDS_INPUT),
    )
    async with database.sessions.begin() as session:
        await session.execute(
            update(JobRow)
            .where(JobRow.id == failed_job.id)
            .values(retention_until=func.now() - text("interval '1 second'"))
        )
    assert await repository.delete_expired_terminal_jobs() == 1
    with pytest.raises(NotFound):
        await repository.get_job(principal=principal, job_id=failed_job.id)
    retained = await repository.get_job(principal=principal, job_id=needs_job.id)
    assert retained.status == JobStatus.NEEDS_INPUT.value
    assert retained.retention_until is None


@pytest.mark.asyncio
async def test_phase6_job_events_enqueue_notifications_atomically_and_by_sequence(
    database,
) -> None:
    redactor = SensitiveValueRedactor(("phase6-secret-canary",))
    repository = PlatformRepository(
        database,
        notifications_enabled=True,
        notification_destination="feishu:platform",
        redactor=redactor,
    )
    _, principal = await _principal(repository, "phase6-events")
    job, _ = await _create(repository, principal, "phase6-events")
    replay = await repository.lookup_idempotent_job(
        principal=principal,
        idempotency_key="phase6-events",
        request_hash="phase6-events".ljust(64, "0")[:64],
    )
    assert replay is not None and replay.id == job.id

    first = await repository.claim_attempt(
        worker_id="phase6-worker-a", lease_seconds=60, max_concurrent_jobs=1
    )
    assert first is not None
    await repository.finish_attempt(
        attempt_id=first.lease.attempt_id,
        worker_id=first.lease.worker_id,
        fencing_token=first.lease.fencing_token,
        outcome=AttemptOutcome(status=AttemptOutcomeStatus.NEEDS_INPUT),
    )
    await repository.add_input(
        principal=principal, job_id=job.id, content="safe additional input"
    )
    second = await repository.claim_attempt(
        worker_id="phase6-worker-b", lease_seconds=60, max_concurrent_jobs=1
    )
    assert second is not None
    await repository.finish_attempt(
        attempt_id=second.lease.attempt_id,
        worker_id=second.lease.worker_id,
        fencing_token=second.lease.fencing_token,
        outcome=AttemptOutcome(
            status=AttemptOutcomeStatus.FAILED,
            error_code="TEST_FAILURE",
            error_message="safe phase6-secret-canary",
        ),
    )
    await repository.retry_job(principal=principal, job_id=job.id)
    third = await repository.claim_attempt(
        worker_id="phase6-worker-c", lease_seconds=60, max_concurrent_jobs=1
    )
    assert third is not None
    await repository.finish_attempt(
        attempt_id=third.lease.attempt_id,
        worker_id=third.lease.worker_id,
        fencing_token=third.lease.fencing_token,
        outcome=AttemptOutcome(
            status=AttemptOutcomeStatus.FAILED,
            error_code="TEST_FAILURE_AGAIN",
        ),
    )

    async with database.sessions() as session:
        rows = (
            await session.scalars(
                select(NotificationOutboxRow)
                .where(NotificationOutboxRow.job_id == job.id)
                .order_by(NotificationOutboxRow.source_event_sequence)
            )
        ).all()
    assert [row.event_type for row in rows] == [
        "JOB_ACCEPTED",
        "NEEDS_INPUT",
        "FAILED",
        "FAILED",
    ]
    assert len({row.source_event_sequence for row in rows}) == 4
    assert all(row.payload["notification_id"] == str(row.id) for row in rows)
    assert "phase6-secret-canary" not in str([row.payload for row in rows])
    assert "[REDACTED]" in str([row.payload for row in rows])

    cancel_job, _ = await _create(repository, principal, "phase6-cancel")
    cancel_attempt = await repository.claim_attempt(
        worker_id="phase6-worker-cancel", lease_seconds=60, max_concurrent_jobs=1
    )
    assert cancel_attempt is not None
    before_cancel = await repository.notification_outbox_stats()
    requested = await repository.cancel_job(
        principal=principal, job_id=cancel_job.id
    )
    assert requested.status == JobStatus.CANCEL_REQUESTED.value
    after_request = await repository.notification_outbox_stats()
    assert (
        after_request.pending,
        after_request.in_flight,
        after_request.delivered,
    ) == (
        before_cancel.pending,
        before_cancel.in_flight,
        before_cancel.delivered,
    )
    await repository.finish_attempt(
        attempt_id=cancel_attempt.lease.attempt_id,
        worker_id=cancel_attempt.lease.worker_id,
        fencing_token=cancel_attempt.lease.fencing_token,
        outcome=AttemptOutcome(status=AttemptOutcomeStatus.CANCELLED),
    )

    success_job, _ = await _create(repository, principal, "phase6-success")
    success_attempt = await repository.claim_attempt(
        worker_id="phase6-worker-success", lease_seconds=60, max_concurrent_jobs=1
    )
    assert success_attempt is not None
    await repository.finish_attempt(
        attempt_id=success_attempt.lease.attempt_id,
        worker_id=success_attempt.lease.worker_id,
        fencing_token=success_attempt.lease.fencing_token,
        outcome=AttemptOutcome(
            status=AttemptOutcomeStatus.COMPLETED,
            pr_number=6,
            pr_url="https://github.com/company/service/pull/6",
            head_branch=f"mewcode/{success_job.id}",
            head_sha="e" * 40,
            verification_succeeded=True,
        ),
    )
    async with database.sessions() as session:
        delivered_types = set(
            await session.scalars(select(NotificationOutboxRow.event_type))
        )
    assert delivered_types == {
        "JOB_ACCEPTED",
        "NEEDS_INPUT",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
    }


@pytest.mark.asyncio
async def test_phase6_outbox_insert_failure_rolls_back_job_and_event(database) -> None:
    repository = PlatformRepository(
        database,
        notifications_enabled=True,
        notification_destination="feishu:" + "x" * 300,
    )
    _, principal = await _principal(repository, "phase6-atomic-rollback")
    with pytest.raises(DBAPIError):
        await _create(repository, principal, "phase6-atomic-rollback")
    async with database.sessions() as session:
        assert await session.scalar(select(func.count(JobRow.id))) == 0
        assert await session.scalar(select(func.count(NotificationOutboxRow.id))) == 0


@pytest.mark.asyncio
async def test_phase6_notification_claim_fencing_recovery_and_retention(database) -> None:
    repository = PlatformRepository(database, notifications_enabled=True)
    _, principal = await _principal(repository, "phase6-fencing")
    job, _ = await _create(repository, principal, "phase6-fencing")
    first, competing = await asyncio.gather(
        repository.claim_notification(notifier_id="notifier-a", lease_seconds=60),
        repository.claim_notification(notifier_id="notifier-b", lease_seconds=60),
    )
    claimed = first or competing
    assert claimed is not None
    assert (first is None) != (competing is None)
    assert not await repository.mark_notification_delivered(
        notification_id=claimed.id,
        notifier_id=claimed.notifier_id,
        fencing_token=UUID(int=6),
    )

    async with database.sessions.begin() as session:
        await session.execute(
            update(NotificationOutboxRow)
            .where(NotificationOutboxRow.id == claimed.id)
            .values(lease_expires_at=func.now() - text("interval '1 second'"))
        )
    assert not await repository.mark_notification_delivered(
        notification_id=claimed.id,
        notifier_id=claimed.notifier_id,
        fencing_token=claimed.fencing_token,
    )
    recovered = await repository.claim_notification(
        notifier_id="notifier-recovery", lease_seconds=60
    )
    assert recovered is not None and recovered.id == claimed.id
    assert recovered.fencing_token != claimed.fencing_token
    assert not await repository.mark_notification_delivered(
        notification_id=claimed.id,
        notifier_id=claimed.notifier_id,
        fencing_token=claimed.fencing_token,
    )

    async with database.sessions.begin() as session:
        await session.execute(
            update(JobRow)
            .where(JobRow.id == job.id)
            .values(
                status=JobStatus.FAILED.value,
                retention_until=func.now() - text("interval '1 second'"),
            )
        )
    assert await repository.delete_expired_terminal_jobs() == 0
    assert await repository.mark_notification_delivered(
        notification_id=recovered.id,
        notifier_id=recovered.notifier_id,
        fencing_token=recovered.fencing_token,
    )
    assert (
        await repository.claim_notification(
            notifier_id="notifier-late", lease_seconds=60
        )
        is None
    )
    assert await repository.delete_expired_terminal_jobs() == 1


@dataclass
class _Resolver:
    async def resolve(self, **kwargs) -> RepositoryTarget:
        return RepositoryTarget(
            kwargs["installation_id"],
            kwargs["owner"],
            kwargs["name"],
            kwargs["base_ref"],
            "d" * 40,
        )


def _api_body(title: str = "Fix it") -> dict:
    return {
        "repository": {
            "installation_id": 123,
            "owner": "company",
            "name": "service",
            "base_ref": "main",
        },
        "work": {
            "kind": "bugfix",
            "title": title,
            "description": "broken",
        },
        "execution": {
            "verification_commands": [_execution()["verification_commands"][0]]
        },
        "attachment_ids": [],
    }


@pytest.mark.asyncio
async def test_control_api_auth_idempotency_tenant_boundary_and_readiness(
    database, postgres_settings
) -> None:
    repository = PlatformRepository(database)
    token, principal = await _principal(repository, "api")
    other_token, _ = await _principal(repository, "other")
    app = create_app(
        components=PlatformComponents(
            settings=postgres_settings,
            database=database,
            repository=repository,
            resolver=_Resolver(),
        )
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "api-key"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/health/live")).status_code == 200
        assert (await client.get("/health/ready")).status_code == 503
        unauthenticated = await client.post("/v1/jobs", json=_api_body())
        assert unauthenticated.status_code == 401
        created = await client.post("/v1/jobs", json=_api_body(), headers=headers)
        assert created.status_code == 202, created.text
        job_id = created.json()["id"]
        replay = await client.post("/v1/jobs", json=_api_body(), headers=headers)
        assert replay.status_code == 202
        assert replay.headers["Idempotency-Replayed"] == "true"
        conflict = await client.post(
            "/v1/jobs", json=_api_body("Different"), headers=headers
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        hidden = await client.get(
            f"/v1/jobs/{job_id}",
            headers={"Authorization": f"Bearer {other_token}"},
        )
        assert hidden.status_code == 404
        events = await client.get(
            f"/v1/jobs/{job_id}/events?after=0&limit=1",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert events.status_code == 200
        assert events.json()["has_more"] is True
        assert events.json()["items"][0]["sequence"] == 1
        oversized_timeout = _api_body()
        oversized_timeout["execution"]["verification_commands"][0][
            "timeout_seconds"
        ] = 601
        invalid = await client.post(
            "/v1/jobs",
            json=oversized_timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": "invalid",
            },
        )
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"

        await repository.register_worker("ready-worker")
        assert (await client.get("/health/ready")).status_code == 200
        assert await repository.revoke_api_key(principal.key_id)
        revoked = await client.get(
            f"/v1/jobs/{job_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert revoked.status_code == 401


@pytest.mark.asyncio
async def test_api_fails_closed_without_repository_resolver(
    database, postgres_settings
) -> None:
    repository = PlatformRepository(database)
    token, _ = await _principal(repository, "closed")
    app = create_app(
        components=PlatformComponents(
            settings=postgres_settings,
            database=database,
            repository=repository,
            resolver=None,
        )
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/jobs",
            json=_api_body(),
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": "closed",
            },
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "REPOSITORY_RESOLVER_UNAVAILABLE"
    async with database.sessions() as session:
        assert await session.scalar(select(func.count(JobRow.id))) == 0


@pytest.mark.asyncio
async def test_phase6_readiness_requires_fresh_worker_and_notifier(
    database, postgres_settings
) -> None:
    settings = replace(postgres_settings, notifications_enabled=True)
    repository = PlatformRepository(database, notifications_enabled=True)
    app = create_app(
        components=PlatformComponents(
            settings=settings,
            database=database,
            repository=repository,
        )
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await repository.register_worker("phase6-ready-worker")
        missing = await client.get("/health/ready")
        assert missing.status_code == 503
        assert missing.json()["checks"]["worker"] is True
        assert missing.json()["checks"]["notifier"] is False
        await repository.register_service(
            service_id="phase6-ready-notifier",
            service_type="notifier",
        )
        ready = await client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json()["checks"]["notifier"] is True


@pytest.mark.asyncio
async def test_phase6_notifier_database_role_cannot_modify_business_tables(
    database, postgres_settings
) -> None:
    roles = ("mewcode_api", "mewcode_worker", "mewcode_notifier")
    created: list[str] = []
    async with database.engine.begin() as connection:
        for role in roles:
            exists = await connection.scalar(
                text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}
            )
            if exists is None:
                await connection.execute(text(f"CREATE ROLE {role} NOLOGIN"))
                created.append(role)
    try:
        assert await _grant_runtime_roles(postgres_settings) == 0
        async with database.engine.connect() as connection:
            for table in ("jobs", "attempts", "artifacts"):
                allowed = await connection.scalar(
                    text(
                        "SELECT has_table_privilege("
                        "'mewcode_notifier', :table, 'UPDATE')"
                    ),
                    {"table": table},
                )
                assert allowed is False
            assert await connection.scalar(
                text(
                    "SELECT has_table_privilege("
                    "'mewcode_notifier', 'notification_outbox', 'UPDATE')"
                )
            ) is True
            for column in ("id", "job_id", "status"):
                assert await connection.scalar(
                    text(
                        "SELECT has_column_privilege("
                        "'mewcode_worker', 'notification_outbox', :column, 'SELECT')"
                    ),
                    {"column": column},
                ) is True
            assert await connection.scalar(
                text(
                    "SELECT has_column_privilege("
                    "'mewcode_worker', 'notification_outbox', 'payload', 'SELECT')"
                )
            ) is False
    finally:
        async with database.engine.begin() as connection:
            for role in reversed(created):
                await connection.execute(text(f"DROP OWNED BY {role}"))
                await connection.execute(text(f"DROP ROLE {role}"))


class _CancellingProcessor:
    def __init__(self, started: asyncio.Event, cancelled: asyncio.Event) -> None:
        self.started = started
        self.cancelled = cancelled

    async def process(
        self, lease: AttemptLease, controls: AttemptControls
    ) -> AttemptOutcome:
        self.started.set()
        await self.cancelled.wait()
        return AttemptOutcome(status=AttemptOutcomeStatus.CANCELLED)

    async def cancel(self) -> None:
        self.cancelled.set()


class _CancellingFactory:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    def create(self, lease: AttemptLease) -> _CancellingProcessor:
        return _CancellingProcessor(self.started, self.cancelled)


class _BarrierProcessor:
    def __init__(self, factory, lease: AttemptLease) -> None:
        self.factory = factory
        self.lease = lease

    async def process(
        self, lease: AttemptLease, controls: AttemptControls
    ) -> AttemptOutcome:
        self.factory.started.append(lease.attempt_id)
        if len(self.factory.started) >= self.factory.expected_concurrency:
            self.factory.capacity_reached.set()
        await self.factory.release.wait()
        pr_number = (lease.job_id.int % 10_000) + 1
        return AttemptOutcome(
            status=AttemptOutcomeStatus.COMPLETED,
            pr_number=pr_number,
            pr_url=f"https://github.com/company/service/pull/{pr_number}",
            head_branch=f"mewcode/{lease.job_id}",
            head_sha="f" * 40,
            verification_succeeded=True,
        )

    async def cancel(self) -> None:
        self.factory.release.set()


class _BarrierFactory:
    def __init__(self, expected_concurrency: int) -> None:
        self.expected_concurrency = expected_concurrency
        self.started: list[UUID] = []
        self.capacity_reached = asyncio.Event()
        self.release = asyncio.Event()

    def create(self, lease: AttemptLease) -> _BarrierProcessor:
        return _BarrierProcessor(self, lease)


class _OrphanCleanupFactory:
    def __init__(self) -> None:
        self.cleaned = asyncio.Event()

    def create(self, lease: AttemptLease):
        raise AssertionError("No Job should be claimed in this cleanup test")

    async def cleanup_orphaned(self) -> tuple[int, int, int]:
        self.cleaned.set()
        return 2, 1, 3


@pytest.mark.asyncio
async def test_worker_recovery_loop_invokes_orphan_resource_cleanup(
    database, postgres_settings
) -> None:
    repository = PlatformRepository(database)
    factory = _OrphanCleanupFactory()
    settings = replace(
        postgres_settings,
        worker_id="worker-orphan-cleanup",
        heartbeat_seconds=1,
        lease_seconds=5,
        recovery_seconds=1,
    )
    service = WorkerService(settings, repository, factory)
    task = asyncio.create_task(service.run_forever())
    try:
        await asyncio.wait_for(factory.cleaned.wait(), timeout=5)
    finally:
        await service.stop()
        await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_worker_observes_cancel_and_finishes_cleanup(
    database, postgres_settings
) -> None:
    repository = PlatformRepository(database)
    _, principal = await _principal(repository, "worker")
    job, _ = await _create(repository, principal, "worker-cancel")
    factory = _CancellingFactory()
    settings = replace(
        postgres_settings,
        worker_id="worker-service",
        heartbeat_seconds=1,
        lease_seconds=5,
        recovery_seconds=1,
    )
    service = WorkerService(settings, repository, factory)
    task = asyncio.create_task(service.run_forever())
    try:
        await asyncio.wait_for(factory.started.wait(), timeout=5)
        running = await repository.get_job(principal=principal, job_id=job.id)
        assert running.status == JobStatus.RUNNING.value
        await repository.cancel_job(principal=principal, job_id=job.id)
        for _ in range(40):
            current = await repository.get_job(principal=principal, job_id=job.id)
            if current.status == JobStatus.CANCELLED.value:
                break
            await asyncio.sleep(0.1)
        assert current.status == JobStatus.CANCELLED.value
        assert factory.cancelled.is_set()
    finally:
        await service.stop()
        await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_worker_shutdown_drains_then_forces_lease_recovery(
    database, postgres_settings
) -> None:
    repository = PlatformRepository(database)
    _, principal = await _principal(repository, "worker-drain")
    job, _ = await _create(repository, principal, "worker-drain")
    factory = _CancellingFactory()
    settings = replace(
        postgres_settings,
        worker_id="worker-drain-service",
        heartbeat_seconds=1,
        lease_seconds=5,
        recovery_seconds=1,
        worker_shutdown_grace_seconds=1,
    )
    service = WorkerService(settings, repository, factory)
    task = asyncio.create_task(service.run_forever())
    await asyncio.wait_for(factory.started.wait(), timeout=5)
    await asyncio.wait_for(service.stop(), timeout=4)
    await asyncio.wait_for(task, timeout=2)
    assert factory.cancelled.is_set()
    assert not await repository.has_fresh_worker(30)
    current = await repository.get_job(principal=principal, job_id=job.id)
    assert current.status == JobStatus.RUNNING.value


@pytest.mark.asyncio
async def test_server_gate_holds_five_slots_and_queues_the_sixth(
    database, postgres_settings
) -> None:
    repository = PlatformRepository(database)
    _, principal = await _principal(repository, "phase7-server-gate")
    jobs = [
        (await _create(repository, principal, f"phase7-server-{index}"))[0]
        for index in range(10)
    ]
    factory = _BarrierFactory(expected_concurrency=5)
    settings = replace(
        postgres_settings,
        worker_id="phase7-five-slot-worker",
        max_concurrent_jobs=5,
        worker_max_concurrent_attempts=5,
        heartbeat_seconds=1,
        lease_seconds=5,
        recovery_seconds=1,
    )
    service = WorkerService(settings, repository, factory)
    task = asyncio.create_task(service.run_forever())
    try:
        await asyncio.wait_for(factory.capacity_reached.wait(), timeout=5)
        stats = await repository.job_queue_stats()
        assert stats.running == 5
        assert stats.queued == 5
        await asyncio.sleep(0.6)
        assert len(factory.started) == 5
        factory.release.set()
        for _ in range(100):
            states = [
                (await repository.get_job(principal=principal, job_id=job.id)).status
                for job in jobs
            ]
            if states == [JobStatus.SUCCEEDED.value] * 10:
                break
            await asyncio.sleep(0.1)
        assert states == [JobStatus.SUCCEEDED.value] * 10
        assert len(factory.started) == 10
    finally:
        factory.release.set()
        await service.stop()
        await asyncio.wait_for(task, timeout=5)
