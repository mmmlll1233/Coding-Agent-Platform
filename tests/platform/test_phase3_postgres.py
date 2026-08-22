from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, replace

import httpx
import pytest
import pytest_asyncio
from alembic import command
from sqlalchemy import func, select, text, update

from mewcode.platform.api import PlatformComponents, create_app
from mewcode.platform.cli import _alembic_config
from mewcode.platform.domain import (
    AttemptControls,
    AttemptLease,
    AttemptOutcome,
    AttemptOutcomeStatus,
    JobStatus,
    RepositoryTarget,
)
from mewcode.platform.execution import SensitiveValueRedactor
from mewcode.platform.persistence import (
    IdempotencyConflict,
    LeaseLost,
    PlatformRepository,
    PostgresJobEventSink,
    StateConflict,
    create_database,
)
from mewcode.platform.persistence.orm import AttemptRow, JobRow
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
    retried = await repository.retry_job(
        principal=principal, job_id=failed_job.id
    )
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
