# Phase 1 Agent Runtime 交付说明

## 交付边界

Phase 1 将 CLI `-p`、TUI、Remote 和后续平台 Worker 收敛到同一个
`Agent.run()` typed-event 主循环。它只表示 Agent 分析和修改阶段，不包含
Docker Executor、Verification、GitHub 发布或平台 Job 终态。

## Runtime 接口

公共接口由 `mewcode.platform.runtime` 导出：

- `AgentRuntimeFactory` 按 `TUI`、`PROMPT`、`REMOTE`、`PLATFORM` profile
  构建 Runtime。
- `AgentRuntime` 持有 Agent、conversation、registry、权限检查器以及对应入口的
  session、memory、skills、MCP、team 和 worktree 服务，并提供幂等
  `start()`/`aclose()`。
- `JobRunner` 每个实例只执行一个 Attempt，返回 `JobResult`；
  `COMPLETED` 只允许 Worker 推进到后续 Verification 阶段。
- `JobEventSink` 按 `job_id/attempt_id/sequence` 接收串行事件；sink 失败会令
  Attempt 以 `EVENT_SINK_FAILED` 结束。

`PLATFORM` profile 不读取用户或仓库 permissions、hooks、MCP、skills、memory、
team 或 worktree 配置。平台策略位于 system prompt；显式传入的仓库指导以
`trust="untrusted"` 用户上下文注入，不能覆盖平台策略。

## 执行和取消契约

所有工具调用统一执行：存在性/启用检查、pre-tool hook、权限检查、参数校验、
执行、post-tool hook。并发安全工具先串行通过策略门，再并发执行，结果仍按模型
调用顺序返回。

`Bash` 返回 `CommandExecutionResult(exit_code, stdout, stderr, timed_out)`；任何
非零退出码都是工具错误。POSIX 使用独立 process group，Windows 使用 Job
Object，超时和任务取消都会清理整棵进程树。

session 和 turn 生命周期由主循环外层 `try/finally` 保证对称，正常完成、异常、
取消或事件流提前关闭都会执行相应的结束 hook。

