---
status: accepted
---

# Store Verification evidence outside the Executor before publication

Verification evidence is redacted and persisted in trusted storage outside the disposable Executor, and publication is allowed only after the Executor has been completely removed and every required Artifact has been stored. This ordering adds a storage handoff and makes evidence failure block an otherwise valid change, but prevents repository code from rewriting its audit trail, prevents a Draft Pull Request from referring to missing evidence, and ensures publication never races a live or partially cleaned execution boundary.
