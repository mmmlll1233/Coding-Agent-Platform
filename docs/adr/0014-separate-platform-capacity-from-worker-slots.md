---
status: accepted
---

# Separate Platform Capacity from Worker Slots

PostgreSQL enforces one Platform Capacity across all active Workers, while every Worker independently limits its own Worker Slots. The existing `MEWCODE_PLATFORM_MAX_CONCURRENT_JOBS` remains the global limit for compatibility; `MEWCODE_PLATFORM_WORKER_MAX_CONCURRENT_ATTEMPTS` is the local limit. Active Workers must agree on Platform Capacity or the later registration fails closed.

## Considered Options

Using one setting for both limits was rejected because adding Workers could either underuse hardware or let a differently configured Worker admit too many Attempts. Persisting a separately managed capacity record was deferred because the local MVP has no control-plane administration surface and active Worker registration can safely establish consensus.

## Consequences

Worker Slot totals may exceed Platform Capacity, but active leases protected by the PostgreSQL claim lock may never exceed it. Operators must deploy the same global value to every active Worker; changing it requires draining the existing Worker set first.
