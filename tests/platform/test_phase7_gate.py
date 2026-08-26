from __future__ import annotations

from copy import deepcopy

import pytest

from scripts.phase7_capacity_gate import run_repository_gate
from scripts.phase7_gate import GIB, GateError, verify_local_evidence, verify_server_evidence


def _local_evidence() -> dict:
    jobs = [
        {
            "job_id": f"job-{index:02d}",
            "agent_kind": "deterministic" if index < 18 else "real",
            "status": "SUCCEEDED",
            "base_sha": "a" * 40,
            "verification_succeeded": True,
            "pr_identity": f"acme/test#{index + 1}",
            "draft_pr_count": 1,
            "pr_closed": True,
            "branch_deleted": True,
            "idempotent_submission_count": 2,
            "cleanup_succeeded": True,
        }
        for index in range(20)
    ]
    return {
        "schema_version": 1,
        "implementation_sha": "b" * 40,
        "ci_run_id": "32951183849",
        "jobs": jobs,
        "database_job_count": 20,
        "faults": {
            "api_restart": "RECOVERED",
            "worker_execution_kill": "RECOVERED",
            "post_publish_kill": "RECOVERED",
            "notification_outage": "RECOVERED",
        },
        "notifications": {
            "recorded_deliveries": 18,
            "normal_duplicates": 0,
            "real_feishu_deliveries": 2,
            "outbox_pending": 0,
        },
        "repository_capacity": {
            "accepted_bytes": 2 * GIB,
            "rejected_bytes": 2 * GIB + 1,
            "cleanup_succeeded": True,
            "temporary_archives_remaining": 0,
            "docker_resources_remaining": 0,
        },
        "attempt_deadline": {
            "configured_seconds": 3600,
            "untrusted_stopped_elapsed_seconds": 3600.2,
            "terminal_elapsed_seconds": 3605,
            "error_code": "ATTEMPT_DEADLINE_EXCEEDED",
            "cleanup_succeeded": True,
        },
        "permanent_verification_failure": {
            "status": "FAILED",
            "downloadable": True,
            "artifact_kinds": [
                "agent_log",
                "command_log",
                "diff",
                "verification_report",
            ],
        },
        "backup_restore": {
            "verified": True,
            "restored_counts_match": True,
            "artifact_hashes_match": True,
            "readiness_succeeded": True,
            "lease_recovery_verified": True,
            "empty_project_restore": True,
            "manifest_sha256": "c" * 64,
        },
        "security": {
            "checklist_passed": True,
            "credential_leaks": 0,
            "docker_resources_remaining": 0,
            "scanned_surfaces": [
                "database",
                "artifacts",
                "logs",
                "pull_requests",
                "notifications",
            ],
            "cleanup_scenarios": {
                "api_restart": 0,
                "worker_execution_kill": 0,
                "post_publish_kill": 0,
                "notification_outage": 0,
                "repository_capacity": 0,
                "attempt_deadline": 0,
            },
        },
        "server_gate_status": "PENDING",
    }


def test_local_gate_requires_complete_sanitized_evidence() -> None:
    summary = verify_local_evidence(_local_evidence())
    assert summary["result"] == "PASSED"
    assert summary["pilot_job_count"] == 20
    assert summary["server_gate_status"] == "PENDING"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value["jobs"].pop(), "Exactly 20"),
        (
            lambda value: value["jobs"][0].update({"base_sha": "main"}),
            "Invalid base SHA",
        ),
        (
            lambda value: value["security"].update({"credential_leaks": 1}),
            "Credential leak",
        ),
        (
            lambda value: value.update({"api_key": "ghp_" + "x" * 30}),
            "Sensitive evidence field",
        ),
        (
            lambda value: value.update({"debug_note": "unexpected"}),
            "fields are not schema compliant",
        ),
    ],
)
def test_local_gate_fails_closed(mutation, message: str) -> None:
    evidence = deepcopy(_local_evidence())
    mutation(evidence)
    with pytest.raises(GateError, match=message):
        verify_local_evidence(evidence)


def test_server_gate_requires_five_running_and_queued_sixth() -> None:
    summary = verify_server_evidence(
        {
            "schema_version": 1,
            "global_capacity": 5,
            "local_slots_total": 5,
            "maximum_observed_running": 5,
            "sixth_job_observed_queued": True,
            "batches_completed": 2,
            "deterministic_jobs": 10,
            "completed_jobs": 10,
            "failed_jobs": 0,
            "docker_resources_remaining": 0,
        }
    )
    assert summary == {
        "schema_version": 1,
        "result": "PASSED",
        "global_capacity": 5,
        "completed_jobs": 10,
    }


def test_repository_capacity_gate_uses_real_path_with_scaled_bytes(tmp_path) -> None:
    evidence = run_repository_gate(
        tmp_path, content_bytes=64, required_free_bytes=0
    )
    assert evidence["accepted_bytes"] == 64
    assert evidence["rejected_bytes"] == 65
    assert evidence["cleanup_succeeded"] is True
