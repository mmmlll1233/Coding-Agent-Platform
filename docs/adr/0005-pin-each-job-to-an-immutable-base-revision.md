---
status: accepted
---

# Pin each Job to an immutable base revision

The platform will resolve the requested GitHub base ref to a commit SHA when accepting a Work Request and execute every Attempt for that Job from the pinned revision. This makes retries reproducible and prevents queue delay or moving branches from silently changing the code under analysis.

