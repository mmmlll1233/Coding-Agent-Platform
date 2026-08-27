# Phase 7 本地 Compose 运维手册

## 启动前检查

1. 使用 Docker Desktop Linux Engine，确认至少16GiB主机内存和12GiB可用临时磁盘；正式2GiB门禁期间不要并行运行其他大 I/O任务。
2. 所有凭证保存在被 Git忽略的 `.mewcode/secrets/`，文件只授予当前用户读取权限。GitHub App、LLM和飞书凭证不得写入 Compose环境值、命令行、日志或验收 JSON。
3. Executor、proxy和平台镜像使用完整 SHA-256 digest；GitHub App不得拥有 Workflows write权限。
4. 本地 MVP保持：

```text
MEWCODE_PLATFORM_MAX_CONCURRENT_JOBS=1
MEWCODE_PLATFORM_WORKER_MAX_CONCURRENT_ATTEMPTS=1
MEWCODE_PLATFORM_ATTEMPT_TIMEOUT_SECONDS=3600
MEWCODE_PLATFORM_WORKER_SHUTDOWN_GRACE_SECONDS=300
```

启动并检查：

```powershell
docker compose -f compose.platform.yml -p mewcode-platform up -d
docker compose -f compose.platform.yml -p mewcode-platform ps
Invoke-WebRequest http://127.0.0.1:8080/health/ready
```

`/health/live` 只证明 API进程存活；`/health/ready` 还要求数据库/schema、至少一个未排空 Worker以及启用通知时的新鲜 Notifier。

## 排空与部署

正常停止 Worker时使用 Compose `stop`，不要先 `kill`：

```powershell
docker compose -f compose.platform.yml -p mewcode-platform stop worker
```

Worker先停止领取，再等待活动 Attempt最多300秒。超时后 Processor取消并清理 Executor；Job保持受原 Lease保护，重启后在 Lease到期时自动恢复。硬杀只用于故障演练；不得删除 Attempt数据库行或手工复用 fencing token。Worker重启后先等待原 Lease过期，恢复循环把旧 Attempt移出数据库活跃白名单，再按 MewCode管理标签回收其孤儿容器、Attempt网络和卷，并删除 `state_root/attempts`下对应的固定哈希状态目录；共享 egress网络不会被该回收器删除。

部署新容量值前先排空全部 Worker。活跃 Worker的全局值不一致会以 `CAPACITY_CONFIGURATION_MISMATCH` 拒绝后来者；本地槽位可不同，但每个 Worker的槽位不得超过全局上限。

## 监控与故障排查

- API `:8080/metrics`：HTTP与 readiness。
- Worker `:9091/metrics`：active Attempts、Platform Capacity、Worker Slots、queued Jobs、最老排队时间、draining、Lease恢复和停机结果。
- Notifier `:9092/metrics`：Outbox backlog、最老待投递年龄和投递结果。

常见故障：

- `worker=false`：检查 Worker日志、是否处于 Drain、容量配置是否冲突以及 heartbeat年龄。
- `ATTEMPT_DEADLINE_EXCEEDED`：下载四类 Artifact确认最后阶段；超时不得继续 Agent/Verification或创建未对账 PR。
- `WORKER_LEASE_EXPIRED`：第一次会创建新 Attempt；第二次结束 Job。迟到 Worker写入应被 fencing拒绝。
- Notifier backlog增长：Job终态仍有效。恢复飞书后等待重试；正常并发不重复，但飞书接受后 ACK落库前崩溃仍可能出现相同 Notification ID卡片。
- 磁盘不足：停止接收、排空 Worker，检查命名卷和保留策略；只删除明确过期 Artifact，禁止 `docker system prune` 或无范围的 volume删除。

任何清理都必须使用明确的 Compose project或同时具备 `com.mewcode.managed=true`与 `com.mewcode.attempt_id`的标签，并在操作前列出精确目标。

## 停机一致备份

备份脚本不属于安装后的公共 CLI。它停止 API、按300秒宽限排空 Worker，并在仍有 `RUNNING` Attempt时拒绝备份；Notifier随后停止，PostgreSQL保持运行。备份包含 PostgreSQL custom dump、Artifact tar和 `manifest v1`，不包含 secrets或临时 Attempt Workspace。

```powershell
uv run python scripts/platform_ops.py `
  --project-name mewcode-platform `
  backup .mewcode/backups/phase7-001

uv run python scripts/platform_ops.py `
  verify .mewcode/backups/phase7-001
```

默认备份完成后重启 API/Worker/Notifier；正式恢复演练使用 `--leave-stopped`，避免两个项目争用本地端口。备份目录必须是新目录，脚本拒绝覆盖。

## 空环境恢复演练

恢复只允许目标数据库和 Artifact卷为空；先校验两个备份文件的大小/SHA-256，再恢复数据库和 Artifact，运行迁移/权限授予、核对 Job/Event/Artifact/Outbox计数与逐 Artifact哈希、等待 readiness，并确认恢复后的未排空 Worker已重新注册且没有活动 Lease。

```powershell
uv run python scripts/platform_ops.py `
  --project-name mewcode-platform `
  backup .mewcode/backups/phase7-drill --leave-stopped

$env:MEWCODE_PLATFORM_CONTROL_NETWORK="mewcode-phase7-restore-control"
uv run python scripts/platform_ops.py `
  --project-name mewcode-phase7-restore `
  restore .mewcode/backups/phase7-drill
```

恢复项目必须设置独立的 `MEWCODE_PLATFORM_CONTROL_NETWORK`，避免与原项目共享 `postgres` 网络别名；Executor egress网络按设计仍可共享。

验收完成后先确认项目名确为 `mewcode-phase7-restore`，再清理该一次性项目的容器和卷，随后重启原项目。不要把计算出的空变量、通配符或用户目录作为删除目标。

## 正式本地与服务器门禁

正式本地门禁必须在干净、固定且已推送的 SHA上从头完成；任何一项失败都重新开始20-Job streak。18个确定性 Job使用记录型通知服务，2个真实模型 Job才调用真实飞书。PR证据采集后关闭 Draft PR并删除测试分支；两张飞书测试卡片无法自动删除。

将各门禁的脱敏结果组合后执行：

```powershell
uv run python scripts/phase7_gate.py verify-local `
  .mewcode/phase7-evidence/input.json `
  .mewcode/phase7-evidence/summary.json
```

服务器迁移后另以全局容量5、Worker Slot总和至少5运行10个确定性 Job；必须观测5个同时 `RUNNING`、第6个 `QUEUED`、两批全部成功和零残留，再用 `verify-server`验证证据。在此之前服务器状态保持 `PENDING`。
