# Phase 2 Docker ExecutionEnvironment 交付说明

## 交付边界

Phase 2 为 `PLATFORM` profile 增加每 Attempt 独立的 Docker 强制隔离层。CLI
`-p`、TUI 和 Remote 仍使用原有宿主工具；Control API、PostgreSQL、GitHub App、
Verification、Artifact Store 和通知不在本阶段。

## 公共接口

`mewcode.platform.execution` 导出：

- `AttemptExecutionSpec`：绑定 Job/Attempt、Executor 与代理镜像摘要、资源限额、
  平台出网白名单、受信任状态目录和脱敏值。
- `ExecutionEnvironment`：定义 `start()`、`run_command()`、`workspace`、
  `import_archive()`、`export_archive()` 和 `aclose()`。
- `WorkspaceAccess`：在容器内实现 read/write/edit/glob/grep，以 SHA-256 内容版本
  保持先读后写和并发修改检测。
- `DockerExecutionEnvironment`：使用 Docker Python SDK 和结构化 Engine API；
  `FakeExecutionEnvironment` 用于非 Docker 契约测试。

`PLATFORM` Runtime 缺少 ExecutionEnvironment 时直接拒绝构建。其工具 registry 只
包含 Bash、ReadFile、WriteFile、EditFile、Glob 和 Grep；仓库 hooks、permissions、
MCP、skills、memory、team、subagent 和 worktree 均不加载。环境上下文固定呈现
Linux、`/workspace` 和 `/bin/sh`，Context spill 与 RecoveryState 写入不挂载进
Executor 的 `job_id/attempt_id` 受信任目录。

## Attempt 隔离

- 每个 Attempt 创建有 size/inode 上限的 tmpfs named volume。无网络、只读根文件
  系统的 holder 仅保持 tmpfs 挂载；重试创建全新资源。
- 受信任 tar 导入排除 `.git/**`、`.mewcode/**` 和精确 `.env`；`.github/**`
  进入独立只读 volume。工作区不使用宿主 bind mount，也不挂载 Docker socket。
- 文件 helper 固定运行在 holder 内，通过 stdin JSON 接收请求；它拒绝 `~`、NUL、
  父目录、工作区外绝对路径和逃逸 symlink。
- 每条 Bash 调用创建一个非 root 短命令容器。正常结束、超时、取消或输出超限后
  都强制删除整个容器，因此子孙进程不能跨命令存活。

默认限额为 4 CPU、6 GiB 内存、256 PID、3 GiB Attempt Workspace、256 MiB
`/tmp`、30 万 inode、单命令 600 秒、Attempt 3600 秒和 stdout/stderr 合计
1 MiB。Executor 使用只读根文件系统、`cap_drop=ALL`、
`no-new-privileges`、UID/GID 65532、无设备、无宿主端口并限制 nofile/nproc。

## 出网与凭证

命令容器只连接 Attempt internal network，外部 DNS 被禁用，并通过代理在 internal
网络上的 IP 访问 Squid。非 root Squid 侧车的第二张网卡连接平台 egress bridge；
默认只允许 CONNECT 到 `pypi.org:443` 和 `files.pythonhosted.org:443`。Squid 在
域名放行后仍拒绝解析到环回、RFC1918、链路本地或云元数据范围的地址，并关闭
缓存和访问日志。

Executor 环境由固定白名单构造，不复制 Worker 环境。LLM、GitHub 和通知凭证不
进入容器。已注册敏感值在 stdout、stderr、ToolResult、JobEvent、RecoveryState
及持久化工具预览之前统一替换为 `[REDACTED]`。

## 终止与清理

资源全部带 `com.mewcode.managed/job_id/attempt_id/resource` labels。`aclose()` 在
10 秒有界窗口内删除活跃命令、代理、holder、Attempt network 和 volumes，随后
按 labels 复查。`JobRunner` 无论完成、异常、超时或取消都会关闭 Runtime；只要
不能证明资源清零，结果强制为 `FAILED/EXECUTOR_CLEANUP_FAILED`。

普通非零退出码和命令超时继续作为可反馈给 Agent 的工具错误。内存、磁盘、
inode、PID、输出上限、Executor 丢失或隔离环境错误使用内部 fatal 标记终止 Agent。

## 构建与测试

Executor 镜像由 `docker/executor/Dockerfile` 构建，基础镜像固定 digest；生产启动
必须传入构建结果和 Canonical Squid 的不可变 digest。安全测试还构建
`docker/mock-package/Dockerfile`，在独立 egress bridge 提供本地 HTTPS package
endpoint，不访问真实 PyPI。

```bash
uv run pytest -q -m executor_security --enforce-platform-outcomes
uv run pytest -q -m resource_exhaustion
```

PR 的 Ubuntu 门禁执行 `executor_security`；`resource_exhaustion` 由夜间和手动
工作流执行。每个测试按 labels 断言无残留，不运行全局 Docker prune。
