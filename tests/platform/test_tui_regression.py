from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from mewcode.agent import (
    LoopComplete,
    PermissionRequest,
    PermissionResponse,
    StreamText,
    ToolResultEvent,
    ToolUseEvent,
    TurnComplete,
)
from mewcode.app import ChatInput, MewCodeApp, ToolCallBlock
from mewcode.config import ProviderConfig


def _provider() -> ProviderConfig:
    return ProviderConfig(
        name="test",
        protocol="anthropic",
        base_url="https://provider.invalid",
        model="test-model",
        api_key="fake-test-key",
    )


class FakeTuiAgent:
    plan_mode = False
    total_input_tokens = 0
    total_output_tokens = 0
    memory_recall_task = None
    _memory_recall_consumed = False

    def __init__(self) -> None:
        self.permission_response: PermissionResponse | None = None

    async def run(self, conversation):
        yield StreamText("working")
        yield ToolUseEvent("ReadFile", "tool-1", {"file_path": "sample.txt"})
        yield ToolResultEvent("tool-1", "ReadFile", "sample", False, 0.02)
        future = asyncio.get_running_loop().create_future()
        yield PermissionRequest("WriteFile", "write sample", future)
        self.permission_response = await future
        yield TurnComplete(1)
        yield StreamText("done")
        yield LoopComplete(2)


@pytest.fixture
def isolated_tui(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        MewCodeApp,
        "_select_provider",
        lambda self, provider: setattr(self, "_selected_provider", provider),
    )


@pytest.mark.asyncio
async def test_tui_mounts_core_widgets(isolated_tui: None) -> None:
    app = MewCodeApp(providers=[_provider()])
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one("#chat-input", ChatInput) is not None
        assert app.query_one("#chat-area") is not None
        assert app.query_one("#status-bar") is not None


@pytest.mark.asyncio
async def test_tui_submission_renders_agent_events_and_permission(
    isolated_tui: None,
) -> None:
    app = MewCodeApp(providers=[_provider()])
    fake_agent = FakeTuiAgent()
    approvals: list[str] = []

    async def approve(request: PermissionRequest) -> None:
        approvals.append(request.tool_name)
        request.future.set_result(PermissionResponse.ALLOW)

    async with app.run_test() as pilot:
        app.agent = fake_agent  # type: ignore[assignment]
        app._handle_permission_request = approve  # type: ignore[method-assign]

        await app.on_chat_input_submitted(ChatInput.Submitted("inspect sample"))
        task = app._agent_task
        assert task is not None
        await asyncio.wait_for(task, timeout=2)
        await pilot.pause()

        blocks = list(app.query(ToolCallBlock))
        assert len(blocks) == 1
        assert blocks[0]._loading is False
        assert blocks[0]._full_output == "sample"
        assert approvals == ["WriteFile"]
        assert fake_agent.permission_response == PermissionResponse.ALLOW
        assert app.conversation.history[0].content == "inspect sample"
        assert app._streaming is False


@pytest.mark.asyncio
async def test_tui_cancel_stops_active_agent_task(isolated_tui: None) -> None:
    app = MewCodeApp(providers=[_provider()])
    async with app.run_test():
        task = asyncio.create_task(asyncio.Event().wait())
        app._agent_task = task

        app.action_cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()
