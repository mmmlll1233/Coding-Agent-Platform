from __future__ import annotations

import pytest
from pydantic import ValidationError

from mewcode.platform.api.schemas import CreateJobRequest


def _request(**command_overrides):
    command = {"name": "tests", "command": "pytest", "timeout_seconds": 600}
    command.update(command_overrides)
    return {
        "repository": {
            "installation_id": 1,
            "owner": "company",
            "name": "service",
            "base_ref": "main",
        },
        "work": {
            "kind": "bugfix",
            "title": "Fix it",
            "description": "The behavior is broken",
        },
        "execution": {"verification_commands": [command]},
        "attachment_ids": [],
    }


def test_job_request_has_stable_canonical_hash() -> None:
    first = CreateJobRequest.model_validate(_request())
    second = CreateJobRequest.model_validate(_request())
    assert first.canonical_hash() == second.canonical_hash()


def test_verification_is_required_and_command_timeout_is_bounded() -> None:
    missing = _request()
    missing["execution"]["verification_commands"] = []
    with pytest.raises(ValidationError):
        CreateJobRequest.model_validate(missing)
    with pytest.raises(ValidationError):
        CreateJobRequest.model_validate(_request(timeout_seconds=601))


def test_phase3_rejects_attachments() -> None:
    request = _request()
    request["attachment_ids"] = ["00000000-0000-0000-0000-000000000001"]
    with pytest.raises(ValidationError):
        CreateJobRequest.model_validate(request)


@pytest.mark.parametrize(
    "secret",
    [
        "-----BEGIN PRIVATE KEY-----",
        "https://bot:password@example.com/repository.git",
        "github_pat_abcdefghijklmnopqrstuvwxyz123456",
        "sk-ant-abcdefghijklmnopqrstuvwxyz",
    ],
)
def test_job_request_rejects_embedded_credentials(secret: str) -> None:
    request = _request()
    request["work"]["description"] = f"do not persist {secret}"
    with pytest.raises(ValidationError, match="credentials or secrets"):
        CreateJobRequest.model_validate(request)
