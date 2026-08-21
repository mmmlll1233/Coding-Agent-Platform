from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.platform.security_support import assert_canaries_absent, make_test_canaries


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "malicious_repositories"
THREAT_MODEL = PROJECT_ROOT / "docs" / "security" / "coding-agent-threat-model.md"
EXPECTED_CASES = {
    "prompt_injection",
    "secret_canary",
    "workspace_escape",
    "timeout_process_tree",
    "disk_exhaustion",
    "pid_exhaustion",
    "egress_probe",
}
REQUIRED_FIELDS = {
    "case_id",
    "threat_ids",
    "target_phase",
    "command",
    "expected_controls",
    "safety_limits",
}
REQUIRED_LIMITS = {
    "host_guard_env",
    "host_guard_value",
    "max_runtime_seconds",
    "max_output_bytes",
    "max_processes",
    "max_write_bytes",
}


def _scenario_paths() -> list[Path]:
    return sorted(FIXTURE_ROOT.glob("*/scenario.yaml"))


def _load_scenario(path: Path) -> dict:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{path} must contain a mapping"
    return loaded


@pytest.mark.security_contract
def test_fixture_catalog_is_complete_and_unique() -> None:
    scenarios = [_load_scenario(path) for path in _scenario_paths()]
    case_ids = [scenario["case_id"] for scenario in scenarios]

    assert set(case_ids) == EXPECTED_CASES
    assert len(case_ids) == len(set(case_ids))


@pytest.mark.security_contract
@pytest.mark.parametrize("scenario_path", _scenario_paths(), ids=lambda path: path.parent.name)
def test_scenario_manifest_contract(scenario_path: Path) -> None:
    scenario = _load_scenario(scenario_path)
    assert set(scenario) == REQUIRED_FIELDS
    assert scenario["case_id"] == scenario_path.parent.name
    assert scenario["target_phase"] == 2
    assert scenario["threat_ids"] and all(
        re.fullmatch(r"[A-Z]+-\d{3}", threat_id)
        for threat_id in scenario["threat_ids"]
    )
    assert scenario["expected_controls"] and all(
        isinstance(control, str) and control
        for control in scenario["expected_controls"]
    )

    command = scenario["command"]
    assert isinstance(command, list) and len(command) >= 2
    assert command[0] == "python"
    assert (scenario_path.parent / command[1]).is_file()

    limits = scenario["safety_limits"]
    assert set(limits) == REQUIRED_LIMITS
    assert limits["host_guard_env"] == "MEWCODE_SECURITY_FIXTURE"
    assert limits["host_guard_value"] == "executor"
    assert 0 < limits["max_runtime_seconds"] <= 30
    assert 0 < limits["max_output_bytes"] <= 1024 * 1024
    assert 0 < limits["max_processes"] <= 256
    assert 0 <= limits["max_write_bytes"] <= 64 * 1024 * 1024


@pytest.mark.security_contract
def test_every_fixture_threat_is_documented() -> None:
    documented = set(re.findall(r"`([A-Z]+-\d{3})`", THREAT_MODEL.read_text(encoding="utf-8")))
    referenced = {
        threat_id
        for path in _scenario_paths()
        for threat_id in _load_scenario(path)["threat_ids"]
    }

    assert referenced <= documented


@pytest.mark.security_contract
@pytest.mark.parametrize("scenario_path", _scenario_paths(), ids=lambda path: path.parent.name)
def test_fixture_refuses_to_run_on_host(scenario_path: Path) -> None:
    scenario = _load_scenario(scenario_path)
    case_dir = scenario_path.parent
    before = {
        path.relative_to(case_dir): path.stat().st_size
        for path in case_dir.rglob("*")
        if path.is_file()
    }
    env = os.environ.copy()
    env.pop(scenario["safety_limits"]["host_guard_env"], None)
    command = [sys.executable, *scenario["command"][1:]]

    result = subprocess.run(
        command,
        cwd=case_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )

    after = {
        path.relative_to(case_dir): path.stat().st_size
        for path in case_dir.rglob("*")
        if path.is_file()
    }
    assert result.returncode == 64
    assert "refusing to run security fixture outside executor" in result.stdout
    assert after == before


@pytest.mark.security_contract
def test_fixtures_do_not_contain_real_secret_material() -> None:
    suspicious = (
        re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
        re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
        re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
        re.compile(r"open\.feishu\.cn/open-apis/bot/v2/hook/[A-Za-z0-9_-]+"),
    )
    for path in FIXTURE_ROOT.rglob("*"):
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8")
        assert not any(pattern.search(content) for pattern in suspicious), path


@pytest.mark.security_contract
def test_dynamic_canaries_are_unique_and_detectable() -> None:
    first = make_test_canaries()
    second = make_test_canaries()
    assert len(set(first.values())) == 3
    assert set(first.values()).isdisjoint(second.values())
    assert_canaries_absent(first.values(), "safe event", "safe artifact")
    with pytest.raises(AssertionError, match="secret canary leaked"):
        assert_canaries_absent(first.values(), f"tool output: {first['MEWCODE_TEST_LLM_SECRET']}")


@pytest.mark.security_contract
def test_prompt_injection_fixture_contains_disabled_repository_extensions() -> None:
    case_dir = FIXTURE_ROOT / "prompt_injection"
    assert (case_dir / "AGENTS.md").is_file()
    assert (case_dir / "MEWCODE.md").is_file()
    assert (case_dir / ".mewcode" / "config.yaml").is_file()
    assert (case_dir / ".mewcode" / "permissions.yaml").is_file()
    assert (case_dir / ".github" / "workflows" / "exfiltrate.yml").is_file()
