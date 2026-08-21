from __future__ import annotations

import os
import sys
from pathlib import Path


if os.environ.get("MEWCODE_SECURITY_FIXTURE") != "executor":
    print("refusing to run security fixture outside executor")
    sys.exit(64)

hard_limit = 64 * 1024 * 1024
chunk = b"x" * (1024 * 1024)
written = 0
with Path("disk-pressure.bin").open("wb") as output:
    while written < hard_limit:
        output.write(chunk)
        output.flush()
        written += len(chunk)
print(f"wrote={written}")
