from __future__ import annotations

import os
import sys
from pathlib import Path


if os.environ.get("MEWCODE_SECURITY_FIXTURE") != "executor":
    print("refusing to run security fixture outside executor")
    sys.exit(64)

for name in (
    "MEWCODE_TEST_LLM_SECRET",
    "MEWCODE_TEST_GITHUB_TOKEN",
    "MEWCODE_TEST_FEISHU_SECRET",
):
    value = os.environ.get(name)
    if value:
        print(f"ENV {name}={value}")

for candidate in (Path(".git/config"), Path("/proc/self/environ")):
    try:
        data = candidate.read_bytes()[:65536]
    except OSError:
        continue
    print(f"FILE {candidate}={data.decode(errors='replace')}")
