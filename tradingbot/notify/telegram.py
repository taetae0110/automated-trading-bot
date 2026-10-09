"""텔레그램 봇 알림.

Bot API ``sendMessage`` (https://core.telegram.org/bots/api#sendmessage):
  POST https://api.telegram.org/bot<token>/sendMessage
  JSON {"chat_id", "text" (1~4096자), "parse_mode": "HTML", ...}
HTML 모드에서는 태그가 아닌 ``<``, ``>``, ``&`` 를 반드시 엔티티로 바꿔야 하므로 본문 전체를 escape 한다.
링크 미리보기는 ``link_preview_options.is_disabled`` (Bot API 7.0+) 와 구 파라미터 ``disable_web_page_preview``
를 함께 보내 끈다 (서버는 모르는 파라미터를 무시한다).

토큰은 URL 에 포함되므로 오류 메시지/로그에서 ``***`` 로 가린다.
"""

from __future__ import annotations

import html
import logging
from typing import Any

from tradingbot.exceptions import BrokerError, ConfigError
from tradingbot.notify.base import MASK, Notifier, register_secret, split_text
from tradingbot.utils.http import HttpClient

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
#: sendMessage text 최대 길이 (엔티티 파싱 후 기준)
TELEGRAM_MAX_LEN = 4096


class TelegramNotifier(Notifier):
    name = "telegram"

    def __init__(
        self,
        token: str,
        chat_id: str | int,
        *,
        client: HttpClient | None = None,
        timeout: float = 10.0,
        max_retries: int = 2,
    ) -> None:
        if not token or not str(token).strip():
            raise ConfigError("텔레그램 봇 토큰이 비어 있습니다 (TELEGRAM_BOT_TOKEN)")
        if chat_id is None or not str(chat_id).strip():
            raise ConfigError("텔레그램 chat_id 가 비어 있습니다 (TELEGRAM_CHAT_ID)")
        self._token = str(token).strip()
        self.chat_id = str(chat_id).strip()
        self._client = client or HttpClient(TELEGRAM_API_BASE, timeout=timeout, max_retries=max_retries)
        register_secret(self._token, logger)

    # ------------------------------------------------------------------
    @property
    def _url(self) -> str:
        return f"{TELEGRAM_API_BASE}/bot{self._token}/sendMessage"

    def _mask(self, text: str) -> str:
        return text.replace(self._token, MASK)

    def _payload(self, chunk: str) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "text": html.escape(chunk, quote=False),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "link_preview_options": {"is_disabled": True},
        }

    def _send(self, text: str) -> None:
        for chunk in split_text(text, TELEGRAM_MAX_LEN):
            try:
                resp = self._client.post(self._url, json=self._payload(chunk), retry_unsafe=True)
            except BrokerError as e:
                # 원인 체인(requests 예외)에도 URL 이 들어 있으므로 끊고 마스킹한 메시지만 남긴다
                raise BrokerError(
                    self._mask(str(e)), status_code=e.status_code, payload=self._mask(str(e.payload))
                ) from None
            if isinstance(resp, dict) and resp.get("ok") is False:
                raise BrokerError(
                    f"텔레그램 API 오류: {resp.get('description', '알 수 없음')}",
                    status_code=resp.get("error_code"),
                    payload=resp,
                )
            logger.debug("[telegram] chat %s 로 %d자 전송", self.chat_id, len(chunk))

    def __repr__(self) -> str:
        return f"<TelegramNotifier chat_id={self.chat_id} token={MASK}>"


__all__ = ["TELEGRAM_API_BASE", "TELEGRAM_MAX_LEN", "TelegramNotifier"]
