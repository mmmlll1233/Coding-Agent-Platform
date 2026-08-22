from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx
import jwt

from mewcode.platform.execution import SensitiveValueRedactor


API_VERSION = "2026-03-10"
API_ORIGIN = "https://api.github.com"
ARCHIVE_ORIGIN = "https://codeload.github.com"


class GitHubError(RuntimeError):
    pass


class GitHubUnavailable(GitHubError):
    pass


class GitHubRejected(GitHubError):
    pass


class GitHubConflict(GitHubError):
    pass


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
        try:
            response = await self._http.request(
                method,
                path,
                headers=self._headers(auth),
                json=dict(json_body) if json_body is not None else None,
                params=dict(params) if params is not None else None,
            )
        except httpx.TransportError as error:
            raise GitHubUnavailable("GitHub is temporarily unavailable") from error
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
        value = await self.request_json(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            app_auth=True,
            json_body={
                "repositories": [repository],
                "permissions": dict(permissions),
            },
        )
        if not isinstance(value, dict) or not isinstance(value.get("token"), str):
            raise GitHubUnavailable("GitHub returned an invalid installation token")
        token = value["token"]
        if len(token) < 20:
            raise GitHubUnavailable("GitHub returned an invalid installation token")
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
        destination.unlink(missing_ok=True)
        try:
            response = await self._http.get(
                path, headers=self._headers(token), follow_redirects=False
            )
        except httpx.TransportError as error:
            raise GitHubUnavailable("GitHub archive download failed") from error
        if response.status_code == 429 or response.status_code >= 500:
            raise GitHubUnavailable("GitHub archive download is unavailable")
        if response.status_code not in {301, 302, 303, 307, 308}:
            if response.status_code in {401, 403, 404}:
                raise GitHubRejected("Repository archive is not accessible")
            raise GitHubUnavailable("GitHub returned an invalid archive redirect")
        location = response.headers.get("location", "")
        parsed = urlparse(location)
        if (
            parsed.scheme != "https"
            or f"{parsed.scheme}://{parsed.netloc}" != ARCHIVE_ORIGIN
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise GitHubUnavailable("GitHub returned an untrusted archive redirect")
        total = 0
        try:
            async with self._http.stream(
                "GET", location, headers=self._headers(), follow_redirects=False
            ) as archive:
                if archive.status_code != 200:
                    raise GitHubUnavailable("GitHub archive download failed")
                with destination.open("wb") as output:
                    async for chunk in archive.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise GitHubRejected("Repository archive exceeds 2 GiB")
                        output.write(chunk)
        except (GitHubRejected, GitHubUnavailable):
            destination.unlink(missing_ok=True)
            raise
        except (httpx.TransportError, OSError) as error:
            destination.unlink(missing_ok=True)
            raise GitHubUnavailable("GitHub archive download failed") from error

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()
