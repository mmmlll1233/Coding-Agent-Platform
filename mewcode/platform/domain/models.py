from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
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
    pr_number: int | None = None
    pr_url: str | None = None
    head_branch: str | None = None
    head_sha: str | None = None
    verification_succeeded: bool = False


@dataclass(frozen=True)
class PreparedRepository:
    target: RepositoryTarget
    base_tree_sha: str
    archive_path: Path
    manifest_path: Path

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", self.base_tree_sha):
            raise ValueError("base_tree_sha must be an immutable object ID")


@dataclass(frozen=True)
class VerifiedDeliveryRequest:
    job_id: UUID
    prepared: PreparedRepository
    workspace_archive_path: Path
    work_title: str
    work_summary: str
    change_summary: str
    verification_summary: str
    risks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.work_title.strip():
            raise ValueError("work_title is required")
        if not self.verification_summary.strip():
            raise ValueError("verified Delivery requires Verification evidence")


@dataclass(frozen=True)
class Delivery:
    pr_number: int
    pr_url: str
    head_branch: str
    head_sha: str

    def __post_init__(self) -> None:
        if self.pr_number <= 0:
            raise ValueError("pr_number must be positive")
        match = re.fullmatch(
            r"https://github[.]com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/([1-9][0-9]*)",
            self.pr_url,
        )
        if match is None or int(match.group(1)) != self.pr_number:
            raise ValueError("pr_url must identify the matching GitHub.com Pull Request")
        if not re.fullmatch(
            r"mewcode/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            self.head_branch,
        ):
            raise ValueError("head_branch must be the deterministic MewCode branch")
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", self.head_sha):
            raise ValueError("head_sha must be an immutable object ID")
