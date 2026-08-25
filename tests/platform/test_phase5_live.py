from __future__ import annotations

import asyncio
import os
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import pytest
from alembic import command

from mewcode.agent import LoopComplete, StreamText
from mewcode.conversation import ConversationManager
from mewcode.platform.artifacts import ArtifactService, LocalArtifactStore
from mewcode.platform.cli import _alembic_config
from mewcode.platform.domain import (
    AttemptControls,
    AttemptOutcomeStatus,
    AttemptStage,
    RepositoryTarget,
)
from mewcode.platform.execution import (
    DockerExecutionEnvironment,
    SensitiveValueRedactor,
)
from mewcode.platform.persistence import (
    PlatformRepository,
    PostgresJobEventSink,
    create_database,
)
from mewcode.platform.processing import ProductionAttemptProcessor
from mewcode.platform.scm import GitHubAppClient, GitHubRepositoryTargetResolver
from mewcode.platform.scm.adapter import GitHubScmAdapter
from mewcode.platform.settings import PlatformSettings

pytestmark = [pytest.mark.platform_phase5_live, pytest.mark.asyncio]
_CANARY = "phase5-live-secret-canary"


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise AssertionError(f"{name} is required for the Phase 5 live gate")
    return value


class _ScriptedAgent:
    def __init__(
        self,
        environment: DockerExecutionEnvironment,
        probe_name: str,
        *,
        repair: bool,
    ) -> None:
        self.environment = environment
        self.probe_name = probe_name
        self.repair = repair
        self.calls = 0

    async def run(self, conversation):
        self.calls += 1
        if self.calls == 1:
            await self.environment.workspace.write_file(
                self.probe_name, "Phase 5 scripted Agent change.\n", None
            )
        elif self.repair and self.calls == 2:
            await self.environment.workspace.write_file(
                ".phase5-fixed", "repaired\n", None
            )
        yield StreamText(f"scripted event {_CANARY}")
        yield LoopComplete(1)


class _ScriptedRuntime:
    def __init__(self, options, probe_name: str, repair: bool, redactor) -> None:
        self.environment = options.execution_environment
        self.agent = _ScriptedAgent(self.environment, probe_name, repair=repair)
        self.conversation = ConversationManager()
        self.services = {
            "execution_environment": self.environment,
            "redactor": redactor,
        }

    async def start(self) -> None:
        await self.environment.start()

    async def aclose(self) -> None:
        await self.environment.aclose()


class _IdempotentScm:
    def __init__(self, adapter: GitHubScmAdapter) -> None:
        self.adapter = adapter
        self.request = None
        self.delivery = None

    async def prepare(self, target, trusted_state_dir):
        return await self.adapter.prepare(target, trusted_state_dir)

    async def publish_verified(self, request):
        self.request = request
        first = await self.adapter.publish_verified(request)
        repeated = await self.adapter.publish_verified(request)
        assert repeated == first
        self.delivery = first
        return first

    async def aclose(self) -> None:
        await self.adapter.aclose()


async def test_phase5_real_postgres_docker_and_github_gate(tmp_path: Path) -> None:
    database_url = _required("MEWCODE_TEST_DATABASE_URL")
    client_id = _required("MEWCODE_TEST_GITHUB_APP_CLIENT_ID")
    private_key = _required("MEWCODE_TEST_GITHUB_PRIVATE_KEY_FILE")
    installation_id = int(_required("MEWCODE_TEST_GITHUB_INSTALLATION_ID"))
    owner = _required("MEWCODE_TEST_GITHUB_OWNER")
    repository_name = _required("MEWCODE_TEST_GITHUB_REPOSITORY")
    base_ref = os.environ.get("MEWCODE_TEST_GITHUB_BASE_REF", "main")
    migration_settings = PlatformSettings(database_url=database_url)
    await asyncio.to_thread(
        command.upgrade,
        _alembic_config(migration_settings),
        "head",
    )
    database = create_database(migration_settings)
    repository = PlatformRepository(database)
    cleanup_client = GitHubAppClient(
        client_id=client_id,
        private_key_file=private_key,
        redactor=SensitiveValueRedactor((_CANARY,)),
    )
    resolver = GitHubRepositoryTargetResolver(cleanup_client)
    target = await resolver.resolve(
        installation_id=installation_id,
        owner=owner,
        name=repository_name,
        base_ref=base_ref,
    )
    cleanup_token = await cleanup_client.installation_token(
        installation_id=installation_id,
        repository=repository_name,
        permissions={"contents": "write", "pull_requests": "write"},
    )
    token, principal = await repository.create_api_key(
        tenant_name="phase5-live", requester_name=f"gate-{uuid4()}"
    )
    assert token
    branches: list[str] = []
    deliveries = []
    settings = PlatformSettings(
        database_url=database_url,
        github_app_client_id=client_id,
        github_private_key_file=private_key,
        llm_protocol="anthropic",
        llm_base_url="https://scripted.invalid",
        llm_model="scripted",
        llm_api_key_file=_required("MEWCODE_TEST_LLM_KEY_FILE"),
        executor_image=_required("MEWCODE_EXECUTOR_IMAGE"),
        proxy_image=_required("MEWCODE_PROXY_IMAGE"),
        state_root=str(tmp_path / "state"),
        artifact_root=str(tmp_path / "artifacts"),
        egress_network="mewcode-phase5-live-egress",
    )
    redactor = SensitiveValueRedactor((_CANARY,))
    artifact_service = ArtifactService(
        repository,
        LocalArtifactStore(settings.artifact_root),
        redactor=redactor,
    )

    async def run_scenario(*, repair: bool):
        job_id = uuid4()
        probe_name = f"mewcode-phase5-live-{job_id}.txt"
        work = {
            "kind": "bugfix",
            "title": "MewCode Phase 5 live gate",
            "description": "Exercise Verification repair and evidence publication.",
        }
        execution = {
            "setup_commands": [
                {
                    "name": "repository-ready",
                    "command": 'test -f README.md || test -n "$(find . -type f -print -quit)"',
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
                    "name": "agent-change",
                    "command": f"test -s {probe_name}",
                    "timeout_seconds": 30,
                },
            ],
        }
        job, _ = await repository.create_job(
            principal=principal,
            idempotency_key=f"phase5-live-{job_id}",
            request_hash=job_id.hex.ljust(64, "0"),
            target=RepositoryTarget(
                target.installation_id,
                target.owner,
                target.name,
                target.base_ref,
                target.base_sha,
            ),
            work_request=work,
            execution_contract=execution,
        )
        if repair:
            branches.append(f"mewcode/{job.id}")
        claimed = await repository.claim_attempt(
            worker_id=f"phase5-live-{job_id}",
            lease_seconds=300,
            max_concurrent_jobs=1,
        )
        assert claimed is not None
        lease = claimed.lease
        sink = PostgresJobEventSink(
            repository,
            job_id=lease.job_id,
            attempt_id=lease.attempt_id,
            worker_id=lease.worker_id,
            fencing_token=lease.fencing_token,
            redactor=redactor,
        )

        async def report_stage(stage: AttemptStage) -> None:
            await repository.report_stage(
                attempt_id=lease.attempt_id,
                worker_id=lease.worker_id,
                fencing_token=lease.fencing_token,
                stage=stage,
            )

        scm_client = GitHubAppClient(
            client_id=client_id,
            private_key_file=private_key,
            redactor=redactor,
        )
        scm = _IdempotentScm(GitHubScmAdapter(scm_client))
        processor = ProductionAttemptProcessor(
            settings,
            repository,
            artifact_service,
            redactor,
            scm=scm,
            environment_factory=lambda spec: DockerExecutionEnvironment(
                spec, egress_network_name=settings.egress_network
            ),
            runtime_factory=lambda options: _ScriptedRuntime(
                options, probe_name, repair, redactor
            ),
        )
        outcome = await processor.process(
            lease,
            AttemptControls(
                event_sink=sink,
                report_stage=report_stage,
                cancellation=asyncio.Event(),
            ),
        )
        await repository.finish_attempt(
            attempt_id=lease.attempt_id,
            worker_id=lease.worker_id,
            fencing_token=lease.fencing_token,
            outcome=outcome,
        )
        artifacts = await repository.list_artifacts(principal=principal, job_id=job.id)
        assert {artifact.kind for artifact in artifacts} == {
            "agent_log",
            "command_log",
            "diff",
            "verification_report",
        }
        for artifact in artifacts:
            content = (
                LocalArtifactStore(settings.artifact_root)
                .path_for(artifact.storage_key)
                .read_text(encoding="utf-8", errors="replace")
            )
            assert _CANARY not in content
        events = await repository.list_events(
            principal=principal, job_id=job.id, limit=500
        )
        assert _CANARY not in str([event.payload for event in events])
        branch = f"mewcode/{job.id}"
        if repair:
            assert outcome.status == AttemptOutcomeStatus.COMPLETED
            assert scm.delivery is not None
            deliveries.append(scm.delivery)
            pull = await cleanup_client.request_json(
                "GET",
                f"/repos/{quote(owner, safe='')}/{quote(repository_name, safe='')}/pulls/{scm.delivery.pr_number}",
                token=cleanup_token,
            )
            body = pull["body"]
            assert pull["draft"] is True
            assert "repair-marker" in body and "agent-change" in body
            assert "Repair rounds: 1" in body
            assert f"Job ID: {job.id}" in body
            assert "Verification report Artifact ID:" in body
        else:
            assert outcome.status == AttemptOutcomeStatus.FAILED
            assert outcome.error_code == "VERIFICATION_FAILED"
            assert scm.delivery is None
            repo_path = (
                f"/repos/{quote(owner, safe='')}/{quote(repository_name, safe='')}"
            )
            ref = await cleanup_client.request_json(
                "GET",
                f"{repo_path}/git/ref/heads/{quote(branch, safe='')}",
                token=cleanup_token,
                not_found_ok=True,
            )
            assert ref is None
        return outcome

    try:
        await run_scenario(repair=False)
        await run_scenario(repair=True)
    finally:
        repo_path = f"/repos/{quote(owner, safe='')}/{quote(repository_name, safe='')}"
        for delivery in deliveries:
            await cleanup_client.request_json(
                "PATCH",
                f"{repo_path}/pulls/{delivery.pr_number}",
                token=cleanup_token,
                json_body={"state": "closed"},
            )
        for branch in branches:
            await cleanup_client.request_json(
                "DELETE",
                f"{repo_path}/git/refs/heads/{quote(branch, safe='')}",
                token=cleanup_token,
                not_found_ok=True,
            )
        await database.aclose()
        await cleanup_client.aclose()
