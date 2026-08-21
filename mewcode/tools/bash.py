from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from mewcode.tools.base import CommandExecutionResult, Tool, ToolResult

if TYPE_CHECKING:
    from mewcode.sandbox import Sandbox, SandboxConfig

MAX_TIMEOUT = 600
log = logging.getLogger(__name__)


if sys.platform == "win32":
    from ctypes import wintypes

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _kernel32.AssignProcessToJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
    ]
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def _create_windows_job(proc: asyncio.subprocess.Process) -> int | None:
    if sys.platform != "win32":
        return None
    job = _kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
    if not _kernel32.SetInformationJobObject(
        job, 9, ctypes.byref(info), ctypes.sizeof(info)
    ):
        _kernel32.CloseHandle(job)
        return None
    try:
        popen = proc._transport.get_extra_info("subprocess")  # type: ignore[attr-defined]
        process_handle = wintypes.HANDLE(int(popen._handle))
    except (AttributeError, TypeError, ValueError):
        _kernel32.CloseHandle(job)
        return None
    if not _kernel32.AssignProcessToJobObject(job, process_handle):
        error = ctypes.get_last_error()
        log.warning("AssignProcessToJobObject failed for %s: %s", proc.pid, error)
        _kernel32.CloseHandle(job)
        return None
    return int(job)


def _terminate_windows_job(job: int | None) -> bool:
    if sys.platform != "win32" or job is None:
        return False
    return bool(_kernel32.TerminateJobObject(wintypes.HANDLE(job), 1))


def _close_windows_job(job: int | None) -> None:
    if sys.platform == "win32" and job is not None:
        _kernel32.CloseHandle(wintypes.HANDLE(job))

def _extract_last_command_name(command: str) -> str | None:
    """从命令字符串中提取最后一个管道段的基础命令名。

    管道中最后一个命令决定了整体退出码，所以只看最后一段。
    例如 "cat file | grep pattern" → "grep"
    """
    # 按管道符拆分，取最后一段
    last_segment = command.rsplit("|", maxsplit=1)[-1].strip()
    if not last_segment:
        return None

    # 跳过常见的环境变量赋值前缀，如 "FOO=bar command ..."
    # 也要处理 sudo/env 等包装命令
    try:
        tokens = shlex.split(last_segment)
    except ValueError:
        # shlex 解析失败时，用简单的空格分割兜底
        tokens = last_segment.split()

    for token in tokens:
        # 跳过形如 VAR=VALUE 的环境变量赋值
        if re.match(r"^[A-Za-z_]\w*=", token):
            continue
        # 取 basename（去掉路径前缀，如 /usr/bin/grep → grep）
        base = token.rsplit("/", maxsplit=1)[-1]
        return base

    return None


# 特殊命令的退出码提示信息
# 帮助 LLM 理解非零退出码的含义，而不是简单地标记为错误
_EXIT_CODE_HINTS: dict[str, str] = {
    "grep": "no matches found",
    "egrep": "no matches found",
    "fgrep": "no matches found",
    "rg": "no matches found",
    "diff": "files differ",
    "find": "some directories were inaccessible",
    "test": "condition is false",
    "[": "condition is false",
}


def _exit_code_hint(command: str, exit_code: int) -> str:
    """为非零退出码生成可读提示。

    对于特殊命令（grep/diff/test 等），附加语义说明让 LLM 理解退出码含义。
    普通命令只显示退出码数字。
    """
    cmd_name = _extract_last_command_name(command)
    hint = _EXIT_CODE_HINTS.get(cmd_name, "") if cmd_name else ""
    if hint:
        return f"Exit code {exit_code} ({hint})"
    return f"Exit code {exit_code}"


class Params(BaseModel):
    command: str = Field(description="Shell command to execute")
    timeout: int = Field(default=120, description="Timeout in seconds (max 600)")


class Bash(Tool):
    name = "Bash"
    description = "Execute a shell command and return stdout and stderr."
    params_model = Params
    category = "command"

    # 工作目录，为 None 时使用当前进程的工作目录
    work_dir: str | None = None

    # OS 级沙箱实例和配置（由外部注入，为 None 时不启用沙箱）
    sandbox: Sandbox | None = None
    sandbox_config: SandboxConfig | None = None

    async def _terminate_process_tree(
        self, proc: asyncio.subprocess.Process, windows_job: int | None = None
    ) -> None:
        """Terminate the shell and every descendant, then reap the root."""
        if sys.platform == "win32":
            if _terminate_windows_job(windows_job):
                await proc.wait()
                return
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(proc.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                killer_stdout, killer_stderr = await asyncio.wait_for(
                    killer.communicate(), timeout=5
                )
                if killer.returncode != 0:
                    log.warning(
                        "taskkill failed for process tree %s with exit code %s: %s %s",
                        proc.pid,
                        killer.returncode,
                        killer_stdout.decode(errors="replace").strip(),
                        killer_stderr.decode(errors="replace").strip(),
                    )
            except (FileNotFoundError, OSError, asyncio.TimeoutError):
                if proc.returncode is None:
                    proc.kill()
        else:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()

    @staticmethod
    def _render_output(command_result: CommandExecutionResult) -> str:
        parts: list[str] = []
        if command_result.stdout:
            parts.append(command_result.stdout.rstrip())
        if command_result.stderr:
            parts.append("stderr:\n" + command_result.stderr.rstrip())
        if command_result.timed_out:
            parts.append("Command timed out")
        elif command_result.exit_code not in (None, 0):
            parts.append(_exit_code_hint("", command_result.exit_code))
        elif command_result.exit_code is None:
            parts.append("Command failed to start")
        return "\n\n".join(part for part in parts if part) or "(no output)"

    async def execute(self, params: Params) -> ToolResult:
        timeout = min(params.timeout, MAX_TIMEOUT)

        # 如果启用了 OS 沙箱，将命令包装为沙箱内执行
        actual_command = params.command
        if self.sandbox and self.sandbox_config and self.sandbox.available():
            actual_command = self.sandbox.wrap(params.command, self.sandbox_config)

        process_options: dict[str, object] = {}
        if sys.platform == "win32":
            process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            process_options["start_new_session"] = True

        windows_job: int | None = None
        try:
            proc = await asyncio.create_subprocess_shell(
                actual_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.work_dir,
                **process_options,
            )
            windows_job = _create_windows_job(proc)
            communicate_task = asyncio.create_task(proc.communicate())
            try:
                stdout, stderr = await asyncio.wait_for(
                    asyncio.shield(communicate_task), timeout=timeout
                )
            except asyncio.TimeoutError:
                await self._terminate_process_tree(proc, windows_job)
                stdout, stderr = await communicate_task
                command_result = CommandExecutionResult(
                    exit_code=proc.returncode,
                    stdout=stdout.decode(errors="replace") if stdout else "",
                    stderr=stderr.decode(errors="replace") if stderr else "",
                    timed_out=True,
                )
                output = self._render_output(command_result)
                if output == "Command timed out":
                    output = f"Error: command timed out after {timeout}s"
                else:
                    output += f"\n\nError: command timed out after {timeout}s"
                result = ToolResult(
                    output=output,
                    is_error=True,
                    command_result=command_result,
                )
                _close_windows_job(windows_job)
                return result
            except asyncio.CancelledError:
                await self._terminate_process_tree(proc, windows_job)
                try:
                    await communicate_task
                except (asyncio.CancelledError, Exception):
                    pass
                raise
        except asyncio.TimeoutError:
            # Kept as a defensive fallback for platform-specific subprocess
            # implementations; the normal timeout path is handled above.
            command_result = CommandExecutionResult(
                exit_code=None,
                stdout="",
                stderr=f"command timed out after {timeout}s",
                timed_out=True,
            )
            result = ToolResult(
                output=f"Error: command timed out after {timeout}s",
                is_error=True,
                command_result=command_result,
            )
            _close_windows_job(windows_job)
            return result
        except asyncio.CancelledError:
            _close_windows_job(windows_job)
            raise
        except Exception as e:
            command_result = CommandExecutionResult(
                exit_code=None,
                stdout="",
                stderr=str(e),
                timed_out=False,
            )
            result = ToolResult(
                output=f"Error executing command: {e}",
                is_error=True,
                command_result=command_result,
            )
            _close_windows_job(windows_job)
            return result

        exit_code = proc.returncode if proc.returncode is not None else 0
        command_result = CommandExecutionResult(
            exit_code=exit_code,
            stdout=stdout.decode(errors="replace") if stdout else "",
            stderr=stderr.decode(errors="replace") if stderr else "",
            timed_out=False,
        )
        output = self._render_output(command_result)
        if exit_code != 0:
            hint = _exit_code_hint(params.command, exit_code)
            if output.endswith(f"Exit code {exit_code}"):
                output = output[: -len(f"Exit code {exit_code}")] + hint
            elif hint not in output:
                output = f"{output}\n\n{hint}"

        result = ToolResult(
            output=output,
            is_error=exit_code != 0,
            command_result=command_result,
        )
        _close_windows_job(windows_job)
        return result

