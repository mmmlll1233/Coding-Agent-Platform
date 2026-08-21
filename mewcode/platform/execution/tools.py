from __future__ import annotations

from mewcode.tools.base import Tool, ToolResult
from mewcode.tools.bash import Params as BashParams, _exit_code_hint
from mewcode.tools.edit_file import Params as EditParams
from mewcode.tools.glob import Params as GlobParams
from mewcode.tools.grep import Params as GrepParams
from mewcode.tools.read_file import Params as ReadParams
from mewcode.tools.write_file import Params as WriteParams

from .base import (
    ExecutionEnvironment,
    ExecutionEnvironmentError,
    ExecutionResourceLimitError,
    WorkspaceConflictError,
    WorkspacePathError,
)
from .models import ExecutionCommand
from .redaction import SensitiveValueRedactor


def _error(prefix: str, exc: Exception) -> ToolResult:
    return ToolResult(output=f"Error {prefix}: {exc}", is_error=True)


def _fatal(code: str, message: str) -> ToolResult:
    return ToolResult(
        output=f"Error: {message}",
        is_error=True,
        fatal_error_code=code,
        fatal_error_message=message,
    )


def _environment_failure(exc: Exception) -> ToolResult:
    if isinstance(exc, ExecutionResourceLimitError):
        return _fatal("EXECUTION_RESOURCE_LIMIT", str(exc))
    return _fatal("EXECUTOR_LOST", str(exc))


class PlatformReadFile(Tool):
    name = "ReadFile"
    description = "Read a file and return its contents with line numbers."
    params_model = ReadParams
    category = "read"
    is_concurrency_safe = True

    def __init__(
        self,
        environment: ExecutionEnvironment,
        versions: dict[str, str],
        redactor: SensitiveValueRedactor,
    ) -> None:
        self.environment = environment
        self.versions = versions
        self.redactor = redactor

    async def execute(self, params: ReadParams) -> ToolResult:
        try:
            result = await self.environment.workspace.read_file(params.file_path)
        except ExecutionEnvironmentError as exc:
            return _environment_failure(exc)
        except FileNotFoundError:
            return ToolResult(
                output=f"Error: file not found: {params.file_path}", is_error=True
            )
        except (WorkspacePathError, OSError, UnicodeError) as exc:
            return _error("reading file", exc)
        self.versions[params.file_path] = result.version
        safe_content = self.redactor.redact(result.content)
        lines = safe_content.splitlines()
        selected = lines[params.offset : params.offset + params.limit]
        numbered = [
            f"{number + params.offset + 1}\t{line}"
            for number, line in enumerate(selected)
        ]
        return ToolResult(
            output="\n".join(numbered),
            recovery_path=params.file_path,
            recovery_content=safe_content,
        )


class PlatformWriteFile(Tool):
    name = "WriteFile"
    description = (
        "Write content to a file, creating parent directories if needed. Overwrites existing files.\n"
        "You MUST read existing files with ReadFile before overwriting them. This tool will fail otherwise."
    )
    params_model = WriteParams
    category = "write"

    def __init__(self, environment: ExecutionEnvironment, versions: dict[str, str]) -> None:
        self.environment = environment
        self.versions = versions

    async def execute(self, params: WriteParams) -> ToolResult:
        try:
            result = await self.environment.workspace.write_file(
                params.file_path,
                params.content,
                self.versions.get(params.file_path),
            )
        except ExecutionEnvironmentError as exc:
            return _environment_failure(exc)
        except (WorkspaceConflictError, WorkspacePathError) as exc:
            return ToolResult(output=f"Error: {exc}", is_error=True)
        except (OSError, UnicodeError) as exc:
            return _error("writing file", exc)
        self.versions[params.file_path] = result.version
        return ToolResult(output=f"Successfully wrote to {params.file_path}")


class PlatformEditFile(Tool):
    name = "EditFile"
    description = (
        "Replace an exact string in a file. The old_string must appear exactly once in the file.\n"
        "You MUST read the file with ReadFile before editing. This tool will fail otherwise."
    )
    params_model = EditParams
    category = "write"

    def __init__(self, environment: ExecutionEnvironment, versions: dict[str, str]) -> None:
        self.environment = environment
        self.versions = versions

    async def execute(self, params: EditParams) -> ToolResult:
        expected = self.versions.get(params.file_path)
        if expected is None:
            return ToolResult(
                output="Error: file has not been read yet. Read it first before editing.",
                is_error=True,
            )
        try:
            result = await self.environment.workspace.edit_file(
                params.file_path,
                params.old_string,
                params.new_string,
                expected,
            )
        except ExecutionEnvironmentError as exc:
            return _environment_failure(exc)
        except FileNotFoundError:
            return ToolResult(
                output=f"Error: file not found: {params.file_path}", is_error=True
            )
        except (WorkspaceConflictError, WorkspacePathError, ValueError) as exc:
            return ToolResult(output=f"Error: {exc}", is_error=True)
        self.versions[params.file_path] = result.version
        return ToolResult(output=f"Successfully edited {params.file_path}")


class PlatformGlob(Tool):
    name = "Glob"
    description = "Find files matching a glob pattern, returning relative paths."
    params_model = GlobParams
    category = "read"
    is_concurrency_safe = True

    def __init__(
        self, environment: ExecutionEnvironment, redactor: SensitiveValueRedactor
    ) -> None:
        self.environment = environment
        self.redactor = redactor

    async def execute(self, params: GlobParams) -> ToolResult:
        try:
            matches = await self.environment.workspace.glob(params.path, params.pattern)
        except ExecutionEnvironmentError as exc:
            return _environment_failure(exc)
        except (WorkspacePathError, OSError, ValueError) as exc:
            return ToolResult(output=f"Error: {exc}", is_error=True)
        output = "\n".join(matches) if matches else "No files matched the pattern."
        return ToolResult(output=self.redactor.redact(output))


class PlatformGrep(Tool):
    name = "Grep"
    description = "Search file contents using a regex pattern, returning file:line:content matches."
    params_model = GrepParams
    category = "read"
    is_concurrency_safe = True

    def __init__(
        self, environment: ExecutionEnvironment, redactor: SensitiveValueRedactor
    ) -> None:
        self.environment = environment
        self.redactor = redactor

    async def execute(self, params: GrepParams) -> ToolResult:
        try:
            matches = await self.environment.workspace.grep(
                params.path, params.pattern, params.include
            )
        except ExecutionEnvironmentError as exc:
            return _environment_failure(exc)
        except (WorkspacePathError, OSError, ValueError) as exc:
            return ToolResult(output=f"Error: {exc}", is_error=True)
        output = "\n".join(matches) if matches else "No matches found."
        return ToolResult(output=self.redactor.redact(output))


class PlatformBash(Tool):
    name = "Bash"
    description = "Execute a shell command in the isolated Attempt environment."
    params_model = BashParams
    category = "command"

    def __init__(
        self, environment: ExecutionEnvironment, redactor: SensitiveValueRedactor
    ) -> None:
        self.environment = environment
        self.redactor = redactor

    async def execute(self, params: BashParams) -> ToolResult:
        try:
            outcome = await self.environment.run_command(
                ExecutionCommand(command=params.command, timeout_seconds=params.timeout)
            )
        except ExecutionEnvironmentError as exc:
            return _environment_failure(exc)
        command_result = outcome.command_result
        parts: list[str] = []
        if command_result.stdout:
            parts.append(command_result.stdout.rstrip())
        if command_result.stderr:
            parts.append("stderr:\n" + command_result.stderr.rstrip())
        if command_result.timed_out:
            parts.append(f"Error: command timed out after {params.timeout}s")
        elif command_result.exit_code not in (None, 0):
            parts.append(_exit_code_hint(params.command, command_result.exit_code))
        elif command_result.exit_code is None:
            parts.append("Command failed to start")
        output = self.redactor.redact(
            "\n\n".join(part for part in parts if part) or "(no output)"
        )
        return ToolResult(
            output=output,
            is_error=(
                command_result.timed_out
                or command_result.exit_code is None
                or command_result.exit_code != 0
                or outcome.fatal_error_code is not None
            ),
            command_result=command_result,
            fatal_error_code=outcome.fatal_error_code,
            fatal_error_message=outcome.fatal_error_message,
        )


def create_platform_registry(
    environment: ExecutionEnvironment,
    redactor: SensitiveValueRedactor | None = None,
):
    from mewcode.tools import ToolRegistry

    versions: dict[str, str] = {}
    redactor = redactor or SensitiveValueRedactor(environment.spec.secret_values)
    registry = ToolRegistry()
    registry.register(PlatformReadFile(environment, versions, redactor))
    registry.register(PlatformWriteFile(environment, versions))
    registry.register(PlatformEditFile(environment, versions))
    registry.register(PlatformBash(environment, redactor))
    registry.register(PlatformGlob(environment, redactor))
    registry.register(PlatformGrep(environment, redactor))
    return registry
