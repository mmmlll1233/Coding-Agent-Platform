from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import quote
from uuid import UUID, uuid4

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig

from mewcode.platform.domain import (
    AttemptControls,
    AttemptLease,
    AttemptOutcome,
    AttemptOutcomeStatus,
    AttemptStage,
    JobStatus,
    RepositoryTarget,
)
from mewcode.platform.execution import (
    AttemptExecutionSpec,
    DockerExecutionEnvironment,
    ExecutionCommand,
    ExecutionLimits,
)
from mewcode.platform.persistence import PlatformRepository, create_database
from mewcode.platform.settings import PlatformSettings
from mewcode.platform.workers import WorkerService

try:
    from scripts.phase7_gate import verify_server_evidence
except ModuleNotFoundError:  # Direct execution puts scripts/ first on sys.path.
    from phase7_gate import verify_server_evidence


SCHEMA_VERSION = 1
GLOBAL_CAPACITY = 5
JOB_COUNT = 10
POSTGRES_IMAGE = (
    "postgres@sha256:"
    "cf78e76683b9ca8c5733cbbdce6c9262b45b6767934dd0a95e671f9a0fc20685"
)
_IMMUTABLE_IMAGE = re.compile(
    r"^(?:sha256:[0-9a-f]{64}|[^\s@]+@sha256:[0-9a-f]{64})$"
)
_MANAGED_LABEL = "com.mewcode.managed=true"
_JOB_LABEL = "com.mewcode.job_id"
_GATE_LABEL = "com.mewcode.validation=phase7-server-gate"


class ServerGateError(RuntimeError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ServerGateError(message)


@dataclass(frozen=True)
class ServerGateConfig:
    executor_image: str
    proxy_image: str
    postgres_image: str = POSTGRES_IMAGE
    work_root: Path = Path(".mewcode/phase7-server-gate")
    evidence_path: Path = Path(".mewcode/phase7-evidence/server-input.json")
    summary_path: Path = Path(".mewcode/phase7-evidence/server-summary.json")
    local_slots: int = GLOBAL_CAPACITY
    timeout_seconds: int = 300
    postgres_startup_timeout_seconds: int = 90

    def __post_init__(self) -> None:
        for name, image in (
            ("executor image", self.executor_image),
            ("proxy image", self.proxy_image),
            ("PostgreSQL image", self.postgres_image),
        ):
            if not _IMMUTABLE_IMAGE.fullmatch(image):
                raise ServerGateError(f"{name} must use an immutable sha256 digest")
        if self.local_slots < GLOBAL_CAPACITY:
            raise ServerGateError("local slots must total at least 5")
        if self.timeout_seconds < 30:
            raise ServerGateError("gate timeout must be at least 30 seconds")
        if self.postgres_startup_timeout_seconds < 10:
            raise ServerGateError("PostgreSQL startup timeout must be at least 10 seconds")
        if self.evidence_path.resolve() == self.summary_path.resolve():
            raise ServerGateError("evidence and summary paths must be different")


@dataclass
class GateObservation:
    job_ids: list[UUID] = field(default_factory=list)
    maximum_observed_running: int = 0
    sixth_job_observed_queued: bool = False
    batches_completed: int = 0
    completed_jobs: int = 0
    failed_jobs: int = 0


def build_server_evidence(
    observation: GateObservation,
    *,
    local_slots_total: int,
    docker_resources_remaining: int,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "global_capacity": GLOBAL_CAPACITY,
        "local_slots_total": local_slots_total,
        "maximum_observed_running": observation.maximum_observed_running,
        "sixth_job_observed_queued": observation.sixth_job_observed_queued,
        "batches_completed": observation.batches_completed,
        "deterministic_jobs": JOB_COUNT,
        "completed_jobs": observation.completed_jobs,
        "failed_jobs": observation.failed_jobs,
        "docker_resources_remaining": docker_resources_remaining,
    }


def _run_docker(
    *arguments: str,
    environment: dict[str, str] | None = None,
    check: bool = True,
) -> str:
    try:
        result = subprocess.run(
            ["docker", *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
        )
    except FileNotFoundError as error:
        raise ServerGateError("Docker CLI is not installed or not on PATH") from error
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ServerGateError(
            f"docker {' '.join(arguments[:2])} failed"
            + (f": {detail}" if detail else "")
        )
    return result.stdout.strip()


def _preflight(config: ServerGateConfig) -> None:
    info = _run_docker("info", "--format", "{{.OSType}}|{{.CgroupVersion}}")
    os_type, separator, cgroup_version = info.partition("|")
    _require(separator == "|" and os_type == "linux", "Docker must use the Linux engine")
    _require(cgroup_version == "2", "Docker must use cgroup v2")
    for image in (
        config.executor_image,
        config.proxy_image,
        config.postgres_image,
    ):
        _run_docker("image", "inspect", image, "--format", "{{.Id}}")


class EphemeralPostgres:
    def __init__(self, image: str, startup_timeout_seconds: int) -> None:
        suffix = uuid4().hex[:12]
        self.image = image
        self.startup_timeout_seconds = startup_timeout_seconds
        self.container_name = f"mewcode-phase7-server-db-{suffix}"
        self.volume_name = f"mewcode-phase7-server-db-{suffix}"
        self.database_name = "mewcode_server_gate"
        self.password = secrets.token_hex(24)
        self._started = False

    @property
    def database_url(self) -> str:
        port = _run_docker("port", self.container_name, "5432/tcp")
        bindings = port.splitlines()
        _require(bool(bindings), "Cannot resolve PostgreSQL port")
        first_binding = bindings[0].strip()
        _, separator, host_port = first_binding.rpartition(":")
        _require(separator == ":" and host_port.isdecimal(), "Cannot resolve PostgreSQL port")
        return (
            "postgresql+asyncpg://postgres:"
            f"{quote(self.password, safe='')}@127.0.0.1:{host_port}/{self.database_name}"
        )

    def start(self) -> None:
        _run_docker("volume", "create", "--label", _GATE_LABEL, self.volume_name)
        environment = dict(os.environ)
        environment["POSTGRES_PASSWORD"] = self.password
        try:
            _run_docker(
                "run",
                "--detach",
                "--name",
                self.container_name,
                "--label",
                _GATE_LABEL,
                "--env",
                "POSTGRES_PASSWORD",
                "--env",
                f"POSTGRES_DB={self.database_name}",
                "--publish",
                "127.0.0.1::5432",
                "--mount",
                f"type=volume,src={self.volume_name},dst=/var/lib/postgresql/data",
                self.image,
                environment=environment,
            )
            self._started = True
            deadline = time.monotonic() + self.startup_timeout_seconds
            while time.monotonic() < deadline:
                result = subprocess.run(
                    [
                        "docker",
                        "exec",
                        self.container_name,
                        "pg_isready",
                        "-U",
                        "postgres",
                        "-d",
                        self.database_name,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                if result.returncode == 0:
                    return
                time.sleep(0.5)
            raise ServerGateError("Ephemeral PostgreSQL did not become ready in time")
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        _run_docker(
            "container",
            "rm",
            "--force",
            "--volumes",
            self.container_name,
            check=False,
        )
        self._started = False
        _run_docker("volume", "rm", "--force", self.volume_name, check=False)


def _upgrade_schema(database_url: str) -> None:
    config = AlembicConfig()
    migrations = (
        Path(__file__).resolve().parents[1]
        / "mewcode"
        / "platform"
        / "persistence"
        / "migrations"
    )
    config.set_main_option("script_location", str(migrations))
    config.set_main_option("sqlalchemy.url", database_url)
    alembic_command.upgrade(config, "head")


class _BatchFactory:
    def __init__(
        self,
        *,
        executor_image: str,
        proxy_image: str,
        state_root: Path,
        egress_network: str,
    ) -> None:
        self.executor_image = executor_image
        self.proxy_image = proxy_image
        self.state_root = state_root
        self.egress_network = egress_network
        self.releases = (asyncio.Event(), asyncio.Event())
        self.ready_events = (asyncio.Event(), asyncio.Event())
        self.created = 0
        self.ready = 0
        self.active = 0
        self.maximum_active = 0
        self._lock = asyncio.Lock()

    def create(self, lease: AttemptLease) -> "_DockerBarrierProcessor":
        self.created += 1
        return _DockerBarrierProcessor(self, self.created)

    async def mark_ready(self, ordinal: int) -> None:
        async with self._lock:
            self.ready += 1
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            batch = (ordinal - 1) // GLOBAL_CAPACITY
            if self.ready >= (batch + 1) * GLOBAL_CAPACITY:
                self.ready_events[batch].set()

    async def mark_closed(self) -> None:
        async with self._lock:
            self.active -= 1

    def release_all(self) -> None:
        for release in self.releases:
            release.set()


class _DockerBarrierProcessor:
    def __init__(self, factory: _BatchFactory, ordinal: int) -> None:
        self.factory = factory
        self.ordinal = ordinal
        self._cancelled = asyncio.Event()
        self._environment: DockerExecutionEnvironment | None = None
        self._close_lock = asyncio.Lock()

    async def _close(self) -> None:
        async with self._close_lock:
            environment, self._environment = self._environment, None
            if environment is not None:
                await environment.aclose()

    async def _wait_for_release(self, controls: AttemptControls) -> bool:
        batch = (self.ordinal - 1) // GLOBAL_CAPACITY
        waits = [
            asyncio.create_task(self.factory.releases[batch].wait()),
            asyncio.create_task(controls.cancellation.wait()),
            asyncio.create_task(self._cancelled.wait()),
        ]
        try:
            done, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            return waits[0] in done and self.factory.releases[batch].is_set()
        finally:
            for task in waits:
                task.cancel()
            await asyncio.gather(*waits, return_exceptions=True)

    async def process(
        self, lease: AttemptLease, controls: AttemptControls
    ) -> AttemptOutcome:
        marked_ready = False
        try:
            limits = ExecutionLimits(
                command_timeout_seconds=30,
                attempt_timeout_seconds=300,
            )
            spec = AttemptExecutionSpec(
                job_id=str(lease.job_id),
                attempt_id=str(lease.attempt_id),
                executor_image=self.factory.executor_image,
                proxy_image=self.factory.proxy_image,
                trusted_state_dir=self.factory.state_root,
                limits=limits,
            )
            self._environment = DockerExecutionEnvironment(
                spec, egress_network_name=self.factory.egress_network
            )
            await self._environment.start()
            await controls.report_stage(AttemptStage.IMPLEMENTING)
            await self._environment.workspace.write_file(
                "phase7-server-gate.txt",
                f"{lease.job_id}:{lease.attempt_id}\n",
                expected_version=None,
            )
            first_check = await self._environment.run_command(
                ExecutionCommand("test -s phase7-server-gate.txt", timeout_seconds=30)
            )
            _require(
                first_check.fatal_error_code is None
                and first_check.command_result.exit_code == 0,
                "Deterministic executor preparation failed",
            )
            await self.factory.mark_ready(self.ordinal)
            marked_ready = True
            if not await self._wait_for_release(controls):
                return AttemptOutcome(status=AttemptOutcomeStatus.CANCELLED)
            await controls.report_stage(AttemptStage.VERIFYING)
            verification = await self._environment.run_command(
                ExecutionCommand("test -s phase7-server-gate.txt", timeout_seconds=30)
            )
            _require(
                verification.fatal_error_code is None
                and verification.command_result.exit_code == 0,
                "Deterministic executor verification failed",
            )
            return AttemptOutcome(
                status=AttemptOutcomeStatus.COMPLETED,
                pr_number=self.ordinal,
                pr_url=(
                    "https://github.com/mewcode/server-gate/pull/"
                    f"{self.ordinal}"
                ),
                head_branch=f"mewcode/{lease.job_id}",
                head_sha="f" * 40,
                verification_succeeded=True,
            )
        finally:
            await asyncio.shield(self._close())
            if marked_ready:
                await self.factory.mark_closed()

    async def cancel(self) -> None:
        self._cancelled.set()
        await self._close()


async def _observe_queue(
    repository: PlatformRepository,
    observation: GateObservation,
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        stats = await repository.job_queue_stats()
        observation.maximum_observed_running = max(
            observation.maximum_observed_running, stats.running
        )
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.05)
        except TimeoutError:
            continue


async def _wait_for_terminal_jobs(
    repository: PlatformRepository,
    principal: Any,
    job_ids: list[UUID],
    timeout_seconds: int,
) -> list[str]:
    async with asyncio.timeout(timeout_seconds):
        while True:
            statuses = [
                (await repository.get_job(principal=principal, job_id=job_id)).status
                for job_id in job_ids
            ]
            if all(
                status
                in {
                    JobStatus.SUCCEEDED.value,
                    JobStatus.FAILED.value,
                    JobStatus.CANCELLED.value,
                    JobStatus.NEEDS_INPUT.value,
                }
                for status in statuses
            ):
                return statuses
            await asyncio.sleep(0.1)


async def _exercise_capacity(
    config: ServerGateConfig,
    database_url: str,
    run_state_root: Path,
    egress_network: str,
    observation: GateObservation,
) -> None:
    settings = PlatformSettings(
        database_url=database_url,
        worker_id=f"phase7-server-gate-{uuid4().hex[:12]}",
        max_concurrent_jobs=GLOBAL_CAPACITY,
        worker_max_concurrent_attempts=config.local_slots,
        lease_seconds=30,
        heartbeat_seconds=5,
        recovery_seconds=5,
        worker_shutdown_grace_seconds=30,
        executor_image=config.executor_image,
        proxy_image=config.proxy_image,
        state_root=str(run_state_root),
        egress_network=egress_network,
        notifications_enabled=False,
    )
    database = create_database(settings)
    repository = PlatformRepository(database)
    factory = _BatchFactory(
        executor_image=config.executor_image,
        proxy_image=config.proxy_image,
        state_root=run_state_root,
        egress_network=egress_network,
    )
    service = WorkerService(settings, repository, factory)
    worker_task: asyncio.Task[None] | None = None
    observer_task: asyncio.Task[None] | None = None
    observer_stop = asyncio.Event()
    try:
        _require(await database.ping(), "Ephemeral PostgreSQL is unreachable")
        _require(await database.schema_is_current(), "Ephemeral schema is not current")
        principal = await repository.ensure_requester(
            tenant_name="phase7-server-gate",
            requester_name="capacity-only-gate",
        )
        target = RepositoryTarget(
            installation_id=1,
            owner="mewcode",
            name="server-gate",
            base_ref="main",
            base_sha="a" * 40,
        )
        for index in range(JOB_COUNT):
            request_hash = hashlib.sha256(f"phase7-server-{index}".encode()).hexdigest()
            job, replayed = await repository.create_job(
                principal=principal,
                idempotency_key=f"phase7-server-{index}-{uuid4().hex}",
                request_hash=request_hash,
                target=target,
                work_request={
                    "kind": "deterministic-capacity-probe",
                    "index": index,
                },
                execution_contract={
                    "network": "proxy-only",
                    "delivery": "synthetic-isolated-database-only",
                },
            )
            _require(not replayed, "Gate Job was unexpectedly replayed")
            observation.job_ids.append(job.id)

        worker_task = asyncio.create_task(service.run_forever(), name="phase7-server-worker")
        observer_task = asyncio.create_task(
            _observe_queue(repository, observation, observer_stop),
            name="phase7-server-observer",
        )
        async with asyncio.timeout(config.timeout_seconds):
            await factory.ready_events[0].wait()
            stats = await repository.job_queue_stats()
            observation.maximum_observed_running = max(
                observation.maximum_observed_running, stats.running
            )
            sixth = await repository.get_job(
                principal=principal, job_id=observation.job_ids[GLOBAL_CAPACITY]
            )
            observation.sixth_job_observed_queued = (
                sixth.status == JobStatus.QUEUED.value
            )
            _require(stats.running == GLOBAL_CAPACITY, "Did not observe five running Attempts")
            _require(stats.queued == GLOBAL_CAPACITY, "Did not observe five queued Attempts")
            _require(
                observation.sixth_job_observed_queued,
                "The sixth submitted Job was not QUEUED",
            )
            await asyncio.sleep(0.75)
            held = await repository.job_queue_stats()
            observation.maximum_observed_running = max(
                observation.maximum_observed_running, held.running
            )
            _require(factory.created == GLOBAL_CAPACITY, "Worker exceeded the five-slot gate")
            factory.releases[0].set()

            await factory.ready_events[1].wait()
            second = await repository.job_queue_stats()
            observation.maximum_observed_running = max(
                observation.maximum_observed_running, second.running
            )
            _require(second.running == GLOBAL_CAPACITY, "Second batch did not fill five slots")
            _require(second.queued == 0, "Jobs remained queued after the second batch started")
            factory.releases[1].set()

            statuses = await _wait_for_terminal_jobs(
                repository,
                principal,
                observation.job_ids,
                config.timeout_seconds,
            )
            observation.completed_jobs = statuses.count(JobStatus.SUCCEEDED.value)
            observation.failed_jobs = statuses.count(JobStatus.FAILED.value)
            _require(factory.ready == JOB_COUNT, "Not all deterministic Attempts became ready")
            _require(factory.maximum_active == GLOBAL_CAPACITY, "Docker concurrency was not five")
            observation.batches_completed = 2
    except TimeoutError as error:
        raise ServerGateError("Server capacity gate timed out") from error
    finally:
        factory.release_all()
        observer_stop.set()
        if observer_task is not None:
            await asyncio.gather(observer_task, return_exceptions=True)
        if worker_task is not None:
            if not worker_task.done():
                await service.stop()
            results = await asyncio.gather(worker_task, return_exceptions=True)
            if results and isinstance(results[0], BaseException):
                active_error = sys.exc_info()[1]
                if active_error is None:
                    raise ServerGateError("Gate Worker stopped unexpectedly") from results[0]
        await database.aclose()


def _listed_resource_ids(kind: str, job_ids: Sequence[UUID]) -> set[str]:
    resources: set[str] = set()
    command = "ps" if kind == "container" else kind
    for job_id in job_ids:
        arguments = [command, "ls"] if kind != "container" else [command]
        arguments.extend(
            [
                "--quiet",
                "--filter",
                f"label={_MANAGED_LABEL}",
                "--filter",
                f"label={_JOB_LABEL}={job_id}",
            ]
        )
        if kind == "container":
            arguments.insert(1, "--all")
        output = _run_docker(*arguments)
        resources.update(line.strip() for line in output.splitlines() if line.strip())
    return resources


def _remove_gate_resources(job_ids: Sequence[UUID], egress_network: str) -> list[str]:
    errors: list[str] = []
    for kind, remove_arguments in (
        ("container", ("container", "rm", "--force")),
        ("network", ("network", "rm")),
        ("volume", ("volume", "rm", "--force")),
    ):
        try:
            resource_ids = _listed_resource_ids(kind, job_ids)
        except ServerGateError as error:
            errors.append(str(error))
            continue
        for resource_id in resource_ids:
            _run_docker(*remove_arguments, resource_id, check=False)
    inspect = subprocess.run(
        ["docker", "network", "inspect", egress_network],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if inspect.returncode == 0:
        result = subprocess.run(
            ["docker", "network", "rm", egress_network],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            errors.append("could not remove the gate egress network")
    return errors


def _remaining_gate_resources(
    job_ids: Sequence[UUID],
    egress_network: str,
    postgres: EphemeralPostgres,
) -> int:
    total = sum(
        len(_listed_resource_ids(kind, job_ids))
        for kind in ("container", "network", "volume")
    )
    inspect = subprocess.run(
        ["docker", "network", "inspect", egress_network],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    infrastructure_remaining = 0
    for kind, name in (
        ("container", postgres.container_name),
        ("volume", postgres.volume_name),
    ):
        result = subprocess.run(
            ["docker", kind, "inspect", name],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        infrastructure_remaining += 1 if result.returncode == 0 else 0
    return total + (1 if inspect.returncode == 0 else 0) + infrastructure_remaining


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def run_server_gate(config: ServerGateConfig) -> dict[str, Any]:
    # A failed rerun must not leave a stale PASSED summary looking current.
    config.summary_path.resolve().unlink(missing_ok=True)
    _preflight(config)
    run_id = uuid4().hex[:12]
    work_root = config.work_root.resolve()
    run_state_root = (work_root / f"run-{run_id}" / "state").resolve()
    egress_network = f"mewcode-phase7-server-egress-{run_id}"
    run_state_root.mkdir(parents=True, exist_ok=False)
    postgres = EphemeralPostgres(
        config.postgres_image, config.postgres_startup_timeout_seconds
    )
    observation = GateObservation()
    primary_error: BaseException | None = None
    cleanup_errors: list[str] = []
    try:
        postgres.start()
        database_url = postgres.database_url
        _upgrade_schema(database_url)
        asyncio.run(
            _exercise_capacity(
                config,
                database_url,
                run_state_root,
                egress_network,
                observation,
            )
        )
    except Exception as error:
        primary_error = error
    finally:
        try:
            cleanup_errors.extend(
                _remove_gate_resources(observation.job_ids, egress_network)
            )
        except Exception as error:
            cleanup_errors.append(str(error))
        postgres.stop()
        run_directory = run_state_root.parent
        if run_directory.is_dir() and run_directory.parent == work_root:
            try:
                shutil.rmtree(run_directory)
            except OSError as error:
                cleanup_errors.append(f"could not remove gate state: {error}")

    try:
        remaining = _remaining_gate_resources(
            observation.job_ids, egress_network, postgres
        )
    except ServerGateError as error:
        cleanup_errors.append(str(error))
        remaining = -1

    evidence = build_server_evidence(
        observation,
        local_slots_total=config.local_slots,
        docker_resources_remaining=remaining,
    )
    _write_json_atomic(config.evidence_path, evidence)
    if primary_error is not None:
        raise ServerGateError(str(primary_error)) from primary_error
    if cleanup_errors:
        raise ServerGateError("; ".join(cleanup_errors))
    summary = verify_server_evidence(evidence)
    _write_json_atomic(config.summary_path, summary)
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the Phase 7 five-concurrent-Attempt server gate in an isolated "
            "ephemeral database"
        )
    )
    parser.add_argument(
        "--executor-image",
        default=os.environ.get("MEWCODE_EXECUTOR_IMAGE", ""),
        help="Immutable executor image digest (or MEWCODE_EXECUTOR_IMAGE)",
    )
    parser.add_argument(
        "--proxy-image",
        default=os.environ.get("MEWCODE_PROXY_IMAGE", ""),
        help="Immutable proxy image digest (or MEWCODE_PROXY_IMAGE)",
    )
    parser.add_argument("--postgres-image", default=POSTGRES_IMAGE)
    parser.add_argument(
        "--work-root", type=Path, default=Path(".mewcode/phase7-server-gate")
    )
    parser.add_argument(
        "--evidence",
        type=Path,
        default=Path(".mewcode/phase7-evidence/server-input.json"),
    )
    parser.add_argument(
        "--summary",
        type=Path,
        default=Path(".mewcode/phase7-evidence/server-summary.json"),
    )
    parser.add_argument("--local-slots", type=int, default=GLOBAL_CAPACITY)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--postgres-startup-timeout-seconds", type=int, default=90)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = ServerGateConfig(
            executor_image=args.executor_image,
            proxy_image=args.proxy_image,
            postgres_image=args.postgres_image,
            work_root=args.work_root,
            evidence_path=args.evidence,
            summary_path=args.summary,
            local_slots=args.local_slots,
            timeout_seconds=args.timeout_seconds,
            postgres_startup_timeout_seconds=args.postgres_startup_timeout_seconds,
        )
        summary = run_server_gate(config)
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(f"phase7-server-gate: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
