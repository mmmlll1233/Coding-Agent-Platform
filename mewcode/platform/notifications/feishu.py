from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from mewcode.platform.execution import SensitiveValueRedactor

_HOOK_PATH = re.compile(r"^/open-apis/bot/v2/hook/[A-Za-z0-9_-]+$")
_GITHUB_PR = re.compile(
    r"^https://github[.]com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*$"
)
_EVENT_STYLE = {
    "JOB_ACCEPTED": ("任务已受理", "blue"),
    "NEEDS_INPUT": ("任务等待补充信息", "orange"),
    "SUCCEEDED": ("任务已完成", "green"),
    "FAILED": ("任务执行失败", "red"),
    "CANCELLED": ("任务已取消", "grey"),
}


class FeishuDeliveryError(RuntimeError):
    def __init__(self, message: str, *, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


def validate_feishu_webhook_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "open.feishu.cn"
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not _HOOK_PATH.fullmatch(parsed.path)
    ):
        raise ValueError("Feishu webhook URL is outside the approved destination")
    return url


def feishu_signature(timestamp: int, secret: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}".encode()
    digest = hmac.new(string_to_sign, digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def _plain(value: Any, limit: int) -> str:
    text = str(value or "").replace("\x00", "").strip()
    return text[:limit]


def build_feishu_card(envelope: dict[str, Any]) -> dict[str, Any]:
    event_type = str(envelope.get("event_type", ""))
    if event_type not in _EVENT_STYLE:
        raise ValueError("Unsupported notification event type")
    heading, template = _EVENT_STYLE[event_type]
    repository = envelope.get("repository")
    if not isinstance(repository, dict):
        repository = {}
    notification_id = _plain(envelope.get("notification_id"), 64)
    fields = [
        ("Job ID", _plain(envelope.get("job_id"), 64)),
        ("Notification ID", notification_id),
        (
            "Repository",
            _plain(f"{repository.get('owner', '')}/{repository.get('name', '')}", 260),
        ),
        ("Base", _plain(repository.get("base_ref"), 255)),
        ("Title", _plain(envelope.get("title"), 200)),
        ("Attempt", _plain(envelope.get("attempt_no"), 12)),
        ("Status", _plain(envelope.get("status"), 32)),
    ]
    if event_type == "FAILED":
        fields.extend(
            (
                ("Error code", _plain(envelope.get("error_code"), 128)),
                ("Error", _plain(envelope.get("error_summary"), 1000)),
            )
        )
    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "plain_text",
                "content": f"{label}: {value or '-'}",
            },
        }
        for label, value in fields
    ]
    pr_url = str(envelope.get("pr_url", ""))
    if event_type == "SUCCEEDED" and _GITHUB_PR.fullmatch(pr_url):
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "查看 Draft PR"},
                        "type": "primary",
                        "url": pr_url,
                    }
                ],
            }
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": heading},
        },
        "elements": elements,
    }


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After", "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(raw)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


class FeishuWebhookClient:
    def __init__(
        self,
        webhook_url: str,
        signing_secret: str,
        *,
        timeout_seconds: int = 10,
        redactor: SensitiveValueRedactor | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.webhook_url = validate_feishu_webhook_url(webhook_url)
        self.signing_secret = signing_secret
        self.redactor = redactor or SensitiveValueRedactor(())
        self.redactor.add(webhook_url, signing_secret, urlsplit(webhook_url).path)
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=float(timeout_seconds), follow_redirects=False, trust_env=False
        )

    async def send(self, envelope: dict[str, Any]) -> None:
        timestamp = int(time.time())
        body = {
            "timestamp": str(timestamp),
            "sign": feishu_signature(timestamp, self.signing_secret),
            "msg_type": "interactive",
            "card": build_feishu_card(envelope),
        }
        try:
            response = await self.client.post(self.webhook_url, json=body)
        except httpx.TransportError as error:
            raise FeishuDeliveryError(
                f"Feishu network failure: {type(error).__name__}"
            ) from error
        if not 200 <= response.status_code < 300:
            raise FeishuDeliveryError(
                f"Feishu HTTP status {response.status_code}",
                retry_after_seconds=_retry_after(response),
            )
        try:
            result = response.json()
        except ValueError as error:
            raise FeishuDeliveryError("Feishu returned invalid JSON") from error
        if not isinstance(result, dict):
            raise FeishuDeliveryError("Feishu returned an invalid response envelope")
        code = result.get("code", result.get("StatusCode"))
        if code != 0:
            safe_code = _plain(code, 32)
            raise FeishuDeliveryError(f"Feishu business code {safe_code or 'missing'}")

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()
