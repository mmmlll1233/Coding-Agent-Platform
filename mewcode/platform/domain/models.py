from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class JobStatus(StrEnum):
    RECEIVED = "RECEIVED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    NEEDS_INPUT = "NEEDS_INPUT"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AttemptStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    NEEDS_INPUT = "NEEDS_INPUT"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AttemptStage(StrEnum):
    PREPARING = "PREPARING"
    ANALYZING = "ANALYZING"
    IMPLEMENTING = "IMPLEMENTING"
    VERIFYING = "VERIFYING"
    PUBLISHING = "PUBLISHING"
    CLEANING_UP = "CLEANING_UP"


class AttemptOutcomeStatus(StrEnum):
    COMPLETED = "COMPLETED"
    NEEDS_INPUT = "NEEDS_INPUT"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class RepositoryTarget:
    installation_id: int
    owner: str
    name: str
    base_ref: str
    base_sha: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", self.base_sha):
            raise ValueError("base_sha must be an immutable hexadecimal object ID")


@dataclass(frozen=True)
class AttemptLease:
    job_id: UUID
    attempt_id: UUID
    attempt_no: int
    tenant_id: UUID
    requester_id: UUID
    worker_id: str
    fencing_token: UUID
    lease_expires_at: datetime
    repository_target: RepositoryTarget
    work_request: dict[str, Any]
    execution_contract: dict[str, Any]
    inputs: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class AttemptOutcome:
    status: AttemptOutcomeStatus
    error_code: str | None = None
    error_message: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    pr_url: str | None = None
    head_sha: str | None = None
    verification_succeeded: bool = False
