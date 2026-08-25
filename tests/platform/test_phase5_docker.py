from __future__ import annotations

import asyncio
import io
import json
import os
import tarfile
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from mewcode.agent import LoopComplete, StreamText
from mewcode.conversation import ConversationManager
from mewcode.platform.artifacts import ArtifactKind
from mewcode.platform.domain import (
    AttemptControls,
    AttemptLease,
    AttemptOutcomeStatus,
    Delivery,
    PreparedRepository,
    RepositoryTarget,
)
from mewcode.platform.execution import (
    DockerExecutionEnvironment,
    SensitiveValueRedactor,
)
from mewcode.platform.persistence import ArtifactMetadata
from mewcode.platform.processing import ProductionAttemptProcessor
from mewcode.platform.runtime import InMemoryJobEventSink
from mewcode.platform.scm.archive import scan_workspace_archive
from mewcode.platform.settings import PlatformSettings

pytestmark = [pytest.mark.executor_security, pytest.mark.asyncio]
_CANARY = "phase5-docker-secret-canary"


def _required_image(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.skip(f"PHASE5-CAPABILITY-{name}: immutable image is not configured")
    return value


def _prepared(tmp_path: Path) -> PreparedRepository:
    archive_path = tmp_path / "prepared.tar"
    manifest_path = tmp_path / "prepared-manifest.json"
    with tarfile.open(archive_path, mode="w") as archive:
        data = b"base\n"
        member = tarfile.TarInfo("README.md")
        member.size = len(data)
        member.mode = 0o644
        archive.addfile(member, io.BytesIO(data))
    entries = scan_workspace_archive(archive_path)
    manifest_path.write_text(
        json.dumps(
            {
                "base_sha": "a" * 40,
                "base_tree_sha": "b" * 40,
                "files": {name: asdict(item) for name, item in entries.items()},
            }
        ),
        encoding="utf-8",
    )
    return PreparedRepository(
        RepositoryTarget(1, "acme", "repo", "main", "a" * 40),
        "b" * 40,
        archive_path,
        manifest_path,
    )


class _Scm:
    def __init__(self, prepared: PreparedRepository) -> None:
        self.prepared = prepared
        self.published = []

    async def prepare(self, target, trusted_state_dir):
        return self.prepared

    async def publish_verified(self, request):
        self.published.append(request)
        return Delivery(
            5,
            "https://github.com/acme/repo/pull/5",
            f"mewcode/{request.job_id}",
            "c" * 40,
        )

    async def aclose(self):
        return None


class _Agent:
    def __init__(self, environment: DockerExecutionEnvironment) -> None:
        self.environment = environment
        self.calls = 0

    async def run(self, conversation):
        self.calls += 1
        if self.calls == 1:
            await self.environment.workspace.write_file(
                "result.txt", "initial change\n", None
            )
        elif self.calls == 2:
            current = await self.environment.workspace.read_file("result.txt")
            await self.environment.workspace.write_file(
                "result.txt", "repair one\n", current.version
            )
        elif self.calls == 3:
            await self.environment.workspace.write_file(
                ".phase5-fixed", "repair two\n", None
            )
        yield StreamText(f"scripted {_CANARY}")
        yield LoopComplete(1)


class _Runtime:
    def __init__(self, options, redactor) -> None:
        self.environment = options.execution_environment
        self.agent = _Agent(self.environment)
        self.conversation = ConversationManager()
        self.services = {
            "execution_environment": self.environment,
            "redactor": redactor,
        }

    async def start(self):
        await self.environment.start()

    async def aclose(self):
        await self.environment.aclose()


class _Artifacts:
    def __init__(self, environment: DockerExecutionEnvironment) -> None:
        self.environment = environment
        self.items: dict[ArtifactKind, bytes] = {}

    async def persist_bytes(self, lease, *, kind, content, content_type):
        assert self.environment.state.value == "CLOSED"
        self.items[kind] = content
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


async def test_phase5_three_rounds_use_one_real_executor_and_leave_no_resources(
    tmp_path: Path,
) -> None:
    job_id = uuid4()
    attempt_id = uuid4()
    key_file = tmp_path / "llm-key"
    key_file.write_text(_CANARY, encoding="utf-8")
    settings = PlatformSettings(
        database_url="postgresql://db/platform",
        llm_protocol="anthropic",
        llm_base_url="https://scripted.invalid",
        llm_model="scripted",
        llm_api_key_file=str(key_file),
        executor_image=_required_image("MEWCODE_EXECUTOR_IMAGE"),
        proxy_image=_required_image("MEWCODE_PROXY_IMAGE"),
        state_root=str(tmp_path / "state"),
        artifact_root=str(tmp_path / "artifacts"),
        egress_network=f"mewcode-phase5-docker-{job_id.hex[:12]}",
    )
    lease = AttemptLease(
        job_id=job_id,
        attempt_id=attempt_id,
        attempt_no=1,
        tenant_id=UUID(int=1),
        requester_id=UUID(int=2),
        worker_id="phase5-docker-worker",
        fencing_token=uuid4(),
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        repository_target=RepositoryTarget(1, "acme", "repo", "main", "a" * 40),
        work_request={"title": "Docker gate", "description": "Repair twice"},
        execution_contract={
            "setup_commands": [
                {
                    "name": "setup",
                    "command": "test -s README.md",
                    "timeout_seconds": 30,
                }
            ],
            "verification_commands": [
                {
                    "name": "repair-marker",
                    "command": (
                        "test -f .phase5-fixed || "
                        f"{{ printf '%s' '{_CANARY}'; exit 1; }}"
                    ),
                    "timeout_seconds": 30,
                },
                {
                    "name": "agent-output",
                    "command": "test -s result.txt",
                    "timeout_seconds": 30,
                },
            ],
        },
    )
    environment_box = {}

    def environment_factory(spec):
        environment = DockerExecutionEnvironment(
            spec, egress_network_name=settings.egress_network
        )
        environment_box["value"] = environment
        return environment

    redactor = SensitiveValueRedactor((_CANARY,))
    runtime_box = {}

    def runtime_factory(options):
        runtime = _Runtime(options, redactor)
        runtime_box["value"] = runtime
        return runtime

    artifact_box = {}

    class _DeferredArtifacts:
        async def persist_bytes(self, *args, **kwargs):
            artifacts = artifact_box.setdefault(
                "value", _Artifacts(environment_box["value"])
            )
            return await artifacts.persist_bytes(*args, **kwargs)

    scm = _Scm(_prepared(tmp_path))
    sink = InMemoryJobEventSink()
    processor = ProductionAttemptProcessor(
        settings,
        SimpleNamespace(),  # type: ignore[arg-type]
        _DeferredArtifacts(),  # type: ignore[arg-type]
        redactor,
        scm=scm,
        environment_factory=environment_factory,
        runtime_factory=runtime_factory,
    )
    outcome = await processor.process(
        lease,
        AttemptControls(
            sink,
            lambda stage: _completed(),
            asyncio.Event(),
        ),
    )
    assert outcome.status == AttemptOutcomeStatus.COMPLETED
    assert runtime_box["value"].agent.calls == 3
    assert len(scm.published) == 1
    assert set(artifact_box["value"].items) == set(ArtifactKind)
    command_lines = [
        json.loads(line)
        for line in artifact_box["value"].items[ArtifactKind.COMMAND_LOG].splitlines()
    ]
    assert len([item for item in command_lines if item["phase"] == "verification"]) == 6
    combined = b"".join(artifact_box["value"].items.values())
    assert _CANARY.encode() not in combined
    assert not environment_box["value"].spec.trusted_state_dir.exists()
    assert [event.attempt_sequence for event in sink.events] == list(
        range(1, len(sink.events) + 1)
    )


async def _completed() -> None:
    return None
