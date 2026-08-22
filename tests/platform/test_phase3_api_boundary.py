from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from mewcode.platform.api import PlatformComponents, create_app
from mewcode.platform.persistence import ApiKeyPrincipal, StoredEvent
from mewcode.platform.settings import PlatformSettings


class _Database:
    async def aclose(self) -> None:
        return None


class _Repository:
    async def authenticate_api_key(self, token: str):
        if token != "test-token":
            return None
        return ApiKeyPrincipal(
            tenant_id=UUID(int=1),
            requester_id=UUID(int=2),
            key_id=UUID(int=3),
            requester_name="requester",
        )

    async def lookup_idempotent_job(self, **kwargs):
        return None


def _body() -> dict:
    return {
        "repository": {
            "installation_id": 1,
            "owner": "company",
            "name": "service",
            "base_ref": "main",
        },
        "work": {
            "kind": "bugfix",
            "title": "Fix",
            "description": "Broken",
        },
        "execution": {
            "verification_commands": [
                {"name": "tests", "command": "pytest", "timeout_seconds": 600}
            ]
        },
        "attachment_ids": [],
    }


@pytest.mark.asyncio
async def test_api_body_limit_and_cached_body_are_enforced() -> None:
    settings = PlatformSettings.from_env(
        {"MEWCODE_PLATFORM_DATABASE_URL": "postgresql://db/platform"}
    )
    app = create_app(
        components=PlatformComponents(
            settings=settings,
            database=_Database(),  # type: ignore[arg-type]
            repository=_Repository(),  # type: ignore[arg-type]
            resolver=None,
        )
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    headers = {
        "Authorization": "Bearer test-token",
        "Idempotency-Key": "request-1",
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        parsed = await client.post("/v1/jobs", json=_body(), headers=headers)
        assert parsed.status_code == 503
        assert parsed.json()["error"]["code"] == "REPOSITORY_RESOLVER_UNAVAILABLE"

        async def chunks():
            yield b"x" * (128 * 1024)
            yield b"x" * (129 * 1024)

        oversized = await client.post(
            "/v1/jobs",
            content=chunks(),
            headers={**headers, "Content-Type": "application/json"},
        )
        assert oversized.status_code == 413
        assert oversized.json()["error"]["code"] == "REQUEST_TOO_LARGE"


@pytest.mark.asyncio
async def test_validation_errors_do_not_echo_request_values() -> None:
    settings = PlatformSettings.from_env(
        {"MEWCODE_PLATFORM_DATABASE_URL": "postgresql://db/platform"}
    )
    app = create_app(
        components=PlatformComponents(
            settings=settings,
            database=_Database(),  # type: ignore[arg-type]
            repository=_Repository(),  # type: ignore[arg-type]
            resolver=None,
        )
    )
    secret = "github_pat_abcdefghijklmnopqrstuvwxyz123456"
    body = _body()
    body["work"]["description"] = secret
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/jobs",
            headers={
                "Authorization": "Bearer test-token",
                "Idempotency-Key": "validation-secret",
            },
            json=body,
        )

    assert response.status_code == 422
    assert secret not in response.text


class _SseRepository(_Repository):
    def __init__(self, job_id: UUID) -> None:
        self.events = [
            StoredEvent(
                id=UUID(int=10 + sequence),
                job_id=job_id,
                attempt_id=UUID(int=20),
                sequence=sequence,
                attempt_sequence=sequence,
                event_type="text_delta",
                payload={"text": f"event-{sequence}"},
                created_at=datetime.now(UTC),
            )
            for sequence in (1, 2)
        ]

    async def get_job(self, **kwargs):
        return object()

    async def list_events(self, *, after: int, **kwargs):
        return [event for event in self.events if event.sequence > after]


@pytest.mark.asyncio
async def test_sse_uses_persisted_sequence_and_last_event_id() -> None:
    job_id = UUID(int=30)
    settings = PlatformSettings.from_env(
        {"MEWCODE_PLATFORM_DATABASE_URL": "postgresql://db/platform"}
    )
    repository = _SseRepository(job_id)
    components = PlatformComponents(
        settings=settings,
        database=_Database(),  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
    )
    app = create_app(components=components)
    route = next(
        route
        for route in app.routes
        if getattr(route, "path", "") == "/v1/jobs/{job_id}/events/stream"
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(components=components)),
        is_disconnected=lambda: _false(),
    )
    principal = await repository.authenticate_api_key("test-token")
    response = await route.endpoint(
        job_id=job_id,
        request=request,
        principal=principal,
        after=0,
        last_event_id="1",
    )
    chunk = await anext(response.body_iterator)
    await response.body_iterator.aclose()
    assert "id: 2" in chunk
    assert "event-2" in chunk
    assert "event-1" not in chunk


async def _false() -> bool:
    return False
