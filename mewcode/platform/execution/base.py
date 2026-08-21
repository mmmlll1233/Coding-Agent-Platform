from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import (
    AttemptExecutionSpec,
    ExecutionCommand,
    ExecutionCommandOutcome,
    ExecutionState,
    RuntimeEnvironmentInfo,
    WorkspaceReadResult,
    WorkspaceWriteResult,
)


class WorkspaceConflictError(RuntimeError):
    pass


class WorkspacePathError(RuntimeError):
    pass


class ExecutionEnvironmentError(RuntimeError):
    pass


class ExecutionCleanupError(ExecutionEnvironmentError):
    pass


class ExecutionResourceLimitError(ExecutionEnvironmentError):
    """A workspace or executor resource boundary was reached."""

    pass


@runtime_checkable
class WorkspaceAccess(Protocol):
    async def read_file(self, path: str) -> WorkspaceReadResult: ...

    async def write_file(
        self, path: str, content: str, expected_version: str | None
    ) -> WorkspaceWriteResult: ...

    async def edit_file(
        self,
        path: str,
        old_string: str,
        new_string: str,
        expected_version: str,
    ) -> WorkspaceWriteResult: ...

    async def glob(self, path: str, pattern: str) -> list[str]: ...

    async def grep(self, path: str, pattern: str, include: str) -> list[str]: ...


@runtime_checkable
class ExecutionEnvironment(Protocol):
    spec: AttemptExecutionSpec
    state: ExecutionState
    workspace: WorkspaceAccess
    runtime_info: RuntimeEnvironmentInfo

    async def start(self) -> None: ...

    async def run_command(self, command: ExecutionCommand) -> ExecutionCommandOutcome: ...

    async def import_archive(self, archive: bytes) -> None: ...

    async def export_archive(self) -> bytes: ...

    async def aclose(self) -> None: ...
