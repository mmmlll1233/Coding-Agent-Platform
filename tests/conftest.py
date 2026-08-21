from __future__ import annotations

import socket
from pathlib import Path

import pytest


EXPECTED_PHASE1_XFAILS = {
    "tests/platform/test_phase1_known_gaps.py::test_parallel_tool_batch_uses_the_same_policy_gate",
    "tests/platform/test_phase1_known_gaps.py::test_bash_nonzero_exit_is_a_structured_error",
    "tests/platform/test_phase1_known_gaps.py::test_bash_timeout_kills_descendant_processes",
    "tests/platform/test_phase1_known_gaps.py::test_run_to_completion_has_symmetric_session_lifecycle",
}
ALLOWED_CAPABILITY_SKIPS = {
    "tests/test_permissions.py::TestPathSandbox::test_symlink_escape",
}

_observed_xfails: dict[str, str] = {}
_observed_skips: dict[str, str] = {}
_outcome_errors: list[str] = []


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--enforce-phase0-outcomes",
        action="store_true",
        default=False,
        help="Reject unregistered skips/xfails and require the four Phase 1 gaps.",
    )


def pytest_sessionstart(session: pytest.Session) -> None:
    _observed_xfails.clear()
    _observed_skips.clear()
    _outcome_errors.clear()


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if not report.skipped:
        return
    if hasattr(report, "wasxfail"):
        _observed_xfails[report.nodeid] = str(report.wasxfail)
        return
    if report.when != "call":
        return
    reason = report.longrepr[-1] if isinstance(report.longrepr, tuple) else str(report.longrepr)
    _observed_skips[report.nodeid] = reason


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if not session.config.getoption("--enforce-phase0-outcomes"):
        return

    unexpected_xfails = set(_observed_xfails) - EXPECTED_PHASE1_XFAILS
    missing_xfails = EXPECTED_PHASE1_XFAILS - set(_observed_xfails)
    unexpected_skips = set(_observed_skips) - ALLOWED_CAPABILITY_SKIPS
    invalid_xfail_reasons = {
        nodeid
        for nodeid, reason in _observed_xfails.items()
        if not reason.startswith("PHASE1-")
    }
    invalid_skip_reasons = {
        nodeid
        for nodeid, reason in _observed_skips.items()
        if "PHASE0-CAPABILITY-" not in reason
    }

    checks = (
        (unexpected_xfails, "unexpected xfail"),
        (missing_xfails, "missing Phase 1 xfail"),
        (unexpected_skips, "unexpected skip"),
        (invalid_xfail_reasons, "xfail without PHASE1 reason"),
        (invalid_skip_reasons, "skip without PHASE0 capability reason"),
    )
    for nodeids, label in checks:
        for nodeid in sorted(nodeids):
            _outcome_errors.append(f"{label}: {nodeid}")

    if _outcome_errors:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    if not _outcome_errors:
        return
    terminalreporter.write_sep("=", "Phase 0 outcome gate failures")
    for error in _outcome_errors:
        terminalreporter.write_line(error)


@pytest.fixture(autouse=True)
def isolate_user_home_and_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep tests away from the real home directory and external network."""
    fake_home = tmp_path / "isolated-home"
    fake_home.mkdir(exist_ok=True)
    original_expanduser = Path.expanduser
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def isolated_expanduser(path: Path) -> Path:
        raw = str(path).replace("\\", "/")
        if raw == "~":
            return fake_home
        if raw.startswith("~/"):
            return fake_home / raw[2:]
        return original_expanduser(path)

    def guarded_connect(sock, address):
        host = address[0] if isinstance(address, tuple) and address else ""
        if host in {"127.0.0.1", "::1", "localhost"}:
            return original_connect(sock, address)
        raise AssertionError("real network access is forbidden in the default test suite")

    def guarded_connect_ex(sock, address):
        host = address[0] if isinstance(address, tuple) and address else ""
        if host in {"127.0.0.1", "::1", "localhost"}:
            return original_connect_ex(sock, address)
        raise AssertionError("real network access is forbidden in the default test suite")

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setattr(Path, "expanduser", isolated_expanduser)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
