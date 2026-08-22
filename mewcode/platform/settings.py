from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import make_url


class PlatformSettingsError(ValueError):
    pass


def _positive_int(env: dict[str, str], name: str, default: int) -> int:
    raw = env.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise PlatformSettingsError(f"{name} must be an integer") from exc
    if value <= 0:
        raise PlatformSettingsError(f"{name} must be positive")
    return value


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
    sse_poll_milliseconds: int = 500
    sse_keepalive_seconds: int = 15
    worker_id: str = ""
    repository_resolver_factory: str = ""
    attempt_processor_factory: str = ""
    log_level: str = "INFO"

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
        return cls(
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
                "MEWCODE_PLATFORM_ATTEMPT_PROCESSOR_FACTORY", ""
            ).strip(),
            log_level=env.get("MEWCODE_PLATFORM_LOG_LEVEL", "INFO").upper(),
        )

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
