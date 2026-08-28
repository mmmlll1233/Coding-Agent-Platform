# Phase 7 端到端硬化交付说明

## 已实现边界

Phase 7 保持 Control API 和 Artifact 类型不变，硬化以下运行边界：

- `MEWCODE_PLATFORM_MAX_CONCURRENT_JOBS` 是 PostgreSQL 强制的 Platform Capacity；新增 `MEWCODE_PLATFORM_WORKER_MAX_CONCURRENT_ATTEMPTS` 作为 Worker Slot，默认均为 `1`。活跃 Worker 声明不同 Platform Capacity 时以 `CAPACITY_CONFIGURATION_MISMATCH` 拒绝启动。
- Worker 收到停止请求后发布 `draining=true`、停止领取、继续维护活动 Lease；默认等待 `MEWCODE_PLATFORM_WORKER_SHUTDOWN_GRACE_SECONDS=300`，之后取消 Processor并让过期 Lease按现有一次自动恢复规则接管。Compose `stop_grace_period` 为330秒。
- Worker恢复循环在数据库租约恢复后，以仍存活的 Job/Attempt ID作为白名单，只清理由 `com.mewcode.managed=true`和 `com.mewcode.attempt_id`共同标记的孤儿容器、Attempt网络和卷，以及 `state_root/attempts`下不在白名单中的固定哈希状态目录；共享 egress网络没有 Attempt标签，不在回收范围。该路径用于硬杀进程后的资源与临时归档收敛，并拒绝全局 prune。
- `MEWCODE_PLATFORM_ATTEMPT_TIMEOUT_SECONDS` 默认且最大为3600。每个 Attempt独立计时，排队和 `NEEDS_INPUT` 不计入；超时错误码为 `ATTEMPT_DEADLINE_EXCEEDED`，清理/幂等发布对账最多额外使用30秒。
- Repository Size按规范化固定 SHA工作树中的普通文件和symlink目标字节计算，默认上限2GiB；GitHub压缩归档下载另有2.25GiB硬上限，Executor Workspace仍为3GiB。
- GitHub App installation token仅在进程内按 App、installation、仓库和精确权限集缓存，并在到期前60秒失效；API/Worker不把 token写入磁盘。GitHub只读请求、归档下载和 token签发对瞬时网络、429与5xx做最多3次短退避，仓库写入请求不做盲重试，仍由 Delivery分支和 Draft PR对账收敛。
- Worker指标新增全局/本地容量、排空状态、排队数量、最老排队年龄和停机结果；readiness忽略正在排空的 Worker。

容量分层的长期取舍见 [ADR 0014](../adr/0014-separate-platform-capacity-from-worker-slots.md)，通知重复边界仍以 [ADR 0013](../adr/0013-deliver-notifications-with-a-transactional-outbox.md) 为准。

## 无凭证门禁

常规测试覆盖配置兼容、容量漂移拒绝、排空 readiness、全局容量不超卖、缩时 Attempt deadline及失败 Artifact、Repository Size恰好上限与 `+1`拒绝、备份 manifest校验和阶段7证据 fail-closed验证。

PostgreSQL测试包含一个服务器门禁预演：Platform Capacity和单 Worker Slot均为5，前5个 Attempt由屏障保持 `RUNNING`，其余5个保持 `QUEUED`；释放后两批共10个 Job全部完成。该测试证明门禁逻辑，不代表目标服务器已经完成迁移或容量验收。

真实 Repository Size I/O门禁：

```powershell
uv run python scripts/phase7_capacity_gate.py `
  --output .mewcode/phase7-evidence/repository-capacity.json
```

脚本先要求工作盘至少有12GiB可用空间，实际流式生成并规范化2GiB内容，再把同一归档声明增大1字节验证拒绝，最后验证临时目录清零。`--repository-bytes` 和 `--required-free-bytes` 仅用于常规缩时测试，正式证据必须保留默认值。

## 正式本地证据

`scripts/phase7_gate.py verify-local INPUT OUTPUT` 只接受完整、脱敏的 `schema_version=1` 证据，并验证：

- 同一实现 SHA和常规 CI run ID下恰好20个成功 Job，数据库计数也必须为20；其中18个确定性、2个真实模型。每个 Job至少两次同 Idempotency-Key提交、固定 base SHA、Verification成功、恰好一个 Draft PR，并在取证后关闭 PR和删除分支。
- API重启、Worker执行中硬杀、PR创建后落库前硬杀和通知中断均恢复。
- 18次记录型通知无普通并发重复，2次真实飞书投递，Outbox归零。
- 实际2GiB/`+1`边界、3600秒停止非可信工作且3630秒内完成终态、永久 Verification失败四类 Artifact、空项目备份恢复 manifest/哈希/readiness/Lease启动检查、五类凭证扫描面和逐故障零残留全部通过。
- 服务器门禁必须明确记录为 `PENDING`，不能被省略或误报为已通过。

验证器拒绝 API key、private key、password、webhook、签名 secret、原始请求/响应体和常见凭证格式。输出只保留 Job ID、经过 SHA-256处理的 PR identity、固定 SHA、计数、摘要哈希和通过状态。真实密钥不得进入仓库、Artifact或验收 JSON。

## 验收状态

Phase 7 本地单并发 MVP 已于2026-08-28正式验收通过，固定的验收实现为
`e1936c457054191822a86dea5da862656d0a9e93`。常规 CI run为
`33071125986`，脱敏证据通过 `scripts/phase7_gate.py verify-local`，本地 gate ID为
`phase7-20260828T011406Z-614c8ac198b2`，源证据 SHA-256为
`614c8ac198b25d53c47cbc2547fa0e5901efb259774e58143281c57888259232`。

本地验收结果：

- 同一实现 SHA连续完成20个逻辑 Job，其中18个确定性模型、2个真实 LLM；固定目标 base SHA为 `2f56e5913b97c0618d0b5cd47fb20f179add6348`。同 Idempotency-Key均至少提交两次，数据库最终恰好20个 Job、22个 Attempt、489个 Event、84个 Artifact和40个已投递 Outbox，待投递为0。
- API重启、Worker执行中硬杀、真实 Draft PR创建后落库前硬杀、通知临时不可用均恢复；20个 Job全部 `SUCCEEDED`、Verification全部成功，每个 Job恰好一个 Draft PR。取证后20个 PR全部关闭，测试分支全部删除。
- 18个确定性终态通知由记录器无重复接收，2个真实模型 Job各投递一张真实飞书测试卡片；正常并发重复数为0。
- Repository Size真实流式门禁接受 `2147483648`字节并拒绝 `2147483649`字节，耗时8.914秒；临时归档和 Docker资源均为0。
- 真实 deadline配置为3600秒，非可信执行在3600.518秒停止，3601.324秒完成 `ATTEMPT_DEADLINE_EXCEEDED`终态、Artifact生成和清理，未发布 PR且零残留。
- 永久 Verification失败保留且可下载 `diff`、`agent_log`、`command_log`和 `verification_report`，未发布 PR且零残留。
- 停机一致备份在一次性空 Compose项目恢复，数据库计数、Artifact哈希、readiness和 Lease恢复均通过；manifest SHA-256为 `df491b34a23478c45c6867e6dacb94e90f77f4dcaac26f38aeacd05eab1cafb8`。
- 凭证扫描覆盖数据库、Artifact、日志、PR和通知记录；扫描427个候选文件并与32个本地凭证值比对，泄漏数为0。每个故障场景结束后的 Attempt容器、网络、卷和临时归档均为0。

目标服务器5并发门禁仍明确为 `PENDING`。这不影响 Phase 7本地单并发 MVP验收，
但在服务器完成“恰好5个 Attempt同时运行、第6个保持 `QUEUED`、两批共10个 Job成功”
之前，不得宣告迁移后的持续运行 Go/No-Go通过。
