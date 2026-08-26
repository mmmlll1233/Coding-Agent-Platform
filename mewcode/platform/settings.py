from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy.engine import make_url


class PlatformSettingsError(ValueError):
    pass


_IMMUTABLE_IMAGE = re.compile(r"^(?:sha256:[0-9a-f]{64}|[^\s@]+@sha256:[0-9a-f]{64})$")
_NOTIFICATION_DESTINATION = re.compile(r"^feishu:[A-Za-z0-9._-]{1,64}$")
_FEISHU_HOOK_PATH = re.compile(r"^/open-apis/bot/v2/hook/[A-Za-z0-9_-]+$")


def _positive_int(env: dict[str, str], name: str, default: int) -> int:
    raw = env.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise PlatformSettingsError(f"{name} must be an integer") from exc
    if value <= 0:
        raise PlatformSettingsError(f"{name} must be positive")
    return value


def _non_negative_int(env: dict[str, str], name: str, default: int) -> int:
    raw = env.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise PlatformSettingsError(f"{name} must be an integer") from exc
    if value < 0:
        raise PlatformSettingsError(f"{name} must be non-negative")
    return value


def _bounded_positive_int(
    env: dict[str, str], name: str, default: int, *, maximum: int
) -> int:
    value = _positive_int(env, name, default)
    if value > maximum:
        raise PlatformSettingsError(f"{name} must be <= {maximum}")
    return value


def _boolean(env: dict[str, str], name: str, default: bool = False) -> bool:
    raw = env.get(name, str(default)).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise PlatformSettingsError(f"{name} must be a boolean")


def _domains(env: dict[str, str], name: str, default: str) -> tuple[str, ...]:
    values = tuple(
        dict.fromkeys(
            item.strip().lower().rstrip(".")
            for item in env.get(name, default).split(",")
            if item.strip()
        )
    )
    domain = re.compile(
        r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
        r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
    )
    if not values or any(
        "*" in value or ":" in value or not domain.fullmatch(value) for value in values
    ):
        raise PlatformSettingsError(f"{name} must contain comma-separated DNS names")
    return values


@dataclass(frozen=True)
class PlatformSettings:
    database_url: str
    database_password_file: str = ""
    host: str = "127.0.0.1"
    port: int = 8080
    lease_seconds: int = 60
    heartbeat_seconds: int = 15
    recovery_seconds: int = 5
    worker_stale_seconds: int = 45
    max_concurrent_jobs: int = 1
    worker_max_concurrent_attempts: int = 1
    attempt_timeout_seconds: int = 3600
    worker_shutdown_grace_seconds: int = 300
    sse_poll_milliseconds: int = 500
    sse_keepalive_seconds: int = 15
    worker_id: str = ""
    repository_resolver_factory: str = ""
    attempt_processor_factory: str = (
        "mewcode.platform.processing:create_attempt_processor_factory"
    )
    github_app_client_id: str = ""
    github_private_key_file: str = ""
    github_timeout_seconds: int = 30
    max_delivery_files: int = 200
    max_delivery_bytes: int = 20 * 1024 * 1024
    max_delivery_file_bytes: int = 5 * 1024 * 1024
    llm_protocol: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key_file: str = ""
    llm_thinking: bool = False
    llm_context_window: int = 0
    llm_max_output_tokens: int = 0
    executor_image: str = ""
    proxy_image: str = ""
    state_root: str = "/var/lib/mewcode/state"
    artifact_root: str = "/var/lib/mewcode/artifacts"
    egress_network: str = "mewcode-phase2-egress"
    egress_allowlist: tuple[str, ...] = ("pypi.org", "files.pythonhosted.org")
    setup_timeout_budget_seconds: int = 600
    verification_timeout_budget_seconds: int = 600
    max_repair_rounds: int = 2
    artifact_retention_days: int = 7
    metadata_retention_days: int = 30
    janitor_interval_seconds: int = 3600
    max_artifact_bytes: int = 64 * 1024 * 1024
    max_attempt_artifact_bytes: int = 128 * 1024 * 1024
    notifications_enabled: bool = False
    notification_destination: str = "feishu:platform"
    feishu_webhook_url_file: str = ""
    feishu_signing_secret_file: str = ""
    notifier_id: str = ""
    notification_poll_milliseconds: int = 500
    notification_lease_seconds: int = 60
    notification_timeout_seconds: int = 10
    notification_backoff_base_seconds: int = 5
    notification_backoff_max_seconds: int = 900
    worker_metrics_port: int = 9091
    notifier_metrics_port: int = 9092
    log_level: str = "INFO"
    log_format: str = "text"

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> PlatformSettings:
        env = dict(os.environ if environ is None else environ)
        database_url = env.get("MEWCODE_PLATFORM_DATABASE_URL", "").strip()
        if not database_url:
            raise PlatformSettingsError("MEWCODE_PLATFORM_DATABASE_URL is required")
        if not database_url.startswith(
            ("postgresql+asyncpg://", "postgresql://", "postgres://")
        ):
            raise PlatformSettingsError(
                "MEWCODE_PLATFORM_DATABASE_URL must use PostgreSQL"
            )
        port = _positive_int(env, "MEWCODE_PLATFORM_PORT", 8080)
        if port > 65535:
            raise PlatformSettingsError("MEWCODE_PLATFORM_PORT must be <= 65535")
        lease_seconds = _positive_int(env, "MEWCODE_PLATFORM_LEASE_SECONDS", 60)
        heartbeat_seconds = _positive_int(env, "MEWCODE_PLATFORM_HEARTBEAT_SECONDS", 15)
        if heartbeat_seconds >= lease_seconds:
            raise PlatformSettingsError(
                "MEWCODE_PLATFORM_HEARTBEAT_SECONDS must be less than the lease"
            )
        settings = cls(
            database_url=database_url,
            database_password_file=env.get(
                "MEWCODE_PLATFORM_DATABASE_PASSWORD_FILE", ""
            ).strip(),
            host=env.get("MEWCODE_PLATFORM_HOST", "127.0.0.1"),
            port=port,
            lease_seconds=lease_seconds,
            heartbeat_seconds=heartbeat_seconds,
            recovery_seconds=_positive_int(env, "MEWCODE_PLATFORM_RECOVERY_SECONDS", 5),
            worker_stale_seconds=_positive_int(
                env, "MEWCODE_PLATFORM_WORKER_STALE_SECONDS", 45
            ),
            max_concurrent_jobs=_positive_int(
                env, "MEWCODE_PLATFORM_MAX_CONCURRENT_JOBS", 1
            ),
            worker_max_concurrent_attempts=_positive_int(
                env, "MEWCODE_PLATFORM_WORKER_MAX_CONCURRENT_ATTEMPTS", 1
            ),
            attempt_timeout_seconds=_bounded_positive_int(
                env,
                "MEWCODE_PLATFORM_ATTEMPT_TIMEOUT_SECONDS",
                3600,
                maximum=3600,
            ),
            worker_shutdown_grace_seconds=_bounded_positive_int(
                env,
                "MEWCODE_PLATFORM_WORKER_SHUTDOWN_GRACE_SECONDS",
                300,
                maximum=3600,
            ),
            sse_poll_milliseconds=_positive_int(
                env, "MEWCODE_PLATFORM_SSE_POLL_MILLISECONDS", 500
            ),
            sse_keepalive_seconds=_positive_int(
                env, "MEWCODE_PLATFORM_SSE_KEEPALIVE_SECONDS", 15
            ),
            worker_id=env.get("MEWCODE_PLATFORM_WORKER_ID", "").strip(),
            repository_resolver_factory=env.get(
                "MEWCODE_PLATFORM_REPOSITORY_RESOLVER_FACTORY", ""
            ).strip(),
            attempt_processor_factory=env.get(
                "MEWCODE_PLATFORM_ATTEMPT_PROCESSOR_FACTORY",
                "mewcode.platform.processing:create_attempt_processor_factory",
            ).strip(),
            github_app_client_id=env.get(
                "MEWCODE_PLATFORM_GITHUB_APP_CLIENT_ID", ""
            ).strip(),
            github_private_key_file=env.get(
                "MEWCODE_PLATFORM_GITHUB_PRIVATE_KEY_FILE", ""
            ).strip(),
            github_timeout_seconds=_positive_int(
                env, "MEWCODE_PLATFORM_GITHUB_TIMEOUT_SECONDS", 30
            ),
            max_delivery_files=_positive_int(
                env, "MEWCODE_PLATFORM_MAX_DELIVERY_FILES", 200
            ),
            max_delivery_bytes=_positive_int(
                env, "MEWCODE_PLATFORM_MAX_DELIVERY_BYTES", 20 * 1024 * 1024
            ),
            max_delivery_file_bytes=_positive_int(
                env, "MEWCODE_PLATFORM_MAX_DELIVERY_FILE_BYTES", 5 * 1024 * 1024
            ),
            llm_protocol=env.get("MEWCODE_PLATFORM_LLM_PROTOCOL", "").strip(),
            llm_base_url=env.get("MEWCODE_PLATFORM_LLM_BASE_URL", "").strip(),
            llm_model=env.get("MEWCODE_PLATFORM_LLM_MODEL", "").strip(),
            llm_api_key_file=env.get("MEWCODE_PLATFORM_LLM_API_KEY_FILE", "").strip(),
            llm_thinking=_boolean(env, "MEWCODE_PLATFORM_LLM_THINKING"),
            llm_context_window=_non_negative_int(
                env, "MEWCODE_PLATFORM_LLM_CONTEXT_WINDOW", 0
            ),
            llm_max_output_tokens=_non_negative_int(
                env, "MEWCODE_PLATFORM_LLM_MAX_OUTPUT_TOKENS", 0
            ),
            executor_image=env.get("MEWCODE_PLATFORM_EXECUTOR_IMAGE", "").strip(),
            proxy_image=env.get("MEWCODE_PLATFORM_PROXY_IMAGE", "").strip(),
            state_root=env.get(
                "MEWCODE_PLATFORM_STATE_ROOT", "/var/lib/mewcode/state"
            ).strip(),
            artifact_root=env.get(
                "MEWCODE_PLATFORM_ARTIFACT_ROOT", "/var/lib/mewcode/artifacts"
            ).strip(),
            egress_network=env.get(
                "MEWCODE_PLATFORM_EGRESS_NETWORK", "mewcode-phase2-egress"
            ).strip(),
            egress_allowlist=_domains(
                env,
                "MEWCODE_PLATFORM_EGRESS_ALLOWLIST",
                "pypi.org,files.pythonhosted.org",
            ),
            setup_timeout_budget_seconds=_positive_int(
                env, "MEWCODE_PLATFORM_SETUP_TIMEOUT_BUDGET_SECONDS", 600
            ),
            verification_timeout_budget_seconds=_positive_int(
                env, "MEWCODE_PLATFORM_VERIFICATION_TIMEOUT_BUDGET_SECONDS", 600
            ),
            max_repair_rounds=_non_negative_int(
                env, "MEWCODE_PLATFORM_MAX_REPAIR_ROUNDS", 2
            ),
            artifact_retention_days=_positive_int(
                env, "MEWCODE_PLATFORM_ARTIFACT_RETENTION_DAYS", 7
            ),
            metadata_retention_days=_positive_int(
                env, "MEWCODE_PLATFORM_METADATA_RETENTION_DAYS", 30
            ),
            janitor_interval_seconds=_positive_int(
                env, "MEWCODE_PLATFORM_JANITOR_INTERVAL_SECONDS", 3600
            ),
            max_artifact_bytes=_positive_int(
                env, "MEWCODE_PLATFORM_MAX_ARTIFACT_BYTES", 64 * 1024 * 1024
            ),
            max_attempt_artifact_bytes=_positive_int(
                env,
                "MEWCODE_PLATFORM_MAX_ATTEMPT_ARTIFACT_BYTES",
                128 * 1024 * 1024,
            ),
            notifications_enabled=_boolean(
                env, "MEWCODE_PLATFORM_NOTIFICATIONS_ENABLED"
            ),
            notification_destination=env.get(
                "MEWCODE_PLATFORM_NOTIFICATION_DESTINATION", "feishu:platform"
            ).strip(),
            feishu_webhook_url_file=env.get(
                "MEWCODE_PLATFORM_FEISHU_WEBHOOK_URL_FILE", ""
            ).strip(),
            feishu_signing_secret_file=env.get(
                "MEWCODE_PLATFORM_FEISHU_SIGNING_SECRET_FILE", ""
            ).strip(),
            notifier_id=env.get("MEWCODE_PLATFORM_NOTIFIER_ID", "").strip(),
            notification_poll_milliseconds=_positive_int(
                env, "MEWCODE_PLATFORM_NOTIFICATION_POLL_MILLISECONDS", 500
            ),
            notification_lease_seconds=_positive_int(
                env, "MEWCODE_PLATFORM_NOTIFICATION_LEASE_SECONDS", 60
            ),
            notification_timeout_seconds=_positive_int(
                env, "MEWCODE_PLATFORM_NOTIFICATION_TIMEOUT_SECONDS", 10
            ),
            notification_backoff_base_seconds=_positive_int(
                env, "MEWCODE_PLATFORM_NOTIFICATION_BACKOFF_BASE_SECONDS", 5
            ),
            notification_backoff_max_seconds=_positive_int(
                env, "MEWCODE_PLATFORM_NOTIFICATION_BACKOFF_MAX_SECONDS", 900
            ),
            worker_metrics_port=_positive_int(
                env, "MEWCODE_PLATFORM_WORKER_METRICS_PORT", 9091
            ),
            notifier_metrics_port=_positive_int(
                env, "MEWCODE_PLATFORM_NOTIFIER_METRICS_PORT", 9092
            ),
            log_level=env.get("MEWCODE_PLATFORM_LOG_LEVEL", "INFO").upper(),
            log_format=env.get("MEWCODE_PLATFORM_LOG_FORMAT", "text").lower(),
        )
        if (
            settings.notification_backoff_base_seconds
            > settings.notification_backoff_max_seconds
        ):
            raise PlatformSettingsError(
                "MEWCODE_PLATFORM_NOTIFICATION_BACKOFF_BASE_SECONDS must not exceed the maximum"
            )
        if (
            settings.notification_timeout_seconds
            >= settings.notification_lease_seconds
        ):
            raise PlatformSettingsError(
                "MEWCODE_PLATFORM_NOTIFICATION_TIMEOUT_SECONDS must be less than the notification lease"
            )
        if settings.worker_max_concurrent_attempts > settings.max_concurrent_jobs:
            raise PlatformSettingsError(
                "MEWCODE_PLATFORM_WORKER_MAX_CONCURRENT_ATTEMPTS must not exceed "
                "MEWCODE_PLATFORM_MAX_CONCURRENT_JOBS"
            )
        if not _NOTIFICATION_DESTINATION.fullmatch(settings.notification_destination):
            raise PlatformSettingsError(
                "MEWCODE_PLATFORM_NOTIFICATION_DESTINATION must be a logical feishu destination"
            )
        if (
            settings.worker_metrics_port > 65535
            or settings.notifier_metrics_port > 65535
        ):
            raise PlatformSettingsError("Platform metrics ports must be <= 65535")
        if settings.log_format not in {"json", "text"}:
            raise PlatformSettingsError(
                "MEWCODE_PLATFORM_LOG_FORMAT must be json or text"
            )
        return settings

    def validate_worker(self) -> None:
        required = {
            "MEWCODE_PLATFORM_LLM_PROTOCOL": self.llm_protocol,
            "MEWCODE_PLATFORM_LLM_BASE_URL": self.llm_base_url,
            "MEWCODE_PLATFORM_LLM_MODEL": self.llm_model,
            "MEWCODE_PLATFORM_LLM_API_KEY_FILE": self.llm_api_key_file,
            "MEWCODE_PLATFORM_EXECUTOR_IMAGE": self.executor_image,
            "MEWCODE_PLATFORM_PROXY_IMAGE": self.proxy_image,
            "MEWCODE_PLATFORM_GITHUB_APP_CLIENT_ID": self.github_app_client_id,
            "MEWCODE_PLATFORM_GITHUB_PRIVATE_KEY_FILE": self.github_private_key_file,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise PlatformSettingsError(
                "Worker requires: " + ", ".join(sorted(missing))
            )
        if self.llm_protocol not in {"anthropic", "openai", "openai-compat"}:
            raise PlatformSettingsError(
                "MEWCODE_PLATFORM_LLM_PROTOCOL must be anthropic, openai, or openai-compat"
            )
        for name, image in (
            ("MEWCODE_PLATFORM_EXECUTOR_IMAGE", self.executor_image),
            ("MEWCODE_PLATFORM_PROXY_IMAGE", self.proxy_image),
        ):
            if not _IMMUTABLE_IMAGE.fullmatch(image):
                raise PlatformSettingsError(
                    f"{name} must use an immutable sha256 digest"
                )
        for name, root in (
            ("MEWCODE_PLATFORM_STATE_ROOT", self.state_root),
            ("MEWCODE_PLATFORM_ARTIFACT_ROOT", self.artifact_root),
        ):
            if not root or not Path(root).is_absolute():
                raise PlatformSettingsError(f"{name} must be an absolute path")
        if self.max_attempt_artifact_bytes < self.max_artifact_bytes:
            raise PlatformSettingsError(
                "MEWCODE_PLATFORM_MAX_ATTEMPT_ARTIFACT_BYTES must be at least the single Artifact limit"
            )
        if self.setup_timeout_budget_seconds > 600:
            raise PlatformSettingsError(
                "MEWCODE_PLATFORM_SETUP_TIMEOUT_BUDGET_SECONDS must not exceed 600"
            )
        if self.verification_timeout_budget_seconds > 600:
            raise PlatformSettingsError(
                "MEWCODE_PLATFORM_VERIFICATION_TIMEOUT_BUDGET_SECONDS must not exceed 600"
            )
        if self.max_repair_rounds > 2:
            raise PlatformSettingsError(
                "MEWCODE_PLATFORM_MAX_REPAIR_ROUNDS must not exceed 2"
            )
        for name, path in (
            ("MEWCODE_PLATFORM_LLM_API_KEY_FILE", self.llm_api_key_file),
            ("MEWCODE_PLATFORM_GITHUB_PRIVATE_KEY_FILE", self.github_private_key_file),
        ):
            try:
                value = Path(path).read_text(encoding="utf-8").strip()
            except OSError as error:
                raise PlatformSettingsError(f"{name} cannot be read") from error
            if not value:
                raise PlatformSettingsError(f"{name} is empty")

    def notifier_secrets(self) -> tuple[str, str]:
        required = {
            "MEWCODE_PLATFORM_FEISHU_WEBHOOK_URL_FILE": self.feishu_webhook_url_file,
            "MEWCODE_PLATFORM_FEISHU_SIGNING_SECRET_FILE": self.feishu_signing_secret_file,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise PlatformSettingsError(
                "Notifier requires: " + ", ".join(sorted(missing))
            )
        values: list[str] = []
        for name, path in required.items():
            try:
                value = Path(path).read_text(encoding="utf-8").strip()
            except OSError as error:
                raise PlatformSettingsError(f"{name} cannot be read") from error
            if not value:
                raise PlatformSettingsError(f"{name} is empty")
            values.append(value)
        return values[0], values[1]

    def validate_notifier(self) -> tuple[str, str]:
        if not self.notifications_enabled:
            raise PlatformSettingsError(
                "MEWCODE_PLATFORM_NOTIFICATIONS_ENABLED must be true for Notifier startup"
            )
        webhook_url, signing_secret = self.notifier_secrets()
        parsed = urlsplit(webhook_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "open.feishu.cn"
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not _FEISHU_HOOK_PATH.fullmatch(parsed.path)
        ):
            raise PlatformSettingsError(
                "MEWCODE_PLATFORM_FEISHU_WEBHOOK_URL_FILE must contain an approved Feishu V2 webhook"
            )
        return webhook_url, signing_secret

    @property
    def async_database_url(self) -> str:
        if self.database_url.startswith("postgresql+asyncpg://"):
            url = self.database_url
        elif self.database_url.startswith("postgres://"):
            url = "postgresql+asyncpg://" + self.database_url[len("postgres://") :]
        else:
            url = "postgresql+asyncpg://" + self.database_url[len("postgresql://") :]
        if self.database_password_file:
            try:
                password = (
                    Path(self.database_password_file)
                    .read_text(encoding="utf-8")
                    .strip()
                )
            except OSError as error:
                raise PlatformSettingsError(
                    "MEWCODE_PLATFORM_DATABASE_PASSWORD_FILE cannot be read"
                ) from error
            if not password:
                raise PlatformSettingsError(
                    "MEWCODE_PLATFORM_DATABASE_PASSWORD_FILE is empty"
                )
            url = (
                make_url(url)
                .set(password=password)
                .render_as_string(hide_password=False)
            )
        return url
