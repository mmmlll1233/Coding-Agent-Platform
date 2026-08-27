from __future__ import annotations

import asyncio
import io
import os
import tarfile
import time
import uuid
from pathlib import Path

import pytest

from mewcode.platform.execution import (
    AttemptExecutionSpec,
    DockerExecutionEnvironment,
    ExecutionCommand,
    ExecutionLimits,
    WorkspacePathError,
    create_platform_registry,
)
from mewcode.tools.read_file import Params as ReadParams


FIXTURES = Path(__file__).parent / "fixtures" / "malicious_repositories"
MIB = 1024**2


def _images() -> tuple[str, str]:
    executor = os.environ.get("MEWCODE_EXECUTOR_IMAGE", "")
    proxy = os.environ.get("MEWCODE_PROXY_IMAGE", "")
    if not executor or not proxy:
        pytest.fail(
            "MEWCODE_EXECUTOR_IMAGE and MEWCODE_PROXY_IMAGE must be immutable "
            "digests when Docker executor tests are selected"
        )
    return executor, proxy


def _archive_directory(root: Path, extra: dict[str, bytes] | None = None) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for candidate in sorted(root.rglob("*")):
            relative = candidate.relative_to(root).as_posix()
            if candidate.is_dir():
                info = tarfile.TarInfo(relative)
                info.type = tarfile.DIRTYPE
                info.mode = 0o750
                archive.addfile(info)
            elif candidate.is_file():
                data = candidate.read_bytes()
                info = tarfile.TarInfo(relative)
                info.size = len(data)
                info.mode = 0o640
                archive.addfile(info, io.BytesIO(data))
        for name, data in (extra or {}).items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o640
            archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


def _labels(job_id: str, attempt_id: str) -> dict[str, list[str]]:
    return {
        "label": [
            "com.mewcode.managed=true",
            f"com.mewcode.job_id={job_id}",
            f"com.mewcode.attempt_id={attempt_id}",
        ]
    }


def _new_environment(
    tmp_path: Path,
    *,
    limits: ExecutionLimits | None = None,
    secret_values: tuple[str, ...] = (),
    egress_allowlist: tuple[str, ...] | None = None,
    egress_network_name: str = "mewcode-phase2-egress",
) -> DockerExecutionEnvironment:
    executor, proxy = _images()
    suffix = uuid.uuid4().hex[:12]
    spec = AttemptExecutionSpec(
        job_id=f"docker-job-{suffix}",
        attempt_id=f"attempt-{suffix}",
        executor_image=executor,
        proxy_image=proxy,
        trusted_state_dir=tmp_path / suffix,
        limits=limits
        or ExecutionLimits(
            cpus=1,
            memory_bytes=256 * MIB,
            pids_limit=64,
            workspace_bytes=32 * MIB,
            tmp_bytes=16 * MIB,
            workspace_inodes=10_000,
            command_timeout_seconds=20,
            attempt_timeout_seconds=120,
            max_output_bytes=256 * 1024,
        ),
        secret_values=secret_values,
        **({"egress_allowlist": egress_allowlist} if egress_allowlist else {}),
    )
    return DockerExecutionEnvironment(
        spec, egress_network_name=egress_network_name
    )


def _start_mock_package_endpoint() -> tuple[object, object, str]:
    import docker

    image = os.environ.get("MEWCODE_MOCK_PACKAGE_IMAGE", "")
    if not image.startswith("sha256:"):
        pytest.fail("MEWCODE_MOCK_PACKAGE_IMAGE must be an immutable digest")
    client = docker.from_env()
    suffix = uuid.uuid4().hex[:12]
    third_octet = int(suffix[:2], 16)
    subnet = f"198.18.{third_octet}.0/24"
    ipam = docker.types.IPAMConfig(
        pool_configs=[
            docker.types.IPAMPool(
                subnet=subnet,
                gateway=f"198.18.{third_octet}.1",
            )
        ]
    )
    network = client.networks.create(
        f"mewcode-mock-egress-{suffix}",
        driver="bridge",
        ipam=ipam,
        labels={"com.mewcode.test-resource": "mock-package-egress"},
    )
    endpoint = client.api.create_endpoint_config(aliases=["packages.test"])
    container = client.containers.create(
        image,
        name=f"mewcode-mock-package-{suffix}",
        network=network.name,
        networking_config={network.name: endpoint},
        read_only=True,
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"],
        pids_limit=32,
        mem_limit=128 * MIB,
        memswap_limit=128 * MIB,
        labels={"com.mewcode.test-resource": "mock-package"},
    )
    container.start()
    container.reload()
    address = container.attrs["NetworkSettings"]["Networks"][network.name][
        "IPAddress"
    ]
    return network, container, str(address)


async def _close_and_assert_clean(environment: DockerExecutionEnvironment) -> None:
    await environment.aclose()
    client = environment._docker()
    filters = _labels(environment.spec.job_id, environment.spec.attempt_id)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not (
            client.containers.list(all=True, filters=filters)
            or client.networks.list(filters=filters)
            or client.volumes.list(filters=filters)
        ):
            return
        await asyncio.sleep(0.1)
    pytest.fail("Attempt Docker resources remained after bounded cleanup")


@pytest.mark.executor_security
@pytest.mark.asyncio
async def test_executor_inspect_contract_and_secret_canary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canaries = {
        "MEWCODE_TEST_LLM_SECRET": "worker-llm-canary-4bdf8d8d",
        "MEWCODE_TEST_GITHUB_TOKEN": "ghs_phase4-canary_7ace9102.test",
        "MEWCODE_TEST_FEISHU_SECRET": "worker-feishu-canary-98d512ab",
    }
    for name, value in canaries.items():
        monkeypatch.setenv(name, value)
    environment = _new_environment(
        tmp_path, secret_values=tuple(canaries.values())
    )
    try:
        await environment.start()
        source_archive = tmp_path / "source.tar"
        source_archive.write_bytes(_archive_directory(FIXTURES / "secret_canary"))
        await environment.import_archive_file(source_archive)
        outcome = await environment.run_command(
            ExecutionCommand(
                "python probe.py; printf 'uid='; id -u; "
                "test ! -S /var/run/docker.sock; test ! -e /.dockerenv-host",
                timeout_seconds=10,
                internal_env={"MEWCODE_SECURITY_FIXTURE": "executor"},
            )
        )
        assert outcome.command_result.exit_code == 0
        assert not any(
            canary in outcome.command_result.stdout for canary in canaries.values()
        )
        assert "uid=65532" in outcome.command_result.stdout
        assert not any(name in outcome.command_result.stdout for name in canaries)
        assert not any(canary in repr(outcome) for canary in canaries.values())

        exported_archive = tmp_path / "exported.tar"
        await environment.export_archive_file(exported_archive)
        exported = exported_archive.read_bytes()
        assert not any(canary.encode() in exported for canary in canaries.values())

        running = asyncio.create_task(
            environment.run_command(ExecutionCommand("sleep 2", timeout_seconds=5))
        )
        for _ in range(100):
            if environment._active_container is not None:
                break
            await asyncio.sleep(0.02)
        assert environment._active_container is not None
        environment._active_container.reload()
        attrs = environment._active_container.attrs
        host = attrs["HostConfig"]
        config = attrs["Config"]
        assert not any(canary in repr(attrs) for canary in canaries.values())
        assert host["ReadonlyRootfs"] is True
        assert host["Privileged"] is False
        assert host["CapDrop"] == ["ALL"]
        assert "no-new-privileges:true" in host["SecurityOpt"]
        assert host["PidsLimit"] == environment.spec.limits.pids_limit
        assert host["Memory"] == environment.spec.limits.memory_bytes
        assert host["MemorySwap"] == environment.spec.limits.memory_bytes
        assert host["NanoCpus"] == 1_000_000_000
        assert host["Dns"] == ["127.0.0.1"]
        assert config["User"] == "65532:65532"
        assert not host.get("PortBindings")
        assert all(
            mount["Type"] == "volume" and "/var/run/docker.sock" not in mount["Source"]
            for mount in attrs["Mounts"]
        )
        persistent_writable = [
            mount
            for mount in attrs["Mounts"]
            if mount["RW"] and mount["Destination"] not in {"/tmp", "/home/mewcode"}
        ]
        assert [mount["Destination"] for mount in persistent_writable] == ["/workspace"]
        assert set(attrs["NetworkSettings"]["Networks"]) == {
            environment._attempt_network.name
        }

        environment._proxy_container.reload()
        proxy_attrs = environment._proxy_container.attrs
        assert proxy_attrs["Config"]["User"] == "13:13"
        assert proxy_attrs["HostConfig"]["ReadonlyRootfs"] is True
        assert proxy_attrs["HostConfig"]["CapDrop"] == ["ALL"]
        assert not proxy_attrs["HostConfig"].get("PortBindings")
        assert set(proxy_attrs["NetworkSettings"]["Networks"]) == {
            environment._attempt_network.name,
            environment._egress_network.name,
        }

        environment._volume_holder_container.reload()
        holder_attrs = environment._volume_holder_container.attrs
        assert holder_attrs["HostConfig"]["NetworkMode"] == "none"
        assert holder_attrs["HostConfig"]["ReadonlyRootfs"] is True
        assert holder_attrs["HostConfig"]["CapDrop"] == ["ALL"]
        assert holder_attrs["HostConfig"]["Init"] is True
        assert holder_attrs["HostConfig"]["PidsLimit"] == 32
        volume_options = environment._workspace_volume.attrs["Options"]["o"]
        assert f"size={environment.spec.limits.workspace_bytes}" in volume_options
        assert f"nr_inodes={environment.spec.limits.workspace_inodes}" in volume_options
        await running
    finally:
        await _close_and_assert_clean(environment)


@pytest.mark.executor_security
@pytest.mark.asyncio
async def test_attempt_workspace_quarantine_paths_and_read_only_metadata(
    tmp_path: Path,
) -> None:
    environment = _new_environment(tmp_path)
    host_canary = tmp_path / "host-canary.txt"
    host_canary.write_text("host-file-canary-72db194f", encoding="utf-8")
    archive = _archive_directory(
        FIXTURES / "prompt_injection",
        {
            ".env": b"TOP_SECRET=forbidden\n",
            ".env.example": b"SAFE_PLACEHOLDER=1\n",
            ".git/config": b"[credential]\nhelper=evil\n",
            ".github/workflows/check.yml": b"name: untrusted\n",
            "normal.txt": b"normal\n",
            "workspace_escape.py": (
                FIXTURES / "workspace_escape" / "probe.py"
            ).read_bytes(),
        },
    )
    try:
        await environment.start()
        await environment.import_archive(archive)
        result = await environment.run_command(
            ExecutionCommand(
                "for p in .git .mewcode .env .github/workflows/check.yml .env.example; "
                "do test -e \"$p\" && e=yes || e=no; test -r \"$p\" && r=yes || r=no; "
                "printf '%s exists=%s readable=%s\\n' \"$p\" \"$e\" \"$r\"; done"
            )
        )
        assert result.command_result.exit_code == 0
        assert ".git exists=no" in result.command_result.stdout
        assert ".mewcode exists=no" in result.command_result.stdout
        assert ".env exists=no" in result.command_result.stdout
        assert ".github/workflows/check.yml exists=yes readable=yes" in result.command_result.stdout
        assert ".env.example exists=yes readable=yes" in result.command_result.stdout
        metadata = await environment.workspace.read_file(
            ".github/workflows/check.yml"
        )
        assert "untrusted" in metadata.content
        with pytest.raises(WorkspacePathError):
            await environment.workspace.write_file(
                ".github/workflows/check.yml", "changed", metadata.version
            )
        for path in ("../outside", "~/.ssh/id_rsa", "/etc/passwd"):
            with pytest.raises(WorkspacePathError):
                await environment.workspace.read_file(path)

        escape_probe = await environment.run_command(
            ExecutionCommand(
                "python workspace_escape.py",
                internal_env={
                    "MEWCODE_SECURITY_FIXTURE": "executor",
                    "MEWCODE_TEST_HOST_CANARY_PATH": str(host_canary),
                },
            )
        )
        assert escape_probe.command_result.exit_code == 0
        assert "host-file-canary-72db194f" not in escape_probe.command_result.stdout
        assert "READ " not in escape_probe.command_result.stdout
        await environment.run_command(ExecutionCommand("rm -f escape-link"))

        linked = await environment.run_command(
            ExecutionCommand("ln -s /etc/passwd escape-link")
        )
        assert linked.command_result.exit_code == 0
        with pytest.raises(WorkspacePathError):
            await environment.workspace.read_file("escape-link")

        exported = await environment.export_archive()
        with tarfile.open(fileobj=io.BytesIO(exported), mode="r:*") as archive_file:
            names = set(archive_file.getnames())
        assert "normal.txt" in names
        assert ".env.example" in names
        assert ".github/workflows/check.yml" in names
        assert ".env" not in names
        assert not any(name == ".git" or name.startswith(".git/") for name in names)
        assert not any(
            name == ".mewcode" or name.startswith(".mewcode/") for name in names
        )
        assert not any(name == "workspace" or name.startswith("workspace/") for name in names)
    finally:
        await _close_and_assert_clean(environment)


@pytest.mark.executor_security
@pytest.mark.asyncio
async def test_timeout_removes_process_tree_but_workspace_remains_usable(
    tmp_path: Path,
) -> None:
    environment = _new_environment(tmp_path)
    try:
        await environment.start()
        await environment.import_archive(
            _archive_directory(FIXTURES / "timeout_process_tree")
        )
        timed_out = await environment.run_command(
            ExecutionCommand(
                "python process_tree.py",
                timeout_seconds=2,
                internal_env={"MEWCODE_SECURITY_FIXTURE": "executor"},
            )
        )
        assert timed_out.command_result.timed_out is True
        assert environment._active_container is None
        heartbeats = await environment.workspace.glob(".", "*.heartbeat")
        assert len(heartbeats) == 3
        before = {
            path: (await environment.workspace.read_file(path)).content
            for path in heartbeats
        }
        await asyncio.sleep(0.5)
        after = {
            path: (await environment.workspace.read_file(path)).content
            for path in heartbeats
        }
        assert after == before
        next_command = await environment.run_command(ExecutionCommand("printf reusable"))
        assert next_command.command_result.stdout == "reusable"

        await environment.run_command(ExecutionCommand("rm -f -- *.heartbeat"))
        cancelled = asyncio.create_task(
            environment.run_command(
                ExecutionCommand(
                    "python process_tree.py",
                    timeout_seconds=10,
                    internal_env={"MEWCODE_SECURITY_FIXTURE": "executor"},
                )
            )
        )
        await asyncio.sleep(0.5)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert environment._active_container is None
        cancelled_heartbeats = await environment.workspace.glob(".", "*.heartbeat")
        before_cancel = {
            path: (await environment.workspace.read_file(path)).content
            for path in cancelled_heartbeats
        }
        await asyncio.sleep(0.5)
        after_cancel = {
            path: (await environment.workspace.read_file(path)).content
            for path in cancelled_heartbeats
        }
        assert after_cancel == before_cancel
    finally:
        await _close_and_assert_clean(environment)


@pytest.mark.executor_security
@pytest.mark.asyncio
async def test_proxy_only_egress_denies_sensitive_targets(tmp_path: Path) -> None:
    network, mock_package, mock_address = _start_mock_package_endpoint()
    environment = _new_environment(
        tmp_path,
        egress_allowlist=("packages.test",),
        egress_network_name=network.name,
    )
    try:
        await environment.start()
        await environment.import_archive(_archive_directory(FIXTURES / "egress_probe"))
        denied = await environment.run_command(
            ExecutionCommand(
                "python probe.py",
                timeout_seconds=15,
                internal_env={"MEWCODE_SECURITY_FIXTURE": "executor"},
            )
        )
        assert denied.command_result.exit_code == 0
        assert denied.command_result.stdout.count("DENIED ") == 5
        direct = await environment.run_command(
                ExecutionCommand(
                    "python - <<'PY'\n"
                    "import socket\n"
                    f"socket.create_connection(('{mock_address}', 443), timeout=1)\n"
                    "PY",
                timeout_seconds=5,
            )
        )
        assert direct.command_result.exit_code != 0
        dns_probe = await environment.run_command(
            ExecutionCommand(
                "python - <<'PY'\n"
                "import socket\n"
                "socket.getaddrinfo('pypi.org', 443)\n"
                "PY",
                timeout_seconds=5,
            )
        )
        assert dns_probe.command_result.exit_code != 0
        allowed = await environment.run_command(
                ExecutionCommand(
                    "python - <<'PY'\n"
                    "import ssl, urllib.request\n"
                    "context = ssl._create_unverified_context()\n"
                    "with urllib.request.urlopen('https://packages.test/simple/', "
                    "timeout=10, context=context) as r:\n"
                    "    print(r.status)\n"
                "PY",
                timeout_seconds=15,
            )
        )
        assert allowed.command_result.exit_code == 0
        assert allowed.command_result.stdout.strip() == "200"
    finally:
        await _close_and_assert_clean(environment)
        mock_package.remove(force=True)
        network.remove()


@pytest.mark.resource_exhaustion
@pytest.mark.asyncio
async def test_disk_limit_is_fatal_and_attempt_local(tmp_path: Path) -> None:
    limits = ExecutionLimits(
        cpus=1,
        memory_bytes=256 * MIB,
        pids_limit=64,
        workspace_bytes=16 * MIB,
        tmp_bytes=8 * MIB,
        workspace_inodes=2_000,
        command_timeout_seconds=30,
        attempt_timeout_seconds=90,
        max_output_bytes=256 * 1024,
    )
    environment = _new_environment(tmp_path, limits=limits)
    try:
        await environment.start()
        await environment.import_archive(
            _archive_directory(FIXTURES / "disk_exhaustion")
        )
        result = await environment.run_command(
            ExecutionCommand(
                "python fill_disk.py",
                timeout_seconds=25,
                internal_env={"MEWCODE_SECURITY_FIXTURE": "executor"},
            )
        )
        assert result.fatal_error_code == "EXECUTION_RESOURCE_LIMIT"
    finally:
        await _close_and_assert_clean(environment)


@pytest.mark.resource_exhaustion
@pytest.mark.asyncio
async def test_pid_limit_is_fatal_and_attempt_local(tmp_path: Path) -> None:
    limits = ExecutionLimits(
        cpus=1,
        memory_bytes=256 * MIB,
        pids_limit=24,
        workspace_bytes=32 * MIB,
        tmp_bytes=8 * MIB,
        workspace_inodes=2_000,
        command_timeout_seconds=30,
        attempt_timeout_seconds=90,
        max_output_bytes=256 * 1024,
    )
    environment = _new_environment(tmp_path, limits=limits)
    control = _new_environment(tmp_path)
    try:
        await environment.start()
        await control.start()
        await environment.import_archive(_archive_directory(FIXTURES / "pid_exhaustion"))
        result = await environment.run_command(
            ExecutionCommand(
                "python spawn_many.py",
                timeout_seconds=25,
                internal_env={"MEWCODE_SECURITY_FIXTURE": "executor"},
            )
        )
        assert result.fatal_error_code == "EXECUTION_RESOURCE_LIMIT"
        unaffected = await control.run_command(ExecutionCommand("printf unaffected"))
        assert unaffected.command_result.stdout == "unaffected"
    finally:
        await _close_and_assert_clean(environment)
        await _close_and_assert_clean(control)


@pytest.mark.executor_security
@pytest.mark.asyncio
async def test_registered_secrets_are_redacted_before_tool_results(tmp_path: Path) -> None:
    secret = "registered-secret-f3141592"
    environment = _new_environment(tmp_path, secret_values=(secret,))
    registry = create_platform_registry(environment)
    try:
        await environment.start()
        await environment.import_archive(
            _archive_directory(FIXTURES / "secret_canary", {"secret.txt": secret.encode()})
        )
        read = registry.get("ReadFile")
        assert read is not None
        result = await read.execute(ReadParams(file_path="secret.txt"))
        assert secret not in result.output
        assert secret not in (result.recovery_content or "")
        assert "[REDACTED]" in result.output
    finally:
        await _close_and_assert_clean(environment)


@pytest.mark.executor_security
@pytest.mark.asyncio
async def test_output_limit_is_fatal_and_closes_attempt(tmp_path: Path) -> None:
    limits = ExecutionLimits(
        cpus=1,
        memory_bytes=256 * MIB,
        pids_limit=64,
        workspace_bytes=32 * MIB,
        tmp_bytes=8 * MIB,
        workspace_inodes=2_000,
        command_timeout_seconds=10,
        attempt_timeout_seconds=30,
        max_output_bytes=16 * 1024,
    )
    environment = _new_environment(tmp_path, limits=limits)
    try:
        await environment.start()
        result = await environment.run_command(
            ExecutionCommand(
                "python -c \"import sys; sys.stdout.write('x' * 65536)\"",
                timeout_seconds=5,
            )
        )
        assert result.fatal_error_code == "EXECUTION_OUTPUT_LIMIT"
        assert len(result.command_result.stdout.encode()) <= limits.max_output_bytes
    finally:
        await _close_and_assert_clean(environment)
