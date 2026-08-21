---
status: accepted
---

# Deliver verified changes as Draft Pull Requests

MewCode will analyze, modify, and verify code without an approval pause, then create a Draft Pull Request only when the change passes the configured Verification policy. It will not merge changes; ambiguity becomes Needs Input and an unsafe or unsuccessful execution becomes Failed, preserving human review without making every Job synchronous.
