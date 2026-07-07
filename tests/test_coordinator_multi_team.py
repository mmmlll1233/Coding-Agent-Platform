# 回归测试：Coordinator Mode 在多 Team 场景下的工具限制/恢复行为，
# 覆盖 team_create.py / team_delete.py 里曾经出现过的两个 bug：
# 1. 存在多个 Team 时，删除其中一个不应该提前恢复全部工具；
# 2. 连续创建第二个 Team 时不应该把已过滤的注册表当成全量注册表存起来。

from __future__ import annotations

import asyncio
import shutil
from unittest.mock import MagicMock

from mewcode.teams.manager import TeamManager
from mewcode.teams.models import resolve_team_dir
from mewcode.tools.team_create import TeamCreateTool, TeamCreateParams
from mewcode.tools.team_delete import TeamDeleteTool, TeamDeleteParams
from mewcode.tools import ToolRegistry
from mewcode.tools.base import Tool, ToolResult


class DummyTool(Tool):
    params_model = MagicMock

    def __init__(self, name: str, category: str = "read"):
        self.name = name
        self.description = f"Dummy {name}"
        self.category = category
        self.is_concurrency_safe = True
        self.is_system_tool = False

    def get_schema(self):
        return {"name": self.name, "description": self.description, "input_schema": {}}

    async def execute(self, params):
        return ToolResult(output=f"{self.name} executed")


def make_registry(*tool_names: str) -> ToolRegistry:
    reg = ToolRegistry()
    for name in tool_names:
        reg.register(DummyTool(name))
    return reg


class FakeAgent:
    def __init__(self, registry):
        self.agent_id = "lead-1"
        self.coordinator_mode = False
        self.registry = registry
        self._full_registry = None
        self._team_manager = None


def cleanup(*names):
    for n in names:
        d = resolve_team_dir(n)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)


def test_deleting_one_of_two_teams_should_not_restore_full_tools():
    cleanup("coordbug1", "coordbug2")
    try:
        tm = TeamManager()
        full_registry = make_registry("Agent", "WriteFile", "EditFile", "Bash")
        agent = FakeAgent(full_registry)

        create = TeamCreateTool(tm, agent, teammate_mode="in-process", is_interactive=False, enable_coordinator_mode=True)
        r1 = asyncio.run(create.execute(TeamCreateParams(team_name="coordbug1")))
        assert not r1.is_error
        assert agent.coordinator_mode is True
        restricted_names = {t.name for t in agent.registry.list_tools()}
        assert "WriteFile" not in restricted_names

        r2 = asyncio.run(create.execute(TeamCreateParams(team_name="coordbug2")))
        assert not r2.is_error
        assert len(tm.list_teams()) == 2

        delete = TeamDeleteTool(tm, agent)
        r3 = asyncio.run(delete.execute(TeamDeleteParams(team_name="coordbug1")))
        assert not r3.is_error
        assert len(tm.list_teams()) == 1

        names_after = {t.name for t in agent.registry.list_tools()}
        assert agent.coordinator_mode is True
        assert "WriteFile" not in names_after
    finally:
        cleanup("coordbug1", "coordbug2")


def test_second_team_create_does_not_corrupt_full_registry_snapshot():
    cleanup("coordbug3", "coordbug4")
    try:
        tm = TeamManager()
        full_registry = make_registry("Agent", "WriteFile", "EditFile", "Bash")
        agent = FakeAgent(full_registry)

        create = TeamCreateTool(tm, agent, teammate_mode="in-process", is_interactive=False, enable_coordinator_mode=True)
        asyncio.run(create.execute(TeamCreateParams(team_name="coordbug3")))
        asyncio.run(create.execute(TeamCreateParams(team_name="coordbug4")))

        snapshot_names = {t.name for t in agent._full_registry.list_tools()}
        assert "WriteFile" in snapshot_names

        delete = TeamDeleteTool(tm, agent)
        asyncio.run(delete.execute(TeamDeleteParams(team_name="coordbug3")))
        asyncio.run(delete.execute(TeamDeleteParams(team_name="coordbug4")))

        final_names = {t.name for t in agent.registry.list_tools()}
        assert "WriteFile" in final_names
    finally:
        cleanup("coordbug3", "coordbug4")
