"""Upbit(업비트) 브로커 어댑터.

공식 문서(https://docs.upbit.com/kr/reference/ , 2026-10 기준) 로 검증한 사실:

- 인증: ``Authorization: Bearer <JWT>``. 페이로드 ``{access_key, nonce(uuid4), query_hash, query_hash_alg}``.
  ``query_hash`` 는 **URL 인코딩되지 않은** 쿼리 문자열(``unquote(urlencode(params, doseq=True))``)의
  SHA512 hex 이며, 배열 파라미터는 ``states[]=wait&states[]=watch`` 처럼 키를 반복한다. POST 는 JSON 본문의
  key=value 쌍을 같은 방식으로 이어 붙여 해시한다. 서명 알고리즘은 ``HS512`` 권장 (HS256 도 허용).
- 공개 시세: ``GET /v1/ticker?markets=KRW-BTC,KRW-ETH`` (ticker 그룹 10회/초/IP),
  ``GET /v1/candles/minutes/{1,3,5,10,15,30,60,240}`` / ``/v1/candles/days`` / ``/v1/candles/weeks``
  (candle 그룹 10회/초/IP, ``count`` 최대 200, ``to`` 는 ISO8601 ``2025-06-24T04:56:53Z`` 형식이며
  **``to`` 미만(exclusive)** 의 캔들을 **최신→과거** 순으로 반환).
- 계좌: ``GET /v1/accounts`` → ``[{currency, balance, locked, avg_buy_price, avg_buy_price_modified, unit_currency}]``.
- 주문 가능 정보: ``GET /v1/orders/chance?market=`` → ``bid_fee/ask_fee``, ``market.bid.min_total``,
  ``market.ask.min_total``, ``market.max_total``, ``bid_types/ask_types``.
- 주문: ``POST /v1/orders`` (JSON 본문, order 그룹 12회/초/포켓). 시장가 매수 ``ord_type=price`` + ``price``(총액),
  시장가 매도 ``ord_type=market`` + ``volume``, 지정가 ``ord_type=limit`` + ``volume`` + ``price``.
  ``identifier``(선택, 계정 내 고유, 최대 64자) 는 클라이언트 주문 ID 로, 조회/취소에 ``uuid`` 대신 쓸 수 있다
  (둘 다 보내면 uuid 기준). 이 어댑터는 주문마다 uuid4 identifier 를 보내고, 응답을 받지 못한 주문을 이것으로 찾는다.
  ``GET /v1/order?uuid=|identifier=`` (trades 포함), ``DELETE /v1/order?uuid=|identifier=``, ``GET /v1/orders/open``
  (``states[]``, ``limit`` 최대 100, ``page``) 는 default 그룹 30회/초/포켓. 주문 상태 ``wait/watch/done/cancel``.
  시장가 매수(``price``)는 체결 후 잔량(호가 단위 미만의 잔돈)이 남으면 ``cancel``, 딱 맞아떨어지면 ``done`` 으로 끝난다
  (주문 목록 조회 문서의 state 설명) — 즉 ``cancel`` 이 금액 주문의 정상 완료 상태다.
- nonce 는 **요청마다** 새 UUID 여야 한다 (같은 요청을 재시도해도 새 값; 재사용 시 401 ``nonce_used``).
  따라서 재시도 때마다 JWT 를 새로 만든다.
- 오류 본문 ``{"error": {"name": ..., "message": ...}}`` (시세 API 는 name 이 정수). 429 = 초당 한도 초과,
  418 = 반복 위반으로 일시 차단. 오류 분류는 HTTP 상태만이 아니라 ``error.name`` 을 함께 본다 (예: 주문 API 의
  403 ``market_offline`` = 시스템 점검, 401/403 ``out_of_scope`` = 권한 없음).
  ``Remaining-Req: group=default; min=1800; sec=29`` 헤더 (``min`` 은 deprecated).
- 원화 마켓 호가 단위 표(docs.upbit.com/kr/docs/krw-market-info, 2025-07-31 개정): 아래 ``KRW_TICK_TABLE``.
  최소 주문 금액 5,000 KRW. BTC 마켓 호가 0.00000001 BTC / 최소 0.00005 BTC, USDT 마켓 최소 0.5 USDT.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
import warnings
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any
from urllib.parse import unquote, urlencode

import jwt
import requests

from tradingbot.brokers.base import BaseBroker
from tradingbot.config import AppConfig, Credentials
from tradingbot.exceptions import (
    AuthenticationError,
    BrokerError,
    ConfigError,
    DataError,
    InsufficientFunds,
    OrderError,
    RateLimitError,
)
from tradingbot.models import (
    AssetClass,
    Balance,
    Candle,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    ensure_utc,
    interval_to_seconds,
    utcnow,
)
from tradingbot.utils.http import RETRY_STATUS, HttpClient

logger = logging.getLogger(__name__)

#: PyJWT 2.10+ 는 HS512 에 64바이트 미만 키를 쓰면 경고한다. 업비트 Secret Key 길이(40자)는 거래소가 정하는
#: 값이므로 서명 시에만 이 경고를 억제한다 (구버전 PyJWT 에는 클래스가 없다).
_KEY_LENGTH_WARNING: type[Warning] | None = getattr(jwt, "InsecureKeyLengthWarning", None)

DEFAULT_BASE_URL = "https://api.upbit.com"
MAX_CANDLE_COUNT = 200
OPEN_ORDERS_PAGE_SIZE = 100
#: 마켓별 최소 주문 금액 (quote 통화). 공식 문서 "마켓별 주문 정책".
KRW_MIN_ORDER_VALUE = 5_000.0
BTC_MIN_ORDER_VALUE = 0.00005
USDT_MIN_ORDER_VALUE = 0.5
#: /v1/orders/chance 를 못 읽었을 때 쓰는 기본 수수료율 (업비트 원화마켓 기본 0.05%)
DEFAULT_FEE_RATE = 0.0005
#: 주문 가능 정보 캐시 유효 시간 (초)
CHANCE_CACHE_TTL = 3600.0
SUPPORTED_JWT_ALGORITHMS = ("HS512", "HS256")

#: 캔들 간격 → 분 단위 (분 캔들 엔드포인트 unit)
_MINUTE_UNITS: dict[str, int] = {
    "1m": 1,
    "3m": 3,
    "5m": 5,
    "10m": 10,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "4h": 240,
}
CANDLE_PATHS: dict[str, str] = {
    **{k: f"/v1/candles/minutes/{u}" for k, u in _MINUTE_UNITS.items()},
    "1d": "/v1/candles/days",
    "1w": "/v1/candles/weeks",
}

#: 요청 그룹별 초당 호출 한도 (문서: order 12/s, default 30/s, 시세 그룹별 10/s). 주문은 여유를 두어 8/s.
RATE_LIMITS: dict[str, float] = {
    "order": 8.0,
    "default": 30.0,
    "candles": 10.0,
    "ticker": 10.0,
    "orderbook": 10.0,
    "market": 10.0,
}

#: 원화 마켓 호가 단위: (가격 하한(이상), 호가 단위). 위에서부터 첫 매칭 구간 적용, 마지막은 0.00001 미만.
KRW_TICK_TABLE: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("2000000"), Decimal("1000")),
    (Decimal("1000000"), Decimal("1000")),
    (Decimal("500000"), Decimal("500")),
    (Decimal("100000"), Decimal("100")),
    (Decimal("50000"), Decimal("50")),
    (Decimal("10000"), Decimal("10")),
    (Decimal("5000"), Decimal("5")),
    (Decimal("1000"), Decimal("1")),
    (Decimal("100"), Decimal("1")),
    (Decimal("10"), Decimal("0.1")),
    (Decimal("1"), Decimal("0.01")),
    (Decimal("0.1"), Decimal("0.001")),
    (Decimal("0.01"), Decimal("0.0001")),
    (Decimal("0.001"), Decimal("0.00001")),
    (Decimal("0.0001"), Decimal("0.000001")),
    (Decimal("0.00001"), Decimal("0.0000001")),
)
KRW_MIN_TICK = Decimal("0.00000001")
#: USDT 마켓 호가 단위 (2024-08-19 개정)
USDT_TICK_TABLE: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("10"), Decimal("0.01")),
    (Decimal("1"), Decimal("0.001")),
    (Decimal("0.1"), Decimal("0.0001")),
    (Decimal("0.01"), Decimal("0.00001")),
    (Decimal("0.001"), Decimal("0.000001")),
    (Decimal("0.0001"), Decimal("0.0000001")),
)
USDT_MIN_TICK = Decimal("0.00000001")
#: BTC 마켓은 가격과 무관하게 단일 호가 단위
BTC_TICK = Decimal("0.00000001")

_QUANTITY_STEP = Decimal("0.00000001")

# 오류 name → 예외 매핑 (REST API 사용 및 에러 안내 문서)
_AUTH_ERROR_NAMES = frozenset(
    {
        "invalid_query_payload",
        "jwt_verification",
        "expired_access_key",
        "nonce_used",
        "no_authorization_ip",
        "no_authorization_token",
        "out_of_scope",
        "invalid_access_key",
        "invalid_jwt",
    }
)
_RATE_LIMIT_ERROR_NAMES = frozenset({"too_many_requests"})
_ORDER_ERROR_NAMES = frozenset(
    {
        "create_ask_error",
        "create_bid_error",
        "under_min_total_ask",
        "under_min_total_bid",
        "invalid_volume_ask",
        "invalid_volume_bid",
        "invalid_price_ask",
        "invalid_price_bid",
        "invalid_time_in_force",
        "invalid_post_only",
        "duplicated_identifier",
        "over_krw_funds_bid",
        "market_offline",
        "notfoundmarket",
        "order_not_found",
        "validation_error",
    }
)
_INSUFFICIENT_FUNDS_PREFIX = "insufficient_funds"


# ---------------------------------------------------------------------- 순수 함수
def krw_tick_size(price: float | Decimal) -> float:
    """원화 마켓 호가 단위 (공식 표). price <= 0 이면 최소 단위."""
    p = price if isinstance(price, Decimal) else Decimal(str(price))
    for lower, tick in KRW_TICK_TABLE:
        if p >= lower:
            return float(tick)
    return float(KRW_MIN_TICK)


def usdt_tick_size(price: float | Decimal) -> float:
    p = price if isinstance(price, Decimal) else Decimal(str(price))
    for lower, tick in USDT_TICK_TABLE:
        if p >= lower:
            return float(tick)
    return float(USDT_MIN_TICK)


def tick_size(symbol: str, price: float) -> float:
    """심볼(quote 통화)과 가격에 맞는 호가 단위."""
    quote = _split_symbol(symbol)[0]
    if quote == "KRW":
        return krw_tick_size(price)
    if quote == "USDT":
        return usdt_tick_size(price)
    return float(BTC_TICK)


def build_query_string(params: dict[str, Any]) -> str:
    """query_hash 계산용 문자열 (URL 인코딩 해제, 배열은 key[]=v 반복). 공식 Python 예제와 동일."""
    return unquote(urlencode(params, doseq=True))


def query_hash(params: dict[str, Any]) -> str:
    return hashlib.sha512(build_query_string(params).encode("utf-8")).hexdigest()


def parse_remaining_req(header: str) -> dict[str, Any]:
    """``Remaining-Req: group=default; min=1800; sec=29`` → {"group": "default", "min": 1800, "sec": 29}."""
    out: dict[str, Any] = {}
    for part in header.split(";"):
        key, _, value = part.strip().partition("=")
        key, value = key.strip(), value.strip()
        if not key or not value:
            continue
        if key in ("min", "sec"):
            try:
                out[key] = int(value)
            except ValueError:
                continue
        else:
            out[key] = value
    return out


def _split_symbol(symbol: str) -> tuple[str, str]:
    """'KRW-BTC' → ('KRW', 'BTC'). 형식이 틀리면 BrokerError."""
    if not isinstance(symbol, str) or symbol.count("-") != 1:
        raise BrokerError(f"잘못된 Upbit 심볼 형식: {symbol!r} (예: KRW-BTC)")
    quote, base = symbol.split("-", 1)
    if not quote or not base:
        raise BrokerError(f"잘못된 Upbit 심볼 형식: {symbol!r} (예: KRW-BTC)")
    return quote, base


def _fmt_decimal(value: float | Decimal, places: int = 8) -> str:
    """API 로 보낼 숫자 문자열. 지수 표기 없이 소수 ``places`` 자리까지 내림, 뒤 0 제거."""
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    d = d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN)
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _to_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_time(value: Any) -> datetime | None:
    """'2025-07-04T15:00:00+09:00' (KST) → UTC aware. 실패 시 None."""
    if not value or not isinstance(value, str):
        return None
    try:
        return ensure_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def _error_fields(payload: Any) -> tuple[str | None, str]:
    """오류 본문 {"error": {"name", "message"}} 에서 (name, message) 추출."""
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            name = err.get("name")
            message = err.get("message")
            return (str(name) if name is not None else None, str(message) if message is not None else "")
    return None, ""


class _RateLimiter:
    """단순 시간 기반 호출 간격 제한기 (초당 N회). 스레드 안전."""

    def __init__(self, rate_per_sec: float) -> None:
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec 는 0보다 커야 합니다")
        self.interval = 1.0 / rate_per_sec
        self._next_allowed = 0.0
        self._lock = threading.Lock()

    def wait(self) -> float:
        """다음 호출이 허용될 때까지 대기하고, 실제 대기 시간을 반환."""
        with self._lock:
            now = time.monotonic()
            delay = self._next_allowed - now
            if delay > 0:
                time.sleep(delay)
                now = time.monotonic()
            self._next_allowed = max(now, self._next_allowed) + self.interval
            return max(delay, 0.0)

    def defer(self, seconds: float) -> None:
        """서버가 잔여 요청 수 0 을 알려줬을 때 다음 허용 시각을 뒤로 미룬다."""
        if seconds <= 0:
            return
        with self._lock:
            self._next_allowed = max(self._next_allowed, time.monotonic() + seconds)


# ---------------------------------------------------------------------- 브로커
class UpbitBroker(BaseBroker):
    """업비트 REST API 어댑터 (원화 마켓 현물, 롱 전용)."""

    name = "upbit"
    asset_class = AssetClass.CRYPTO
    supported_intervals: tuple[str, ...] = tuple(CANDLE_PATHS)

    def __init__(
        self,
        access_key: str | None = None,
        secret_key: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 10.0,
        client: HttpClient | None = None,
        jwt_algorithm: str = "HS512",
    ) -> None:
        if jwt_algorithm not in SUPPORTED_JWT_ALGORITHMS:
            raise ConfigError(
                f"지원하지 않는 JWT 알고리즘: {jwt_algorithm!r} (가능: {', '.join(SUPPORTED_JWT_ALGORITHMS)})"
            )
        self._access_key = access_key or None
        self._secret_key = secret_key or None
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.jwt_algorithm = jwt_algorithm
        self._owns_client = client is None
        self._client = client or HttpClient(
            self.base_url,
            timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": "tradingbot-upbit/0.1"},
        )
        self._limiters: dict[str, _RateLimiter] = {g: _RateLimiter(r) for g, r in RATE_LIMITS.items()}
        #: 최근 응답의 Remaining-Req 정보 (group → {"group", "min", "sec"})
        self.remaining_req: dict[str, dict[str, Any]] = {}
        self._chance_cache: dict[str, dict[str, Any]] = {}
        #: 시장가 매수(금액 주문)는 응답에 volume 이 없어 요청 수량을 기억해 둔다 (uuid → 수량)
        self._requested_qty: dict[str, float] = {}
        self._client.session.hooks.setdefault("response", []).append(self._on_response)

    @classmethod
    def from_config(cls, config: AppConfig) -> UpbitBroker:
        """환경변수(UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY)에서 키를 읽는다. 없으면 공개 API 전용."""
        creds = Credentials.from_env()
        extra = config.broker.extra or {}
        base_url = str(extra.get("base_url") or DEFAULT_BASE_URL)
        timeout = float(extra.get("timeout") or 10.0)
        jwt_algorithm = str(extra.get("jwt_algorithm") or "HS512")
        broker = cls(
            creds.upbit_access_key,
            creds.upbit_secret_key,
            base_url=base_url,
            timeout=timeout,
            jwt_algorithm=jwt_algorithm,
        )
        if not broker.has_credentials:
            logger.info(
                "Upbit API 키가 없어 공개 시세 조회만 가능합니다 (UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY)"
            )
        return broker

    # ------------------------------------------------------------------ 속성/메타
    @property
    def has_credentials(self) -> bool:
        return bool(self._access_key and self._secret_key)

    def quote_currency(self, symbol: str) -> str:
        return _split_symbol(symbol)[0]

    def base_currency(self, symbol: str) -> str:
        return _split_symbol(symbol)[1]

    def min_order_value(self, symbol: str) -> float:
        """최소 주문 금액. /v1/orders/chance 를 읽은 적이 있으면 그 값(min_total), 아니면 마켓별 공식 기본값."""
        cached = self._chance_cache.get(symbol)
        if cached and cached.get("min_total_bid", 0.0) > 0:
            return float(cached["min_total_bid"])
        quote = self.quote_currency(symbol)
        return {"KRW": KRW_MIN_ORDER_VALUE, "BTC": BTC_MIN_ORDER_VALUE, "USDT": USDT_MIN_ORDER_VALUE}.get(
            quote, 0.0
        )

    def round_quantity(self, symbol: str, quantity: float) -> float:
        """소수 8자리 내림 (Decimal 로 계산해 부동소수 오차 없음)."""
        if quantity <= 0:
            return 0.0
        return float(Decimal(str(quantity)).quantize(_QUANTITY_STEP, rounding=ROUND_DOWN))

    def round_price(self, symbol: str, price: float) -> float:
        """호가 단위로 반올림 (ROUND_HALF_UP)."""
        if price <= 0:
            raise OrderError(f"가격은 0보다 커야 합니다: {price}")
        tick = Decimal(str(tick_size(symbol, price)))
        p = Decimal(str(price))
        rounded = (p / tick).quantize(Decimal(1), rounding=ROUND_HALF_UP) * tick
        return float(rounded)

    def close(self) -> None:
        if self._owns_client:
            self._client.session.close()

    # ------------------------------------------------------------------ 시세 (공개)
    def get_candles(
        self,
        symbol: str,
        interval: str,
        limit: int = 200,
        end: datetime | None = None,
        include_partial: bool = False,
    ) -> list[Candle]:
        """``end`` 미만(exclusive)의 캔들 limit 개를 오래된→최신 순으로.

        Upbit 는 한 번에 최대 200개만 주므로 limit 이 200 을 넘으면 ``to`` (배타적) 를 가장 오래된 캔들의
        시각으로 옮겨 가며 과거 방향으로 페이지네이션한다 (엔진의 candle_limit=300 등).
        include_partial=False 면 아직 진행 중인 캔들(시작 + 간격 > 현재) 을 제외한다. 이때 limit 개의 완성 캔들을
        돌려주기 위해 한 개를 더 요청한다 (limit=200 이면 완성 캔들 199개가 될 수 있다).
        """
        path = self._candle_path(interval)
        if limit <= 0:
            return []
        want = int(limit)
        secs = interval_to_seconds(interval)
        cursor = ensure_utc(end) if end is not None else None
        collected: dict[datetime, Candle] = {}
        max_pages = -(-want // MAX_CANDLE_COUNT) + 1
        for _ in range(max_pages):
            remaining = want - len(collected)
            if remaining <= 0:
                break
            count = min(remaining if include_partial else remaining + 1, MAX_CANDLE_COUNT)
            params: dict[str, Any] = {"market": symbol, "count": count}
            if cursor is not None:
                params["to"] = cursor.strftime("%Y-%m-%dT%H:%M:%SZ")
            raw = self._public(path, params, group="candles")
            if not isinstance(raw, list):
                raise DataError(f"Upbit 캔들 응답 형식 오류 ({symbol} {interval}): {type(raw).__name__}")
            if not raw:
                break
            page = sorted((self._parse_candle(item) for item in raw), key=lambda c: c.timestamp)
            oldest = page[0].timestamp
            if not include_partial:
                now = utcnow()
                page = [c for c in page if c.timestamp + timedelta(seconds=secs) <= now]
            new = [c for c in page if c.timestamp not in collected]
            for c in new:
                collected[c.timestamp] = c
            if len(raw) < count or (cursor is not None and oldest >= cursor) or (not new and page):
                # 마지막 페이지이거나, 더 과거로 진행하지 못하면 중단
                break
            cursor = oldest
        candles = sorted(collected.values(), key=lambda c: c.timestamp)
        if len(candles) > want:
            candles = candles[-want:]
        return candles

    def get_tickers(self, symbols: list[str]) -> dict[str, float]:
        """여러 심볼의 현재가를 한 번의 /v1/ticker 호출로."""
        markets = [s for s in symbols if s]
        if not markets:
            return {}
        raw = self._public("/v1/ticker", {"markets": ",".join(markets)}, group="ticker")
        if not isinstance(raw, list):
            raise BrokerError(f"Upbit 현재가 응답 형식 오류: {type(raw).__name__}")
        out: dict[str, float] = {}
        for item in raw:
            try:
                out[str(item["market"])] = float(item["trade_price"])
            except (KeyError, TypeError, ValueError) as e:
                raise BrokerError(f"Upbit 현재가 응답 파싱 실패: {e}") from e
        return out

    def get_ticker(self, symbol: str) -> float:
        tickers = self.get_tickers([symbol])
        if symbol not in tickers:
            raise BrokerError(f"Upbit 현재가 응답에 {symbol} 이 없습니다")
        return tickers[symbol]

    # ------------------------------------------------------------------ 계좌 (비공개)
    def get_balances(self) -> dict[str, Balance]:
        out: dict[str, Balance] = {}
        for acc in self._accounts():
            currency = str(acc.get("currency") or "")
            if not currency:
                continue
            balance = _to_float(acc.get("balance"))
            locked = _to_float(acc.get("locked"))
            out[currency] = Balance(currency=currency, total=balance + locked, available=balance)
        return out

    def get_positions(self) -> dict[str, Position]:
        """평균 매수가 > 0 이고 보유 수량(balance+locked) > 0 인 자산을 ``{unit_currency}-{currency}`` 로."""
        out: dict[str, Position] = {}
        for acc in self._accounts():
            currency = str(acc.get("currency") or "")
            unit = str(acc.get("unit_currency") or "KRW")
            if not currency or currency == unit:
                continue
            avg = _to_float(acc.get("avg_buy_price"))
            balance = _to_float(acc.get("balance"))
            locked = _to_float(acc.get("locked"))
            qty = balance + locked
            if avg <= 0 or qty <= 0:
                continue
            symbol = f"{unit}-{currency}"
            out[symbol] = Position(
                symbol=symbol,
                quantity=qty,
                average_price=avg,
                meta={
                    "available": balance,
                    "locked": locked,
                    "avg_buy_price_modified": bool(acc.get("avg_buy_price_modified", False)),
                },
            )
        return out

    def get_equity(self, symbols: list[str] | None = None) -> float:
        """KRW 총 잔고 + Σ 포지션 평가액(KRW). 현재가는 /v1/ticker 한 번으로 묶어 조회."""
        balances = self.get_balances()
        positions = self.get_positions()
        cash = balances["KRW"].total if "KRW" in balances else 0.0
        if not positions:
            return cash
        markets: set[str] = set(positions)
        for sym in positions:
            quote = self.quote_currency(sym)
            if quote != "KRW":
                markets.add(f"KRW-{quote}")
        try:
            tickers = self.get_tickers(sorted(markets))
        except BrokerError as e:
            logger.warning("평가액 계산용 현재가 조회 실패, 매수 원가로 대체: %s", e)
            tickers = {}
        value = 0.0
        for sym, pos in positions.items():
            price = tickers.get(sym)
            quote = self.quote_currency(sym)
            fx = 1.0 if quote == "KRW" else tickers.get(f"KRW-{quote}")
            if price is None or fx is None:
                logger.warning("%s 현재가를 구할 수 없어 매수 원가로 평가합니다", sym)
                value += pos.cost
            else:
                value += pos.market_value(price) * fx
        return cash + value

    def get_order_chance(self, symbol: str, *, refresh: bool = False) -> dict[str, Any]:
        """주문 가능 정보 (/v1/orders/chance). 마켓별로 CHANCE_CACHE_TTL 동안 캐시."""
        cached = self._chance_cache.get(symbol)
        if cached and not refresh and time.monotonic() - cached["fetched_at"] < CHANCE_CACHE_TTL:
            return cached
        raw = self._private("GET", "/v1/orders/chance", params={"market": symbol})
        if not isinstance(raw, dict):
            raise BrokerError(f"Upbit 주문 가능 정보 응답 형식 오류 ({symbol})")
        market = raw.get("market") if isinstance(raw.get("market"), dict) else {}
        bid = market.get("bid") if isinstance(market.get("bid"), dict) else {}
        ask = market.get("ask") if isinstance(market.get("ask"), dict) else {}
        info: dict[str, Any] = {
            "market": symbol,
            "bid_fee": _to_float(raw.get("bid_fee"), DEFAULT_FEE_RATE),
            "ask_fee": _to_float(raw.get("ask_fee"), DEFAULT_FEE_RATE),
            "min_total_bid": _to_float(bid.get("min_total")),
            "min_total_ask": _to_float(ask.get("min_total")),
            "max_total": _to_float(market.get("max_total")),
            "bid_types": list(market.get("bid_types") or []),
            "ask_types": list(market.get("ask_types") or []),
            "fetched_at": time.monotonic(),
            "raw": raw,
        }
        self._chance_cache[symbol] = info
        return info

    # ------------------------------------------------------------------ 주문 (비공개)
    def place_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        order_type: OrderType = OrderType.MARKET,
        price: float | None = None,
    ) -> Order:
        self._require_credentials()
        if order_type == OrderType.STOP:
            raise OrderError(
                f"Upbit 는 STOP 주문을 네이티브로 지원하지 않습니다 ({symbol}). 엔진/백테스터가 폴링으로 흉내냅니다."
            )
        if order_type not in (OrderType.MARKET, OrderType.LIMIT):
            raise OrderError(f"지원하지 않는 주문 유형: {order_type!r}")
        if quantity is None or quantity <= 0:
            raise OrderError(f"주문 수량은 0보다 커야 합니다: {quantity!r} ({symbol})")
        qty = self.round_quantity(symbol, quantity)
        if qty <= 0:
            raise OrderError(f"수량 정밀도(8자리) 반올림 후 0 이 되었습니다: {quantity} ({symbol})")
        quote = self.quote_currency(symbol)
        body: dict[str, Any] = {"market": symbol, "side": "bid" if side == OrderSide.BUY else "ask"}

        if order_type == OrderType.MARKET and side == OrderSide.BUY:
            ticker = self.get_ticker(symbol)
            fee = self._bid_fee(symbol)
            amount = self.market_buy_amount(symbol, qty, ticker, fee)
            min_total = self.min_order_value(symbol)
            if amount < min_total:
                raise OrderError(
                    f"시장가 매수 금액 {amount:g} {quote} 이 최소 주문 금액 {min_total:g} {quote} 미만입니다 "
                    f"({symbol}, 수량 {qty:g} × 현재가 {ticker:g})"
                )
            body["ord_type"] = "price"
            body["price"] = _fmt_decimal(amount)
        elif order_type == OrderType.MARKET:
            body["ord_type"] = "market"
            body["volume"] = _fmt_decimal(qty)
        else:
            if price is None or price <= 0:
                raise OrderError(f"지정가 주문에는 0보다 큰 price 가 필요합니다: {price!r} ({symbol})")
            body["ord_type"] = "limit"
            body["volume"] = _fmt_decimal(qty)
            body["price"] = _fmt_decimal(self.round_price(symbol, price))

        # 클라이언트 주문 ID: 응답을 받지 못해도(타임아웃/연결 끊김) 거래소에 접수된 주문을 찾아 취소/확정할 수 있다.
        identifier = str(uuid.uuid4())
        body["identifier"] = identifier
        logger.info("Upbit 주문 접수: %s", body)
        recovered = False
        try:
            raw = self._private("POST", "/v1/orders", body=body, group="order", order_context=True)
        except BrokerError as e:
            if e.status_code is not None:
                raise
            raw = self._recover_lost_order(identifier, symbol, e)
            recovered = True
        if not isinstance(raw, dict) or not raw.get("uuid"):
            raise OrderError(f"Upbit 주문 응답에 uuid 가 없습니다: {raw!r}", payload=raw)
        order_id = str(raw["uuid"])
        self._remember_qty(order_id, qty)
        order = self._parse_order(raw, requested_quantity=qty)
        if recovered:
            # identifier 조회 응답은 개별 주문 조회와 같은 형식(trades 포함)이라 추가 조회가 필요 없다
            logger.warning(
                "Upbit 주문 응답 유실 후 identifier 로 복구 uuid=%s state=%s (%s)",
                order_id,
                raw.get("state"),
                symbol,
            )
            return order
        logger.info("Upbit 주문 접수 완료 uuid=%s state=%s", order_id, raw.get("state"))
        # 체결 수량 / 평균 체결가 / 수수료 보강 (trades 는 개별 주문 조회에서만 제공)
        try:
            order = self.get_order(order_id, symbol)
        except BrokerError as e:
            logger.warning("주문 %s 접수 후 상태 조회 실패 (접수 자체는 완료): %s", order_id, e)
        return order

    def cancel_order(
        self, order_id: str | None = None, symbol: str | None = None, *, identifier: str | None = None
    ) -> bool:
        """취소 접수 (``uuid`` 또는 클라이언트 ``identifier``). 존재하지 않는 주문이면 False."""
        params = self._order_key(order_id, identifier)
        try:
            raw = self._private("DELETE", "/v1/order", params=params, order_context=True)
        except OrderError as e:
            name, _ = _error_fields(e.payload)
            if e.status_code == 404 or name == "order_not_found":
                logger.info("취소할 주문을 찾지 못함 %s", params)
                return False
            raise
        return isinstance(raw, dict) and bool(raw.get("uuid"))

    def get_order(
        self, order_id: str | None = None, symbol: str | None = None, *, identifier: str | None = None
    ) -> Order:
        """개별 주문 조회 (``uuid`` 또는 클라이언트 ``identifier``; 둘 다 주면 서버는 uuid 기준)."""
        params = self._order_key(order_id, identifier)
        raw = self._private("GET", "/v1/order", params=params, order_context=True)
        if not isinstance(raw, dict):
            raise OrderError(f"Upbit 주문 조회 응답 형식 오류 {params}", payload=raw)
        return self._parse_order(raw)

    @staticmethod
    def _order_key(order_id: str | None, identifier: str | None) -> dict[str, str]:
        """``/v1/order`` 조회·취소 파라미터: uuid 와 identifier 중 적어도 하나 (공식 문서)."""
        params: dict[str, str] = {}
        if order_id:
            params["uuid"] = str(order_id)
        if identifier:
            params["identifier"] = str(identifier)
        if not params:
            raise OrderError("Upbit 주문 조회/취소에는 uuid 또는 identifier 가 필요합니다")
        return params

    def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        """체결 대기(wait) + 예약(watch) 주문. 100개씩 페이지네이션."""
        orders: list[Order] = []
        page = 1
        while True:
            params: dict[str, Any] = {}
            if symbol:
                params["market"] = symbol
            params["states[]"] = ["wait", "watch"]
            params["page"] = page
            params["limit"] = OPEN_ORDERS_PAGE_SIZE
            params["order_by"] = "asc"
            raw = self._private("GET", "/v1/orders/open", params=params)
            if not isinstance(raw, list):
                raise BrokerError(f"Upbit 미체결 주문 응답 형식 오류: {type(raw).__name__}")
            orders.extend(self._parse_order(item) for item in raw if isinstance(item, dict))
            if len(raw) < OPEN_ORDERS_PAGE_SIZE or page >= 50:
                break
            page += 1
        return orders

    # ------------------------------------------------------------------ 주문 보조
    def market_buy_amount(self, symbol: str, quantity: float, ticker: float, fee_rate: float) -> float:
        """시장가 매수 금액 환산: quantity × 현재가 를 (1 + 수수료율) 로 나눠 내림.

        업비트는 금액 주문(ord_type=price)에서 ``price × fee`` 를 별도로 예약하므로, 이렇게 줄여야
        ``price × (1 + fee)`` 가 원래 예산(quantity × 현재가)을 넘지 않는다. KRW 는 정수 원 단위로 내림.
        """
        gross = Decimal(str(quantity)) * Decimal(str(ticker))
        net = gross / (Decimal(1) + Decimal(str(fee_rate)))
        if self.quote_currency(symbol) == "KRW":
            return float(net.quantize(Decimal(1), rounding=ROUND_DOWN))
        return float(net.quantize(_QUANTITY_STEP, rounding=ROUND_DOWN))

    def _bid_fee(self, symbol: str) -> float:
        try:
            return float(self.get_order_chance(symbol)["bid_fee"])
        except AuthenticationError:
            raise
        except BrokerError as e:
            logger.warning(
                "주문 가능 정보 조회 실패, 기본 수수료율 %.4f 사용 (%s): %s", DEFAULT_FEE_RATE, symbol, e
            )
            return DEFAULT_FEE_RATE

    def _remember_qty(self, order_id: str, quantity: float) -> None:
        self._requested_qty[order_id] = quantity
        while len(self._requested_qty) > 1000:
            self._requested_qty.pop(next(iter(self._requested_qty)))

    def _recover_lost_order(self, identifier: str, symbol: str, cause: BrokerError) -> dict[str, Any]:
        """``POST /v1/orders`` 의 응답을 받지 못했을 때 ``identifier`` 로 접수 여부를 1회 확인한다.

        거래소가 주문을 받았으면 그 주문(개별 조회 형식, trades 포함)을 돌려준다. ``order_not_found`` 면 접수되지
        않은 것이므로 원래 네트워크 오류를 다시 던진다 (확인 자체가 실패하면 status 없는 BrokerError 로 감싼다 —
        엔진은 이를 "결과 미확인" 으로 보고 거래소 기준으로 다시 확정한다).
        """
        logger.warning(
            "Upbit 주문 응답 유실 (%s), identifier=%s 로 접수 여부 확인: %s", symbol, identifier, cause
        )
        try:
            raw = self._private("GET", "/v1/order", params={"identifier": identifier}, order_context=True)
        except BrokerError as e:
            lookup_error: BrokerError = e
        else:
            if isinstance(raw, dict) and raw.get("uuid"):
                return raw
            raise BrokerError(
                f"Upbit 주문 응답 유실 후 identifier={identifier} 조회 응답 형식 오류: {raw!r} (원인: {cause})",
                payload=raw,
            ) from cause
        name, _ = _error_fields(lookup_error.payload)
        if isinstance(lookup_error, OrderError) and (
            lookup_error.status_code == 404 or name == "order_not_found"
        ):
            logger.warning("identifier=%s 주문이 거래소에 없음 → 접수되지 않은 것으로 처리", identifier)
            raise cause
        raise BrokerError(
            f"Upbit 주문 응답 유실 후 identifier={identifier} 접수 여부 확인 실패: {lookup_error} (원인: {cause})",
            payload=lookup_error.payload,
        ) from cause

    def _parse_order(self, raw: dict[str, Any], requested_quantity: float | None = None) -> Order:
        order_id = str(raw.get("uuid") or "")
        side = OrderSide.BUY if raw.get("side") == "bid" else OrderSide.SELL
        ord_type = str(raw.get("ord_type") or "")
        otype = OrderType.LIMIT if ord_type == "limit" else OrderType.MARKET
        volume = float(raw["volume"]) if raw.get("volume") not in (None, "") else None
        executed = _to_float(raw.get("executed_volume"))
        trades = raw.get("trades") if isinstance(raw.get("trades"), list) else []

        average_price: float | None = None
        updated_at: datetime | None = None
        if trades:
            traded_volume = sum(_to_float(t.get("volume")) for t in trades)
            traded_funds = sum(_to_float(t.get("funds")) for t in trades)
            if traded_volume > 0:
                average_price = traded_funds / traded_volume
            times = [ts for ts in (_parse_time(t.get("created_at")) for t in trades) if ts is not None]
            if times:
                updated_at = max(times)
        elif executed > 0 and ord_type == "limit" and raw.get("price") not in (None, ""):
            average_price = float(raw["price"])

        if requested_quantity is None:
            requested_quantity = self._requested_qty.get(order_id)
        if volume is not None:
            quantity = volume
        elif requested_quantity:
            quantity = requested_quantity
        else:
            quantity = executed

        price = float(raw["price"]) if ord_type == "limit" and raw.get("price") not in (None, "") else None
        status = self._map_state(raw.get("state"), ord_type, volume, executed)
        return Order(
            id=order_id,
            symbol=str(raw.get("market") or ""),
            side=side,
            type=otype,
            quantity=quantity,
            price=price,
            status=status,
            filled_quantity=executed,
            average_price=average_price,
            fee=_to_float(raw.get("paid_fee")),
            created_at=_parse_time(raw.get("created_at")) or utcnow(),
            updated_at=updated_at,
            raw=dict(raw),
        )

    @staticmethod
    def _map_state(state: Any, ord_type: str, volume: float | None, executed: float) -> OrderStatus:
        """wait/watch → OPEN(일부 체결이면 PARTIALLY_FILLED), done → FILLED(volume 미달이면 PARTIALLY_FILLED), cancel → CANCELED.

        단, 시장가 주문(``price``/``market``)의 ``cancel`` 은 체결 후 남은 잔량이 취소되며 **종료된** 상태다
        (공식 문서: 시장가 매수는 체결 후 잔량이 생기면 ``cancel``, 딱 맞아떨어지면 ``done``). 체결 수량이 있으면
        ``done`` 과 같이 판정한다 — 금액 주문(volume 없음)은 FILLED, 수량 주문은 volume 미달이면 PARTIALLY_FILLED.
        지정가의 ``cancel`` 은 체결분이 있어도 CANCELED (사용자/엔진이 취소한 주문).
        """
        if state in ("wait", "watch"):
            return OrderStatus.OPEN if executed <= 0 else OrderStatus.PARTIALLY_FILLED
        if state == "cancel" and ord_type in ("price", "market") and executed > 0:
            state = "done"
        if state == "done":
            if volume is not None and volume > 0 and executed + 1e-12 < volume:
                return OrderStatus.PARTIALLY_FILLED
            return OrderStatus.FILLED
        if state == "cancel":
            return OrderStatus.CANCELED
        logger.warning("알 수 없는 Upbit 주문 상태: %r", state)
        return OrderStatus.PENDING

    # ------------------------------------------------------------------ 내부: 캔들
    @staticmethod
    def _candle_path(interval: str) -> str:
        try:
            return CANDLE_PATHS[interval]
        except KeyError as e:
            raise DataError(
                f"Upbit 가 지원하지 않는 캔들 간격: {interval!r} (가능: {', '.join(CANDLE_PATHS)})"
            ) from e

    @staticmethod
    def _parse_candle(item: dict[str, Any]) -> Candle:
        try:
            ts = datetime.fromisoformat(str(item["candle_date_time_utc"])).replace(tzinfo=timezone.utc)
            return Candle(
                timestamp=ts,
                open=float(item["opening_price"]),
                high=float(item["high_price"]),
                low=float(item["low_price"]),
                close=float(item["trade_price"]),
                volume=float(item["candle_acc_trade_volume"]),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise DataError(f"Upbit 캔들 응답 파싱 실패: {e}") from e

    # ------------------------------------------------------------------ 내부: 인증/전송
    def _require_credentials(self) -> None:
        if not self.has_credentials:
            raise AuthenticationError(
                "Upbit 비공개 API(잔고/주문) 호출에는 UPBIT_ACCESS_KEY 와 UPBIT_SECRET_KEY 환경변수가 필요합니다 "
                "(.env 파일 또는 export 로 설정)"
            )

    def _jwt(self, params: dict[str, Any] | None) -> str:
        """공식 가이드와 동일한 JWT. params 가 있으면 query_hash(SHA512 hex) 포함."""
        self._require_credentials()
        payload: dict[str, Any] = {"access_key": self._access_key, "nonce": str(uuid.uuid4())}
        if params:
            payload["query_hash"] = query_hash(params)
            payload["query_hash_alg"] = "SHA512"
        with warnings.catch_warnings():
            if _KEY_LENGTH_WARNING is not None:
                warnings.simplefilter("ignore", _KEY_LENGTH_WARNING)
            token = jwt.encode(payload, self._secret_key, algorithm=self.jwt_algorithm)
        return token if isinstance(token, str) else token.decode("utf-8")

    def _throttle(self, group: str) -> None:
        limiter = self._limiters.get(group)
        if limiter is not None:
            waited = limiter.wait()
            if waited > 0:
                logger.debug("Upbit %s 그룹 호출 간격 조절 %.3fs", group, waited)

    def _on_response(self, response: requests.Response, *args: Any, **kwargs: Any) -> None:
        """requests 응답 훅: Remaining-Req 헤더를 기록하고 잔여 0 이면 다음 초까지 대기시킨다."""
        header = response.headers.get("Remaining-Req")
        if not header:
            return
        info = parse_remaining_req(header)
        group = info.get("group")
        if not group:
            return
        self.remaining_req[group] = info
        if info.get("sec") == 0:
            limiter = self._limiters.get(group)
            if limiter is not None:
                limiter.defer(1.0 - (time.time() % 1.0))

    def _public(self, path: str, params: dict[str, Any], *, group: str) -> Any:
        self._throttle(group)
        try:
            return self._client.get(path, params=params)
        except BrokerError as e:
            raise self._translate(e, order_context=False) from e

    def _private(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        group: str = "default",
        order_context: bool = False,
    ) -> Any:
        """인증 요청. GET/DELETE 는 해시한 쿼리 문자열을 그대로 URL 에 붙여 보내고, POST 는 JSON 본문.

        재시도 규칙은 ``HttpClient`` 와 같다 (GET 만 429/5xx/네트워크 오류를 지수 백오프로 재시도, POST/DELETE 는 1회).
        다만 nonce 는 요청마다 새 값이어야 하므로(재사용 시 401 ``nonce_used``) **시도마다 JWT 를 새로 만든다** —
        ``HttpClient.request`` 는 같은 헤더로 재시도하기 때문에 세션을 직접 쓴다 (KIS 어댑터와 같은 방식).
        """
        self._require_credentials()
        method = method.upper()
        hash_params = params if params is not None else body
        client = self._client
        url = f"{client.base_url}{path}"
        if params:
            url = f"{url}?{urlencode(params, doseq=True)}"
        attempts = client.max_retries + 1 if method in ("GET", "HEAD") else 1
        try:
            for attempt in range(attempts):
                self._throttle(group)
                headers = {"Authorization": f"Bearer {self._jwt(hash_params)}"}
                try:
                    resp = client.session.request(
                        method, url, json=body, headers=headers, timeout=client.timeout
                    )
                except requests.RequestException as e:
                    if attempt < attempts - 1:
                        logger.warning(
                            "%s %s 네트워크 오류, 재시도 %d/%d: %s",
                            method,
                            path,
                            attempt + 1,
                            attempts - 1,
                            e,
                        )
                        client._sleep(attempt)
                        continue
                    raise BrokerError(f"{method} {url} 네트워크 오류: {e}") from e
                if resp.status_code in RETRY_STATUS and attempt < attempts - 1:
                    logger.warning(
                        "%s %s -> %s, 재시도 %d/%d", method, path, resp.status_code, attempt + 1, attempts - 1
                    )
                    client._sleep(attempt, retry_after=resp.headers.get("Retry-After"))
                    continue
                return client._handle(resp, method, url)
        except BrokerError as e:
            translated = self._translate(e, order_context=order_context)
            if translated is e:
                raise
            raise translated from e
        raise BrokerError(f"{method} {url} 실패: 재시도 횟수 설정 오류 (max_retries={client.max_retries})")

    def _accounts(self) -> list[dict[str, Any]]:
        raw = self._private("GET", "/v1/accounts")
        if not isinstance(raw, list):
            raise BrokerError(f"Upbit 계좌 응답 형식 오류: {type(raw).__name__}")
        return [item for item in raw if isinstance(item, dict)]

    @staticmethod
    def _translate(exc: BrokerError, *, order_context: bool) -> BrokerError:
        """HttpClient 가 던진 BrokerError 를 업비트 오류 name 기준으로 세분화한다.

        공식 가이드대로 HTTP 상태만으로 분기하지 않고 ``error.name`` 을 먼저 본다: 예를 들어 주문 API 의
        403 ``market_offline``(시스템 점검) 은 인증 오류가 아니라 주문 오류다. 이름을 모르면 상태 코드로 분류한다.
        """
        status = exc.status_code
        if status is None:  # 네트워크 오류 등: 그대로
            return exc
        name, message = _error_fields(exc.payload)
        detail = f"[{name}] {message}".strip() if name else str(exc.payload)[:300]
        msg = f"Upbit API 오류 (HTTP {status}): {detail}"
        kwargs: dict[str, Any] = {"status_code": status, "payload": exc.payload}
        if name and name.startswith(_INSUFFICIENT_FUNDS_PREFIX):
            return InsufficientFunds(msg, **kwargs)
        if name in _RATE_LIMIT_ERROR_NAMES:
            return RateLimitError(msg, **kwargs)
        if name in _ORDER_ERROR_NAMES:
            return OrderError(msg, **kwargs)
        if (name in _AUTH_ERROR_NAMES) or status in (401, 403):
            return AuthenticationError(msg, **kwargs)
        if status in (429, 418):
            return RateLimitError(msg, **kwargs)
        if order_context and 400 <= status < 500:
            return OrderError(msg, **kwargs)
        return BrokerError(msg, **kwargs)


__all__ = [
    "CANDLE_PATHS",
    "KRW_MIN_ORDER_VALUE",
    "KRW_TICK_TABLE",
    "RATE_LIMITS",
    "UpbitBroker",
    "build_query_string",
    "krw_tick_size",
    "parse_remaining_req",
    "query_hash",
    "tick_size",
    "usdt_tick_size",
]
