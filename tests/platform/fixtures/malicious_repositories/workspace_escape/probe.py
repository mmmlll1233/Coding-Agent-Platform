from __future__ import annotations

import os
import sys
from pathlib import Path


if os.environ.get("MEWCODE_SECURITY_FIXTURE") != "executor":
    print("refusing to run security fixture outside executor")
    sys.exit(64)

candidates = [
    Path("../host-canary.txt"),
    Path.home() / ".mewcode-host-canary",
]
configured = os.environ.get("MEWCODE_TEST_HOST_CANARY_PATH")
if configured:
    candidates.append(Path(configured))

for candidate in candidates:
    try:
        print(f"READ {candidate}={candidate.read_text(encoding='utf-8')}")
    except OSError as exc:
        print(f"DENIED {candidate}={type(exc).__name__}")

try:
    link = Path("escape-link")
    link.symlink_to(candidates[0])
    print(f"SYMLINK={link.read_text(encoding='utf-8')}")
except OSError as exc:
    print(f"SYMLINK_DENIED={type(exc).__name__}")
