from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".tox", ".mypy_cache"}

MAX_OUTPUT_CHARS = 10000

ToolCategory = Literal["read", "write", "command"]


@dataclass(frozen=True)
class CommandExecutionResult:
    """Machine-readable outcome for command-backed tools.

    ``exit_code`` is ``None`` only when the command could not be started.  A
    timed-out command is reaped before this object is created, so its observed
    return code is retained when the operating system provides one.
    """

    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass
class ToolResult:
    output: str
    is_error: bool = False
    command_result: CommandExecutionResult | None = None
    # Platform-only fatal failures terminate the Agent loop after the tool
    # result has been projected. Local tools leave these fields unset.
    fatal_error_code: str | None = None
    fatal_error_message: str | None = None
    # ReadFile can attach the unnumbered content for RecoveryState without
    # forcing the Agent process to reopen an isolated workspace path.
    recovery_path: str | None = None
    recovery_content: str | None = None


class Tool(ABC):
    name: str
    description: str
    params_model: type[BaseModel]
    category: ToolCategory = "read"
    is_concurrency_safe: bool = False
    is_system_tool: bool = False
    should_defer: bool = False

    @property
    def is_read_only(self) -> bool:
        return self.category == "read"


    def get_schema(self) -> dict[str, Any]:
        schema = self.params_model.model_json_schema()
        schema.pop("title", None)
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": schema,
        }

    @abstractmethod
    async def execute(self, params: BaseModel) -> ToolResult: ...


# --- 流式事件 ---


@dataclass
class TextDelta:
    text: str


@dataclass
class ToolCallStart:
    tool_name: str
    tool_id: str


@dataclass
class ToolCallDelta:
    text: str


@dataclass
class ToolCallComplete:
    tool_id: str
    tool_name: str
    arguments: dict[str, Any]


@dataclass
class ThinkingDelta:
    text: str


@dataclass
class ThinkingComplete:
    thinking: str
    signature: str


@dataclass
class StreamEnd:
    stop_reason: str
    input_tokens: int = 0
    output_tokens: int = 0
    # API 返回的 prompt cache 用量。Anthropic 把缓存前缀 token 分为
    # "read"（cache 命中，按 10% 计费）和 "creation"（cache 写入）。
    # input_tokens 已排除这两部分，因此实际 prompt 大小 =
    # input + cache_read + cache_creation。OpenAI 系列只暴露
    # cache_read（通过 *_tokens_details.cached_tokens），没有 creation
    # 计数，所以 cache_creation 在那边始终为 0。
    cache_read: int = 0
    cache_creation: int = 0


StreamEvent = TextDelta | ThinkingDelta | ThinkingComplete | ToolCallStart | ToolCallDelta | ToolCallComplete | StreamEnd
