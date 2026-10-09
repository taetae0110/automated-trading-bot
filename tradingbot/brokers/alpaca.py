"""Alpaca(미국 주식) 브로커 어댑터.

공식 문서(https://docs.alpaca.markets , 2026-10 기준) 로 검증한 사실:

- 베이스 URL: 모의투자 ``https://paper-api.alpaca.markets``, 실거래 ``https://api.alpaca.markets``,
  시세 ``https://data.alpaca.markets`` (docs/authentication, docs/paper-trading).
- 인증 헤더: ``APCA-API-KEY-ID`` / ``APCA-API-SECRET-KEY`` (docs/authentication). 시세 API 도 같은 키가 필요하다.
- ``GET /v2/account`` → ``{cash, equity, buying_power, non_marginable_buying_power, currency: "USD", status,
  trading_blocked, ...}`` (숫자는 문자열). ``cash`` 는 현금 잔고, ``equity = cash + long_market_value +
  short_market_value``, ``non_marginable_buying_power`` 는 미체결 주문을 뺀 비마진 매수 여력 (reference/getaccount-1).
- ``GET /v2/assets/{symbol_or_asset_id}`` → Asset ``{id, class, exchange, symbol, name, status, tradable, marginable,
  shortable, easy_to_borrow, fractionable, ...}``. docs/fractional-trading: 소수점 주문 전에 ``fractionable: true``
  를 확인해야 하며, 아니면 ``"requested asset is not fractionable"`` 로 거부된다 (약 2,000 종목만 소수점 가능).
- ``GET /v2/positions`` → ``[{symbol, qty, qty_available, avg_entry_price, side: "long", market_value,
  current_price, asset_class, ...}]`` (숫자는 문자열).
- ``POST /v2/orders`` 본문 ``{symbol, qty | notional, side, type(market|limit|stop|stop_limit|trailing_stop),
  time_in_force(day|gtc|opg|cls|ioc|fok), limit_price, client_order_id(<=128), extended_hours}``.
  ``qty`` 는 **market + day 주문에서만 소수(최대 9자리)** 가능. 403 = 매수 여력/보유 주식 부족,
  422 = 파라미터 오류 (예 ``{"code": 42210000, "message": "invalid limit_price 290.123. sub-penny increment ..."}``).
  지정가 $1 이상은 소수 2자리, $1 미만은 4자리까지 (docs/orders-at-alpaca).
- 주문 응답(Order): ``id, client_order_id, status, symbol, qty, filled_qty, filled_avg_price, limit_price, type,
  side, time_in_force, created_at, updated_at, submitted_at, filled_at, ...``.
  status: new, partially_filled, filled, done_for_day, canceled, expired, replaced, pending_cancel,
  pending_replace, accepted, pending_new, accepted_for_bidding, stopped, rejected, suspended, calculated, held.
- ``GET /v2/orders?status=open|closed|all&limit(<=500, 기본 50)&symbols=A,B&direction=asc|desc``,
  ``GET /v2/orders/{order_id}``, ``DELETE /v2/orders/{order_id}`` → 204 (취소 불가 상태면 422).
- ``GET /v2/clock`` → ``{timestamp, is_open, next_open, next_close}`` (RFC3339, ET 오프셋 포함).
- ``GET /v2/stocks/{symbol}/bars?timeframe=[1-59]Min|[1-23]Hour|1Day|1Week&start&end&limit(기본 1000, 최대 10000)
  &adjustment=raw|split|dividend|spin-off|all&feed=iex|sip|otc|boats&sort=asc|desc&page_token`` →
  ``{"bars": [{t, o, h, l, c, v, n, vw}], "symbol", "next_page_token"}``. start/end 는 **포함(inclusive)**,
  start 기본값은 당일 시작이므로 반드시 넘겨야 한다. 기본 정렬 asc(오래된→최신).
- ``GET /v2/stocks/{symbol}/trades/latest?feed=`` → ``{"symbol", "trade": {t, p, s, x, c, i, z}}``,
  ``GET /v2/stocks/{symbol}/quotes/latest?feed=`` → ``{"symbol", "quote": {t, ap, as, ax, bp, bs, bx, c, z}}``.
  (feed: iex | sip | delayed_sip | otc | boats | overnight)
- 오류: 401 인증 실패, 403 권한/매수여력 부족, 422 입력 오류, 429 레이트리밋(``X-RateLimit-*`` 헤더, 200회/분).

수량 정책 (round_quantity)
- 정규장(``is_market_open``) 이고 종목이 ``fractionable`` 이면 시장가 소수점 주식(소수 9자리 내림)을 쓴다.
  종목 정보는 ``GET /v2/assets/{symbol}`` 로 심볼당 한 번 조회해 캐시한다 (조회 실패 시 소수점 가능으로 간주하고 경고).
- 그 외(장외, 지정가, 비(非)fractionable 종목)는 정수 주로 내림한다. 지정가는 항상 정수 주 (보수적 정책: 소수
  지정가는 거래소가 day 주문으로만 허용함).
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any

from tradingbot.brokers.base import BaseBroker
from tradingbot.config import AppConfig, Credentials
from tradingbot.exceptions import (
    AuthenticationError,
    BrokerError,
    ConfigError,
    InsufficientFunds,
    OrderError,
    RateLimitError,
)
from tradingbot.models import (
    INTERVAL_SECONDS,
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
from tradingbot.utils.http import HttpClient
from tradingbot.utils.timeutil import is_nyse_open

logger = logging.getLogger(__name__)

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
LIVE_BASE_URL = "https://api.alpaca.markets"
DATA_BASE_URL = "https://data.alpaca.markets"

HEADER_KEY_ID = "APCA-API-KEY-ID"
HEADER_SECRET = "APCA-API-SECRET-KEY"

#: 봇 interval → Alpaca timeframe. ([1-59]Min, [1-23]Hour, 1Day, 1Week)
TIMEFRAMES: dict[str, str] = {
    "1m": "1Min",
    "3m": "3Min",
    "5m": "5Min",
    "10m": "10Min",
    "15m": "15Min",
    "30m": "30Min",
    "1h": "1Hour",
    "4h": "4Hour",
    "1d": "1Day",
    "1w": "1Week",
}
#: bars 요청 1회 최대 개수 (문서: limit 최대 10000)
BARS_PAGE_LIMIT = 10_000
#: bars 페이지네이션 안전 상한
MAX_BARS_PAGES = 20
#: 주식 봉은 주말/휴장/장외 시간에 비어 있으므로 limit*interval 보다 넉넉히 거슬러 올라간다.
LOOKBACK_FACTOR_DAILY = 2.0
LOOKBACK_FACTOR_INTRADAY = 6.0
LOOKBACK_PADDING = timedelta(days=14)
#: /v2/clock 캐시 유효 시간 (초)
CLOCK_CACHE_TTL = 30.0
#: 미체결 주문 조회 한도 (문서 최대 500)
OPEN_ORDERS_LIMIT = 500
#: 소수점 주식 최대 소수 자리 (문서: qty/notional 최대 9자리)
FRACTIONAL_DECIMALS = 9
#: Alpaca 소수점 주식 최소 주문 금액 (docs/fractional-trading: "as little as $1")
MIN_ORDER_VALUE_USD = 1.0
#: 403 응답의 대표 오류 코드 (예: "insufficient buying power"). 같은 코드가 다른 403 메시지에도 쓰인다.
FORBIDDEN_ERROR_CODE = 40310000

_QTY_STEP = Decimal(1).scaleb(-FRACTIONAL_DECIMALS)
_CENT = Decimal("0.01")
_SUB_DOLLAR_TICK = Decimal("0.0001")

#: Alpaca 주문 status → OrderStatus
STATUS_MAP: dict[str, OrderStatus] = {
    "new": OrderStatus.OPEN,
    "accepted": OrderStatus.OPEN,
    "pending_new": OrderStatus.OPEN,
    "accepted_for_bidding": OrderStatus.OPEN,
    "pending_cancel": OrderStatus.OPEN,
    "pending_replace": OrderStatus.OPEN,
    "done_for_day": OrderStatus.OPEN,
    "stopped": OrderStatus.OPEN,
    "suspended": OrderStatus.OPEN,
    "calculated": OrderStatus.OPEN,
    "held": OrderStatus.OPEN,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "replaced": OrderStatus.CANCELED,
    "expired": OrderStatus.EXPIRED,
    "rejected": OrderStatus.REJECTED,
}

_FRACTION_RE = re.compile(r"\.(\d+)")


def parse_rfc3339(value: str) -> datetime:
    """RFC3339 (나노초, 'Z', ±HH:MM 오프셋 포함) → UTC aware datetime."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"RFC3339 문자열이 아닙니다: {value!r}")
    s = value.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    # Python fromisoformat 은 소수점 6자리까지만 받는다 → 나노초 절단
    s = _FRACTION_RE.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), s, count=1)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_alpaca_timeframe(interval: str) -> str:
    try:
        return TIMEFRAMES[interval]
    except KeyError as e:
        raise ValueError(
            f"Alpaca 어댑터가 지원하지 않는 캔들 간격: {interval!r} (가능: {', '.join(TIMEFRAMES)})"
        ) from e


def format_quantity(qty: float) -> str:
    """주문 본문용 수량 문자열. 정수면 "5", 소수면 최대 9자리에서 불필요한 0 제거."""
    d = Decimal(str(qty)).quantize(_QTY_STEP, rounding=ROUND_DOWN)
    s = format(d.normalize(), "f")
    return s if s else "0"


def _to_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class AlpacaBroker(BaseBroker):
    """Alpaca Trading API v2 + Market Data API v2 (미국 주식, 롱 전용)."""

    name = "alpaca"
    asset_class = AssetClass.STOCK
    supported_intervals: tuple[str, ...] = tuple(k for k in INTERVAL_SECONDS if k in TIMEFRAMES)

    def __init__(
        self,
        api_key: str | None = None,
        secret_key: str | None = None,
        *,
        paper: bool = True,
        feed: str = "iex",
        client: HttpClient | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._api_key = api_key or None
        self._secret_key = secret_key or None
        self.paper = bool(paper)
        self.feed = (feed or "iex").lower()
        self.base_url = PAPER_BASE_URL if self.paper else LIVE_BASE_URL
        self.data_url = DATA_BASE_URL
        self.timeout = timeout
        self._owns_client = client is None
        self._client = client or HttpClient(
            self.base_url,
            timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": "tradingbot-alpaca/0.1"},
        )
        self._clock: dict[str, Any] | None = None
        self._clock_fetched_at: float | None = None
        #: 심볼 → Asset (``GET /v2/assets/{symbol}``). fractionable 여부는 거의 바뀌지 않으므로 프로세스 수명 동안 캐시.
        self._assets: dict[str, dict[str, Any]] = {}

    @classmethod
    def from_config(cls, config: AppConfig) -> AlpacaBroker:
        """환경변수(ALPACA_API_KEY / ALPACA_SECRET_KEY 또는 APCA_API_KEY_ID / APCA_API_SECRET_KEY)에서 키를 읽는다."""
        creds = Credentials.from_env()
        extra = config.broker.extra or {}
        feed = str(extra.get("feed") or "iex")
        try:
            timeout = float(extra.get("timeout") or 10.0)
        except (TypeError, ValueError) as e:
            raise ConfigError(f"broker.extra.timeout 이 올바르지 않습니다: {extra.get('timeout')!r}") from e
        broker = cls(
            creds.alpaca_api_key,
            creds.alpaca_secret_key,
            paper=bool(config.broker.sandbox),
            feed=feed,
            timeout=timeout,
        )
        if not broker.has_credentials:
            logger.warning(
                "Alpaca API 키가 없습니다 (ALPACA_API_KEY / ALPACA_SECRET_KEY). 시세 조회도 키가 필요합니다"
            )
        if not broker.paper:
            logger.warning("Alpaca 실거래 서버(%s)를 사용합니다", LIVE_BASE_URL)
        return broker

    # ------------------------------------------------------------------ 속성/메타
    @property
    def has_credentials(self) -> bool:
        return bool(self._api_key and self._secret_key)

    def quote_currency(self, symbol: str) -> str:
        return "USD"

    def base_currency(self, symbol: str) -> str:
        return symbol

    def min_order_value(self, symbol: str) -> float:
        return MIN_ORDER_VALUE_USD

    def round_quantity(self, symbol: str, quantity: float, *, fractional: bool | None = None) -> float:
        """정규장 중이고 종목이 fractionable 이면 소수 9자리 내림(소수점 주식), 아니면 정수 주로 내림.

        ``fractional`` 을 주면 장 운영 여부/종목 정보 조회 없이 그 정책을 강제한다.
        """
        if quantity is None or quantity <= 0:
            return 0.0
        if fractional is None:
            fractional = self.is_market_open() and self._is_fractionable(symbol)
        if fractional:
            return float(Decimal(str(quantity)).quantize(_QTY_STEP, rounding=ROUND_DOWN))
        return float(math.floor(quantity))

    def round_price(self, symbol: str, price: float) -> float:
        """$1 이상은 센트 단위, $1 미만은 0.0001 단위로 반올림 (문서의 지정가 소수 자릿수 규칙)."""
        if price is None or price <= 0:
            raise OrderError(f"가격은 0보다 커야 합니다: {price}")
        p = Decimal(str(price))
        tick = _CENT if p >= 1 else _SUB_DOLLAR_TICK
        return float(p.quantize(tick, rounding=ROUND_HALF_UP))

    def close(self) -> None:
        if self._owns_client:
            try:
                self._client.session.close()
            except Exception as e:  # noqa: BLE001
                logger.debug("세션 종료 실패 (무시): %s", e)

    # ------------------------------------------------------------------ HTTP
    def _require_credentials(self) -> None:
        if not self.has_credentials:
            raise AuthenticationError(
                "Alpaca API 호출에는 ALPACA_API_KEY 와 ALPACA_SECRET_KEY 환경변수가 필요합니다 "
                "(.env 파일 또는 export 로 설정)"
            )

    def _auth_headers(self) -> dict[str, str]:
        self._require_credentials()
        return {HEADER_KEY_ID: str(self._api_key), HEADER_SECRET: str(self._secret_key)}

    def _request(self, method: str, url: str, *, order: bool = False, **kw: Any) -> Any:
        """인증 헤더를 붙여 호출하고 HttpClient 의 예외를 Alpaca 의미에 맞게 재분류한다."""
        headers = self._auth_headers()
        try:
            return self._client.request(method, url, headers=headers, **kw)
        except RateLimitError:
            raise
        except BrokerError as e:
            raise self._translate(e, order=order) from e

    @staticmethod
    def _translate(err: BrokerError, *, order: bool) -> BrokerError:
        status = err.status_code
        payload = err.payload
        code = payload.get("code") if isinstance(payload, Mapping) else None
        message = str(payload.get("message", "")) if isinstance(payload, Mapping) else str(payload or "")
        lowered = message.lower()
        if status == 403 and ("buying power" in lowered or "insufficient" in lowered):
            # 예: {"code": 40310000, "message": "insufficient buying power"} — 코드 40310000 은 다른 403 메시지에도
            # 쓰이므로 메시지로 구분한다.
            return InsufficientFunds(
                f"Alpaca 매수 여력/보유 주식 부족: {message} (code={code})",
                status_code=status,
                payload=payload,
            )
        if status in (401, 403):
            return AuthenticationError(
                f"Alpaca 인증/권한 오류 (HTTP {status}): {message or err}",
                status_code=status,
                payload=payload,
            )
        if status == 429:
            return RateLimitError(
                f"Alpaca 레이트리밋 초과: {message or err}", status_code=status, payload=payload
            )
        if order and status in (400, 404, 422):
            return OrderError(
                f"Alpaca 주문 오류 (HTTP {status}): {message or err}", status_code=status, payload=payload
            )
        if status == 422:
            return OrderError(
                f"Alpaca 입력 오류 (HTTP {status}): {message or err}", status_code=status, payload=payload
            )
        return err

    def _trading(self, method: str, path: str, *, order: bool = False, **kw: Any) -> Any:
        return self._request(method, f"{self.base_url}{path}", order=order, **kw)

    def _data(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", f"{self.data_url}{path}", params=params)

    # ------------------------------------------------------------------ 시세
    def get_candles(
        self,
        symbol: str,
        interval: str,
        limit: int = 200,
        end: datetime | None = None,
        include_partial: bool = False,
    ) -> list[Candle]:
        timeframe = to_alpaca_timeframe(interval)
        if limit <= 0:
            raise ValueError(f"limit 은 1 이상이어야 합니다: {limit}")
        step_sec = interval_to_seconds(interval)
        step = timedelta(seconds=step_sec)
        now = utcnow()
        end_utc = ensure_utc(end) if end is not None else now
        factor = LOOKBACK_FACTOR_DAILY if interval in ("1d", "1w") else LOOKBACK_FACTOR_INTRADAY
        start_utc = end_utc - step * int(math.ceil((limit + 1) * factor)) - LOOKBACK_PADDING
        want = limit + 1  # 진행 중 봉을 제외해도 limit 개가 남도록

        params: dict[str, Any] = {
            "timeframe": timeframe,
            "start": start_utc.isoformat().replace("+00:00", "Z"),
            "end": end_utc.isoformat().replace("+00:00", "Z"),
            "limit": min(want, BARS_PAGE_LIMIT),
            "adjustment": "all",
            "feed": self.feed,
            "sort": "desc",
        }
        rows: dict[datetime, Mapping[str, Any]] = {}
        page_token: str | None = None
        for _ in range(MAX_BARS_PAGES):
            page_params = dict(params)
            page_params["limit"] = min(want - len(rows), BARS_PAGE_LIMIT)
            if page_token:
                page_params["page_token"] = page_token
            payload = self._data(f"/v2/stocks/{symbol}/bars", params=page_params)
            if not isinstance(payload, Mapping):
                raise BrokerError(f"Alpaca bars 응답 형식 오류: {payload!r}")
            bars = payload.get("bars") or []
            if isinstance(bars, Mapping):  # 멀티 심볼 형식 방어
                bars = bars.get(symbol) or []
            for bar in bars:
                if not isinstance(bar, Mapping) or "t" not in bar:
                    raise BrokerError(f"Alpaca bar 형식 오류: {bar!r}")
                rows[parse_rfc3339(str(bar["t"]))] = bar
            page_token = payload.get("next_page_token") or None
            if not page_token or len(rows) >= want or not bars:
                break

        candles: list[Candle] = []
        for ts in sorted(rows):
            if ts >= end_utc:
                continue
            if not include_partial and ts + step > now:
                continue
            bar = rows[ts]
            try:
                candles.append(
                    Candle(
                        timestamp=ts,
                        open=float(bar["o"]),
                        high=float(bar["h"]),
                        low=float(bar["l"]),
                        close=float(bar["c"]),
                        volume=_to_float(bar.get("v")),
                    )
                )
            except (KeyError, TypeError, ValueError) as e:
                raise BrokerError(f"Alpaca bar 형식 오류: {dict(bar)!r}") from e
        return candles[-limit:]

    def get_ticker(self, symbol: str) -> float:
        """최근 체결가 (``trades/latest``). 없으면 호가 중간값(``quotes/latest``)."""
        params = {"feed": self.feed}
        trade_payload = self._data(f"/v2/stocks/{symbol}/trades/latest", params=params)
        trade = trade_payload.get("trade") if isinstance(trade_payload, Mapping) else None
        price = _to_float(trade.get("p")) if isinstance(trade, Mapping) else 0.0
        if price > 0:
            return price
        quote_payload = self._data(f"/v2/stocks/{symbol}/quotes/latest", params=params)
        quote = quote_payload.get("quote") if isinstance(quote_payload, Mapping) else None
        if isinstance(quote, Mapping):
            bid, ask = _to_float(quote.get("bp")), _to_float(quote.get("ap"))
            if bid > 0 and ask > 0:
                return (bid + ask) / 2
            if ask > 0 or bid > 0:
                return ask or bid
        raise BrokerError(f"Alpaca {symbol} 현재가를 알 수 없습니다 (최근 체결/호가 없음)")

    # ------------------------------------------------------------------ 계좌
    def get_account(self) -> dict[str, Any]:
        payload = self._trading("GET", "/v2/account")
        if not isinstance(payload, Mapping):
            raise BrokerError(f"Alpaca account 응답 형식 오류: {payload!r}")
        return dict(payload)

    def get_balances(self) -> dict[str, Balance]:
        """``{"USD": Balance(total=cash, available=min(cash, non_marginable_buying_power))}``.

        ``total`` 은 다른 어댑터와 같이 **현금 잔고**(``cash``) 다 — 포지션 평가액이 섞인 ``equity`` 는
        ``get_equity()`` 가 준다 (total 에 equity 를 두면 ``locked`` 가 보유 주식 평가액이 되고 엔진의 fallback
        equity 가 포지션을 이중 계산한다). ``available`` 은 미체결 주문에 묶인 금액을 뺀 비마진 매수 여력
        (``non_marginable_buying_power``, 없으면 ``buying_power``) 과 현금 중 작은 값이라 ``locked`` 가
        주문에 묶인 현금이 된다.
        """
        acct = self.get_account()
        currency = str(acct.get("currency") or "USD")
        cash = _to_float(acct.get("cash"))
        available = cash
        for key in ("non_marginable_buying_power", "buying_power"):
            raw = acct.get(key)
            if raw not in (None, ""):
                available = min(cash, _to_float(raw))
                break
        return {currency: Balance(currency=currency, total=cash, available=available)}

    def get_asset(self, symbol: str) -> dict[str, Any]:
        """``GET /v2/assets/{symbol}`` (Asset 구조: tradable, fractionable, status, ...). 심볼별로 캐시한다."""
        cached = self._assets.get(symbol)
        if cached is not None:
            return cached
        payload = self._trading("GET", f"/v2/assets/{symbol}")
        if not isinstance(payload, Mapping):
            raise BrokerError(f"Alpaca asset 응답 형식 오류: {payload!r}")
        asset = dict(payload)
        self._assets[symbol] = asset
        return asset

    def _is_fractionable(self, symbol: str) -> bool:
        """종목의 ``fractionable`` 플래그. 조회 실패 시 소수점 가능으로 간주(이전 정책 유지) 하고 경고한다."""
        try:
            return bool(self.get_asset(symbol).get("fractionable"))
        except BrokerError as e:
            logger.warning("Alpaca %s 종목 정보 조회 실패, 소수점 주문 가능으로 간주합니다: %s", symbol, e)
            return True

    def get_positions(self) -> dict[str, Position]:
        payload = self._trading("GET", "/v2/positions")
        if not isinstance(payload, list):
            raise BrokerError(f"Alpaca positions 응답 형식 오류: {payload!r}")
        positions: dict[str, Position] = {}
        for p in payload:
            if not isinstance(p, Mapping):
                continue
            qty = _to_float(p.get("qty"))
            side = str(p.get("side") or "long").lower()
            if qty <= 0 or side != "long":
                if side != "long":
                    logger.warning("Alpaca 공매도 포지션 %s 는 무시합니다 (롱 전용)", p.get("symbol"))
                continue
            symbol = str(p.get("symbol"))
            positions[symbol] = Position(
                symbol=symbol,
                quantity=qty,
                average_price=_to_float(p.get("avg_entry_price")),
                meta={
                    "qty_available": _to_float(p.get("qty_available"), qty),
                    "market_value": _to_float(p.get("market_value")),
                    "current_price": _to_float(p.get("current_price")),
                    "asset_class": p.get("asset_class"),
                },
            )
        return positions

    def get_equity(self, symbols: list[str] | None = None) -> float:
        """계좌 equity (현금 + 포지션 평가액) 를 그대로 사용한다."""
        return _to_float(self.get_account().get("equity"))

    # ------------------------------------------------------------------ 주문
    def place_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        order_type: OrderType = OrderType.MARKET,
        price: float | None = None,
    ) -> Order:
        if order_type == OrderType.STOP:
            raise OrderError(
                "Alpaca 어댑터는 STOP 주문을 지원하지 않습니다 (엔진/백테스터가 폴링으로 흉내냅니다)"
            )
        if order_type not in (OrderType.MARKET, OrderType.LIMIT):
            raise OrderError(f"지원하지 않는 주문 유형: {order_type}")
        if quantity is None or quantity <= 0:
            raise OrderError(f"주문 수량은 0보다 커야 합니다: {quantity}")
        self._require_credentials()

        body: dict[str, Any] = {
            "symbol": symbol,
            "side": side.value,
            "type": order_type.value,
            "time_in_force": "day",
        }
        if order_type == OrderType.LIMIT:
            if price is None or price <= 0:
                raise OrderError("지정가 주문에는 price 가 필요합니다")
            qty = float(math.floor(quantity))
            if qty < 1:
                raise OrderError(f"지정가 주문은 정수 주 단위입니다 (요청 {quantity})")
            limit_price = self.round_price(symbol, price)
            body["limit_price"] = format(Decimal(str(limit_price)).normalize(), "f")
        else:
            qty = self.round_quantity(symbol, quantity)
            if qty <= 0:
                raise OrderError(f"{symbol} 수량 {quantity} 는 주문 가능 최소 단위보다 작습니다")
        body["qty"] = format_quantity(qty)
        logger.info(
            "Alpaca 주문 %s %s %s %s%s",
            body["type"],
            body["side"],
            symbol,
            body["qty"],
            f" @ {body['limit_price']}" if "limit_price" in body else "",
        )
        payload = self._trading("POST", "/v2/orders", order=True, json=body)
        return self._parse_order(payload)

    def cancel_order(self, order_id: str, symbol: str | None = None) -> bool:
        """취소 요청 성공(204) True. 이미 종료되어 취소 불가(422) 면 False. 없는 주문(404) 은 OrderError."""
        try:
            self._trading("DELETE", f"/v2/orders/{order_id}", order=True)
        except OrderError as e:
            if e.status_code == 422:
                logger.info("Alpaca 주문 %s 취소 불가(이미 종료): %s", order_id, e)
                return False
            raise
        return True

    def get_order(self, order_id: str, symbol: str | None = None) -> Order:
        payload = self._trading("GET", f"/v2/orders/{order_id}", order=True)
        return self._parse_order(payload)

    def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        params: dict[str, Any] = {"status": "open", "limit": OPEN_ORDERS_LIMIT, "direction": "desc"}
        if symbol:
            params["symbols"] = symbol
        payload = self._trading("GET", "/v2/orders", params=params)
        if not isinstance(payload, list):
            raise BrokerError(f"Alpaca orders 응답 형식 오류: {payload!r}")
        return [self._parse_order(o) for o in payload]

    # ------------------------------------------------------------------ 장 운영
    def is_market_open(self) -> bool:
        """``/v2/clock`` 의 is_open (30초 캐시). 조회 실패 시 timeutil.is_nyse_open 으로 대체."""
        clock = self._get_clock()
        if clock is not None:
            return bool(clock.get("is_open"))
        return is_nyse_open()

    def _get_clock(self) -> dict[str, Any] | None:
        now = time.monotonic()
        if (
            self._clock is not None
            and self._clock_fetched_at is not None
            and now - self._clock_fetched_at < CLOCK_CACHE_TTL
        ):
            return self._clock
        try:
            payload = self._trading("GET", "/v2/clock")
            if not isinstance(payload, Mapping) or "is_open" not in payload:
                raise BrokerError(f"Alpaca clock 응답 형식 오류: {payload!r}")
            self._clock = dict(payload)
            self._clock_fetched_at = now
            return self._clock
        except BrokerError as e:
            logger.warning("Alpaca /v2/clock 조회 실패, 정규장 시간표로 대체: %s", e)
            return None

    @property
    def next_market_open(self) -> datetime | None:
        clock = self._get_clock()
        return parse_rfc3339(str(clock["next_open"])) if clock and clock.get("next_open") else None

    @property
    def next_market_close(self) -> datetime | None:
        clock = self._get_clock()
        return parse_rfc3339(str(clock["next_close"])) if clock and clock.get("next_close") else None

    # ------------------------------------------------------------------ 파싱
    def _parse_order(self, payload: Any) -> Order:
        if not isinstance(payload, Mapping) or not payload.get("id"):
            raise BrokerError(f"Alpaca 주문 응답 형식 오류: {payload!r}")
        raw_side = str(payload.get("side") or "").lower()
        if raw_side not in ("buy", "sell"):
            raise BrokerError(f"Alpaca 주문 {payload.get('id')} 의 side 를 알 수 없습니다: {raw_side!r}")
        raw_type = str(payload.get("type") or payload.get("order_type") or "").lower()
        if raw_type == "market":
            order_type = OrderType.MARKET
        elif raw_type == "limit":
            order_type = OrderType.LIMIT
        else:
            order_type = OrderType.STOP if "stop" in raw_type else OrderType.MARKET

        filled = _to_float(payload.get("filled_qty"))
        quantity = _to_float(payload.get("qty"))
        if quantity <= 0:
            # notional 주문은 qty 가 null → 체결 수량으로 대체
            quantity = filled
        avg = _to_float(payload.get("filled_avg_price"))
        limit_price = _to_float(payload.get("limit_price"))
        status = self._parse_status(payload.get("status"), filled)

        created_raw = payload.get("created_at") or payload.get("submitted_at")
        created_at = parse_rfc3339(str(created_raw)) if created_raw else utcnow()
        updated_raw = payload.get("updated_at") or payload.get("filled_at")
        updated_at = parse_rfc3339(str(updated_raw)) if updated_raw else None

        return Order(
            id=str(payload["id"]),
            symbol=str(payload.get("symbol") or ""),
            side=OrderSide(raw_side),
            type=order_type,
            quantity=quantity,
            price=limit_price if order_type == OrderType.LIMIT and limit_price > 0 else None,
            status=status,
            filled_quantity=filled,
            average_price=avg if avg > 0 else None,
            fee=0.0,  # Alpaca 는 수수료 무료 (규제 수수료는 별도 활동으로 기록됨)
            created_at=created_at,
            updated_at=updated_at,
            raw=dict(payload),
        )

    @staticmethod
    def _parse_status(raw_status: Any, filled: float) -> OrderStatus:
        key = str(raw_status or "").lower()
        if not key:
            return OrderStatus.PENDING
        status = STATUS_MAP.get(key)
        if status is None:
            logger.warning("알 수 없는 Alpaca 주문 상태 %r → PENDING", raw_status)
            return OrderStatus.PENDING
        if status == OrderStatus.OPEN and filled > 0:
            return OrderStatus.PARTIALLY_FILLED
        return status


__all__ = [
    "AlpacaBroker",
    "DATA_BASE_URL",
    "LIVE_BASE_URL",
    "PAPER_BASE_URL",
    "STATUS_MAP",
    "TIMEFRAMES",
    "format_quantity",
    "parse_rfc3339",
    "to_alpaca_timeframe",
]
