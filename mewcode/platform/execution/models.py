from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Mapping

from mewcode.tools.base import CommandExecutionResult


GIB = 1024**3
MIB = 1024**2
_IMMUTABLE_IMAGE = re.compile(
    r"^(?:sha256:[0-9a-f]{64}|[^\s@]+@sha256:[0-9a-f]{64})$"
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DOMAIN = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)


class ExecutionState(str, Enum):
    CREATED = "CREATED"
    STARTING = "STARTING"
    READY = "READY"
    BROKEN = "BROKEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"


@dataclass(frozen=True)
class ExecutionLimits:
    cpus: float = 4.0
    memory_bytes: int = 6 * GIB
    pids_limit: int = 256
    workspace_bytes: int = 3 * GIB
    tmp_bytes: int = 256 * MIB
    workspace_inodes: int = 300_000
    command_timeout_seconds: int = 600
    attempt_timeout_seconds: int = 3600
    max_output_bytes: int = MIB
    nofile_limit: int = 4096

    def __post_init__(self) -> None:
        positive = {
            "cpus": self.cpus,
            "memory_bytes": self.memory_bytes,
            "pids_limit": self.pids_limit,
            "workspace_bytes": self.workspace_bytes,
            "tmp_bytes": self.tmp_bytes,
            "workspace_inodes": self.workspace_inodes,
            "command_timeout_seconds": self.command_timeout_seconds,
            "attempt_timeout_seconds": self.attempt_timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
            "nofile_limit": self.nofile_limit,
        }
        invalid = [name for name, value in positive.items() if value <= 0]
        if invalid:
            raise ValueError("Execution limits must be positive: " + ", ".join(invalid))
        if self.command_timeout_seconds > self.attempt_timeout_seconds:
            raise ValueError("Command timeout cannot exceed Attempt timeout")


@dataclass(frozen=True)
class AttemptExecutionSpec:
    job_id: str
    attempt_id: str
    executor_image: str
    proxy_image: str
    trusted_state_dir: Path
    limits: ExecutionLimits = field(default_factory=ExecutionLimits)
    egress_allowlist: tuple[str, ...] = (
        "pypi.org",
        "files.pythonhosted.org",
    )
    secret_values: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name, value in (("job_id", self.job_id), ("attempt_id", self.attempt_id)):
            if not _IDENTIFIER.fullmatch(value):
                raise ValueError(f"Invalid {name}: {value!r}")
        for name, image in (
            ("executor_image", self.executor_image),
            ("proxy_image", self.proxy_image),
        ):
            if not _IMMUTABLE_IMAGE.fullmatch(image):
                raise ValueError(f"{name} must use an immutable sha256 digest")
        normalized_domains: list[str] = []
        for domain in self.egress_allowlist:
            normalized = domain.lower().rstrip(".")
            if (
                not normalized
                or "*" in normalized
                or ":" in normalized
                or not _DOMAIN.fullmatch(normalized)
            ):
                raise ValueError(f"Invalid egress allowlist domain: {domain!r}")
            normalized_domains.append(normalized)
        object.__setattr__(self, "egress_allowlist", tuple(dict.fromkeys(normalized_domains)))
        state_root = Path(self.trusted_state_dir).resolve()
        state_key = hashlib.sha256(
            f"{self.job_id}\0{self.attempt_id}".encode("utf-8")
        ).hexdigest()[:32]
        if state_root.name == state_key and state_root.parent.name == "attempts":
            isolated_state_dir = state_root
        else:
            isolated_state_dir = state_root / "attempts" / state_key
        object.__setattr__(
            self,
            "trusted_state_dir",
            isolated_state_dir,
        )
        object.__setattr__(
            self,
            "secret_values",
            tuple(value for value in self.secret_values if len(value) >= 8),
        )


@dataclass(frozen=True)
class RuntimeEnvironmentInfo:
    work_dir: str = "/workspace"
    operating_system: str = "Linux"
    shell: str = "/bin/sh"


@dataclass(frozen=True)
class ExecutionCommand:
    command: str
    timeout_seconds: int = 120
    internal_env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionCommandOutcome:
    command_result: CommandExecutionResult
    fatal_error_code: str | None = None
    fatal_error_message: str | None = None


@dataclass(frozen=True)
class WorkspaceReadResult:
    content: str
    version: str


@dataclass(frozen=True)
class WorkspaceWriteResult:
    version: str
