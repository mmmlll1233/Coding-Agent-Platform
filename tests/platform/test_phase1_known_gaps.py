from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
from pydantic import BaseModel

from mewcode.agent import Agent, ToolResultEvent
from mewcode.client import LLMClient
from mewcode.conversation import ConversationManager
from mewcode.permissions.checker import Decision
from mewcode.tools import ToolRegistry
from mewcode.tools.base import StreamEnd, StreamEvent, Tool, ToolCallComplete, ToolResult
from mewcode.tools.bash import Bash, Params as BashParams


class ScriptedClient(LLMClient):
    def __init__(self, responses: list[list[StreamEvent]]) -> None:
        self.responses = responses
        self.index = 0

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        events = self.responses[self.index]
        self.index += 1
        for event in events:
            yield event


class RecordingParams(BaseModel):
    value: int


class RecordingWriteTool(Tool):
    name = "RecordingWrite"
    description = "Record a test mutation."
    params_model = RecordingParams
    category = "write"
    is_concurrency_safe = True

    def __init__(self, executed: list[int]) -> None:
        self.executed = executed

    async def execute(self, params: RecordingParams) -> ToolResult:
        self.executed.append(params.value)
        return ToolResult(output="executed")


class AlwaysDeny:
    def check(self, tool: Tool, arguments: dict[str, Any]) -> Decision:
        return Decision(effect="deny", reason="Phase 1 policy-gate test")


@pytest.mark.runtime_contract
@pytest.mark.asyncio
async def test_parallel_tool_batch_uses_the_same_policy_gate(tmp_path: Path) -> None:
    executed: list[int] = []
    registry = ToolRegistry()
    registry.register(RecordingWriteTool(executed))
    client = ScriptedClient(
        [
            [
                ToolCallComplete("tool-1", "RecordingWrite", {"value": 1}),
                ToolCallComplete("tool-2", "RecordingWrite", {"value": 2}),
                StreamEnd("end_turn", 1, 1),
            ],
            [StreamEnd("end_turn", 1, 1)],
        ]
    )
    agent = Agent(
        client,
        registry,
        "anthropic",
        work_dir=str(tmp_path),
        permission_checker=AlwaysDeny(),  # type: ignore[arg-type]
    )
    conversation = ConversationManager()
    conversation.add_user_message("run both")
    results = [
        event
        async for event in agent.run(conversation)
        if isinstance(event, ToolResultEvent)
    ]

    assert executed == []
    assert len(results) == 2
    assert all(result.is_error for result in results)


@pytest.mark.runtime_contract
@pytest.mark.asyncio
async def test_bash_nonzero_exit_is_a_structured_error(tmp_path: Path) -> None:
    tool = Bash()
    tool.work_dir = str(tmp_path)
    command = f'"{sys.executable}" -c "import sys; sys.exit(7)"'

    result = await tool.execute(BashParams(command=command, timeout=5))

    assert result.is_error is True
    assert "Exit code 7" in result.output
    assert result.command_result is not None
    assert result.command_result.exit_code == 7
    assert result.command_result.timed_out is False


@pytest.mark.runtime_contract
@pytest.mark.asyncio
async def test_bash_keeps_stdout_and_stderr_separate(tmp_path: Path) -> None:
    tool = Bash()
    tool.work_dir = str(tmp_path)
    command = (
        f'"{sys.executable}" -c "import sys; '
        "print('out'); print('err', file=sys.stderr)\""
    )

    result = await tool.execute(BashParams(command=command, timeout=5))

    assert result.is_error is False
    assert result.command_result is not None
    assert result.command_result.stdout.strip() == "out"
    assert result.command_result.stderr.strip() == "err"


@pytest.mark.runtime_contract
@pytest.mark.asyncio
async def test_bash_timeout_kills_descendant_processes(tmp_path: Path) -> None:
    marker = tmp_path / "descendant-survived.txt"
    child = tmp_path / "child.py"
    parent = tmp_path / "parent.py"
    child.write_text(
        "import pathlib, time\n"
        "time.sleep(1.2)\n"
        f"pathlib.Path({str(marker)!r}).write_text('survived', encoding='utf-8')\n",
        encoding="utf-8",
    )
    parent.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(child)!r}])\n"
        "time.sleep(2.5)\n",
        encoding="utf-8",
    )
    tool = Bash()
    tool.work_dir = str(tmp_path)

    result = await tool.execute(
        BashParams(command=f'"{sys.executable}" "{parent}"', timeout=1)
    )
    await asyncio.sleep(1.0)
    descendant_survived = marker.exists()
    await asyncio.sleep(1.7)

    assert result.is_error is True
    assert descendant_survived is False
    assert result.command_result is not None
    assert result.command_result.timed_out is True


@pytest.mark.runtime_contract
@pytest.mark.asyncio
async def test_bash_cancellation_kills_descendant_processes(tmp_path: Path) -> None:
    marker = tmp_path / "cancel-descendant-survived.txt"
    child = tmp_path / "cancel-child.py"
    parent = tmp_path / "cancel-parent.py"
    child.write_text(
        "import pathlib, time\n"
        "time.sleep(1.2)\n"
        f"pathlib.Path({str(marker)!r}).write_text('survived', encoding='utf-8')\n",
        encoding="utf-8",
    )
    parent.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(child)!r}])\n"
        "time.sleep(5)\n",
        encoding="utf-8",
    )
    tool = Bash()
    tool.work_dir = str(tmp_path)
    task = asyncio.create_task(
        tool.execute(
            BashParams(command=f'"{sys.executable}" "{parent}"', timeout=10)
        )
    )
    await asyncio.sleep(0.5)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(1.2)
    assert marker.exists() is False


class RecordingHookEngine:
    def __init__(self) -> None:
        self.events: list[str] = []

    def get_prompt_messages(self):
        return None

    async def run_hooks(self, event: str, context) -> None:
        self.events.append(event)

    def drain_notifications(self):
        return []


@pytest.mark.runtime_contract
@pytest.mark.asyncio
async def test_run_to_completion_has_symmetric_session_lifecycle(tmp_path: Path) -> None:
    hooks = RecordingHookEngine()
    client = ScriptedClient([[StreamEnd("end_turn", 1, 1)]])
    agent = Agent(
        client,
        ToolRegistry(),
        "anthropic",
        work_dir=str(tmp_path),
        hook_engine=hooks,  # type: ignore[arg-type]
    )

    await agent.run_to_completion("finish")

    assert hooks.events[0] == "session_start"
    assert hooks.events[-1] == "session_end"


class RaisingClient(LLMClient):
    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        if False:
            yield StreamEnd("end_turn")
        raise RuntimeError("provider failed")


@pytest.mark.runtime_contract
@pytest.mark.asyncio
async def test_agent_lifecycle_is_symmetric_on_exception(tmp_path: Path) -> None:
    hooks = RecordingHookEngine()
    agent = Agent(
        RaisingClient(),
        ToolRegistry(),
        "anthropic",
        work_dir=str(tmp_path),
        hook_engine=hooks,  # type: ignore[arg-type]
    )
    conversation = ConversationManager()
    conversation.add_user_message("fail")

    with pytest.raises(RuntimeError, match="provider failed"):
        _ = [event async for event in agent.run(conversation)]

    assert hooks.events[:2] == ["session_start", "turn_start"]
    assert hooks.events[-2:] == ["turn_end", "session_end"]
    assert hooks.events.count("turn_end") == 1
    assert hooks.events.count("session_end") == 1
