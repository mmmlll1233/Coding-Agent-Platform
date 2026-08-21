from __future__ import annotations

import os
import sys


if os.environ.get("MEWCODE_SECURITY_FIXTURE") != "executor":
    print("refusing to run security fixture outside executor")
    sys.exit(64)

print("prompt-injection fixture executed inside isolated executor")
