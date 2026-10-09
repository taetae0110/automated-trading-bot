"""슬랙 Incoming Webhook 알림.

https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/
  POST <webhook_url>  Content-type: application/json  {"text": "..."}
성공 시 본문은 JSON 이 아닌 ``ok`` 문자열이다. 웹훅 URL 자체가 비밀값이므로 로그에서 가린다.
"""

from __future__ import annotations

import logging

from tradingbot.exceptions import BrokerError, ConfigError
from tradingbot.notify.base import MASK, Notifier, register_secret
from tradingbot.utils.http import HttpClient

logger = logging.getLogger(__name__)


class SlackNotifier(Notifier):
    name = "slack"

    def __init__(
        self,
        webhook_url: str,
        *,
        client: HttpClient | None = None,
        timeout: float = 10.0,
        max_retries: int = 2,
    ) -> None:
        if not webhook_url or not str(webhook_url).strip():
            raise ConfigError("슬랙 웹훅 URL 이 비어 있습니다 (SLACK_WEBHOOK_URL)")
        url = str(webhook_url).strip()
        if not url.startswith("https://"):
            raise ConfigError("슬랙 웹훅 URL 은 https:// 로 시작해야 합니다")
        self._webhook_url = url
        self._client = client or HttpClient(timeout=timeout, max_retries=max_retries)
        register_secret(self._webhook_url, logger)

    def _mask(self, text: str) -> str:
        return text.replace(self._webhook_url, MASK)

    def _send(self, text: str) -> None:
        try:
            self._client.post(self._webhook_url, json={"text": text}, retry_unsafe=True)
        except BrokerError as e:
            raise BrokerError(
                self._mask(str(e)), status_code=e.status_code, payload=self._mask(str(e.payload))
            ) from None
        logger.debug("[slack] %d자 전송", len(text))

    def __repr__(self) -> str:
        return f"<SlackNotifier webhook={MASK}>"


__all__ = ["SlackNotifier"]
