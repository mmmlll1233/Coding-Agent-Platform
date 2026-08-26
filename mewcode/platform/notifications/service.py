from __future__ import annotations

import asyncio
import logging
import random
import socket
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from mewcode.platform.execution import SensitiveValueRedactor
from mewcode.platform.observability import NotifierMetrics, log_context
from mewcode.platform.persistence import ClaimedNotification, PlatformRepository
from mewcode.platform.settings import PlatformSettings

from .feishu import FeishuDeliveryError, FeishuWebhookClient

log = logging.getLogger(__name__)


class NotifierService:
    def __init__(
        self,
        settings: PlatformSettings,
        repository: PlatformRepository,
        client: FeishuWebhookClient,
        *,
        redactor: SensitiveValueRedactor | None = None,
        random_source: Callable[[], float] = random.random,
        metrics: NotifierMetrics | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.client = client
        self.redactor = redactor or SensitiveValueRedactor(())
        self.notifier_id = settings.notifier_id or f"notifier-{socket.gethostname()}"
        self.random_source = random_source
        self.metrics = metrics
        self._stopping = asyncio.Event()

    async def _heartbeat_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                if not await self.repository.heartbeat_service(
                    service_id=self.notifier_id, service_type="notifier"
                ):
                    log.error(
                        "Notifier heartbeat registration is missing",
                        extra={"event": "notifier_heartbeat_failure"},
                    )
                    if self.metrics is not None:
                        self.metrics.heartbeat_failures.inc()
            except Exception:
                log.exception(
                    "Notifier heartbeat failed",
                    extra={"event": "notifier_heartbeat_failure"},
                )
                if self.metrics is not None:
                    self.metrics.heartbeat_failures.inc()
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self.settings.heartbeat_seconds
                )
            except TimeoutError:
                pass

    def _retry_delay(self, attempt_count: int, retry_after: float | None) -> float:
        maximum = float(self.settings.notification_backoff_max_seconds)
        if retry_after is not None:
            return min(maximum, max(0.0, retry_after))
        exponent = min(max(attempt_count - 1, 0), 20)
        base = min(
            maximum,
            float(self.settings.notification_backoff_base_seconds) * (2**exponent),
        )
        return min(maximum, base * (0.8 + 0.4 * self.random_source()))

    async def deliver_once(self) -> bool:
        claimed = await self.repository.claim_notification(
            notifier_id=self.notifier_id,
            lease_seconds=self.settings.notification_lease_seconds,
        )
        if claimed is None:
            await self.refresh_backlog_metrics()
            return False
        with log_context(
            notification_id=str(claimed.id),
            job_id=str(claimed.job_id),
            event_type=claimed.event_type,
        ):
            return await self._deliver_claimed(claimed)

    async def _deliver_claimed(self, claimed: ClaimedNotification) -> bool:
        started = time.monotonic()
        event_type = (
            self.metrics.event_type(claimed.event_type)
            if self.metrics is not None
            else claimed.event_type
        )
        try:
            await self.client.send(claimed.payload)
        except Exception as error:  # noqa: BLE001 - every delivery failure is retried
            retry_after = (
                error.retry_after_seconds
                if isinstance(error, FeishuDeliveryError)
                else None
            )
            delay = self._retry_delay(claimed.attempt_count, retry_after)
            safe_error = self.redactor.redact(str(error))[:512]
            acknowledged = await self.repository.retry_notification(
                notification_id=claimed.id,
                notifier_id=self.notifier_id,
                fencing_token=claimed.fencing_token,
                next_attempt_at=datetime.now(UTC) + timedelta(seconds=delay),
                error=safe_error,
            )
            log.warning(
                "Notification delivery failed; retry scheduled",
                extra={
                    "event": "notification_delivery_retry",
                    "notification_id": str(claimed.id),
                    "event_type": claimed.event_type,
                    "result": "retry" if acknowledged else "lease_lost",
                },
            )
            if self.metrics is not None:
                result = "retry" if acknowledged else "lease_lost"
                self.metrics.delivery.labels(event_type, result).inc()
                self.metrics.delivery_duration.labels(event_type, result).observe(
                    time.monotonic() - started
                )
            await self.refresh_backlog_metrics()
            return True
        acknowledged = await self.repository.mark_notification_delivered(
            notification_id=claimed.id,
            notifier_id=self.notifier_id,
            fencing_token=claimed.fencing_token,
        )
        log.info(
            "Notification delivered",
            extra={
                "event": "notification_delivered",
                "notification_id": str(claimed.id),
                "event_type": claimed.event_type,
                "result": "delivered" if acknowledged else "lease_lost",
            },
        )
        if self.metrics is not None:
            result = "delivered" if acknowledged else "lease_lost"
            self.metrics.delivery.labels(event_type, result).inc()
            self.metrics.delivery_duration.labels(event_type, result).observe(
                time.monotonic() - started
            )
        await self.refresh_backlog_metrics()
        return True

    async def refresh_backlog_metrics(self) -> None:
        if self.metrics is None:
            return
        stats = await self.repository.notification_outbox_stats()
        self.metrics.backlog.labels("pending").set(stats.pending)
        self.metrics.backlog.labels("in_flight").set(stats.in_flight)
        self.metrics.oldest_pending.set(stats.oldest_pending_seconds)

    async def run_forever(self) -> None:
        await self.repository.register_service(
            service_id=self.notifier_id,
            service_type="notifier",
            metadata={"destination": self.settings.notification_destination},
        )
        heartbeat = asyncio.create_task(self._heartbeat_loop())
        try:
            while not self._stopping.is_set():
                if await self.deliver_once():
                    continue
                try:
                    await asyncio.wait_for(
                        self._stopping.wait(),
                        timeout=self.settings.notification_poll_milliseconds / 1000,
                    )
                except TimeoutError:
                    pass
        finally:
            self._stopping.set()
            await heartbeat

    def stop(self) -> None:
        self._stopping.set()
