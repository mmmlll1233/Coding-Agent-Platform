from __future__ import annotations

import contextvars
import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    start_http_server,
)

from mewcode.platform.execution import SensitiveValueRedactor

_CONTEXT_FIELDS = (
    "request_id",
    "job_id",
    "attempt_id",
    "worker_id",
    "notification_id",
    "event_type",
)
_CONTEXT = {
    name: contextvars.ContextVar(f"platform_{name}", default=None)
    for name in _CONTEXT_FIELDS
}
_EVENT_TYPES = {
    "JOB_ACCEPTED",
    "NEEDS_INPUT",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "UNKNOWN",
}


@contextmanager
def log_context(**values: str | None) -> Iterator[None]:
    tokens: list[tuple[contextvars.ContextVar[str | None], contextvars.Token]] = []
    try:
        for name, value in values.items():
            variable = _CONTEXT.get(name)
            if variable is not None:
                tokens.append((variable, variable.set(value)))
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


class JsonLogFormatter(logging.Formatter):
    def __init__(self, service: str, redactor: SensitiveValueRedactor) -> None:
        super().__init__()
        self.service = service
        self.redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        event = str(getattr(record, "event", "log"))
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "event": event,
            "message": self.redactor.redact(record.getMessage()),
        }
        for name, variable in _CONTEXT.items():
            value = getattr(record, name, None) or variable.get()
            if value is not None:
                payload[name] = self.redactor.redact(str(value))
        for name in ("route", "method", "status", "duration_ms", "outcome", "result"):
            value = getattr(record, name, None)
            if value is not None:
                payload[name] = value
        if record.exc_info:
            payload["exception"] = self.redactor.redact(
                self.formatException(record.exc_info)
            )
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_platform_logging(
    *,
    service: str,
    level: str,
    log_format: str,
    redactor: SensitiveValueRedactor,
) -> None:
    handler = logging.StreamHandler(sys.stderr)
    if log_format == "json":
        handler.setFormatter(JsonLogFormatter(service, redactor))
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))


class ApiMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.http_requests = Counter(
            "mewcode_platform_http_requests_total",
            "Control API HTTP requests",
            ("route", "method", "status_class"),
            registry=self.registry,
        )
        self.http_duration = Histogram(
            "mewcode_platform_http_request_duration_seconds",
            "Control API HTTP request duration",
            ("route", "method"),
            registry=self.registry,
        )
        self.readiness = Gauge(
            "mewcode_platform_readiness_check",
            "Current readiness check state",
            ("check",),
            registry=self.registry,
        )

    def observe_request(
        self, *, route: str, method: str, status_code: int, duration_seconds: float
    ) -> None:
        status_class = f"{status_code // 100}xx"
        self.http_requests.labels(route, method, status_class).inc()
        self.http_duration.labels(route, method).observe(duration_seconds)

    def set_readiness(self, checks: dict[str, bool]) -> None:
        for check, ready in checks.items():
            self.readiness.labels(check).set(1 if ready else 0)

    def render(self) -> tuple[bytes, str]:
        return generate_latest(self.registry), CONTENT_TYPE_LATEST


class WorkerMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.active_attempts = Gauge(
            "mewcode_platform_worker_active_attempts",
            "Currently active attempts",
            registry=self.registry,
        )
        self.attempts = Counter(
            "mewcode_platform_worker_attempts_total",
            "Completed Worker attempts",
            ("outcome",),
            registry=self.registry,
        )
        self.attempt_duration = Histogram(
            "mewcode_platform_worker_attempt_duration_seconds",
            "Worker attempt duration",
            ("outcome",),
            registry=self.registry,
        )
        self.lease_recoveries = Counter(
            "mewcode_platform_worker_lease_recoveries_total",
            "Recovered expired Worker leases",
            registry=self.registry,
        )
        self.janitor_failures = Counter(
            "mewcode_platform_worker_janitor_failures_total",
            "Worker janitor failures",
            registry=self.registry,
        )
        self.capacity = Gauge(
            "mewcode_platform_worker_capacity",
            "Configured platform and local Worker capacity",
            ("scope",),
            registry=self.registry,
        )
        self.draining = Gauge(
            "mewcode_platform_worker_draining",
            "Whether this Worker is draining",
            registry=self.registry,
        )
        self.queued_jobs = Gauge(
            "mewcode_platform_jobs_queued",
            "Currently queued Jobs",
            registry=self.registry,
        )
        self.oldest_queued = Gauge(
            "mewcode_platform_oldest_queued_seconds",
            "Age of the oldest queued Job",
            registry=self.registry,
        )
        self.shutdowns = Counter(
            "mewcode_platform_worker_shutdowns_total",
            "Worker shutdown outcomes",
            ("result",),
            registry=self.registry,
        )


class NotifierMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.delivery = Counter(
            "mewcode_platform_notification_delivery_total",
            "Notification delivery attempts and results",
            ("event_type", "result"),
            registry=self.registry,
        )
        self.delivery_duration = Histogram(
            "mewcode_platform_notification_delivery_duration_seconds",
            "Notification delivery duration",
            ("event_type", "result"),
            registry=self.registry,
        )
        self.backlog = Gauge(
            "mewcode_platform_notification_backlog",
            "Notification backlog by state",
            ("status",),
            registry=self.registry,
        )
        self.oldest_pending = Gauge(
            "mewcode_platform_notification_oldest_pending_seconds",
            "Age of the oldest pending or in-flight notification",
            registry=self.registry,
        )
        self.heartbeat_failures = Counter(
            "mewcode_platform_notifier_heartbeat_failures_total",
            "Notifier heartbeat failures",
            registry=self.registry,
        )

    @staticmethod
    def event_type(value: str) -> str:
        return value if value in _EVENT_TYPES else "UNKNOWN"


def start_metrics_server(port: int, registry: CollectorRegistry):
    return start_http_server(port, addr="0.0.0.0", registry=registry)
