# Phase 5 Verification 与 Artifact 交付说明

## 生产链路

内置 `mewcode.platform.processing:create_attempt_processor_factory` 将固定 `base_sha`
准备、Docker Workspace 导入、Setup、PLATFORM Runtime、完整 Verification、最多两轮
Repair Round、Workspace 导出、Executor 清理、四类 Artifact 持久化和 Draft Pull Request
发布串成一个生产 Attempt。每次 Repair Round 复用同一 Runtime 与对话，但使用新的
`JobRunner`；初始事件序号承接上一轮最终序号，Attempt 事件不会重复或倒退。

Setup 遇到首个非零退出或超时立即以 `SETUP_FAILED` 结束。Verification 每轮都执行
全部声明命令，只有最后完整一轮全部成功才可发布。普通失败最多反馈 Agent 两次；
`EXECUTION_RESOURCE_LIMIT`、`EXECUTOR_LOST`、其他执行边界 fatal、清理失败和 Artifact
失败均不进入 Repair Round。Runtime 的权限请求映射为 `NEEDS_INPUT`，普通命令失败
不会占用该状态。

Attempt 外层截止时间为 3600 秒；Setup 和每轮 Verification 的声明超时总和分别不超过
600 秒。API 和 Worker 都验证这一约束，避免旧数据或绕过 API 的写入扩大执行预算。

## Artifact

Worker 在可信命名卷中原子写入以下 Attempt 证据：

- `agent_log.ndjson`：连续、脱敏的 Runtime 事件。
- `command_log.ndjson`：阶段、轮次、命令、退出码、超时、耗时和输出。
- `diff.patch`：全部变化路径的模式、SHA-256、大小，以及文本 unified diff；二进制显式标记。
- `verification_report.json`：版本、所有 Verification 轮次、最终结果、Repair Round 数、变化路径清单和关联 Artifact ID。

`LocalArtifactStore` 只接受由三个 UUID 组成的 storage key，使用同目录临时文件、`fsync`、
`0600` 权限和原子重命名。metadata 写入必须持有当前 Worker Lease 和 fencing token。
默认单 Artifact 64 MiB、单 Attempt 128 MiB；日志和 diff 会保留显式截断标记，并为不可
截断的 Verification Report 预留容量。所有文本在落盘前经过共享 secret redactor。

Artifact 默认保留 7 天，终态 Job、Attempt 和事件保留 30 天；`NEEDS_INPUT` 不自动过期。
Worker 启动时运行 janitor，之后默认每小时运行一次。janitor 先删除 Artifact 文件和
metadata，再删除超过保留期的终态 Job。

## API 与取消闸门

Requester 可访问：

```text
GET /v1/jobs/{job_id}/artifacts
GET /v1/jobs/{job_id}/artifacts/{artifact_id}
```

列表不返回内部 `storage_key`。下载响应提供 SHA-256 ETag、Content-Length、固定安全
文件名、`nosniff` 和私有不可缓存策略；`ArtifactService` 读取时会重新校验实际大小和
SHA-256。不存在、过期、文件丢失或非所属 Requester统一返回 404。API 对 Artifact 卷
只有只读权限；附件上传路由仍不存在，所有
`attachment_ids` 仍必须为空。

Processor 只有在 Executor 清理成功且四类 Artifact 全部保存后，才在数据库事务中将
阶段切换为 `PUBLISHING`。`cancel_job` 对 `PUBLISHING` 返回
`409 JOB_NOT_CANCELLABLE`；如果取消事务先提交，阶段切换会失败且不会调用 SCM。

## 配置与 Compose

Worker 必需配置：

```text
MEWCODE_PLATFORM_LLM_PROTOCOL
MEWCODE_PLATFORM_LLM_BASE_URL
MEWCODE_PLATFORM_LLM_MODEL
MEWCODE_PLATFORM_LLM_API_KEY_FILE
MEWCODE_PLATFORM_EXECUTOR_IMAGE
MEWCODE_PLATFORM_PROXY_IMAGE
MEWCODE_PLATFORM_GITHUB_APP_CLIENT_ID
MEWCODE_PLATFORM_GITHUB_PRIVATE_KEY_FILE
```

Compose 使用一次性 `storage-init` 初始化 state 和 Artifact 命名卷权限。Worker 以 UID
65532 非 root 运行，通过 `MEWCODE_DOCKER_SOCKET_GID` 加入 Docker socket 组；Worker
读写 state/Artifact 卷，API 只读 Artifact 卷。LLM key 只挂载到 Worker；GitHub App
private key分别挂载到 API Resolver 和 Worker SCM Adapter，均不会进入 Executor。

## 验收

默认门禁覆盖超时预算、一次/两次 Repair Round、最终失败、全量复验、fatal 不修复、
连续事件序号、Artifact 原子性/配额/哈希/脱敏/路径逃逸、Requester 隔离和下载响应。
PostgreSQL 门禁覆盖 Artifact fencing 与 `PUBLISHING` 取消冲突。

受保护手动门禁为 `.github/workflows/phase5-live.yml`，使用真实 PostgreSQL、Docker 和
GitHub App，但 Runtime 使用确定性 Scripted Agent，不调用真实 LLM。它同时验证失败
不创建分支/PR、Repair 后成功、重复发布幂等、PR 正文证据、secret canary 和 finally
清理。真实 LLM 供应商仅作为部署 smoke test，不属于 CI。

2026-08-25，验收实现提交 `9757b70` 的受保护 GitHub Actions run `32843163043` 通过；
同一提交的常规跨平台 run `32843118872` 同时通过，Phase 5 正式验收完成。
