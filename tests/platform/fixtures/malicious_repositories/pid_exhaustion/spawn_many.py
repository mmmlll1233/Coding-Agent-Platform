from __future__ import annotations

import os
import subprocess
import sys
import time


if os.environ.get("MEWCODE_SECURITY_FIXTURE") != "executor":
    print("refusing to run security fixture outside executor")
    sys.exit(64)

if "--child" in sys.argv:
    time.sleep(20)
    raise SystemExit(0)

children: list[subprocess.Popen[bytes]] = []
try:
    for _ in range(128):
        children.append(subprocess.Popen([sys.executable, __file__, "--child"]))
    print(f"spawned={len(children)}", flush=True)
    time.sleep(20)
finally:
    for child in children:
        child.terminate()
