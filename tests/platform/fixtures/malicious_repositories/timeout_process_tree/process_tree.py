from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


if os.environ.get("MEWCODE_SECURITY_FIXTURE") != "executor":
    print("refusing to run security fixture outside executor")
    sys.exit(64)

role = sys.argv[1] if len(sys.argv) > 1 else "parent"
deadline = time.monotonic() + 20

if role == "parent":
    subprocess.Popen([sys.executable, __file__, "child"])
elif role == "child":
    subprocess.Popen([sys.executable, __file__, "grandchild"])

heartbeat = Path(f"{role}-{os.getpid()}.heartbeat")
print(f"{role} pid={os.getpid()}", flush=True)
while time.monotonic() < deadline:
    heartbeat.write_text(str(time.monotonic()), encoding="utf-8")
    time.sleep(0.1)
