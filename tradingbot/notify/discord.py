"""디스코드 Webhook 알림.

https://discord.com/developers/docs/resources/webhook#execute-webhook
  POST /webhooks/{webhook.id}/{webhook.token}  JSON {"content": "..."}  (content 최대 2000자)
``wait`` 쿼리 없이 보내면 성공 시 ``204 No Content``. 2000자를 넘는 메시지는 여러 개로 나눠 보낸다.
웹훅 URL 에 토큰이 들어 있으므로 로그에서 가린다.
"""

from __future__ import annotations

import logging

from tradingbot.exceptions import BrokerError, ConfigError
from tradingbot.notify.base import MASK, Notifier, register_secret, split_text
from tradingbot.utils.http import HttpClient

logger = logging.getLogger(__name__)

#: 메시지 content 최대 길이
DISCORD_MAX_LEN = 2000


class DiscordNotifier(Notifier):
    name = "discord"

    def __init__(
        self,
        webhook_url: str,
        *,
        client: HttpClient | None = None,
        timeout: float = 10.0,
        max_retries: int = 2,
    ) -> None:
        if not webhook_url or not str(webhook_url).strip():
            raise ConfigError("디스코드 웹훅 URL 이 비어 있습니다 (DISCORD_WEBHOOK_URL)")
        url = str(webhook_url).strip()
        if not url.startswith("https://"):
            raise ConfigError("디스코드 웹훅 URL 은 https:// 로 시작해야 합니다")
        self._webhook_url = url
        self._client = client or HttpClient(timeout=timeout, max_retries=max_retries)
        register_secret(self._webhook_url, logger)

    def _mask(self, text: str) -> str:
        return text.replace(self._webhook_url, MASK)

    def _send(self, text: str) -> None:
        for chunk in split_text(text, DISCORD_MAX_LEN):
            try:
                self._client.post(self._webhook_url, json={"content": chunk}, retry_unsafe=True)
            except BrokerError as e:
                raise BrokerError(
                    self._mask(str(e)), status_code=e.status_code, payload=self._mask(str(e.payload))
                ) from None
            logger.debug("[discord] %d자 전송", len(chunk))

    def __repr__(self) -> str:
        return f"<DiscordNotifier webhook={MASK}>"


__all__ = ["DISCORD_MAX_LEN", "DiscordNotifier"]
