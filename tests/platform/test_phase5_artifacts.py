from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest

from mewcode.platform.api import PlatformComponents, create_app
from mewcode.platform.artifacts import (
    ArtifactIntegrityError,
    ArtifactKind,
    ArtifactLimitError,
    ArtifactService,
    ArtifactStoreError,
    LocalArtifactStore,
)
from mewcode.platform.domain import AttemptLease, RepositoryTarget
from mewcode.platform.execution import SensitiveValueRedactor
from mewcode.platform.persistence import ApiKeyPrincipal, ArtifactMetadata, NotFound
from mewcode.platform.settings import PlatformSettings


def _lease() -> AttemptLease:
    return AttemptLease(
        job_id=UUID(int=1),
        attempt_id=UUID(int=2),
        attempt_no=1,
        tenant_id=UUID(int=3),
        requester_id=UUID(int=4),
        worker_id="worker",
        fencing_token=UUID(int=5),
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        repository_target=RepositoryTarget(1, "acme", "repo", "main", "a" * 40),
        work_request={},
        execution_contract={},
    )


class _ArtifactRepository:
    def __init__(self) -> None:
        self.rows: list[ArtifactMetadata] = []

    async def attempt_artifact_bytes(self, attempt_id: UUID) -> int:
        return sum(row.size_bytes for row in self.rows if row.attempt_id == attempt_id)

    async def add_artifact_fenced(self, *, artifact, worker_id, fencing_token):
        assert worker_id == "worker"
        assert fencing_token == UUID(int=5)
        stored = ArtifactMetadata(
            **{
                **artifact.__dict__,
                "created_at": datetime.now(UTC),
            }
        )
        self.rows.append(stored)
        return stored

    async def list_expired_artifacts(self, *, limit):
        now = datetime.now(UTC)
        return [row for row in self.rows if row.expires_at <= now][:limit]

    async def delete_artifact_metadata(self, artifact_id):
        before = len(self.rows)
        self.rows = [row for row in self.rows if row.id != artifact_id]
        return len(self.rows) != before

    async def delete_expired_terminal_jobs(self, *, limit):
        return 1


@pytest.mark.asyncio
async def test_artifact_store_is_atomic_redacted_and_quota_bounded(tmp_path) -> None:
    repository = _ArtifactRepository()
    store = LocalArtifactStore(tmp_path / "artifacts")
    service = ArtifactService(
        repository,  # type: ignore[arg-type]
        store,
        redactor=SensitiveValueRedactor(("phase5-secret-canary",)),
        max_artifact_bytes=128,
        max_attempt_bytes=100,
    )
    artifact = await service.persist_bytes(
        _lease(),
        kind=ArtifactKind.AGENT_LOG,
        content=b'{"text":"phase5-secret-canary"}\n',
        content_type="application/x-ndjson",
    )
    content = store.path_for(artifact.storage_key).read_bytes()
    assert b"phase5-secret-canary" not in content
    assert hashlib.sha256(content).hexdigest() == artifact.sha256
    assert await service.read_bytes(artifact) == content
    assert not list((tmp_path / "artifacts").rglob("*.tmp"))

    store.path_for(artifact.storage_key).write_bytes(b"tampered")
    with pytest.raises(ArtifactIntegrityError):
        await service.read_bytes(artifact)
    store.path_for(artifact.storage_key).write_bytes(content)

    with pytest.raises(ArtifactLimitError, match="total Artifact"):
        await service.persist_bytes(
            _lease(),
            kind=ArtifactKind.DIFF,
            content=b"x" * 90,
            content_type="text/x-diff",
        )
    with pytest.raises(ArtifactStoreError):
        store.path_for("../escape")

    repository.rows[0] = replace(
        repository.rows[0], expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    assert await service.cleanup_expired() == (1, 1)
    assert not store.path_for(artifact.storage_key).exists()
    assert repository.rows == []


class _Database:
    async def aclose(self) -> None:
        return None


class _ApiRepository:
    def __init__(self, artifact: ArtifactMetadata) -> None:
        self.artifact = artifact

    async def authenticate_api_key(self, token: str):
        if token != "owner-token":
            return None
        return ApiKeyPrincipal(UUID(int=7), UUID(int=8), UUID(int=9), "owner")

    async def list_artifacts(self, *, principal, job_id):
        if job_id != self.artifact.job_id:
            raise NotFound("Job does not exist")
        return [self.artifact]

    async def get_artifact(self, *, principal, job_id, artifact_id):
        if job_id != self.artifact.job_id or artifact_id != self.artifact.id:
            raise NotFound("Artifact does not exist")
        return self.artifact


@pytest.mark.asyncio
async def test_artifact_api_hides_storage_key_and_downloads_with_digest(
    tmp_path,
) -> None:
    store = LocalArtifactStore(tmp_path)
    job_id = uuid4()
    attempt_id = uuid4()
    artifact_id = uuid4()
    storage_key = f"{job_id}/{attempt_id}/{artifact_id}"
    content = b'{"schema_version":1}\n'
    store.put(storage_key, content)
    artifact = ArtifactMetadata(
        id=artifact_id,
        job_id=job_id,
        attempt_id=attempt_id,
        kind="verification_report",
        storage_key=storage_key,
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        content_type="application/json",
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    settings = PlatformSettings(database_url="postgresql://db/platform")
    app = create_app(
        components=PlatformComponents(
            settings=settings,
            database=_Database(),  # type: ignore[arg-type]
            repository=_ApiRepository(artifact),  # type: ignore[arg-type]
            artifact_store=store,
        )
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    headers = {"Authorization": "Bearer owner-token"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get(f"/v1/jobs/{job_id}/artifacts", headers=headers)
        downloaded = await client.get(
            f"/v1/jobs/{job_id}/artifacts/{artifact_id}", headers=headers
        )
        hidden = await client.get(
            f"/v1/jobs/{uuid4()}/artifacts/{artifact_id}", headers=headers
        )
    assert listed.status_code == 200
    assert "storage_key" not in listed.text
    assert listed.json()[0]["sha256"] == artifact.sha256
    assert downloaded.status_code == 200
    assert downloaded.content == content
    assert downloaded.headers["etag"] == f'"{artifact.sha256}"'
    assert downloaded.headers["content-disposition"].endswith(
        'filename="verification_report.json"'
    )
    assert hidden.status_code == 404
