from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from mewcode.platform.runtime import JobEventSink

from .models import (
    AttemptLease,
    AttemptOutcome,
    AttemptStage,
    Delivery,
    PreparedRepository,
    RepositoryTarget,
    VerifiedDeliveryRequest,
)


class RepositoryTargetUnavailable(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "REPOSITORY_RESOLVER_UNAVAILABLE",
    ) -> None:
        super().__init__(message)
        self.code = code


class RepositoryTargetRejected(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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


class ScmAdapter(Protocol):
    async def prepare(
        self, target: RepositoryTarget, trusted_state_dir: Path
    ) -> PreparedRepository: ...

    async def publish_verified(
        self, request: VerifiedDeliveryRequest
    ) -> Delivery: ...

    async def aclose(self) -> None: ...
