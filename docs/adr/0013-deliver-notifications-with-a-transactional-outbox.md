---
status: accepted
---

# Deliver Notifications with a Transactional Outbox and at-least-once semantics

A Notification is created in the same PostgreSQL transaction as its source Job Event and is delivered later by an independent Notifier. This prevents a Feishu outage from rolling back or blocking a Job outcome and lets delivery recover after process failure without losing notifications. A custom bot webhook has no idempotency or transaction protocol shared with PostgreSQL, so exactly-once delivery is impossible across the crash window after Feishu accepts a card but before the Notifier records success; delivery is therefore explicitly at-least-once, and every duplicate carries the same stable Notification ID for recognition and audit.

## Considered Options

Synchronous notification in the Job transaction was rejected because external latency and failure would couple Job correctness to Feishu. A best-effort in-memory queue was rejected because process failure could lose terminal notifications. Updating an existing Feishu message was deferred because custom bot callbacks and message lifecycle management are outside the MVP boundary.

## Consequences

Outbox rows are claimed with expiring leases and fencing tokens, retried without a maximum attempt count, and retained until delivered. Normal concurrent Notifiers send a row once, while the documented post-acceptance crash window may produce a duplicate. Outbox backlog and permanent configuration errors are operational alerts, not API readiness failures.
