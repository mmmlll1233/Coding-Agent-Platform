from __future__ import annotations

from mewcode.platform.execution import shared_platform_redactor
from mewcode.platform.settings import PlatformSettings

from .adapter import GitHubScmAdapter
from .client import (
    GitHubAppClient,
    GitHubConflict,
    GitHubError,
    GitHubRejected,
    GitHubUnavailable,
)
from .resolver import GitHubRepositoryTargetResolver
from .errors import (
    NoChangesError,
    ScmDeliveryConflict,
    ScmError,
    ScmPolicyError,
    ScmUnavailable,
)


def create_repository_resolver(
    settings: PlatformSettings,
) -> GitHubRepositoryTargetResolver:
    redactor = shared_platform_redactor()
    client = GitHubAppClient(
        client_id=settings.github_app_client_id,
        private_key_file=settings.github_private_key_file,
        timeout_seconds=settings.github_timeout_seconds,
        redactor=redactor,
    )
    return GitHubRepositoryTargetResolver(client)


def create_scm_adapter(settings: PlatformSettings) -> GitHubScmAdapter:
    redactor = shared_platform_redactor()
    client = GitHubAppClient(
        client_id=settings.github_app_client_id,
        private_key_file=settings.github_private_key_file,
        timeout_seconds=settings.github_timeout_seconds,
        redactor=redactor,
    )
    return GitHubScmAdapter(
        client,
        max_delivery_files=settings.max_delivery_files,
        max_delivery_bytes=settings.max_delivery_bytes,
        max_delivery_file_bytes=settings.max_delivery_file_bytes,
    )


__all__ = [
    "GitHubAppClient",
    "GitHubConflict",
    "GitHubError",
    "GitHubRejected",
    "GitHubRepositoryTargetResolver",
    "GitHubScmAdapter",
    "GitHubUnavailable",
    "NoChangesError",
    "ScmDeliveryConflict",
    "ScmError",
    "ScmPolicyError",
    "ScmUnavailable",
    "create_repository_resolver",
    "create_scm_adapter",
]
