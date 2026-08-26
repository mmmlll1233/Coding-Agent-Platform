from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import os
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

import uvicorn
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url

from mewcode.platform.api import PlatformComponents, create_app
from mewcode.platform.artifacts import LocalArtifactStore
from mewcode.platform.execution import (
    SensitiveValueRedactor,
    shared_platform_redactor,
)
from mewcode.platform.notifications import FeishuWebhookClient, NotifierService
from mewcode.platform.observability import (
    ApiMetrics,
    NotifierMetrics,
    WorkerMetrics,
    configure_platform_logging,
    start_metrics_server,
)
from mewcode.platform.persistence import PlatformRepository, create_database
from mewcode.platform.settings import PlatformSettings, PlatformSettingsError
from mewcode.platform.workers import WorkerService


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mewcode-platform")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("api", help="Run the Control API")
    subcommands.add_parser("worker", help="Run the Job Worker")
    subcommands.add_parser("notifier", help="Run the Feishu notification service")

    database = subcommands.add_parser("db", help="Manage the PostgreSQL schema")
    database_subcommands = database.add_subparsers(dest="db_command", required=True)
    database_subcommands.add_parser("upgrade", help="Apply all migrations")
    database_subcommands.add_parser(
        "grant-runtime", help="Grant least-privilege access to Compose runtime roles"
    )

    api_key = subcommands.add_parser("api-key", help="Manage Requester API Keys")
    key_subcommands = api_key.add_subparsers(dest="key_command", required=True)
    create = key_subcommands.add_parser("create", help="Create an API Key")
    create.add_argument("--tenant", default="default")
    create.add_argument("--requester", required=True)
    revoke = key_subcommands.add_parser("revoke", help="Revoke an API Key")
    revoke.add_argument("key_id", type=UUID)
    return parser


def _load_component(
    path: str, settings: PlatformSettings, **factory_options: Any
) -> Any:
    module_name, separator, attribute_name = path.partition(":")
    if not separator or not module_name or not attribute_name:
        raise RuntimeError(
            "Component paths must use the 'package.module:factory' format"
        )
    factory = getattr(importlib.import_module(module_name), attribute_name)
    signature = inspect.signature(factory)
    accepts_options = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    supported_options = (
        factory_options
        if accepts_options
        else {
            name: value
            for name, value in factory_options.items()
            if name in signature.parameters
        }
    )
    component = factory(settings, **supported_options)
    if component is None:
        raise RuntimeError(f"Component factory {path} returned None")
    return component


def _alembic_config(settings: PlatformSettings) -> Config:
    config = Config()
    migrations = Path(__file__).resolve().parent / "persistence" / "migrations"
    config.set_main_option("script_location", str(migrations))
    config.set_main_option("sqlalchemy.url", settings.async_database_url)
    return config


def _secret_redactor(settings: PlatformSettings) -> SensitiveValueRedactor:
    secrets: list[str] = [settings.database_url]
    password = make_url(settings.async_database_url).password
    if password:
        secrets.append(password)
    for name, value in os.environ.items():
        upper = name.upper()
        if (
            value
            and len(value) >= 8
            and upper.endswith(("_KEY", "_TOKEN", "_SECRET", "_PASSWORD"))
        ):
            secrets.append(value)
    if settings.github_private_key_file:
        try:
            secrets.append(
                Path(settings.github_private_key_file).read_text(encoding="utf-8")
            )
        except OSError:
            pass
    if settings.llm_api_key_file:
        try:
            secrets.append(Path(settings.llm_api_key_file).read_text(encoding="utf-8"))
        except OSError:
            pass
    for secret_file in (
        settings.feishu_webhook_url_file,
        settings.feishu_signing_secret_file,
    ):
        if secret_file:
            try:
                secrets.append(Path(secret_file).read_text(encoding="utf-8").strip())
            except OSError:
                pass
    redactor = shared_platform_redactor()
    redactor.add(*secrets)
    return redactor


async def _create_key(settings: PlatformSettings, tenant: str, requester: str) -> int:
    database = create_database(settings)
    try:
        token, principal = await PlatformRepository(database).create_api_key(
            tenant_name=tenant, requester_name=requester
        )
    finally:
        await database.aclose()
    print(f"key_id={principal.key_id}")
    print(f"api_key={token}")
    print("This API Key will not be shown again.")
    return 0


async def _revoke_key(settings: PlatformSettings, key_id: UUID) -> int:
    database = create_database(settings)
    try:
        revoked = await PlatformRepository(database).revoke_api_key(key_id)
    finally:
        await database.aclose()
    if not revoked:
        print("API Key was not found or was already revoked.", file=sys.stderr)
        return 1
    print(f"Revoked API Key {key_id}")
    return 0


async def _grant_runtime_roles(settings: PlatformSettings) -> int:
    database = create_database(settings)
    statements = (
        "GRANT USAGE ON SCHEMA public TO mewcode_api, mewcode_worker, mewcode_notifier",
        "GRANT SELECT ON alembic_version TO mewcode_api",
        "GRANT SELECT ON tenants, requesters, api_keys, worker_nodes, artifacts TO mewcode_api",
        "GRANT SELECT, INSERT, UPDATE ON jobs, attempts, job_inputs, job_events TO mewcode_api",
        "GRANT SELECT, INSERT, UPDATE ON jobs, attempts, job_events, worker_nodes TO mewcode_worker",
        "GRANT SELECT, INSERT, DELETE ON artifacts TO mewcode_worker",
        "GRANT DELETE ON jobs TO mewcode_worker",
        "GRANT SELECT ON job_inputs TO mewcode_worker",
        "GRANT INSERT ON notification_outbox TO mewcode_api, mewcode_worker",
        "GRANT SELECT (status, created_at) ON notification_outbox TO mewcode_api",
        "GRANT SELECT (job_id, status) ON notification_outbox TO mewcode_worker",
        "GRANT SELECT, UPDATE ON notification_outbox TO mewcode_notifier",
        "GRANT SELECT, INSERT, UPDATE ON worker_nodes TO mewcode_notifier",
    )
    try:
        async with database.engine.begin() as connection:
            for statement in statements:
                await connection.execute(text(statement))
    finally:
        await database.aclose()
    return 0


async def _run_worker(settings: PlatformSettings) -> int:
    if not settings.attempt_processor_factory:
        raise RuntimeError(
            "MEWCODE_PLATFORM_ATTEMPT_PROCESSOR_FACTORY is required for Worker startup"
        )
    settings.validate_worker()
    database = create_database(settings)
    redactor = _secret_redactor(settings)
    configure_platform_logging(
        service="worker",
        level=settings.log_level,
        log_format=settings.log_format,
        redactor=redactor,
    )
    repository = PlatformRepository(
        database,
        metadata_retention_days=settings.metadata_retention_days,
        notifications_enabled=settings.notifications_enabled,
        notification_destination=settings.notification_destination,
        redactor=redactor,
    )
    metrics = WorkerMetrics()
    metrics_server, _ = start_metrics_server(
        settings.worker_metrics_port, metrics.registry
    )
    processor_factory = _load_component(
        settings.attempt_processor_factory,
        settings,
        repository=repository,
        redactor=redactor,
    )
    service = WorkerService(
        settings,
        repository,
        processor_factory,
        redactor=redactor,
        metrics=metrics,
    )
    try:
        await service.run_forever()
    finally:
        metrics_server.shutdown()
        metrics_server.server_close()
        await database.aclose()
    return 0


async def _run_notifier(settings: PlatformSettings) -> int:
    webhook_url, signing_secret = settings.validate_notifier()
    redactor = _secret_redactor(settings)
    redactor.add(webhook_url, signing_secret)
    configure_platform_logging(
        service="notifier",
        level=settings.log_level,
        log_format=settings.log_format,
        redactor=redactor,
    )
    database = create_database(settings)
    repository = PlatformRepository(
        database,
        notifications_enabled=True,
        notification_destination=settings.notification_destination,
        redactor=redactor,
    )
    metrics = NotifierMetrics()
    metrics_server, _ = start_metrics_server(
        settings.notifier_metrics_port, metrics.registry
    )
    client = FeishuWebhookClient(
        webhook_url,
        signing_secret,
        timeout_seconds=settings.notification_timeout_seconds,
        redactor=redactor,
    )
    service = NotifierService(
        settings, repository, client, redactor=redactor, metrics=metrics
    )
    try:
        await service.run_forever()
    finally:
        metrics_server.shutdown()
        metrics_server.server_close()
        await client.aclose()
        await database.aclose()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = PlatformSettings.from_env()
        redactor = _secret_redactor(settings)
        configure_platform_logging(
            service=args.command,
            level=settings.log_level,
            log_format=settings.log_format,
            redactor=redactor,
        )
        if args.command == "db":
            if args.db_command == "upgrade":
                command.upgrade(_alembic_config(settings), "head")
                return 0
            return asyncio.run(_grant_runtime_roles(settings))
        if args.command == "api-key":
            if args.key_command == "create":
                return asyncio.run(_create_key(settings, args.tenant, args.requester))
            return asyncio.run(_revoke_key(settings, args.key_id))
        if args.command == "worker":
            return asyncio.run(_run_worker(settings))
        if args.command == "notifier":
            return asyncio.run(_run_notifier(settings))

        resolver = (
            _load_component(settings.repository_resolver_factory, settings)
            if settings.repository_resolver_factory
            else None
        )
        database = create_database(settings)
        metrics = ApiMetrics()
        app = create_app(
            components=PlatformComponents(
                settings=settings,
                database=database,
                repository=PlatformRepository(
                    database,
                    metadata_retention_days=settings.metadata_retention_days,
                    notifications_enabled=settings.notifications_enabled,
                    notification_destination=settings.notification_destination,
                    redactor=redactor,
                ),
                resolver=resolver,
                artifact_store=LocalArtifactStore(settings.artifact_root),
                metrics=metrics,
            )
        )
        uvicorn.run(
            app,
            host=settings.host,
            port=settings.port,
            log_level=settings.log_level.lower(),
            access_log=False,
            log_config=None,
        )
        return 0
    except (PlatformSettingsError, RuntimeError) as error:
        print(f"mewcode-platform: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
