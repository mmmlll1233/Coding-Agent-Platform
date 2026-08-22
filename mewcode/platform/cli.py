from __future__ import annotations

import argparse
import asyncio
import importlib
import logging
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
from mewcode.platform.execution import (
    SensitiveValueRedactor,
    shared_platform_redactor,
)
from mewcode.platform.persistence import PlatformRepository, create_database
from mewcode.platform.settings import PlatformSettings, PlatformSettingsError
from mewcode.platform.workers import WorkerService


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mewcode-platform")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("api", help="Run the Control API")
    subcommands.add_parser("worker", help="Run the Job Worker")

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


def _load_component(path: str, settings: PlatformSettings) -> Any:
    module_name, separator, attribute_name = path.partition(":")
    if not separator or not module_name or not attribute_name:
        raise RuntimeError(
            "Component paths must use the 'package.module:factory' format"
        )
    factory = getattr(importlib.import_module(module_name), attribute_name)
    component = factory(settings)
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
                Path(settings.github_private_key_file).read_text(
                    encoding="utf-8"
                )
            )
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
        "GRANT USAGE ON SCHEMA public TO mewcode_api, mewcode_worker",
        "GRANT SELECT ON alembic_version TO mewcode_api",
        "GRANT SELECT ON tenants, requesters, api_keys, worker_nodes TO mewcode_api",
        "GRANT SELECT, INSERT, UPDATE ON jobs, attempts, job_inputs, job_events TO mewcode_api",
        "GRANT SELECT, INSERT, UPDATE ON jobs, attempts, job_events, worker_nodes TO mewcode_worker",
        "GRANT SELECT ON job_inputs TO mewcode_worker",
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
    processor_factory = _load_component(settings.attempt_processor_factory, settings)
    database = create_database(settings)
    service = WorkerService(
        settings,
        PlatformRepository(database),
        processor_factory,
        redactor=_secret_redactor(settings),
    )
    try:
        await service.run_forever()
    finally:
        await database.aclose()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = PlatformSettings.from_env()
        logging.basicConfig(
            level=getattr(logging, settings.log_level, logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
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

        resolver = (
            _load_component(settings.repository_resolver_factory, settings)
            if settings.repository_resolver_factory
            else None
        )
        database = create_database(settings)
        app = create_app(
            components=PlatformComponents(
                settings=settings,
                database=database,
                repository=PlatformRepository(database),
                resolver=resolver,
            )
        )
        uvicorn.run(
            app,
            host=settings.host,
            port=settings.port,
            log_level=settings.log_level.lower(),
            access_log=False,
        )
        return 0
    except (PlatformSettingsError, RuntimeError) as error:
        print(f"mewcode-platform: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
