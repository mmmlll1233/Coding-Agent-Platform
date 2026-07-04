# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from mewcode.teams.mailbox import Mailbox, MailboxMessage, create_message
from mewcode.teams.progress import TeammateProgress, random_verb

if TYPE_CHECKING:
    from mewcode.agent import Agent
    from mewcode.conversation import ConversationManager
    from mewcode.teams.models import TeammateInfo

log = logging.getLogger(__name__)

# Idle 轮询间隔（秒），对齐 Go 的 IdlePollInterval = 500ms
IDLE_POLL_INTERVAL = 0.5

# shutdown 消息前缀，对齐 Go 的 ShutdownPrefix
SHUTDOWN_PREFIX = "[shutdown]"

# lead 名称，对齐 Go 的 LeadName
LEAD_NAME = "lead"


def _is_shutdown_request(msg: MailboxMessage) -> bool:
    """判断邮箱消息是否为关闭请求。"""
    return msg.content.strip().startswith(SHUTDOWN_PREFIX)


def _create_idle_notification(member_name: str, reason: str) -> MailboxMessage:
    """构造 idle 通知消息，发给 lead 表明 teammate 当前轮次已完成。"""
    return create_message(
        from_agent=member_name,
        to_agent=LEAD_NAME,
        content=f"[idle] {member_name} (reason: {reason})",
        summary="idle",
    )


def _inject_pending_messages(mailbox: Mailbox, member_name: str) -> str:
    """读取 teammate 邮箱中的未读消息，拼成 system-reminder 字符串。"""
    msgs = mailbox.consume(member_name)
    if not msgs:
        return ""
    parts = ["You have new messages:\n"]
    for m in msgs:
        parts.append(f"From {m.from_agent}: {m.content}\n")
    return "\n".join(parts)


async def _wait_for_next_prompt_or_shutdown(
    mailbox: Mailbox,
    member_name: str,
) -> tuple[str, bool]:
    """阻塞轮询邮箱，等到有新消息后返回 (prompt, is_shutdown)。

    对齐 Go 的 waitForNextPromptOrShutdown：循环 sleep + 检查邮箱。
    收到 shutdown 消息返回 ("", True)；否则把普通消息拼成下一轮的 prompt。
    """
    while True:
        await asyncio.sleep(IDLE_POLL_INTERVAL)

        msgs = mailbox.consume(member_name)
        if not msgs:
            continue

        has_shutdown = False
        keep: list[MailboxMessage] = []
        for m in msgs:
            if _is_shutdown_request(m):
                has_shutdown = True
            else:
                keep.append(m)

        if has_shutdown:
            return "", True

        # 把剩余消息拼成下一轮的 user prompt
        if not keep:
            continue
        parts = ["You have new messages from your team:\n"]
        for m in keep:
            parts.append(f"From {m.from_agent}: {m.content}\n")
        return "\n".join(parts), False


class InProcessTeammateHandle:
    def __init__(
        self,
        agent: Agent,
        task: asyncio.Task[str],
        name: str,
        progress: TeammateProgress | None = None,
    ) -> None:
        self.agent = agent
        self.task = task
        self.name = name
        self.progress = progress


    @property
    def done(self) -> bool:
        return self.task.done()

    @property
    def result(self) -> str | None:
        if self.task.done():
            try:
                return self.task.result()
            except (asyncio.CancelledError, Exception):
                return None
        return None


    def cancel(self) -> None:
        if not self.task.done():
            self.task.cancel()


def spawn_inprocess_teammate(
    agent: Agent,
    prompt: str,
    name: str,
    conversation: ConversationManager | None = None,
    member: TeammateInfo | None = None,
    team_name: str = "",
    mailbox: Mailbox | None = None,
) -> InProcessTeammateHandle:

    # Create progress tracker and attach to member if provided
    progress = TeammateProgress(
        name=name,
        team_name=team_name,
        spinner_verb=random_verb(),
    )
    if member is not None:
        member.progress = progress

    def _on_event(event: dict[str, Any]) -> None:
        """Event callback wired into agent.run_to_completion."""
        event_type = event.get("type")
        if event_type == "tool_use":
            tool_name = event.get("toolName", "")
            args = event.get("args", {})
            progress.record_tool_use(tool_name, args)
        elif event_type == "usage":
            usage = event.get("usage", {})
            progress.record_tokens(
                usage.get("inputTokens", 0),
                usage.get("outputTokens", 0),
            )
        elif event_type == "stream_text":
            text = event.get("text")
            if text:
                with progress._lock:
                    progress.last_message = text

    async def _run() -> str:
        """teammate 主循环，对齐 Go 的 RunInProcessTeammate。

        有 mailbox 时进入长驻循环：执行 agent → 发 idle 通知 → 轮询等待新任务。
        没有 mailbox 时退化为单次执行（向后兼容）。
        """
        try:
            if conversation is not None:
                conv = conversation
            else:
                from mewcode.conversation import ConversationManager as CM
                conv = CM()

            next_prompt = prompt
            idle_reason = "available"

            while True:
                # 注入本轮开始前邮箱里堆积的消息
                if mailbox is not None:
                    reminder = _inject_pending_messages(mailbox, name)
                    if reminder:
                        conv.add_system_reminder(reminder)

                # 执行一个完整的 agent turn
                if next_prompt:
                    result = await agent.run_to_completion(
                        next_prompt, conv, event_callback=_on_event,
                    )
                else:
                    result = await agent.run_to_completion(
                        "", conv, event_callback=_on_event,
                    )
                next_prompt = ""

                # 没有 mailbox 时退化为单次执行（向后兼容旧调用方式）
                if mailbox is None:
                    progress.status = "completed"
                    return result

                # 更新进度状态
                if idle_reason == "failed":
                    progress.status = "failed"
                else:
                    progress.status = "idle"

                # 通知 lead 本轮已完成
                mailbox.write(
                    LEAD_NAME,
                    _create_idle_notification(name, idle_reason),
                )
                idle_reason = "available"

                # 轮询等待 lead 下发新任务或 shutdown 指令
                new_prompt, shutdown = await _wait_for_next_prompt_or_shutdown(
                    mailbox, name,
                )
                if shutdown:
                    progress.status = "completed"
                    return result

                next_prompt = new_prompt

        except asyncio.CancelledError:
            progress.status = "stopped"
            raise
        except Exception:
            progress.status = "failed"
            raise

    task = asyncio.create_task(_run(), name=f"teammate-{name}")
    log.info("Spawned in-process teammate %s (verb=%s)", name, progress.spinner_verb)
    return InProcessTeammateHandle(agent=agent, task=task, name=name, progress=progress)
