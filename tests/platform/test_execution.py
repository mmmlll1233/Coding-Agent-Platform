from __future__ import annotations

import io
import tarfile
from types import SimpleNamespace

import pytest

from mewcode.conversation import ConversationManager
from mewcode.platform.execution import (
    AttemptExecutionSpec,
    ExecutionCommand,
    ExecutionCommandOutcome,
    ExecutionEnvironmentError,
    ExecutionLimits,
    FakeExecutionEnvironment,
    SensitiveValueRedactor,
    WorkspacePathError,
    create_platform_registry,
)
from mewcode.platform.runtime import (
    InMemoryJobEventSink,
    JobRunRequest,
    JobRunner,
    JobRunStatus,
)
from mewcode.tools.base import CommandExecutionResult
from mewcode.tools.bash import Params as BashParams
from mewcode.tools.edit_file import Params as EditParams
from mewcode.tools.read_file import Params as ReadParams
from mewcode.tools.write_file import Params as WriteParams


def _spec(tmp_path, **overrides) -> AttemptExecutionSpec:
    values = {
        "job_id": "job-1",
        "attempt_id": "attempt-1",
        "executor_image": "sha256:" + "1" * 64,
        "proxy_image": "sha256:" + "2" * 64,
        "trusted_state_dir": tmp_path / "state",
    }
    values.update(overrides)
    return AttemptExecutionSpec(**values)


def test_execution_spec_rejects_mutable_images_and_unsafe_domains(tmp_path) -> None:
    with pytest.raises(ValueError, match="immutable"):
        _spec(tmp_path, executor_image="python:3.13-slim")
    with pytest.raises(ValueError, match="allowlist"):
        _spec(tmp_path, egress_allowlist=("*.example.com",))
    with pytest.raises(ValueError, match="Attempt timeout"):
        _spec(
            tmp_path,
            limits=ExecutionLimits(
                command_timeout_seconds=10,
                attempt_timeout_seconds=5,
            ),
        )


def test_execution_limit_defaults_are_platform_contract() -> None:
    limits = ExecutionLimits()
    assert limits.cpus == 4
    assert limits.memory_bytes == 6 * 1024**3
    assert limits.pids_limit == 256
    assert limits.workspace_bytes == 3 * 1024**3
    assert limits.tmp_bytes == 256 * 1024**2
    assert limits.workspace_inodes == 300_000
    assert limits.command_timeout_seconds == 600
    assert limits.attempt_timeout_seconds == 3600
    assert limits.max_output_bytes == 1024**2


def test_attempt_state_directories_are_isolated_by_identity(tmp_path) -> None:
    first = _spec(tmp_path, attempt_id="attempt-1")
    retry = _spec(tmp_path, attempt_id="attempt-2")
    assert first.trusted_state_dir != retry.trusted_state_dir
    assert first.trusted_state_dir.parent == tmp_path.resolve() / "state" / "attempts"
    assert retry.trusted_state_dir.parent == first.trusted_state_dir.parent


@pytest.mark.asyncio
async def test_execution_environment_file_archive_api_preserves_compatibility(
    tmp_path,
) -> None:
    source = tmp_path / "source.tar"
    with tarfile.open(source, mode="w") as archive:
        content = b"phase4\n"
        info = tarfile.TarInfo("hello.txt")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    environment = FakeExecutionEnvironment(_spec(tmp_path))
    await environment.start()
    await environment.import_archive_file(source)
    assert (await environment.workspace.read_file("hello.txt")).content == "phase4\n"
    exported = tmp_path / "exported.tar"
    await environment.export_archive_file(exported)
    assert exported.is_file()
    with tarfile.open(exported, mode="r:*") as archive:
        assert archive.extractfile("hello.txt").read() == b"phase4\n"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_platform_tools_preserve_read_before_write_and_revision(tmp_path) -> None:
    environment = FakeExecutionEnvironment(
        _spec(tmp_path), files={"src/app.py": "value = 1\n"}
    )
    await environment.start()
    registry = create_platform_registry(environment)
    read = registry.get("ReadFile")
    write = registry.get("WriteFile")
    edit = registry.get("EditFile")
    assert read is not None and write is not None and edit is not None

    blocked = await edit.execute(EditParams(
        file_path="src/app.py", old_string="1", new_string="2"
    ))
    assert blocked.is_error is True
    assert "has not been read" in blocked.output

    read_result = await read.execute(ReadParams(file_path="src/app.py"))
    assert read_result.is_error is False
    assert "1\tvalue = 1" in read_result.output

    edited = await edit.execute(EditParams(
        file_path="src/app.py", old_string="1", new_string="2"
    ))
    assert edited.is_error is False
    assert environment.workspace.files["src/app.py"] == "value = 2\n"

    created = await write.execute(WriteParams(file_path="new.txt", content="new"))
    assert created.is_error is False


@pytest.mark.asyncio
async def test_platform_workspace_rejects_escape_and_read_only_metadata(tmp_path) -> None:
    environment = FakeExecutionEnvironment(
        _spec(tmp_path), files={".github/workflows/test.yml": "safe"}
    )
    await environment.start()
    with pytest.raises(WorkspacePathError):
        await environment.workspace.read_file("../host.txt")
    with pytest.raises(WorkspacePathError):
        await environment.workspace.read_file("/etc/passwd")
    with pytest.raises(WorkspacePathError):
        await environment.workspace.write_file(
            ".github/workflows/test.yml", "bad", None
        )


@pytest.mark.asyncio
async def test_executor_loss_becomes_terminal_tool_error(tmp_path) -> None:
    async def lost(command: ExecutionCommand) -> ExecutionCommandOutcome:
        raise ExecutionEnvironmentError("daemon connection lost")

    environment = FakeExecutionEnvironment(_spec(tmp_path), command_handler=lost)
    await environment.start()
    bash = create_platform_registry(environment).get("Bash")
    assert bash is not None
    result = await bash.execute(BashParams(command="true"))
    assert result.is_error is True
    assert result.fatal_error_code == "EXECUTOR_LOST"


@pytest.mark.asyncio
async def test_job_runner_owns_environment_lifecycle_and_redacts(tmp_path) -> None:
    secret = "CANARY_SUPER_SECRET_VALUE"

    async def command_handler(command: ExecutionCommand) -> ExecutionCommandOutcome:
        return ExecutionCommandOutcome(
            CommandExecutionResult(0, f"value={secret}", "")
        )

    environment = FakeExecutionEnvironment(
        _spec(tmp_path, secret_values=(secret,)), command_handler=command_handler
    )

    class Agent:
        async def run(self, conversation):
            from mewcode.agent import LoopComplete, StreamText

            yield StreamText(f"done {secret}")
            yield LoopComplete(1)

    from mewcode.platform.execution import SensitiveValueRedactor

    runtime = SimpleNamespace(
        agent=Agent(),
        conversation=ConversationManager(),
        services={
            "execution_environment": environment,
            "redactor": SensitiveValueRedactor((secret,)),
        },
        start=environment.start,
        aclose=environment.aclose,
    )
    sink = InMemoryJobEventSink()
    result = await JobRunner(runtime, sink).run(
        JobRunRequest("job-1", "attempt-1", "go")
    )

    assert result.status == JobRunStatus.COMPLETED
    assert environment.start_count == 1
    assert environment.close_count == 1
    assert secret not in result.final_text
    assert secret not in repr(sink.events)


@pytest.mark.asyncio
async def test_cleanup_failure_overrides_cancelled_or_completed_result(tmp_path) -> None:
    secret = "CLEANUP_CANARY_SUPER_SECRET"
    environment = FakeExecutionEnvironment(
        _spec(tmp_path, secret_values=(secret,)),
        cleanup_error=RuntimeError(f"leftover volume {secret}"),
    )

    class Agent:
        async def run(self, conversation):
            from mewcode.agent import LoopComplete

            yield LoopComplete(1)

    runtime = SimpleNamespace(
        agent=Agent(),
        conversation=ConversationManager(),
        services={
            "execution_environment": environment,
            "redactor": SensitiveValueRedactor((secret,)),
        },
        start=environment.start,
        aclose=environment.aclose,
    )
    result = await JobRunner(runtime).run(
        JobRunRequest("job-1", "attempt-1", "go")
    )
    assert result.status == JobRunStatus.FAILED
    assert result.error_code == "EXECUTOR_CLEANUP_FAILED"
    assert secret not in (result.error_message or "")
    assert "[REDACTED]" in (result.error_message or "")
