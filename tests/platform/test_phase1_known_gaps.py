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


@pytest.mark.phase1_gap
@pytest.mark.xfail(
    strict=True,
    reason="PHASE1-POLICY-GATE: concurrent-safe batches bypass permission and hook gates",
)
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


@pytest.mark.phase1_gap
@pytest.mark.xfail(
    strict=True,
    reason="PHASE1-BASH-RESULT: non-zero exit codes are not represented as tool errors",
)
@pytest.mark.asyncio
async def test_bash_nonzero_exit_is_a_structured_error(tmp_path: Path) -> None:
    tool = Bash()
    tool.work_dir = str(tmp_path)
    command = f'"{sys.executable}" -c "import sys; sys.exit(7)"'

    result = await tool.execute(BashParams(command=command, timeout=5))

    assert result.is_error is True
    assert "Exit code 7" in result.output


@pytest.mark.phase1_gap
@pytest.mark.xfail(
    strict=True,
    reason="PHASE1-CANCEL-TREE: Bash timeout kills only the shell process, not descendants",
)
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


class RecordingHookEngine:
    def __init__(self) -> None:
        self.events: list[str] = []

    def get_prompt_messages(self):
        return None

    async def run_hooks(self, event: str, context) -> None:
        self.events.append(event)

    def drain_notifications(self):
        return []


@pytest.mark.phase1_gap
@pytest.mark.xfail(
    strict=True,
    reason="PHASE1-LIFECYCLE: run_to_completion omits symmetric session lifecycle hooks",
)
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
