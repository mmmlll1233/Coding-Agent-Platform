from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from mewcode.platform.artifacts import (
    ArtifactService,
    ArtifactStoreError,
    LocalArtifactStore,
)
from mewcode.platform.domain import (
    RepositoryTargetRejected,
    RepositoryTargetResolver,
    RepositoryTargetUnavailable,
)
from mewcode.platform.execution import SensitiveValueRedactor
from mewcode.platform.observability import ApiMetrics, log_context
from mewcode.platform.persistence import (
    ApiKeyPrincipal,
    Database,
    IdempotencyConflict,
    NotFound,
    PlatformRepository,
    StateConflict,
    create_database,
)
from mewcode.platform.settings import PlatformSettings

from .schemas import (
    ArtifactResponse,
    CreateJobRequest,
    ErrorBody,
    ErrorEnvelope,
    EventPage,
    EventResponse,
    HealthResponse,
    JobInputRequest,
    JobResponse,
)

log = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 256 * 1024
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


class ApiError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        self.headers = headers or {}


@dataclass
class PlatformComponents:
    settings: PlatformSettings
    database: Database
    repository: PlatformRepository
    resolver: RepositoryTargetResolver | None = None
    artifact_store: LocalArtifactStore | None = None
    artifact_service: ArtifactService | None = None
    metrics: ApiMetrics | None = None


def _error_response(request: Request, error: ApiError) -> JSONResponse:
    request_id = getattr(request.state, "request_id", str(uuid4()))
    body = ErrorEnvelope(
        error=ErrorBody(
            code=error.code,
            message=error.message,
            details=error.details,
            request_id=request_id,
        )
    )
    return JSONResponse(
        status_code=error.status_code,
        content=body.model_dump(mode="json"),
        headers=error.headers,
    )


async def _principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> ApiKeyPrincipal:
    if not authorization or not authorization.startswith("Bearer "):
        raise ApiError(
            401,
            "AUTHENTICATION_REQUIRED",
            "A valid Bearer API Key is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization[len("Bearer ") :].strip()
    principal = await request.app.state.components.repository.authenticate_api_key(
        token
    )
    if principal is None:
        raise ApiError(
            401,
            "INVALID_API_KEY",
            "The API Key is invalid or revoked",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


async def _job_response(
    repository: PlatformRepository,
    principal: ApiKeyPrincipal,
    job_id: UUID,
) -> JobResponse:
    job = await repository.get_job(principal=principal, job_id=job_id)
    attempt = await repository.get_current_attempt(principal=principal, job_id=job_id)
    return JobResponse.from_rows(job, attempt)


def create_app(
    settings: PlatformSettings | None = None,
    *,
    components: PlatformComponents | None = None,
) -> FastAPI:
    if components is None:
        resolved_settings = settings or PlatformSettings.from_env()
        database = create_database(resolved_settings)
        components = PlatformComponents(
            settings=resolved_settings,
            database=database,
            repository=PlatformRepository(
                database,
                metadata_retention_days=resolved_settings.metadata_retention_days,
                notifications_enabled=resolved_settings.notifications_enabled,
                notification_destination=resolved_settings.notification_destination,
            ),
            artifact_store=LocalArtifactStore(resolved_settings.artifact_root),
            metrics=ApiMetrics(),
        )
    if components.metrics is None:
        components.metrics = ApiMetrics()
    if components.artifact_service is None and components.artifact_store is not None:
        components.artifact_service = ArtifactService(
            components.repository,
            components.artifact_store,
            redactor=SensitiveValueRedactor(()),
            retention_days=components.settings.artifact_retention_days,
            max_artifact_bytes=components.settings.max_artifact_bytes,
            max_attempt_bytes=components.settings.max_attempt_artifact_bytes,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.components = components
        try:
            yield
        finally:
            close_resolver = getattr(components.resolver, "aclose", None)
            if close_resolver is not None:
                await close_resolver()
            await components.database.aclose()

    app = FastAPI(
        title="MewCode Coding Platform",
        version="1.0.0-phase6",
        lifespan=lifespan,
    )
    app.state.components = components

    @app.middleware("http")
    async def request_boundary(request: Request, call_next):
        started = time.monotonic()
        supplied = request.headers.get("X-Request-ID", "")
        request.state.request_id = (
            supplied if _REQUEST_ID.fullmatch(supplied) else str(uuid4())
        )

        def finish(response: Response) -> Response:
            response.headers["X-Request-ID"] = request.state.request_id
            route = getattr(request.scope.get("route"), "path", "unmatched")
            duration = time.monotonic() - started
            assert components.metrics is not None
            components.metrics.observe_request(
                route=route,
                method=request.method,
                status_code=response.status_code,
                duration_seconds=duration,
            )
            log.info(
                "HTTP request completed",
                extra={
                    "event": "http_request",
                    "request_id": request.state.request_id,
                    "route": route,
                    "method": request.method,
                    "status": response.status_code,
                    "duration_ms": round(duration * 1000, 3),
                },
            )
            return response

        content_length = request.headers.get("content-length")
        if content_length:
            try:
                oversized = int(content_length) > MAX_REQUEST_BYTES
            except ValueError:
                oversized = True
            if oversized:
                response = _error_response(
                    request,
                    ApiError(
                        413,
                        "REQUEST_TOO_LARGE",
                        f"Request body must not exceed {MAX_REQUEST_BYTES} bytes",
                    ),
                )
                return finish(response)
        if request.method in {"POST", "PUT", "PATCH"}:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > MAX_REQUEST_BYTES:
                    response = _error_response(
                        request,
                        ApiError(
                            413,
                            "REQUEST_TOO_LARGE",
                            f"Request body must not exceed {MAX_REQUEST_BYTES} bytes",
                        ),
                    )
                    return finish(response)
            request._body = bytes(body)
        with log_context(request_id=request.state.request_id):
            response = await call_next(request)
        return finish(response)

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, error: ApiError) -> JSONResponse:
        return _error_response(request, error)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        details = [
            {
                "type": item.get("type", "validation_error"),
                "loc": item.get("loc", ()),
                "msg": item.get("msg", "Invalid value"),
            }
            for item in error.errors()
        ]
        return _error_response(
            request,
            ApiError(
                422,
                "VALIDATION_ERROR",
                "The request does not satisfy the API contract",
                details=details,
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        request: Request, error: StarletteHTTPException
    ) -> JSONResponse:
        code = "NOT_FOUND" if error.status_code == 404 else "HTTP_ERROR"
        message = (
            "Resource not found" if error.status_code == 404 else str(error.detail)
        )
        return _error_response(request, ApiError(error.status_code, code, message))

    @app.exception_handler(Exception)
    async def unhandled_error_handler(
        request: Request, error: Exception
    ) -> JSONResponse:
        log.exception(
            "Unhandled platform API error request_id=%s",
            getattr(request.state, "request_id", "unknown"),
        )
        return _error_response(
            request,
            ApiError(500, "INTERNAL_ERROR", "An internal error occurred"),
        )

    @app.get("/health/live", response_model=HealthResponse)
    async def live() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get("/health/ready", response_model=HealthResponse)
    async def ready(request: Request, response: Response) -> HealthResponse:
        current: PlatformComponents = request.app.state.components
        database_ready = await current.database.ping()
        schema_ready = (
            await current.database.schema_is_current() if database_ready else False
        )
        worker_ready = (
            await current.repository.has_fresh_worker(
                current.settings.worker_stale_seconds
            )
            if schema_ready
            else False
        )
        notifier_ready = (
            await current.repository.has_fresh_service(
                service_type="notifier",
                stale_seconds=current.settings.worker_stale_seconds,
            )
            if schema_ready and current.settings.notifications_enabled
            else not current.settings.notifications_enabled
        )
        checks = {
            "database": database_ready,
            "schema": schema_ready,
            "worker": worker_ready,
            "notifier": notifier_ready,
        }
        assert current.metrics is not None
        current.metrics.set_readiness(checks)
        if not all(checks.values()):
            response.status_code = 503
            return HealthResponse(status="not_ready", checks=checks)
        return HealthResponse(status="ready", checks=checks)

    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request) -> Response:
        current: PlatformComponents = request.app.state.components
        assert current.metrics is not None
        body, media_type = current.metrics.render()
        return Response(content=body, media_type=media_type)

    @app.post("/v1/jobs", status_code=202, response_model=JobResponse)
    async def create_job(
        body: CreateJobRequest,
        response: Response,
        request: Request,
        principal: Annotated[ApiKeyPrincipal, Depends(_principal)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JobResponse:
        if not idempotency_key or not (1 <= len(idempotency_key) <= 128):
            raise ApiError(
                400,
                "IDEMPOTENCY_KEY_REQUIRED",
                "Idempotency-Key must contain 1 to 128 characters",
            )
        current: PlatformComponents = request.app.state.components
        request_hash = body.canonical_hash()
        try:
            existing = await current.repository.lookup_idempotent_job(
                principal=principal,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
        except IdempotencyConflict as error:
            raise ApiError(409, error.code, str(error)) from error
        if existing is not None:
            response.headers["Idempotency-Replayed"] = "true"
            return await _job_response(current.repository, principal, existing.id)
        if current.resolver is None:
            raise ApiError(
                503,
                "REPOSITORY_RESOLVER_UNAVAILABLE",
                "Repository Target resolution is not configured",
            )
        repository = body.repository
        try:
            target = await current.resolver.resolve(
                installation_id=repository.installation_id,
                owner=repository.owner,
                name=repository.name,
                base_ref=repository.base_ref,
            )
        except RepositoryTargetUnavailable as error:
            raise ApiError(503, error.code, str(error)) from error
        except RepositoryTargetRejected as error:
            raise ApiError(422, error.code, str(error)) from error
        except ValueError as error:
            raise ApiError(422, "INVALID_REPOSITORY_TARGET", str(error)) from error
        if (
            target.installation_id != repository.installation_id
            or target.owner.casefold() != repository.owner.casefold()
            or target.name.casefold() != repository.name.casefold()
            or target.base_ref != repository.base_ref
        ):
            raise ApiError(
                503,
                "REPOSITORY_TARGET_MISMATCH",
                "Repository resolver returned a different target",
            )
        try:
            job, replayed = await current.repository.create_job(
                principal=principal,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                target=target,
                work_request=body.work.model_dump(mode="json"),
                execution_contract=body.execution.model_dump(mode="json"),
            )
        except IdempotencyConflict as error:
            raise ApiError(409, error.code, str(error)) from error
        if replayed:
            response.headers["Idempotency-Replayed"] = "true"
        return await _job_response(current.repository, principal, job.id)

    @app.get("/v1/jobs/{job_id}", response_model=JobResponse)
    async def get_job(
        job_id: UUID,
        request: Request,
        principal: Annotated[ApiKeyPrincipal, Depends(_principal)],
    ) -> JobResponse:
        try:
            return await _job_response(
                request.app.state.components.repository, principal, job_id
            )
        except NotFound as error:
            raise ApiError(404, "JOB_NOT_FOUND", str(error)) from error

    @app.get("/v1/jobs/{job_id}/events", response_model=EventPage)
    async def list_events(
        job_id: UUID,
        request: Request,
        principal: Annotated[ApiKeyPrincipal, Depends(_principal)],
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> EventPage:
        repository = request.app.state.components.repository
        try:
            rows = await repository.list_events(
                principal=principal,
                job_id=job_id,
                after=after,
                limit=limit + 1,
            )
        except NotFound as error:
            raise ApiError(404, "JOB_NOT_FOUND", str(error)) from error
        has_more = len(rows) > limit
        rows = rows[:limit]
        return EventPage(
            items=[EventResponse.from_stored(item) for item in rows],
            next_after=rows[-1].sequence if rows else after,
            has_more=has_more,
        )

    @app.get(
        "/v1/jobs/{job_id}/artifacts",
        response_model=list[ArtifactResponse],
    )
    async def list_artifacts(
        job_id: UUID,
        request: Request,
        principal: Annotated[ApiKeyPrincipal, Depends(_principal)],
    ) -> list[ArtifactResponse]:
        repository = request.app.state.components.repository
        try:
            artifacts = await repository.list_artifacts(
                principal=principal, job_id=job_id
            )
        except NotFound as error:
            raise ApiError(404, "JOB_NOT_FOUND", str(error)) from error
        return [ArtifactResponse.from_metadata(item) for item in artifacts]

    @app.get("/v1/jobs/{job_id}/artifacts/{artifact_id}")
    async def download_artifact(
        job_id: UUID,
        artifact_id: UUID,
        request: Request,
        principal: Annotated[ApiKeyPrincipal, Depends(_principal)],
    ) -> Response:
        current: PlatformComponents = request.app.state.components
        try:
            artifact = await current.repository.get_artifact(
                principal=principal,
                job_id=job_id,
                artifact_id=artifact_id,
            )
            if current.artifact_service is None:
                raise FileNotFoundError
            content = await current.artifact_service.read_bytes(artifact)
        except (NotFound, FileNotFoundError, ArtifactStoreError) as error:
            raise ApiError(
                404, "ARTIFACT_NOT_FOUND", "Artifact does not exist"
            ) from error
        filenames = {
            "agent_log": "agent_log.ndjson",
            "command_log": "command_log.ndjson",
            "diff": "diff.patch",
            "verification_report": "verification_report.json",
        }
        filename = filenames.get(artifact.kind, "artifact.bin")
        return Response(
            content=content,
            media_type=artifact.content_type,
            headers={
                "ETag": f'"{artifact.sha256}"',
                "Content-Length": str(artifact.size_bytes),
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/v1/jobs/{job_id}/events/stream")
    async def stream_events(
        job_id: UUID,
        request: Request,
        principal: Annotated[ApiKeyPrincipal, Depends(_principal)],
        after: Annotated[int, Query(ge=0)] = 0,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        repository = request.app.state.components.repository
        try:
            await repository.get_job(principal=principal, job_id=job_id)
        except NotFound as error:
            raise ApiError(404, "JOB_NOT_FOUND", str(error)) from error
        if last_event_id:
            try:
                after = max(after, int(last_event_id))
            except ValueError as error:
                raise ApiError(
                    400, "INVALID_EVENT_CURSOR", "Last-Event-ID must be an integer"
                ) from error
        settings = request.app.state.components.settings

        async def generate() -> AsyncIterator[str]:
            cursor = after
            last_send = asyncio.get_running_loop().time()
            while not await request.is_disconnected():
                rows = await repository.list_events(
                    principal=principal,
                    job_id=job_id,
                    after=cursor,
                    limit=100,
                )
                if rows:
                    for row in rows:
                        event = EventResponse.from_stored(row)
                        data = json.dumps(
                            event.model_dump(mode="json"),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        yield f"id: {row.sequence}\nevent: {row.event_type}\ndata: {data}\n\n"
                        cursor = row.sequence
                    last_send = asyncio.get_running_loop().time()
                    continue
                now = asyncio.get_running_loop().time()
                if now - last_send >= settings.sse_keepalive_seconds:
                    yield ": keepalive\n\n"
                    last_send = now
                await asyncio.sleep(settings.sse_poll_milliseconds / 1000)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/v1/jobs/{job_id}/input", response_model=JobResponse)
    async def add_input(
        job_id: UUID,
        body: JobInputRequest,
        request: Request,
        principal: Annotated[ApiKeyPrincipal, Depends(_principal)],
    ) -> JobResponse:
        repository = request.app.state.components.repository
        try:
            await repository.add_input(
                principal=principal, job_id=job_id, content=body.content
            )
            return await _job_response(repository, principal, job_id)
        except NotFound as error:
            raise ApiError(404, "JOB_NOT_FOUND", str(error)) from error
        except StateConflict as error:
            raise ApiError(409, error.code, str(error)) from error

    @app.post("/v1/jobs/{job_id}/retry", response_model=JobResponse)
    async def retry_job(
        job_id: UUID,
        request: Request,
        principal: Annotated[ApiKeyPrincipal, Depends(_principal)],
    ) -> JobResponse:
        repository = request.app.state.components.repository
        try:
            await repository.retry_job(principal=principal, job_id=job_id)
            return await _job_response(repository, principal, job_id)
        except NotFound as error:
            raise ApiError(404, "JOB_NOT_FOUND", str(error)) from error
        except StateConflict as error:
            raise ApiError(409, error.code, str(error)) from error

    @app.post("/v1/jobs/{job_id}/cancel", response_model=JobResponse)
    async def cancel_job(
        job_id: UUID,
        request: Request,
        principal: Annotated[ApiKeyPrincipal, Depends(_principal)],
    ) -> JobResponse:
        repository = request.app.state.components.repository
        try:
            await repository.cancel_job(principal=principal, job_id=job_id)
            return await _job_response(repository, principal, job_id)
        except NotFound as error:
            raise ApiError(404, "JOB_NOT_FOUND", str(error)) from error
        except StateConflict as error:
            raise ApiError(409, error.code, str(error)) from error

    return app
