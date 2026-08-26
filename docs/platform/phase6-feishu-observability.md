# Phase 6 飞书通知与可观测性交付说明

## 一致性与投递语义

五类持久化 Job Event 会在创建事件的同一 PostgreSQL 事务中生成版本化
`NotificationEnvelope`：

| Job Event | Notification |
| --- | --- |
| `job_received` | `JOB_ACCEPTED` |
| `job_needs_input` | `NEEDS_INPUT` |
| `job_succeeded` | `SUCCEEDED` |
| `job_failed` | `FAILED` |
| `job_cancelled` | `CANCELLED` |

幂等 API replay、普通重新排队、自动租约恢复和 `CANCEL_REQUESTED` 不产生通知。
唯一键为 `(job_id, source_event_sequence, destination)`，因此同一 Job 的不同 Attempt
可以分别产生 `NEEDS_INPUT` 或 `FAILED`。Outbox 只保存逻辑目的地和卡片所需的脱敏、
限长业务字段，不保存 webhook、签名 secret、API Key或原始日志。

Notifier 使用 `FOR UPDATE SKIP LOCKED` 领取一条到期记录，提交 owner、随机 fencing
token和默认 60 秒租约后，在事务外调用飞书。成功确认和失败重排都必须匹配 owner 与
fencing token；进程退出后，过期 `IN_FLIGHT` 可由另一 Notifier 接管，迟到 ACK 无效。
Job 的业务事务不等待飞书，通知失败不会改变 Job 终态。未投递通知会阻止对应终态 Job
被 retention janitor 删除，成功后恢复原有保留策略。

飞书自定义机器人不支持与 PostgreSQL 共享事务或幂等键，因此交付是明确的
at-least-once：正常并发只有一个 Notifier 发送；飞书已接收而数据库确认前进程崩溃的
窗口可能重复。重复卡片携带相同 Notification ID。该边界由
[ADR 0013](../adr/0013-deliver-notifications-with-a-transactional-outbox.md) 固定。

## 飞书安全边界

Notifier 只接受
`https://open.feishu.cn/open-apis/bot/v2/hook/<token>`，拒绝 HTTP、userinfo、非标准
端口、query、fragment和其他主机。HTTP client关闭 redirect和环境代理，默认超时
10 秒。webhook和签名 secret分别从只读 Compose secret读取，只挂载到 Notifier；
API、Worker和Executor不获得这些值。

请求使用飞书 HMAC-SHA256 时间戳签名。成功必须同时满足 HTTP 2xx，以及 JSON响应
`code=0` 或兼容的 `StatusCode=0`。HTTP 429、5xx、网络/协议错误、超时和业务错误均
无限重试；退避从 5 秒指数增长，加入 ±20% jitter，最大 15 分钟。有效
`Retry-After` 优先使用且同样受 15 分钟上限约束。

五类卡片的 Work Request、仓库、错误和标识字段全部使用 `plain_text` 并限制长度，
避免 mention/Markdown注入。只有 `SUCCEEDED` 且 URL严格匹配当前 GitHub Draft PR
格式时才展示链接按钮。日志不记录请求体、响应原文或 webhook URL；统一 redactor
覆盖完整 webhook、hook path、签名 secret、数据库密码和其他服务凭证。

## 可观测性与健康检查

Compose 设置 `MEWCODE_PLATFORM_LOG_FORMAT=json`。每行日志固定包含 `timestamp`、
`level`、`service`、`logger`、`event` 和 `message`，并按执行边界传播 `request_id`、
`job_id`、`attempt_id`、`worker_id`、`notification_id`、`event_type`。异常堆栈序列化后
再次脱敏。API只记录 route template、method、status和duration，不记录 path参数、
query、Authorization或请求体。

Prometheus端点为：

- API `:8080/metrics`：HTTP数量/延迟、database/schema/worker/notifier readiness。
- Worker `:9091/metrics`：active attempts、outcome数量/耗时、lease recovery、janitor failure。
- Notifier `:9092/metrics`：投递结果/耗时、pending/in-flight backlog、最老待投递年龄、heartbeat failure。

标签只包含 route template、method、status class、固定 outcome/result/event type和状态，
不包含 Job ID、仓库、Requester或Notification ID。Compose只将端口绑定到
`127.0.0.1`，不部署 Prometheus或Grafana。

`/health/live` 仍只表示 API进程存活。`/health/ready` 分别检查数据库、schema、Worker
心跳和（启用通知时）Notifier心跳；任一必要检查失败返回 503。飞书不可用和 backlog
不会使 API not-ready，而是通过日志和指标告警。

## 配置与权限

Notifier 命令为：

```text
mewcode-platform notifier
```

主要配置与默认值：

```text
MEWCODE_PLATFORM_NOTIFICATIONS_ENABLED=true
MEWCODE_PLATFORM_NOTIFICATION_DESTINATION=feishu:platform
MEWCODE_PLATFORM_FEISHU_WEBHOOK_URL_FILE=/run/secrets/feishu_webhook_url
MEWCODE_PLATFORM_FEISHU_SIGNING_SECRET_FILE=/run/secrets/feishu_signing_secret
MEWCODE_PLATFORM_NOTIFIER_ID=notifier-compose
MEWCODE_PLATFORM_NOTIFICATION_POLL_MILLISECONDS=500
MEWCODE_PLATFORM_NOTIFICATION_LEASE_SECONDS=60
MEWCODE_PLATFORM_NOTIFICATION_TIMEOUT_SECONDS=10
MEWCODE_PLATFORM_NOTIFICATION_BACKOFF_BASE_SECONDS=5
MEWCODE_PLATFORM_NOTIFICATION_BACKOFF_MAX_SECONDS=900
MEWCODE_PLATFORM_WORKER_METRICS_PORT=9091
MEWCODE_PLATFORM_NOTIFIER_METRICS_PORT=9092
MEWCODE_PLATFORM_LOG_FORMAT=json
```

只有 Notifier 启动时读取并校验飞书 secrets；缺失、空文件或非法 webhook会 fail
closed。Compose数据库角色分离为 migrator、API、Worker和Notifier。Notifier只能读写
Outbox和维护服务心跳，不能修改 Job、Attempt或Artifact；API和Worker只获得 Outbox
插入及各自内部维护所需的最小读取权限。

## 验收

默认门禁使用本地 `httpx.MockTransport` 覆盖签名固定向量、卡片 schema、URL/PR
白名单、redirect、HTTP/业务错误、`Retry-After`、无限重试、JSON日志、contextvars、
secret canary和指标标签。PostgreSQL门禁覆盖迁移拒绝历史 Outbox、事务共同回滚、
幂等 replay、多次状态通知、双 Notifier并发、租约恢复、fencing、retention和 readiness。

常规 CI不访问飞书。`.github/workflows/phase6-live.yml` 是受保护的手动门禁，使用真实
PostgreSQL和测试飞书群，只投递带唯一 gate ID 的卡片；其他故障、并发和五类卡片均
由本地 HTTP fake验证。该工作流不得打印或上传 webhook、签名 secret和卡片请求体。

2026-08-26 本地验收结果为默认门禁 690 passed/1 skipped/31 deselected、PostgreSQL
门禁 19 passed、Phase 6定向门禁 19 passed、Docker Executor安全门禁 7 passed、隔离
资源压力门禁 2 passed；生产镜像构建和 Compose config解析通过，结束后无 Attempt
container、network或volume残留。
真实飞书工作流需要受保护 Environment凭证，未在本地执行，不能以本地 fake结果替代。
