"""requests 세션 + 재시도/백오프. 모든 REST 브로커 어댑터가 사용한다."""

from __future__ import annotations

import logging
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter

from tradingbot.exceptions import AuthenticationError, BrokerError, RateLimitError

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0
RETRY_STATUS = {429, 500, 502, 503, 504}


class HttpClient:
    """얇은 requests 래퍼. 5xx/429 는 지수 백오프로 재시도, 4xx 는 BrokerError 로 변환.

    주문(POST/DELETE)은 중복 체결 위험이 있으므로 기본적으로 재시도하지 않는다 (retry_unsafe=False).
    """

    def __init__(
        self,
        base_url: str = "",
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = 3,
        backoff: float = 0.5,
        headers: dict[str, str] | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self.session = session or requests.Session()
        self.session.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=8))
        if headers:
            self.session.headers.update(headers)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: Any = None,
        headers: dict[str, str] | None = None,
        retry_unsafe: bool = False,
        timeout: float | None = None,
    ) -> Any:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        method = method.upper()
        safe = method in ("GET", "HEAD") or retry_unsafe
        attempts = self.max_retries + 1 if safe else 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                resp = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    data=data,
                    headers=headers,
                    timeout=timeout or self.timeout,
                )
            except requests.RequestException as e:
                last_exc = e
                if attempt < attempts - 1:
                    self._sleep(attempt)
                    continue
                raise BrokerError(f"{method} {url} 네트워크 오류: {e}") from e

            if resp.status_code in RETRY_STATUS and attempt < attempts - 1:
                logger.warning("%s %s -> %s, 재시도 %d/%d", method, url, resp.status_code, attempt + 1, attempts - 1)
                self._sleep(attempt, retry_after=resp.headers.get("Retry-After"))
                continue
            return self._handle(resp, method, url)
        raise BrokerError(f"{method} {url} 실패: {last_exc}")

    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> Any:
        return self.request("POST", path, **kw)

    def delete(self, path: str, **kw: Any) -> Any:
        return self.request("DELETE", path, **kw)

    # ------------------------------------------------------------------
    def _sleep(self, attempt: int, retry_after: str | None = None) -> None:
        delay = self.backoff * (2**attempt)
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        time.sleep(min(delay, 10.0))

    @staticmethod
    def _handle(resp: requests.Response, method: str, url: str) -> Any:
        try:
            payload: Any = resp.json() if resp.content else None
        except ValueError:
            payload = resp.text
        if 200 <= resp.status_code < 300:
            return payload
        msg = f"{method} {url} -> HTTP {resp.status_code}: {_short(payload)}"
        if resp.status_code in (401, 403):
            raise AuthenticationError(msg, status_code=resp.status_code, payload=payload)
        if resp.status_code == 429:
            raise RateLimitError(msg, status_code=resp.status_code, payload=payload)
        raise BrokerError(msg, status_code=resp.status_code, payload=payload)


def _short(payload: Any, limit: int = 300) -> str:
    s = str(payload)
    return s if len(s) <= limit else s[:limit] + "..."
