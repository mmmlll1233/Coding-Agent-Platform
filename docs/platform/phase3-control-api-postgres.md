# Phase 3 Control API 与 PostgreSQL 工作流交付说明

## 交付边界

Phase 3 提供持久化 Control API、Requester API Key、Job/Attempt 状态机、JobEvent、
SSE、PostgreSQL 队列、Worker Lease、heartbeat、fencing 和过期恢复。CLI、TUI、
Remote 与 Phase 2 Docker ExecutionEnvironment 保持原行为。

GitHub App Repository Target Resolver 和真实 Attempt Processor 仍属于后续阶段。
Resolver 未配置时，创建 Job 返回 `503 REPOSITORY_RESOLVER_UNAVAILABLE`，不会保存
未解析 base SHA 的 Job；Processor 未配置时 Worker 拒绝启动。Artifact 和
Notification Outbox 本阶段只有表结构与 repository port，不提供附件上传或通知。

## 公共接口

新增 `mewcode-platform` 命令：

```bash
mewcode-platform db upgrade
mewcode-platform db grant-runtime
mewcode-platform api-key create --tenant default --requester internal
mewcode-platform api-key revoke <key-id>
mewcode-platform api
mewcode-platform worker
```

平台配置只读取 `MEWCODE_PLATFORM_*` 环境变量，不读取用户或仓库 YAML。必需配置是
`MEWCODE_PLATFORM_DATABASE_URL`；默认监听 `127.0.0.1:8080`、全局并发 1、
Worker Lease 60 秒、heartbeat 15 秒、恢复扫描 5 秒。Compose secret 可通过
`MEWCODE_PLATFORM_DATABASE_PASSWORD_FILE` 注入数据库密码。

REST API 包括：

- `POST /v1/jobs`：Bearer API Key、`Idempotency-Key`、Repository Target 解析和
  Work Request/Verification Contract 校验。
- `GET /v1/jobs/{job_id}`：当前 Job、Attempt、阶段、Delivery 和错误摘要。
- `GET /v1/jobs/{job_id}/events`：按 Job 全局 sequence 分页。
- `GET /v1/jobs/{job_id}/events/stream`：SSE，支持 `Last-Event-ID` 和 `after`。
- `POST /v1/jobs/{job_id}/input|retry|cancel`：状态受限且不会重复创建 Attempt。
- `GET /health/live|ready`：ready 同时检查数据库、迁移版本和 Worker 心跳。

所有错误统一为 `error.code/message/details/request_id`。阶段 3 不注册
`POST /v1/attachments`，且 Work Request 和补充输入中的 `attachment_ids` 必须为空。

## 持久工作流

每次首次提交、补充输入、手动重开或自动恢复都会得到独立 Attempt。FAILED 停止自动
处理，但 Requester 可显式 `/retry` 重开同一个 Job；SUCCEEDED 和 CANCELLED 不可
重开。SUCCEEDED 的数据库约束要求 PR URL、head SHA 和成功 Verification 证据。

所有状态变化与对应 JobEvent 在同一事务提交。Runtime 的
`JobEvent.attempt_sequence` 只在一个 Attempt 内递增；`job_events.sequence` 由
PostgreSQL 锁定 Job 行后分配，是 SSE 使用的 Job 全局游标。Runtime 事件在持久化前
统一脱敏。

Worker 在 PostgreSQL advisory transaction lock 内执行全局容量判断，再用
`FOR UPDATE SKIP LOCKED` 领取最早的 QUEUED Attempt。每次领取生成 fencing token；
heartbeat、阶段、事件和终态写入都必须同时匹配 Attempt、Worker 和 token，且租约尚未
过期。旧 Worker 的迟到写入会以 `LEASE_LOST` 被拒绝。

租约过期会结束旧 Attempt。Job 最多自动创建一次新 Attempt；再次过期进入 FAILED。
如果 Job 已是 CANCEL_REQUESTED，则过期后直接进入 CANCELLED，不自动重试。

## 本地 Compose

`compose.platform.yml` 使用固定 digest 的 PostgreSQL 和平台基础镜像，为 migrator、
API 和 Worker 分配不同数据库角色。先在忽略提交的 `.mewcode/secrets/` 创建三个随机
密码文件：

```text
.mewcode/secrets/migrator_db_password
.mewcode/secrets/api_db_password
.mewcode/secrets/worker_db_password
```

随后运行：

```bash
docker compose -f compose.platform.yml up --build
```

迁移和 runtime grants 是一次性服务；API/Worker 不会自动修改 schema。Worker 位于
`worker` profile，只有配置后续阶段的
`MEWCODE_PLATFORM_ATTEMPT_PROCESSOR_FACTORY` 时才应启用。Phase 3 单独启动时
`/health/live` 成功而 `/health/ready` 因无可用 Worker 返回 503，符合 fail-closed
边界。

## 测试

默认跨平台门禁不需要 PostgreSQL：

```bash
uv run pytest -q --strict-markers --enforce-platform-outcomes \
  -m "not executor_security and not resource_exhaustion and not platform_postgres"
```

PostgreSQL 门禁使用独立数据库：

```bash
MEWCODE_TEST_DATABASE_URL=postgresql+asyncpg://... \
  uv run pytest -q --strict-markers --enforce-platform-outcomes \
  -m platform_postgres
```

覆盖并发幂等、Requester 隔离、全局容量、租约续期、fencing、一次自动恢复、跨
Attempt 事件顺序、脱敏、API Key 撤销、readiness、取消、显式重开、未验证成功拒绝
和 Worker 清理。CI 使用固定
digest 的 PostgreSQL service，不访问 LLM、GitHub 或通知服务。
