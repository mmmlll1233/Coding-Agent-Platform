from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import json
import logging
import posixpath
import socket as stdlib_socket
import tarfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

from mewcode.tools.base import CommandExecutionResult

from .base import (
    ExecutionCleanupError,
    ExecutionEnvironmentError,
    ExecutionResourceLimitError,
    WorkspaceConflictError,
    WorkspacePathError,
)
from .fake import normalize_workspace_path
from .models import (
    MIB,
    AttemptExecutionSpec,
    ExecutionCommand,
    ExecutionCommandOutcome,
    ExecutionState,
    RuntimeEnvironmentInfo,
    WorkspaceReadResult,
    WorkspaceWriteResult,
)
from .redaction import SensitiveValueRedactor


log = logging.getLogger(__name__)
_MANAGED_LABEL = "com.mewcode.managed"
_JOB_LABEL = "com.mewcode.job_id"
_ATTEMPT_LABEL = "com.mewcode.attempt_id"
_RESOURCE_LABEL = "com.mewcode.resource"
_INTERNAL_ENV_NAMES = frozenset(
    {
        "MEWCODE_SECURITY_FIXTURE",
        "MEWCODE_TEST_HOST_CANARY_PATH",
        "MEWCODE_TEST_ALLOWED_URL",
    }
)
_WORKSPACE_HELPER_RETRY_DELAYS = (0.1, 0.2, 0.4, 0.8, 1.6)


class _TransientWorkspaceHelperError(ExecutionEnvironmentError):
    """Docker has not finished releasing a recently removed process tree."""


def _is_transient_workspace_helper_failure(value: object) -> bool:
    detail = value if isinstance(value, bytes) else str(value).encode(errors="replace")
    normalized = detail.lower()
    return (
        b"oci runtime exec failed" in normalized
        and b"procready not received" in normalized
    )


def _resource_suffix(spec: AttemptExecutionSpec) -> str:
    raw = f"{spec.job_id}\0{spec.attempt_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:20]


def _attempt_resource_id(resource: Any) -> str | None:
    labels = getattr(resource, "labels", None)
    if not isinstance(labels, dict):
        attrs = getattr(resource, "attrs", {})
        labels = attrs.get("Labels") if isinstance(attrs, dict) else None
        if not isinstance(labels, dict) and isinstance(attrs, dict):
            config = attrs.get("Config", {})
            labels = config.get("Labels") if isinstance(config, dict) else None
    if not isinstance(labels, dict) or labels.get(_MANAGED_LABEL) != "true":
        return None
    attempt_id = labels.get(_ATTEMPT_LABEL)
    return str(attempt_id) if attempt_id else None


def cleanup_orphaned_attempt_resources(
    active_attempt_ids: set[str] | frozenset[str],
    *,
    client: Any | None = None,
) -> tuple[int, int, int]:
    """Remove only MewCode Attempt resources absent from the DB live set."""
    owned_client = client is None
    if client is None:
        try:
            import docker
        except ImportError as exc:
            raise ExecutionCleanupError(
                "Docker SDK is required for orphan Attempt cleanup"
            ) from exc
        client = docker.from_env()
    active = {str(value) for value in active_attempt_ids}
    filters = {"label": [f"{_MANAGED_LABEL}=true", _ATTEMPT_LABEL]}
    removed = [0, 0, 0]
    errors: list[str] = []
    collections = (
        ("container", client.containers, True),
        ("network", client.networks, False),
        ("volume", client.volumes, True),
    )
    try:
        for index, (kind, collection, force) in enumerate(collections):
            try:
                resources = collection.list(
                    all=True, filters=filters
                ) if kind == "container" else collection.list(filters=filters)
            except Exception as exc:
                errors.append(f"{kind} enumeration: {exc}")
                continue
            for resource in resources:
                attempt_id = _attempt_resource_id(resource)
                if attempt_id is None or attempt_id in active:
                    continue
                try:
                    if force:
                        resource.remove(force=True)
                    else:
                        resource.remove()
                    removed[index] += 1
                except Exception as exc:
                    errors.append(f"{kind} removal: {exc}")
        remaining: list[str] = []
        for kind, collection, _ in collections:
            try:
                resources = collection.list(
                    all=True, filters=filters
                ) if kind == "container" else collection.list(filters=filters)
            except Exception as exc:
                errors.append(f"{kind} verification: {exc}")
                continue
            remaining.extend(
                f"{kind}:{attempt_id}"
                for resource in resources
                if (attempt_id := _attempt_resource_id(resource)) is not None
                and attempt_id not in active
            )
        if remaining:
            errors.append("orphan resources remain: " + ", ".join(sorted(remaining)))
        if errors:
            raise ExecutionCleanupError("; ".join(errors))
        return removed[0], removed[1], removed[2]
    finally:
        if owned_client:
            client.close()


def _tar_bytes(
    files: dict[str, bytes], *, uid: int = 65532, gid: int = 65532
) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for path, content in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(content)
            info.mode = 0o640
            info.mtime = 0
            info.uid = uid
            info.gid = gid
            archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


def _safe_archive_member(name: str) -> str:
    raw = name.replace("\\", "/")
    if raw.startswith("/"):
        raise ExecutionEnvironmentError(f"Unsafe archive path: {name!r}")
    while raw.startswith("./"):
        raw = raw[2:]
    normalized = posixpath.normpath(raw)
    pure = PurePosixPath(normalized)
    if (
        not normalized
        or pure.is_absolute()
        or any(part in ("", ".", "..") for part in pure.parts)
    ):
        raise ExecutionEnvironmentError(f"Unsafe archive path: {name!r}")
    return pure.as_posix()


def _split_workspace_archive(archive_bytes: bytes, max_bytes: int) -> tuple[bytes, bytes]:
    main = io.BytesIO()
    metadata = io.BytesIO()
    total = 0
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as source:
        with tarfile.open(fileobj=main, mode="w") as main_tar, tarfile.open(
            fileobj=metadata, mode="w"
        ) as metadata_tar:
            for member in source.getmembers():
                normalized = _safe_archive_member(member.name)
                if normalized == ".":
                    continue
                if normalized == ".git" or normalized.startswith(".git/"):
                    continue
                if normalized == ".mewcode" or normalized.startswith(".mewcode/"):
                    continue
                if normalized == ".env":
                    continue
                if member.isdev() or member.isfifo() or member.islnk():
                    raise ExecutionEnvironmentError(
                        f"Unsupported archive member: {member.name}"
                    )
                if member.issym():
                    target = PurePosixPath(member.linkname.replace("\\", "/"))
                    if target.is_absolute():
                        raise ExecutionEnvironmentError(
                            f"Absolute symlink is forbidden: {member.name}"
                        )
                    resolved = PurePosixPath(normalized).parent.joinpath(target)
                    depth = 0
                    for part in resolved.parts:
                        depth += -1 if part == ".." else 0 if part == "." else 1
                        if depth < 0:
                            raise ExecutionEnvironmentError(
                                f"Escaping symlink is forbidden: {member.name}"
                            )
                total += max(0, member.size)
                if total > max_bytes:
                    raise ExecutionEnvironmentError(
                        "Workspace archive exceeds configured capacity"
                    )
                extracted = source.extractfile(member) if member.isfile() else None
                if normalized == ".github" or normalized.startswith(".github/"):
                    relative = normalized.removeprefix(".github").lstrip("/") or "."
                    if relative == ".":
                        continue
                    cloned = copy.copy(member)
                    cloned.name = relative
                    cloned.uid = 65532
                    cloned.gid = 65532
                    metadata_tar.addfile(cloned, extracted)
                else:
                    cloned = copy.copy(member)
                    cloned.name = normalized
                    cloned.uid = 65532
                    cloned.gid = 65532
                    main_tar.addfile(cloned, extracted)
    return main.getvalue(), metadata.getvalue()


def _split_workspace_archive_file(
    source_path: Path, main_path: Path, metadata_path: Path, max_bytes: int
) -> None:
    total = 0
    with tarfile.open(source_path, mode="r:*") as source, tarfile.open(
        main_path, mode="w"
    ) as main_tar, tarfile.open(metadata_path, mode="w") as metadata_tar:
        for member in source.getmembers():
            normalized = _safe_archive_member(member.name)
            if normalized == ".":
                continue
            if normalized == ".git" or normalized.startswith(".git/"):
                continue
            if normalized == ".mewcode" or normalized.startswith(".mewcode/"):
                continue
            if normalized == ".env":
                continue
            if member.isdev() or member.isfifo() or member.islnk():
                raise ExecutionEnvironmentError(
                    f"Unsupported archive member: {member.name}"
                )
            if member.issym():
                target = PurePosixPath(member.linkname.replace("\\", "/"))
                if target.is_absolute():
                    raise ExecutionEnvironmentError(
                        f"Absolute symlink is forbidden: {member.name}"
                    )
                resolved = PurePosixPath(normalized).parent.joinpath(target)
                depth = 0
                for part in resolved.parts:
                    depth += -1 if part == ".." else 0 if part == "." else 1
                    if depth < 0:
                        raise ExecutionEnvironmentError(
                            f"Escaping symlink is forbidden: {member.name}"
                        )
            total += max(0, member.size)
            if total > max_bytes:
                raise ExecutionEnvironmentError(
                    "Workspace archive exceeds configured capacity"
                )
            extracted = source.extractfile(member) if member.isfile() else None
            cloned = copy.copy(member)
            cloned.uid = 65532
            cloned.gid = 65532
            if normalized == ".github" or normalized.startswith(".github/"):
                relative = normalized.removeprefix(".github").lstrip("/") or "."
                if relative == ".":
                    continue
                cloned.name = relative
                metadata_tar.addfile(cloned, extracted)
            else:
                cloned.name = normalized
                main_tar.addfile(cloned, extracted)


def _rebase_export_archive(archive_bytes: bytes) -> bytes:
    """Remove Docker's top-level ``workspace/`` archive wrapper."""
    output = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as source:
        with tarfile.open(fileobj=output, mode="w") as target:
            for member in source.getmembers():
                raw = member.name.replace("\\", "/").lstrip("./")
                if raw == "workspace":
                    continue
                if not raw.startswith("workspace/"):
                    raise ExecutionEnvironmentError(
                        f"Unexpected Docker archive member: {member.name!r}"
                    )
                normalized = _safe_archive_member(raw.removeprefix("workspace/"))
                cloned = copy.copy(member)
                cloned.name = normalized
                content = source.extractfile(member) if member.isfile() or member.islnk() else None
                if member.islnk():
                    data = content.read() if content is not None else b""
                    cloned.type = tarfile.REGTYPE
                    cloned.linkname = ""
                    cloned.size = len(data)
                    content = io.BytesIO(data)
                target.addfile(cloned, content)
    return output.getvalue()


def _rebase_export_archive_file(source_path: Path, destination_path: Path) -> None:
    with tarfile.open(source_path, mode="r:*") as source, tarfile.open(
        destination_path, mode="w"
    ) as target:
        for member in source.getmembers():
            raw = member.name.replace("\\", "/").lstrip("./")
            if raw == "workspace":
                continue
            if not raw.startswith("workspace/"):
                raise ExecutionEnvironmentError(
                    f"Unexpected Docker archive member: {member.name!r}"
                )
            normalized = _safe_archive_member(raw.removeprefix("workspace/"))
            cloned = copy.copy(member)
            cloned.name = normalized
            content = (
                source.extractfile(member)
                if member.isfile() or member.islnk()
                else None
            )
            if member.islnk():
                data = content.read() if content is not None else b""
                cloned.type = tarfile.REGTYPE
                cloned.linkname = ""
                cloned.size = len(data)
                content = io.BytesIO(data)
            target.addfile(cloned, content)


class DockerWorkspaceAccess:
    def __init__(self, environment: "DockerExecutionEnvironment") -> None:
        self.environment = environment

    async def _request(self, request: dict[str, Any]) -> dict[str, Any]:
        response = await self.environment._run_workspace_helper(request)
        if response.get("ok"):
            return response
        code = response.get("code")
        message = str(response.get("error", "workspace helper failed"))
        if code == "not_found":
            raise FileNotFoundError(message)
        if code == "workspace_path":
            raise WorkspacePathError(message)
        if code == "workspace_conflict":
            raise WorkspaceConflictError(message)
        if code == "resource_limit":
            raise ExecutionResourceLimitError(message)
        raise OSError(message)

    async def read_file(self, path: str) -> WorkspaceReadResult:
        normalize_workspace_path(path)
        response = await self._request({"op": "read", "path": path})
        return WorkspaceReadResult(
            content=str(response["content"]), version=str(response["version"])
        )

    async def write_file(
        self, path: str, content: str, expected_version: str | None
    ) -> WorkspaceWriteResult:
        normalize_workspace_path(path)
        response = await self._request(
            {
                "op": "write",
                "path": path,
                "content": content,
                "expected_version": expected_version,
            }
        )
        return WorkspaceWriteResult(version=str(response["version"]))

    async def edit_file(
        self,
        path: str,
        old_string: str,
        new_string: str,
        expected_version: str,
    ) -> WorkspaceWriteResult:
        normalize_workspace_path(path)
        response = await self._request(
            {
                "op": "edit",
                "path": path,
                "old_string": old_string,
                "new_string": new_string,
                "expected_version": expected_version,
            }
        )
        return WorkspaceWriteResult(version=str(response["version"]))

    async def glob(self, path: str, pattern: str) -> list[str]:
        response = await self._request(
            {"op": "glob", "path": path, "pattern": pattern}
        )
        return [str(value) for value in response["matches"]]

    async def grep(self, path: str, pattern: str, include: str) -> list[str]:
        response = await self._request(
            {
                "op": "grep",
                "path": path,
                "pattern": pattern,
                "include": include,
            }
        )
        return [str(value) for value in response["matches"]]


class DockerExecutionEnvironment:
    """A disposable, fail-closed Docker boundary for one Attempt."""

    def __init__(
        self,
        spec: AttemptExecutionSpec,
        client: Any | None = None,
        *,
        egress_network_name: str = "mewcode-phase2-egress",
    ) -> None:
        self.spec = spec
        self.state = ExecutionState.CREATED
        self.runtime_info = RuntimeEnvironmentInfo()
        self.workspace = DockerWorkspaceAccess(self)
        self._client = client
        self._suffix = _resource_suffix(spec)
        if not egress_network_name or len(egress_network_name) > 63:
            raise ValueError("Invalid deployment egress network name")
        self._egress_network_name = egress_network_name
        self._labels = {
            _MANAGED_LABEL: "true",
            _JOB_LABEL: spec.job_id,
            _ATTEMPT_LABEL: spec.attempt_id,
        }
        self._workspace_volume: Any | None = None
        self._metadata_volume: Any | None = None
        self._proxy_config_volume: Any | None = None
        self._attempt_network: Any | None = None
        self._egress_network: Any | None = None
        self._proxy_container: Any | None = None
        self._proxy_internal_address: str | None = None
        self._volume_holder_container: Any | None = None
        self._active_container: Any | None = None
        self._command_lock = asyncio.Lock()
        self._workspace_helper_lock = asyncio.Lock()
        self._started_monotonic: float | None = None
        self._workspace_imported = False
        self._redactor = SensitiveValueRedactor(spec.secret_values)

    def _docker(self) -> Any:
        if self._client is None:
            try:
                import docker
            except ImportError as exc:
                raise ExecutionEnvironmentError(
                    "Docker SDK is not installed; install the platform dependencies"
                ) from exc
            self._client = docker.from_env()
        return self._client

    def _resource_labels(self, kind: str) -> dict[str, str]:
        return {**self._labels, _RESOURCE_LABEL: kind}

    def _preflight_sync(self) -> None:
        client = self._docker()
        client.ping()
        info = client.info()
        if str(info.get("OSType", "")).lower() != "linux":
            raise ExecutionEnvironmentError("PLATFORM execution requires a Linux Docker Engine")
        if str(info.get("CgroupVersion", "")) != "2":
            raise ExecutionEnvironmentError("PLATFORM execution requires cgroup v2")
        client.images.get(self.spec.executor_image)
        client.images.get(self.spec.proxy_image)

    def _create_volume_sync(
        self,
        name: str,
        size: int,
        inodes: int,
        kind: str,
        *,
        uid: int = 65532,
        gid: int = 65532,
    ) -> Any:
        options = (
            f"size={size},nr_inodes={inodes},uid={uid},gid={gid},"
            "mode=0750,nosuid,nodev"
        )
        return self._docker().volumes.create(
            name=name,
            driver="local",
            driver_opts={"type": "tmpfs", "device": "tmpfs", "o": options},
            labels=self._resource_labels(kind),
        )

    def _create_egress_network_sync(self) -> Any:
        from docker.errors import NotFound

        client = self._docker()
        name = self._egress_network_name
        try:
            network = client.networks.get(name)
        except NotFound:
            try:
                network = client.networks.create(
                    name,
                    driver="bridge",
                    labels={_MANAGED_LABEL: "true", _RESOURCE_LABEL: "shared-egress"},
                )
            except Exception:
                network = client.networks.get(name)

        network.reload()
        attributes = network.attrs
        if attributes.get("Driver") != "bridge" or attributes.get("Internal") is True:
            raise ExecutionEnvironmentError(
                "The configured platform egress network must be a non-internal bridge"
            )
        return network

    def _seed_volume_sync(self, volume: Any, archive: Any, target: str = "/data") -> None:
        container = self._docker().containers.create(
            self.spec.executor_image,
            command=["/bin/sh", "-lc", "sleep 30"],
            user="65532:65532",
            network_mode="none",
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            tmpfs={"/tmp": "rw,noexec,nosuid,size=16m"},
            volumes={volume.name: {"bind": target, "mode": "rw"}},
            labels=self._resource_labels("seed"),
        )
        try:
            container.start()
            if archive:
                container.put_archive(target, archive)
        finally:
            try:
                container.remove(force=True)
            except Exception:
                log.exception("Failed to remove workspace seed container")

    def _start_volume_holder_sync(self) -> None:
        assert self._workspace_volume is not None
        assert self._metadata_volume is not None
        assert self._proxy_config_volume is not None
        holder = self._docker().containers.create(
            self.spec.executor_image,
            name=f"mewcode-volume-holder-{self._suffix}",
            command=["sleep", "infinity"],
            # Workspace operations use repeated ``docker exec`` calls.  Docker's
            # init process reaps children, while the trusted-helper-only PID
            # budget leaves room for exec teardown on slower Linux hosts.
            init=True,
            user="65532:65532",
            network_mode="none",
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            pids_limit=128,
            mem_limit=32 * MIB,
            memswap_limit=32 * MIB,
            volumes={
                **self._mounts(),
                self._proxy_config_volume.name: {
                    "bind": "/proxy-config",
                    "mode": "rw",
                },
            },
            labels=self._resource_labels("volume-holder"),
        )
        holder.start()
        self._volume_holder_container = holder

    def _restart_volume_holder_sync(self) -> None:
        holder = self._volume_holder_container
        self._volume_holder_container = None
        if holder is not None:
            holder.remove(force=True)
        self._start_volume_holder_sync()

    def _squid_config(self) -> bytes:
        domains = " ".join(self.spec.egress_allowlist)
        config = f"""
http_port 3128
acl Safe_ports port 443
acl CONNECT method CONNECT
acl allowed_domains dstdomain {domains}
acl forbidden_dst dst 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8
acl forbidden_dst dst 169.254.0.0/16 172.16.0.0/12 192.168.0.0/16 224.0.0.0/4
acl forbidden_dst dst ::1/128 fc00::/7 fe80::/10
http_access deny !Safe_ports
http_access deny CONNECT !Safe_ports
http_access deny !CONNECT
http_access deny forbidden_dst
http_access allow allowed_domains
http_access deny all
cache deny all
access_log none
cache_log /dev/null
cache_store_log none
pid_filename none
visible_hostname mewcode-egress-proxy
""".lstrip()
        return config.encode("utf-8")

    def _start_proxy_sync(self) -> None:
        assert self._attempt_network is not None
        assert self._egress_network is not None
        assert self._proxy_config_volume is not None
        proxy = self._docker().containers.create(
            self.spec.proxy_image,
            name=f"mewcode-proxy-{self._suffix}",
            entrypoint=["/usr/sbin/squid"],
            command=["-f", "/etc/squid/mewcode/squid.conf", "-NYC"],
            user="13:13",
            network=self._attempt_network.name,
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            pids_limit=64,
            mem_limit=256 * MIB,
            memswap_limit=256 * MIB,
            tmpfs={
                "/run": "rw,noexec,nosuid,size=8m,uid=13,gid=13,mode=0750",
                "/var/log/squid": "rw,noexec,nosuid,size=8m,uid=13,gid=13,mode=0750",
                "/var/spool/squid": "rw,noexec,nosuid,size=32m,uid=13,gid=13,mode=0750",
            },
            volumes={
                self._proxy_config_volume.name: {
                    "bind": "/etc/squid/mewcode",
                    "mode": "ro",
                }
            },
            labels=self._resource_labels("proxy"),
        )
        # Attach egress last so it owns the proxy's default route. Command
        # containers remain connected only to the internal Attempt network.
        self._egress_network.connect(proxy)
        proxy.start()
        self._proxy_container = proxy
        time.sleep(0.25)
        proxy.reload()
        if proxy.status != "running":
            detail = proxy.logs(tail=50).decode(errors="replace")
            raise ExecutionEnvironmentError(
                "Egress proxy failed to start: "
                + self._redactor.redact(detail[-4000:])
            )
        networks = proxy.attrs.get("NetworkSettings", {}).get("Networks", {})
        internal = networks.get(self._attempt_network.name, {})
        address = str(internal.get("IPAddress", ""))
        if not address:
            raise ExecutionEnvironmentError(
                "Egress proxy has no address on the Attempt network"
            )
        self._proxy_internal_address = address

    def _start_sync(self) -> None:
        self._preflight_sync()
        client = self._docker()
        limits = self.spec.limits
        self._workspace_volume = self._create_volume_sync(
            f"mewcode-workspace-{self._suffix}",
            limits.workspace_bytes,
            limits.workspace_inodes,
            "workspace",
        )
        self._metadata_volume = self._create_volume_sync(
            f"mewcode-metadata-{self._suffix}", 64 * MIB, 20_000, "metadata"
        )
        self._proxy_config_volume = self._create_volume_sync(
            f"mewcode-proxy-config-{self._suffix}",
            MIB,
            128,
            "proxy-config",
            uid=13,
            gid=13,
        )
        self._attempt_network = client.networks.create(
            f"mewcode-attempt-{self._suffix}",
            driver="bridge",
            internal=True,
            options={"com.docker.network.bridge.gateway_mode_ipv4": "isolated"},
            labels=self._resource_labels("attempt-network"),
        )
        self._egress_network = self._create_egress_network_sync()
        self._start_volume_holder_sync()
        self._seed_volume_sync(
            self._proxy_config_volume,
            _tar_bytes({"squid.conf": self._squid_config()}, uid=13, gid=13),
        )
        self._start_proxy_sync()

    async def start(self) -> None:
        if self.state == ExecutionState.READY:
            return
        if self.state == ExecutionState.CLOSED:
            raise ExecutionEnvironmentError("Cannot start a closed ExecutionEnvironment")
        if self.state != ExecutionState.CREATED:
            raise ExecutionEnvironmentError(f"Cannot start environment in state {self.state}")
        self.state = ExecutionState.STARTING
        self.spec.trusted_state_dir.mkdir(parents=True, exist_ok=True)
        try:
            await asyncio.to_thread(self._start_sync)
        except Exception:
            self.state = ExecutionState.BROKEN
            try:
                await asyncio.to_thread(self._cleanup_sync)
            except Exception:
                log.exception("Cleanup after Docker start failure also failed")
            raise
        self._started_monotonic = time.monotonic()
        self.state = ExecutionState.READY

    def _mounts(self, metadata_mode: str = "ro") -> dict[str, dict[str, str]]:
        assert self._workspace_volume is not None
        assert self._metadata_volume is not None
        return {
            self._workspace_volume.name: {"bind": "/workspace", "mode": "rw"},
            self._metadata_volume.name: {
                "bind": "/workspace/.github",
                "mode": metadata_mode,
            },
        }

    def _fixed_environment(self, internal: dict[str, str]) -> dict[str, str]:
        forbidden = set(internal) - _INTERNAL_ENV_NAMES
        if forbidden:
            raise ExecutionEnvironmentError(
                "Unapproved internal environment variables: " + ", ".join(sorted(forbidden))
            )
        proxy_address = self._proxy_internal_address or ""
        environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": "/home/mewcode",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TZ": "UTC",
            "CI": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": "/dev/null",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
            "HTTP_PROXY": f"http://{proxy_address}:3128",
            "HTTPS_PROXY": f"http://{proxy_address}:3128",
            "http_proxy": f"http://{proxy_address}:3128",
            "https_proxy": f"http://{proxy_address}:3128",
            "NO_PROXY": "",
            "no_proxy": "",
        }
        environment.update(internal)
        return environment

    def _create_command_container_sync(self, command: ExecutionCommand) -> Any:
        try:
            from docker.types import LogConfig, Ulimit
        except ImportError as exc:
            raise ExecutionEnvironmentError("Docker SDK is not installed") from exc
        limits = self.spec.limits
        container = self._docker().containers.create(
            self.spec.executor_image,
            command=["/bin/sh", "-lc", command.command],
            user="65532:65532",
            working_dir="/workspace",
            environment=self._fixed_environment(dict(command.internal_env)),
            network=self._attempt_network.name,
            # Command containers never use Docker's external DNS forwarding;
            # they reach the proxy by its internal IP and Squid resolves the
            # allowlisted target on its separate egress NIC.
            dns=["127.0.0.1"],
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            privileged=False,
            init=True,
            pids_limit=limits.pids_limit,
            mem_limit=limits.memory_bytes,
            memswap_limit=limits.memory_bytes,
            nano_cpus=int(limits.cpus * 1_000_000_000),
            ulimits=[
                Ulimit(name="nofile", soft=limits.nofile_limit, hard=limits.nofile_limit),
                Ulimit(name="nproc", soft=limits.pids_limit, hard=limits.pids_limit),
            ],
            tmpfs={
                "/tmp": f"rw,noexec,nosuid,size={limits.tmp_bytes},uid=65532,gid=65532",
                "/home/mewcode": "rw,noexec,nosuid,size=16m,uid=65532,gid=65532",
            },
            volumes=self._mounts(),
            labels=self._resource_labels("command"),
            log_config=LogConfig(
                type="local",
                config={
                    # Keep a small detection margin beyond the observable cap;
                    # the poller kills the container as soon as the cap is crossed.
                    "max-size": str(limits.max_output_bytes + 64 * 1024),
                    "max-file": "1",
                    "compress": "false",
                },
            ),
        )
        self._active_container = container
        return container

    def _container_snapshot_sync(
        self, container: Any
    ) -> tuple[bool, int | None, str, str, bool, bool]:
        container.reload()
        state = container.attrs.get("State", {})
        stdout_bytes = container.logs(stdout=True, stderr=False)
        stderr_bytes = container.logs(stdout=False, stderr=True)
        stdout_bytes = stdout_bytes or b""
        stderr_bytes = stderr_bytes or b""
        limit = self.spec.limits.max_output_bytes
        combined = stdout_bytes + stderr_bytes
        stdout_size = min(len(stdout_bytes), limit)
        remaining = max(0, limit - stdout_size)
        stdout = stdout_bytes[:stdout_size].decode(errors="replace")
        stderr = stderr_bytes[:remaining].decode(errors="replace")
        exit_code = state.get("ExitCode")
        return (
            bool(state.get("Running", False)),
            int(exit_code) if exit_code is not None else None,
            stdout,
            stderr,
            bool(state.get("OOMKilled", False)),
            len(combined) > limit,
        )

    def _remove_container_sync(self, container: Any) -> None:
        try:
            container.remove(force=True)
        finally:
            if self._active_container is container:
                self._active_container = None

    async def _terminate_active(self) -> None:
        container = self._active_container
        if container is None:
            return
        await asyncio.to_thread(self._remove_container_sync, container)

    async def run_command(self, command: ExecutionCommand) -> ExecutionCommandOutcome:
        if self.state != ExecutionState.READY:
            raise ExecutionEnvironmentError("ExecutionEnvironment is not ready")
        if not command.command or "\x00" in command.command:
            raise ExecutionEnvironmentError("Command must be non-empty and contain no NUL")
        if self._started_monotonic is not None and (
            time.monotonic() - self._started_monotonic
            >= self.spec.limits.attempt_timeout_seconds
        ):
            self.state = ExecutionState.BROKEN
            return ExecutionCommandOutcome(
                CommandExecutionResult(None, "", "Attempt deadline exceeded"),
                "ATTEMPT_DEADLINE_EXCEEDED",
                "Attempt exceeded its execution deadline",
            )
        timeout = min(
            max(1, command.timeout_seconds),
            self.spec.limits.command_timeout_seconds,
        )
        async with self._command_lock:
            try:
                container = await asyncio.to_thread(
                    self._create_command_container_sync, command
                )
                try:
                    try:
                        await asyncio.to_thread(container.start)
                        deadline = time.monotonic() + timeout
                        while True:
                            result = await asyncio.to_thread(
                                self._container_snapshot_sync, container
                            )
                            (
                                running,
                                exit_code,
                                stdout,
                                stderr,
                                oom_killed,
                                output_limited,
                            ) = result
                            if output_limited:
                                await asyncio.shield(self._terminate_active())
                                self.state = ExecutionState.BROKEN
                                return ExecutionCommandOutcome(
                                    CommandExecutionResult(
                                        exit_code=exit_code,
                                        stdout=self._redactor.redact(stdout),
                                        stderr=self._redactor.redact(stderr),
                                    ),
                                    "EXECUTION_OUTPUT_LIMIT",
                                    "Executor output exceeded the configured limit",
                                )
                            if not running:
                                break
                            if time.monotonic() >= deadline:
                                await asyncio.shield(self._terminate_active())
                                return ExecutionCommandOutcome(
                                    CommandExecutionResult(
                                        exit_code=137,
                                        stdout=self._redactor.redact(stdout),
                                        stderr=self._redactor.redact(stderr),
                                        timed_out=True,
                                    )
                                )
                            await asyncio.sleep(0.05)
                    except asyncio.CancelledError:
                        await asyncio.shield(self._terminate_active())
                        raise
                    stdout = self._redactor.redact(stdout)
                    stderr = self._redactor.redact(stderr)
                    if oom_killed:
                        self.state = ExecutionState.BROKEN
                        return ExecutionCommandOutcome(
                            CommandExecutionResult(exit_code, stdout, stderr),
                            "EXECUTION_RESOURCE_LIMIT",
                            "Executor exceeded its memory limit",
                        )
                    lowered_error = stderr.lower()
                    if any(
                        marker in lowered_error
                        for marker in (
                            "no space left on device",
                            "disk quota exceeded",
                            "resource temporarily unavailable",
                            "cannot fork",
                            "fork: retry",
                        )
                    ):
                        self.state = ExecutionState.BROKEN
                        return ExecutionCommandOutcome(
                            CommandExecutionResult(exit_code, stdout, stderr),
                            "EXECUTION_RESOURCE_LIMIT",
                            "Executor reached a configured disk, inode, or process limit",
                        )
                    return ExecutionCommandOutcome(
                        CommandExecutionResult(exit_code, stdout, stderr)
                    )
                finally:
                    if self._active_container is container:
                        await asyncio.shield(
                            asyncio.to_thread(self._remove_container_sync, container)
                        )
            except asyncio.CancelledError:
                raise
            except ExecutionEnvironmentError:
                self.state = ExecutionState.BROKEN
                raise
            except Exception as exc:
                self.state = ExecutionState.BROKEN
                raise ExecutionEnvironmentError(
                    f"Docker executor failure: {exc}"
                ) from exc

    def _workspace_helper_sync(self, request: dict[str, Any]) -> dict[str, Any]:
        assert self._volume_holder_container is not None
        from docker.utils.socket import STDERR, STDOUT, frames_iter

        payload = json.dumps(request, ensure_ascii=False).encode("utf-8")
        if len(payload) > 16 * MIB:
            raise ExecutionEnvironmentError("Workspace helper request limit exceeded")
        api = self._docker().api
        try:
            created = api.exec_create(
                self._volume_holder_container.id,
                [
                    "python",
                    "/opt/mewcode/workspace_helper.py",
                    "--stdin-size",
                    str(len(payload)),
                ],
                stdin=True,
                stdout=True,
                stderr=True,
                user="65532:65532",
                workdir="/workspace",
            )
            socket = api.exec_start(created["Id"], socket=True)
            stdout_parts: list[bytes] = []
            stderr_parts: list[bytes] = []
            try:
                writer = socket if hasattr(socket, "sendall") else socket._sock
                writer.sendall(payload)
                if isinstance(writer, stdlib_socket.socket):
                    writer.shutdown(stdlib_socket.SHUT_WR)
                observed = 0
                for stream, data in frames_iter(socket, tty=False):
                    observed += len(data)
                    if observed > self.spec.limits.max_output_bytes:
                        raise ExecutionEnvironmentError(
                            "Workspace helper output limit exceeded"
                        )
                    if stream == STDOUT:
                        stdout_parts.append(data)
                    elif stream == STDERR:
                        stderr_parts.append(data)
            finally:
                response = getattr(socket, "_response", None)
                if response is not None:
                    response.close()
                else:
                    socket.close()
            inspected = api.exec_inspect(created["Id"])
        except _TransientWorkspaceHelperError:
            raise
        except Exception as exc:
            if _is_transient_workspace_helper_failure(exc):
                raise _TransientWorkspaceHelperError(
                    "Docker workspace helper was temporarily unavailable"
                ) from exc
            raise
        stdout = b"".join(stdout_parts)
        stderr = b"".join(stderr_parts)
        if _is_transient_workspace_helper_failure(stdout + stderr):
            raise _TransientWorkspaceHelperError(
                "Docker workspace helper was temporarily unavailable"
            )
        if not stdout:
            detail = stderr.decode(errors="replace")
            raise ExecutionEnvironmentError(
                f"Workspace helper exited {inspected.get('ExitCode')}: {detail}"
            )
        return json.loads(stdout.decode("utf-8"))

    async def _run_workspace_helper(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.state != ExecutionState.READY:
            raise ExecutionEnvironmentError("ExecutionEnvironment is not ready")
        async with self._workspace_helper_lock:
            if self.state != ExecutionState.READY:
                raise ExecutionEnvironmentError("ExecutionEnvironment is not ready")
            last_transient: _TransientWorkspaceHelperError | None = None
            for holder_generation in range(2):
                for delay in (*_WORKSPACE_HELPER_RETRY_DELAYS, None):
                    try:
                        return await asyncio.to_thread(
                            self._workspace_helper_sync, request
                        )
                    except _TransientWorkspaceHelperError as exc:
                        last_transient = exc
                        if delay is None:
                            break
                        await asyncio.sleep(delay)
                    except ExecutionEnvironmentError:
                        self.state = ExecutionState.BROKEN
                        raise
                    except Exception as exc:
                        self.state = ExecutionState.BROKEN
                        raise ExecutionEnvironmentError(
                            f"Docker workspace helper failure: {exc}"
                        ) from exc
                if holder_generation == 0:
                    try:
                        await asyncio.to_thread(self._restart_volume_holder_sync)
                    except Exception as exc:
                        self.state = ExecutionState.BROKEN
                        raise ExecutionEnvironmentError(
                            "Docker workspace helper recovery failed"
                        ) from exc
                    continue
                self.state = ExecutionState.BROKEN
                raise ExecutionEnvironmentError(
                    "Docker workspace helper remained unavailable"
                ) from last_transient
            if last_transient is not None:
                self.state = ExecutionState.BROKEN
                raise ExecutionEnvironmentError(
                    "Docker workspace helper remained unavailable"
                ) from last_transient
            raise AssertionError("Workspace helper retry loop did not terminate")

    async def import_archive(self, archive: bytes) -> None:
        if self.state != ExecutionState.READY:
            raise ExecutionEnvironmentError("ExecutionEnvironment is not ready")
        if self._workspace_imported:
            self.state = ExecutionState.BROKEN
            raise ExecutionEnvironmentError(
                "Attempt Workspace can be initialized only once"
            )
        try:
            main, metadata = await asyncio.to_thread(
                _split_workspace_archive, archive, self.spec.limits.workspace_bytes
            )
            assert self._workspace_volume is not None
            assert self._metadata_volume is not None
            await asyncio.to_thread(
                self._seed_volume_sync, self._workspace_volume, main
            )
            await asyncio.to_thread(
                self._seed_volume_sync, self._metadata_volume, metadata
            )
        except ExecutionEnvironmentError:
            self.state = ExecutionState.BROKEN
            raise
        except Exception as exc:
            self.state = ExecutionState.BROKEN
            message = str(exc)
            if "no space left" in message.lower() or "quota" in message.lower():
                raise ExecutionResourceLimitError(
                    "Attempt Workspace import exceeded its resource limit"
                ) from exc
            raise ExecutionEnvironmentError(
                f"Attempt Workspace import failed: {exc}"
            ) from exc
        self._workspace_imported = True

    async def import_archive_file(self, archive_path: Path) -> None:
        if self.state != ExecutionState.READY:
            raise ExecutionEnvironmentError("ExecutionEnvironment is not ready")
        if self._workspace_imported:
            self.state = ExecutionState.BROKEN
            raise ExecutionEnvironmentError(
                "Attempt Workspace can be initialized only once"
            )
        main_path = self.spec.trusted_state_dir / "workspace-main.tar"
        metadata_path = self.spec.trusted_state_dir / "workspace-metadata.tar"
        try:
            await asyncio.to_thread(
                _split_workspace_archive_file,
                Path(archive_path),
                main_path,
                metadata_path,
                self.spec.limits.workspace_bytes,
            )
            assert self._workspace_volume is not None
            assert self._metadata_volume is not None
            with main_path.open("rb") as main, metadata_path.open("rb") as metadata:
                await asyncio.to_thread(
                    self._seed_volume_sync, self._workspace_volume, main
                )
                await asyncio.to_thread(
                    self._seed_volume_sync, self._metadata_volume, metadata
                )
        except Exception as exc:
            self.state = ExecutionState.BROKEN
            if isinstance(exc, ExecutionEnvironmentError):
                raise
            message = str(exc)
            if "no space left" in message.lower() or "quota" in message.lower():
                raise ExecutionResourceLimitError(
                    "Attempt Workspace file import exceeded its resource limit"
                ) from exc
            raise ExecutionEnvironmentError(
                f"Attempt Workspace file import failed: {exc}"
            ) from exc
        finally:
            main_path.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
        self._workspace_imported = True

    def _export_archive_sync(self) -> bytes:
        container = self._docker().containers.create(
            self.spec.executor_image,
            command=["/bin/sh", "-lc", "sleep 30"],
            user="65532:65532",
            network_mode="none",
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            volumes=self._mounts(),
            labels=self._resource_labels("export"),
        )
        try:
            container.start()
            stream, _ = container.get_archive("/workspace")
            return _rebase_export_archive(b"".join(stream))
        finally:
            container.remove(force=True)

    async def export_archive(self) -> bytes:
        if self.state != ExecutionState.READY:
            raise ExecutionEnvironmentError("ExecutionEnvironment is not ready")
        return await asyncio.to_thread(self._export_archive_sync)

    def _export_archive_file_sync(self, destination_path: Path) -> None:
        raw_path = destination_path.with_suffix(destination_path.suffix + ".docker")
        destination_path.unlink(missing_ok=True)
        container = self._docker().containers.create(
            self.spec.executor_image,
            command=["/bin/sh", "-lc", "sleep 30"],
            user="65532:65532",
            network_mode="none",
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            volumes=self._mounts(),
            labels=self._resource_labels("export"),
        )
        try:
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            container.start()
            stream, _ = container.get_archive("/workspace")
            with raw_path.open("wb") as output:
                for chunk in stream:
                    output.write(chunk)
            _rebase_export_archive_file(raw_path, destination_path)
        except Exception:
            destination_path.unlink(missing_ok=True)
            raise
        finally:
            raw_path.unlink(missing_ok=True)
            container.remove(force=True)

    async def export_archive_file(self, archive_path: Path) -> None:
        if self.state != ExecutionState.READY:
            raise ExecutionEnvironmentError("ExecutionEnvironment is not ready")
        await asyncio.to_thread(self._export_archive_file_sync, Path(archive_path))

    def _cleanup_sync(self) -> None:
        errors: list[str] = []
        filters = {
            "label": [
                f"{_MANAGED_LABEL}=true",
                f"{_JOB_LABEL}={self.spec.job_id}",
                f"{_ATTEMPT_LABEL}={self.spec.attempt_id}",
            ]
        }
        client = self._client
        resources = [
            self._active_container,
            self._proxy_container,
            self._volume_holder_container,
        ]
        for container in resources:
            if container is None:
                continue
            try:
                container.remove(force=True)
            except Exception as exc:
                log.warning("Initial Attempt container removal failed: %s", exc)
        self._active_container = None
        self._proxy_container = None
        self._proxy_internal_address = None
        self._volume_holder_container = None
        if client is not None:
            try:
                for container in client.containers.list(all=True, filters=filters):
                    try:
                        container.remove(force=True)
                    except Exception as exc:
                        log.warning("Label-sweep container removal failed: %s", exc)
            except Exception as exc:
                errors.append(f"container enumeration: {exc}")
        if self._attempt_network is not None:
            try:
                self._attempt_network.remove()
            except Exception as exc:
                log.warning("Initial Attempt network removal failed: %s", exc)
            self._attempt_network = None
        if client is not None:
            try:
                for network in client.networks.list(filters=filters):
                    try:
                        network.remove()
                    except Exception as exc:
                        log.warning("Label-sweep network removal failed: %s", exc)
            except Exception as exc:
                errors.append(f"network enumeration: {exc}")
        for volume in (
            self._workspace_volume,
            self._metadata_volume,
            self._proxy_config_volume,
        ):
            if volume is None:
                continue
            try:
                volume.remove(force=True)
            except Exception as exc:
                log.warning("Initial Attempt volume removal failed: %s", exc)
        self._workspace_volume = None
        self._metadata_volume = None
        self._proxy_config_volume = None
        if client is not None:
            try:
                for volume in client.volumes.list(filters=filters):
                    try:
                        volume.remove(force=True)
                    except Exception as exc:
                        log.warning("Label-sweep volume removal failed: %s", exc)
            except Exception as exc:
                errors.append(f"volume enumeration: {exc}")

        if client is not None:
            try:
                leftovers = client.containers.list(all=True, filters=filters)
                leftover_volumes = client.volumes.list(filters=filters)
                leftover_networks = client.networks.list(filters=filters)
                if leftovers or leftover_volumes or leftover_networks:
                    errors.append("managed Docker resources remain after cleanup")
            except Exception as exc:
                errors.append(f"cleanup verification: {exc}")
        if errors:
            raise ExecutionCleanupError("; ".join(errors))

    async def aclose(self) -> None:
        if self.state == ExecutionState.CLOSED:
            return
        self.state = ExecutionState.CLOSING
        try:
            await asyncio.wait_for(asyncio.to_thread(self._cleanup_sync), timeout=10)
        finally:
            self.state = ExecutionState.CLOSED
