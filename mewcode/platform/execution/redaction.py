from __future__ import annotations

from collections.abc import Iterable
from threading import Lock


class SensitiveValueRedactor:
    """Redact explicitly registered trusted-service values from observable text."""

    def __init__(self, values: Iterable[str] = ()) -> None:
        self._lock = Lock()
        self._values = tuple(
            sorted(
                {value for value in values if value and len(value) >= 8},
                key=len,
                reverse=True,
            )
        )

    def add(self, *values: str) -> None:
        """Register short-lived credentials before they reach observability."""
        additions = {value for value in values if value and len(value) >= 8}
        if not additions:
            return
        with self._lock:
            self._values = tuple(
                sorted(set(self._values).union(additions), key=len, reverse=True)
            )

    def redact(self, text: str) -> str:
        redacted = text
        for value in self._values:
            redacted = redacted.replace(value, "[REDACTED]")
        return redacted

    def contains_secret(self, text: str) -> bool:
        return any(value in text for value in self._values)


_SHARED_PLATFORM_REDACTOR = SensitiveValueRedactor(())


def shared_platform_redactor() -> SensitiveValueRedactor:
    """Return the trusted process-wide redactor used by SCM and observability."""
    return _SHARED_PLATFORM_REDACTOR
