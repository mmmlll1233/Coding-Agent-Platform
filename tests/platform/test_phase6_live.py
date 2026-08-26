from __future__ import annotations

import asyncio
import os

import pytest
from alembic import command
from sqlalchemy import func, select, text, update

from mewcode.platform.cli import _alembic_config
from mewcode.platform.domain import RepositoryTarget
from mewcode.platform.execution import SensitiveValueRedactor
from mewcode.platform.notifications import FeishuWebhookClient, NotifierService
from mewcode.platform.persistence import PlatformRepository, create_database
from mewcode.platform.persistence.orm import NotificationOutboxRow
from mewcode.platform.settings import PlatformSettings

pytestmark = [pytest.mark.platform_phase6_live, pytest.mark.asyncio]


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise AssertionError(f"{name} is required for the Phase 6 live gate")
    return value


async def test_phase6_feishu_delivery_and_notifier_restart_recovery() -> None:
    gate_id = _required("MEWCODE_PHASE6_GATE_ID")
    settings = PlatformSettings.from_env(
        {
            "MEWCODE_PLATFORM_DATABASE_URL": _required("MEWCODE_TEST_DATABASE_URL"),
            "MEWCODE_PLATFORM_NOTIFICATIONS_ENABLED": "true",
            "MEWCODE_PLATFORM_FEISHU_WEBHOOK_URL_FILE": _required(
                "MEWCODE_TEST_FEISHU_WEBHOOK_URL_FILE"
            ),
            "MEWCODE_PLATFORM_FEISHU_SIGNING_SECRET_FILE": _required(
                "MEWCODE_TEST_FEISHU_SIGNING_SECRET_FILE"
            ),
            "MEWCODE_PLATFORM_NOTIFIER_ID": f"phase6-restarted-{gate_id}"[:128],
            "MEWCODE_PLATFORM_NOTIFICATION_LEASE_SECONDS": "60",
            "MEWCODE_PLATFORM_NOTIFICATION_TIMEOUT_SECONDS": "10",
        }
    )
    webhook_url, signing_secret = settings.validate_notifier()
    redactor = SensitiveValueRedactor((webhook_url, signing_secret))
    database = create_database(settings)
    await asyncio.to_thread(command.upgrade, _alembic_config(settings), "head")
    repository = PlatformRepository(
        database,
        notifications_enabled=True,
        notification_destination=settings.notification_destination,
        redactor=redactor,
    )
    client = FeishuWebhookClient(
        webhook_url,
        signing_secret,
        timeout_seconds=settings.notification_timeout_seconds,
        redactor=redactor,
    )
    try:
        async with database.engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE notification_outbox, artifacts, job_events, "
                    "job_inputs, attempts, jobs, api_keys, requesters, tenants, "
                    "worker_nodes CASCADE"
                )
            )
        _, principal = await repository.create_api_key(
            tenant_name="phase6-live", requester_name=f"gate-{gate_id}"[:128]
        )
        job, replayed = await repository.create_job(
            principal=principal,
            idempotency_key=f"phase6-{gate_id}"[:128],
            request_hash=(gate_id * 64)[:64].ljust(64, "6"),
            target=RepositoryTarget(
                6, "mewcode-live", "notification-gate", "main", "a" * 40
            ),
            work_request={
                "kind": "test",
                "title": f"Phase 6 live gate {gate_id}"[:200],
                "description": "Protected Feishu delivery check",
            },
            execution_contract={"verification_commands": []},
        )
        assert not replayed

        abandoned = await repository.claim_notification(
            notifier_id="phase6-crashed-notifier", lease_seconds=60
        )
        assert abandoned is not None and abandoned.job_id == job.id
        async with database.sessions.begin() as session:
            await session.execute(
                update(NotificationOutboxRow)
                .where(NotificationOutboxRow.id == abandoned.id)
                .values(lease_expires_at=func.now() - text("interval '1 second'"))
            )

        restarted = NotifierService(
            settings, repository, client, redactor=redactor, random_source=lambda: 0.5
        )
        assert await restarted.deliver_once()
        async with database.sessions() as session:
            delivered = await session.scalar(
                select(NotificationOutboxRow).where(
                    NotificationOutboxRow.id == abandoned.id
                )
            )
        assert delivered is not None
        assert delivered.status == "DELIVERED"
        assert delivered.attempt_count == 2
        assert delivered.delivered_at is not None
        observable = str(
            {
                "destination": delivered.destination,
                "payload": delivered.payload,
                "last_error": delivered.last_error,
            }
        )
        assert webhook_url not in observable
        assert signing_secret not in observable
        assert gate_id in observable
    finally:
        await client.aclose()
        async with database.engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE notification_outbox, artifacts, job_events, "
                    "job_inputs, attempts, jobs, api_keys, requesters, tenants, "
                    "worker_nodes CASCADE"
                )
            )
        await database.aclose()
