from __future__ import annotations

import fnmatch
import hashlib
import io
import re
import tarfile
from collections.abc import Awaitable, Callable
from pathlib import PurePosixPath
from pathlib import Path

from mewcode.tools.base import CommandExecutionResult

from .base import WorkspaceConflictError, WorkspacePathError
from .models import (
    AttemptExecutionSpec,
    ExecutionCommand,
    ExecutionCommandOutcome,
    ExecutionState,
    RuntimeEnvironmentInfo,
    WorkspaceReadResult,
    WorkspaceWriteResult,
)


def normalize_workspace_path(path: str, *, allow_root: bool = False) -> str:
    if not path or "\x00" in path or path.startswith("~"):
        raise WorkspacePathError(f"Invalid workspace path: {path!r}")
    normalized_input = path.replace("\\", "/")
    pure = PurePosixPath(normalized_input)
    if pure.is_absolute():
        try:
            pure = pure.relative_to("/workspace")
        except ValueError as exc:
            raise WorkspacePathError(f"Path is outside /workspace: {path}") from exc
    if any(part in ("", ".", "..") for part in pure.parts):
        raise WorkspacePathError(f"Path traversal is forbidden: {path}")
    normalized = pure.as_posix()
    if not allow_root and normalized in ("", "."):
        raise WorkspacePathError("Workspace root is not a file")
    if normalized == ".mewcode" or normalized.startswith(".mewcode/"):
        raise WorkspacePathError("Repository .mewcode extensions are quarantined")
    if normalized == ".git" or normalized.startswith(".git/"):
        raise WorkspacePathError("Repository Git metadata is outside Agent execution")
    return normalized


def _version(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class InMemoryWorkspaceAccess:
    def __init__(self, files: dict[str, str] | None = None) -> None:
        self.files: dict[str, str] = {}
        for path, content in (files or {}).items():
            self.files[normalize_workspace_path(path)] = content

    async def read_file(self, path: str) -> WorkspaceReadResult:
        normalized = normalize_workspace_path(path)
        if normalized not in self.files:
            raise FileNotFoundError(path)
        content = self.files[normalized]
        return WorkspaceReadResult(content=content, version=_version(content))

    async def write_file(
        self, path: str, content: str, expected_version: str | None
    ) -> WorkspaceWriteResult:
        normalized = normalize_workspace_path(path)
        if normalized == ".github" or normalized.startswith(".github/"):
            raise WorkspacePathError(".github is read-only in platform execution")
        current = self.files.get(normalized)
        if current is not None:
            if expected_version is None:
                raise WorkspaceConflictError(
                    "File has not been read yet. Read it first before editing."
                )
            if _version(current) != expected_version:
                raise WorkspaceConflictError(
                    "File has been modified since last read. Read it again before editing."
                )
        self.files[normalized] = content
        return WorkspaceWriteResult(version=_version(content))

    async def edit_file(
        self,
        path: str,
        old_string: str,
        new_string: str,
        expected_version: str,
    ) -> WorkspaceWriteResult:
        normalized = normalize_workspace_path(path)
        if normalized == ".github" or normalized.startswith(".github/"):
            raise WorkspacePathError(".github is read-only in platform execution")
        if normalized not in self.files:
            raise FileNotFoundError(path)
        current = self.files[normalized]
        if _version(current) != expected_version:
            raise WorkspaceConflictError(
                "File has been modified since last read. Read it again before editing."
            )
        count = current.count(old_string)
        if count == 0:
            raise ValueError("old_string not found in file")
        if count > 1:
            raise ValueError(f"old_string found {count} times, must be unique")
        updated = current.replace(old_string, new_string, 1)
        self.files[normalized] = updated
        return WorkspaceWriteResult(version=_version(updated))

    async def glob(self, path: str, pattern: str) -> list[str]:
        base = normalize_workspace_path(path, allow_root=True) if path not in ("", ".") else ""
        prefix = f"{base}/" if base else ""
        return sorted(
            relative
            for candidate in self.files
            if candidate.startswith(prefix)
            for relative in [candidate[len(prefix) :]]
            if fnmatch.fnmatch(relative, pattern)
        )

    async def grep(self, path: str, pattern: str, include: str) -> list[str]:
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"invalid regex: {exc}") from exc
        base = normalize_workspace_path(path, allow_root=True) if path not in ("", ".") else ""
        prefix = f"{base}/" if base else ""
        matches: list[str] = []
        for candidate in sorted(self.files):
            if not candidate.startswith(prefix):
                continue
            relative = candidate[len(prefix) :]
            if include and not fnmatch.fnmatch(PurePosixPath(relative).name, include):
                continue
            for number, line in enumerate(self.files[candidate].splitlines(), 1):
                if regex.search(line):
                    matches.append(f"{relative}:{number}:{line}")
        return matches


class FakeExecutionEnvironment:
    def __init__(
        self,
        spec: AttemptExecutionSpec,
        *,
        files: dict[str, str] | None = None,
        command_handler: Callable[[ExecutionCommand], Awaitable[ExecutionCommandOutcome]]
        | None = None,
        cleanup_error: Exception | None = None,
    ) -> None:
        self.spec = spec
        self.state = ExecutionState.CREATED
        self.runtime_info = RuntimeEnvironmentInfo()
        self.workspace = InMemoryWorkspaceAccess(files)
        self._handler = command_handler
        self._cleanup_error = cleanup_error
        self._workspace_imported = False
        self.start_count = 0
        self.close_count = 0

    async def start(self) -> None:
        if self.state == ExecutionState.CLOSED:
            raise RuntimeError("Cannot start a closed ExecutionEnvironment")
        if self.state == ExecutionState.READY:
            return
        self.start_count += 1
        self.spec.trusted_state_dir.mkdir(parents=True, exist_ok=True)
        self.state = ExecutionState.READY

    async def run_command(self, command: ExecutionCommand) -> ExecutionCommandOutcome:
        if self.state != ExecutionState.READY:
            raise RuntimeError("ExecutionEnvironment is not ready")
        if self._handler is not None:
            return await self._handler(command)
        return ExecutionCommandOutcome(
            CommandExecutionResult(exit_code=0, stdout="", stderr="")
        )

    async def import_archive(self, archive: bytes) -> None:
        if self._workspace_imported:
            raise RuntimeError("Attempt Workspace can be initialized only once")
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                raw = member.name.replace("\\", "/").removeprefix("./")
                if raw == ".env":
                    continue
                if raw == ".git" or raw.startswith(".git/"):
                    continue
                if raw == ".mewcode" or raw.startswith(".mewcode/"):
                    continue
                path = normalize_workspace_path(raw)
                extracted = tar.extractfile(member)
                if extracted is not None:
                    self.workspace.files[path] = extracted.read().decode("utf-8")
        self._workspace_imported = True

    async def export_archive(self) -> bytes:
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w") as tar:
            for path, content in sorted(self.workspace.files.items()):
                data = content.encode("utf-8")
                info = tarfile.TarInfo(path)
                info.size = len(data)
                info.mode = 0o640
                tar.addfile(info, io.BytesIO(data))
        return output.getvalue()

    async def import_archive_file(self, archive_path: Path) -> None:
        await self.import_archive(archive_path.read_bytes())

    async def export_archive_file(self, archive_path: Path) -> None:
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        archive_path.write_bytes(await self.export_archive())

    async def aclose(self) -> None:
        if self.state == ExecutionState.CLOSED:
            return
        self.close_count += 1
        self.state = ExecutionState.CLOSED
        if self._cleanup_error is not None:
            raise self._cleanup_error
