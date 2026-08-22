from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mewcode.platform.persistence.orm import AttemptRow, JobRow
from mewcode.platform.persistence.repositories import StoredEvent


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RepositoryRequest(StrictModel):
    installation_id: int = Field(gt=0)
    owner: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    base_ref: str = Field(min_length=1, max_length=255)

    @field_validator("base_ref")
    @classmethod
    def safe_ref(cls, value: str) -> str:
        parts = value.split("/")
        if (
            value.startswith("-")
            or value == "@"
            or ".." in value
            or "@{" in value
            or value.startswith(("/", "."))
            or value.endswith(("/", ".", ".lock"))
            or any(
                not part
                or part.startswith(".")
                or part.endswith((".", ".lock"))
                for part in parts
            )
            or any(char in value for char in "~^:?*[\\")
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or any(char.isspace() for char in value)
        ):
            raise ValueError("base_ref is not a safe Git reference")
        return value


class WorkRequestBody(StrictModel):
    kind: Literal["bugfix", "small_change"]
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=50_000)
    expected_behavior: str = Field(default="", max_length=20_000)
    reproduction: str = Field(default="", max_length=20_000)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("acceptance_criteria")
    @classmethod
    def criteria_are_bounded(cls, values: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 2_000 for item in values):
            raise ValueError(
                "acceptance criteria must be non-empty and at most 2000 characters"
            )
        return values


class CommandRequest(StrictModel):
    name: str = Field(min_length=1, max_length=128)
    command: str = Field(min_length=1, max_length=4_096)
    timeout_seconds: int = Field(ge=1, le=600)


class ExecutionRequest(StrictModel):
    setup_commands: list[CommandRequest] = Field(default_factory=list, max_length=10)
    verification_commands: list[CommandRequest] = Field(min_length=1, max_length=10)

    @field_validator("setup_commands", "verification_commands")
    @classmethod
    def command_names_are_unique(
        cls, values: list[CommandRequest]
    ) -> list[CommandRequest]:
        names = [item.name for item in values]
        if len(names) != len(set(names)):
            raise ValueError("command names must be unique within each command list")
        return values


class CreateJobRequest(StrictModel):
    repository: RepositoryRequest
    work: WorkRequestBody
    execution: ExecutionRequest
    attachment_ids: list[UUID] = Field(default_factory=list, max_length=0)

    @model_validator(mode="after")
    def reject_secret_material(self) -> CreateJobRequest:
        serialized = json.dumps(self.model_dump(mode="json"), ensure_ascii=False)
        secret_patterns = (
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----",
            r"https?://[^/\s:@]+:[^@\s]+@",
            r"\bgithub_pat_[A-Za-z0-9_]{20,}\b",
            r"\bgh[pousr]_[A-Za-z0-9_.-]{20,}\b",
            r"\bsk-ant-[A-Za-z0-9_-]{16,}\b",
            r"\bsk-[A-Za-z0-9_-]{20,}\b",
            r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b",
        )
        if any(re.search(pattern, serialized) for pattern in secret_patterns):
            raise ValueError("request payload must not contain credentials or secrets")
        return self

    def canonical_hash(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class JobInputRequest(StrictModel):
    content: str = Field(min_length=1, max_length=20_000)
    attachment_ids: list[UUID] = Field(default_factory=list, max_length=0)


class AttemptResponse(BaseModel):
    id: UUID
    attempt_no: int
    status: str
    stage: str | None
    worker_id: str | None
    lease_expires_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    failure_code: str | None

    @classmethod
    def from_row(cls, row: AttemptRow) -> AttemptResponse:
        return cls(
            id=row.id,
            attempt_no=row.attempt_no,
            status=row.status,
            stage=row.stage,
            worker_id=row.worker_id,
            lease_expires_at=row.lease_expires_at,
            started_at=row.started_at,
            finished_at=row.finished_at,
            failure_code=row.failure_code,
        )


class RepositoryTargetResponse(BaseModel):
    installation_id: int
    owner: str
    name: str
    base_ref: str
    base_sha: str


class JobResponse(BaseModel):
    id: UUID
    status: str
    stage: str | None
    current_attempt_no: int
    repository: RepositoryTargetResponse
    work: dict[str, Any]
    execution: dict[str, Any]
    current_attempt: AttemptResponse | None
    pr_number: int | None
    pr_url: str | None
    head_branch: str | None
    head_sha: str | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None

    @classmethod
    def from_rows(cls, job: JobRow, attempt: AttemptRow | None = None) -> JobResponse:
        return cls(
            id=job.id,
            status=job.status,
            stage=job.stage,
            current_attempt_no=job.current_attempt_no,
            repository=RepositoryTargetResponse(
                installation_id=job.installation_id,
                owner=job.repo_owner,
                name=job.repo_name,
                base_ref=job.base_ref,
                base_sha=job.base_sha or "",
            ),
            work=job.work_request,
            execution=job.execution_contract,
            current_attempt=(AttemptResponse.from_row(attempt) if attempt else None),
            pr_number=job.pr_number,
            pr_url=job.pr_url,
            head_branch=job.head_branch,
            head_sha=job.head_sha,
            error_code=job.error_code,
            error_message=job.error_message,
            created_at=job.created_at,
            updated_at=job.updated_at,
            finished_at=job.finished_at,
        )


class EventResponse(BaseModel):
    id: UUID
    job_id: UUID
    attempt_id: UUID | None
    sequence: int
    attempt_sequence: int | None
    event_type: str
    payload: dict[str, Any]
    created_at: datetime

    @classmethod
    def from_stored(cls, event: StoredEvent) -> EventResponse:
        return cls(**event.__dict__)


class EventPage(BaseModel):
    items: list[EventResponse]
    next_after: int
    has_more: bool


class HealthResponse(BaseModel):
    status: str
    checks: dict[str, bool] = Field(default_factory=dict)


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Any = None
    request_id: str


class ErrorEnvelope(BaseModel):
    error: ErrorBody
