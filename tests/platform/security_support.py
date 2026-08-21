from __future__ import annotations

from collections.abc import Iterable
from uuid import uuid4


def make_test_canaries() -> dict[str, str]:
    """Return recognizable fake secrets that are unique to one test run."""
    suffix = uuid4().hex
    return {
        "MEWCODE_TEST_LLM_SECRET": f"CANARY_LLM_{suffix}",
        "MEWCODE_TEST_GITHUB_TOKEN": f"CANARY_GITHUB_{suffix}",
        "MEWCODE_TEST_FEISHU_SECRET": f"CANARY_FEISHU_{suffix}",
    }


def assert_canaries_absent(canaries: Iterable[str], *payloads: str) -> None:
    combined = "\n".join(payloads)
    leaked = [canary for canary in canaries if canary in combined]
    if leaked:
        raise AssertionError(f"secret canary leaked through an observable output: {leaked!r}")
