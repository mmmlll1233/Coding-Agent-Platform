from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from pydantic import ValidationError

from mewcode.client import LLMClient
from mewcode.context import (
    CompactBoundary,
    CompactCircuitBreaker,
    CompactEvent,
    ContentReplacementRecord,
    ContentReplacementState,
    RecoveryState,
    append_replacement_records,
    apply_tool_result_budget,
    auto_compact,
    create_replacement_state,
    ensure_session_dir,
    load_replacement_records,
    reconstruct_replacement_state,
)
from mewcode.conversation import ConversationManager, ToolResultBlock, ToolUseBlock
from mewcode.conversation import ThinkingBlock as ConvThinkingBlock
from mewcode.memory.auto_memory import MemoryManager
from mewcode.permissions import (
    Decision,
    PermissionChecker,
    PermissionMode,
)
from mewcode.hooks import HookContext, HookEngine, ToolRejectedError
from mewcode.hooks.engine import HookNotification
from mewcode.prompts import build_environment_context, build_plan_mode_reminder, build_system_prompt
from mewcode.tools import ToolRegistry
from mewcode.tools.base import (
    CommandExecutionResult,
    MAX_OUTPUT_CHARS,
    StreamEnd,
    StreamEvent,
    TextDelta,
    ThinkingComplete,
    ThinkingDelta,
    Tool,
    ToolCallComplete,
    ToolCallDelta,
    ToolCallStart,
    ToolResult,
)

log = logging.getLogger(__name__)

MEMORY_EXTRACTION_INTERVAL = 5
MAX_TOKENS_CEILING = 64000
MAX_OUTPUT_TOKENS_RECOVERIES = 3


# ---------------------------------------------------------------------------
# AgentEvent 事件类型
# ---------------------------------------------------------------------------

@dataclass
class StreamText:
    text: str


@dataclass
class ThinkingText:
    text: str


@dataclass
class RetryEvent:
    reason: str
    wait: float = 0.0


@dataclass
class ToolUseEvent:
    tool_name: str
    tool_id: str
    arguments: dict[str, Any]


@dataclass
class ToolResultEvent:
    tool_id: str
    tool_name: str
    output: str
    is_error: bool
    elapsed: float
    command_result: CommandExecutionResult | None = None


@dataclass
class TurnComplete:
    turn: int


@dataclass
class LoopComplete:
    total_turns: int


@dataclass
class UsageEvent:
    input_tokens: int
    output_tokens: int


@dataclass
class ErrorEvent:
    message: str
    code: str = "RUNTIME_ERROR"
    terminal: bool = False


@dataclass
class CompactNotification:
    before_tokens: int
    message: str
    # 结构化 boundary（摘要 + 原文保留尾部），UI/session 层用它持久化 compact_boundary 记录。
    # 失败路径下为 None。
    boundary: "CompactBoundary | None" = None


@dataclass
class HookEvent:
    hook_id: str
    event: str
    output: str
    success: bool


@dataclass
class _TurnStarted:
    """Internal lifecycle marker consumed by :meth:`Agent.run`."""

    turn: int


class PermissionResponse(Enum):
    ALLOW = "allow"
    DENY = "deny"
    ALLOW_ALWAYS = "allow_always"


@dataclass
class PermissionRequest:
    tool_name: str
    description: str
    future: asyncio.Future[PermissionResponse]


AgentEvent = (
    StreamText
    | ThinkingText
    | RetryEvent
    | ToolUseEvent
    | ToolResultEvent
    | TurnComplete
    | LoopComplete
    | UsageEvent
    | ErrorEvent
    | PermissionRequest
    | CompactNotification
    | HookEvent
)


# ---------------------------------------------------------------------------
# LLM 响应收集器
# ---------------------------------------------------------------------------

@dataclass
class ThinkingBlock:
    thinking: str
    signature: str


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCallComplete] = field(default_factory=list)
    thinking_blocks: list[ThinkingBlock] = field(default_factory=list)
    stop_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_creation: int = 0


class StreamCollector:
    def __init__(self) -> None:
        self.response = LLMResponse()

    async def consume(
        self, stream: AsyncIterator[StreamEvent]
    ) -> AsyncIterator[AgentEvent]:
        async for event in stream:
            if isinstance(event, TextDelta):
                self.response.text += event.text
                yield StreamText(text=event.text)
            elif isinstance(event, ThinkingDelta):
                yield ThinkingText(text=event.text)
            elif isinstance(event, ThinkingComplete):
                self.response.thinking_blocks.append(
                    ThinkingBlock(thinking=event.thinking, signature=event.signature)
                )
            elif isinstance(event, ToolCallStart):
                pass
            elif isinstance(event, ToolCallDelta):
                pass
            elif isinstance(event, ToolCallComplete):
                self.response.tool_calls.append(event)
                yield ToolUseEvent(
                    tool_name=event.tool_name,
                    tool_id=event.tool_id,
                    arguments=event.arguments,
                )
            elif isinstance(event, StreamEnd):
                self.response.stop_reason = event.stop_reason
                self.response.input_tokens = event.input_tokens
                self.response.output_tokens = event.output_tokens
                self.response.cache_read = event.cache_read
                self.response.cache_creation = event.cache_creation


# ---------------------------------------------------------------------------
# tool 批量执行
# ---------------------------------------------------------------------------

@dataclass
class ToolBatch:
    concurrent: bool
    calls: list[ToolCallComplete]


def partition_tool_calls(
    tool_calls: list[ToolCallComplete],
    registry: ToolRegistry,
) -> list[ToolBatch]:
    batches: list[ToolBatch] = []
    for tc in tool_calls:
        tool = registry.get(tc.tool_name)
        safe = tool is not None and tool.is_concurrency_safe and registry.is_enabled(tc.tool_name)

        if safe and batches and batches[-1].concurrent:
            batches[-1].calls.append(tc)
        else:
            batches.append(ToolBatch(concurrent=safe, calls=[tc]))
    return batches


# ---------------------------------------------------------------------------
# streaming 执行器 — 在 LLM streaming 期间启动 tool 执行
# ---------------------------------------------------------------------------

@dataclass
class _ToolExecResult:
    tool_id: str
    tool_name: str
    result: ToolResult
    elapsed: float
    is_unknown: bool


@dataclass
class _PreparedToolCall:
    call: ToolCallComplete
    tool: Tool
    started_at: float


class StreamingExecutor:
    def __init__(self) -> None:
        self._tasks: list[tuple[int, asyncio.Task[_ToolExecResult]]] = []
        self._order = 0

    def submit(
        self,
        coro: Any,
    ) -> None:
        task = asyncio.create_task(coro)
        self._tasks.append((self._order, task))
        self._order += 1

    async def collect_results(self) -> list[_ToolExecResult]:
        if not self._tasks:
            return []
        tasks = [t for _, t in sorted(self._tasks, key=lambda x: x[0])]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out: list[_ToolExecResult] = []
        for r in results:
            if isinstance(r, Exception):
                out.append(_ToolExecResult(
                    tool_id="",
                    tool_name="",
                    result=ToolResult(output=f"Tool execution error: {r}", is_error=True),
                    elapsed=0.0,
                    is_unknown=False,
                ))
            else:
                out.append(r)
        return out


# ---------------------------------------------------------------------------
# Agent 主循环
# ---------------------------------------------------------------------------

class Agent:
    def __init__(
        self,
        client: LLMClient,
        registry: ToolRegistry,
        protocol: str,
        work_dir: str = ".",
        max_iterations: int = 0,
        permission_checker: PermissionChecker | None = None,
        context_window: int = 200_000,
        instructions_content: str = "",
        memory_manager: MemoryManager | None = None,
        hook_engine: HookEngine | None = None,
        trusted_system_instructions: str = "",
        repository_guidance: str = "",
        session_dir: str | Path | None = None,
        runtime_environment_info: Any | None = None,
        result_redactor: Callable[[str], str] | None = None,
    ) -> None:
        self.client = client
        self.registry = registry
        self.protocol = protocol
        self.work_dir = work_dir
        self.max_iterations = max_iterations
        self.permission_checker = permission_checker
        self.permission_mode: PermissionMode = (
            getattr(permission_checker, "mode", PermissionMode.DEFAULT)
            if permission_checker
            else PermissionMode.DEFAULT
        )
        self.context_window = context_window
        if session_dir is None:
            self.session_dir = ensure_session_dir(work_dir)
        else:
            self.session_dir = Path(session_dir)
            self.session_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_environment_info = runtime_environment_info
        self.result_redactor = result_redactor
        self.compact_breaker = CompactCircuitBreaker()
        self.replacement_state: ContentReplacementState = create_replacement_state()
        # 保存重建工作上下文所需的快照，在 Layer 2 压缩对话后使用：
        # 最近的文件读取和 skill 调用。每次 ReadFile / skill 调用时记录，
        # auto_compact 触发阈值时消费。
        self.recovery_state: RecoveryState = RecoveryState()
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.instructions_content = instructions_content
        self.memory_manager = memory_manager
        self.hook_engine = hook_engine
        self.trusted_system_instructions = trusted_system_instructions
        self.repository_guidance = repository_guidance
        self._loop_count = 0
        # 记忆提取合并策略（对齐 Go 版 inProgress + pendingContext）：
        # _extracting: 标记是否有提取正在进行
        # _pending_extraction: 提取期间又触发了新请求，标记需要尾随提取
        self._extracting = False
        self._pending_extraction = False
        self.session_id: str = ""
        self.active_skills: dict[str, str] = {}
        self._skill_catalog: str = ""
        self._agent_catalog: str = ""
        self._agent_catalog_list: list[tuple[str, str]] = []
        self.agent_id: str = uuid.uuid4().hex[:12]
        self.parent_id: str | None = None
        self.trace_id: str | None = None
        self.coordinator_mode: bool = False
        self.team_name: str = ""
        self._team_manager: Any = None
        self.notification_fn: Callable[[], list[str]] | None = None
        self.file_history: Any = None

        # 非阻塞 memory recall：prefetch task 与主 LLM 调用并行，工具执行后注入
        self.memory_recall_task: Any | None = None
        self._memory_recall_consumed: bool = False

    @property
    def _transcript_path(self) -> str:
        if self.session_id:
            return str(Path(self.work_dir) / ".mewcode" / "sessions" / f"{self.session_id}.jsonl")
        return ""

    @property
    def plan_mode(self) -> bool:
        return self.permission_mode == PermissionMode.PLAN

    _plan_path_cache: Path | None = None

    def _get_plan_path(self) -> Path:
        if self._plan_path_cache is not None:
            return self._plan_path_cache
        import random
        import datetime
        _ADJECTIVES = ["bold", "bright", "calm", "cool", "deep", "fair", "fast", "fine",
                       "glad", "keen", "kind", "lean", "mild", "neat", "pure", "safe",
                       "slim", "soft", "tall", "warm", "wise", "grand", "swift", "vivid"]
        _NOUNS = ["sketch", "draft", "spark", "bloom", "trail", "ridge", "creek", "grove",
                  "cliff", "cloud", "field", "forge", "frost", "haven", "pearl", "stone",
                  "storm", "river", "tower", "delta", "flame", "orbit", "pulse", "shore"]
        plans_dir = Path(self.work_dir) / ".mewcode" / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%m%d-%H%M")
        slug = f"{random.choice(_ADJECTIVES)}-{random.choice(_NOUNS)}-{ts}"
        self._plan_path_cache = plans_dir / f"{slug}.md"
        return self._plan_path_cache

    def set_permission_mode(self, mode: PermissionMode) -> None:
        self.permission_mode = mode
        if self.permission_checker:
            self.permission_checker.mode = mode

    def activate_skill(self, name: str, prompt_body: str) -> None:
        self.active_skills[name] = prompt_body

    def clear_active_skills(self) -> None:
        self.active_skills.clear()

    def set_skill_catalog(self, catalog: str) -> None:
        self._skill_catalog = catalog


    def set_agent_catalog(self, catalog: str, catalog_list: list[tuple[str, str]] | None = None) -> None:
        self._agent_catalog = catalog
        if catalog_list is not None:
            self._agent_catalog_list = catalog_list

    def _build_hook_context(self, event: str, **kwargs: str | dict) -> HookContext:
        return HookContext(
            event_name=event,
            tool_name=str(kwargs.get("tool_name", "")),
            tool_args=kwargs.get("tool_args", {}),
            file_path=str(kwargs.get("file_path", "")),
            message=str(kwargs.get("message", "")),
            error=str(kwargs.get("error", "")),
        )

    def _infer_file_path(self, args: dict) -> str:
        return str(args.get("file_path", args.get("path", "")))

    def _drain_hook_events(self) -> list[HookEvent]:
        if not self.hook_engine:
            return []
        return [
            HookEvent(
                hook_id=n.hook_id,
                event=n.event,
                output=n.output,
                success=n.success,
            )
            for n in self.hook_engine.drain_notifications()
        ]

    async def run(self, conversation: ConversationManager) -> AsyncIterator[AgentEvent]:
        """Run the canonical Agent loop with symmetric lifecycle hooks.

        Every public execution path, including ``run_to_completion`` and the
        platform ``JobRunner``, consumes this generator.  The internal turn
        marker lets lifecycle hooks wrap retries, failures, cancellation, and
        early generator close without duplicating the model/tool loop.
        """
        session_open = False
        turn_open = False
        session_closed = False

        async def end_turn() -> list[HookEvent]:
            nonlocal turn_open
            if not turn_open or not self.hook_engine:
                turn_open = False
                return []
            ctx = self._build_hook_context("turn_end")
            await self.hook_engine.run_hooks("turn_end", ctx)
            turn_open = False
            return self._drain_hook_events()

        async def end_session() -> list[HookEvent]:
            nonlocal session_closed
            if session_closed or not session_open or not self.hook_engine:
                session_closed = True
                return []
            ctx = self._build_hook_context("session_end")
            await self.hook_engine.run_hooks("session_end", ctx)
            session_closed = True
            return self._drain_hook_events()

        try:
            if self.hook_engine:
                session_open = True
                ctx = self._build_hook_context("session_start")
                await self.hook_engine.run_hooks("session_start", ctx)
                for he in self._drain_hook_events():
                    yield he
            async for event in self._run_loop(conversation):
                if isinstance(event, _TurnStarted):
                    for he in await end_turn():
                        yield he
                    turn_open = True
                    if self.hook_engine:
                        ctx = self._build_hook_context("turn_start")
                        await self.hook_engine.run_hooks("turn_start", ctx)
                        for he in self._drain_hook_events():
                            yield he
                    continue

                if isinstance(event, TurnComplete):
                    for he in await end_turn():
                        yield he
                elif isinstance(event, LoopComplete):
                    for he in await end_turn():
                        yield he
                    for he in await end_session():
                        yield he
                yield event
        finally:
            # Hook notifications cannot be yielded while an async generator is
            # being closed, but the hooks themselves must always run.
            await end_turn()
            await end_session()

    async def _run_loop(
        self, conversation: ConversationManager
    ) -> AsyncIterator[AgentEvent | _TurnStarted]:
        self._current_conversation = conversation
        env_context = build_environment_context(
            self.work_dir,
            self.active_skills,
            self._skill_catalog,
            self._agent_catalog,
            runtime_environment_info=self.runtime_environment_info,
        )
        conversation.inject_environment(env_context)

        memory_content = self.memory_manager.load() if self.memory_manager else ""
        conversation.inject_long_term_memory(self.instructions_content, memory_content)
        if self.repository_guidance:
            conversation.inject_repository_guidance(self.repository_guidance)

        iteration = 0
        consecutive_unknown = 0
        max_tokens_escalated = False
        output_recoveries = 0

        while True:
            iteration += 1

            if self.max_iterations > 0 and iteration > self.max_iterations:
                yield ErrorEvent(
                    message=f"Agent reached maximum iterations ({self.max_iterations})",
                    code="MAX_ITERATIONS",
                    terminal=True,
                )
                break

            yield _TurnStarted(turn=iteration)

            self._consume_mailbox(conversation)
            if self.notification_fn:
                for note in self.notification_fn():
                    conversation.add_system_reminder(note)

            if self.hook_engine:
                ctx = self._build_hook_context("pre_send")
                await self.hook_engine.run_hooks("pre_send", ctx)
                for he in self._drain_hook_events():
                    yield he

            hook_prompts = (
                self.hook_engine.get_prompt_messages() if self.hook_engine else None
            )
            system = build_system_prompt(
                hook_prompts=hook_prompts,
                coordinator_mode=self.coordinator_mode,
                agent_catalog=self._agent_catalog_list or None,
                custom_instructions=self.trusted_system_instructions,
                work_dir=self.work_dir,
            )

            if self.plan_mode:
                plan_path = str(self._get_plan_path())
                if self.permission_checker:
                    self.permission_checker.plan_file_path = plan_path
                plan_exists = self._get_plan_path().exists()
                plan_reminder = build_plan_mode_reminder(
                    plan_path, plan_exists, iteration
                )
                conversation.add_system_reminder(plan_reminder)

            if self.hook_engine:
                for note in self.hook_engine.drain_notifications():
                    conversation.add_system_reminder(
                        f"Hook [{note.hook_id}] {note.event}: {note.output}"
                    )

            deferred_names = self.registry.get_deferred_tool_names()
            if deferred_names:
                conversation.add_system_reminder(
                    "The following deferred tools are available via ToolSearch. "
                    "Their schemas are NOT loaded - use ToolSearch with "
                    'query "select:<name>[,<name>...]" to load tool schemas before calling them:\n'
                    + "\n".join(deferred_names)
                )

            tools = self.registry.get_all_schemas(self.protocol)

            # Layer 1: apply tool-result budget（就地修改 conversation）
            new_records = apply_tool_result_budget(
                conversation, self.session_dir, self.replacement_state
            )
            if new_records:
                append_replacement_records(self.session_dir, new_records)

            # Layer 2: 接近 context window 上限时自动 compact
            # tool-result budget 已就地修改 conversation，直接用 conversation.history 估算
            compact_result = await auto_compact(
                conversation,
                self.client,
                self.context_window,
                self.session_dir,
                protocol=self.protocol,
                breaker=self.compact_breaker,
                recovery=self.recovery_state,
                tool_schemas=self.registry.get_all_schemas(self.protocol),
                transcript_path=self._transcript_path,
            )
            if isinstance(compact_result, CompactEvent):
                yield CompactNotification(
                    before_tokens=compact_result.before_tokens,
                    message=f"上下文已压缩（压缩前 {compact_result.before_tokens:,} tokens）",
                    boundary=compact_result.boundary,
                )
                conversation.inject_environment(env_context)
                mem = self.memory_manager.load() if self.memory_manager else ""
                conversation.inject_long_term_memory(
                    self.instructions_content, mem
                )
                if self.repository_guidance:
                    conversation.inject_repository_guidance(
                        self.repository_guidance
                    )
                # 压缩后重新应用 budget（就地修改）
                apply_tool_result_budget(
                    conversation, self.session_dir, self.replacement_state
                )
            elif isinstance(compact_result, str):
                yield ErrorEvent(message=compact_result)

            collector = StreamCollector()
            llm_stream = self.client.stream(conversation, system=system, tools=tools)
            async for event in collector.consume(llm_stream):
                yield event

            response = collector.response

            if self.hook_engine:
                ctx = self._build_hook_context("post_receive", message=response.text)
                await self.hook_engine.run_hooks("post_receive", ctx)
                for he in self._drain_hook_events():
                    yield he

            self.total_input_tokens += response.input_tokens
            self.total_output_tokens += response.output_tokens
            yield UsageEvent(
                input_tokens=self.total_input_tokens,
                output_tokens=self.total_output_tokens,
            )

            conv_thinking = [
                ConvThinkingBlock(thinking=tb.thinking, signature=tb.signature)
                for tb in response.thinking_blocks
            ]

            if response.stop_reason == "max_tokens":
                if not max_tokens_escalated:
                    self.client.set_max_output_tokens(MAX_TOKENS_CEILING)
                    max_tokens_escalated = True
                    if response.text:
                        conversation.add_assistant_message(
                            response.text, thinking_blocks=conv_thinking
                        )
                        conversation.add_user_message(
                            "Output token limit hit. Resume directly from where you stopped. "
                            "Do not apologize or repeat previous content. Pick up mid-thought if needed."
                        )
                    yield RetryEvent(reason="max_tokens escalation")
                    continue
                elif output_recoveries < MAX_OUTPUT_TOKENS_RECOVERIES:
                    output_recoveries += 1
                    conversation.add_assistant_message(
                        response.text, thinking_blocks=conv_thinking
                    )
                    conversation.add_user_message(
                        "Output token limit hit. Resume directly from where you stopped. "
                        "Break remaining work into smaller pieces."
                    )
                    yield RetryEvent(
                        reason=f"max_tokens recovery {output_recoveries}/{MAX_OUTPUT_TOKENS_RECOVERIES}"
                    )
                    continue
            else:
                output_recoveries = 0

            if not response.tool_calls:
                conversation.add_assistant_message(
                    response.text, thinking_blocks=conv_thinking
                )
                self._loop_count += 1
                if (
                    self._loop_count % MEMORY_EXTRACTION_INTERVAL == 0
                    and self.memory_manager
                ):
                    asyncio.ensure_future(self._extract_memories(conversation))
                if self.file_history is not None:
                    summary = response.text[:60] + "..." if len(response.text) > 60 else response.text
                    self.file_history.make_snapshot(len(conversation.history), summary)
                yield LoopComplete(total_turns=iteration)
                break

            tool_uses = [
                ToolUseBlock(
                    tool_use_id=tc.tool_id,
                    tool_name=tc.tool_name,
                    arguments=tc.arguments,
                )
                for tc in response.tool_calls
            ]
            conversation.add_assistant_message(
                response.text, tool_uses, thinking_blocks=conv_thinking
            )
            # 在 assistant 回复加入历史后锚定实际用量：基线（input + cache + output）
            # 覆盖到当前位置，因此下一轮迭代顶部的 auto-compact 检查只需对
            # 接下来追加的 tool results 做字符估算。
            conversation.record_usage_anchor(
                response.input_tokens,
                response.output_tokens,
                response.cache_read,
                response.cache_creation,
            )

            tool_results: list[ToolResultBlock] = []
            terminal_tool_error: tuple[str, str] | None = None
            batches = partition_tool_calls(response.tool_calls, self.registry)

            for batch in batches:
                # Hooks and permission decisions are deliberately serialized in
                # model-call order.  Only calls that pass this gate are eligible
                # for concurrent execution.
                prepared_entries: list[_PreparedToolCall | _ToolExecResult] = []
                for tc in batch.calls:
                    prepared: _PreparedToolCall | _ToolExecResult | None = None
                    async for item in self._prepare_tool_call(tc):
                        if isinstance(item, (PermissionRequest, HookEvent)):
                            yield item
                        else:
                            prepared = item
                    if prepared is None:
                        prepared = _ToolExecResult(
                            tool_id=tc.tool_id,
                            tool_name=tc.tool_name,
                            result=ToolResult(
                                output="Error: tool policy gate produced no decision",
                                is_error=True,
                            ),
                            elapsed=0.0,
                            is_unknown=False,
                        )
                    prepared_entries.append(prepared)

                authorized = [
                    item
                    for item in prepared_entries
                    if isinstance(item, _PreparedToolCall)
                ]
                if batch.concurrent and len(authorized) > 1:
                    executed = await asyncio.gather(
                        *(self._execute_prepared_tool(item) for item in authorized)
                    )
                else:
                    executed = [
                        await self._execute_prepared_tool(item)
                        for item in authorized
                    ]
                executed_by_id = {result.tool_id: result for result in executed}

                for entry in prepared_entries:
                    was_executed = isinstance(entry, _PreparedToolCall)
                    if was_executed:
                        br = executed_by_id[entry.call.tool_id]
                        for he in await self._run_post_tool_hook(entry.call):
                            yield he
                    else:
                        br = entry

                    if br.is_unknown:
                        consecutive_unknown += 1
                    else:
                        consecutive_unknown = 0

                    content = self._maybe_persist_or_truncate(
                        br.tool_id, br.result.output
                    )
                    tool_results.append(
                        ToolResultBlock(
                            tool_use_id=br.tool_id,
                            content=content,
                            is_error=br.result.is_error,
                        )
                    )
                    yield ToolResultEvent(
                        tool_id=br.tool_id,
                        tool_name=br.tool_name,
                        output=br.result.output,
                        is_error=br.result.is_error,
                        elapsed=br.elapsed,
                        command_result=br.result.command_result,
                    )
                    if (
                        terminal_tool_error is None
                        and br.result.fatal_error_code is not None
                    ):
                        terminal_tool_error = (
                            br.result.fatal_error_code,
                            br.result.fatal_error_message
                            or br.result.output
                            or "Execution environment failed",
                        )

            if consecutive_unknown >= 3:
                yield ErrorEvent(
                    message="Agent terminated: too many consecutive unknown tool calls",
                    code="TOO_MANY_UNKNOWN_TOOLS",
                    terminal=True,
                )
                break

            exit_plan_called = any(
                tc.tool_name == "ExitPlanMode" for tc in response.tool_calls
            )
            conversation.add_tool_results_message(tool_results)

            if terminal_tool_error is not None:
                code, message = terminal_tool_error
                yield ErrorEvent(message=message, code=code, terminal=True)
                break

            # 非阻塞 memory recall：工具执行完后检查 prefetch 是否就绪
            if self.memory_recall_task and not self._memory_recall_consumed:
                if self.memory_recall_task.done():
                    try:
                        recall = self.memory_recall_task.result()
                        if recall:
                            conversation.add_system_reminder(recall)
                    except Exception:
                        pass
                    self._memory_recall_consumed = True

            if exit_plan_called:
                yield TurnComplete(turn=iteration)
                yield LoopComplete(total_turns=iteration)
                break

            yield TurnComplete(turn=iteration)


    def _consume_mailbox(self, conversation: ConversationManager) -> None:
        if not self.team_name or not self._team_manager:
            return
        try:
            mailbox = self._team_manager.get_mailbox(self.team_name)
            if mailbox is None:
                return
            messages = mailbox.consume(self.agent_id)
            for msg in messages:
                prefix = f"[Message from {msg.from_agent}]"
                if msg.message_type != "text":
                    prefix = f"[{msg.message_type} from {msg.from_agent}]"
                content = f"{prefix} {msg.content}"
                conversation.add_user_message(content)
        except Exception as e:
            log.debug("Mailbox consumption failed: %s", e)

    def _build_permission_description(self, tc: ToolCallComplete) -> str:
        """为 HITL 权限确认生成人类可读的操作描述。"""
        return PermissionChecker.describe_tool_action(tc.tool_name, tc.arguments)

    async def _prepare_tool_call(
        self, tc: ToolCallComplete
    ) -> AsyncIterator[
        PermissionRequest | HookEvent | _PreparedToolCall | _ToolExecResult
    ]:
        """Run the serial policy gate and yield one final preparation outcome."""
        tool = self.registry.get(tc.tool_name)
        start = time.monotonic()

        if tool is None:
            yield _ToolExecResult(
                tool_id=tc.tool_id,
                tool_name=tc.tool_name,
                result=ToolResult(output=f"Error: unknown tool '{tc.tool_name}'", is_error=True),
                elapsed=time.monotonic() - start,
                is_unknown=True,
            )
            return

        if not self.registry.is_enabled(tc.tool_name):
            yield _ToolExecResult(
                tool_id=tc.tool_id,
                tool_name=tc.tool_name,
                result=ToolResult(output=f"Error: tool '{tc.tool_name}' is disabled", is_error=True),
                elapsed=time.monotonic() - start,
                is_unknown=False,
            )
            return

        if self.hook_engine:
            file_path = self._infer_file_path(tc.arguments)
            hook_ctx = self._build_hook_context(
                "pre_tool_use",
                tool_name=tc.tool_name,
                tool_args=tc.arguments,
                file_path=file_path,
            )
            rejection = await self.hook_engine.run_pre_tool_hooks(hook_ctx)
            for he in self._drain_hook_events():
                yield he
            if rejection is not None:
                yield _ToolExecResult(
                    tool_id=tc.tool_id,
                    tool_name=tc.tool_name,
                    result=ToolResult(
                        output=f"Hook rejected: {rejection.reason}",
                        is_error=True,
                    ),
                    elapsed=time.monotonic() - start,
                    is_unknown=False,
                )
                return

        if self.permission_checker:
            decision = self.permission_checker.check(tool, tc.arguments)
            if decision.effect == "deny":
                yield _ToolExecResult(
                    tool_id=tc.tool_id,
                    tool_name=tc.tool_name,
                    result=ToolResult(
                        output=f"Permission denied: {decision.reason}",
                        is_error=True,
                    ),
                    elapsed=time.monotonic() - start,
                    is_unknown=False,
                )
                return
            if decision.effect == "ask":
                loop = asyncio.get_running_loop()
                future: asyncio.Future[PermissionResponse] = loop.create_future()
                yield PermissionRequest(
                    tool_name=tc.tool_name,
                    description=self._build_permission_description(tc),
                    future=future,
                )
                response = await future
                if response == PermissionResponse.DENY:
                    yield _ToolExecResult(
                        tool_id=tc.tool_id,
                        tool_name=tc.tool_name,
                        result=ToolResult(
                            output="Permission denied: 用户拒绝了此操作",
                            is_error=True,
                        ),
                        elapsed=time.monotonic() - start,
                        is_unknown=False,
                    )
                    return
                if response == PermissionResponse.ALLOW_ALWAYS:
                    from mewcode.permissions.rules import Rule, extract_content
                    content = extract_content(tc.tool_name, tc.arguments)
                    pattern = f"{content[:60]}*" if len(content) > 60 else f"{content}*"
                    rule = Rule(tool_name=tc.tool_name, pattern=pattern, effect="allow")
                    self.permission_checker.rule_engine.append_local_rule(rule)
                    self.permission_checker.add_session_allow(tc.tool_name, content)

        yield _PreparedToolCall(call=tc, tool=tool, started_at=start)

    async def _execute_prepared_tool(
        self, prepared: _PreparedToolCall
    ) -> _ToolExecResult:
        tc = prepared.call
        try:
            params = prepared.tool.params_model.model_validate(tc.arguments)
            result = await prepared.tool.execute(params)
        except ValidationError as e:
            result = ToolResult(
                output=f"Parameter validation error: {e}", is_error=True
            )
        except Exception as e:
            result = ToolResult(
                output=f"Tool execution error: {e}", is_error=True
            )

        if self.result_redactor is not None:
            result.output = self.result_redactor(result.output)
            if result.fatal_error_message is not None:
                result.fatal_error_message = self.result_redactor(
                    result.fatal_error_message
                )
            if result.recovery_content is not None:
                result.recovery_content = self.result_redactor(
                    result.recovery_content
                )
            if result.command_result is not None:
                command_result = result.command_result
                result.command_result = CommandExecutionResult(
                    exit_code=command_result.exit_code,
                    stdout=self.result_redactor(command_result.stdout),
                    stderr=self.result_redactor(command_result.stderr),
                    timed_out=command_result.timed_out,
                )

        self._snapshot_for_recovery(tc, result)
        return _ToolExecResult(
            tool_id=tc.tool_id,
            tool_name=tc.tool_name,
            result=result,
            elapsed=time.monotonic() - prepared.started_at,
            is_unknown=False,
        )

    async def _run_post_tool_hook(
        self, tc: ToolCallComplete
    ) -> list[HookEvent]:
        if not self.hook_engine:
            return []
        file_path = self._infer_file_path(tc.arguments)
        hook_ctx = self._build_hook_context(
            "post_tool_use",
            tool_name=tc.tool_name,
            tool_args=tc.arguments,
            file_path=file_path,
        )
        await self.hook_engine.run_hooks("post_tool_use", hook_ctx)
        return self._drain_hook_events()

    def _snapshot_for_recovery(
        self, tc: ToolCallComplete, result: ToolResult
    ) -> None:
        """捕获 ReadFile 刚交给模型的内容，以便 Layer 2 压缩对话后
        auto_compact 能重新附加这些数据。每次 ReadFile 多一次磁盘读取，
        比从 tool 输出中反向解析行号要划算。
        """
        if result.is_error or tc.tool_name != "ReadFile":
            return
        if result.recovery_content is not None:
            self.recovery_state.record_file_read(
                result.recovery_path
                or str(tc.arguments.get("file_path", "")),
                result.recovery_content,
            )
            return
        path = tc.arguments.get("file_path") if isinstance(tc.arguments, dict) else None
        if not path:
            return
        file_path = Path(path)
        if not file_path.is_absolute():
            file_path = Path(self.work_dir) / file_path
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError:
            return
        self.recovery_state.record_file_read(str(file_path), content)

    async def _extract_memories(
        self, conversation: ConversationManager
    ) -> None:
        """触发记忆提取，对齐 Go 版 inProgress + pendingContext 合并策略。

        当提取正在进行时，新的触发不会启动并发提取，而是标记 _pending_extraction。
        当前提取完成后检查该标志，如果有 pending 则立即执行一次尾随提取，
        防止多个触发器同时执行导致重复提取。
        """
        if not self.memory_manager:
            return

        # 合并策略：正在提取时暂存新请求，等当前提取完成后尾随执行
        if self._extracting:
            log.debug("[extractMemories] extraction in progress — stashing for trailing run")
            self._pending_extraction = True
            return

        self._extracting = True
        try:
            await self.memory_manager.extract(
                self.client, conversation, self.protocol
            )
        except Exception as e:
            log.debug("Memory extraction failed: %s", e)
        finally:
            self._extracting = False
            # 检查是否有尾随提取请求
            if self._pending_extraction:
                self._pending_extraction = False
                log.debug("[extractMemories] running trailing extraction for stashed context")
                # 递归调用自身处理尾随请求
                await self._extract_memories(conversation)

    async def manual_compact(
        self, conversation: ConversationManager
    ) -> CompactNotification | ErrorEvent:
        # auto_compact 会用摘要替换 conversation.history，所有 tool-result 内容
        # （原始或已替换的）都将被丢弃。这里跳过 apply_tool_result_budget —
        # 它在主循环中的唯一目的是为 LLM 调用生成 api_conv，而本路径不需要
        # 发起看到替换结果的 LLM 调用（auto_compact 内部的摘要调用操作的是原始对话）。
        result = await auto_compact(
            conversation,
            self.client,
            self.context_window,
            self.session_dir,
            protocol=self.protocol,
            manual=True,
            breaker=self.compact_breaker,
            recovery=self.recovery_state,
            tool_schemas=self.registry.get_all_schemas(self.protocol),
            transcript_path=self._transcript_path,
        )
        if isinstance(result, CompactEvent):
            env_context = build_environment_context(
                self.work_dir,
                self.active_skills,
                self._skill_catalog,
                self._agent_catalog,
                runtime_environment_info=self.runtime_environment_info,
            )
            conversation.inject_environment(env_context)
            memory_content = self.memory_manager.load() if self.memory_manager else ""
            conversation.inject_long_term_memory(
                self.instructions_content, memory_content
            )
            if self.repository_guidance:
                conversation.inject_repository_guidance(
                    self.repository_guidance
                )
            return CompactNotification(
                before_tokens=result.before_tokens,
                message=f"上下文已压缩（压缩前 {result.before_tokens:,} tokens）",
                boundary=result.boundary,
            )
        return ErrorEvent(message=result or "压缩失败：对话历史为空或未达到压缩条件")

    async def run_to_completion(
        self, task: str, conversation: ConversationManager | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> str:
        """Compatibility adapter over the canonical typed-event loop."""
        if conversation is None:
            conversation = ConversationManager()

        if task:
            conversation.add_user_message(task)

        log.info(
            "[run_to_completion] agent=%s tools=%d names=%s coordinator=%s",
            self.agent_id,
            len(self.registry.get_all_schemas(self.protocol)),
            [
                t["name"]
                for t in self.registry.get_all_schemas(self.protocol)
            ][:10],
            self.coordinator_mode,
        )

        async for event in self.run(conversation):
            if isinstance(event, PermissionRequest) and not event.future.done():
                response = (
                    PermissionResponse.ALLOW
                    if self.permission_mode == PermissionMode.BYPASS
                    else PermissionResponse.DENY
                )
                event.future.set_result(response)

            if not event_callback:
                continue
            if isinstance(event, UsageEvent):
                event_callback({
                    "type": "usage",
                    "usage": {
                        "inputTokens": event.input_tokens,
                        "outputTokens": event.output_tokens,
                    },
                })
            elif isinstance(event, StreamText):
                event_callback({"type": "stream_text", "text": event.text})
            elif isinstance(event, ToolUseEvent):
                event_callback({
                    "type": "tool_use",
                    "toolName": event.tool_name,
                    "args": event.arguments,
                })

        for message in reversed(conversation.history):
            if message.role == "assistant":
                return message.content
        return ""

    def _maybe_persist_or_truncate(self, tool_use_id: str, text: str) -> str:
        from mewcode.context.manager import (
            SINGLE_RESULT_CHAR_LIMIT,
            make_persisted_preview,
            persist_tool_result,
        )

        if len(text) > SINGLE_RESULT_CHAR_LIMIT:
            fp = persist_tool_result(tool_use_id, text, self.session_dir)
            return make_persisted_preview(text, fp)
        if len(text) > MAX_OUTPUT_CHARS:
            return text[:MAX_OUTPUT_CHARS] + "\n… (output truncated)"
        return text
