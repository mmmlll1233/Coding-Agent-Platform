from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

import mewcode.__main__ as main_module
from mewcode.agent import (
    LoopComplete,
    PermissionRequest,
    PermissionResponse,
    StreamText,
    ToolResultEvent,
    ToolUseEvent,
    TurnComplete,
    UsageEvent,
)
from mewcode.config import AppConfig, ProviderConfig
from mewcode.permissions import PermissionMode


def _config() -> AppConfig:
    return AppConfig(
        providers=[
            ProviderConfig(
                name="test",
                protocol="anthropic",
                base_url="https://provider.invalid",
                model="test-model",
                api_key="fake-test-key",
                context_window=128_000,
            )
        ]
    )


@pytest.fixture
def isolated_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setattr(main_module, "load_config", _config)
    monkeypatch.setattr(main_module, "load_hooks", lambda raw: [])
    return tmp_path


def test_main_dispatches_noninteractive_prompt(
    isolated_cli: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: dict[str, object] = {}

    async def fake_run_prompt(config, mode, hooks, prompt, output_format):
        called.update(
            mode=mode,
            prompt=prompt,
            output_format=output_format,
            hooks=hooks,
        )

    monkeypatch.setattr(main_module, "_run_prompt", fake_run_prompt)
    monkeypatch.setattr(
        sys,
        "argv",
        ["mewcode", "-p", "fix it", "--output-format", "stream-json"],
    )

    main_module.main()

    assert called == {
        "mode": PermissionMode.DEFAULT,
        "prompt": "fix it",
        "output_format": "stream-json",
        "hooks": None,
    }


def test_main_dispatches_remote_server(
    isolated_cli: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mewcode.remote as remote_module

    called: dict[str, object] = {}

    class FakeRemoteServer:
        def __init__(self, **kwargs):
            called["kwargs"] = kwargs

        async def run(self):
            called["ran"] = True

    monkeypatch.setattr(remote_module, "RemoteServer", FakeRemoteServer)
    monkeypatch.setattr(sys, "argv", ["mewcode", "--remote"])

    main_module.main()

    assert called["ran"] is True
    assert called["kwargs"]["hook_engine"] is None


def test_main_dispatches_tui(
    isolated_cli: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mewcode.app as app_module

    called: dict[str, object] = {}

    class FakeApp:
        def __init__(self, **kwargs):
            called["kwargs"] = kwargs

        def run(self):
            called["ran"] = True

    monkeypatch.setattr(app_module, "MewCodeApp", FakeApp)
    monkeypatch.setattr(sys, "argv", ["mewcode"])

    main_module.main()

    assert called["ran"] is True
    assert called["kwargs"]["permission_mode"] == PermissionMode.DEFAULT


class FakePromptAgent:
    last_permission_response: PermissionResponse | None = None

    def __init__(self, *args, **kwargs) -> None:
        self.notification_fn = None

    async def run(self, conversation):
        yield StreamText("hello")
        yield ToolUseEvent("ReadFile", "tool-1", {"file_path": "sample.txt"})
        yield ToolResultEvent("tool-1", "ReadFile", "sample", False, 0.01)
        yield UsageEvent(12, 4)
        future = asyncio.get_running_loop().create_future()
        yield PermissionRequest("WriteFile", "write sample", future)
        self.__class__.last_permission_response = await future
        yield TurnComplete(1)
        yield LoopComplete(2)


@pytest.mark.asyncio
@pytest.mark.parametrize("output_format", ["text", "stream-json"])
async def test_run_prompt_projects_typed_events_and_approves_permissions(
    output_format: str,
    isolated_cli: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import mewcode.agent as agent_module
    import mewcode.agents.loader as loader_module
    import mewcode.client as client_module

    config = _config()
    FakePromptAgent.last_permission_response = None
    monkeypatch.setattr(agent_module, "Agent", FakePromptAgent)
    monkeypatch.setattr(client_module, "create_client", lambda provider: object())

    async def fake_resolve_context_window(provider):
        return None

    monkeypatch.setattr(client_module, "resolve_context_window", fake_resolve_context_window)
    monkeypatch.setattr(loader_module.AgentLoader, "load_all", lambda self: {})

    await main_module._run_prompt(
        config,
        PermissionMode.DEFAULT,
        None,
        "say hello",
        output_format,
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert FakePromptAgent.last_permission_response == PermissionResponse.ALLOW
    if output_format == "text":
        assert captured.out == "hello"
        return

    events = [json.loads(line) for line in captured.out.splitlines()]
    assert [event["type"] for event in events] == [
        "assistant",
        "tool_use",
        "tool_result",
        "usage",
        "turn_complete",
        "result",
    ]
    assert events[2]["is_error"] is False
    assert events[-1]["result"] == "hello"
    assert events[-1]["num_turns"] == 2
    assert events[-1]["usage"] == {"input_tokens": 12, "output_tokens": 4}
