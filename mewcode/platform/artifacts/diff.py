from __future__ import annotations

import difflib
import tarfile
from pathlib import Path

from mewcode.platform.scm.archive import (
    Change,
    ManifestEntry,
    diff_manifests,
    load_manifest,
    safe_archive_path,
    scan_workspace_archive,
)


def _contents(archive_path: Path, wanted: set[str]) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    with tarfile.open(archive_path, mode="r:*") as archive:
        for member in archive.getmembers():
            path = safe_archive_path(member.name)
            if path not in wanted or member.isdir():
                continue
            if member.issym():
                result[path] = member.linkname.encode("utf-8")
                continue
            extracted = archive.extractfile(member)
            if extracted is not None:
                result[path] = extracted.read()
    return result


def _text(content: bytes) -> list[str] | None:
    if b"\0" in content:
        return None
    try:
        return content.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return None


def _identity(entry: ManifestEntry | None) -> str:
    if entry is None:
        return "absent"
    return f"mode={entry.mode} sha256={entry.sha256} size={entry.size}"


def render_workspace_diff(
    *,
    prepared_archive: Path,
    prepared_manifest: Path,
    workspace_archive: Path,
    max_files: int,
    max_bytes: int,
    max_file_bytes: int,
) -> tuple[bytes, list[Change]]:
    """Render trusted, deterministic evidence for every deliverable path change."""
    _, _, baseline = load_manifest(prepared_manifest)
    current = scan_workspace_archive(workspace_archive)
    changes = diff_manifests(
        baseline,
        current,
        max_files=max_files,
        max_bytes=max_bytes,
        max_file_bytes=max_file_bytes,
    )
    paths = {change.path for change in changes}
    before = _contents(prepared_archive, paths)
    after = _contents(workspace_archive, paths)
    output: list[str] = ["# mewcode-diff-v1\n"]
    for change in changes:
        path = change.path
        old_entry = baseline.get(path)
        new_entry = current.get(path)
        output.extend(
            [
                f"\n# path: {path}\n",
                f"# before: {_identity(old_entry)}\n",
                f"# after: {_identity(new_entry)}\n",
            ]
        )
        old = _text(before.get(path, b"")) if old_entry is not None else []
        new = _text(after.get(path, b"")) if new_entry is not None else []
        if old is None or new is None:
            output.append("# binary content; unified diff omitted\n")
            continue
        output.extend(
            difflib.unified_diff(
                old,
                new,
                fromfile=f"a/{path}" if old_entry is not None else "/dev/null",
                tofile=f"b/{path}" if new_entry is not None else "/dev/null",
            )
        )
    return "".join(output).encode("utf-8"), changes
