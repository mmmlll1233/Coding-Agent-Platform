from __future__ import annotations

from typing import Any
from urllib.parse import quote

from mewcode.platform.domain import (
    RepositoryTarget,
    RepositoryTargetRejected,
    RepositoryTargetUnavailable,
)

from .client import GitHubAppClient, GitHubRejected, GitHubUnavailable


def validate_installation(installation: dict[str, Any]) -> None:
    if installation.get("suspended_at") is not None:
        raise RepositoryTargetRejected(
            "GITHUB_INSTALLATION_SUSPENDED", "GitHub App installation is suspended"
        )
    permissions = installation.get("permissions")
    if not isinstance(permissions, dict):
        raise RepositoryTargetUnavailable(
            "GitHub installation permissions are unavailable",
            code="GITHUB_UNAVAILABLE",
        )
    if permissions.get("workflows") == "write":
        raise RepositoryTargetRejected(
            "GITHUB_WORKFLOWS_PERMISSION_FORBIDDEN",
            "GitHub App must not have Workflows write permission",
        )
    if (
        permissions.get("metadata") != "read"
        or permissions.get("contents") != "write"
        or permissions.get("pull_requests") != "write"
    ):
        raise RepositoryTargetRejected(
            "GITHUB_PERMISSIONS_INSUFFICIENT",
            "GitHub App requires Metadata read, Contents write, and Pull requests write",
        )


class GitHubRepositoryTargetResolver:
    def __init__(self, client: GitHubAppClient) -> None:
        self.client = client

    async def resolve(
        self,
        *,
        installation_id: int,
        owner: str,
        name: str,
        base_ref: str,
    ) -> RepositoryTarget:
        try:
            installation = await self.client.get_installation(installation_id)
            validate_installation(installation)
            token = await self.client.installation_token(
                installation_id=installation_id,
                repository=name,
                permissions={"contents": "read"},
            )
            requested_repository_path = (
                f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
            )
            repository = await self.client.request_json(
                "GET", requested_repository_path, token=token
            )
            if not isinstance(repository, dict):
                raise GitHubUnavailable("GitHub returned an invalid repository")
            canonical_owner = str((repository.get("owner") or {}).get("login", ""))
            canonical_name = str(repository.get("name", ""))
            if (
                not canonical_owner
                or not canonical_name
                or canonical_owner.casefold() != owner.casefold()
                or canonical_name.casefold() != name.casefold()
            ):
                raise RepositoryTargetRejected(
                    "REPOSITORY_NOT_ACCESSIBLE",
                    "Repository is not accessible through this installation",
                )
            if repository.get("archived") or repository.get("disabled"):
                raise RepositoryTargetRejected(
                    "REPOSITORY_READ_ONLY", "Repository is archived or disabled"
                )
            canonical_repository_path = (
                f"/repos/{quote(canonical_owner, safe='')}/"
                f"{quote(canonical_name, safe='')}"
            )
            ref = await self.client.request_json(
                "GET",
                f"{canonical_repository_path}/git/ref/heads/"
                f"{quote(base_ref, safe='')}",
                token=token,
            )
            if not isinstance(ref, dict) or not isinstance(ref.get("object"), dict):
                raise GitHubUnavailable("GitHub returned an invalid branch reference")
            obj = ref["object"]
            sha = str(obj.get("sha", "")).lower()
            if obj.get("type") != "commit":
                raise RepositoryTargetRejected(
                    "BASE_REF_NOT_BRANCH", "base_ref must identify a Git branch"
                )
            return RepositoryTarget(
                installation_id=installation_id,
                owner=canonical_owner,
                name=canonical_name,
                base_ref=base_ref,
                base_sha=sha,
            )
        except RepositoryTargetRejected:
            raise
        except GitHubRejected as error:
            raise RepositoryTargetRejected(
                "REPOSITORY_NOT_ACCESSIBLE",
                "Repository or base branch is not accessible through this installation",
            ) from error
        except (GitHubUnavailable, RepositoryTargetUnavailable) as error:
            raise RepositoryTargetUnavailable(
                "GitHub is temporarily unavailable", code="GITHUB_UNAVAILABLE"
            ) from error

    async def aclose(self) -> None:
        await self.client.aclose()
