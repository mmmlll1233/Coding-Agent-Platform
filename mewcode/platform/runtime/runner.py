from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import Any

from mewcode.agent import (
    AgentEvent,
    CompactNotification,
    ErrorEvent,
    HookEvent,
    LoopComplete,
    PermissionRequest,
    PermissionResponse,
    RetryEvent,
    StreamText,
    ThinkingText,
    ToolResultEvent,
    ToolUseEvent,
    TurnComplete,
    UsageEvent,
)

from .models import (
    JobEvent,
    JobEventSink,
    JobResult,
    JobRunRequest,
    JobRunStatus,
    NullJobEventSink,
)


class _EventSinkFailure(RuntimeError):
    pass


class JobRunner:
    """Drive one Agent attempt and project its typed events to a durable boundary."""

    def __init__(
        self,
        runtime: Any,
        sink: JobEventSink | None = None,
        *,
        owns_runtime: bool = True,
        initial_sequence: int = 0,
    ) -> None:
        if initial_sequence < 0:
            raise ValueError("initial_sequence must be non-negative")
        self.runtime = runtime
        self.sink = sink or NullJobEventSink()
        self.owns_runtime = owns_runtime
        self._active_task: asyncio.Task[Any] | None = None
        self._used = False
        self._sequence = initial_sequence

    @property
    def last_sequence(self) -> int:
        return self._sequence

    def _redact_value(self, value: Any) -> Any:
        services = getattr(self.runtime, "services", {})
        redactor = services.get("redactor") if isinstance(services, dict) else None
        if redactor is None:
            return value
        if isinstance(value, str):
            return redactor.redact(value)
        if isinstance(value, dict):
            return {key: self._redact_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact_value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self._redact_value(item) for item in value)
        return value

    async def cancel(self) -> None:
        task = self._active_task
        if task is None or task.done():
            return
        task.cancel()
        if task is asyncio.current_task():
            return
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _emit(
        self,
        request: JobRunRequest,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self._sequence += 1
        event = JobEvent(
            job_id=request.job_id,
            attempt_id=request.attempt_id,
            attempt_sequence=self._sequence,
            timestamp=datetime.now(UTC),
            event_type=event_type,
            payload=self._redact_value(payload or {}),
        )
        try:
            await self.sink.emit(event)
        except Exception as exc:
            raise _EventSinkFailure(str(exc)) from exc

    @staticmethod
    def _event_projection(event: AgentEvent) -> tuple[str, dict[str, Any]]:
        if isinstance(event, StreamText):
            return "text_delta", {"text": event.text}
        if isinstance(event, ThinkingText):
            return "thinking_delta", {"text": event.text}
        if isinstance(event, ToolUseEvent):
            return "tool_started", {
                "tool_id": event.tool_id,
                "tool_name": event.tool_name,
                "arguments": event.arguments,
            }
        if isinstance(event, ToolResultEvent):
            payload: dict[str, Any] = {
                "tool_id": event.tool_id,
                "tool_name": event.tool_name,
                "output": event.output,
                "is_error": event.is_error,
                "elapsed": event.elapsed,
            }
            if event.command_result is not None:
                payload["command_result"] = asdict(event.command_result)
            return "tool_finished", payload
        if isinstance(event, UsageEvent):
            return "usage", {
                "input_tokens": event.input_tokens,
                "output_tokens": event.output_tokens,
            }
        if isinstance(event, RetryEvent):
            return "retry", {"reason": event.reason, "wait": event.wait}
        if isinstance(event, CompactNotification):
            return "compact", {
                "before_tokens": event.before_tokens,
                "message": event.message,
            }
        if isinstance(event, HookEvent):
            return "hook", {
                "hook_id": event.hook_id,
                "event": event.event,
                "output": event.output,
                "success": event.success,
            }
        if isinstance(event, TurnComplete):
            return "turn_completed", {"turn": event.turn}
        if isinstance(event, LoopComplete):
            return "agent_completed", {"total_turns": event.total_turns}
        if isinstance(event, ErrorEvent):
            return "runtime_error", {
                "code": event.code,
                "message": event.message,
                "terminal": event.terminal,
            }
        if isinstance(event, PermissionRequest):
            return "permission_required", {
                "tool_name": event.tool_name,
                "description": event.description,
            }
        raise TypeError(f"Unsupported AgentEvent: {type(event).__name__}")

    @staticmethod
    def _final_text(runtime: Any) -> str:
        for message in reversed(runtime.conversation.history):
            if message.role == "assistant":
                return message.content
        return ""

    async def run(self, request: JobRunRequest) -> JobResult:
        if self._used:
            raise RuntimeError("JobRunner instances can execute only one Attempt")
        self._used = True
        self._active_task = asyncio.current_task()
        started_at = datetime.now(UTC)
        status = JobRunStatus.FAILED
        error_code: str | None = "INCOMPLETE_AGENT_RUN"
        error_message: str | None = "Agent stream ended without LoopComplete"
        total_turns = 0
        input_tokens = 0
        output_tokens = 0

        stream: Any | None = None
        started_runtime = False
        cleanup_failure: Exception | None = None
        stream_close_failure: Exception | None = None
        try:
            services = getattr(self.runtime, "services", {})
            environment = (
                services.get("execution_environment")
                if isinstance(services, dict)
                else None
            )
            if environment is not None:
                spec = environment.spec
                if (request.job_id, request.attempt_id) != (
                    spec.job_id,
                    spec.attempt_id,
                ):
                    raise ValueError(
                        "JobRunRequest identity does not match AttemptExecutionSpec"
                    )

            start = getattr(self.runtime, "start", None)
            if start is not None:
                await start()
                started_runtime = True

            if request.prompt:
                self.runtime.conversation.add_user_message(request.prompt)

            stream = self.runtime.agent.run(self.runtime.conversation)
            deadline = (
                environment.spec.limits.attempt_timeout_seconds
                if environment is not None
                else None
            )

            async def consume() -> None:
                nonlocal status, error_code, error_message
                nonlocal total_turns, input_tokens, output_tokens
                assert stream is not None
                async for event in stream:
                    event_type, payload = self._event_projection(event)
                    await self._emit(request, event_type, payload)

                    if isinstance(event, UsageEvent):
                        input_tokens = event.input_tokens
                        output_tokens = event.output_tokens
                    elif isinstance(event, TurnComplete):
                        total_turns = max(total_turns, event.turn)
                    elif isinstance(event, LoopComplete):
                        total_turns = event.total_turns
                        status = JobRunStatus.COMPLETED
                        error_code = None
                        error_message = None
                    elif isinstance(event, ErrorEvent) and event.terminal:
                        status = JobRunStatus.FAILED
                        error_code = event.code
                        error_message = event.message
                    elif isinstance(event, PermissionRequest):
                        if not event.future.done():
                            event.future.set_result(PermissionResponse.DENY)
                        status = JobRunStatus.NEEDS_INPUT
                        error_code = "PERMISSION_REQUIRED"
                        error_message = event.description
                        break

            if deadline is None:
                await consume()
            else:
                async with asyncio.timeout(deadline):
                    await consume()
        except asyncio.CancelledError:
            status = JobRunStatus.CANCELLED
            error_code = "CANCELLED"
            error_message = "Attempt was cancelled"
        except TimeoutError:
            status = JobRunStatus.FAILED
            error_code = "ATTEMPT_DEADLINE_EXCEEDED"
            error_message = "Attempt exceeded its execution deadline"
        except ValueError as exc:
            status = JobRunStatus.FAILED
            error_code = "ATTEMPT_CONTEXT_MISMATCH"
            error_message = str(exc)
        except _EventSinkFailure as exc:
            status = JobRunStatus.FAILED
            error_code = "EVENT_SINK_FAILED"
            error_message = str(exc)
        except Exception as exc:  # noqa: BLE001 - Runtime trust boundary
            status = JobRunStatus.FAILED
            error_code = (
                "RUNTIME_EXCEPTION" if started_runtime else "EXECUTOR_START_FAILED"
            )
            error_message = str(exc)
        finally:
            if stream is not None:
                try:
                    await stream.aclose()
                except Exception as exc:  # noqa: BLE001 - stream trust boundary
                    stream_close_failure = exc
            close = getattr(self.runtime, "aclose", None)
            if self.owns_runtime and close is not None:
                try:
                    await asyncio.shield(close())
                except Exception as exc:  # noqa: BLE001 - cleanup trust boundary
                    cleanup_failure = exc
            self._active_task = None

        if stream_close_failure is not None and cleanup_failure is None:
            status = JobRunStatus.FAILED
            error_code = "RUNTIME_STREAM_CLOSE_FAILED"
            error_message = str(stream_close_failure)

        if cleanup_failure is not None:
            status = JobRunStatus.FAILED
            error_code = "EXECUTOR_CLEANUP_FAILED"
            error_message = str(cleanup_failure)

        finished_at = datetime.now(UTC)
        result = JobResult(
            job_id=request.job_id,
            attempt_id=request.attempt_id,
            status=status,
            final_text=self._redact_value(self._final_text(self.runtime)),
            total_turns=total_turns,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            error_code=error_code,
            error_message=self._redact_value(error_message),
            started_at=started_at,
            finished_at=finished_at,
        )

        final_event_type = {
            JobRunStatus.COMPLETED: "runtime_completed",
            JobRunStatus.NEEDS_INPUT: "runtime_needs_input",
            JobRunStatus.FAILED: "runtime_failed",
            JobRunStatus.CANCELLED: "runtime_cancelled",
        }[status]
        try:
            await self._emit(
                request,
                final_event_type,
                {
                    "status": status.value,
                    "error_code": error_code,
                    "error_message": error_message,
                },
            )
        except _EventSinkFailure as exc:
            result = replace(
                result,
                status=JobRunStatus.FAILED,
                error_code="EVENT_SINK_FAILED",
                error_message=self._redact_value(str(exc)),
                finished_at=datetime.now(UTC),
            )
        return result
