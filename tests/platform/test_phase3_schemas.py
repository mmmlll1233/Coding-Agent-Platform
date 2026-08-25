from __future__ import annotations

from dataclasses import replace

import pytest
from pydantic import ValidationError

from mewcode.platform.api.schemas import CreateJobRequest
from mewcode.platform.settings import PlatformSettings, PlatformSettingsError


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


def test_phase5_command_group_timeout_budgets_are_bounded() -> None:
    request = _request(timeout_seconds=301)
    request["execution"]["verification_commands"].append(
        {"name": "lint", "command": "ruff check", "timeout_seconds": 300}
    )
    with pytest.raises(ValidationError, match="budget must not exceed 600"):
        CreateJobRequest.model_validate(request)

    request = _request(timeout_seconds=1)
    request["execution"]["setup_commands"] = [
        {"name": "one", "command": "one", "timeout_seconds": 400},
        {"name": "two", "command": "two", "timeout_seconds": 201},
    ]
    with pytest.raises(ValidationError, match="budget must not exceed 600"):
        CreateJobRequest.model_validate(request)


def test_phase5_worker_configuration_is_fail_closed(tmp_path) -> None:
    llm_key = tmp_path / "llm-key"
    github_key = tmp_path / "github-key"
    llm_key.write_text("test-llm-key", encoding="utf-8")
    github_key.write_text("test-github-private-key", encoding="utf-8")
    settings = PlatformSettings(
        database_url="postgresql://db/platform",
        llm_protocol="anthropic",
        llm_base_url="https://llm.invalid",
        llm_model="model",
        llm_api_key_file=str(llm_key),
        executor_image="sha256:" + "1" * 64,
        proxy_image="sha256:" + "2" * 64,
        github_app_client_id="client-id",
        github_private_key_file=str(github_key),
        state_root=str(tmp_path / "state"),
        artifact_root=str(tmp_path / "artifacts"),
    )
    settings.validate_worker()
    assert settings.attempt_processor_factory == (
        "mewcode.platform.processing:create_attempt_processor_factory"
    )
    with pytest.raises(PlatformSettingsError, match="immutable sha256"):
        replace(settings, executor_image="mewcode-executor:latest").validate_worker()
    with pytest.raises(PlatformSettingsError, match="must not exceed 2"):
        replace(settings, max_repair_rounds=3).validate_worker()


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
