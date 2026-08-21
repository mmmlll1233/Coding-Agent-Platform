---
status: accepted
---

# Isolate each Attempt in a disposable Docker environment

Each Attempt receives a new bounded tmpfs Attempt Workspace, an internal network, and a two-network Squid sidecar. Agent commands run in separate short-lived non-root containers and reach approved HTTPS domains only through the sidecar; a networkless holder keeps the tmpfs mounted for the Attempt lifetime. This gives retries clean state and makes timeout cleanup enforceable, at the cost of Docker Engine dependence and rebuilding dependencies on every Attempt.
