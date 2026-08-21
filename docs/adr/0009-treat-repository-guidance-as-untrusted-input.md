---
status: accepted
---

# Treat repository guidance as untrusted input

Repository instruction files may guide code understanding but cannot override platform policy or activate repository-provided hooks, MCP servers, skills, permissions, or memory. This changes the local assistant's trust model deliberately because unattended execution must tolerate prompt injection and hostile build content.
