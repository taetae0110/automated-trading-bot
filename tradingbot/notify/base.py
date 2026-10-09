"""알림(Notifier) 공통 인터페이스.

규약 (ARCHITECTURE.md 7절)
- ``Notifier.send(text)`` 는 **절대 예외를 밖으로 던지지 않는다**. 전송 실패는 로그만 남긴다.
- 토큰/웹훅 URL 같은 비밀값은 로그에 남기지 않는다. 각 구현체는 비밀값을 ``register_secret`` 으로 등록하고,
  오류 메시지는 ``mask_secrets`` 로 가린 뒤 로그에 쓴다. ``tradingbot.utils.http`` 로거의 재시도 경고
  (URL 포함) 에도 마스킹 필터를 건다.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterable

from tradingbot.exceptions import BrokerError

logger = logging.getLogger(__name__)

MASK = "***"

#: 비밀값이 URL 에 들어가는 HTTP 클라이언트 로거 (재시도 경고에 URL 이 찍힌다)
_HTTP_LOGGER_NAME = "tradingbot.utils.http"


class _SecretMaskFilter(logging.Filter):
    """등록된 비밀 문자열을 로그 레코드(msg/args)에서 ``***`` 로 치환한다."""

    def __init__(self) -> None:
        super().__init__(name="tradingbot.notify.secret_mask")
        self.secrets: set[str] = set()

    def add(self, secret: str) -> None:
        if secret:
            self.secrets.add(secret)

    def mask(self, text: str) -> str:
        for s in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(s, MASK)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.secrets:
            return True
        if isinstance(record.msg, str):
            record.msg = self.mask(record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(self.mask(a) if isinstance(a, str) else a for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: (self.mask(v) if isinstance(v, str) else v) for k, v in record.args.items()}
        return True


_mask_filter = _SecretMaskFilter()


def register_secret(secret: str | None, *loggers: logging.Logger) -> None:
    """비밀값을 마스킹 대상에 등록한다.

    로거 필터는 자식 로거에 상속되지 않으므로, HTTP 클라이언트 로거(재시도 경고에 URL 포함)와 이 모듈 로거,
    그리고 호출자가 넘긴 로거(각 채널 모듈 로거)에 각각 필터를 설치한다.
    """
    if not secret:
        return
    _mask_filter.add(secret)
    for lg in (logging.getLogger(_HTTP_LOGGER_NAME), logger, *loggers):
        if _mask_filter not in lg.filters:
            lg.addFilter(_mask_filter)


def mask_secrets(text: str) -> str:
    """등록된 모든 비밀값을 ``***`` 로 치환한 문자열."""
    return _mask_filter.mask(text)


def split_text(text: str, limit: int) -> list[str]:
    """``limit`` 글자 이하의 조각으로 나눈다. 가능하면 줄바꿈 경계에서 자르고, 공백뿐인 조각은 버린다."""
    if limit <= 0:
        raise ValueError(f"limit 은 양수여야 합니다: {limit}")
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 1, limit + 1)
        if cut == -1:
            chunk, rest = rest[:limit], rest[limit:]
        else:
            chunk, rest = rest[:cut], rest[cut + 1 :]
        chunk = chunk.strip("\n")
        if chunk.strip():
            chunks.append(chunk)
    rest = rest.strip("\n")
    if rest.strip():
        chunks.append(rest)
    return chunks


class Notifier(ABC):
    """알림 채널 베이스. 구현체는 ``_send`` 만 구현하면 된다 (예외를 던져도 ``send`` 가 삼킨다)."""

    #: 채널 식별자 (로그용)
    name: str = "base"

    def send(self, text: str) -> None:
        """메시지 전송. 어떤 경우에도 예외를 밖으로 던지지 않는다."""
        if not isinstance(text, str) or not text.strip():
            logger.debug("[%s] 빈 메시지는 보내지 않습니다", self.name)
            return
        try:
            self._send(text)
        except BrokerError as e:
            logger.warning("[%s] 알림 전송 실패: %s", self.name, mask_secrets(str(e)))
        except Exception as e:  # noqa: BLE001 - 알림 실패가 매매 루프를 멈추면 안 된다
            logger.error(
                "[%s] 알림 전송 중 예기치 못한 오류: %s: %s",
                self.name,
                type(e).__name__,
                mask_secrets(str(e)),
            )

    @abstractmethod
    def _send(self, text: str) -> None:
        """실제 전송. 실패 시 BrokerError (또는 다른 예외) 를 던져도 된다."""

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}>"


class NullNotifier(Notifier):
    """아무것도 보내지 않는 알림기 (알림 채널 미설정 시)."""

    name = "null"

    def _send(self, text: str) -> None:
        logger.debug("[null] 알림 생략 (%d자)", len(text))


class MultiNotifier(Notifier):
    """여러 채널에 같은 메시지를 보낸다. 한 채널이 실패해도 나머지는 계속 보낸다."""

    name = "multi"

    def __init__(self, notifiers: Iterable[Notifier]) -> None:
        self.notifiers: list[Notifier] = list(notifiers)
        for n in self.notifiers:
            if not isinstance(n, Notifier):
                raise TypeError(f"Notifier 가 아닙니다: {n!r}")

    def _send(self, text: str) -> None:
        for n in self.notifiers:
            n.send(text)  # 각 send 는 예외를 던지지 않는다

    def __len__(self) -> int:
        return len(self.notifiers)

    def __repr__(self) -> str:
        return f"<MultiNotifier {[n.name for n in self.notifiers]}>"


__all__ = [
    "MASK",
    "MultiNotifier",
    "Notifier",
    "NullNotifier",
    "mask_secrets",
    "register_secret",
    "split_text",
]
