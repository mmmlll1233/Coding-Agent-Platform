from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from mewcode.platform.api.schemas import RepositoryRequest
from mewcode.platform.domain import (
    AttemptStatus,
    InvalidTransition,
    JobStatus,
    RepositoryTarget,
    ensure_attempt_transition,
    ensure_job_transition,
)
from mewcode.platform.persistence import PostgresJobEventSink
from mewcode.platform.runtime import JobEvent
from mewcode.platform.settings import PlatformSettings, PlatformSettingsError
from mewcode.platform.workers import WorkerService


def test_job_state_machine_requires_delivery_evidence() -> None:
    ensure_job_transition(JobStatus.FAILED, JobStatus.QUEUED)
    ensure_job_transition(JobStatus.NEEDS_INPUT, JobStatus.QUEUED)
    with pytest.raises(InvalidTransition):
        ensure_job_transition(JobStatus.SUCCEEDED, JobStatus.QUEUED)
    with pytest.raises(InvalidTransition, match="requires PR URL"):
        ensure_job_transition(JobStatus.RUNNING, JobStatus.SUCCEEDED)
    ensure_job_transition(
        JobStatus.RUNNING,
        JobStatus.SUCCEEDED,
        pr_number=1,
        pr_url="https://github.com/example/repository/pull/1",
        head_branch="mewcode/00000000-0000-0000-0000-000000000001",
        head_sha="a" * 40,
        verification_succeeded=True,
    )
    with pytest.raises(InvalidTransition):
        ensure_job_transition(
            JobStatus.RUNNING,
            JobStatus.SUCCEEDED,
            pr_url=" ",
            head_sha="not-a-sha",
            verification_succeeded=True,
        )
    with pytest.raises(InvalidTransition):
        ensure_job_transition(
            JobStatus.RUNNING,
            JobStatus.SUCCEEDED,
            pr_number=2,
            pr_url="https://github.com/example/repository/pull/1",
            head_branch="mewcode/00000000-0000-0000-0000-000000000001",
            head_sha="a" * 40,
            verification_succeeded=True,
        )


def test_attempt_state_machine_is_terminal_after_execution() -> None:
    ensure_attempt_transition(AttemptStatus.QUEUED, AttemptStatus.RUNNING)
    ensure_attempt_transition(AttemptStatus.RUNNING, AttemptStatus.FAILED)
    with pytest.raises(InvalidTransition):
        ensure_attempt_transition(AttemptStatus.FAILED, AttemptStatus.QUEUED)


def test_repository_target_requires_immutable_object_id() -> None:
    target = RepositoryTarget(1, "company", "repo", "main", "a" * 40)
    assert target.base_sha == "a" * 40
    with pytest.raises(ValueError, match="immutable"):
        RepositoryTarget(1, "company", "repo", "main", "main")


@pytest.mark.parametrize("base_ref", ["foo//bar", "foo/.hidden/bar", "foo.lock/bar", "@"])
def test_repository_request_rejects_invalid_git_branch_names(base_ref: str) -> None:
    with pytest.raises(ValueError, match="safe Git reference"):
        RepositoryRequest(
            installation_id=1,
            owner="company",
            name="repo",
            base_ref=base_ref,
        )


def test_platform_settings_use_only_explicit_environment() -> None:
    settings = PlatformSettings.from_env(
        {"MEWCODE_PLATFORM_DATABASE_URL": "postgresql://db/platform"}
    )
    assert settings.host == "127.0.0.1"
    assert settings.port == 8080
    assert settings.lease_seconds == 60
    assert settings.heartbeat_seconds == 15
    assert settings.max_concurrent_jobs == 1
    assert settings.worker_max_concurrent_attempts == 1
    assert settings.attempt_timeout_seconds == 3600
    assert settings.worker_shutdown_grace_seconds == 300
    assert settings.async_database_url == "postgresql+asyncpg://db/platform"


def test_platform_settings_separate_global_and_worker_capacity() -> None:
    settings = PlatformSettings.from_env(
        {
            "MEWCODE_PLATFORM_DATABASE_URL": "postgresql://db/platform",
            "MEWCODE_PLATFORM_MAX_CONCURRENT_JOBS": "5",
            "MEWCODE_PLATFORM_WORKER_MAX_CONCURRENT_ATTEMPTS": "2",
            "MEWCODE_PLATFORM_ATTEMPT_TIMEOUT_SECONDS": "30",
            "MEWCODE_PLATFORM_WORKER_SHUTDOWN_GRACE_SECONDS": "10",
        }
    )
    assert settings.max_concurrent_jobs == 5
    assert settings.worker_max_concurrent_attempts == 2
    assert settings.attempt_timeout_seconds == 30
    assert settings.worker_shutdown_grace_seconds == 10


@pytest.mark.parametrize(
    "name,value",
    [
        ("MEWCODE_PLATFORM_ATTEMPT_TIMEOUT_SECONDS", "3601"),
        ("MEWCODE_PLATFORM_WORKER_SHUTDOWN_GRACE_SECONDS", "3601"),
    ],
)
def test_platform_settings_bound_phase7_deadlines(name: str, value: str) -> None:
    with pytest.raises(PlatformSettingsError):
        PlatformSettings.from_env(
            {"MEWCODE_PLATFORM_DATABASE_URL": "postgresql://db/platform", name: value}
        )


def test_worker_capacity_cannot_exceed_global_capacity() -> None:
    with pytest.raises(PlatformSettingsError, match="must not exceed"):
        PlatformSettings.from_env(
            {
                "MEWCODE_PLATFORM_DATABASE_URL": "postgresql://db/platform",
                "MEWCODE_PLATFORM_MAX_CONCURRENT_JOBS": "1",
                "MEWCODE_PLATFORM_WORKER_MAX_CONCURRENT_ATTEMPTS": "2",
            }
        )


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {"MEWCODE_PLATFORM_DATABASE_URL": "sqlite:///platform.db"},
        {
            "MEWCODE_PLATFORM_DATABASE_URL": "postgresql://db/platform",
            "MEWCODE_PLATFORM_LEASE_SECONDS": "10",
            "MEWCODE_PLATFORM_HEARTBEAT_SECONDS": "10",
        },
    ],
)
def test_platform_settings_reject_unsafe_configuration(
    environment: dict[str, str],
) -> None:
    with pytest.raises(PlatformSettingsError):
        PlatformSettings.from_env(environment)


def test_runtime_event_sequence_is_explicitly_attempt_local() -> None:
    event = JobEvent(
        job_id="job",
        attempt_id="attempt",
        attempt_sequence=3,
        timestamp=datetime.now(UTC),
        event_type="text_delta",
    )
    assert event.attempt_sequence == 3
    assert event.sequence == 3


@pytest.mark.asyncio
async def test_worker_refuses_to_start_without_processor() -> None:
    settings = PlatformSettings.from_env(
        {"MEWCODE_PLATFORM_DATABASE_URL": "postgresql://db/platform"}
    )
    service = WorkerService(settings, repository=object(), processor_factory=None)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="No Attempt Processor"):
        await service.run_forever()


@pytest.mark.asyncio
async def test_persistent_event_sink_rejects_cross_job_event() -> None:
    sink = PostgresJobEventSink(
        repository=object(),  # type: ignore[arg-type]
        job_id=UUID(int=1),
        attempt_id=UUID(int=2),
        worker_id="worker",
        fencing_token=UUID(int=3),
    )
    with pytest.raises(ValueError, match="leased Job"):
        await sink.emit(
            JobEvent(
                job_id=str(UUID(int=4)),
                attempt_id=str(UUID(int=2)),
                attempt_sequence=1,
                timestamp=datetime.now(UTC),
                event_type="text_delta",
            )
        )
