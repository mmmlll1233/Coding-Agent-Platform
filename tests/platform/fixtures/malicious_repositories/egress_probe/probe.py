from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request


if os.environ.get("MEWCODE_SECURITY_FIXTURE") != "executor":
    print("refusing to run security fixture outside executor")
    sys.exit(64)

targets = (
    "http://127.0.0.1/",
    "http://10.0.0.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://host.docker.internal/",
    "https://not-allowlisted.invalid/",
)
for target in targets:
    try:
        with urllib.request.urlopen(target, timeout=1) as response:
            print(f"REACHABLE {target} status={response.status}")
    except (OSError, urllib.error.URLError) as exc:
        print(f"DENIED {target} error={type(exc).__name__}")
