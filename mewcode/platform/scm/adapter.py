from __future__ import annotations

import base64
import html
import re
import unicodedata
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import quote

from mewcode.platform.domain import (
    Delivery,
    PreparedRepository,
    RepositoryTarget,
    RepositoryTargetRejected,
    VerifiedDeliveryRequest,
)

from .archive import (
    GIB,
    diff_manifests,
    load_manifest,
    normalize_source_archive,
    read_change_contents,
    scan_workspace_archive,
)
from .client import (
    GitHubAppClient,
    GitHubConflict,
    GitHubRejected,
    GitHubUnavailable,
)
from .errors import ScmDeliveryConflict, ScmPolicyError, ScmUnavailable
from .resolver import validate_installation


class GitHubScmAdapter:
    def __init__(
        self,
        client: GitHubAppClient,
        *,
        max_delivery_files: int = 200,
        max_delivery_bytes: int = 20 * 1024 * 1024,
        max_delivery_file_bytes: int = 5 * 1024 * 1024,
    ) -> None:
        self.client = client
        self.max_delivery_files = max_delivery_files
        self.max_delivery_bytes = max_delivery_bytes
        self.max_delivery_file_bytes = max_delivery_file_bytes

    @staticmethod
    def _repo_path(target: RepositoryTarget) -> str:
        return f"/repos/{quote(target.owner, safe='')}/{quote(target.name, safe='')}"

    @staticmethod
    def _object_sha(value: Any, label: str) -> str:
        sha = str(value or "").lower()
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
            raise ScmUnavailable(f"GitHub returned an invalid {label}")
        return sha

    @staticmethod
    def _clean_title(value: str) -> str:
        cleaned = "".join(
            character
            for character in value
            if not unicodedata.category(character).startswith("C")
        )
        return " ".join(cleaned.split())[:200] or "MewCode Delivery"

    async def _token(
        self, target: RepositoryTarget, *, writable: bool
    ) -> str:
        try:
            installation = await self.client.get_installation(target.installation_id)
            validate_installation(installation)
            permissions = (
                {"contents": "write", "pull_requests": "write"}
                if writable
                else {"contents": "read"}
            )
            return await self.client.installation_token(
                installation_id=target.installation_id,
                repository=target.name,
                permissions=permissions,
            )
        except RepositoryTargetRejected as error:
            raise ScmPolicyError(str(error)) from error
        except GitHubRejected as error:
            raise ScmPolicyError("GitHub rejected the SCM operation") from error
        except GitHubUnavailable as error:
            raise ScmUnavailable("GitHub is temporarily unavailable") from error

    async def _reject_gitlinks(
        self, target: RepositoryTarget, tree_sha: str, token: str
    ) -> None:
        pending = deque([tree_sha])
        visited: set[str] = set()
        entries_seen = 0
        while pending:
            current = pending.popleft()
            if current in visited:
                continue
            visited.add(current)
            value = await self.client.request_json(
                "GET", f"{self._repo_path(target)}/git/trees/{current}", token=token
            )
            if not isinstance(value, dict) or not isinstance(value.get("tree"), list):
                raise ScmUnavailable("GitHub returned an invalid Git tree")
            for entry in value["tree"]:
                if not isinstance(entry, dict):
                    raise ScmUnavailable("GitHub returned an invalid Git tree entry")
                entries_seen += 1
                if entries_seen > 300_000:
                    raise ScmPolicyError("Repository tree has too many entries")
                if entry.get("type") == "commit" or entry.get("mode") == "160000":
                    raise ScmPolicyError("Git submodules are unsupported")
                if entry.get("type") == "tree" and isinstance(entry.get("sha"), str):
                    pending.append(entry["sha"])

    async def prepare(
        self, target: RepositoryTarget, trusted_state_dir: Path
    ) -> PreparedRepository:
        token = await self._token(target, writable=False)
        try:
            commit = await self.client.request_json(
                "GET",
                f"{self._repo_path(target)}/git/commits/{target.base_sha}",
                token=token,
            )
            if not isinstance(commit, dict) or not isinstance(commit.get("tree"), dict):
                raise ScmUnavailable("GitHub returned an invalid base commit")
            base_tree_sha = self._object_sha(
                commit["tree"].get("sha"), "base tree"
            )
            await self._reject_gitlinks(target, base_tree_sha, token)
            state_dir = Path(trusted_state_dir).resolve()
            state_dir.mkdir(parents=True, exist_ok=True)
            raw_archive = state_dir / "github-source.tar"
            workspace_archive = state_dir / "repository.tar"
            manifest_path = state_dir / "repository-manifest.json"
            await self.client.download_archive(
                owner=target.owner,
                repository=target.name,
                revision=target.base_sha,
                token=token,
                destination=raw_archive,
                max_bytes=2 * GIB,
            )
            try:
                try:
                    normalize_source_archive(
                        raw_archive,
                        workspace_archive,
                        manifest_path,
                        base_sha=target.base_sha,
                        base_tree_sha=base_tree_sha,
                    )
                except Exception:
                    workspace_archive.unlink(missing_ok=True)
                    manifest_path.unlink(missing_ok=True)
                    raise
            finally:
                raw_archive.unlink(missing_ok=True)
            return PreparedRepository(
                target=target,
                base_tree_sha=base_tree_sha,
                archive_path=workspace_archive,
                manifest_path=manifest_path,
            )
        except (ScmPolicyError, ScmUnavailable):
            raise
        except GitHubRejected as error:
            raise ScmPolicyError("Pinned repository revision is not accessible") from error
        except GitHubUnavailable as error:
            raise ScmUnavailable("GitHub is temporarily unavailable") from error

    @staticmethod
    def _marker(job_id: Any, base_sha: str) -> str:
        return f"<!-- mewcode:job={job_id};base={base_sha} -->"

    @staticmethod
    def _safe_markdown(value: str) -> str:
        return html.escape(value.strip(), quote=False).replace("@", "@\u200b")

    def _pr_body(self, request: VerifiedDeliveryRequest) -> str:
        risks = "\n".join(
            f"- {self._safe_markdown(item)}" for item in request.risks
        ) or "- None declared"
        return "\n\n".join(
            (
                self._marker(request.job_id, request.prepared.target.base_sha),
                "## Work Request\n" + self._safe_markdown(request.work_summary),
                "## Changes\n" + self._safe_markdown(request.change_summary),
                "## Verification\n" + self._safe_markdown(request.verification_summary),
                "## Risks and uncovered areas\n" + risks,
                f"Generated by MewCode Coding Platform. Job ID: `{request.job_id}`.",
            )
        )

    def _commit_message(self, request: VerifiedDeliveryRequest) -> str:
        title = self._clean_title(request.work_title)
        target = request.prepared.target
        return (
            f"{title}\n\n"
            f"MewCode-Job-ID: {request.job_id}\n"
            f"MewCode-Base-SHA: {target.base_sha}"
        )

    async def _existing_delivery(
        self,
        request: VerifiedDeliveryRequest,
        token: str,
        branch: str,
    ) -> tuple[str | None, Delivery | None]:
        target = request.prepared.target
        ref = await self.client.request_json(
            "GET",
            f"{self._repo_path(target)}/git/ref/heads/{quote(branch, safe='')}",
            token=token,
            not_found_ok=True,
        )
        if ref is None:
            return None, None
        if not isinstance(ref, dict) or not isinstance(ref.get("object"), dict):
            raise ScmDeliveryConflict("Existing Delivery branch is invalid")
        if ref["object"].get("type") not in {None, "commit"}:
            raise ScmDeliveryConflict("Existing Delivery branch is not a commit")
        try:
            head_sha = self._object_sha(ref["object"].get("sha"), "Delivery head")
        except ScmUnavailable as error:
            raise ScmDeliveryConflict("Existing Delivery branch is invalid") from error
        commit = await self.client.request_json(
            "GET", f"{self._repo_path(target)}/git/commits/{head_sha}", token=token
        )
        if not isinstance(commit, dict):
            raise ScmDeliveryConflict("Existing Delivery commit is invalid")
        parents = commit.get("parents")
        message = str(commit.get("message", ""))
        expected_job = f"MewCode-Job-ID: {request.job_id}"
        expected_base = f"MewCode-Base-SHA: {target.base_sha}"
        parent = parents[0] if isinstance(parents, list) and parents else None
        if (
            not isinstance(parents, list)
            or len(parents) != 1
            or not isinstance(parent, dict)
            or str(parent.get("sha", "")).lower() != target.base_sha
            or expected_job not in message.splitlines()
            or expected_base not in message.splitlines()
        ):
            raise ScmDeliveryConflict("Delivery branch is not owned by this Job")
        pulls = await self.client.request_json(
            "GET",
            f"{self._repo_path(target)}/pulls",
            token=token,
            params={
                "state": "all",
                "head": f"{target.owner}:{branch}",
                "base": target.base_ref,
                "per_page": 100,
            },
        )
        if not isinstance(pulls, list):
            raise ScmDeliveryConflict("Existing Delivery Pull Request is invalid")
        marker = self._marker(request.job_id, target.base_sha)
        owned = [
            item
            for item in pulls
            if isinstance(item, dict) and marker in str(item.get("body", ""))
        ]
        if len(owned) > 1 or (pulls and len(owned) != len(pulls)):
            raise ScmDeliveryConflict("Delivery branch has conflicting Pull Requests")
        if not owned:
            return head_sha, None
        pull = owned[0]
        if pull.get("state") != "open" or pull.get("draft") is not True or pull.get("merged_at") is not None:
            raise ScmDeliveryConflict("Existing Delivery is no longer an open Draft PR")
        try:
            delivery = Delivery(
                pr_number=int(pull["number"]),
                pr_url=str(pull["html_url"]),
                head_branch=branch,
                head_sha=head_sha,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ScmDeliveryConflict("Existing Delivery Pull Request is invalid") from error
        return head_sha, delivery

    async def _create_pr(
        self,
        request: VerifiedDeliveryRequest,
        token: str,
        branch: str,
        head_sha: str,
    ) -> Delivery:
        target = request.prepared.target
        title = self._safe_markdown(self._clean_title(request.work_title))
        try:
            pull = await self.client.request_json(
                "POST",
                f"{self._repo_path(target)}/pulls",
                token=token,
                conflict_ok=True,
                json_body={
                    "title": title,
                    "body": self._pr_body(request),
                    "head": branch,
                    "base": target.base_ref,
                    "draft": True,
                },
            )
        except GitHubConflict:
            existing_head, delivery = await self._existing_delivery(
                request, token, branch
            )
            if existing_head == head_sha and delivery is not None:
                return delivery
            raise ScmDeliveryConflict("Draft PR creation raced with another Delivery")
        if (
            not isinstance(pull, dict)
            or pull.get("draft") is not True
            or pull.get("state", "open") != "open"
            or pull.get("merged_at") is not None
        ):
            raise ScmDeliveryConflict("GitHub did not create a Draft Pull Request")
        try:
            return Delivery(
                pr_number=int(pull["number"]),
                pr_url=str(pull["html_url"]),
                head_branch=branch,
                head_sha=head_sha,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ScmDeliveryConflict("GitHub returned an invalid Draft Pull Request") from error

    async def publish_verified(
        self, request: VerifiedDeliveryRequest
    ) -> Delivery:
        target = request.prepared.target
        branch = f"mewcode/{request.job_id}"
        token = await self._token(target, writable=True)
        try:
            existing_head, delivery = await self._existing_delivery(
                request, token, branch
            )
            if delivery is not None:
                return delivery
            if existing_head is not None:
                return await self._create_pr(
                    request, token, branch, existing_head
                )

            manifest_base, manifest_tree, baseline = load_manifest(
                request.prepared.manifest_path
            )
            if (
                manifest_base != target.base_sha
                or manifest_tree != request.prepared.base_tree_sha
            ):
                raise ScmPolicyError("Prepared Repository identity changed")
            current = scan_workspace_archive(request.workspace_archive_path)
            changes = diff_manifests(
                baseline,
                current,
                max_files=self.max_delivery_files,
                max_bytes=self.max_delivery_bytes,
                max_file_bytes=self.max_delivery_file_bytes,
            )
            contents = read_change_contents(request.workspace_archive_path, changes)
            tree_entries: list[dict[str, Any]] = []
            for change in changes:
                if change.entry is None:
                    tree_entries.append(
                        {
                            "path": change.path,
                            "mode": baseline[change.path].mode,
                            "type": "blob",
                            "sha": None,
                        }
                    )
                    continue
                blob = await self.client.request_json(
                    "POST",
                    f"{self._repo_path(target)}/git/blobs",
                    token=token,
                    json_body={
                        "content": base64.b64encode(contents[change.path]).decode("ascii"),
                        "encoding": "base64",
                    },
                )
                if not isinstance(blob, dict):
                    raise ScmUnavailable("GitHub returned an invalid Git blob")
                blob_sha = self._object_sha(blob.get("sha"), "Git blob")
                tree_entries.append(
                    {
                        "path": change.path,
                        "mode": change.entry.mode,
                        "type": "blob",
                        "sha": blob_sha,
                    }
                )
            tree = await self.client.request_json(
                "POST",
                f"{self._repo_path(target)}/git/trees",
                token=token,
                json_body={
                    "base_tree": request.prepared.base_tree_sha,
                    "tree": tree_entries,
                },
            )
            if not isinstance(tree, dict):
                raise ScmUnavailable("GitHub returned an invalid Git tree")
            tree_sha = self._object_sha(tree.get("sha"), "Git tree")
            commit = await self.client.request_json(
                "POST",
                f"{self._repo_path(target)}/git/commits",
                token=token,
                json_body={
                    "message": self._commit_message(request),
                    "tree": tree_sha,
                    "parents": [target.base_sha],
                },
            )
            if not isinstance(commit, dict):
                raise ScmUnavailable("GitHub returned an invalid Delivery commit")
            head_sha = self._object_sha(commit.get("sha"), "Delivery commit")
            try:
                await self.client.request_json(
                    "POST",
                    f"{self._repo_path(target)}/git/refs",
                    token=token,
                    conflict_ok=True,
                    json_body={"ref": f"refs/heads/{branch}", "sha": head_sha},
                )
            except GitHubConflict:
                raced_head, raced_delivery = await self._existing_delivery(
                    request, token, branch
                )
                if raced_delivery is not None:
                    return raced_delivery
                if raced_head is not None:
                    return await self._create_pr(
                        request, token, branch, raced_head
                    )
                raise ScmDeliveryConflict("Delivery branch creation raced")
            return await self._create_pr(request, token, branch, head_sha)
        except (ScmPolicyError, ScmDeliveryConflict, ScmUnavailable):
            raise
        except GitHubRejected as error:
            raise ScmPolicyError("GitHub rejected Delivery publication") from error
        except GitHubUnavailable as error:
            raise ScmUnavailable("GitHub is temporarily unavailable") from error

    async def aclose(self) -> None:
        await self.client.aclose()
