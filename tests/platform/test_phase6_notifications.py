from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from uuid import UUID

import httpx
import pytest
import yaml

from mewcode.platform.api import PlatformComponents, create_app
from mewcode.platform.execution import SensitiveValueRedactor
from mewcode.platform.notifications import (
    FeishuDeliveryError,
    FeishuWebhookClient,
    NotifierService,
    build_feishu_card,
    feishu_signature,
    validate_feishu_webhook_url,
)
from mewcode.platform.observability import (
    JsonLogFormatter,
    NotifierMetrics,
    log_context,
)
from mewcode.platform.persistence import ClaimedNotification, NotificationOutboxStats
from mewcode.platform.settings import PlatformSettings, PlatformSettingsError

_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/phase6-test-hook"


def _envelope(event_type: str = "SUCCEEDED") -> dict:
    return {
        "schema_version": 1,
        "notification_id": "00000000-0000-0000-0000-000000000601",
        "job_id": "00000000-0000-0000-0000-000000000602",
        "source_event_sequence": 5,
        "event_type": event_type,
        "status": event_type,
        "repository": {
            "owner": "<at user_id=all>everyone</at>",
            "name": "service **unsafe**",
            "base_ref": "main",
        },
        "title": "Fix <at id=all>all</at> **markdown**",
        "attempt_no": 2,
        "pr_url": "https://github.com/company/service/pull/6",
    }


def test_feishu_signature_fixed_vector_and_webhook_allowlist() -> None:
    assert (
        feishu_signature(1_700_000_000, "phase6-signing-secret")
        == "Q5DaHNhuRu3hd5FK+cv/r2HYvkSTNx6+gNIdlz06W3c="
    )
    assert validate_feishu_webhook_url(_WEBHOOK) == _WEBHOOK
    for invalid in (
        "http://open.feishu.cn/open-apis/bot/v2/hook/token",
        "https://evil.example/open-apis/bot/v2/hook/token",
        "https://open.feishu.cn.evil.example/open-apis/bot/v2/hook/token",
        "https://open.feishu.cn/open-apis/bot/v2/hook/token?next=evil",
        "https://user@open.feishu.cn/open-apis/bot/v2/hook/token",
    ):
        with pytest.raises(ValueError):
            validate_feishu_webhook_url(invalid)


def test_feishu_cards_keep_untrusted_fields_plain_and_bound_lengths() -> None:
    card = build_feishu_card(_envelope())
    serialized = json.dumps(card)
    assert "00000000-0000-0000-0000-000000000601" in serialized
    text_nodes = [
        element["text"] for element in card["elements"] if element["tag"] == "div"
    ]
    assert text_nodes
    assert all(node["tag"] == "plain_text" for node in text_nodes)
    action = card["elements"][-1]
    assert action["tag"] == "action"
    assert action["actions"][0]["url"].startswith("https://github.com/")

    unsafe = _envelope()
    unsafe["pr_url"] = "https://evil.example/phish"
    assert all(element["tag"] != "action" for element in build_feishu_card(unsafe)["elements"])

    failed = _envelope("FAILED")
    failed["error_summary"] = "x" * 2_000
    failed_card = build_feishu_card(failed)
    error_node = next(
        element["text"]["content"]
        for element in failed_card["elements"]
        if element["text"]["content"].startswith("Error:")
    )
    assert len(error_node) <= 1_007


@pytest.mark.parametrize(
    "event_type",
    ["JOB_ACCEPTED", "NEEDS_INPUT", "SUCCEEDED", "FAILED", "CANCELLED"],
)
def test_all_five_notification_cards_have_stable_identity(event_type: str) -> None:
    card = build_feishu_card(_envelope(event_type))
    serialized = json.dumps(card)
    assert "00000000-0000-0000-0000-000000000601" in serialized
    assert card["header"]["title"]["tag"] == "plain_text"


@pytest.mark.asyncio
@pytest.mark.parametrize("response_body", [{"code": 0}, {"StatusCode": 0}])
async def test_feishu_accepts_only_documented_success_envelopes(response_body) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=response_body)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, follow_redirects=False, trust_env=False
    ) as http_client:
        client = FeishuWebhookClient(
            _WEBHOOK,
            "phase6-signing-secret",
            client=http_client,
        )
        await client.send(_envelope())
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body["msg_type"] == "interactive"
    assert body["card"]["header"]["title"]["tag"] == "plain_text"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "retry_after"),
    [
        (302, {"code": 0}, None),
        (429, {"code": 0}, 23.0),
        (503, {"code": 0}, None),
        (200, {"code": 19001}, None),
        (200, {"unexpected": True}, None),
    ],
)
async def test_feishu_retries_http_and_business_failures(
    status: int, body: dict, retry_after: float | None
) -> None:
    headers = {"Retry-After": "23"} if retry_after is not None else {}
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status, json=body, headers=headers)
    )
    async with httpx.AsyncClient(
        transport=transport, follow_redirects=False, trust_env=False
    ) as http_client:
        client = FeishuWebhookClient(
            _WEBHOOK,
            "phase6-signing-secret",
            client=http_client,
        )
        with pytest.raises(FeishuDeliveryError) as captured:
            await client.send(_envelope())
    assert captured.value.retry_after_seconds == retry_after
    assert _WEBHOOK not in str(captured.value)


def test_notifier_settings_validate_secrets_only_for_notifier(tmp_path) -> None:
    base = {"MEWCODE_PLATFORM_DATABASE_URL": "postgresql://db/platform"}
    settings = PlatformSettings.from_env(base)
    assert not settings.notifications_enabled  # API parsing does not read Feishu files.
    enabled = PlatformSettings.from_env(
        {**base, "MEWCODE_PLATFORM_NOTIFICATIONS_ENABLED": "true"}
    )
    with pytest.raises(PlatformSettingsError, match="Notifier requires"):
        enabled.validate_notifier()

    webhook = tmp_path / "webhook"
    signing = tmp_path / "signing"
    webhook.write_text(_WEBHOOK, encoding="utf-8")
    signing.write_text("phase6-signing-secret", encoding="utf-8")
    notifier = PlatformSettings.from_env(
        {
            **base,
            "MEWCODE_PLATFORM_NOTIFICATIONS_ENABLED": "true",
            "MEWCODE_PLATFORM_FEISHU_WEBHOOK_URL_FILE": str(webhook),
            "MEWCODE_PLATFORM_FEISHU_SIGNING_SECRET_FILE": str(signing),
        }
    )
    assert notifier.validate_notifier() == (_WEBHOOK, "phase6-signing-secret")


def test_json_logs_propagate_context_and_redact_exception() -> None:
    secret = "phase6-secret-canary"
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter("notifier", SensitiveValueRedactor((secret,))))
    logger = logging.getLogger("phase6-test-json")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    with log_context(job_id="job-6", notification_id="notification-6"):
        try:
            raise RuntimeError(f"failure {secret}")
        except RuntimeError:
            logger.exception("delivery failed %s", secret, extra={"event": "retry"})
    payload = json.loads(stream.getvalue())
    assert payload["service"] == "notifier"
    assert payload["job_id"] == "job-6"
    assert payload["notification_id"] == "notification-6"
    assert secret not in stream.getvalue()


class _FakeRepository:
    def __init__(self) -> None:
        self.claimed = ClaimedNotification(
            id=UUID("00000000-0000-0000-0000-000000000601"),
            job_id=UUID("00000000-0000-0000-0000-000000000602"),
            source_event_sequence=2,
            event_type="FAILED",
            destination="feishu:platform",
            payload=_envelope("FAILED"),
            attempt_count=4,
            notifier_id="notifier-test",
            fencing_token=UUID("00000000-0000-0000-0000-000000000603"),
        )
        self.retry: dict | None = None

    async def claim_notification(self, **kwargs):
        claimed, self.claimed = self.claimed, None
        return claimed

    async def retry_notification(self, **kwargs):
        self.retry = kwargs
        return True

    async def notification_outbox_stats(self):
        return NotificationOutboxStats(1, 0, 0, 2.0)


class _FailingClient:
    async def send(self, envelope):
        raise FeishuDeliveryError("HTTP 503")


@pytest.mark.asyncio
async def test_notifier_retries_without_changing_job_or_exposing_high_cardinality_metrics() -> None:
    settings = PlatformSettings.from_env(
        {
            "MEWCODE_PLATFORM_DATABASE_URL": "postgresql://db/platform",
            "MEWCODE_PLATFORM_NOTIFICATIONS_ENABLED": "true",
            "MEWCODE_PLATFORM_NOTIFIER_ID": "notifier-test",
        }
    )
    repository = _FakeRepository()
    metrics = NotifierMetrics()
    service = NotifierService(
        settings,
        repository,  # type: ignore[arg-type]
        _FailingClient(),  # type: ignore[arg-type]
        random_source=lambda: 0.5,
        metrics=metrics,
    )
    assert await service.deliver_once()
    assert repository.retry is not None
    assert repository.retry["next_attempt_at"] > datetime.now(UTC)
    rendered = metrics.registry.collect()
    label_names = {
        name
        for family in rendered
        for sample in family.samples
        for name in sample.labels
    }
    assert label_names <= {"event_type", "result", "status", "le"}


class _ReadyDatabase:
    async def ping(self) -> bool:
        return True

    async def schema_is_current(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


class _ReadyRepository:
    async def has_fresh_worker(self, stale_seconds: int) -> bool:
        return True

    async def has_fresh_service(self, **kwargs) -> bool:
        return True

    async def authenticate_api_key(self, token: str):
        return None


@pytest.mark.asyncio
async def test_api_metrics_use_route_templates_and_readiness_checks() -> None:
    settings = PlatformSettings.from_env(
        {
            "MEWCODE_PLATFORM_DATABASE_URL": "postgresql://db/platform",
            "MEWCODE_PLATFORM_NOTIFICATIONS_ENABLED": "true",
        }
    )
    app = create_app(
        components=PlatformComponents(
            settings=settings,
            database=_ReadyDatabase(),  # type: ignore[arg-type]
            repository=_ReadyRepository(),  # type: ignore[arg-type]
        )
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    job_id = "00000000-0000-0000-0000-000000000699"
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        ready = await client.get("/health/ready")
        assert ready.status_code == 200
        assert set(ready.json()["checks"]) == {
            "database",
            "schema",
            "worker",
            "notifier",
        }
        unauthorized = await client.get(f"/v1/jobs/{job_id}?secret=canary")
        assert unauthorized.status_code == 401
        await client.get("/metrics")
        metrics = (await client.get("/metrics")).text
    assert 'route="/v1/jobs/{job_id}"' in metrics
    assert job_id not in metrics
    assert "secret" not in metrics
    assert 'check="notifier"' in metrics


def test_compose_mounts_feishu_secrets_only_into_notifier() -> None:
    root = Path(__file__).resolve().parents[2]
    compose = yaml.safe_load((root / "compose.platform.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    for secret in ("feishu_webhook_url", "feishu_signing_secret"):
        holders = {
            service_name
            for service_name, service in services.items()
            if secret in (service.get("secrets") or [])
        }
        assert holders == {"notifier"}
    assert services["worker"]["ports"] == ["127.0.0.1:9091:9091"]
    assert services["worker"]["stop_grace_period"] == "330s"
    worker_environment = services["worker"]["environment"]
    assert "MEWCODE_PLATFORM_WORKER_MAX_CONCURRENT_ATTEMPTS" in worker_environment
    assert "MEWCODE_PLATFORM_ATTEMPT_TIMEOUT_SECONDS" in worker_environment
    assert "MEWCODE_PLATFORM_WORKER_SHUTDOWN_GRACE_SECONDS" in worker_environment
    assert services["notifier"]["ports"] == ["127.0.0.1:9092:9092"]
    assert "healthcheck" in services["api"]
    assert compose["networks"]["default"]["name"] == (
        "${MEWCODE_PLATFORM_CONTROL_NETWORK:-mewcode-platform-control}"
    )
