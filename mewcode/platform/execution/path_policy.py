from __future__ import annotations

from pathlib import PurePosixPath

from .fake import normalize_workspace_path


class WorkspacePathSandbox:
    """Permission-layer lexical gate for the logical Linux workspace."""

    project_root = PurePosixPath("/workspace")

    def check(self, path: str) -> tuple[bool, str]:
        try:
            normalize_workspace_path(path, allow_root=True)
        except Exception as exc:
            return False, str(exc)
        return True, ""

