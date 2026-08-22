# AGENTS.md

本文档用于指导 Codex 或其他 AI 编码助手在本仓库中工作。内容以当前 Python 代码为准，优先级高于仓库中可能过期或描述不准确的旧说明。

## 项目概览

MewCode 是一个用 Python 实现的终端 AI 编码助手，形态接近 Codex/Claude Code。它支持三种主要使用方式：

- 交互式 TUI：在终端中进行多轮 AI 编码协作。
- 非交互模式：通过 `-p` 传入单次提示词，输出文本或流式 JSON。
- 远程模式：启动 WebSocket 服务，并通过浏览器 UI 与 Agent 交互。

项目使用 `uv` 管理依赖，入口脚本定义在 `pyproject.toml`：

```toml
mewcode = "mewcode.__main__:main"
```

## 常用命令

```bash
# 安装/同步依赖
uv sync

# 启动终端 TUI
uv run mewcode

# 单次非交互执行
uv run mewcode -p "你的提示词"

# 单次非交互执行，并输出 NDJSON 事件流
uv run mewcode -p "你的提示词" --output-format stream-json

# 启动远程 WebSocket 模式，浏览器访问 http://localhost:18888
uv run mewcode --remote

# 从命令行覆盖权限模式
uv run mewcode --mode bypassPermissions
```

## 测试命令

```bash
# 运行全部测试
uv run pytest

# 运行单个测试文件
uv run pytest tests/test_hello_world.py

# 运行指定测试函数
uv run pytest tests/test_hello_world.py::test_hello_world
```

测试框架是 `pytest`，异步测试依赖 `pytest-asyncio`。测试目录为 `tests/`。

## 目录地图

- `mewcode/__main__.py`：CLI 入口，解析参数，加载配置与 hooks，并分发到 TUI、`-p` 或远程模式。
- `mewcode/agent.py`：核心 Agent 循环，负责 LLM 流式响应、工具调用、权限检查、上下文压缩、记忆抽取和 hook 生命周期。
- `mewcode/client.py`：LLM 客户端抽象和实现，支持 Anthropic、OpenAI Responses API、OpenAI 兼容 Chat Completions。
- `mewcode/app.py`：基于 Textual 的终端 TUI。
- `mewcode/remote.py`：WebSocket/HTTP 远程模式服务，内置浏览器 UI。
- `mewcode/config.py`：YAML 配置加载、合并、校验和 provider 配置。
- `mewcode/conversation.py`：对话历史、消息块和 token 估算。
- `mewcode/context/`：工具结果预算、持久化预览、自动 compact、恢复状态。
- `mewcode/tools/`：内置工具、工具注册表、延迟加载工具搜索。
- `mewcode/permissions/`：权限模式、规则引擎、危险命令检测和路径沙箱。
- `mewcode/mcp/`：MCP 客户端、服务管理和 MCP 工具封装。
- `mewcode/worktree/`：基于 git worktree 的隔离执行环境。
- `mewcode/teams/`：多 Agent 团队、协调者、邮箱和进度管理。
- `mewcode/agents/`：子 Agent 加载、任务管理、trace、内置 agent prompt。
- `mewcode/platform/`：Coding Platform 的领域模型、Control API、PostgreSQL 持久化、Docker ExecutionEnvironment、Runtime 与 Worker。
- `mewcode/commands/`：斜杠命令注册、解析与处理器。
- `mewcode/skills/`：Skill 加载、安装、解析与执行。
- `mewcode/hooks/`：生命周期 hook 的配置、条件、执行器和事件模型。
- `mewcode/memory/`：长期记忆加载、回忆和自动抽取。
- `tests/`：单元测试和集成测试。

## 运行入口

`mewcode/__main__.py` 的 `main()` 是统一入口：

1. 创建 `.mewcode/` 目录并将日志写入 `.mewcode/debug.log`。
2. 解析 CLI 参数：`--mode`、`-p`、`--output-format`、`--remote`。
3. 调用 `load_config()` 加载配置。
4. 加载 hooks，并根据运行模式分发：
   - `-p`：调用 `_run_prompt()`，构建非交互 Agent。
   - `--remote`：创建 `RemoteServer`，监听 `0.0.0.0:18888`。
   - 默认：创建 `MewCodeApp`，使用 Textual TUI 运行。

非交互 `-p` 模式会自动批准权限请求；普通 TUI/远程模式会按权限系统处理。

## 核心 Agent 循环

`mewcode/agent.py` 中的 `Agent` 是主控制器。主循环的大致流程如下：

1. 注入运行环境上下文、长期记忆、项目说明和系统 prompt。
2. 调用 LLM 客户端进行流式生成。
3. 收集文本、thinking、tool call 和 usage 事件。
4. 按工具并发安全性分批执行工具：安全工具可并行，不安全工具顺序执行。
5. 执行前进行权限检查：沙箱、规则引擎、权限模式。
6. 将工具结果写回对话历史。
7. 应用上下文管理：
   - L1：超大工具结果替换为持久化文件引用和预览。
   - L2：接近上下文窗口时调用 LLM 总结历史前缀，保留尾部对话。
8. 每隔固定轮次触发自动记忆抽取。
9. 在 session/turn/tool 等阶段触发 hooks。

Agent 对外产出的是 typed event，例如 `StreamText`、`ThinkingText`、`ToolUseEvent`、`ToolResultEvent`、`PermissionRequest`、`CompactNotification`、`UsageEvent`、`LoopComplete`。TUI、远程模式和非交互模式都消费这些事件。

## LLM 客户端

`mewcode/client.py` 定义 `LLMClient` 抽象类和三个实现：

- `AnthropicClient`：使用 Anthropic Messages streaming API，支持 prompt cache、thinking、模型 context window 自动探测。
- `OpenAIClient`：使用 OpenAI Responses API。
- `OpenAICompatClient`：使用 Chat Completions API，适配 vLLM、Ollama、Together、Azure OpenAI 等兼容服务。

使用 `create_client(config)` 按 provider 的 `protocol` 分发：

- `anthropic`
- `openai`
- `openai-compat`

context window 的解析逻辑在 `ProviderConfig.get_context_window()` 和 `resolve_context_window()`：

1. 显式配置 `context_window`。
2. Anthropic 协议 provider 从 `/v1/models/{model}` 尝试获取。
3. 内置模型名映射表。
4. 默认值：Claude 系列 200K，其他模型 128K。

## TUI 与远程模式

TUI 位于 `mewcode/app.py`，基于 Textual。关键组件包括：

- `MewCodeApp`：主应用，管理 provider 选择、权限模式切换、会话、工具块渲染和斜杠命令。
- `ChatInput`：自定义输入框，支持历史、`@file` 自动补全和 slash command 补全。
- `ToolCallBlock`、`SubAgentBlock`、`ToolGroupSummary`：工具调用展示组件。
- `NoAltScreenDriver`：自定义 Textual driver，避免切换到 alternate screen，保留终端滚动历史。

远程模式位于 `mewcode/remote.py`：

- HTTP 根路径返回内置 `INDEX_HTML`。
- WebSocket 路径为 `/ws`。
- 将 Agent 事件桥接为前端 JSON 消息。
- 支持浏览器侧权限确认、取消、斜杠命令和 compact。

## 配置系统

配置由 `mewcode/config.py` 处理，使用 YAML。默认按以下顺序合并：

1. `~/.mewcode/config.yaml`
2. `.mewcode/config.yaml`
3. `.mewcode/config.local.yaml`

后面的配置覆盖或补充前面的配置。`.mewcode/config.local.yaml` 通常用于本机私有配置，不应提交。

主要配置项包括：

- providers
- MCP servers
- hooks
- permission mode
- worktree
- sandbox
- teammate/coordinator mode
- fork mode
- verification agent

配置结构校验在 `mewcode/validator.py` 中。

## 工具系统

`mewcode/tools/__init__.py` 中的 `ToolRegistry` 管理工具注册、启用/禁用、schema 输出和延迟工具发现。

默认注册工具包括：

- `ReadFile`
- `WriteFile`
- `EditFile`
- `Bash`
- `Glob`
- `Grep`

不同模式还会额外注册：

- `ToolSearchTool`：用于发现延迟加载工具 schema。
- `AgentTool`：启动子 Agent。
- `TeamCreateTool` / `TeamDeleteTool`：管理团队任务。
- `LoadSkill`：加载 skill。
- MCP 工具：命名格式为 `mcp__<server>__<tool>`。

工具基类位于 `mewcode/tools/base.py`。新增工具时应提供：

- `name`
- `description`
- `params_model`
- `execute()`
- `is_concurrency_safe`

如果工具会写文件、执行命令或改变外部状态，必须认真设置权限描述和并发安全性。

## 权限与沙箱

权限逻辑在 `mewcode/permissions/`。`PermissionChecker` 的判断顺序是：

1. 路径沙箱或 OS 沙箱限制。
2. 持久化 allow/deny 规则。
3. 当前权限模式。

规则文件分三层：

- 用户级：`~/.mewcode/permissions.yaml`
- 项目级：`.mewcode/permissions.yaml`
- 本地级：`.mewcode/permissions.local.yaml`

权限模式由 `PermissionMode` 定义，常见模式包括：

- `default`
- `acceptEdits`
- `plan`
- `bypassPermissions`

TUI 中可通过快捷键循环权限模式；CLI 可用 `--mode` 覆盖。

## 上下文、记忆与会话

`ConversationManager` 管理消息列表和 token 估算。每次 LLM 调用后会记录 usage anchor，后续只对新增消息做字符估算。

上下文管理在 `mewcode/context/manager.py`：

- 单个大工具结果会持久化到 `.mewcode/sessions/` 下，并在对话中替换为路径引用和截断预览。
- 当估算 token 接近 context window，会调用总结模型压缩历史前缀。
- `RecoveryState` 会记录近期读取过的文件内容，便于 compact 后恢复必要上下文。

记忆系统在 `mewcode/memory/`：

- 支持用户级和项目级记忆。
- `MemoryManager` 负责加载和抽取。
- Agent 默认每 5 轮左右触发一次自动记忆抽取。

## MCP、Skills、Hooks

MCP：

- 配置来源于 `.mewcode/config*.yaml`。
- 支持 stdio 和 HTTP server。
- `MCPManager` 连接服务并将工具注册到 `ToolRegistry`。
- 服务说明会作为系统提醒注入对话。

Skills：

- skill 加载器位于 `mewcode/skills/loader.py`。
- 内置和项目级 skills 会组成 catalog。
- 命中任务时，Agent 可通过 `LoadSkill` 激活对应 skill。

Hooks：

- hook 生命周期包括 `session_start`、`turn_start`、`pre_send`、`post_receive`、`pre_tool_use`、`post_tool_use`、`turn_end`、`session_end`、`shutdown`、`startup`。
- `pre_tool_use` hook 可以拒绝工具调用。
- hook 输出会作为事件或系统提醒进入对话和 UI。

## Worktree、子 Agent 与团队

`mewcode/worktree/` 使用 git worktree 为子 Agent 创建隔离工作区，并可将 `node_modules`、`.venv`、`vendor` 等目录通过配置软链接到工作区。

`mewcode/tools/agent_tool.py` 可启动 in-process 子 Agent，子 Agent 使用 `run_to_completion()` 执行任务，并通过 trace 和 task manager 回传结果。

`mewcode/teams/` 支持多 Agent 团队：

- `TeamManager` 管理团队生命周期。
- coordinator 模式可让一个指定 Agent 分派任务。
- mailbox 负责 agent 间消息传递。
- TUI 通过 `TeammateTree` 展示队友状态和进度。

## 开发约定

- 保持文件顶部已有的来源/署名注释，不要因为无关改动删除。
- 本项目真实技术栈是 Python，不要被 `MEWCODE.md` 中可能过期的 Go 技术栈描述误导。
- 优先遵循现有模块边界，不要把 TUI、Agent、工具、权限、配置逻辑混在一起。
- 新功能应尽量配套测试，尤其是权限、上下文压缩、工具调用、配置合并、MCP 和团队相关逻辑。
- 修改工具参数时，同时检查对应 Pydantic model、schema 输出和 OpenAI/Anthropic 序列化兼容性。
- 修改事件 dataclass 时，要同步检查 TUI、远程模式、非交互 `stream-json` 输出和测试。
- 修改配置结构时，要同步更新 `validator.py`、默认值、文档和相关测试。
- 文件编码应使用 UTF-8。当前部分源码注释存在历史编码损坏，除非任务明确要求，不要在无关改动中大面积重写。

## 常见风险点

- `-p` 非交互模式和 TUI/远程模式初始化路径不同，改一个模式时要确认另一个模式是否也需要同步。
- `ToolRegistry` 对 Anthropic 和 OpenAI 协议输出的 schema 格式不同，改工具 schema 时要两边都看。
- context compact 会改写对话历史，涉及 token 估算、工具结果持久化和恢复状态，改动后必须跑相关测试。
- 权限系统同时受规则文件、权限模式、路径沙箱和 hook 影响，不能只测 happy path。
- MCP 工具名包含 server 前缀，涉及注册、展示和调用时要保持一致。
- worktree 和 team 功能会创建临时工作区或后台任务，测试时注意清理和异步任务状态。

## 推荐阅读顺序

首次接手任务时，建议按下面顺序阅读：

1. `pyproject.toml`
2. `mewcode/__main__.py`
3. `mewcode/agent.py`
4. `mewcode/client.py`
5. 与任务相关的子目录，例如 `tools/`、`permissions/`、`context/`、`app.py`、`remote.py`
6. 对应的 `tests/test_*.py`

如果只做局部修复，优先阅读目标文件、直接调用方、测试文件和配置校验逻辑。
