from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence


GIB = 1024**3
SCHEMA_VERSION = 1
SHA = re.compile(r"^[0-9a-f]{40}$")
FORBIDDEN_KEY_PARTS = (
    "api_key",
    "password",
    "private_key",
    "secret",
    "token",
    "webhook",
    "raw_log",
    "request_body",
    "response_body",
)
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_.-]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b"),
)


class GateError(RuntimeError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise GateError(message)


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GateError("Phase 7 evidence is missing or invalid JSON") from error
    _require(isinstance(value, dict), "Phase 7 evidence must be a JSON object")
    return value


def _reject_sensitive(value: Any, path: str = "evidence") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower()
            if any(part in normalized for part in FORBIDDEN_KEY_PARTS):
                raise GateError(f"Sensitive evidence field is forbidden: {path}.{key}")
            _reject_sensitive(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_sensitive(item, f"{path}[{index}]")
    elif isinstance(value, str):
        for pattern in SECRET_PATTERNS:
            if pattern.search(value):
                raise GateError(f"Secret-like value found in {path}")


def _exact_keys(value: dict[str, Any], expected: set[str], name: str) -> None:
    _require(set(value) == expected, f"{name} evidence fields are not schema compliant")


def _job_summary(value: Any) -> list[dict[str, Any]]:
    _require(isinstance(value, list) and len(value) == 20, "Exactly 20 Jobs are required")
    result: list[dict[str, Any]] = []
    job_ids: set[str] = set()
    pull_requests: set[str] = set()
    kinds: list[str] = []
    for item in value:
        _require(isinstance(item, dict), "Every Job evidence item must be an object")
        required = {
            "job_id",
            "agent_kind",
            "status",
            "base_sha",
            "verification_succeeded",
            "pr_identity",
            "draft_pr_count",
            "pr_closed",
            "branch_deleted",
            "idempotent_submission_count",
            "cleanup_succeeded",
        }
        _exact_keys(item, required, "Job")
        job_id = str(item["job_id"])
        pr_identity = str(item["pr_identity"])
        kind = str(item["agent_kind"])
        _require(job_id not in job_ids, "Duplicate Job ID in evidence")
        _require(pr_identity not in pull_requests, "Duplicate Pull Request in evidence")
        _require(kind in {"deterministic", "real"}, "Unknown Agent kind")
        _require(item["status"] == "SUCCEEDED", "All 20 pilot Jobs must succeed")
        _require(SHA.fullmatch(str(item["base_sha"])) is not None, "Invalid base SHA")
        _require(item["verification_succeeded"] is True, "Verification must succeed")
        _require(item["draft_pr_count"] == 1, "Every Job must create exactly one Draft PR")
        _require(item["pr_closed"] is True, "Every test Pull Request must be closed")
        _require(item["branch_deleted"] is True, "Every test branch must be deleted")
        _require(
            isinstance(item["idempotent_submission_count"], int)
            and item["idempotent_submission_count"] >= 2,
            "Every Job must include an idempotent API replay",
        )
        _require(item["cleanup_succeeded"] is True, "Job cleanup must succeed")
        job_ids.add(job_id)
        pull_requests.add(pr_identity)
        kinds.append(kind)
        result.append(
            {
                "job_id": job_id,
                "agent_kind": kind,
                "base_sha": item["base_sha"],
                "pr_identity_sha256": hashlib.sha256(pr_identity.encode()).hexdigest(),
            }
        )
    _require(kinds.count("deterministic") == 18, "18 deterministic Jobs are required")
    _require(kinds.count("real") == 2, "2 real-model Jobs are required")
    return result


def verify_local_evidence(value: dict[str, Any]) -> dict[str, Any]:
    _reject_sensitive(value)
    _exact_keys(
        value,
        {
            "schema_version",
            "implementation_sha",
            "ci_run_id",
            "jobs",
            "database_job_count",
            "faults",
            "notifications",
            "repository_capacity",
            "attempt_deadline",
            "permanent_verification_failure",
            "backup_restore",
            "security",
            "server_gate_status",
        },
        "Local gate",
    )
    _require(value.get("schema_version") == SCHEMA_VERSION, "Unsupported evidence schema")
    implementation_sha = str(value.get("implementation_sha", ""))
    _require(SHA.fullmatch(implementation_sha) is not None, "Invalid implementation SHA")
    ci_run_id = str(value.get("ci_run_id", ""))
    _require(ci_run_id.isdecimal() and int(ci_run_id) > 0, "Invalid regular CI run ID")
    jobs = _job_summary(value.get("jobs"))
    _require(value.get("database_job_count") == 20, "Database must contain exactly 20 pilot Jobs")

    faults = value.get("faults")
    _require(isinstance(faults, dict), "Fault evidence is missing")
    _exact_keys(
        faults,
        {
            "api_restart",
            "worker_execution_kill",
            "post_publish_kill",
            "notification_outage",
        },
        "Fault",
    )
    for name in (
        "api_restart",
        "worker_execution_kill",
        "post_publish_kill",
        "notification_outage",
    ):
        _require(faults.get(name) == "RECOVERED", f"Fault did not recover: {name}")

    notifications = value.get("notifications")
    _require(isinstance(notifications, dict), "Notification evidence is missing")
    _exact_keys(
        notifications,
        {
            "recorded_deliveries",
            "normal_duplicates",
            "real_feishu_deliveries",
            "outbox_pending",
        },
        "Notification",
    )
    _require(notifications.get("recorded_deliveries") == 18, "Recorder must deliver 18")
    _require(notifications.get("normal_duplicates") == 0, "Normal notifications duplicated")
    _require(
        notifications.get("real_feishu_deliveries") == 2,
        "Two live Feishu deliveries are required",
    )
    _require(notifications.get("outbox_pending") == 0, "Notification Outbox is not drained")

    repository = value.get("repository_capacity")
    _require(isinstance(repository, dict), "Repository capacity evidence is missing")
    _exact_keys(
        repository,
        {
            "accepted_bytes",
            "rejected_bytes",
            "cleanup_succeeded",
            "temporary_archives_remaining",
            "docker_resources_remaining",
        },
        "Repository capacity",
    )
    _require(repository.get("accepted_bytes") == 2 * GIB, "2 GiB repository was not accepted")
    _require(repository.get("rejected_bytes") == 2 * GIB + 1, "2 GiB + 1 was not rejected")
    _require(repository.get("cleanup_succeeded") is True, "Repository gate cleanup failed")
    _require(
        repository.get("temporary_archives_remaining") == 0,
        "Repository temporary archives remain",
    )
    _require(
        repository.get("docker_resources_remaining") == 0,
        "Repository Docker resources remain",
    )

    deadline = value.get("attempt_deadline")
    _require(isinstance(deadline, dict), "Attempt deadline evidence is missing")
    _exact_keys(
        deadline,
        {
            "configured_seconds",
            "untrusted_stopped_elapsed_seconds",
            "terminal_elapsed_seconds",
            "error_code",
            "cleanup_succeeded",
        },
        "Attempt deadline",
    )
    _require(deadline.get("configured_seconds") == 3600, "Deadline was not 3600 seconds")
    untrusted_elapsed = deadline.get("untrusted_stopped_elapsed_seconds")
    _require(
        isinstance(untrusted_elapsed, (int, float)) and 3600 <= untrusted_elapsed <= 3601,
        "Untrusted execution did not stop at the Attempt deadline",
    )
    elapsed = deadline.get("terminal_elapsed_seconds")
    _require(
        isinstance(elapsed, (int, float)) and 3600 <= elapsed <= 3630,
        "Deadline timing is outside tolerance",
    )
    _require(
        deadline.get("error_code") == "ATTEMPT_DEADLINE_EXCEEDED",
        "Deadline error code is wrong",
    )
    _require(deadline.get("cleanup_succeeded") is True, "Deadline cleanup failed")

    failure_artifacts = value.get("permanent_verification_failure")
    _require(isinstance(failure_artifacts, dict), "Permanent failure evidence is missing")
    _exact_keys(
        failure_artifacts,
        {"status", "downloadable", "artifact_kinds"},
        "Permanent Verification failure",
    )
    _require(
        failure_artifacts.get("status") == "FAILED",
        "Permanent Verification failure did not fail",
    )
    _require(
        failure_artifacts.get("downloadable") is True,
        "Failure Artifacts are not downloadable",
    )
    _require(
        set(failure_artifacts.get("artifact_kinds", []))
        == {"agent_log", "command_log", "diff", "verification_report"},
        "Permanent failure did not preserve all Artifact kinds",
    )

    backup = value.get("backup_restore")
    _require(isinstance(backup, dict), "Backup/restore evidence is missing")
    _exact_keys(
        backup,
        {
            "verified",
            "restored_counts_match",
            "artifact_hashes_match",
            "readiness_succeeded",
            "lease_recovery_verified",
            "empty_project_restore",
            "manifest_sha256",
        },
        "Backup/restore",
    )
    _require(backup.get("verified") is True, "Backup bundle was not verified")
    _require(backup.get("restored_counts_match") is True, "Restore counts differ")
    _require(backup.get("artifact_hashes_match") is True, "Restored Artifact hashes differ")
    _require(backup.get("readiness_succeeded") is True, "Restored platform is not ready")
    _require(backup.get("lease_recovery_verified") is True, "Lease recovery was not verified")
    _require(
        backup.get("empty_project_restore") is True,
        "Restore did not use an empty Compose project",
    )
    manifest_sha = str(backup.get("manifest_sha256", ""))
    _require(
        re.fullmatch(r"[0-9a-f]{64}", manifest_sha) is not None,
        "Invalid backup manifest hash",
    )

    security = value.get("security")
    _require(isinstance(security, dict), "Security evidence is missing")
    _exact_keys(
        security,
        {
            "checklist_passed",
            "credential_leaks",
            "docker_resources_remaining",
            "scanned_surfaces",
            "cleanup_scenarios",
        },
        "Security",
    )
    _require(security.get("checklist_passed") is True, "Security checklist failed")
    _require(security.get("credential_leaks") == 0, "Credential leak detected")
    _require(security.get("docker_resources_remaining") == 0, "Docker resources remain")
    _require(
        set(security.get("scanned_surfaces", []))
        == {"database", "artifacts", "logs", "pull_requests", "notifications"},
        "Security scan did not cover every evidence surface",
    )
    cleanup_scenarios = security.get("cleanup_scenarios")
    _require(
        isinstance(cleanup_scenarios, dict)
        and cleanup_scenarios
        and all(value == 0 for value in cleanup_scenarios.values()),
        "A fault scenario left Docker or temporary resources",
    )
    _require(
        value.get("server_gate_status") == "PENDING",
        "Server gate must remain explicitly PENDING",
    )

    canonical_input = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    source_hash = hashlib.sha256(canonical_input).hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "gate_id": (
            f"phase7-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-"
            f"{source_hash[:12]}"
        ),
        "implementation_sha": implementation_sha,
        "ci_run_id": ci_run_id,
        "result": "PASSED",
        "pilot_jobs": jobs,
        "pilot_job_count": 20,
        "deterministic_job_count": 18,
        "real_model_job_count": 2,
        "real_feishu_delivery_count": 2,
        "repository_accepted_bytes": 2 * GIB,
        "repository_rejected_bytes": 2 * GIB + 1,
        "deadline_seconds": 3600,
        "backup_manifest_sha256": manifest_sha,
        "security_checklist": "PASSED",
        "server_gate_status": "PENDING",
        "source_evidence_sha256": source_hash,
    }


def verify_server_evidence(value: dict[str, Any]) -> dict[str, Any]:
    _reject_sensitive(value)
    _exact_keys(
        value,
        {
            "schema_version",
            "global_capacity",
            "local_slots_total",
            "maximum_observed_running",
            "sixth_job_observed_queued",
            "batches_completed",
            "deterministic_jobs",
            "completed_jobs",
            "failed_jobs",
            "docker_resources_remaining",
        },
        "Server gate",
    )
    _require(value.get("schema_version") == SCHEMA_VERSION, "Unsupported evidence schema")
    _require(value.get("global_capacity") == 5, "Server global capacity must be 5")
    _require(value.get("local_slots_total", 0) >= 5, "Server Worker slots must total at least 5")
    _require(
        value.get("maximum_observed_running") == 5,
        "Server did not hold exactly five concurrent Attempts",
    )
    _require(value.get("sixth_job_observed_queued") is True, "Sixth Job was not observed QUEUED")
    _require(value.get("batches_completed") == 2, "Server gate requires two batches")
    _require(value.get("deterministic_jobs") == 10, "Server gate requires ten deterministic Jobs")
    _require(value.get("completed_jobs") == 10, "Server gate requires ten completed Jobs")
    _require(value.get("failed_jobs") == 0, "Server gate contains failed Jobs")
    _require(value.get("docker_resources_remaining") == 0, "Server Docker resources remain")
    return {
        "schema_version": SCHEMA_VERSION,
        "result": "PASSED",
        "global_capacity": 5,
        "completed_jobs": 10,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate sanitized Phase 7 evidence")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("verify-local", "verify-server"):
        command = commands.add_parser(name)
        command.add_argument("input", type=Path)
        command.add_argument("output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        value = _read(args.input)
        summary = (
            verify_local_evidence(value)
            if args.command == "verify-local"
            else verify_server_evidence(value)
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0
    except GateError as error:
        print(f"phase7-gate: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
