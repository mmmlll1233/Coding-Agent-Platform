from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol


class JobRunStatus(str, Enum):
    COMPLETED = "COMPLETED"
    NEEDS_INPUT = "NEEDS_INPUT"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class JobRunRequest:
    job_id: str
    attempt_id: str
    prompt: str


@dataclass(frozen=True)
class JobEvent:
    job_id: str
    attempt_id: str
    attempt_sequence: int
    timestamp: datetime
    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def sequence(self) -> int:
        """Compatibility alias for the producer-local Attempt sequence."""
        return self.attempt_sequence


@dataclass(frozen=True)
class JobResult:
    job_id: str
    attempt_id: str
    status: JobRunStatus
    final_text: str
    total_turns: int
    input_tokens: int
    output_tokens: int
    error_code: str | None
    error_message: str | None
    started_at: datetime
    finished_at: datetime


class JobEventSink(Protocol):
    async def emit(self, event: JobEvent) -> None: ...


class NullJobEventSink:
    async def emit(self, event: JobEvent) -> None:
        return None


class InMemoryJobEventSink:
    """Simple ordered sink for tests and local integrations."""

    def __init__(self) -> None:
        self.events: list[JobEvent] = []

    async def emit(self, event: JobEvent) -> None:
        self.events.append(event)
