"""ccxt 범용 암호화폐 거래소 어댑터 (Binance 등 현물, 롱 전용).

ccxt(https://docs.ccxt.com, 4.5 기준) 의 **통합(unified) API** 만 사용한다. 검증한 사실:

- 거래소 인스턴스: ``ccxt.<exchange_id>({"apiKey", "secret", "password", "enableRateLimit": True, "options": {...}})``.
  생성자는 ``options`` 를 거래소 기본 옵션과 **깊은 병합** 한다 (중첩 dict 의 다른 키는 유지).
  ``set_sandbox_mode(True)`` 는 ``urls["test"]`` 가 있는 거래소만 지원한다. 키가 없으면 ``NotSupported``, 키는 있지만
  값이 ``None`` 인 거래소(bithumb, upbit, kraken, kucoin, bitstamp 등 다수) 는 ``TypeError`` 를 던지며, 어느 쪽이든
  ``urls["api"]`` 는 **실전 서버 그대로** 남는다. 그래서 이 어댑터는 샌드박스를 요청받고 켜지 못하면 ``ConfigError``
  로 생성을 거부한다 (조용히 실전 서버로 내려가지 않는다).
- 마켓: ``load_markets()`` → ``{symbol: market}``. market 구조의 ``base/quote/precision.amount/precision.price``,
  ``limits.amount.min``, ``limits.cost.min`` (Binance 는 ``minNotional``) 을 사용한다.
- 캔들: ``fetch_ohlcv(symbol, timeframe, since(ms), limit)`` → ``[[ts_ms, o, h, l, c, v], ...]`` **오래된→최신**,
  ``since`` 이상의 캔들을 반환하며 마지막 원소는 진행 중인 캔들일 수 있다. Binance timeframes:
  1s,1m,3m,5m,15m,30m,1h,2h,4h,6h,8h,12h,1d,3d,1w,1M (``10m`` 없음).
- 시세: ``fetch_ticker(symbol)["last"]``. 잔고: ``fetch_balance()`` → ``{code: {"free","used","total"}, "free": {...}, ...}``.
- 주문: ``create_order(symbol, type("market"|"limit"), side, amount, price, params)`` →
  통합 주문 구조 ``{id, clientOrderId, timestamp, lastTradeTimestamp, status, symbol, type, side, price, average,
  amount, filled, remaining, cost, fee{cost,currency}, fees[], trades[], info}``. 통합 status 는
  ``open | closed | canceled | expired | rejected`` (Binance 는 ``PARTIALLY_FILLED`` 도 ``open``, PENDING_CANCEL 은 ``canceling``).
  Binance 시장가 매수는 ``params["quoteOrderQty"]`` (금액 기준) 를 지원한다.
  ``createMarketBuyOrderRequiresPrice`` 옵션이 True 인 거래소는 시장가 매수에 ``price`` 를 넘겨야 ccxt 가
  ``amount * price`` 로 총액을 계산한다. ccxt 는 이 옵션을 ``options["createOrder"][...]`` → ``options[...]`` →
  거래소 코드의 기본값 순으로 읽는다 (``Exchange.handle_option``; bitget/bigone/bittrade 는 중첩 키에만 True,
  btse 는 옵션 없이 코드 기본값 True). 값이 True 인데 price 가 없으면 ccxt 는 **요청을 보내기 전에**
  ``InvalidOrder("<id> createOrder() requires the price argument for market buy orders ...")`` 를 던진다.
- ``fetch_open_orders()`` 를 심볼 없이 부르면 Binance 는 기본 옵션 ``fetchOpenOrders.warnWithoutSymbol`` 때문에
  요청 전에 ``ExchangeError`` 를 던진다 (전체 조회 가중치 80 vs 심볼 지정 6). 어댑터는 BaseBroker 계약(심볼 생략
  허용) 을 지키려고 이 경고를 기본 옵션으로 해제한다 — 심볼 없는 조회는 비싸므로 자주 부르지 않는다.
- 정밀도: ``amount_to_precision(symbol, amount)`` 는 **내림(TRUNCATE)** 문자열, 결과가 0 이면 ``InvalidOrder``;
  ``price_to_precision`` 은 반올림(ROUND) 문자열.
- 예외 계층: ``AuthenticationError(PermissionDenied, AccountSuspended) / InsufficientFunds / InvalidOrder(OrderNotFound)
  / BadRequest(BadSymbol)`` ← ``ExchangeError``;  ``RateLimitExceeded / DDoSProtection / RequestTimeout /
  ExchangeNotAvailable`` ← ``NetworkError``;  둘 다 ``BaseError`` 의 하위.

주의
- ccxt 는 선택 의존성이다 (``pip install tradingbot[ccxt]``). 모듈 임포트 시에는 ccxt 를 임포트하지 않고
  ``CCXTBroker.__init__`` 에서 지연 임포트한다. 테스트는 ``exchange=`` 로 가짜 거래소 객체를 주입한다.
- 현물 잔고에는 평균 매수가가 없다. ``get_positions`` 는 잔고 기반 수량만 알려주며 ``average_price`` 는
  0.0(모름) 또는 호출자가 넘긴 ``average_prices`` 값이다. 실제 평균단가는 엔진이 자신의 상태 파일에 보관한다.
"""

from __future__ import annotations

import importlib
import logging
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
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
    TradingBotError,
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
from tradingbot.utils.timeutil import floor_to_interval

logger = logging.getLogger(__name__)

CCXT_INSTALL_HINT = "ccxt 패키지가 필요합니다: pip install tradingbot[ccxt]"

#: 봇 interval → ccxt timeframe. ``10m`` 은 대부분의 거래소(Binance 포함)에 없으므로 지원하지 않는다.
TIMEFRAMES: dict[str, str] = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
    "1w": "1w",
}

#: 잔고에서 포지션을 추출할 때 "현금" 으로 취급하는 결제 통화 (우선순위 순).
DEFAULT_QUOTE_CURRENCIES: tuple[str, ...] = ("USDT", "USDC", "KRW", "USD")

#: fetch_ohlcv 1회 요청 최대 개수 (Binance 1000, 다른 거래소는 더 작을 수 있어 페이지네이션으로 보완).
OHLCV_BATCH_LIMIT = 1000
#: 캔들 페이지네이션 안전 상한 (무한 루프 방지).
MAX_OHLCV_PAGES = 50

#: fetch_balance 응답에서 통화 코드가 아닌 키.
_BALANCE_META_KEYS = frozenset({"info", "timestamp", "datetime", "free", "used", "total", "debt"})

#: 시장가 매수에 price 가 필요한지 정하는 ccxt 옵션 이름.
MARKET_BUY_REQUIRES_PRICE_OPTION = "createMarketBuyOrderRequiresPrice"

#: ccxt 가 요청을 보내기 전에 던지는 "시장가 매수에 price 필요" InvalidOrder 의 공통 문구
#: (bitget/btse/bigone/bittrade 등: ``<id> createOrder() requires the price argument for market buy orders ...``).
_MARKET_BUY_PRICE_RE = re.compile(
    r"createOrder\(\) requires (?:the|a) price(?: and amount)? argument.*market buy", re.IGNORECASE
)

#: 어댑터 기본 ccxt 옵션 (사용자 ``options`` 가 우선): fetchOpenOrders 심볼 생략 경고 해제 (신·구 두 표기).
#: BaseBroker 계약은 심볼 없는 미체결 조회를 허용한다. Binance 는 그 호출의 가중치가 크므로(80 vs 6) 자주 부르지 않는다.
DEFAULT_EXCHANGE_OPTIONS: dict[str, Any] = {
    "warnOnFetchOpenOrdersWithoutSymbol": False,
    "fetchOpenOrders": {"warnWithoutSymbol": False},
}

#: ccxt 예외 클래스 이름 → 봇 예외. MRO(구체적 → 일반) 순으로 처음 매칭되는 항목을 쓴다.
#: 클래스 이름으로 비교하므로 ccxt 가 설치되지 않은 환경(가짜 거래소 주입)에서도 동작한다.
_CCXT_ERROR_MAP: dict[str, type[BrokerError]] = {
    "AuthenticationError": AuthenticationError,
    "PermissionDenied": AuthenticationError,
    "AccountSuspended": AuthenticationError,
    "InsufficientFunds": InsufficientFunds,
    "RateLimitExceeded": RateLimitError,
    "DDoSProtection": RateLimitError,
    "InvalidOrder": OrderError,
    "OrderNotFound": OrderError,
    "NetworkError": BrokerError,
    "ExchangeError": BrokerError,
    "BaseError": BrokerError,
}

#: ccxt 통합 주문 status → OrderStatus. ``open`` 은 체결 수량에 따라 OPEN/PARTIALLY_FILLED 로 나뉜다.
_STATUS_MAP: dict[str, OrderStatus] = {
    "open": OrderStatus.OPEN,
    "canceling": OrderStatus.OPEN,
    "closed": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "cancelled": OrderStatus.CANCELED,
    "expired": OrderStatus.EXPIRED,
    "rejected": OrderStatus.REJECTED,
}


def _import_ccxt() -> Any:
    """ccxt 를 지연 임포트한다. 없으면 설치 안내가 담긴 ConfigError."""
    try:
        return importlib.import_module("ccxt")
    except ImportError as e:
        raise ConfigError(CCXT_INSTALL_HINT) from e


def to_ccxt_timeframe(interval: str) -> str:
    """봇 interval → ccxt timeframe. 지원하지 않으면 ValueError."""
    try:
        return TIMEFRAMES[interval]
    except KeyError as e:
        raise ValueError(
            f"ccxt 어댑터가 지원하지 않는 캔들 간격: {interval!r} (가능: {', '.join(TIMEFRAMES)})"
        ) from e


def translate_ccxt_error(exc: BaseException) -> BrokerError | None:
    """ccxt 예외를 봇 예외로 변환한다. ccxt 계열이 아니면 None."""
    if isinstance(exc, TradingBotError):
        return exc if isinstance(exc, BrokerError) else None
    for klass in type(exc).__mro__:
        mapped = _CCXT_ERROR_MAP.get(klass.__name__)
        if mapped is not None:
            return mapped(f"{type(exc).__name__}: {exc}", payload=str(exc))
    return None


def split_symbol(symbol: str) -> tuple[str, str]:
    """ccxt 통합 심볼 ``BASE/QUOTE`` (``BASE/QUOTE:SETTLE`` 포함) → (base, quote)."""
    base, sep, rest = symbol.partition("/")
    quote = rest.split(":", 1)[0]
    if not sep or not base or not quote:
        raise ValueError(f"ccxt 심볼은 'BASE/QUOTE' 형식이어야 합니다: {symbol!r}")
    return base, quote


def _to_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _merge_options(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    """ccxt options 깊은 병합: 중첩 dict 는 재귀적으로 합치고(다른 키 유지) 말단 값은 ``source`` 가 덮어쓴다."""
    for key, value in source.items():
        if isinstance(value, Mapping):
            existing = target.get(key)
            if not isinstance(existing, dict):
                existing = target[key] = {}
            _merge_options(existing, value)
        else:
            target[key] = value


def is_market_buy_price_error(exc: BaseException) -> bool:
    """ccxt 가 요청 전에 던지는 "시장가 매수에 price 필요" InvalidOrder (또는 그 변환 결과) 인지."""
    return _MARKET_BUY_PRICE_RE.search(str(exc)) is not None


class CCXTBroker(BaseBroker):
    """ccxt 로 접근하는 현물 거래소 어댑터. 레지스트리 이름 ``ccxt`` / ``binance``."""

    name = "ccxt"
    asset_class = AssetClass.CRYPTO
    supported_intervals: tuple[str, ...] = tuple(TIMEFRAMES)

    def __init__(
        self,
        exchange_id: str = "binance",
        api_key: str | None = None,
        secret: str | None = None,
        password: str | None = None,
        *,
        sandbox: bool = False,
        options: Mapping[str, Any] | None = None,
        exchange: Any = None,
    ) -> None:
        if not exchange_id or not str(exchange_id).strip():
            raise ConfigError("exchange_id 가 비어 있습니다 (예: binance, bybit, okx)")
        self.exchange_id = str(exchange_id).strip().lower()
        self.sandbox = bool(sandbox)
        self._api_key = api_key or None
        self._secret = secret or None
        self._password = password or None
        self._markets: dict[str, Any] | None = None

        if exchange is None:
            exchange = self._create_exchange(options)
        else:
            # 주입된 거래소에도 어댑터 기본 옵션 → 사용자 옵션 순으로 깊은 병합한다 (ccxt 인스턴스는 dict 형 options).
            current = getattr(exchange, "options", None)
            if isinstance(current, dict):
                _merge_options(current, DEFAULT_EXCHANGE_OPTIONS)
                if options:
                    _merge_options(current, options)
        self._exchange = exchange
        if self.sandbox:
            self._enable_sandbox()

    def _enable_sandbox(self) -> None:
        """``set_sandbox_mode(True)``. 거래소가 지원하지 않으면 ConfigError — 실전 서버로 조용히 내려가지 않는다."""
        try:
            self._exchange.set_sandbox_mode(True)
        except Exception as e:  # noqa: BLE001 - NotSupported / TypeError(urls["test"] 가 None) 등 거래소별로 다름
            raise ConfigError(
                f"{self.exchange_id} 는 ccxt 샌드박스(테스트넷)를 지원하지 않습니다 ({type(e).__name__}: {e}). "
                "실전 서버로 거래하려면 broker.sandbox: false 로 명시해 실거래임을 확인하세요"
            ) from e
        logger.info("%s 샌드박스(테스트넷) 모드", self.exchange_id)

    # ------------------------------------------------------------------ 생성
    def _create_exchange(self, options: Mapping[str, Any] | None) -> Any:
        ccxt = _import_ccxt()
        exchange_cls = getattr(ccxt, self.exchange_id, None)
        if exchange_cls is None or self.exchange_id not in set(
            getattr(ccxt, "exchanges", [self.exchange_id])
        ):
            raise ConfigError(f"ccxt 가 모르는 거래소 id: {self.exchange_id!r}")
        config: dict[str, Any] = {"enableRateLimit": True}
        if self._api_key:
            config["apiKey"] = self._api_key
        if self._secret:
            config["secret"] = self._secret
        if self._password:
            config["password"] = self._password
        merged: dict[str, Any] = {}
        _merge_options(merged, DEFAULT_EXCHANGE_OPTIONS)
        if options:
            _merge_options(merged, options)
        config["options"] = merged  # ccxt 생성자가 거래소 기본 옵션과 다시 깊은 병합한다
        return exchange_cls(config)

    @classmethod
    def from_config(cls, config: AppConfig) -> CCXTBroker:
        """환경변수(CCXT_API_KEY / CCXT_SECRET / CCXT_PASSWORD 또는 BINANCE_*)에서 키를 읽는다."""
        creds = Credentials.from_env()
        broker_name = (config.broker.name or "").lower()
        exchange_id = config.broker.exchange_id or (
            "binance" if broker_name in ("binance", "ccxt") else broker_name
        )
        extra = config.broker.extra or {}
        options = extra.get("options")
        if options is not None and not isinstance(options, Mapping):
            raise ConfigError("broker.extra.options 는 매핑이어야 합니다")
        broker = cls(
            exchange_id,
            creds.ccxt_api_key,
            creds.ccxt_secret,
            creds.ccxt_password,
            sandbox=bool(config.broker.sandbox),
            options=options,
        )
        if not broker.has_credentials:
            logger.info(
                "%s API 키가 없어 공개 시세 조회만 가능합니다 (CCXT_API_KEY / CCXT_SECRET)", exchange_id
            )
        return broker

    # ------------------------------------------------------------------ 속성/메타
    @property
    def exchange(self) -> Any:
        """내부 ccxt 거래소 인스턴스 (고급 사용자용)."""
        return self._exchange

    @property
    def has_credentials(self) -> bool:
        return bool(self._api_key and self._secret)

    def _require_credentials(self) -> None:
        if not self.has_credentials:
            raise AuthenticationError(
                f"{self.exchange_id} 비공개 API(잔고/주문) 호출에는 CCXT_API_KEY 와 CCXT_SECRET 환경변수가 필요합니다 "
                "(.env 파일 또는 export 로 설정)"
            )

    def _call(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        """ccxt 호출을 감싸 예외를 봇 예외로 변환한다."""
        try:
            return func(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - 아래에서 ccxt 계열만 변환, 나머지는 그대로
            mapped = translate_ccxt_error(e)
            if mapped is None:
                raise
            raise mapped from e

    def _load_markets(self, reload: bool = False) -> dict[str, Any]:
        if self._markets is None or reload:
            markets = (
                self._call(self._exchange.load_markets, reload)
                if reload
                else self._call(self._exchange.load_markets)
            )
            if not isinstance(markets, dict):
                raise BrokerError(f"{self.exchange_id} load_markets 응답이 올바르지 않습니다")
            self._markets = markets
        return self._markets

    def _market(self, symbol: str) -> dict[str, Any]:
        market = self._load_markets().get(symbol)
        if market is None:
            raise BrokerError(f"{self.exchange_id} 에 없는 마켓: {symbol!r}")
        return market

    def quote_currency(self, symbol: str) -> str:
        if self._markets and symbol in self._markets and self._markets[symbol].get("quote"):
            return str(self._markets[symbol]["quote"])
        return split_symbol(symbol)[1]

    def base_currency(self, symbol: str) -> str:
        if self._markets and symbol in self._markets and self._markets[symbol].get("base"):
            return str(self._markets[symbol]["base"])
        return split_symbol(symbol)[0]

    def min_order_value(self, symbol: str) -> float:
        """마켓 ``limits.cost.min`` (Binance minNotional). 정보가 없으면 0."""
        limits = self._market(symbol).get("limits") or {}
        cost = limits.get("cost") or {}
        return _to_float(cost.get("min"))

    def round_quantity(self, symbol: str, quantity: float) -> float:
        """ccxt ``amount_to_precision`` (내림). 최소 정밀도보다 작으면 0.0."""
        if quantity <= 0:
            return 0.0
        self._load_markets()
        try:
            return float(self._exchange.amount_to_precision(symbol, quantity))
        except Exception as e:  # noqa: BLE001
            mapped = translate_ccxt_error(e)
            if isinstance(mapped, OrderError):
                # ccxt 는 결과가 '0' 이면 InvalidOrder 를 던진다 → 주문 불가 수량
                return 0.0
            if mapped is None:
                raise
            raise mapped from e

    def round_price(self, symbol: str, price: float) -> float:
        """ccxt ``price_to_precision`` (반올림)."""
        if price <= 0:
            raise OrderError(f"가격은 0보다 커야 합니다: {price}")
        self._load_markets()
        return float(self._call(self._exchange.price_to_precision, symbol, price))

    def is_market_open(self) -> bool:
        return True

    def close(self) -> None:
        closer = getattr(self._exchange, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception as e:  # noqa: BLE001
                logger.debug("%s close() 실패 (무시): %s", self.exchange_id, e)

    # ------------------------------------------------------------------ 시세
    def get_candles(
        self,
        symbol: str,
        interval: str,
        limit: int = 200,
        end: datetime | None = None,
        include_partial: bool = False,
    ) -> list[Candle]:
        timeframe = to_ccxt_timeframe(interval)
        if limit <= 0:
            raise ValueError(f"limit 은 1 이상이어야 합니다: {limit}")
        step_sec = interval_to_seconds(interval)
        step_ms = step_sec * 1000
        now = utcnow()
        end_utc = ensure_utc(end) if end is not None else None

        # 마지막으로 포함될 수 있는 캔들의 시작 시각 (end 미만, end 가 없으면 진행 중 캔들까지)
        reference = end_utc if end_utc is not None else now
        last_start = floor_to_interval(reference, interval)
        if end_utc is not None and last_start == end_utc:
            last_start -= timedelta(seconds=step_sec)
        # 진행 중 캔들을 버려도 limit 개가 남도록 한 개 여유를 둔다.
        want = limit + 1
        since_ms = int(last_start.timestamp() * 1000) - limit * step_ms
        last_start_ms = int(last_start.timestamp() * 1000)

        rows: dict[int, list[Any]] = {}
        since = since_ms
        for _ in range(MAX_OHLCV_PAGES):
            remaining = want - len(rows)
            if remaining <= 0:
                break
            batch = self._call(
                self._exchange.fetch_ohlcv, symbol, timeframe, since, min(remaining, OHLCV_BATCH_LIMIT)
            )
            if not batch:
                break
            newest = None
            for row in batch:
                try:
                    ts = int(row[0])
                except (TypeError, ValueError, IndexError) as e:
                    raise BrokerError(f"{self.exchange_id} fetch_ohlcv 응답 형식 오류: {row!r}") from e
                rows[ts] = row
                newest = ts if newest is None else max(newest, ts)
            if newest is None or newest >= last_start_ms:
                break
            next_since = newest + step_ms
            if next_since <= since:
                break
            since = next_since

        end_ms = int(end_utc.timestamp() * 1000) if end_utc is not None else None
        now_ms = int(now.timestamp() * 1000)
        candles: list[Candle] = []
        for ts in sorted(rows):
            if end_ms is not None and ts >= end_ms:
                continue
            if not include_partial and ts + step_ms > now_ms:
                continue
            row = rows[ts]
            try:
                candles.append(
                    Candle(
                        timestamp=datetime.fromtimestamp(ts / 1000, tz=timezone.utc),
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=_to_float(row[5]),
                    )
                )
            except (TypeError, ValueError, IndexError) as e:
                raise BrokerError(f"{self.exchange_id} fetch_ohlcv 응답 형식 오류: {row!r}") from e
        return candles[-limit:]

    def get_ticker(self, symbol: str) -> float:
        ticker = self._call(self._exchange.fetch_ticker, symbol)
        if not isinstance(ticker, Mapping):
            raise BrokerError(f"{self.exchange_id} fetch_ticker 응답 형식 오류: {ticker!r}")
        for key in ("last", "close"):
            price = _to_float(ticker.get(key))
            if price > 0:
                return price
        raise BrokerError(f"{self.exchange_id} {symbol} 현재가를 알 수 없습니다 (last/close 없음)")

    # ------------------------------------------------------------------ 계좌
    def _fetch_balance(self) -> dict[str, dict[str, float]]:
        self._require_credentials()
        raw = self._call(self._exchange.fetch_balance)
        if not isinstance(raw, Mapping):
            raise BrokerError(f"{self.exchange_id} fetch_balance 응답 형식 오류")
        out: dict[str, dict[str, float]] = {}
        for code, entry in raw.items():
            if code in _BALANCE_META_KEYS or not isinstance(entry, Mapping):
                continue
            free = _to_float(entry.get("free"))
            used = _to_float(entry.get("used"))
            total = entry.get("total")
            total_f = _to_float(total) if total is not None else free + used
            if total_f <= 0 and free <= 0:
                continue
            out[str(code)] = {"free": free, "used": used, "total": max(total_f, free)}
        return out

    def get_balances(self) -> dict[str, Balance]:
        return {
            code: Balance(currency=code, total=b["total"], available=b["free"])
            for code, b in self._fetch_balance().items()
        }

    def get_positions(
        self,
        quote_currencies: Sequence[str] = DEFAULT_QUOTE_CURRENCIES,
        average_prices: Mapping[str, float] | None = None,
    ) -> dict[str, Position]:
        """현물 잔고 → 포지션 추정.

        결제 통화(quote_currencies)가 아닌 통화 중 ``<통화>/<quote>`` 마켓이 존재하는 것을 포지션으로 본다
        (quote_currencies 순서대로 첫 번째 존재하는 마켓). 거래소 최소 수량 미만의 먼지(dust) 잔고는 제외한다.
        현물 잔고에는 평균 매수가가 없으므로 ``average_price`` 는 ``average_prices[symbol]`` 이 있으면 그 값,
        없으면 0.0 이다 (``meta["average_price_known"]`` 로 구분). 엔진은 진짜 평균단가를 자신의 상태에 보관한다.
        """
        balances = self._fetch_balance()
        markets = self._load_markets()
        quotes = [q.upper() for q in quote_currencies]
        known = {k: float(v) for k, v in (average_prices or {}).items()}
        positions: dict[str, Position] = {}
        for code, bal in balances.items():
            if code.upper() in quotes or bal["total"] <= 0:
                continue
            symbol = next((f"{code}/{q}" for q in quotes if f"{code}/{q}" in markets), None)
            if symbol is None:
                logger.debug("%s 잔고 %s 는 결제통화 마켓이 없어 포지션에서 제외", self.exchange_id, code)
                continue
            min_amount = _to_float(((markets[symbol].get("limits") or {}).get("amount") or {}).get("min"))
            if min_amount > 0 and bal["total"] < min_amount:
                logger.debug(
                    "%s %s 잔고 %.10f 는 최소 수량 %s 미만(dust) → 제외",
                    self.exchange_id,
                    symbol,
                    bal["total"],
                    min_amount,
                )
                continue
            avg = known.get(symbol)
            positions[symbol] = Position(
                symbol=symbol,
                quantity=bal["total"],
                average_price=avg if avg is not None and avg > 0 else 0.0,
                meta={
                    "source": "balance",
                    "average_price_known": avg is not None and avg > 0,
                    "free": bal["free"],
                    "used": bal["used"],
                },
            )
        return positions

    # ------------------------------------------------------------------ 주문
    def _market_buy_requires_price(self) -> bool:
        """ccxt 와 같은 순서로 읽는다: ``options["createOrder"][KEY]`` → ``options[KEY]`` → False.

        거래소 코드에 기본값 True 가 박혀 옵션으로는 알 수 없는 경우(btse 등) 는 ``place_order`` 가
        ccxt 의 사전 거부(InvalidOrder) 를 보고 현재가로 한 번 재시도한다.
        """
        handler = getattr(self._exchange, "handle_option", None)
        if callable(handler):
            return bool(handler("createOrder", MARKET_BUY_REQUIRES_PRICE_OPTION, False))
        options = getattr(self._exchange, "options", None)
        if not isinstance(options, Mapping):
            return False
        nested = options.get("createOrder")
        value = nested.get(MARKET_BUY_REQUIRES_PRICE_OPTION) if isinstance(nested, Mapping) else None
        if value is None:
            value = options.get(MARKET_BUY_REQUIRES_PRICE_OPTION)
        return bool(value)

    def _market_buy_price(self, symbol: str, amount: float, params: dict[str, Any]) -> float | None:
        """시장가 매수 총액 환산용 현재가. Binance 는 ``params["quoteOrderQty"]`` 에 금액을 넣고 price 는 None."""
        ticker = self.get_ticker(symbol)
        if self.exchange_id == "binance":
            cost = float(self._call(self._exchange.price_to_precision, symbol, amount * ticker))
            params["quoteOrderQty"] = cost
            logger.info("%s 시장가 매수 %s 금액 %s (quoteOrderQty)", self.exchange_id, symbol, cost)
            return None
        logger.info("%s 시장가 매수 %s %s (가격 %s 기준 총액 환산)", self.exchange_id, symbol, amount, ticker)
        return ticker

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
                "ccxt 어댑터는 STOP 주문을 지원하지 않습니다 (엔진/백테스터가 폴링으로 흉내냅니다)"
            )
        if order_type not in (OrderType.MARKET, OrderType.LIMIT):
            raise OrderError(f"지원하지 않는 주문 유형: {order_type}")
        if quantity is None or quantity <= 0:
            raise OrderError(f"주문 수량은 0보다 커야 합니다: {quantity}")
        self._market(symbol)  # 마켓 존재 확인 (+ 정밀도 정보 로드)
        amount = self.round_quantity(symbol, quantity)
        if amount <= 0:
            raise OrderError(f"{symbol} 수량 {quantity} 는 거래소 최소 정밀도보다 작습니다")

        ccxt_side = side.value
        params: dict[str, Any] = {}
        if order_type == OrderType.LIMIT:
            if price is None or price <= 0:
                raise OrderError("지정가 주문에는 price 가 필요합니다")
            limit_price = self.round_price(symbol, price)
            logger.info("%s 지정가 %s %s %s @ %s", self.exchange_id, ccxt_side, symbol, amount, limit_price)
            raw = self._call(
                self._exchange.create_order, symbol, "limit", ccxt_side, amount, limit_price, params
            )
            return self._parse_order(raw, symbol, side, order_type, amount, limit_price)

        # 시장가
        order_price: float | None = None
        priced = side == OrderSide.BUY and self._market_buy_requires_price()
        if priced:
            order_price = self._market_buy_price(symbol, amount, params)
        else:
            logger.info("%s 시장가 %s %s %s", self.exchange_id, ccxt_side, symbol, amount)
        try:
            raw = self._call(
                self._exchange.create_order, symbol, "market", ccxt_side, amount, order_price, params
            )
        except OrderError as e:
            if priced or side != OrderSide.BUY or not is_market_buy_price_error(e):
                raise
            # ccxt 가 요청을 보내기 전에 거부했다 (거래소 코드의 기본값 True 는 옵션으로 알 수 없다: btse 등).
            # 아직 아무 요청도 나가지 않았으므로 현재가를 넘겨 한 번만 재시도한다.
            logger.warning(
                "%s 시장가 매수에 price 가 필요합니다 → 현재가로 재시도 (broker.extra.options 에 %s: true 를 두면 "
                "재시도 없이 보냅니다): %s",
                self.exchange_id,
                MARKET_BUY_REQUIRES_PRICE_OPTION,
                e,
            )
            order_price = self._market_buy_price(symbol, amount, params)
            raw = self._call(
                self._exchange.create_order, symbol, "market", ccxt_side, amount, order_price, params
            )
        return self._parse_order(raw, symbol, side, order_type, amount, None)

    def cancel_order(self, order_id: str, symbol: str | None = None) -> bool:
        """취소 성공 True. 이미 체결/취소되어 찾을 수 없으면(OrderNotFound) False."""
        self._require_credentials()
        try:
            self._exchange.cancel_order(order_id, symbol)
        except Exception as e:  # noqa: BLE001
            if any(k.__name__ == "OrderNotFound" for k in type(e).__mro__):
                logger.info("%s 주문 %s 취소 불가(이미 종료): %s", self.exchange_id, order_id, e)
                return False
            mapped = translate_ccxt_error(e)
            if mapped is None:
                raise
            raise mapped from e
        return True

    def get_order(self, order_id: str, symbol: str | None = None) -> Order:
        self._require_credentials()
        raw = self._call(self._exchange.fetch_order, order_id, symbol)
        return self._parse_order(raw, symbol or "", None, None, None, None)

    def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        """미체결 주문. ``symbol`` 생략 시 전체 조회 — Binance 는 가중치가 크다(전체 80 vs 심볼 6) 므로 자주 부르지 않는다."""
        self._require_credentials()
        raw_orders = self._call(self._exchange.fetch_open_orders, symbol)
        return [self._parse_order(o, symbol or "", None, None, None, None) for o in raw_orders or []]

    # ------------------------------------------------------------------ 파싱
    def _parse_order(
        self,
        raw: Any,
        symbol: str,
        side: OrderSide | None,
        order_type: OrderType | None,
        amount: float | None,
        price: float | None,
    ) -> Order:
        if not isinstance(raw, Mapping):
            raise BrokerError(f"{self.exchange_id} 주문 응답 형식 오류: {raw!r}")
        order_id = raw.get("id") or raw.get("clientOrderId")
        if order_id in (None, ""):
            raise BrokerError(f"{self.exchange_id} 주문 응답에 id 가 없습니다: {dict(raw)!r}")
        sym = str(raw.get("symbol") or symbol)

        raw_side = str(raw.get("side") or "").lower()
        parsed_side = OrderSide(raw_side) if raw_side in ("buy", "sell") else side
        if parsed_side is None:
            raise BrokerError(f"{self.exchange_id} 주문 {order_id} 의 side 를 알 수 없습니다")

        raw_type = str(raw.get("type") or "").lower()
        if raw_type == "market":
            parsed_type = OrderType.MARKET
        elif raw_type == "limit":
            parsed_type = OrderType.LIMIT
        elif "stop" in raw_type:
            parsed_type = OrderType.STOP
        else:
            parsed_type = order_type or OrderType.MARKET

        filled = _to_float(raw.get("filled"))
        quantity = _to_float(raw.get("amount"))
        if quantity <= 0:
            remaining = raw.get("remaining")
            if remaining is not None and filled + _to_float(remaining) > 0:
                quantity = filled + _to_float(remaining)
            elif amount is not None and amount > 0:
                quantity = amount
            else:
                quantity = filled

        average = _to_float(raw.get("average"))
        cost = _to_float(raw.get("cost"))
        if average <= 0 and filled > 0 and cost > 0:
            average = cost / filled
        order_price = _to_float(raw.get("price"))
        if order_price <= 0:
            order_price = price if price is not None and price > 0 else 0.0

        quote = self.quote_currency(sym) if "/" in sym else ""
        base = self.base_currency(sym) if "/" in sym else ""
        fee = self._fee_in_quote(raw, quote, base, average)

        status = self._parse_status(raw.get("status"), filled, quantity)

        ts = raw.get("timestamp")
        created_at = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc) if ts else utcnow()
        updated_ts = raw.get("lastUpdateTimestamp") or raw.get("lastTradeTimestamp")
        updated_at = datetime.fromtimestamp(int(updated_ts) / 1000, tz=timezone.utc) if updated_ts else None

        return Order(
            id=str(order_id),
            symbol=sym,
            side=parsed_side,
            type=parsed_type,
            quantity=quantity,
            price=order_price if parsed_type != OrderType.MARKET and order_price > 0 else None,
            status=status,
            filled_quantity=filled,
            average_price=average if average > 0 else None,
            fee=fee,
            created_at=created_at,
            updated_at=updated_at,
            raw=dict(raw),
        )

    @staticmethod
    def _parse_status(raw_status: Any, filled: float, quantity: float) -> OrderStatus:
        status_str = str(raw_status or "").lower()
        if not status_str:
            return OrderStatus.PENDING
        status = _STATUS_MAP.get(status_str)
        if status is None:
            logger.warning("알 수 없는 ccxt 주문 상태 %r → PENDING", raw_status)
            return OrderStatus.PENDING
        if status == OrderStatus.OPEN and filled > 0:
            return OrderStatus.PARTIALLY_FILLED
        if status == OrderStatus.FILLED and quantity > 0 and 0 < filled < quantity * (1 - 1e-9):
            # IOC 등으로 일부만 체결되고 닫힌 주문. 터미널이므로 FILLED 로 두되 filled_quantity 로 구분한다.
            logger.info("부분 체결 후 종료된 주문 (filled %.10g / %.10g)", filled, quantity)
        return status

    @staticmethod
    def _fee_in_quote(raw: Mapping[str, Any], quote: str, base: str, average: float) -> float:
        """수수료를 quote 통화 기준으로 합산한다. base 통화 수수료는 평균 체결가로 환산."""
        entries: list[Mapping[str, Any]] = []
        fee = raw.get("fee")
        if isinstance(fee, Mapping) and fee.get("cost") is not None:
            entries.append(fee)
        fees = raw.get("fees")
        if not entries and isinstance(fees, Sequence) and not isinstance(fees, (str, bytes)):
            entries.extend(f for f in fees if isinstance(f, Mapping) and f.get("cost") is not None)
        total = 0.0
        for entry in entries:
            cost = _to_float(entry.get("cost"))
            currency = str(entry.get("currency") or "").upper()
            if not currency or currency == quote.upper():
                total += cost
            elif currency == base.upper() and average > 0:
                total += cost * average
            else:
                # BNB 등 제3 통화 수수료는 quote 환산 불가 → 제외 (raw 에 원본 보존)
                logger.debug("수수료 통화 %s 는 quote(%s) 로 환산하지 않음", currency, quote)
        return total


__all__ = [
    "CCXTBroker",
    "DEFAULT_EXCHANGE_OPTIONS",
    "DEFAULT_QUOTE_CURRENCIES",
    "MARKET_BUY_REQUIRES_PRICE_OPTION",
    "TIMEFRAMES",
    "is_market_buy_price_error",
    "split_symbol",
    "to_ccxt_timeframe",
    "translate_ccxt_error",
]
