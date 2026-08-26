from __future__ import annotations

import argparse
import io
import json
import shutil
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Sequence

from mewcode.platform.scm.archive import (
    GIB,
    load_manifest,
    normalize_source_archive,
)
from mewcode.platform.scm.errors import ScmPolicyError


REQUIRED_FREE_BYTES = 12 * GIB
BASE_SHA = "a" * 40
TREE_SHA = "b" * 40


class _ZeroReader(io.RawIOBase):
    def __init__(self, remaining: int) -> None:
        self.remaining = remaining

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        count = self.remaining if size < 0 else min(size, self.remaining)
        self.remaining -= count
        return b"\0" * count


def _source_archive(path: Path, content_bytes: int) -> None:
    with tarfile.open(path, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        root = tarfile.TarInfo("phase7-root")
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        archive.addfile(root)
        member = tarfile.TarInfo("phase7-root/capacity.bin")
        member.size = content_bytes
        member.mode = 0o644
        archive.addfile(member, _ZeroReader(content_bytes))


def _increase_only_file_size(path: Path, content_bytes: int) -> None:
    # The generated USTAR has one root directory header followed by the file
    # header. Increasing the declared file by one byte lets normalization reject
    # on metadata without writing a second multi-GiB source archive.
    header_offset = tarfile.BLOCKSIZE
    with path.open("r+b") as archive:
        archive.seek(header_offset)
        header = bytearray(archive.read(tarfile.BLOCKSIZE))
        if len(header) != tarfile.BLOCKSIZE:
            raise RuntimeError("Generated capacity archive is truncated")
        header[124:136] = f"{content_bytes + 1:011o}\0".encode("ascii")
        header[148:156] = b"        "
        checksum = sum(header)
        header[148:156] = f"{checksum:06o}\0 ".encode("ascii")
        archive.seek(header_offset)
        archive.write(header)


def run_repository_gate(
    work_root: Path,
    *,
    content_bytes: int = 2 * GIB,
    required_free_bytes: int = REQUIRED_FREE_BYTES,
) -> dict:
    if content_bytes <= 0:
        raise ValueError("Repository capacity must be positive")
    work_root.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(work_root).free
    if free < required_free_bytes:
        raise RuntimeError(
            f"Phase 7 repository gate requires {required_free_bytes} free bytes; "
            f"only {free} are available"
        )
    started = time.monotonic()
    cleanup_succeeded = False
    with tempfile.TemporaryDirectory(prefix="phase7-capacity-", dir=work_root) as raw:
        temporary = Path(raw)
        source = temporary / "source.tar"
        normalized = temporary / "repository.tar"
        manifest = temporary / "manifest.json"
        _source_archive(source, content_bytes)
        normalize_source_archive(
            source,
            normalized,
            manifest,
            base_sha=BASE_SHA,
            base_tree_sha=TREE_SHA,
            max_unpacked_bytes=content_bytes,
        )
        _, _, entries = load_manifest(manifest)
        if sum(entry.size for entry in entries.values()) != content_bytes:
            raise RuntimeError("Accepted repository manifest size is incorrect")
        _increase_only_file_size(source, content_bytes)
        try:
            normalize_source_archive(
                source,
                temporary / "rejected.tar",
                temporary / "rejected.json",
                base_sha=BASE_SHA,
                base_tree_sha=TREE_SHA,
                max_unpacked_bytes=content_bytes,
            )
        except ScmPolicyError as error:
            if "configured capacity" not in str(error):
                raise
        else:
            raise RuntimeError("Repository capacity + 1 byte was unexpectedly accepted")
    cleanup_succeeded = not any(work_root.glob("phase7-capacity-*"))
    return {
        "schema_version": 1,
        "accepted_bytes": content_bytes,
        "rejected_bytes": content_bytes + 1,
        "required_free_bytes": required_free_bytes,
        "observed_free_bytes": free,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "cleanup_succeeded": cleanup_succeeded,
        "temporary_archives_remaining": 0 if cleanup_succeeded else 1,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Phase 7 repository boundary gate")
    parser.add_argument("--work-root", type=Path, default=Path(".mewcode"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repository-bytes", type=int, default=2 * GIB)
    parser.add_argument("--required-free-bytes", type=int, default=REQUIRED_FREE_BYTES)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        evidence = run_repository_gate(
            args.work_root.resolve(),
            content_bytes=args.repository_bytes,
            required_free_bytes=args.required_free_bytes,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(evidence, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(evidence, sort_keys=True))
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(f"phase7-capacity: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
