from __future__ import annotations

from collections.abc import Iterable


class SensitiveValueRedactor:
    """Redact explicitly registered trusted-service values from observable text."""

    def __init__(self, values: Iterable[str] = ()) -> None:
        self._values = tuple(
            sorted(
                {value for value in values if value and len(value) >= 8},
                key=len,
                reverse=True,
            )
        )

    def redact(self, text: str) -> str:
        redacted = text
        for value in self._values:
            redacted = redacted.replace(value, "[REDACTED]")
        return redacted

    def contains_secret(self, text: str) -> bool:
        return any(value in text for value in self._values)

