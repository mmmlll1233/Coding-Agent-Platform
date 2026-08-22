# MewCode Coding Agent 平台威胁模型

## 1. 目的与范围

本文档定义内部单租户 MVP 在接收 Work Request、执行 Agent、运行 Verification、发布 GitHub Draft Pull Request 和投递通知时的安全边界。它是 Phase 0 起持续维护的测试依据；Docker ExecutionEnvironment 已在 Phase 2 实现，Control API 与 PostgreSQL 持久工作流已在 Phase 3 实现，GitHub App、Verification 发布链路和通知仍属于后续阶段。

MVP 把 Requester、Work Request、附件、仓库内容、仓库指导文件、构建脚本、依赖代码、Agent 生成的命令和 Verification 命令全部视为不可信输入。内部单租户降低了身份和计费复杂度，但不降低仓库内容、供应链代码或提示注入的风险。

## 2. 安全不变量与优先级

下列约束从高到低生效，低层输入不得覆盖高层约束：

1. 平台安全策略、凭证隔离、网络和资源限制。
2. 固定的 Repository Target，包括接收 Work Request 时解析的不可变 base SHA。
3. Requester 提供的 Verification Contract；Agent 可以增加检查，但不能删除、弱化或重新解释已有检查。
4. Work Request 的业务意图与补充输入。
5. `AGENTS.md`、`MEWCODE.md`、源码注释和其他仓库指导。

任何 Attempt 不能证明这些不变量时必须 fail closed：进入 `NEEDS_INPUT` 或 `FAILED`，不得创建 Delivery。只有 Draft Pull Request 已创建、URL 已持久化且完整 Verification 成功后，Job 才能进入 `SUCCEEDED`。

## 3. 受保护资产

- 源码、未提交 diff、附件以及内部业务信息。
- LLM API Key、GitHub App private key、installation token、飞书 webhook 和签名 secret。
- 宿主文件、用户目录、Docker socket、其他 Attempt Workspace 和平台私网。
- Job、Attempt、Event、Artifact、Outbox 数据及其租户和顺序边界。
- 固定 base SHA、Verification 证据、发布分支和 Draft Pull Request 的完整性。
- Worker、Executor、Notifier 的可用性及资源容量。

## 4. 信任边界

| 边界 | 受信任侧 | 不可信侧 | 必须执行的控制 |
| --- | --- | --- | --- |
| Requester → Control API | API、schema、认证和幂等逻辑 | Work Request、命令、附件 | 认证、大小/类型/超时校验、禁止凭证、固定 base SHA |
| GitHub → SCM Adapter | 受信任 SCM Adapter | 仓库文件、Git 配置、hooks、依赖代码 | 禁用 hooks/LFS/submodule，清除凭证，只获取固定 SHA |
| AgentRunner → Executor | AgentRunner、平台 policy gate | Agent 命令和仓库进程 | 非 root、最小挂载、资源限制、进程组取消、清洗环境 |
| Executor → Network | egress proxy 和白名单 | 任意出站请求 | 拒绝环回、私网、元数据地址和非白名单域名 |
| Worker → Docker Engine | 专用受信任 Worker | Executor 容器 | Docker socket 不挂载给 Executor，终态强制销毁 |
| 平台 → LLM/GitHub/飞书 | 各服务适配器 | 外部服务响应和失败 | 最小权限、短期凭证、超时、重试、脱敏 |
| AgentEvent → JobEvent/Artifact | 持久化和脱敏层 | 工具输出、日志、模型文本 | canary 脱敏、Job/Attempt/sequence 绑定、保留策略 |

## 5. 威胁与测试追踪矩阵

| 威胁编号 | 攻击方式 | 预期控制与失败方式 | 自动化测试/夹具 | 负责阶段 |
| --- | --- | --- | --- | --- |
| `INJ-001` | 仓库指导要求泄漏秘密、绕过 Verification 或发布非 Draft PR | 平台策略优先；无法安全继续时 `NEEDS_INPUT`/`FAILED`，不创建 PR | `prompt_injection` | Phase 1/2 |
| `CFG-001` | 仓库携带 permissions、hooks、MCP、skills、memory 或本地配置 | 平台模式不加载仓库提供的扩展能力 | `prompt_injection` | Phase 2 |
| `CRED-001` | 脚本枚举环境、进程环境、Git 配置和常见 secret 文件 | 凭证不进入 Executor、prompt、工具参数或 Git remote | `secret_canary` | Phase 2/4 |
| `CRED-002` | 模型或工具把已观察到的敏感值写入日志、Event、Artifact 或 diff | 所有出口统一 redaction；发现 canary 时 Attempt 失败并报警 | `secret_canary` | Phase 3/5/6 |
| `FS-001` | 使用 `..`、绝对路径、`~` 或 symlink 访问 workspace 外文件 | 统一路径边界和最小挂载；越界操作被拒绝 | `workspace_escape` | Phase 2 |
| `PROC-001` | 超时命令派生子孙进程，取消 coroutine 后继续运行 | kill 进程组并销毁容器；发现残留时 Attempt 失败 | `timeout_process_tree` | Phase 1/2 |
| `RES-001` | 持续写文件耗尽 Attempt 或宿主磁盘 | Attempt Workspace 的 size/inode 限制，写满只影响当前 Attempt | `disk_exhaustion` | Phase 2/7 |
| `RES-002` | fork/spawn 大量进程耗尽 PID | 容器 PID 限制和终态清理 | `pid_exhaustion` | Phase 2/7 |
| `NET-001` | 访问环回、RFC1918、云元数据、宿主服务或任意公网 | Executor 只能经过 egress proxy 访问平台白名单 | `egress_probe` | Phase 2/7 |
| `SCM-001` | Agent 命令读取或复用 GitHub installation token | token 只存在于 SCM Adapter，Executor 的 Git remote 不含凭证 | `secret_canary` 加 SCM 集成测试 | Phase 4 |
| `SCM-002` | force push、改写 base、修改 workflows、启用 LFS/submodule/hooks | SCM Adapter 固定 SHA、限制路径和发布操作；拒绝 Delivery | GitHub 测试仓库 | Phase 4 |
| `VER-001` | Agent 跳过、替换或将非零 Verification 解释为成功 | 平台逐条运行原始 Verification Contract；任一失败均不发布 | Verification 集成测试 | Phase 1/5 |
| `EVT-001` | 内存事件丢失、乱序或跨 Job 混合 | 持久化前绑定 job/attempt/sequence，SSE 只投影持久化事件 | JobEvent/恢复测试 | Phase 3 |
| `AVAIL-001` | LLM、GitHub、飞书或 Worker 崩溃造成重复任务或重复 PR | 租约、幂等键、Outbox 和幂等 SCM 发布 | 崩溃与重复投递测试 | Phase 3/4/6/7 |

## 6. Phase 0 测试策略

- 默认测试只验证安全夹具清单、威胁追踪完整性和宿主执行保护，不执行资源耗尽或真实网络探测。
- 所有安全脚本要求 `MEWCODE_SECURITY_FIXTURE=executor` 才能运行，并带硬编码的运行时间、输出、进程数或写入字节上限。
- canary 必须是测试时生成的假值，不得在仓库中保存真实 provider、GitHub 或飞书凭证。
- Phase 1 的已知 Agent Runtime 缺陷已升级为普通契约测试；Phase 2 的七类夹具已接入 Docker 隔离执行断言。
- 默认单元测试门禁不访问真实 LLM、GitHub、飞书或 Docker daemon；独立的
  Ubuntu Phase 2 门禁只访问本机 Docker daemon 和同一 egress bridge 上的 HTTPS
  mock endpoint，不访问外部测试目标。

## 7. 评审和变更规则

新增平台能力、凭证、网络目的地、持久化出口或可执行扩展时，必须先增加威胁编号和测试映射。安全测试可以从 `xfail`/清单契约升级为可执行测试，但不得静默删除；删除威胁必须在 ADR 中说明风险为何消失。

## 8. Phase 0 验收记录

2026-08-21 在 Windows、Python 3.13 的隔离临时目录中执行：

```text
python -m pytest -q --strict-markers --enforce-phase0-outcomes \
  -m "not executor_security and not resource_exhaustion"

584 passed, 1 skipped, 4 xfailed
```

唯一 skip 是 Windows 缺少 symlink 创建能力时的 `PHASE0-CAPABILITY-SYMLINK`；四个严格 xfail 分别对应 Phase 1 的统一 policy gate、Bash 非零退出码、进程树取消和生命周期对称性。Linux/Windows 与 Python 3.11/3.13 的其余组合由 GitHub Actions 测试矩阵持续验证。

## 9. Phase 1 验收记录

2026-08-21 在 Windows、Python 3.13 上执行升级后的平台门禁：

```text
python -m pytest -q --strict-markers --enforce-platform-outcomes \
  -m "not executor_security and not resource_exhaustion"

600 passed, 1 skipped
```

四个 Phase 1 `xfail` 已全部升级为普通测试。统一 policy gate、结构化 Bash
结果、Windows Job Object/POSIX process group 取消、对称 lifecycle、Runtime
profile 隔离以及 JobRunner 四类结果均已有自动化覆盖。唯一 skip 仍是 Windows
缺少 symlink 创建能力时的 `PHASE0-CAPABILITY-SYMLINK`。Docker、资源耗尽和
真实隔离断言的已交付结果见下方 Phase 2 验收记录。

## 10. Phase 2 验收记录

2026-08-21 在 Docker Desktop Linux Engine、cgroup v2、Python 3.13 上执行：

```text
python -m pytest -q tests/platform/test_docker_execution.py -m executor_security
6 passed, 2 deselected

python -m pytest -q tests/platform/test_docker_execution.py -m resource_exhaustion
2 passed, 6 deselected
```

安全门禁覆盖 secret canary、路径和 symlink 逃逸、`.github` 只读、提示注入配置
隔离、Docker inspect 契约、超时/取消进程树、输出上限、proxy-only egress、磁盘和
PID 限制。出网成功路径使用同一 Docker egress bridge 上的本地 HTTPS mock
package endpoint；测试不依赖真实 PyPI。每个用例结束后按 Attempt labels 验证
container、network 和 volume 在 10 秒内清零，未使用全局 Docker prune。

## 11. Phase 3 验收记录

2026-08-21 在 PostgreSQL 16、Python 3.13 上执行独立持久化门禁：

```text
python -m pytest -q -m platform_postgres
8 passed
```

`EVT-001` 已覆盖 Job 全局 sequence、跨 Attempt 顺序、SSE 所依赖的持久化读取、
Requester 隔离和统一脱敏。`AVAIL-001` 的 Phase 3 部分已覆盖并发幂等提交、数据库
全局单并发、Worker Lease、heartbeat、fencing、过期后一次自动恢复和迟到 Worker
写入拒绝。测试使用 localhost PostgreSQL，不访问真实 LLM、GitHub 或飞书。
