---
status: accepted
---

# Use a durable asynchronous Job API

Submitting a Work Request returns an idempotent Job identity instead of holding an HTTP connection for the Agent run. Job state and events are persisted and exposed through REST and resumable server-sent events, allowing work to survive caller disconnects and service restarts.

