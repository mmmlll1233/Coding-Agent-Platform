from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Sequence


FORMAT_VERSION = 1
PROJECT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
COUNT_QUERY = (
    "SELECT json_build_object("
    "'jobs',(SELECT count(*) FROM jobs),"
    "'events',(SELECT count(*) FROM job_events),"
    "'artifacts',(SELECT count(*) FROM artifacts),"
    "'outbox',(SELECT count(*) FROM notification_outbox))::text;"
)
ACTIVE_QUERY = "SELECT count(*) FROM attempts WHERE status = 'RUNNING';"
REVISION_QUERY = "SELECT version_num FROM alembic_version LIMIT 1;"
FRESH_WORKER_QUERY = (
    "SELECT count(*) FROM worker_nodes WHERE service_type = 'worker' "
    "AND heartbeat_at >= clock_timestamp() - interval '5 minutes' "
    "AND coalesce((metadata_json->>'draining')::boolean, false) = false;"
)
ARTIFACT_QUERY = (
    "SELECT coalesce(json_agg(json_build_object("
    "'storage_key',storage_key,'sha256',sha256,'size_bytes',size_bytes) "
    "ORDER BY storage_key),'[]'::json)::text FROM artifacts;"
)
ARTIFACT_ARCHIVE_SCRIPT = r"""
import sys, tarfile
from pathlib import Path
root = Path('/var/lib/mewcode/artifacts')
with tarfile.open(fileobj=sys.stdout.buffer, mode='w|gz') as archive:
    if root.exists():
        for path in sorted(root.rglob('*')):
            if path.is_symlink():
                raise SystemExit('Artifact backup refuses symbolic links')
            archive.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)
""".strip()
ARTIFACT_EMPTY_SCRIPT = r"""
from pathlib import Path
root = Path('/var/lib/mewcode/artifacts')
print('0' if not root.exists() else sum(1 for path in root.rglob('*') if path.is_file()))
""".strip()
ARTIFACT_MANIFEST_SCRIPT = r"""
import hashlib, json
from pathlib import Path
root = Path('/var/lib/mewcode/artifacts')
items = []
if root.exists():
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.is_symlink():
            continue
        digest = hashlib.sha256()
        with path.open('rb') as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        items.append({'storage_key': path.relative_to(root).as_posix(),
                      'sha256': digest.hexdigest(),
                      'size_bytes': path.stat().st_size})
print(json.dumps(items, sort_keys=True, separators=(',', ':')))
""".strip()
ARTIFACT_RESTORE_SCRIPT = r"""
import shutil, sys, tarfile
from pathlib import Path, PurePosixPath
root = Path('/var/lib/mewcode/artifacts').resolve()
root.mkdir(parents=True, exist_ok=True)
with tarfile.open(fileobj=sys.stdin.buffer, mode='r|gz') as archive:
    for member in archive:
        pure = PurePosixPath(member.name)
        if (not member.name or pure.is_absolute() or '..' in pure.parts
                or member.issym() or member.islnk() or member.isdev() or member.isfifo()):
            raise SystemExit('Unsafe Artifact backup entry')
        target = root.joinpath(*pure.parts).resolve()
        if root not in target.parents and target != root:
            raise SystemExit('Artifact backup path escapes restore root')
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not member.isfile():
            raise SystemExit('Unsupported Artifact backup entry')
        target.parent.mkdir(parents=True, exist_ok=True)
        source = archive.extractfile(member)
        if source is None:
            raise SystemExit('Artifact backup entry cannot be read')
        with target.open('xb') as output:
            shutil.copyfileobj(source, output, 1024 * 1024)
""".strip()


class OperationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ComposeTarget:
    compose_file: Path
    project_name: str

    def prefix(self) -> list[str]:
        return [
            "docker",
            "compose",
            "-f",
            str(self.compose_file),
            "-p",
            self.project_name,
        ]


class Runner:
    def run(
        self,
        command: Sequence[str],
        *,
        stdin: BinaryIO | None = None,
        stdout: BinaryIO | int | None = None,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                list(command),
                stdin=stdin,
                stdout=subprocess.PIPE if capture else stdout,
                stderr=subprocess.PIPE,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            detail = ""
            if isinstance(error, subprocess.CalledProcessError) and error.stderr:
                detail = error.stderr.decode("utf-8", errors="replace")[-1000:]
            raise OperationError(
                f"Command failed: {' '.join(command)}" + (f": {detail}" if detail else "")
            ) from error


def _secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=False)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _secure_file(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _compose(target: ComposeTarget, *arguments: str) -> list[str]:
    return [*target.prefix(), *arguments]


def _psql(runner: Runner, target: ComposeTarget, query: str) -> str:
    result = runner.run(
        _compose(
            target,
            "exec",
            "-T",
            "postgres",
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "mewcode_migrator",
            "-d",
            "mewcode",
            "-tAc",
            query,
        ),
        capture=True,
    )
    return (result.stdout or b"").decode("utf-8").strip()


def _counts(runner: Runner, target: ComposeTarget) -> dict[str, int]:
    try:
        value = json.loads(_psql(runner, target, COUNT_QUERY))
    except (json.JSONDecodeError, ValueError) as error:
        raise OperationError("Platform database counts are unavailable") from error
    expected = {"jobs", "events", "artifacts", "outbox"}
    if set(value) != expected or any(
        not isinstance(value[name], int) or value[name] < 0 for name in expected
    ):
        raise OperationError("Platform database returned invalid counts")
    return value


def _artifact_file_count(runner: Runner, target: ComposeTarget) -> int:
    result = runner.run(
        _compose(
            target,
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--entrypoint",
            "python",
            "storage-init",
            "-c",
            ARTIFACT_EMPTY_SCRIPT,
        ),
        capture=True,
    )
    try:
        return int((result.stdout or b"").decode("utf-8").strip())
    except ValueError as error:
        raise OperationError("Artifact volume count is unavailable") from error


def _artifact_manifest(runner: Runner, target: ComposeTarget) -> list[dict[str, Any]]:
    result = runner.run(
        _compose(
            target,
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--entrypoint",
            "python",
            "storage-init",
            "-c",
            ARTIFACT_MANIFEST_SCRIPT,
        ),
        capture=True,
    )
    try:
        value = json.loads((result.stdout or b"").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OperationError("Artifact volume manifest is unavailable") from error
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise OperationError("Artifact volume manifest is invalid")
    return value


def _git_sha(runner: Runner) -> str:
    result = runner.run(["git", "rev-parse", "HEAD"], capture=True)
    value = (result.stdout or b"").decode("ascii", errors="ignore").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise OperationError("Current implementation SHA is unavailable")
    return value


def verify_bundle(bundle: Path) -> dict:
    manifest_path = bundle / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OperationError("Backup manifest is missing or invalid") from error
    if manifest.get("format_version") != FORMAT_VERSION:
        raise OperationError("Unsupported backup manifest format")
    if not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("implementation_sha", ""))):
        raise OperationError("Backup manifest implementation SHA is invalid")
    if not isinstance(manifest.get("alembic_revision"), str) or not manifest[
        "alembic_revision"
    ]:
        raise OperationError("Backup manifest Alembic revision is invalid")
    try:
        created_at = datetime.fromisoformat(str(manifest.get("created_at", "")))
    except ValueError as error:
        raise OperationError("Backup manifest creation time is invalid") from error
    if created_at.tzinfo is None:
        raise OperationError("Backup manifest creation time must include a timezone")
    counts = manifest.get("counts")
    if not isinstance(counts, dict) or set(counts) != {
        "jobs",
        "events",
        "artifacts",
        "outbox",
    } or any(not isinstance(value, int) or value < 0 for value in counts.values()):
        raise OperationError("Backup manifest counts are invalid")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != {
        "database.dump",
        "artifacts.tar.gz",
    }:
        raise OperationError("Backup manifest file list is invalid")
    for name in ("database.dump", "artifacts.tar.gz"):
        value = files.get(name, {})
        if not isinstance(value, dict):
            raise OperationError(f"Backup manifest file entry is invalid: {name}")
        path = bundle / name
        if not path.is_file():
            raise OperationError(f"Backup file is missing: {name}")
        if path.stat().st_size != value.get("size_bytes") or _sha256(path) != value.get(
            "sha256"
        ):
            raise OperationError(f"Backup checksum mismatch: {name}")
    return manifest


def backup(
    runner: Runner,
    target: ComposeTarget,
    output: Path,
    *,
    leave_stopped: bool,
) -> dict:
    _secure_directory(output)
    stopped = False
    try:
        runner.run(_compose(target, "stop", "api"))
        stopped = True
        runner.run(_compose(target, "stop", "worker"))
        active = int(_psql(runner, target, ACTIVE_QUERY))
        if active:
            raise OperationError(
                f"Backup refused because {active} Attempt(s) are still RUNNING"
            )
        runner.run(_compose(target, "stop", "notifier"))
        counts = _counts(runner, target)
        revision = _psql(runner, target, REVISION_QUERY)

        database_dump = output / "database.dump"
        with database_dump.open("xb") as destination:
            runner.run(
                _compose(
                    target,
                    "exec",
                    "-T",
                    "postgres",
                    "pg_dump",
                    "-U",
                    "mewcode_migrator",
                    "-d",
                    "mewcode",
                    "--format=custom",
                    "--no-owner",
                    "--no-privileges",
                ),
                stdout=destination,
            )
        _secure_file(database_dump)

        artifact_archive = output / "artifacts.tar.gz"
        with artifact_archive.open("xb") as destination:
            runner.run(
                _compose(
                    target,
                    "run",
                    "--rm",
                    "--no-deps",
                    "-T",
                    "--entrypoint",
                    "python",
                    "storage-init",
                    "-c",
                    ARTIFACT_ARCHIVE_SCRIPT,
                ),
                stdout=destination,
            )
        _secure_file(artifact_archive)

        manifest = {
            "format_version": FORMAT_VERSION,
            "created_at": datetime.now(UTC).isoformat(),
            "implementation_sha": _git_sha(runner),
            "alembic_revision": revision,
            "counts": counts,
            "files": {
                path.name: {
                    "size_bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                }
                for path in (database_dump, artifact_archive)
            },
        }
        manifest_path = output / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        _secure_file(manifest_path)
        return verify_bundle(output)
    finally:
        if stopped and not leave_stopped:
            runner.run(_compose(target, "up", "-d", "api", "worker", "notifier"))


def restore(
    runner: Runner,
    target: ComposeTarget,
    bundle: Path,
    *,
    readiness_url: str,
    readiness_timeout: int,
) -> dict:
    manifest = verify_bundle(bundle)
    runner.run(_compose(target, "up", "-d", "postgres"))
    for _ in range(60):
        try:
            relation = _psql(runner, target, "SELECT to_regclass('public.jobs');")
            break
        except OperationError:
            time.sleep(1)
    else:
        raise OperationError("Restore PostgreSQL did not become ready")
    if relation:
        raise OperationError(
            "Restore refused because the target Compose database already has a schema"
        )
    if _artifact_file_count(runner, target):
        raise OperationError("Restore refused because the target Artifact volume is not empty")

    with (bundle / "database.dump").open("rb") as source:
        runner.run(
            _compose(
                target,
                "exec",
                "-T",
                "postgres",
                "pg_restore",
                "-U",
                "mewcode_migrator",
                "-d",
                "mewcode",
                "--clean",
                "--if-exists",
                "--no-owner",
                "--no-privileges",
            ),
            stdin=source,
        )
    with (bundle / "artifacts.tar.gz").open("rb") as source:
        runner.run(
            _compose(
                target,
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "--entrypoint",
                "python",
                "storage-init",
                "-c",
                ARTIFACT_RESTORE_SCRIPT,
            ),
            stdin=source,
        )
    runner.run(_compose(target, "run", "--rm", "--no-deps", "migrate"))
    runner.run(_compose(target, "run", "--rm", "--no-deps", "grant-runtime"))
    restored_counts = _counts(runner, target)
    if restored_counts != manifest["counts"]:
        raise OperationError("Restored database counts do not match the backup manifest")
    if _artifact_file_count(runner, target) != manifest["counts"]["artifacts"]:
        raise OperationError("Restored Artifact count does not match the database")
    try:
        database_artifacts = json.loads(_psql(runner, target, ARTIFACT_QUERY))
    except json.JSONDecodeError as error:
        raise OperationError("Restored Artifact metadata is unavailable") from error
    if _artifact_manifest(runner, target) != database_artifacts:
        raise OperationError("Restored Artifact hashes do not match PostgreSQL metadata")
    runner.run(_compose(target, "up", "-d", "api", "worker", "notifier"))
    deadline = time.monotonic() + readiness_timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(readiness_url, timeout=3) as response:
                if response.status == 200:
                    if int(_psql(runner, target, ACTIVE_QUERY)):
                        raise OperationError(
                            "Restored platform unexpectedly has an active Attempt"
                        )
                    if int(_psql(runner, target, FRESH_WORKER_QUERY)) < 1:
                        raise OperationError(
                            "Restored Worker did not register after lease recovery startup"
                        )
                    return manifest
        except OSError:
            time.sleep(1)
    raise OperationError("Restored platform did not become ready")


def _target(args: argparse.Namespace) -> ComposeTarget:
    compose_file = Path(args.compose_file).resolve()
    if not compose_file.is_file():
        raise OperationError("Compose file does not exist")
    if not args.project_name or not PROJECT_NAME.fullmatch(args.project_name):
        raise OperationError("Project name contains unsafe characters")
    return ComposeTarget(compose_file, args.project_name)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Quiesced MewCode Platform backup and restore helper"
    )
    parser.add_argument("--compose-file", default="compose.platform.yml")
    parser.add_argument("--project-name")
    commands = parser.add_subparsers(dest="command", required=True)
    backup_parser = commands.add_parser("backup")
    backup_parser.add_argument("output", type=Path)
    backup_parser.add_argument("--leave-stopped", action="store_true")
    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("bundle", type=Path)
    restore_parser = commands.add_parser("restore")
    restore_parser.add_argument("bundle", type=Path)
    restore_parser.add_argument(
        "--readiness-url", default="http://127.0.0.1:8080/health/ready"
    )
    restore_parser.add_argument("--readiness-timeout", type=int, default=120)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    runner = Runner()
    try:
        if args.command == "verify":
            manifest = verify_bundle(args.bundle.resolve())
        else:
            target = _target(args)
            if args.command == "backup":
                manifest = backup(
                    runner,
                    target,
                    args.output.resolve(),
                    leave_stopped=args.leave_stopped,
                )
            else:
                if args.readiness_timeout <= 0:
                    raise OperationError("Readiness timeout must be positive")
                manifest = restore(
                    runner,
                    target,
                    args.bundle.resolve(),
                    readiness_url=args.readiness_url,
                    readiness_timeout=args.readiness_timeout,
                )
        print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, OperationError, ValueError) as error:
        print(f"platform-ops: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
