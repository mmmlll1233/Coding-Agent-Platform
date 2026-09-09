from __future__ import annotations

from pathlib import Path

import pytest

from scripts.phase7_gate import GateError, verify_server_evidence
from scripts.phase7_server_gate import (
    GLOBAL_CAPACITY,
    GateObservation,
    ServerGateConfig,
    ServerGateError,
    build_server_evidence,
)


EXECUTOR_IMAGE = "sha256:" + "1" * 64
PROXY_IMAGE = "ubuntu/squid@sha256:" + "2" * 64


def _observation() -> GateObservation:
    return GateObservation(
        maximum_observed_running=GLOBAL_CAPACITY,
        sixth_job_observed_queued=True,
        batches_completed=2,
        completed_jobs=10,
        failed_jobs=0,
    )


def test_server_executor_builds_evidence_accepted_by_the_formal_verifier() -> None:
    evidence = build_server_evidence(
        _observation(), local_slots_total=5, docker_resources_remaining=0
    )
    assert verify_server_evidence(evidence) == {
        "schema_version": 1,
        "result": "PASSED",
        "global_capacity": 5,
        "completed_jobs": 10,
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("maximum_observed_running", 4, "exactly five"),
        ("sixth_job_observed_queued", False, "Sixth Job"),
        ("batches_completed", 1, "two batches"),
        ("completed_jobs", 9, "ten completed"),
        ("failed_jobs", 1, "failed Jobs"),
    ],
)
def test_server_executor_evidence_fails_closed(
    field: str, value: object, message: str
) -> None:
    observation = _observation()
    setattr(observation, field, value)
    evidence = build_server_evidence(
        observation, local_slots_total=5, docker_resources_remaining=0
    )
    with pytest.raises(GateError, match=message):
        verify_server_evidence(evidence)


def test_server_executor_requires_immutable_images(tmp_path: Path) -> None:
    with pytest.raises(ServerGateError, match="immutable"):
        ServerGateConfig(
            executor_image="mewcode-executor:latest",
            proxy_image=PROXY_IMAGE,
            work_root=tmp_path,
        )


def test_server_executor_requires_at_least_five_local_slots(tmp_path: Path) -> None:
    with pytest.raises(ServerGateError, match="at least 5"):
        ServerGateConfig(
            executor_image=EXECUTOR_IMAGE,
            proxy_image=PROXY_IMAGE,
            work_root=tmp_path,
            local_slots=4,
        )


def test_server_executor_accepts_digest_only_images(tmp_path: Path) -> None:
    config = ServerGateConfig(
        executor_image=EXECUTOR_IMAGE,
        proxy_image=PROXY_IMAGE,
        work_root=tmp_path,
    )
    assert config.local_slots == GLOBAL_CAPACITY


def test_server_executor_requires_distinct_evidence_paths(tmp_path: Path) -> None:
    output = tmp_path / "gate.json"
    with pytest.raises(ServerGateError, match="must be different"):
        ServerGateConfig(
            executor_image=EXECUTOR_IMAGE,
            proxy_image=PROXY_IMAGE,
            evidence_path=output,
            summary_path=output,
        )
