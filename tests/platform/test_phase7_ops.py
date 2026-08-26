from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts.platform_ops import (
    ACTIVE_QUERY,
    ARTIFACT_EMPTY_SCRIPT,
    ARTIFACT_MANIFEST_SCRIPT,
    ARTIFACT_QUERY,
    COUNT_QUERY,
    FRESH_WORKER_QUERY,
    REVISION_QUERY,
    ComposeTarget,
    OperationError,
    backup,
    restore,
    verify_bundle,
)


def test_fresh_worker_query_uses_physical_metadata_column() -> None:
    assert "metadata->>'draining'" in FRESH_WORKER_QUERY
    assert "metadata_json" not in FRESH_WORKER_QUERY


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _bundle(path: Path) -> Path:
    path.mkdir()
    files = {"database.dump": b"database", "artifacts.tar.gz": b"artifacts"}
    for name, content in files.items():
        (path / name).write_bytes(content)
    manifest = {
        "format_version": 1,
        "created_at": "2026-08-26T00:00:00+00:00",
        "implementation_sha": "a" * 40,
        "alembic_revision": "0004",
        "counts": {"jobs": 2, "events": 4, "artifacts": 8, "outbox": 2},
        "files": {
            name: {"size_bytes": len(content), "sha256": _digest(content)}
            for name, content in files.items()
        },
    }
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_backup_bundle_verification_detects_tampering(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path / "bundle")
    assert verify_bundle(bundle)["counts"]["artifacts"] == 8
    (bundle / "database.dump").write_bytes(b"tampered")
    with pytest.raises(OperationError, match="checksum mismatch"):
        verify_bundle(bundle)


class _FakeRunner:
    def __init__(self, *, active: int = 0) -> None:
        self.active = active
        self.commands: list[list[str]] = []

    def run(self, command, *, stdin=None, stdout=None, capture=False):
        command = list(command)
        self.commands.append(command)
        data = b""
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            data = ("b" * 40).encode()
        elif command[-1] == ACTIVE_QUERY:
            data = str(self.active).encode()
        elif command[-1] == COUNT_QUERY:
            data = json.dumps(
                {"jobs": 1, "events": 2, "artifacts": 4, "outbox": 1}
            ).encode()
        elif command[-1] == REVISION_QUERY:
            data = b"0004_phase6"
        elif "pg_dump" in command:
            assert stdout is not None
            stdout.write(b"database-dump")
        elif "storage-init" in command and stdout is not None:
            stdout.write(b"artifact-archive")
        return subprocess.CompletedProcess(command, 0, stdout=data, stderr=b"")


def test_quiesced_backup_stops_ingress_and_records_manifest(tmp_path: Path) -> None:
    runner = _FakeRunner()
    target = ComposeTarget(tmp_path / "compose.yml", "phase7-test")
    target.compose_file.write_text("services: {}", encoding="utf-8")
    manifest = backup(
        runner, target, tmp_path / "backup", leave_stopped=True  # type: ignore[arg-type]
    )
    assert manifest["implementation_sha"] == "b" * 40
    assert manifest["counts"] == {
        "jobs": 1,
        "events": 2,
        "artifacts": 4,
        "outbox": 1,
    }
    stop_commands = [command[-2:] for command in runner.commands if "stop" in command]
    assert stop_commands[:2] == [["stop", "api"], ["stop", "worker"]]
    assert ["stop", "notifier"] in stop_commands


def test_backup_refuses_running_attempts(tmp_path: Path) -> None:
    runner = _FakeRunner(active=1)
    target = ComposeTarget(tmp_path / "compose.yml", "phase7-test")
    target.compose_file.write_text("services: {}", encoding="utf-8")
    with pytest.raises(OperationError, match="still RUNNING"):
        backup(
            runner,
            target,
            tmp_path / "backup",
            leave_stopped=True,  # type: ignore[arg-type]
        )
    assert not (tmp_path / "backup" / "database.dump").exists()


class _ReadyResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None


class _RestoreRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.empty_checks = 0
        self.artifacts = [
            {
                "storage_key": f"job/attempt/artifact-{index}",
                "sha256": f"{index:064x}",
                "size_bytes": index,
            }
            for index in range(8)
        ]

    def run(self, command, *, stdin=None, stdout=None, capture=False):
        command = list(command)
        self.commands.append(command)
        data = b""
        query = command[-1]
        if query == "SELECT to_regclass('public.jobs');":
            data = b""
        elif query == COUNT_QUERY:
            data = json.dumps(
                {"jobs": 2, "events": 4, "artifacts": 8, "outbox": 2}
            ).encode()
        elif query == ARTIFACT_QUERY:
            data = json.dumps(self.artifacts).encode()
        elif query == ACTIVE_QUERY:
            data = b"0"
        elif query == FRESH_WORKER_QUERY:
            data = b"1"
        elif query == ARTIFACT_EMPTY_SCRIPT:
            data = b"0" if self.empty_checks == 0 else b"8"
            self.empty_checks += 1
        elif query == ARTIFACT_MANIFEST_SCRIPT:
            data = json.dumps(self.artifacts).encode()
        if "pg_restore" in command or stdin is not None:
            assert stdin is not None
            stdin.read()
        return subprocess.CompletedProcess(command, 0, stdout=data, stderr=b"")


def test_restore_verifies_empty_target_hashes_readiness_and_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path / "bundle")
    target = ComposeTarget(tmp_path / "compose.yml", "phase7-restore")
    target.compose_file.write_text("services: {}", encoding="utf-8")
    runner = _RestoreRunner()
    monkeypatch.setattr(
        "scripts.platform_ops.urllib.request.urlopen",
        lambda *args, **kwargs: _ReadyResponse(),
    )

    manifest = restore(
        runner,  # type: ignore[arg-type]
        target,
        bundle,
        readiness_url="http://127.0.0.1:18080/health/ready",
        readiness_timeout=1,
    )

    assert manifest["counts"]["jobs"] == 2
    assert any("pg_restore" in command for command in runner.commands)
    assert any("migrate" in command for command in runner.commands)
    assert any("grant-runtime" in command for command in runner.commands)
