from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import pytest

from mewcode.platform.domain import VerifiedDeliveryRequest
from mewcode.platform.execution import (
    AttemptExecutionSpec,
    DockerExecutionEnvironment,
    ExecutionCommand,
    ExecutionLimits,
    SensitiveValueRedactor,
)
from mewcode.platform.scm import GitHubAppClient, GitHubRepositoryTargetResolver
from mewcode.platform.scm.adapter import GitHubScmAdapter


pytestmark = [pytest.mark.platform_github_live, pytest.mark.asyncio]


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise AssertionError(f"{name} is required for the Phase 4 live gate")
    return value


async def test_github_app_creates_one_idempotent_draft_pr(tmp_path: Path) -> None:
    installation_id = int(_required("MEWCODE_TEST_GITHUB_INSTALLATION_ID"))
    owner = _required("MEWCODE_TEST_GITHUB_OWNER")
    repository = _required("MEWCODE_TEST_GITHUB_REPOSITORY")
    base_ref = os.environ.get("MEWCODE_TEST_GITHUB_BASE_REF", "main")
    client = GitHubAppClient(
        client_id=_required("MEWCODE_TEST_GITHUB_APP_CLIENT_ID"),
        private_key_file=_required("MEWCODE_TEST_GITHUB_PRIVATE_KEY_FILE"),
        redactor=SensitiveValueRedactor(()),
    )
    resolver = GitHubRepositoryTargetResolver(client)
    adapter = GitHubScmAdapter(client)
    execution = None
    target = None
    delivery = None
    cleanup_token = None
    job_id = uuid4()
    branch = f"mewcode/{job_id}"
    try:
        target = await resolver.resolve(
            installation_id=installation_id,
            owner=owner,
            name=repository,
            base_ref=base_ref,
        )
        prepared = await adapter.prepare(target, tmp_path / "trusted")
        execution = DockerExecutionEnvironment(
            AttemptExecutionSpec(
                job_id=f"github-live-{job_id}",
                attempt_id=f"attempt-{job_id}",
                executor_image=_required("MEWCODE_EXECUTOR_IMAGE"),
                proxy_image=_required("MEWCODE_PROXY_IMAGE"),
                trusted_state_dir=tmp_path / "executor-state",
                limits=ExecutionLimits(
                    cpus=1,
                    memory_bytes=512 * 1024 * 1024,
                    pids_limit=64,
                    workspace_bytes=3 * 1024**3,
                    tmp_bytes=64 * 1024 * 1024,
                    workspace_inodes=300_000,
                    command_timeout_seconds=30,
                    attempt_timeout_seconds=180,
                    max_output_bytes=256 * 1024,
                ),
            ),
            egress_network_name="mewcode-phase4-live-egress",
        )
        await execution.start()
        await execution.import_archive_file(prepared.archive_path)
        probe_name = f"mewcode-phase4-live-{job_id}.txt"
        await execution.workspace.write_file(
            probe_name,
            f"Phase 4 live Delivery for Job {job_id}\n",
            None,
        )
        verified = await execution.run_command(
            ExecutionCommand(f"test -s {probe_name}", timeout_seconds=10)
        )
        assert verified.command_result.exit_code == 0
        workspace = tmp_path / "workspace.tar"
        await execution.export_archive_file(workspace)
        request = VerifiedDeliveryRequest(
            job_id=job_id,
            prepared=prepared,
            workspace_archive_path=workspace,
            work_title="MewCode Phase 4 live gate",
            work_summary="Validate the trusted GitHub SCM boundary.",
            change_summary="Add a unique live-gate probe file.",
            verification_summary="Phase 4 controlled SCM fixture passed.",
            risks=("The Draft PR is closed and its branch is deleted in cleanup.",),
        )
        cleanup_token = await client.installation_token(
            installation_id=installation_id,
            repository=target.name,
            permissions={"contents": "write", "pull_requests": "write"},
        )
        delivery = await adapter.publish_verified(request)
        repeated = await adapter.publish_verified(request)
        assert repeated == delivery
        assert delivery.head_branch == branch
        commit = await client.request_json(
            "GET",
            f"/repos/{quote(target.owner, safe='')}/{quote(target.name, safe='')}/git/commits/{delivery.head_sha}",
            token=cleanup_token,
        )
        assert len(commit["parents"]) == 1
        assert commit["parents"][0]["sha"] == target.base_sha
        pull = await client.request_json(
            "GET",
            f"/repos/{quote(target.owner, safe='')}/{quote(target.name, safe='')}/pulls/{delivery.pr_number}",
            token=cleanup_token,
        )
        assert pull["state"] == "open"
        assert pull["draft"] is True
        assert pull["head"]["sha"] == delivery.head_sha
        assert pull["head"]["ref"] == branch
    finally:
        try:
            if execution is not None:
                await execution.aclose()
        finally:
            try:
                if target is not None and cleanup_token is not None:
                    repo_path = (
                        f"/repos/{quote(target.owner, safe='')}/"
                        f"{quote(target.name, safe='')}"
                    )
                    if delivery is not None:
                        await client.request_json(
                            "PATCH",
                            f"{repo_path}/pulls/{delivery.pr_number}",
                            token=cleanup_token,
                            json_body={"state": "closed"},
                        )
                    await client.request_json(
                        "DELETE",
                        f"{repo_path}/git/refs/heads/{quote(branch, safe='')}",
                        token=cleanup_token,
                        not_found_ok=True,
                    )
            finally:
                await adapter.aclose()
