---
status: accepted
---

# Publish Deliveries through the GitHub Git Data API

The trusted SCM boundary downloads a credential-free archive at the pinned base SHA and publishes verified changes by creating Git blobs, a tree, a single-parent commit, a deterministic branch, and a Draft Pull Request through GitHub's REST API. We deliberately do not place `.git`, an authenticated remote, or an installation token in the Attempt Workspace: this adds manifest and Git-object assembly code, but prevents repository code and Agent commands from observing or reusing SCM credentials and makes force push structurally unavailable.
