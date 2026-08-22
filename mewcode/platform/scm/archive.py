from __future__ import annotations

import copy
import hashlib
import io
import json
import posixpath
import re
import tarfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from .errors import NoChangesError, ScmPolicyError


GIB = 1024**3
LFS_POINTER = b"version https://git-lfs.github.com/spec/v1"


@dataclass(frozen=True)
class ManifestEntry:
    kind: str
    mode: str
    sha256: str
    size: int
    link_target: str = ""


@dataclass(frozen=True)
class Change:
    path: str
    entry: ManifestEntry | None


class _DigestReader:
    def __init__(self, source: BinaryIO) -> None:
        self.source = source
        self.digest = hashlib.sha256()
        self.prefix = bytearray()

    def read(self, size: int = -1) -> bytes:
        data = self.source.read(size)
        if data:
            self.digest.update(data)
            if len(self.prefix) < 256:
                self.prefix.extend(data[: 256 - len(self.prefix)])
        return data


def safe_archive_path(name: str) -> str:
    raw = name.replace("\\", "/")
    if raw.startswith("/") or "\x00" in raw:
        raise ScmPolicyError(f"Unsafe repository archive path: {name!r}")
    while raw.startswith("./"):
        raw = raw[2:]
    normalized = posixpath.normpath(raw)
    pure = PurePosixPath(normalized)
    if (
        not normalized
        or normalized == "."
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise ScmPolicyError(f"Unsafe repository archive path: {name!r}")
    return pure.as_posix()


def _safe_symlink(path: str, target: str) -> None:
    normalized_target = target.replace("\\", "/")
    pure_target = PurePosixPath(normalized_target)
    if pure_target.is_absolute() or not normalized_target or "\x00" in normalized_target:
        raise ScmPolicyError(f"Unsafe repository symlink: {path}")
    depth = len(PurePosixPath(path).parent.parts)
    for part in pure_target.parts:
        if part == "..":
            depth -= 1
        elif part != ".":
            depth += 1
        if depth < 0:
            raise ScmPolicyError(f"Escaping repository symlink: {path}")


def _is_quarantined(path: str) -> bool:
    return (
        path == ".env"
        or path == ".git"
        or path.startswith(".git/")
        or path == ".mewcode"
        or path.startswith(".mewcode/")
    )


def _mode(member: tarfile.TarInfo) -> str:
    if member.issym():
        return "120000"
    return "100755" if member.mode & 0o111 else "100644"


def _reject_lfs(path: str, prefix: bytes, full_content: bytes | None = None) -> None:
    if prefix.startswith(LFS_POINTER):
        raise ScmPolicyError(f"Git LFS pointer is unsupported: {path}")
    if PurePosixPath(path).name == ".gitattributes":
        content = full_content if full_content is not None else prefix
        text = content.decode("utf-8", errors="replace")
        if any(
            re.search(r"(?:^|\s)filter\s*=\s*lfs(?:\s|$)", line, re.IGNORECASE)
            for line in text.splitlines()
            if not line.lstrip().startswith("#")
        ):
            raise ScmPolicyError(f"Git LFS attributes are unsupported: {path}")


def _record_archive_path(
    path: str,
    kind: str,
    seen: dict[str, str],
    required_directories: set[str],
    *,
    scope: str,
) -> None:
    if path in seen:
        raise ScmPolicyError(f"Duplicate {scope} path: {path}")
    if kind != "directory" and path in required_directories:
        raise ScmPolicyError(f"{scope.title()} path type conflict: {path}")
    for parent in PurePosixPath(path).parents:
        parent_name = parent.as_posix()
        if parent_name == ".":
            break
        if seen.get(parent_name) not in {None, "directory"}:
            raise ScmPolicyError(f"{scope.title()} path type conflict: {path}")
        required_directories.add(parent_name)
    seen[path] = kind


def _clone(member: tarfile.TarInfo, name: str) -> tarfile.TarInfo:
    cloned = copy.copy(member)
    cloned.name = name
    cloned.uid = 65532
    cloned.gid = 65532
    cloned.uname = ""
    cloned.gname = ""
    cloned.mtime = 0
    return cloned


def normalize_source_archive(
    source_path: Path,
    destination_path: Path,
    manifest_path: Path,
    *,
    base_sha: str,
    base_tree_sha: str,
    max_unpacked_bytes: int = 3 * GIB,
    max_members: int = 300_000,
) -> dict[str, ManifestEntry]:
    entries: dict[str, ManifestEntry] = {}
    seen: dict[str, str] = {}
    required_directories: set[str] = set()
    total = 0
    count = 0
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(source_path, mode="r:*") as source, tarfile.open(
        destination_path, mode="w"
    ) as output:
        members = source.getmembers()
        if not members:
            raise ScmPolicyError("Repository archive is empty")
        if not members[0].isdir():
            raise ScmPolicyError("Repository archive root is not a directory")
        first = safe_archive_path(members[0].name)
        prefix = first.split("/", 1)[0]
        for member in members:
            raw = safe_archive_path(member.name)
            if raw == prefix:
                if not member.isdir():
                    raise ScmPolicyError("Repository archive root is invalid")
                continue
            if not raw.startswith(prefix + "/"):
                raise ScmPolicyError("Repository archive has multiple roots")
            path = safe_archive_path(raw[len(prefix) + 1 :])
            if _is_quarantined(path):
                continue
            count += 1
            if count > max_members:
                raise ScmPolicyError("Repository archive has too many entries")
            if member.isdev() or member.isfifo() or member.islnk():
                raise ScmPolicyError(f"Unsupported repository entry: {path}")
            if path == ".gitmodules":
                raise ScmPolicyError("Git submodules are unsupported")
            kind = (
                "directory"
                if member.isdir()
                else "symlink"
                if member.issym()
                else "file"
                if member.isfile()
                else "unsupported"
            )
            if kind == "unsupported":
                raise ScmPolicyError(f"Unsupported repository entry: {path}")
            _record_archive_path(
                path,
                kind,
                seen,
                required_directories,
                scope="repository",
            )
            cloned = _clone(member, path)
            if member.isdir():
                output.addfile(cloned)
                continue
            if member.issym():
                _safe_symlink(path, member.linkname)
                target = member.linkname.encode("utf-8")
                output.addfile(cloned)
                entries[path] = ManifestEntry(
                    kind="symlink",
                    mode="120000",
                    sha256=hashlib.sha256(target).hexdigest(),
                    size=len(target),
                    link_target=member.linkname,
                )
                continue
            if not member.isfile():
                raise ScmPolicyError(f"Unsupported repository entry: {path}")
            total += max(0, member.size)
            if total > max_unpacked_bytes:
                raise ScmPolicyError("Repository archive exceeds workspace capacity")
            extracted = source.extractfile(member)
            if extracted is None:
                raise ScmPolicyError(f"Repository file cannot be read: {path}")
            if PurePosixPath(path).name == ".gitattributes":
                if member.size > 1024 * 1024:
                    raise ScmPolicyError(".gitattributes is unexpectedly large")
                data = extracted.read()
                _reject_lfs(path, data[:256], data)
                output.addfile(cloned, io.BytesIO(data))
                digest = hashlib.sha256(data).hexdigest()
            else:
                reader = _DigestReader(extracted)
                output.addfile(cloned, reader)
                _reject_lfs(path, bytes(reader.prefix))
                digest = reader.digest.hexdigest()
            entries[path] = ManifestEntry(
                kind="file",
                mode=_mode(member),
                sha256=digest,
                size=member.size,
            )
    manifest = {
        "base_sha": base_sha,
        "base_tree_sha": base_tree_sha,
        "files": {path: asdict(entry) for path, entry in sorted(entries.items())},
    }
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return entries


def load_manifest(path: Path) -> tuple[str, str, dict[str, ManifestEntry]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        files = {
            name: ManifestEntry(**entry) for name, entry in value["files"].items()
        }
        return str(value["base_sha"]), str(value["base_tree_sha"]), files
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ScmPolicyError("Trusted repository manifest is invalid") from error


def scan_workspace_archive(
    archive_path: Path,
    *,
    max_unpacked_bytes: int = 3 * GIB,
    max_members: int = 300_000,
) -> dict[str, ManifestEntry]:
    entries: dict[str, ManifestEntry] = {}
    seen: dict[str, str] = {}
    required_directories: set[str] = set()
    total = 0
    count = 0
    try:
        source = tarfile.open(archive_path, mode="r:*")
    except (OSError, tarfile.TarError) as error:
        raise ScmPolicyError("Workspace archive is invalid") from error
    with source:
        for member in source.getmembers():
            path = safe_archive_path(member.name)
            if _is_quarantined(path):
                raise ScmPolicyError(f"Quarantined path cannot be delivered: {path}")
            count += 1
            if count > max_members:
                raise ScmPolicyError("Workspace archive has too many entries")
            if member.isdev() or member.isfifo() or member.islnk():
                raise ScmPolicyError(f"Unsupported workspace entry: {path}")
            kind = (
                "directory"
                if member.isdir()
                else "symlink"
                if member.issym()
                else "file"
                if member.isfile()
                else "unsupported"
            )
            if kind == "unsupported":
                raise ScmPolicyError(f"Unsupported workspace entry: {path}")
            _record_archive_path(
                path,
                kind,
                seen,
                required_directories,
                scope="workspace",
            )
            if member.isdir():
                continue
            if member.issym():
                _safe_symlink(path, member.linkname)
                data = member.linkname.encode("utf-8")
                entries[path] = ManifestEntry(
                    "symlink",
                    "120000",
                    hashlib.sha256(data).hexdigest(),
                    len(data),
                    member.linkname,
                )
                continue
            if not member.isfile():
                raise ScmPolicyError(f"Unsupported workspace entry: {path}")
            total += max(0, member.size)
            if total > max_unpacked_bytes:
                raise ScmPolicyError("Workspace archive exceeds capacity")
            extracted = source.extractfile(member)
            if extracted is None:
                raise ScmPolicyError(f"Workspace file cannot be read: {path}")
            reader = _DigestReader(extracted)
            while reader.read(1024 * 1024):
                pass
            full = None
            if PurePosixPath(path).name == ".gitattributes":
                if member.size > 1024 * 1024:
                    raise ScmPolicyError(".gitattributes is unexpectedly large")
                extracted = source.extractfile(member)
                full = extracted.read() if extracted is not None else b""
            _reject_lfs(path, bytes(reader.prefix), full)
            entries[path] = ManifestEntry(
                "file", _mode(member), reader.digest.hexdigest(), member.size
            )
    return entries


def diff_manifests(
    baseline: dict[str, ManifestEntry],
    current: dict[str, ManifestEntry],
    *,
    max_files: int,
    max_bytes: int,
    max_file_bytes: int,
) -> list[Change]:
    paths = sorted(set(baseline).union(current))
    changes = [
        Change(path, current.get(path))
        for path in paths
        if baseline.get(path) != current.get(path)
    ]
    if not changes:
        raise NoChangesError("Workspace contains no deliverable changes")
    if len(changes) > max_files:
        raise ScmPolicyError("Delivery changes too many files")
    changed_bytes = 0
    for change in changes:
        if change.path == ".github" or change.path.startswith(".github/"):
            raise ScmPolicyError(".github changes are forbidden")
        if change.path == ".gitmodules":
            raise ScmPolicyError("Git submodules are unsupported")
        if change.entry is not None:
            if change.entry.size > max_file_bytes:
                raise ScmPolicyError(f"Delivery file is too large: {change.path}")
            changed_bytes += change.entry.size
    if changed_bytes > max_bytes:
        raise ScmPolicyError("Delivery content exceeds configured capacity")
    return changes


def read_change_contents(
    archive_path: Path,
    changes: list[Change],
) -> dict[str, bytes]:
    wanted = {change.path for change in changes if change.entry is not None}
    result: dict[str, bytes] = {}
    with tarfile.open(archive_path, mode="r:*") as source:
        for member in source.getmembers():
            path = safe_archive_path(member.name)
            if path not in wanted:
                continue
            if member.issym():
                result[path] = member.linkname.encode("utf-8")
            else:
                extracted = source.extractfile(member)
                if extracted is None:
                    raise ScmPolicyError(f"Workspace file cannot be read: {path}")
                result[path] = extracted.read()
    if set(result) != wanted:
        raise ScmPolicyError("Workspace archive changed during publication")
    return result
