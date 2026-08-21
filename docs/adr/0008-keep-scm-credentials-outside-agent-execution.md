---
status: accepted
---

# Keep SCM credentials outside Agent execution

A trusted SCM boundary will obtain short-lived GitHub App credentials and perform checkout, commit, push, and Draft Pull Request publication. Agent-controlled commands and repository code never receive GitHub credentials, trading a larger platform adapter for a materially smaller credential-exfiltration surface.

