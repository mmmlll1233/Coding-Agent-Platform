from __future__ import annotations

import fnmatch
import errno
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path, PurePosixPath


WORKSPACE = Path("/workspace")
SKIP_DIRS = {".git", ".mewcode", ".venv", "node_modules", "__pycache__", ".tox", ".mypy_cache"}


class HelperError(RuntimeError):
    code = "workspace_error"


class PathError(HelperError):
    code = "workspace_path"


class ConflictError(HelperError):
    code = "workspace_conflict"


class ResourceLimitError(HelperError):
    code = "resource_limit"


def _normalized(path: str, *, allow_root: bool = False) -> str:
    if not path or "\x00" in path or path.startswith("~"):
        raise PathError(f"invalid workspace path: {path!r}")
    pure = PurePosixPath(path.replace("\\", "/"))
    if pure.is_absolute():
        try:
            pure = pure.relative_to("/workspace")
        except ValueError as exc:
            raise PathError(f"path is outside /workspace: {path}") from exc
    if any(part in ("", ".", "..") for part in pure.parts):
        raise PathError(f"path traversal is forbidden: {path}")
    normalized = pure.as_posix()
    if not allow_root and normalized in ("", "."):
        raise PathError("workspace root is not a file")
    if normalized == ".git" or normalized.startswith(".git/"):
        raise PathError("Git metadata is outside Agent execution")
    if normalized == ".mewcode" or normalized.startswith(".mewcode/"):
        raise PathError("repository .mewcode extensions are quarantined")
    return normalized


def _existing_path(path: str) -> Path:
    normalized = _normalized(path)
    candidate = WORKSPACE / normalized
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(WORKSPACE.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise PathError(f"path escapes workspace: {path}") from exc
    return resolved


def _write_path(path: str) -> Path:
    normalized = _normalized(path)
    if normalized == ".github" or normalized.startswith(".github/"):
        raise PathError(".github is read-only in platform execution")
    candidate = WORKSPACE / normalized
    ancestor = candidate.parent
    missing: list[str] = []
    while not ancestor.exists():
        missing.append(ancestor.name)
        ancestor = ancestor.parent
    try:
        resolved_ancestor = ancestor.resolve(strict=True)
        resolved_ancestor.relative_to(WORKSPACE.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise PathError(f"path escapes workspace: {path}") from exc
    current = resolved_ancestor
    for part in reversed(missing):
        current = current / part
        current.mkdir(mode=0o750)
    resolved_parent = candidate.parent.resolve(strict=True)
    try:
        resolved_parent.relative_to(WORKSPACE.resolve(strict=True))
    except ValueError as exc:
        raise PathError(f"path escapes workspace: {path}") from exc
    return resolved_parent / candidate.name


def _version(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _read(request: dict) -> dict:
    path = _existing_path(str(request["path"]))
    if not path.is_file():
        raise FileNotFoundError(str(request["path"]))
    content = path.read_text(encoding="utf-8")
    return {"content": content, "version": _version(content)}


def _atomic_write(path: Path, content: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".mewcode-write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _write(request: dict) -> dict:
    path = _write_path(str(request["path"]))
    expected = request.get("expected_version")
    if path.exists():
        current = path.read_text(encoding="utf-8")
        if expected is None:
            raise ConflictError("file has not been read yet; read it before editing")
        if _version(current) != expected:
            raise ConflictError("file changed since it was read; read it again")
    content = str(request["content"])
    _atomic_write(path, content)
    return {"version": _version(content)}


def _edit(request: dict) -> dict:
    path = _existing_path(str(request["path"]))
    normalized = _normalized(str(request["path"]))
    if normalized == ".github" or normalized.startswith(".github/"):
        raise PathError(".github is read-only in platform execution")
    content = path.read_text(encoding="utf-8")
    if _version(content) != str(request["expected_version"]):
        raise ConflictError("file changed since it was read; read it again")
    old = str(request["old_string"])
    count = content.count(old)
    if count == 0:
        raise ValueError("old_string not found in file")
    if count > 1:
        raise ValueError(f"old_string found {count} times, must be unique")
    updated = content.replace(old, str(request["new_string"]), 1)
    _atomic_write(path, updated)
    return {"version": _version(updated)}


def _base_path(path: str) -> Path:
    if path in ("", "."):
        return WORKSPACE.resolve(strict=True)
    base = _existing_path(path)
    if not base.is_dir():
        raise NotADirectoryError(path)
    return base


def _glob(request: dict) -> dict:
    base = _base_path(str(request.get("path", ".")))
    pattern = str(request["pattern"])
    matches: list[str] = []
    workspace = WORKSPACE.resolve(strict=True)
    for candidate in base.glob(pattern):
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(base)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        try:
            candidate.resolve(strict=True).relative_to(workspace)
        except (OSError, ValueError):
            continue
        matches.append(relative.as_posix())
    matches.sort()
    return {"matches": matches}


def _grep(request: dict) -> dict:
    base = _base_path(str(request.get("path", ".")))
    try:
        regex = re.compile(str(request["pattern"]))
    except re.error as exc:
        raise ValueError(f"invalid regex: {exc}") from exc
    include = str(request.get("include", ""))
    matches: list[str] = []
    workspace = WORKSPACE.resolve(strict=True)
    for candidate in sorted(base.rglob("*")):
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(base)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if include and not fnmatch.fnmatch(candidate.name, include):
            continue
        try:
            candidate.resolve(strict=True).relative_to(workspace)
        except (OSError, ValueError):
            continue
        try:
            content = candidate.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for number, line in enumerate(content.splitlines(), 1):
            if regex.search(line):
                matches.append(f"{relative.as_posix()}:{number}:{line}")
    return {"matches": matches}


OPERATIONS = {
    "read": _read,
    "write": _write,
    "edit": _edit,
    "glob": _glob,
    "grep": _grep,
}


def main() -> int:
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--stdin-size":
            size = int(sys.argv[2])
            if size < 0 or size > 16 * 1024 * 1024:
                raise ValueError("workspace helper request is too large")
            request = json.loads(sys.stdin.buffer.read(size).decode("utf-8"))
        else:
            request_path = Path(sys.argv[1])
            request = json.loads(request_path.read_text(encoding="utf-8"))
        operation = OPERATIONS[str(request["op"])]
        response = {"ok": True, **operation(request)}
    except FileNotFoundError as exc:
        response = {"ok": False, "code": "not_found", "error": str(exc)}
    except HelperError as exc:
        response = {"ok": False, "code": exc.code, "error": str(exc)}
    except OSError as exc:
        if exc.errno in {
            errno.EDQUOT,
            errno.EFBIG,
            errno.EMFILE,
            errno.ENFILE,
            errno.ENOSPC,
        }:
            response = {"ok": False, "code": "resource_limit", "error": str(exc)}
        else:
            response = {"ok": False, "code": "workspace_error", "error": str(exc)}
    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
        response = {"ok": False, "code": "workspace_error", "error": str(exc)}
    sys.stdout.write(json.dumps(response, ensure_ascii=False))
    return 0 if response["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
