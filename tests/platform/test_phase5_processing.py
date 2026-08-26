from __future__ import annotations

import asyncio
import io
import json
import tarfile
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from mewcode.agent import LoopComplete
from mewcode.conversation import ConversationManager
from mewcode.platform.artifacts import ArtifactKind
from mewcode.platform.domain import (
    AttemptControls,
    AttemptLease,
    AttemptOutcomeStatus,
    AttemptStage,
    Delivery,
    PreparedRepository,
    RepositoryTarget,
)
from mewcode.platform.execution import (
    AttemptExecutionSpec,
    ExecutionCleanupError,
    ExecutionCommandOutcome,
    FakeExecutionEnvironment,
    SensitiveValueRedactor,
)
from mewcode.platform.persistence import ArtifactMetadata
from mewcode.platform.processing import ProductionAttemptProcessor
from mewcode.platform.runtime import InMemoryJobEventSink
from mewcode.platform.scm.archive import scan_workspace_archive
from mewcode.platform.settings import PlatformSettings
from mewcode.tools.base import CommandExecutionResult


def _archive(path: Path, files: dict[str, str]) -> None:
    with tarfile.open(path, mode="w") as archive:
        for name, value in sorted(files.items()):
            content = value.encode()
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(content))


def _prepared(tmp_path: Path) -> PreparedRepository:
    archive = tmp_path / "prepared.tar"
    manifest = tmp_path / "manifest.json"
    _archive(archive, {"README.md": "before\n", "AGENTS.md": "Use pytest.\n"})
    entries = scan_workspace_archive(archive)
    manifest.write_text(
        json.dumps(
            {
                "base_sha": "a" * 40,
                "base_tree_sha": "b" * 40,
                "files": {name: asdict(value) for name, value in entries.items()},
            }
        ),
        encoding="utf-8",
    )
    return PreparedRepository(
        RepositoryTarget(1, "acme", "repo", "main", "a" * 40),
        "b" * 40,
        archive,
        manifest,
    )


class _Scm:
    def __init__(self, prepared: PreparedRepository, events: list[str]) -> None:
        self.prepared = prepared
        self.events = events
        self.published = []

    async def prepare(self, target, trusted_state_dir):
        self.events.append("prepared")
        return self.prepared

    async def publish_verified(self, request):
        self.events.append("published")
        self.published.append(request)
        return Delivery(
            7,
            "https://github.com/acme/repo/pull/7",
            f"mewcode/{request.job_id}",
            "c" * 40,
        )

    async def aclose(self):
        self.events.append("scm_closed")


class _Agent:
    def __init__(self, environment: FakeExecutionEnvironment) -> None:
        self.environment = environment
        self.calls = 0

    async def run(self, conversation):
        self.calls += 1
        self.environment.workspace.files["README.md"] = f"change-{self.calls}\n"
        yield LoopComplete(1)


class _Runtime:
    def __init__(
        self,
        environment: FakeExecutionEnvironment,
        redactor: SensitiveValueRedactor,
    ) -> None:
        self.agent = _Agent(environment)
        self.conversation = ConversationManager()
        self.services = {
            "execution_environment": environment,
            "redactor": redactor,
        }
        self.environment = environment

    async def start(self):
        await self.environment.start()

    async def aclose(self):
        await self.environment.aclose()


class _Artifacts:
    def __init__(
        self, environment: FakeExecutionEnvironment, events: list[str]
    ) -> None:
        self.environment = environment
        self.events = events
        self.items: list[tuple[ArtifactKind, bytes]] = []

    async def persist_bytes(self, lease, *, kind, content, content_type):
        assert self.environment.close_count == 1
        self.events.append(f"artifact:{kind.value}")
        self.items.append((kind, content))
        now = datetime.now(UTC)
        return ArtifactMetadata(
            id=uuid4(),
            job_id=lease.job_id,
            attempt_id=lease.attempt_id,
            kind=kind.value,
            storage_key=f"{lease.job_id}/{lease.attempt_id}/{uuid4()}",
            sha256="d" * 64,
            size_bytes=len(content),
            content_type=content_type,
            created_at=now,
            expires_at=now + timedelta(days=7),
        )


def _lease() -> AttemptLease:
    return AttemptLease(
        job_id=UUID("00000000-0000-0000-0000-000000000501"),
        attempt_id=UUID("00000000-0000-0000-0000-000000000502"),
        attempt_no=1,
        tenant_id=UUID(int=3),
        requester_id=UUID(int=4),
        worker_id="worker",
        fencing_token=UUID(int=5),
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        repository_target=RepositoryTarget(1, "acme", "repo", "main", "a" * 40),
        work_request={"title": "Fix", "description": "Make it work"},
        execution_contract={
            "setup_commands": [
                {"name": "setup", "command": "setup", "timeout_seconds": 5}
            ],
            "verification_commands": [
                {"name": "one", "command": "verify-one", "timeout_seconds": 5},
                {"name": "two", "command": "verify-two", "timeout_seconds": 5},
            ],
        },
    )


def _settings(tmp_path: Path, key_file: Path) -> PlatformSettings:
    return PlatformSettings(
        database_url="postgresql://db/platform",
        llm_protocol="anthropic",
        llm_base_url="https://llm.invalid",
        llm_model="scripted",
        llm_api_key_file=str(key_file),
        executor_image="sha256:" + "1" * 64,
        proxy_image="sha256:" + "2" * 64,
        state_root=str(tmp_path / "state"),
        artifact_root=str(tmp_path / "artifacts"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verification", "expected_status", "expected_code", "agent_calls", "commands"),
    [
        ([1, 0, 0, 0], AttemptOutcomeStatus.COMPLETED, None, 2, 5),
        ([1, 0, 1, 0, 0, 0], AttemptOutcomeStatus.COMPLETED, None, 3, 7),
        (
            [1, 0, 1, 0, 1, 0],
            AttemptOutcomeStatus.FAILED,
            "VERIFICATION_FAILED",
            3,
            7,
        ),
    ],
)
async def test_processor_runs_complete_rounds_before_publish(
    tmp_path,
    verification,
    expected_status,
    expected_code,
    agent_calls,
    commands,
) -> None:
    key_file = tmp_path / "llm-key"
    key_file.write_text("phase5-llm-secret", encoding="utf-8")
    events: list[str] = []
    verification_results = iter(verification)

    async def command_handler(command):
        events.append(f"command:{command.command}")
        exit_code = 0 if command.command == "setup" else next(verification_results)
        return ExecutionCommandOutcome(
            CommandExecutionResult(exit_code=exit_code, stdout="ok", stderr="")
        )

    environments: list[FakeExecutionEnvironment] = []

    def environment_factory(spec: AttemptExecutionSpec):
        environment = FakeExecutionEnvironment(spec, command_handler=command_handler)
        environments.append(environment)
        return environment

    runtime_box = {}

    def runtime_factory(options):
        runtime = _Runtime(options.execution_environment, redactor)
        runtime_box["runtime"] = runtime
        return runtime

    prepared = _prepared(tmp_path)
    scm = _Scm(prepared, events)
    redactor = SensitiveValueRedactor(("phase5-llm-secret",))
    artifact_box = {}

    class _DeferredArtifacts:
        async def persist_bytes(self, *args, **kwargs):
            service = artifact_box.setdefault(
                "service", _Artifacts(environments[0], events)
            )
            return await service.persist_bytes(*args, **kwargs)

    processor = ProductionAttemptProcessor(
        _settings(tmp_path, key_file),
        SimpleNamespace(),  # type: ignore[arg-type]
        _DeferredArtifacts(),  # type: ignore[arg-type]
        redactor,
        scm=scm,
        environment_factory=environment_factory,
        runtime_factory=runtime_factory,
    )
    stages: list[AttemptStage] = []

    async def report_stage(stage):
        if stage == AttemptStage.PUBLISHING:
            assert len(artifact_box["service"].items) == 4
            assert environments[0].close_count == 1
        stages.append(stage)

    sink = InMemoryJobEventSink()
    outcome = await processor.process(
        _lease(),
        AttemptControls(sink, report_stage, asyncio.Event()),
    )

    assert outcome.status == expected_status
    assert outcome.error_code == expected_code
    assert runtime_box["runtime"].agent.calls == agent_calls
    assert len([item for item in events if item.startswith("command:")]) == commands
    assert [event.attempt_sequence for event in sink.events] == list(
        range(1, len(sink.events) + 1)
    )
    assert environments[0].close_count == 1
    assert not environments[0].spec.trusted_state_dir.exists()
    assert len(artifact_box["service"].items) == 4
    if expected_status == AttemptOutcomeStatus.COMPLETED:
        assert stages[-1] == AttemptStage.PUBLISHING
        assert len(scm.published) == 1
        assert "Verification report Artifact ID" in (
            scm.published[0].verification_summary
        )
    else:
        assert AttemptStage.PUBLISHING not in stages
        assert not scm.published


@pytest.mark.asyncio
async def test_processor_fatal_verification_does_not_repair_or_publish(
    tmp_path,
) -> None:
    key_file = tmp_path / "llm-key"
    key_file.write_text("phase5-llm-secret", encoding="utf-8")
    events: list[str] = []

    async def command_handler(command):
        if command.command == "setup":
            return ExecutionCommandOutcome(CommandExecutionResult(0, "", ""))
        return ExecutionCommandOutcome(
            CommandExecutionResult(None, "", "executor gone"),
            fatal_error_code="EXECUTOR_LOST",
            fatal_error_message="executor gone",
        )

    environment_box = {}

    def environment_factory(spec):
        environment = FakeExecutionEnvironment(spec, command_handler=command_handler)
        environment_box["value"] = environment
        return environment

    redactor = SensitiveValueRedactor(())
    runtime_box = {}

    def runtime_factory(options):
        runtime = _Runtime(options.execution_environment, redactor)
        runtime_box["value"] = runtime
        return runtime

    class _ArtifactService:
        async def persist_bytes(self, lease, *, kind, content, content_type):
            return await _Artifacts(environment_box["value"], events).persist_bytes(
                lease, kind=kind, content=content, content_type=content_type
            )

    scm = _Scm(_prepared(tmp_path), events)
    processor = ProductionAttemptProcessor(
        _settings(tmp_path, key_file),
        SimpleNamespace(),  # type: ignore[arg-type]
        _ArtifactService(),  # type: ignore[arg-type]
        redactor,
        scm=scm,
        environment_factory=environment_factory,
        runtime_factory=runtime_factory,
    )
    outcome = await processor.process(
        _lease(),
        AttemptControls(
            InMemoryJobEventSink(),
            lambda stage: _completed(),
            asyncio.Event(),
        ),
    )
    assert outcome.status == AttemptOutcomeStatus.FAILED
    assert outcome.error_code == "EXECUTOR_LOST"
    assert runtime_box["value"].agent.calls == 1
    assert not scm.published


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        ("setup", "SETUP_FAILED"),
        ("artifact", "ARTIFACT_PERSIST_FAILED"),
        ("cleanup", "EXECUTOR_CLEANUP_FAILED"),
        ("no_changes", "NO_CHANGES"),
    ],
)
async def test_processor_blocks_publish_on_prerequisite_failure(
    tmp_path, failure, expected_code
) -> None:
    key_file = tmp_path / "llm-key"
    key_file.write_text("phase5-llm-secret", encoding="utf-8")
    events: list[str] = []

    async def command_handler(command):
        exit_code = 1 if failure == "setup" and command.command == "setup" else 0
        return ExecutionCommandOutcome(
            CommandExecutionResult(exit_code, "", "failed" if exit_code else "")
        )

    environment_box = {}

    def environment_factory(spec):
        environment = FakeExecutionEnvironment(
            spec,
            command_handler=command_handler,
            cleanup_error=(
                ExecutionCleanupError("cleanup failed")
                if failure == "cleanup"
                else None
            ),
        )
        environment_box["value"] = environment
        return environment

    redactor = SensitiveValueRedactor(())

    def runtime_factory(options):
        runtime = _Runtime(options.execution_environment, redactor)
        if failure == "no_changes":

            class _StaticAgent:
                async def run(self, conversation):
                    yield LoopComplete(1)

            runtime.agent = _StaticAgent()
        return runtime

    persisted: list[ArtifactKind] = []

    class _ArtifactService:
        async def persist_bytes(self, lease, *, kind, content, content_type):
            if failure == "artifact" and kind == ArtifactKind.DIFF:
                raise OSError("artifact volume unavailable")
            persisted.append(kind)
            return await _Artifacts(environment_box["value"], events).persist_bytes(
                lease, kind=kind, content=content, content_type=content_type
            )

    scm = _Scm(_prepared(tmp_path), events)
    processor = ProductionAttemptProcessor(
        _settings(tmp_path, key_file),
        SimpleNamespace(),  # type: ignore[arg-type]
        _ArtifactService(),  # type: ignore[arg-type]
        redactor,
        scm=scm,
        environment_factory=environment_factory,
        runtime_factory=runtime_factory,
    )
    stages = []

    async def report_stage(stage):
        stages.append(stage)

    outcome = await processor.process(
        _lease(),
        AttemptControls(InMemoryJobEventSink(), report_stage, asyncio.Event()),
    )
    assert outcome.status == AttemptOutcomeStatus.FAILED
    assert outcome.error_code == expected_code
    assert AttemptStage.PUBLISHING not in stages
    assert not scm.published
    assert environment_box["value"].close_count == 1
    assert not environment_box["value"].spec.trusted_state_dir.exists()
    if failure != "artifact":
        assert set(persisted) == set(ArtifactKind)


async def _completed() -> None:
    return None


@pytest.mark.asyncio
async def test_processor_attempt_deadline_is_configurable_and_preserves_artifacts(
    tmp_path: Path,
) -> None:
    key_file = tmp_path / "llm-key"
    key_file.write_text("phase7-llm-secret", encoding="utf-8")
    events: list[str] = []
    environment_box = {}

    def environment_factory(spec):
        environment = FakeExecutionEnvironment(spec)
        environment_box["value"] = environment
        return environment

    class _BlockingAgent:
        async def run(self, conversation):
            await asyncio.Event().wait()
            yield LoopComplete(1)

    def runtime_factory(options):
        runtime = _Runtime(options.execution_environment, redactor)
        runtime.agent = _BlockingAgent()
        return runtime

    class _ArtifactService:
        def __init__(self) -> None:
            self.kinds: list[ArtifactKind] = []
            self.contents: dict[ArtifactKind, bytes] = {}

        async def persist_bytes(self, lease, *, kind, content, content_type):
            self.kinds.append(kind)
            self.contents[kind] = content
            return await _Artifacts(environment_box["value"], events).persist_bytes(
                lease, kind=kind, content=content, content_type=content_type
            )

    artifacts = _ArtifactService()
    redactor = SensitiveValueRedactor(("phase7-llm-secret",))
    settings = replace(
        _settings(tmp_path, key_file), attempt_timeout_seconds=1
    )
    processor = ProductionAttemptProcessor(
        settings,
        SimpleNamespace(),  # type: ignore[arg-type]
        artifacts,  # type: ignore[arg-type]
        redactor,
        scm=_Scm(_prepared(tmp_path), events),
        environment_factory=environment_factory,
        runtime_factory=runtime_factory,
    )
    outcome = await processor.process(
        _lease(),
        AttemptControls(InMemoryJobEventSink(), lambda stage: _completed(), asyncio.Event()),
    )
    assert outcome.status == AttemptOutcomeStatus.FAILED
    assert outcome.error_code == "ATTEMPT_DEADLINE_EXCEEDED"
    assert set(artifacts.kinds) == set(ArtifactKind)
    report = json.loads(artifacts.contents[ArtifactKind.VERIFICATION_REPORT])
    assert report["terminal"]["code"] == "ATTEMPT_DEADLINE_EXCEEDED"
    assert environment_box["value"].close_count == 1
    assert not environment_box["value"].spec.trusted_state_dir.exists()
