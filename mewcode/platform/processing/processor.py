from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tarfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from mewcode.config import ProviderConfig
from mewcode.platform.api.schemas import CommandRequest, ExecutionRequest
from mewcode.platform.artifacts import (
    ArtifactKind,
    ArtifactService,
    LocalArtifactStore,
    render_workspace_diff,
)
from mewcode.platform.domain import (
    AttemptControls,
    AttemptLease,
    AttemptOutcome,
    AttemptOutcomeStatus,
    AttemptStage,
    ScmAdapter,
    VerifiedDeliveryRequest,
)
from mewcode.platform.execution import (
    AttemptExecutionSpec,
    DockerExecutionEnvironment,
    ExecutionCommand,
    ExecutionEnvironment,
    ExecutionEnvironmentError,
    ExecutionLimits,
    SensitiveValueRedactor,
)
from mewcode.platform.persistence import PlatformRepository, StateConflict
from mewcode.platform.runtime import (
    AgentRuntimeFactory,
    JobEvent,
    JobEventSink,
    JobRunner,
    JobRunRequest,
    JobRunStatus,
    RuntimeOptions,
    RuntimeProfile,
)
from mewcode.platform.scm import (
    NoChangesError,
    ScmDeliveryConflict,
    ScmPolicyError,
    ScmUnavailable,
    create_scm_adapter,
)
from mewcode.platform.settings import PlatformSettings

_LOG_RESERVE_DIVISOR = 3
_GUIDANCE_BYTES = 32 * 1024
_FEEDBACK_BYTES = 64 * 1024
log = logging.getLogger(__name__)


class _AttemptTerminal(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: AttemptOutcomeStatus = AttemptOutcomeStatus.FAILED,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass
class _AttemptEvidence:
    commands: list[dict[str, Any]] = field(default_factory=list)
    rounds: list[dict[str, Any]] = field(default_factory=list)
    final_success: bool = False
    repair_rounds: int = 0
    agent_log: bytes = b""
    diff: bytes = b"# mewcode-diff-v1\n# Workspace was not exported.\n"
    changed_paths: list[dict[str, Any]] = field(default_factory=list)
    workspace_archive: Path | None = None
    prepared: Any | None = None
    terminal: _AttemptTerminal | None = None
    usage: dict[str, int] = field(
        default_factory=lambda: {"input_tokens": 0, "output_tokens": 0}
    )


class _RecordingSink(JobEventSink):
    def __init__(
        self,
        target: JobEventSink,
        redactor: SensitiveValueRedactor,
        max_bytes: int,
    ) -> None:
        self.target = target
        self.redactor = redactor
        self.max_bytes = max_bytes
        self.buffer = bytearray()
        self.truncated = False

    async def emit(self, event: JobEvent) -> None:
        await self.target.emit(event)
        value = {
            "job_id": event.job_id,
            "attempt_id": event.attempt_id,
            "attempt_sequence": event.attempt_sequence,
            "timestamp": event.timestamp.isoformat(),
            "event_type": event.event_type,
            "payload": event.payload,
        }
        line = (
            self.redactor.redact(
                json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            ).encode("utf-8")
            + b"\n"
        )
        if len(self.buffer) + len(line) <= self.max_bytes:
            self.buffer.extend(line)
        elif not self.truncated:
            marker = b'{"event_type":"artifact_truncated","reason":"quota"}\n'
            self.buffer.extend(marker[: max(0, self.max_bytes - len(self.buffer))])
            self.truncated = True


class ProductionAttemptProcessor:
    def __init__(
        self,
        settings: PlatformSettings,
        repository: PlatformRepository,
        artifact_service: ArtifactService,
        redactor: SensitiveValueRedactor,
        *,
        scm: ScmAdapter,
        environment_factory: Callable[[AttemptExecutionSpec], ExecutionEnvironment],
        runtime_factory: Callable[[RuntimeOptions], Any] = AgentRuntimeFactory.create,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.artifact_service = artifact_service
        self.redactor = redactor
        self.scm = scm
        self.environment_factory = environment_factory
        self.runtime_factory = runtime_factory
        self._runner: JobRunner | None = None
        self._runtime: Any | None = None
        self._environment: ExecutionEnvironment | None = None
        self._cancelled = asyncio.Event()
        self._state_dir: Path | None = None

    async def cancel(self) -> None:
        self._cancelled.set()
        if self._runner is not None:
            await self._runner.cancel()
        elif self._environment is not None:
            await self._environment.aclose()

    def _provider(self) -> ProviderConfig:
        api_key = (
            Path(self.settings.llm_api_key_file).read_text(encoding="utf-8").strip()
        )
        return ProviderConfig(
            name="platform",
            protocol=self.settings.llm_protocol,
            base_url=self.settings.llm_base_url,
            model=self.settings.llm_model,
            api_key=api_key,
            thinking=self.settings.llm_thinking,
            context_window=self.settings.llm_context_window,
            max_output_tokens=self.settings.llm_max_output_tokens,
        )

    def _execution_spec(self, lease: AttemptLease) -> AttemptExecutionSpec:
        spec = AttemptExecutionSpec(
            job_id=str(lease.job_id),
            attempt_id=str(lease.attempt_id),
            executor_image=self.settings.executor_image,
            proxy_image=self.settings.proxy_image,
            trusted_state_dir=Path(self.settings.state_root),
            limits=ExecutionLimits(
                command_timeout_seconds=min(
                    600, self.settings.attempt_timeout_seconds
                ),
                attempt_timeout_seconds=self.settings.attempt_timeout_seconds,
            ),
            egress_allowlist=self.settings.egress_allowlist,
        )
        self._state_dir = spec.trusted_state_dir
        return spec

    def _remove_trusted_state(self) -> None:
        state_dir = self._state_dir
        self._state_dir = None
        if state_dir is None:
            return
        root = Path(self.settings.state_root).resolve()
        resolved = state_dir.resolve()
        if resolved.parent.name != "attempts" or root not in resolved.parents:
            raise RuntimeError("Refusing to remove an unexpected trusted state path")
        shutil.rmtree(resolved)

    def _contract(self, lease: AttemptLease) -> ExecutionRequest:
        try:
            contract = ExecutionRequest.model_validate(lease.execution_contract)
        except ValidationError as error:
            raise _AttemptTerminal(
                "INVALID_EXECUTION_CONTRACT", "Stored execution contract is invalid"
            ) from error
        if (
            sum(item.timeout_seconds for item in contract.setup_commands)
            > self.settings.setup_timeout_budget_seconds
        ):
            raise _AttemptTerminal(
                "SETUP_FAILED", "Setup timeout budget exceeds Worker policy"
            )
        if (
            sum(item.timeout_seconds for item in contract.verification_commands)
            > self.settings.verification_timeout_budget_seconds
        ):
            raise _AttemptTerminal(
                "VERIFICATION_FAILED",
                "Verification timeout budget exceeds Worker policy",
            )
        return contract

    @staticmethod
    def _repository_guidance(archive_path: Path) -> str:
        candidates = {"AGENTS.md", "MEWCODE.md"}
        sections: list[str] = []
        remaining = _GUIDANCE_BYTES
        try:
            with tarfile.open(archive_path, mode="r:*") as archive:
                for member in archive.getmembers():
                    name = member.name.replace("\\", "/").removeprefix("./")
                    if name not in candidates or not member.isfile() or remaining <= 0:
                        continue
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        continue
                    content = extracted.read(remaining + 1)[:remaining]
                    remaining -= len(content)
                    sections.append(
                        f"## Untrusted repository guidance: {name}\n"
                        + content.decode("utf-8", errors="replace")
                    )
        except (OSError, tarfile.TarError):
            return ""
        return "\n\n".join(sections)

    @staticmethod
    def _initial_prompt(lease: AttemptLease, contract: ExecutionRequest) -> str:
        payload = {
            "work_request": lease.work_request,
            "supplemental_inputs": list(lease.inputs),
            "verification_contract": [
                item.model_dump(mode="json") for item in contract.verification_commands
            ],
        }
        return (
            "Implement the following platform Work Request in /workspace. "
            "The Verification Contract is immutable: do not delete, replace, or "
            "weaken its commands. Do not publish or access credentials.\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
        )

    def _repair_prompt(self, failed_round: dict[str, Any]) -> str:
        value = json.dumps(failed_round, ensure_ascii=False, separators=(",", ":"))
        safe = self.redactor.redact(value).encode("utf-8")[:_FEEDBACK_BYTES]
        return (
            "Platform Verification failed. Fix the implementation without changing "
            "the immutable Verification Contract. After this bounded Repair Round, "
            "the platform will run every declared command again. Redacted results:\n"
            + safe.decode("utf-8", errors="replace")
        )

    async def _run_command(
        self,
        command: CommandRequest,
        *,
        phase: str,
        round_number: int,
        controls: AttemptControls,
        evidence: _AttemptEvidence,
    ) -> dict[str, Any]:
        assert self._environment is not None
        if controls.cancellation.is_set() or self._cancelled.is_set():
            raise _AttemptTerminal(
                "CANCELLED",
                "Attempt was cancelled",
                status=AttemptOutcomeStatus.CANCELLED,
            )
        started = time.monotonic()
        command_task = asyncio.create_task(
            self._environment.run_command(
                ExecutionCommand(
                    command=command.command,
                    timeout_seconds=command.timeout_seconds,
                )
            )
        )
        cancelled = asyncio.create_task(controls.cancellation.wait())
        try:
            done, _ = await asyncio.wait(
                {command_task, cancelled}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancelled in done and controls.cancellation.is_set():
                command_task.cancel()
                await asyncio.gather(command_task, return_exceptions=True)
                raise _AttemptTerminal(
                    "CANCELLED",
                    "Attempt was cancelled",
                    status=AttemptOutcomeStatus.CANCELLED,
                )
            outcome = await command_task
        finally:
            cancelled.cancel()
            await asyncio.gather(cancelled, return_exceptions=True)
        result = outcome.command_result
        record = {
            "phase": phase,
            "round": round_number,
            "name": command.name,
            "command": self.redactor.redact(command.command),
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout": self.redactor.redact(result.stdout),
            "stderr": self.redactor.redact(result.stderr),
            "fatal_error_code": outcome.fatal_error_code,
            "fatal_error_message": self.redactor.redact(
                outcome.fatal_error_message or ""
            ),
        }
        evidence.commands.append(record)
        if outcome.fatal_error_code:
            raise _AttemptTerminal(
                outcome.fatal_error_code,
                outcome.fatal_error_message or "Execution boundary failed",
            )
        return record

    async def _run_setup(
        self,
        commands: list[CommandRequest],
        controls: AttemptControls,
        evidence: _AttemptEvidence,
    ) -> None:
        for command in commands:
            result = await self._run_command(
                command,
                phase="setup",
                round_number=0,
                controls=controls,
                evidence=evidence,
            )
            if result["timed_out"] or result["exit_code"] != 0:
                raise _AttemptTerminal(
                    "SETUP_FAILED", f"Setup command failed: {command.name}"
                )

    async def _run_verification(
        self,
        commands: list[CommandRequest],
        round_number: int,
        controls: AttemptControls,
        evidence: _AttemptEvidence,
    ) -> dict[str, Any]:
        results = []
        for command in commands:
            results.append(
                await self._run_command(
                    command,
                    phase="verification",
                    round_number=round_number,
                    controls=controls,
                    evidence=evidence,
                )
            )
        round_result = {
            "round": round_number,
            "succeeded": all(
                item["exit_code"] == 0 and not item["timed_out"] for item in results
            ),
            "commands": results,
        }
        evidence.rounds.append(round_result)
        return round_result

    async def _run_agent(
        self,
        lease: AttemptLease,
        prompt: str,
        sink: _RecordingSink,
        sequence: int,
    ) -> tuple[int, Any]:
        assert self._runtime is not None
        runner = JobRunner(
            self._runtime,
            sink,
            owns_runtime=False,
            initial_sequence=sequence,
        )
        self._runner = runner
        result = await runner.run(
            JobRunRequest(
                job_id=str(lease.job_id),
                attempt_id=str(lease.attempt_id),
                prompt=prompt,
            )
        )
        self._runner = None
        return runner.last_sequence, result

    @staticmethod
    def _result_terminal(result: Any) -> _AttemptTerminal | None:
        if result.status == JobRunStatus.COMPLETED:
            return None
        if result.status == JobRunStatus.NEEDS_INPUT:
            return _AttemptTerminal(
                result.error_code or "NEEDS_INPUT",
                result.error_message or "Agent requires Requester input",
                status=AttemptOutcomeStatus.NEEDS_INPUT,
            )
        if result.status == JobRunStatus.CANCELLED:
            return _AttemptTerminal(
                "CANCELLED",
                "Attempt was cancelled",
                status=AttemptOutcomeStatus.CANCELLED,
            )
        return _AttemptTerminal(
            result.error_code or "RUNTIME_FAILED",
            result.error_message or "Agent Runtime failed",
        )

    async def _execute(
        self,
        lease: AttemptLease,
        controls: AttemptControls,
        evidence: _AttemptEvidence,
        sink: _RecordingSink,
    ) -> tuple[Any, int]:
        contract = self._contract(lease)
        spec = self._execution_spec(lease)
        spec.trusted_state_dir.mkdir(parents=True, exist_ok=True)
        await controls.report_stage(AttemptStage.PREPARING)
        prepared = await self.scm.prepare(
            lease.repository_target, spec.trusted_state_dir
        )
        evidence.prepared = prepared
        self._environment = self.environment_factory(spec)
        await self._environment.start()
        await self._environment.import_archive_file(prepared.archive_path)
        await self._run_setup(contract.setup_commands, controls, evidence)

        await controls.report_stage(AttemptStage.ANALYZING)
        self._runtime = self.runtime_factory(
            RuntimeOptions(
                profile=RuntimeProfile.PLATFORM,
                provider=self._provider(),
                repository_guidance=self.redactor.redact(
                    self._repository_guidance(prepared.archive_path)
                ),
                execution_environment=self._environment,
            )
        )
        await controls.report_stage(AttemptStage.IMPLEMENTING)
        sequence, result = await self._run_agent(
            lease, self._initial_prompt(lease, contract), sink, 0
        )
        evidence.usage["input_tokens"] += result.input_tokens
        evidence.usage["output_tokens"] += result.output_tokens
        terminal = self._result_terminal(result)
        if terminal is not None:
            raise terminal

        for round_number in range(self.settings.max_repair_rounds + 1):
            await controls.report_stage(AttemptStage.VERIFYING)
            round_result = await self._run_verification(
                contract.verification_commands,
                round_number,
                controls,
                evidence,
            )
            if round_result["succeeded"]:
                evidence.final_success = True
                evidence.repair_rounds = round_number
                break
            if round_number >= self.settings.max_repair_rounds:
                evidence.repair_rounds = round_number
                raise _AttemptTerminal(
                    "VERIFICATION_FAILED",
                    "Verification failed after all Repair Rounds",
                )
            await controls.report_stage(AttemptStage.IMPLEMENTING)
            sequence, result = await self._run_agent(
                lease, self._repair_prompt(round_result), sink, sequence
            )
            evidence.usage["input_tokens"] += result.input_tokens
            evidence.usage["output_tokens"] += result.output_tokens
            terminal = self._result_terminal(result)
            if terminal is not None:
                raise terminal

        workspace_archive = spec.trusted_state_dir / "workspace-final.tar"
        await self._environment.export_archive_file(workspace_archive)
        evidence.workspace_archive = workspace_archive
        try:
            evidence.diff, changes = render_workspace_diff(
                prepared_archive=prepared.archive_path,
                prepared_manifest=prepared.manifest_path,
                workspace_archive=workspace_archive,
                max_files=self.settings.max_delivery_files,
                max_bytes=self.settings.max_delivery_bytes,
                max_file_bytes=self.settings.max_delivery_file_bytes,
            )
            evidence.changed_paths = [
                {
                    "path": change.path,
                    "mode": change.entry.mode if change.entry else None,
                    "sha256": change.entry.sha256 if change.entry else None,
                    "size": change.entry.size if change.entry else 0,
                    "deleted": change.entry is None,
                }
                for change in changes
            ]
        except NoChangesError as error:
            evidence.diff = b"# mewcode-diff-v1\n# No deliverable changes.\n"
            raise _AttemptTerminal("NO_CHANGES", str(error)) from error
        return prepared, sequence

    async def _export_failure_workspace(self, evidence: _AttemptEvidence) -> None:
        if self._environment is None or evidence.workspace_archive is not None:
            return
        path = self._environment.spec.trusted_state_dir / "workspace-final.tar"
        try:
            await self._environment.export_archive_file(path)
            evidence.workspace_archive = path
            if evidence.prepared is not None:
                try:
                    evidence.diff, changes = render_workspace_diff(
                        prepared_archive=evidence.prepared.archive_path,
                        prepared_manifest=evidence.prepared.manifest_path,
                        workspace_archive=path,
                        max_files=self.settings.max_delivery_files,
                        max_bytes=self.settings.max_delivery_bytes,
                        max_file_bytes=self.settings.max_delivery_file_bytes,
                    )
                    evidence.changed_paths = [
                        {
                            "path": change.path,
                            "mode": change.entry.mode if change.entry else None,
                            "sha256": change.entry.sha256 if change.entry else None,
                            "size": change.entry.size if change.entry else 0,
                            "deleted": change.entry is None,
                        }
                        for change in changes
                    ]
                except NoChangesError:
                    evidence.diff = b"# mewcode-diff-v1\n# No deliverable changes.\n"
        except Exception:  # noqa: BLE001 - best-effort evidence after a terminal error
            return

    async def _close_executor(self) -> None:
        target = self._runtime or self._environment
        self._runtime = None
        self._environment = None
        if target is not None:
            await asyncio.shield(target.aclose())

    def _command_log(self, evidence: _AttemptEvidence, limit: int) -> bytes:
        output = bytearray()
        for command in evidence.commands:
            line = (
                self.redactor.redact(
                    json.dumps(command, ensure_ascii=False, separators=(",", ":"))
                ).encode("utf-8")
                + b"\n"
            )
            if len(output) + len(line) > limit:
                output.extend(
                    b'{"event_type":"artifact_truncated","reason":"quota"}\n'[
                        : max(0, limit - len(output))
                    ]
                )
                break
            output.extend(line)
        return bytes(output)

    @staticmethod
    def _truncate_diff(content: bytes, limit: int) -> bytes:
        if len(content) <= limit:
            return content
        marker = b"\n# ARTIFACT TRUNCATED: quota exceeded\n"
        return content[: max(0, limit - len(marker))] + marker

    async def _persist_evidence(
        self,
        lease: AttemptLease,
        evidence: _AttemptEvidence,
    ) -> dict[str, Any]:
        reserve = min(
            self.settings.max_artifact_bytes,
            max(
                1,
                (
                    self.settings.max_attempt_artifact_bytes
                    - self.settings.max_artifact_bytes
                )
                // _LOG_RESERVE_DIVISOR,
            ),
        )
        agent = await self.artifact_service.persist_bytes(
            lease,
            kind=ArtifactKind.AGENT_LOG,
            content=evidence.agent_log[:reserve],
            content_type="application/x-ndjson",
        )
        command = await self.artifact_service.persist_bytes(
            lease,
            kind=ArtifactKind.COMMAND_LOG,
            content=self._command_log(evidence, reserve),
            content_type="application/x-ndjson",
        )
        diff = await self.artifact_service.persist_bytes(
            lease,
            kind=ArtifactKind.DIFF,
            content=self._truncate_diff(evidence.diff, reserve),
            content_type="text/x-diff",
        )
        report_value = {
            "schema_version": 1,
            "job_id": str(lease.job_id),
            "attempt_id": str(lease.attempt_id),
            "attempt_no": lease.attempt_no,
            "final_succeeded": evidence.final_success,
            "repair_rounds": evidence.repair_rounds,
            "rounds": evidence.rounds,
            "changed_paths": evidence.changed_paths,
            "terminal": (
                {
                    "status": evidence.terminal.status.value,
                    "code": evidence.terminal.code,
                    "message": self.redactor.redact(str(evidence.terminal)),
                }
                if evidence.terminal
                else None
            ),
            "artifacts": {
                "agent_log": str(agent.id),
                "command_log": str(command.id),
                "diff": str(diff.id),
            },
        }
        report_content = self.redactor.redact(
            json.dumps(report_value, ensure_ascii=False, sort_keys=True, indent=2)
        ).encode("utf-8")
        report = await self.artifact_service.persist_bytes(
            lease,
            kind=ArtifactKind.VERIFICATION_REPORT,
            content=report_content,
            content_type="application/json",
        )
        return {
            "agent_log": agent,
            "command_log": command,
            "diff": diff,
            "verification_report": report,
        }

    async def process(
        self, lease: AttemptLease, controls: AttemptControls
    ) -> AttemptOutcome:
        evidence = _AttemptEvidence()
        per_log_limit = min(
            self.settings.max_artifact_bytes,
            max(
                1,
                (
                    self.settings.max_attempt_artifact_bytes
                    - self.settings.max_artifact_bytes
                )
                // _LOG_RESERVE_DIVISOR,
            ),
        )
        sink = _RecordingSink(controls.event_sink, self.redactor, per_log_limit)
        prepared: Any | None = None
        artifacts: dict[str, Any] = {}
        attempt_deadline: asyncio.Timeout | None = None
        deadline_cleanup_at: float | None = None
        try:
            async with asyncio.timeout(
                self.settings.attempt_timeout_seconds
            ) as attempt_deadline:
                try:
                    prepared, _ = await self._execute(lease, controls, evidence, sink)
                except _AttemptTerminal as terminal:
                    evidence.terminal = terminal
                    await self._export_failure_workspace(evidence)
                except ScmPolicyError as error:
                    evidence.terminal = _AttemptTerminal(error.code, str(error))
                except ScmUnavailable as error:
                    evidence.terminal = _AttemptTerminal(error.code, str(error))
                except ExecutionEnvironmentError as error:
                    evidence.terminal = _AttemptTerminal("EXECUTOR_LOST", str(error))
                    await self._export_failure_workspace(evidence)
                except Exception as error:  # noqa: BLE001 - Processor trust boundary
                    evidence.terminal = _AttemptTerminal(
                        "PROCESSOR_EXCEPTION", str(error)
                    )
                    await self._export_failure_workspace(evidence)
                finally:
                    if prepared is None:
                        prepared = evidence.prepared
                    evidence.agent_log = bytes(sink.buffer)
                    if not attempt_deadline.expired():
                        try:
                            async with asyncio.timeout(30):
                                await self._close_executor()
                        except Exception as error:  # noqa: BLE001 - cleanup trust boundary
                            evidence.terminal = _AttemptTerminal(
                                "EXECUTOR_CLEANUP_FAILED", str(error)
                            )

                if attempt_deadline.expired():
                    # A Runtime may translate Agent cancellation into a normal
                    # CANCELLED result. Re-enter the single deadline cleanup path
                    # instead of letting work continue without a timeout.
                    raise TimeoutError

                if evidence.workspace_archive is not None:
                    try:
                        artifacts = await self._persist_evidence(lease, evidence)
                    except Exception as error:  # noqa: BLE001 - storage trust boundary
                        evidence.terminal = _AttemptTerminal(
                            "ARTIFACT_PERSIST_FAILED", str(error)
                        )

                if evidence.terminal is not None:
                    return AttemptOutcome(
                        status=evidence.terminal.status,
                        error_code=evidence.terminal.code,
                        error_message=self.redactor.redact(str(evidence.terminal)),
                        usage=evidence.usage,
                    )
                if prepared is None or evidence.workspace_archive is None:
                    return AttemptOutcome(
                        status=AttemptOutcomeStatus.FAILED,
                        error_code="ARTIFACT_PERSIST_FAILED",
                        error_message="Final Workspace evidence is unavailable",
                        usage=evidence.usage,
                    )
                if controls.cancellation.is_set() or self._cancelled.is_set():
                    return AttemptOutcome(
                        status=AttemptOutcomeStatus.CANCELLED,
                        error_code="CANCELLED",
                        error_message="Attempt was cancelled",
                        usage=evidence.usage,
                    )
                try:
                    await controls.report_stage(AttemptStage.PUBLISHING)
                except StateConflict as error:
                    if error.code == "JOB_CANCEL_REQUESTED":
                        return AttemptOutcome(
                            status=AttemptOutcomeStatus.CANCELLED,
                            error_code="CANCELLED",
                            error_message="Attempt was cancelled",
                            usage=evidence.usage,
                        )
                    raise
                report_id = artifacts["verification_report"].id
                verification_lines = [
                    f"- `{item['command']}` ({item['name']}): passed"
                    for item in evidence.rounds[-1]["commands"]
                ]
                delivery_request = VerifiedDeliveryRequest(
                    job_id=lease.job_id,
                    prepared=prepared,
                    workspace_archive_path=evidence.workspace_archive,
                    work_title=str(lease.work_request.get("title", "MewCode change")),
                    work_summary=str(
                        lease.work_request.get(
                            "description", "Platform work request"
                        )
                    ),
                    change_summary=(
                        f"Changed {len(evidence.changed_paths)} deliverable path(s)."
                    ),
                    verification_summary=(
                        "\n".join(verification_lines)
                        + f"\nRepair rounds: {evidence.repair_rounds}"
                        + f"\nJob ID: {lease.job_id}"
                        + f"\nVerification report Artifact ID: {report_id}"
                    ),
                )
                try:
                    delivery = await self.scm.publish_verified(delivery_request)
                except asyncio.CancelledError:
                    if controls.cancellation.is_set() or self._cancelled.is_set():
                        raise
                    # A deadline can race with GitHub accepting a branch or PR.
                    # Spend the one 30 second trusted budget replaying the
                    # deterministic Delivery request so no PR is left unaccounted.
                    deadline_cleanup_at = asyncio.get_running_loop().time() + 30
                    try:
                        async with asyncio.timeout_at(deadline_cleanup_at):
                            delivery = await self.scm.publish_verified(delivery_request)
                    except Exception as error:
                        raise TimeoutError from error
                return AttemptOutcome(
                    status=AttemptOutcomeStatus.COMPLETED,
                    usage=evidence.usage,
                    pr_number=delivery.pr_number,
                    pr_url=delivery.pr_url,
                    head_branch=delivery.head_branch,
                    head_sha=delivery.head_sha,
                    verification_succeeded=True,
                )
        except TimeoutError:
            if attempt_deadline is None or not attempt_deadline.expired():
                raise
            evidence.terminal = _AttemptTerminal(
                "ATTEMPT_DEADLINE_EXCEEDED",
                "Attempt exceeded its "
                f"{self.settings.attempt_timeout_seconds} second deadline",
            )
            evidence.agent_log = bytes(sink.buffer)
            if deadline_cleanup_at is None:
                deadline_cleanup_at = asyncio.get_running_loop().time() + 30
            try:
                async with asyncio.timeout_at(deadline_cleanup_at):
                    await self._export_failure_workspace(evidence)
                    await self._close_executor()
                    evidence.agent_log = bytes(sink.buffer)
                    # Even a deadline before repository preparation must leave
                    # all four downloadable diagnostic Artifact kinds.
                    await self._persist_evidence(lease, evidence)
            except Exception:  # noqa: BLE001 - bounded deadline cleanup boundary
                log.warning("Attempt deadline cleanup was incomplete", exc_info=True)
            return AttemptOutcome(
                status=AttemptOutcomeStatus.FAILED,
                error_code="ATTEMPT_DEADLINE_EXCEEDED",
                error_message=(
                    "Attempt exceeded its "
                    f"{self.settings.attempt_timeout_seconds} second deadline"
                ),
                usage=evidence.usage,
            )
        except ScmDeliveryConflict as error:
            return AttemptOutcome(
                status=AttemptOutcomeStatus.FAILED,
                error_code=error.code,
                error_message=self.redactor.redact(str(error)),
                usage=evidence.usage,
            )
        except (ScmPolicyError, ScmUnavailable) as error:
            return AttemptOutcome(
                status=AttemptOutcomeStatus.FAILED,
                error_code=error.code,
                error_message=self.redactor.redact(str(error)),
                usage=evidence.usage,
            )
        finally:
            try:
                await self.scm.aclose()
            except Exception:
                log.warning("SCM Adapter cleanup failed", exc_info=True)
            try:
                await asyncio.to_thread(self._remove_trusted_state)
            except FileNotFoundError:
                pass
            except Exception:
                log.warning("Attempt trusted state cleanup failed", exc_info=True)


class ProductionAttemptProcessorFactory:
    def __init__(
        self,
        settings: PlatformSettings,
        repository: PlatformRepository,
        redactor: SensitiveValueRedactor,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.redactor = redactor
        self.artifact_service = ArtifactService(
            repository,
            LocalArtifactStore(settings.artifact_root),
            redactor=redactor,
            retention_days=settings.artifact_retention_days,
            max_artifact_bytes=settings.max_artifact_bytes,
            max_attempt_bytes=settings.max_attempt_artifact_bytes,
        )

    def create(self, lease: AttemptLease) -> ProductionAttemptProcessor:
        return ProductionAttemptProcessor(
            self.settings,
            self.repository,
            self.artifact_service,
            self.redactor,
            scm=create_scm_adapter(self.settings),
            environment_factory=lambda spec: DockerExecutionEnvironment(
                spec, egress_network_name=self.settings.egress_network
            ),
        )

    async def cleanup_expired(self) -> tuple[int, int]:
        return await self.artifact_service.cleanup_expired()


def create_attempt_processor_factory(
    settings: PlatformSettings,
    *,
    repository: PlatformRepository | None = None,
    redactor: SensitiveValueRedactor | None = None,
) -> ProductionAttemptProcessorFactory:
    if repository is None:
        raise RuntimeError("Production Attempt Processor requires PlatformRepository")
    return ProductionAttemptProcessorFactory(
        settings,
        repository,
        redactor or SensitiveValueRedactor(()),
    )
