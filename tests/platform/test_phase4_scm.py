from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from uuid import UUID

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from mewcode.platform.domain import (
    PreparedRepository,
    RepositoryTarget,
    RepositoryTargetRejected,
    RepositoryTargetUnavailable,
    VerifiedDeliveryRequest,
)
from mewcode.platform.execution import SensitiveValueRedactor
from mewcode.platform.scm import (
    GitHubAppClient,
    GitHubConflict,
    GitHubRejected,
    GitHubRepositoryTargetResolver,
    GitHubUnavailable,
)
from mewcode.platform.scm.adapter import GitHubScmAdapter
from mewcode.platform.scm.archive import (
    diff_manifests,
    load_manifest,
    normalize_source_archive,
    scan_workspace_archive,
)
from mewcode.platform.scm.errors import (
    NoChangesError,
    ScmDeliveryConflict,
    ScmPolicyError,
)


BASE_SHA = "a" * 40
TREE_SHA = "b" * 40
HEAD_SHA = "c" * 40
JOB_ID = UUID("00000000-0000-0000-0000-000000000123")


def _private_key(path: Path) -> Path:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return path


def _tar(path: Path, files: dict[str, bytes], *, prefix: str = "") -> Path:
    with tarfile.open(path, mode="w") as archive:
        if prefix:
            root = tarfile.TarInfo(prefix)
            root.type = tarfile.DIRTYPE
            root.mode = 0o755
            archive.addfile(root)
        for name, content in files.items():
            info = tarfile.TarInfo(f"{prefix}/{name}" if prefix else name)
            info.size = len(content)
            info.mode = 0o755 if name.endswith(".sh") else 0o644
            archive.addfile(info, io.BytesIO(content))
    return path


def _tar_members(
    path: Path, members: list[tuple[tarfile.TarInfo, bytes | None]]
) -> Path:
    with tarfile.open(path, mode="w") as archive:
        for info, content in members:
            archive.addfile(info, io.BytesIO(content) if content is not None else None)
    return path


@pytest.mark.asyncio
async def test_github_resolver_scopes_token_and_pins_branch(tmp_path: Path) -> None:
    observed: list[httpx.Request] = []
    app_tokens: list[str] = []
    redactor = SensitiveValueRedactor(())
    token = "ghs_1234567890_stateless_token.with_more_parts"

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        if request.url.path == "/app/installations/7":
            encoded = request.headers["Authorization"].removeprefix("Bearer ")
            app_tokens.append(encoded)
            claims = jwt.decode(encoded, options={"verify_signature": False})
            assert claims["iss"] == "Iv1.phase4"
            assert claims["exp"] - claims["iat"] == 600
            return httpx.Response(
                200,
                json={
                    "id": 7,
                    "suspended_at": None,
                    "permissions": {
                        "metadata": "read",
                        "contents": "write",
                        "pull_requests": "write",
                    },
                },
            )
        if request.url.path == "/app/installations/7/access_tokens":
            body = json.loads(request.content)
            assert body == {
                "repositories": ["repo"],
                "permissions": {"contents": "read"},
            }
            return httpx.Response(201, json={"token": token})
        assert request.headers["Authorization"] == f"Bearer {token}"
        if request.url.path == "/repos/acme/repo":
            return httpx.Response(
                200,
                json={
                    "name": "Repo",
                    "owner": {"login": "Acme"},
                    "archived": False,
                    "disabled": False,
                },
            )
        if request.url.path == "/repos/Acme/Repo/git/ref/heads/main":
            return httpx.Response(
                200, json={"object": {"type": "commit", "sha": BASE_SHA}}
            )
        raise AssertionError(request.url)

    http = httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)
    )
    client = GitHubAppClient(
        client_id="Iv1.phase4",
        private_key_file=_private_key(tmp_path / "app.pem"),
        redactor=redactor,
        http_client=http,
        clock=lambda: 1_800_000_000,
    )
    resolver = GitHubRepositoryTargetResolver(client)
    target = await resolver.resolve(
        installation_id=7, owner="acme", name="repo", base_ref="main"
    )
    assert target == RepositoryTarget(7, "Acme", "Repo", "main", BASE_SHA)
    assert redactor.contains_secret(f"credential={token}")
    assert app_tokens and redactor.contains_secret(f"jwt={app_tokens[0]}")
    assert all("github_pat_" not in request.headers.get("Authorization", "") for request in observed)
    await http.aclose()


def test_github_client_rejects_invalid_private_key(tmp_path: Path) -> None:
    key = tmp_path / "invalid.pem"
    key.write_text("not an RSA key", encoding="utf-8")
    with pytest.raises(ValueError, match="valid RSA"):
        GitHubAppClient(client_id="Iv1.phase4", private_key_file=key)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("installation", "code"),
    [
        (
            {
                "suspended_at": "2026-08-22T00:00:00Z",
                "permissions": {
                    "metadata": "read",
                    "contents": "write",
                    "pull_requests": "write",
                },
            },
            "GITHUB_INSTALLATION_SUSPENDED",
        ),
        (
            {
                "suspended_at": None,
                "permissions": {
                    "contents": "write",
                    "pull_requests": "write",
                },
            },
            "GITHUB_PERMISSIONS_INSUFFICIENT",
        ),
    ],
)
async def test_resolver_rejects_suspended_or_underprivileged_installation(
    tmp_path: Path, installation: dict, code: str
) -> None:
    http = httpx.AsyncClient(
        base_url="https://api.github.com",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=installation)
        ),
    )
    resolver = GitHubRepositoryTargetResolver(
        GitHubAppClient(
            client_id="Iv1.phase4",
            private_key_file=_private_key(tmp_path / "app.pem"),
            http_client=http,
        )
    )
    with pytest.raises(RepositoryTargetRejected) as caught:
        await resolver.resolve(
            installation_id=7, owner="Acme", name="Repo", base_ref="main"
        )
    assert caught.value.code == code
    await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_resolver_maps_transient_github_failures_to_stable_unavailable(
    tmp_path: Path, status: int
) -> None:
    http = httpx.AsyncClient(
        base_url="https://api.github.com",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, text="secret response ignored")
        ),
    )
    resolver = GitHubRepositoryTargetResolver(
        GitHubAppClient(
            client_id="Iv1.phase4",
            private_key_file=_private_key(tmp_path / "app.pem"),
            http_client=http,
        )
    )
    with pytest.raises(RepositoryTargetUnavailable) as caught:
        await resolver.resolve(
            installation_id=7, owner="Acme", name="Repo", base_ref="main"
        )
    assert caught.value.code == "GITHUB_UNAVAILABLE"
    assert "secret response" not in str(caught.value)
    await http.aclose()


@pytest.mark.asyncio
async def test_resolver_maps_timeout_to_stable_unavailable(tmp_path: Path) -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("request header secret", request=request)

    http = httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(timeout)
    )
    resolver = GitHubRepositoryTargetResolver(
        GitHubAppClient(
            client_id="Iv1.phase4",
            private_key_file=_private_key(tmp_path / "app.pem"),
            http_client=http,
        )
    )
    with pytest.raises(RepositoryTargetUnavailable) as caught:
        await resolver.resolve(
            installation_id=7, owner="Acme", name="Repo", base_ref="main"
        )
    assert caught.value.code == "GITHUB_UNAVAILABLE"
    assert "secret" not in str(caught.value)
    await http.aclose()


@pytest.mark.asyncio
async def test_resolver_rejects_missing_branch_with_stable_422_code(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/7":
            return httpx.Response(
                200,
                json={
                    "suspended_at": None,
                    "permissions": {
                        "metadata": "read",
                        "contents": "write",
                        "pull_requests": "write",
                    },
                },
            )
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": "ghs_missing_branch_1234567890"})
        if request.url.path == "/repos/Acme/Repo":
            return httpx.Response(
                200,
                json={
                    "name": "Repo",
                    "owner": {"login": "Acme"},
                    "archived": False,
                    "disabled": False,
                },
            )
        return httpx.Response(404)

    http = httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)
    )
    resolver = GitHubRepositoryTargetResolver(
        GitHubAppClient(
            client_id="Iv1.phase4",
            private_key_file=_private_key(tmp_path / "app.pem"),
            http_client=http,
        )
    )
    with pytest.raises(RepositoryTargetRejected) as caught:
        await resolver.resolve(
            installation_id=7, owner="Acme", name="Repo", base_ref="missing"
        )
    assert caught.value.code == "REPOSITORY_NOT_ACCESSIBLE"
    await http.aclose()


@pytest.mark.asyncio
async def test_resolver_rejects_workflows_write_permission(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "suspended_at": None,
                "permissions": {
                    "contents": "write",
                    "pull_requests": "write",
                    "workflows": "write",
                },
            },
        )

    http = httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)
    )
    resolver = GitHubRepositoryTargetResolver(
        GitHubAppClient(
            client_id="Iv1.phase4",
            private_key_file=_private_key(tmp_path / "app.pem"),
            http_client=http,
        )
    )
    with pytest.raises(
        RepositoryTargetRejected, match="must not have Workflows write"
    ):
        await resolver.resolve(
            installation_id=7, owner="Acme", name="Repo", base_ref="main"
        )
    await http.aclose()


@pytest.mark.asyncio
async def test_archive_redirect_drops_authorization_and_rejects_other_hosts(
    tmp_path: Path,
) -> None:
    token = "ghs_phase4_archive_canary_1234567890"
    archive_bytes = _tar(
        tmp_path / "fixture.tar", {"a.txt": b"a"}, prefix="Acme-Repo-base"
    ).read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            assert request.headers["Authorization"] == f"Bearer {token}"
            return httpx.Response(
                302,
                headers={"Location": "https://codeload.github.com/Acme/Repo/tar.gz/base"},
            )
        assert request.url.host == "codeload.github.com"
        assert "Authorization" not in request.headers
        return httpx.Response(200, content=archive_bytes)

    http = httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)
    )
    client = GitHubAppClient(
        client_id="Iv1.phase4",
        private_key_file=_private_key(tmp_path / "app.pem"),
        http_client=http,
    )
    destination = tmp_path / "download.tar"
    await client.download_archive(
        owner="Acme",
        repository="Repo",
        revision=BASE_SHA,
        token=token,
        destination=destination,
        max_bytes=1024 * 1024,
    )
    assert destination.read_bytes() == archive_bytes
    await http.aclose()


@pytest.mark.asyncio
async def test_archive_download_rejects_untrusted_redirect_and_cleans_partial_file(
    tmp_path: Path,
) -> None:
    responses = iter(
        (
            httpx.Response(302, headers={"Location": "https://example.com/archive"}),
            httpx.Response(
                302,
                headers={"Location": "https://codeload.github.com/Acme/Repo/tar"},
            ),
            httpx.Response(200, content=b"too-large"),
        )
    )
    http = httpx.AsyncClient(
        base_url="https://api.github.com",
        transport=httpx.MockTransport(lambda request: next(responses)),
    )
    client = GitHubAppClient(
        client_id="Iv1.phase4",
        private_key_file=_private_key(tmp_path / "app.pem"),
        http_client=http,
    )
    destination = tmp_path / "download.tar"
    with pytest.raises(GitHubUnavailable, match="untrusted archive redirect"):
        await client.download_archive(
            owner="Acme",
            repository="Repo",
            revision=BASE_SHA,
            token="ghs_phase4_archive_canary_1234567890",
            destination=destination,
            max_bytes=4,
        )
    assert not destination.exists()
    with pytest.raises(GitHubRejected, match="exceeds"):
        await client.download_archive(
            owner="Acme",
            repository="Repo",
            revision=BASE_SHA,
            token="ghs_phase4_archive_canary_1234567890",
            destination=destination,
            max_bytes=4,
        )
    assert not destination.exists()
    await http.aclose()


def test_repository_archive_manifest_and_diff_are_bounded(tmp_path: Path) -> None:
    source = _tar(
        tmp_path / "source.tar",
        {
            "hello.txt": b"old\n",
            "run.sh": b"#!/bin/sh\n",
            ".env": b"keep-out\n",
            ".mewcode/config.yaml": b"untrusted: true\n",
            ".github/workflows/test.yml": b"name: test\n",
        },
        prefix="Acme-Repo-deadbeef",
    )
    normalized = tmp_path / "repository.tar"
    manifest = tmp_path / "manifest.json"
    normalize_source_archive(
        source,
        normalized,
        manifest,
        base_sha=BASE_SHA,
        base_tree_sha=TREE_SHA,
    )
    _, _, baseline = load_manifest(manifest)
    assert ".env" not in baseline
    assert ".mewcode/config.yaml" not in baseline
    assert baseline["run.sh"].mode == "100755"

    workspace = _tar(
        tmp_path / "workspace.tar",
        {
            "hello.txt": b"new\n",
            "new.bin": b"\x00\x01\xff",
            ".github/workflows/test.yml": b"name: test\n",
        },
    )
    current = scan_workspace_archive(workspace)
    changes = diff_manifests(
        baseline,
        current,
        max_files=200,
        max_bytes=20 * 1024 * 1024,
        max_file_bytes=5 * 1024 * 1024,
    )
    assert [(item.path, item.entry is None) for item in changes] == [
        ("hello.txt", False),
        ("new.bin", False),
        ("run.sh", True),
    ]


def test_archive_rejects_traversal_links_type_conflicts_and_bombs(
    tmp_path: Path,
) -> None:
    root = tarfile.TarInfo("Acme-Repo-base")
    root.type = tarfile.DIRTYPE

    traversal = tarfile.TarInfo("Acme-Repo-base/../../escape")
    traversal.size = 1
    with pytest.raises(ScmPolicyError, match="Unsafe"):
        normalize_source_archive(
            _tar_members(tmp_path / "traversal.tar", [(root, None), (traversal, b"x")]),
            tmp_path / "traversal-out.tar",
            tmp_path / "traversal.json",
            base_sha=BASE_SHA,
            base_tree_sha=TREE_SHA,
        )

    link = tarfile.TarInfo("Acme-Repo-base/link")
    link.type = tarfile.SYMTYPE
    link.linkname = "../../outside"
    with pytest.raises(ScmPolicyError, match="Escaping"):
        normalize_source_archive(
            _tar_members(tmp_path / "link.tar", [(root, None), (link, None)]),
            tmp_path / "link-out.tar",
            tmp_path / "link.json",
            base_sha=BASE_SHA,
            base_tree_sha=TREE_SHA,
        )

    parent = tarfile.TarInfo("Acme-Repo-base/a")
    parent.size = 1
    child = tarfile.TarInfo("Acme-Repo-base/a/b")
    child.size = 1
    with pytest.raises(ScmPolicyError, match="type conflict"):
        normalize_source_archive(
            _tar_members(
                tmp_path / "conflict.tar",
                [(root, None), (parent, b"a"), (child, b"b")],
            ),
            tmp_path / "conflict-out.tar",
            tmp_path / "conflict.json",
            base_sha=BASE_SHA,
            base_tree_sha=TREE_SHA,
        )

    with pytest.raises(ScmPolicyError, match="workspace capacity"):
        normalize_source_archive(
            _tar(tmp_path / "bomb.tar", {"large": b"x" * 33}, prefix="root"),
            tmp_path / "bomb-out.tar",
            tmp_path / "bomb.json",
            base_sha=BASE_SHA,
            base_tree_sha=TREE_SHA,
            max_unpacked_bytes=32,
        )


def test_safe_symlinks_and_protected_github_paths_are_compared(tmp_path: Path) -> None:
    root = tarfile.TarInfo("Acme-Repo-base")
    root.type = tarfile.DIRTYPE
    target = tarfile.TarInfo("Acme-Repo-base/target.txt")
    target.size = 6
    link = tarfile.TarInfo("Acme-Repo-base/link")
    link.type = tarfile.SYMTYPE
    link.linkname = "target.txt"
    source = _tar_members(
        tmp_path / "symlink-source.tar",
        [(root, None), (target, b"target"), (link, None)],
    )
    normalize_source_archive(
        source,
        tmp_path / "symlink-repository.tar",
        tmp_path / "symlink-manifest.json",
        base_sha=BASE_SHA,
        base_tree_sha=TREE_SHA,
    )
    _, _, baseline = load_manifest(tmp_path / "symlink-manifest.json")
    assert baseline["link"].mode == "120000"

    workspace_target = tarfile.TarInfo("target.txt")
    workspace_target.size = 6
    changed_link = tarfile.TarInfo("link")
    changed_link.type = tarfile.SYMTYPE
    changed_link.linkname = "other.txt"
    current = scan_workspace_archive(
        _tar_members(
            tmp_path / "symlink-workspace.tar",
            [(workspace_target, b"target"), (changed_link, None)],
        )
    )
    changes = diff_manifests(
        baseline,
        current,
        max_files=200,
        max_bytes=20 * 1024 * 1024,
        max_file_bytes=5 * 1024 * 1024,
    )
    assert [change.path for change in changes] == ["link"]

    protected_source = _tar(
        tmp_path / "protected-source.tar",
        {"hello.txt": b"hello", ".github/CODEOWNERS": b"* @owners\n"},
        prefix="root",
    )
    normalize_source_archive(
        protected_source,
        tmp_path / "protected-repository.tar",
        tmp_path / "protected-manifest.json",
        base_sha=BASE_SHA,
        base_tree_sha=TREE_SHA,
    )
    _, _, protected = load_manifest(tmp_path / "protected-manifest.json")
    unchanged = scan_workspace_archive(
        _tar(
            tmp_path / "protected-unchanged.tar",
            {"hello.txt": b"hello", ".github/CODEOWNERS": b"* @owners\n"},
        )
    )
    with pytest.raises(NoChangesError):
        diff_manifests(
            protected,
            unchanged,
            max_files=200,
            max_bytes=20 * 1024 * 1024,
            max_file_bytes=5 * 1024 * 1024,
        )
    changed = scan_workspace_archive(
        _tar(
            tmp_path / "protected-changed.tar",
            {"hello.txt": b"hello", ".github/CODEOWNERS": b"* @other\n"},
        )
    )
    with pytest.raises(ScmPolicyError, match=".github"):
        diff_manifests(
            protected,
            changed,
            max_files=200,
            max_bytes=20 * 1024 * 1024,
            max_file_bytes=5 * 1024 * 1024,
        )


@pytest.mark.parametrize(
    ("name", "content", "message"),
    [
        (".gitmodules", b"[submodule]\n", "submodules"),
        (".gitattributes", b"*.bin\tfilter = lfs\tdiff=lfs\n", "LFS"),
        ("large.bin", b"x" * 32, "too large"),
    ],
)
def test_repository_policy_rejects_unsupported_content(
    tmp_path: Path, name: str, content: bytes, message: str
) -> None:
    source = _tar(
        tmp_path / "source.tar", {name: content}, prefix="Acme-Repo-deadbeef"
    )
    if name == "large.bin":
        normalized = tmp_path / "repository.tar"
        manifest = tmp_path / "manifest.json"
        normalize_source_archive(
            source,
            normalized,
            manifest,
            base_sha=BASE_SHA,
            base_tree_sha=TREE_SHA,
        )
        _, _, baseline = load_manifest(manifest)
        current = scan_workspace_archive(
            _tar(tmp_path / "workspace.tar", {name: content + b"!"})
        )
        with pytest.raises(ScmPolicyError, match=message):
            diff_manifests(
                baseline,
                current,
                max_files=200,
                max_bytes=20,
                max_file_bytes=16,
            )
    else:
        with pytest.raises(ScmPolicyError, match=message):
            normalize_source_archive(
                source,
                tmp_path / "repository.tar",
                tmp_path / "manifest.json",
                base_sha=BASE_SHA,
                base_tree_sha=TREE_SHA,
            )


class _PublishingGithub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.branch_created = False
        self.pull: dict | None = None
        self.pull_title = ""
        self.commit_message = ""

    async def get_installation(self, installation_id: int) -> dict:
        return {
            "id": installation_id,
            "suspended_at": None,
            "permissions": {
                "contents": "write",
                "pull_requests": "write",
                "metadata": "read",
            },
        }

    async def installation_token(self, **kwargs) -> str:
        assert kwargs["permissions"] == {
            "contents": "write",
            "pull_requests": "write",
        }
        return "ghs_phase4_known_canary_token_1234567890"

    async def request_json(self, method: str, path: str, **kwargs):
        body = kwargs.get("json_body")
        self.calls.append((method, path, body))
        if path.endswith("/git/ref/heads/mewcode%2F00000000-0000-0000-0000-000000000123"):
            if not self.branch_created:
                return None
            return {"object": {"sha": HEAD_SHA}}
        if path.endswith(f"/git/commits/{HEAD_SHA}"):
            return {
                "message": self.commit_message,
                "parents": [{"sha": BASE_SHA}],
            }
        if path.endswith("/pulls") and method == "GET":
            return [self.pull] if self.pull else []
        if path.endswith("/git/blobs"):
            return {"sha": "d" * 40}
        if path.endswith("/git/trees"):
            return {"sha": TREE_SHA}
        if path.endswith("/git/commits"):
            self.commit_message = body["message"]
            return {"sha": HEAD_SHA}
        if path.endswith("/git/refs"):
            assert method == "POST"
            assert "force" not in body
            self.branch_created = True
            return {"object": {"sha": HEAD_SHA}}
        if path.endswith("/pulls") and method == "POST":
            assert body["draft"] is True
            assert body["head"] == f"mewcode/{JOB_ID}"
            self.pull_title = body["title"]
            self.pull = {
                "number": 9,
                "html_url": "https://github.com/Acme/Repo/pull/9",
                "state": "open",
                "draft": True,
                "merged_at": None,
                "body": body["body"],
            }
            return self.pull
        raise AssertionError((method, path, kwargs))

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_publish_is_draft_idempotent_and_never_updates_ref(tmp_path: Path) -> None:
    source = _tar(
        tmp_path / "source.tar", {"hello.txt": b"old\n"}, prefix="Acme-Repo-base"
    )
    repository = tmp_path / "repository.tar"
    manifest = tmp_path / "manifest.json"
    normalize_source_archive(
        source,
        repository,
        manifest,
        base_sha=BASE_SHA,
        base_tree_sha=TREE_SHA,
    )
    workspace = _tar(tmp_path / "workspace.tar", {"hello.txt": b"new\n"})
    target = RepositoryTarget(7, "Acme", "Repo", "main", BASE_SHA)
    request = VerifiedDeliveryRequest(
        job_id=JOB_ID,
        prepared=PreparedRepository(target, TREE_SHA, repository, manifest),
        workspace_archive_path=workspace,
        work_title="Fix @team <bug>\x00",
        work_summary="Requester @team reported <failure>.",
        change_summary="Updated hello.txt",
        verification_summary="pytest: passed",
    )
    github = _PublishingGithub()
    adapter = GitHubScmAdapter(github)  # type: ignore[arg-type]
    first = await adapter.publish_verified(request)
    second = await adapter.publish_verified(request)
    assert first == second
    assert first.pr_number == 9
    assert github.pull is not None
    assert "@\u200bteam" in github.pull["body"]
    assert "&lt;failure&gt;" in github.pull["body"]
    assert github.pull_title == "Fix @\u200bteam &lt;bug&gt;"
    assert "\x00" not in github.commit_message
    assert all(method != "PATCH" for method, _, _ in github.calls)
    assert sum(path.endswith("/git/refs") for _, path, _ in github.calls) == 1
    assert sum(method == "POST" and path.endswith("/pulls") for method, path, _ in github.calls) == 1


@pytest.mark.asyncio
async def test_existing_foreign_branch_fails_closed(tmp_path: Path) -> None:
    github = _PublishingGithub()
    github.branch_created = True
    github.commit_message = "Human commit"
    target = RepositoryTarget(7, "Acme", "Repo", "main", BASE_SHA)
    request = VerifiedDeliveryRequest(
        job_id=JOB_ID,
        prepared=PreparedRepository(target, TREE_SHA, tmp_path / "x", tmp_path / "y"),
        workspace_archive_path=tmp_path / "z",
        work_title="Fix",
        work_summary="Fix",
        change_summary="Fix",
        verification_summary="passed",
    )
    with pytest.raises(ScmDeliveryConflict, match="not owned"):
        await GitHubScmAdapter(github).publish_verified(request)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_git_tree_walk_rejects_gitlinks() -> None:
    class Github:
        async def request_json(self, method: str, path: str, **kwargs):
            return {
                "tree": [
                    {
                        "path": "vendor/module",
                        "mode": "160000",
                        "type": "commit",
                        "sha": "d" * 40,
                    }
                ]
            }

    target = RepositoryTarget(7, "Acme", "Repo", "main", BASE_SHA)
    with pytest.raises(ScmPolicyError, match="submodules"):
        await GitHubScmAdapter(Github())._reject_gitlinks(  # type: ignore[arg-type]
            target, TREE_SHA, "ghs_test"
        )


class _RefRaceGithub(_PublishingGithub):
    async def request_json(self, method: str, path: str, **kwargs):
        if method == "POST" and path.endswith("/git/refs"):
            self.calls.append((method, path, kwargs.get("json_body")))
            self.branch_created = True
            raise GitHubConflict("concurrent ref")
        return await super().request_json(method, path, **kwargs)


class _PullRaceGithub(_PublishingGithub):
    async def request_json(self, method: str, path: str, **kwargs):
        if method == "POST" and path.endswith("/pulls"):
            body = kwargs["json_body"]
            self.calls.append((method, path, body))
            self.pull_title = body["title"]
            self.pull = {
                "number": 9,
                "html_url": "https://github.com/Acme/Repo/pull/9",
                "state": "open",
                "draft": True,
                "merged_at": None,
                "body": body["body"],
            }
            raise GitHubConflict("concurrent pull")
        return await super().request_json(method, path, **kwargs)


def _delivery_request(tmp_path: Path) -> VerifiedDeliveryRequest:
    source = _tar(
        tmp_path / "race-source.tar",
        {"hello.txt": b"old\n"},
        prefix="Acme-Repo-base",
    )
    repository = tmp_path / "race-repository.tar"
    manifest = tmp_path / "race-manifest.json"
    normalize_source_archive(
        source,
        repository,
        manifest,
        base_sha=BASE_SHA,
        base_tree_sha=TREE_SHA,
    )
    workspace = _tar(tmp_path / "race-workspace.tar", {"hello.txt": b"new\n"})
    target = RepositoryTarget(7, "Acme", "Repo", "main", BASE_SHA)
    return VerifiedDeliveryRequest(
        job_id=JOB_ID,
        prepared=PreparedRepository(target, TREE_SHA, repository, manifest),
        workspace_archive_path=workspace,
        work_title="Fix",
        work_summary="Fix",
        change_summary="Fix",
        verification_summary="passed",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("github_type", [_RefRaceGithub, _PullRaceGithub])
async def test_publish_converges_after_ref_or_pull_race(
    tmp_path: Path, github_type: type[_PublishingGithub]
) -> None:
    github = github_type()
    delivery = await GitHubScmAdapter(github).publish_verified(
        _delivery_request(tmp_path)
    )
    assert delivery.pr_number == 9
    assert delivery.head_sha == HEAD_SHA
    assert github.branch_created is True
    assert github.pull is not None


@pytest.mark.asyncio
async def test_closed_or_ready_pull_request_is_a_delivery_conflict(
    tmp_path: Path,
) -> None:
    github = _PublishingGithub()
    adapter = GitHubScmAdapter(github)  # type: ignore[arg-type]
    request = _delivery_request(tmp_path)
    await adapter.publish_verified(request)
    assert github.pull is not None
    github.pull["state"] = "closed"
    with pytest.raises(ScmDeliveryConflict, match="no longer an open Draft"):
        await adapter.publish_verified(request)
