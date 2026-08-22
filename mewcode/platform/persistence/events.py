from __future__ import annotations

from typing import Any
from uuid import UUID

from mewcode.platform.execution import SensitiveValueRedactor
from mewcode.platform.runtime import JobEvent, JobEventSink

from .repositories import PlatformRepository


class PostgresJobEventSink(JobEventSink):
    """Persist Runtime events behind an active, fenced Worker Lease."""

    def __init__(
        self,
        repository: PlatformRepository,
        *,
        job_id: UUID,
        attempt_id: UUID,
        worker_id: str,
        fencing_token: UUID,
        redactor: SensitiveValueRedactor | None = None,
    ) -> None:
        self.repository = repository
        self.job_id = job_id
        self.attempt_id = attempt_id
        self.worker_id = worker_id
        self.fencing_token = fencing_token
        self.redactor = redactor or SensitiveValueRedactor(())

    def _redact(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redactor.redact(value)
        if isinstance(value, dict):
            return {str(key): self._redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, tuple):
            return [self._redact(item) for item in value]
        return value

    async def emit(self, event: JobEvent) -> None:
        if UUID(event.job_id) != self.job_id:
            raise ValueError("Runtime JobEvent does not match the leased Job")
        if UUID(event.attempt_id) != self.attempt_id:
            raise ValueError("Runtime JobEvent does not match the leased Attempt")
        await self.repository.append_attempt_event(
            attempt_id=self.attempt_id,
            worker_id=self.worker_id,
            fencing_token=self.fencing_token,
            attempt_sequence=event.attempt_sequence,
            event_type=event.event_type,
            payload=self._redact(event.payload),
        )
