from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx
import jwt

from mewcode.platform.execution import SensitiveValueRedactor


API_VERSION = "2026-03-10"
API_ORIGIN = "https://api.github.com"
ARCHIVE_ORIGIN = "https://codeload.github.com"
_TRANSIENT_ATTEMPTS = 3


class GitHubError(RuntimeError):
    pass


class GitHubUnavailable(GitHubError):
    pass


class GitHubRejected(GitHubError):
    pass


class GitHubConflict(GitHubError):
    pass


class _TransientArchiveDownload(GitHubUnavailable):
    pass


@dataclass(frozen=True)
class _CachedInstallationToken:
    token: str
    expires_at: float


class GitHubAppCache:
    """Process-local GitHub App cache; values are never persisted."""

    def __init__(self) -> None:
        self._tokens: dict[tuple[Any, ...], _CachedInstallationToken] = {}
        self._token_locks: dict[tuple[Any, ...], asyncio.Lock] = {}

    async def installation_token(
        self,
        key: tuple[Any, ...],
        *,
        now: float,
        issue: Callable[[], Awaitable[_CachedInstallationToken]],
    ) -> str:
        lock = self._token_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._tokens.get(key)
            if cached is not None and cached.expires_at - now > 60:
                return cached.token
            issued = await issue()
            self._tokens[key] = issued
            return issued.token


class GitHubAppClient:
    """Small fail-closed GitHub.com client for the trusted SCM boundary."""

    def __init__(
        self,
        *,
        client_id: str,
        private_key_file: str | Path,
        timeout_seconds: int = 30,
        redactor: SensitiveValueRedactor | None = None,
        http_client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
        app_cache: GitHubAppCache | None = None,
    ) -> None:
        if not client_id.strip():
            raise ValueError("GitHub App client ID is required")
        key_path = Path(private_key_file)
        if not str(private_key_file).strip():
            raise ValueError("GitHub App private key file is required")
        try:
            self._private_key = key_path.read_bytes()
        except OSError as error:
            raise ValueError("GitHub App private key file cannot be read") from error
        if not self._private_key.strip():
            raise ValueError("GitHub App private key file is empty")
        self.client_id = client_id.strip()
        self.redactor = redactor or SensitiveValueRedactor(())
        key_text = self._private_key.decode("utf-8", errors="ignore")
        self.redactor.add(
            key_text,
            *(line.strip() for line in key_text.splitlines() if len(line.strip()) >= 16),
        )
        self._clock = clock
        self._app_cache = app_cache or GitHubAppCache()
        # Validate the PEM at startup, before accepting Work Requests.
        try:
            self._app_jwt()
        except Exception as error:
            raise ValueError("GitHub App private key is not a valid RSA key") from error
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(
            base_url=API_ORIGIN,
            timeout=timeout_seconds,
            follow_redirects=False,
        )

    def _app_jwt(self) -> str:
        now = int(self._clock())
        encoded = jwt.encode(
            {"iat": now - 60, "exp": now + 9 * 60, "iss": self.client_id},
            self._private_key,
            algorithm="RS256",
        )
        token = str(encoded)
        self.redactor.add(token)
        return token

    @staticmethod
    def _headers(token: str | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "mewcode-platform-phase4",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        app_auth: bool = False,
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        not_found_ok: bool = False,
        conflict_ok: bool = False,
    ) -> Any | None:
        auth = self._app_jwt() if app_auth else token
        attempts = (
            _TRANSIENT_ATTEMPTS if method.upper() in {"GET", "HEAD"} else 1
        )
        response: httpx.Response | None = None
        last_transport: httpx.TransportError | None = None
        for attempt in range(attempts):
            try:
                response = await self._http.request(
                    method,
                    path,
                    headers=self._headers(auth),
                    json=dict(json_body) if json_body is not None else None,
                    params=dict(params) if params is not None else None,
                )
            except httpx.TransportError as error:
                last_transport = error
                if attempt + 1 >= attempts:
                    raise GitHubUnavailable(
                        "GitHub is temporarily unavailable"
                    ) from error
                await asyncio.sleep(0.5 * (2**attempt))
                continue
            if (
                response.status_code == 429 or response.status_code >= 500
            ) and attempt + 1 < attempts:
                await asyncio.sleep(0.5 * (2**attempt))
                continue
            break
        if response is None:
            raise GitHubUnavailable(
                "GitHub is temporarily unavailable"
            ) from last_transport
        if 200 <= response.status_code < 300:
            if response.status_code == 204 or not response.content:
                return None
            try:
                return response.json()
            except ValueError as error:
                raise GitHubUnavailable("GitHub returned an invalid response") from error
        if response.status_code == 404 and not_found_ok:
            return None
        if response.status_code in {409, 422} and conflict_ok:
            raise GitHubConflict("GitHub reported a concurrent Delivery conflict")
        if response.status_code == 429 or response.status_code >= 500:
            raise GitHubUnavailable("GitHub is temporarily unavailable")
        if response.status_code in {401, 403, 404, 409, 422}:
            raise GitHubRejected("GitHub rejected the requested repository operation")
        raise GitHubUnavailable("GitHub returned an unexpected response")

    async def get_installation(self, installation_id: int) -> dict[str, Any]:
        value = await self.request_json(
            "GET", f"/app/installations/{installation_id}", app_auth=True
        )
        assert isinstance(value, dict)
        return value

    async def installation_token(
        self,
        *,
        installation_id: int,
        repository: str,
        permissions: Mapping[str, str],
    ) -> str:
        cache_key = (
            self.client_id,
            int(installation_id),
            repository.casefold(),
            tuple(
                sorted(
                    (str(name), str(level))
                    for name, level in permissions.items()
                )
            ),
        )

        async def issue() -> _CachedInstallationToken:
            value = None
            for attempt in range(_TRANSIENT_ATTEMPTS):
                try:
                    value = await self.request_json(
                        "POST",
                        f"/app/installations/{installation_id}/access_tokens",
                        app_auth=True,
                        json_body={
                            "repositories": [repository],
                            "permissions": dict(permissions),
                        },
                    )
                    break
                except GitHubUnavailable:
                    if attempt + 1 >= _TRANSIENT_ATTEMPTS:
                        raise
                    await asyncio.sleep(0.5 * (2**attempt))
            if not isinstance(value, dict) or not isinstance(
                value.get("token"), str
            ):
                raise GitHubUnavailable(
                    "GitHub returned an invalid installation token"
                )
            token = value["token"]
            if len(token) < 20:
                raise GitHubUnavailable(
                    "GitHub returned an invalid installation token"
                )
            expires_at = self._clock() + 300
            raw_expiry = value.get("expires_at")
            if isinstance(raw_expiry, str):
                try:
                    parsed = datetime.fromisoformat(
                        raw_expiry.replace("Z", "+00:00")
                    )
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=UTC)
                    expires_at = parsed.timestamp()
                except ValueError:
                    pass
            return _CachedInstallationToken(token=token, expires_at=expires_at)

        token = await self._app_cache.installation_token(
            cache_key, now=self._clock(), issue=issue
        )
        self.redactor.add(token)
        return token

    async def download_archive(
        self,
        *,
        owner: str,
        repository: str,
        revision: str,
        token: str,
        destination: Path,
        max_bytes: int,
    ) -> None:
        path = (
            f"/repos/{quote(owner, safe='')}/{quote(repository, safe='')}/"
            f"tarball/{quote(revision, safe='')}"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(_TRANSIENT_ATTEMPTS):
            destination.unlink(missing_ok=True)
            try:
                response = await self._http.get(
                    path, headers=self._headers(token), follow_redirects=False
                )
                if response.status_code == 429 or response.status_code >= 500:
                    raise _TransientArchiveDownload(
                        "GitHub archive download is unavailable"
                    )
                if response.status_code not in {301, 302, 303, 307, 308}:
                    if response.status_code in {401, 403, 404}:
                        raise GitHubRejected(
                            "Repository archive is not accessible"
                        )
                    raise GitHubUnavailable(
                        "GitHub returned an invalid archive redirect"
                    )
                location = response.headers.get("location", "")
                parsed = urlparse(location)
                if (
                    parsed.scheme != "https"
                    or f"{parsed.scheme}://{parsed.netloc}" != ARCHIVE_ORIGIN
                    or parsed.username is not None
                    or parsed.password is not None
                ):
                    raise GitHubUnavailable(
                        "GitHub returned an untrusted archive redirect"
                    )
                total = 0
                async with self._http.stream(
                    "GET", location, headers=self._headers(), follow_redirects=False
                ) as archive:
                    if archive.status_code != 200:
                        if archive.status_code == 429 or archive.status_code >= 500:
                            raise _TransientArchiveDownload(
                                "GitHub archive download failed"
                            )
                        raise GitHubUnavailable("GitHub archive download failed")
                    with destination.open("wb") as output:
                        async for chunk in archive.aiter_bytes():
                            total += len(chunk)
                            if total > max_bytes:
                                raise GitHubRejected(
                                    "Repository archive exceeds 2 GiB"
                                )
                            output.write(chunk)
                return
            except GitHubRejected:
                destination.unlink(missing_ok=True)
                raise
            except (
                _TransientArchiveDownload,
                httpx.TransportError,
                OSError,
            ) as error:
                destination.unlink(missing_ok=True)
                if attempt + 1 >= _TRANSIENT_ATTEMPTS:
                    if isinstance(error, _TransientArchiveDownload):
                        raise GitHubUnavailable(str(error)) from error
                    raise GitHubUnavailable(
                        "GitHub archive download failed"
                    ) from error
                await asyncio.sleep(0.5 * (2**attempt))
            except GitHubUnavailable:
                destination.unlink(missing_ok=True)
                raise

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()
