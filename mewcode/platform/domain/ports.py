from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from mewcode.platform.runtime import JobEventSink

from .models import AttemptLease, AttemptOutcome, AttemptStage, RepositoryTarget


class RepositoryTargetUnavailable(RuntimeError):
    pass


class RepositoryTargetResolver(Protocol):
    async def resolve(
        self,
        *,
        installation_id: int,
        owner: str,
        name: str,
        base_ref: str,
    ) -> RepositoryTarget: ...


StageReporter = Callable[[AttemptStage], Awaitable[None]]


@dataclass(frozen=True)
class AttemptControls:
    event_sink: JobEventSink
    report_stage: StageReporter
    cancellation: asyncio.Event


class AttemptProcessor(Protocol):
    async def process(
        self, lease: AttemptLease, controls: AttemptControls
    ) -> AttemptOutcome: ...

    async def cancel(self) -> None: ...


class AttemptProcessorFactory(Protocol):
    def create(self, lease: AttemptLease) -> AttemptProcessor: ...
