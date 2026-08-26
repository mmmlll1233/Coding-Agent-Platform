from __future__ import annotations

import asyncio
import logging
import socket
import time
from uuid import uuid4

from mewcode.platform.domain import (
    AttemptControls,
    AttemptOutcome,
    AttemptOutcomeStatus,
    AttemptProcessor,
    AttemptProcessorFactory,
    AttemptStage,
)
from mewcode.platform.execution import SensitiveValueRedactor
from mewcode.platform.observability import WorkerMetrics, log_context
from mewcode.platform.persistence import (
    ClaimedAttempt,
    LeaseLost,
    PlatformRepository,
    PostgresJobEventSink,
)
from mewcode.platform.settings import PlatformSettings

log = logging.getLogger(__name__)


class WorkerService:
    def __init__(
        self,
        settings: PlatformSettings,
        repository: PlatformRepository,
        processor_factory: AttemptProcessorFactory | None,
        *,
        redactor: SensitiveValueRedactor | None = None,
        metrics: WorkerMetrics | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.processor_factory = processor_factory
        self.redactor = redactor or SensitiveValueRedactor(())
        self.metrics = metrics
        self.worker_id = settings.worker_id or (
            f"{socket.gethostname()}-{uuid4().hex[:12]}"
        )
        self._stop = asyncio.Event()
        self._active: set[asyncio.Task[None]] = set()

    async def stop(self) -> None:
        self._stop.set()
        if self._active:
            await asyncio.gather(*tuple(self._active), return_exceptions=True)

    async def _worker_heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            if not await self.repository.heartbeat_worker(self.worker_id):
                raise RuntimeError("Worker registration disappeared")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.settings.heartbeat_seconds
                )
            except TimeoutError:
                continue

    async def _recovery_loop(self) -> None:
        while not self._stop.is_set():
            recovered = await self.repository.recover_expired_leases()
            if recovered:
                log.warning("Recovered %s expired Worker Lease(s)", recovered)
                if self.metrics is not None:
                    self.metrics.lease_recoveries.inc(recovered)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.settings.recovery_seconds
                )
            except TimeoutError:
                continue

    async def _janitor_loop(self) -> None:
        cleanup = getattr(self.processor_factory, "cleanup_expired", None)
        if cleanup is None:
            await self._stop.wait()
            return
        while not self._stop.is_set():
            try:
                artifacts, jobs = await cleanup()
                if artifacts or jobs:
                    log.info(
                        "Retention janitor deleted artifacts=%s jobs=%s",
                        artifacts,
                        jobs,
                    )
            except Exception:
                log.exception("Retention janitor failed")
                if self.metrics is not None:
                    self.metrics.janitor_failures.inc()
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self.settings.janitor_interval_seconds,
                )
            except TimeoutError:
                continue

    async def _lease_monitor(
        self,
        claimed: ClaimedAttempt,
        processor: AttemptProcessor,
        cancellation: asyncio.Event,
        done: asyncio.Event,
        monitor_failed: asyncio.Event,
    ) -> None:
        lease = claimed.lease
        cancel_sent = False
        while not done.is_set():
            try:
                await self.repository.heartbeat_lease(
                    attempt_id=lease.attempt_id,
                    worker_id=lease.worker_id,
                    fencing_token=lease.fencing_token,
                    lease_seconds=self.settings.lease_seconds,
                )
                cancel_requested = await self.repository.is_cancel_requested(
                    attempt_id=lease.attempt_id,
                    worker_id=lease.worker_id,
                    fencing_token=lease.fencing_token,
                )
            except LeaseLost:
                cancellation.set()
                if not cancel_sent:
                    cancel_sent = True
                    try:
                        await processor.cancel()
                    except Exception:
                        log.exception(
                            "Attempt Processor cancellation failed attempt_id=%s",
                            lease.attempt_id,
                        )
                return
            except Exception:
                log.exception(
                    "Worker Lease monitor failed attempt_id=%s", lease.attempt_id
                )
                monitor_failed.set()
                cancellation.set()
                if not cancel_sent:
                    try:
                        await processor.cancel()
                    except Exception:
                        log.exception(
                            "Attempt Processor cancellation failed attempt_id=%s",
                            lease.attempt_id,
                        )
                return
            if cancel_requested and not cancel_sent:
                cancellation.set()
                cancel_sent = True
                try:
                    await processor.cancel()
                except Exception:
                    log.exception(
                        "Attempt Processor cancellation failed attempt_id=%s",
                        lease.attempt_id,
                    )
            try:
                await asyncio.wait_for(
                    done.wait(), timeout=self.settings.heartbeat_seconds
                )
            except TimeoutError:
                continue

    async def _run_claimed(self, claimed: ClaimedAttempt) -> None:
        lease = claimed.lease
        with log_context(
            job_id=str(lease.job_id),
            attempt_id=str(lease.attempt_id),
            worker_id=lease.worker_id,
        ):
            await self._run_claimed_in_context(claimed)

    async def _run_claimed_in_context(self, claimed: ClaimedAttempt) -> None:
        assert self.processor_factory is not None
        lease = claimed.lease
        started = time.monotonic()
        processor = self.processor_factory.create(lease)
        cancellation = asyncio.Event()
        done = asyncio.Event()
        monitor_failed = asyncio.Event()
        sink = PostgresJobEventSink(
            self.repository,
            job_id=lease.job_id,
            attempt_id=lease.attempt_id,
            worker_id=lease.worker_id,
            fencing_token=lease.fencing_token,
            redactor=self.redactor,
        )

        async def report_stage(stage: AttemptStage) -> None:
            await self.repository.report_stage(
                attempt_id=lease.attempt_id,
                worker_id=lease.worker_id,
                fencing_token=lease.fencing_token,
                stage=stage,
            )

        controls = AttemptControls(
            event_sink=sink,
            report_stage=report_stage,
            cancellation=cancellation,
        )
        monitor = asyncio.create_task(
            self._lease_monitor(claimed, processor, cancellation, done, monitor_failed),
            name=f"lease-monitor-{lease.attempt_id}",
        )
        try:
            outcome = await processor.process(lease, controls)
        except asyncio.CancelledError:
            cancellation.set()
            try:
                await processor.cancel()
            finally:
                raise
        except Exception as error:
            log.exception("Attempt Processor failed attempt_id=%s", lease.attempt_id)
            outcome = AttemptOutcome(
                status=AttemptOutcomeStatus.FAILED,
                error_code="PROCESSOR_EXCEPTION",
                error_message=self.redactor.redact(str(error)),
            )
        finally:
            done.set()
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
        if monitor_failed.is_set():
            outcome = AttemptOutcome(
                status=AttemptOutcomeStatus.FAILED,
                error_code="LEASE_MONITOR_FAILED",
                error_message="Worker Lease monitoring failed",
            )
        try:
            await self.repository.finish_attempt(
                attempt_id=lease.attempt_id,
                worker_id=lease.worker_id,
                fencing_token=lease.fencing_token,
                outcome=outcome,
            )
        except LeaseLost:
            log.warning(
                "Discarded stale Attempt outcome attempt_id=%s", lease.attempt_id
            )
        if self.metrics is not None:
            label = outcome.status.value.lower()
            self.metrics.attempts.labels(label).inc()
            self.metrics.attempt_duration.labels(label).observe(
                time.monotonic() - started
            )

    def _track(self, task: asyncio.Task[None]) -> None:
        self._active.add(task)
        if self.metrics is not None:
            self.metrics.active_attempts.set(len(self._active))

        def finished(completed: asyncio.Task[None]) -> None:
            self._active.discard(completed)
            if self.metrics is not None:
                self.metrics.active_attempts.set(len(self._active))
            if (
                not completed.cancelled()
                and (error := completed.exception()) is not None
            ):
                log.error(
                    "Attempt task failed: %s",
                    error,
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(finished)

    async def run_forever(self) -> None:
        if self.processor_factory is None:
            raise RuntimeError(
                "No Attempt Processor is configured; refusing to start the Worker"
            )
        await self.repository.register_worker(
            self.worker_id,
            metadata={"max_concurrent_jobs": self.settings.max_concurrent_jobs},
        )
        heartbeat = asyncio.create_task(
            self._worker_heartbeat_loop(), name="worker-heartbeat"
        )
        recovery = asyncio.create_task(self._recovery_loop(), name="lease-recovery")
        janitor = asyncio.create_task(self._janitor_loop(), name="retention-janitor")
        try:
            while not self._stop.is_set():
                for background in (heartbeat, recovery, janitor):
                    if background.done():
                        error = background.exception()
                        raise RuntimeError(
                            f"Worker background task stopped: {background.get_name()}"
                        ) from error
                while len(self._active) < self.settings.max_concurrent_jobs:
                    claimed = await self.repository.claim_attempt(
                        worker_id=self.worker_id,
                        lease_seconds=self.settings.lease_seconds,
                        max_concurrent_jobs=self.settings.max_concurrent_jobs,
                    )
                    if claimed is None:
                        break
                    task = asyncio.create_task(
                        self._run_claimed(claimed),
                        name=f"attempt-{claimed.lease.attempt_id}",
                    )
                    self._track(task)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=0.5)
                except TimeoutError:
                    continue
        finally:
            self._stop.set()
            heartbeat.cancel()
            recovery.cancel()
            janitor.cancel()
            await asyncio.gather(heartbeat, recovery, janitor, return_exceptions=True)
            if self._active:
                for task in tuple(self._active):
                    task.cancel()
                await asyncio.gather(*tuple(self._active), return_exceptions=True)
