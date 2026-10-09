"""모의 체결 엔진 (PaperBroker).

백테스트와 모의투자(paper trading)가 공용으로 쓰는 시뮬레이션 브로커. 실제 거래소에 주문을 내지 않고
현금/포지션/주문/체결 내역을 메모리에서 관리한다.

동작 요약
- 시세: ``mark_price()`` 로 설정한 가격 > ``data_source.get_ticker()`` > ``BrokerError``.
- MARKET 주문은 즉시 ``현재가 * (1 ± slippage_pct)`` 로 체결하고 수수료를 현금에서 차감한다.
- LIMIT / STOP 주문은 OPEN 상태로 보관했다가 ``process_candle()``(백테스트, 캔들 OHLC 기준) 또는
  ``check_pending()``(모의투자 폴링, 현재가 기준) 에서 체결 판정한다.
- LIMIT 체결가는 **시장이 이미 지정가를 지나쳤으면(갭 통과) 시장 가격**, 아니면 지정가다. 캔들 경로는
  매수 ``min(지정가, 시가)`` / 매도 ``max(지정가, 시가)`` 라 체결가가 항상 캔들 ``[low, high]`` 안에 있고,
  폴링 경로는 현재가가 지정가를 지나쳤으면 현재가에 체결한다 (STOP 폴링 체결과 같은 규칙). 지정가에는
  슬리피지를 더하지 않는다. 체결 근거는 ``Order.raw["fill_basis"]`` (``open``/``limit``/``trigger``/``market``).
- 매도 체결 시 평균단가 기준으로 ``Trade`` 를 만들어 ``trades`` 에 쌓는다. Trade.fee 는
  (해당 수량에 비례 배분한 매수 수수료) + (매도 수수료).
- 잔고 계산: 미체결 LIMIT 매수는 quote 통화를, 미체결 매도(LIMIT/STOP)는 기초자산을 잠근다(locked).
  STOP 매수는 트리거 가격을 미리 알 수 없으므로 잠그지 않고 체결 시점에 현금을 검사한다 (부족하면 REJECTED).
- 시뮬레이션 시각: ``mark_price(..., timestamp=)`` 또는 ``process_candle()`` 이 호출되면 그 시각이
  "현재 시각" 이 되어 주문/체결/Trade 의 시각에 쓰인다. 한 번도 호출되지 않았으면 ``clock()``(기본 UTC now).

주의
- 단일 결제통화 계좌다. 심볼의 결제통화(``quote_currency(symbol)``)가 계좌 통화와 다르면 주문을 거부한다.
- 데모/샘플 시세는 없다. 가격은 반드시 백테스터(``mark_price``)나 ``data_source`` 가 공급한다.
"""

from __future__ import annotations

import logging
import math
import numbers
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from tradingbot.brokers.base import BaseBroker
from tradingbot.exceptions import (
    BrokerError,
    ConfigError,
    DataError,
    InsufficientFunds,
    OrderError,
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
    Trade,
    ensure_utc,
    utcnow,
)

if TYPE_CHECKING:  # pragma: no cover
    from tradingbot.config import AppConfig

logger = logging.getLogger(__name__)

#: 상태 직렬화 포맷 버전
STATE_VERSION = 1


def _tol(x: float) -> float:
    """부동소수 비교 허용 오차 (값 크기에 비례, 최소 1e-9)."""
    return 1e-9 * max(1.0, abs(x))


def _is_real(x: Any) -> bool:
    """bool 을 제외한 실수(numpy 스칼라 포함)."""
    return isinstance(x, numbers.Real) and not isinstance(x, bool)


def _is_positive_finite(x: Any) -> bool:
    return _is_real(x) and math.isfinite(x) and x > 0


def _jsonable(value: Any) -> Any:
    """to_dict 용: datetime → ISO 문자열, Enum → value, 컨테이너는 재귀 변환."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return ensure_utc(value).isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    item = getattr(value, "item", None)  # numpy 스칼라
    if callable(item):
        try:
            return _jsonable(item())
        except (TypeError, ValueError):
            pass
    return str(value)


def _dt_to_iso(dt: datetime | None) -> str | None:
    return None if dt is None else ensure_utc(dt).isoformat()


def _iso_to_dt(s: str | None) -> datetime | None:
    if s is None:
        return None
    return ensure_utc(datetime.fromisoformat(s))


def _opt_float(v: Any) -> float | None:
    return None if v is None else float(v)


class PaperBroker(BaseBroker):
    """모의 체결 브로커. 백테스터와 모의투자 엔진이 공용으로 사용한다."""

    name = "paper"

    def __init__(
        self,
        *,
        initial_cash: float,
        quote_currency: str = "KRW",
        fee_pct: float = 0.0005,
        slippage_pct: float = 0.0005,
        data_source: BaseBroker | None = None,
        asset_class: AssetClass = AssetClass.CRYPTO,
        min_order_value: float = 0.0,
    ) -> None:
        if not _is_positive_finite(initial_cash):
            raise ConfigError(f"initial_cash 는 0보다 큰 유한한 값이어야 합니다: {initial_cash!r}")
        if not (0 <= fee_pct < 1):
            raise ConfigError(f"fee_pct 는 0 이상 1 미만이어야 합니다: {fee_pct!r}")
        if not (0 <= slippage_pct < 1):
            raise ConfigError(f"slippage_pct 는 0 이상 1 미만이어야 합니다: {slippage_pct!r}")
        if min_order_value < 0:
            raise ConfigError(f"min_order_value 는 0 이상이어야 합니다: {min_order_value!r}")
        quote = (quote_currency or "").strip()
        if not quote:
            raise ConfigError("quote_currency 가 비어 있습니다")

        self._initial_cash = float(initial_cash)
        self._cash = float(initial_cash)
        self._quote = quote
        self._fee_pct = float(fee_pct)
        self._slippage_pct = float(slippage_pct)
        self._data_source = data_source
        self.asset_class = AssetClass(asset_class)
        self._min_order_value = float(min_order_value)
        self.supported_intervals = data_source.supported_intervals if data_source is not None else ()

        #: 시뮬레이션 시각을 외부에서 바꾸고 싶을 때 교체 (기본 UTC now). mark_price(timestamp=) /
        #: process_candle 이 호출되면 그 시각이 우선한다.
        self.clock: Callable[[], datetime] = utcnow
        self._sim_time: datetime | None = None

        self._prices: dict[str, float] = {}
        self._positions: dict[str, Position] = {}
        #: 심볼별 미청산 포지션에 누적된 매수 수수료 (매도 시 비례 배분)
        self._position_fees: dict[str, float] = {}
        self._orders: dict[str, Order] = {}
        self._trades: list[Trade] = []
        self._order_seq = 0

    # ------------------------------------------------------------------ 생성
    @classmethod
    def from_config(cls, config: AppConfig, data_source: BaseBroker | None = None) -> PaperBroker:
        """``config.paper.*`` 로 생성. data_source 가 있으면 asset_class / quote 통화는 data_source 를 따른다."""
        paper = config.paper
        asset_class = AssetClass.CRYPTO
        quote = paper.quote_currency
        if data_source is not None:
            asset_class = AssetClass(data_source.asset_class)
            try:
                quote = data_source.quote_currency(config.symbols[0])
            except Exception as e:  # noqa: BLE001 - 어댑터 구현에 따라 임의 예외 가능
                logger.warning(
                    "data_source 의 결제통화를 알 수 없어 설정값 %s 을 사용합니다 (%s)",
                    paper.quote_currency,
                    e,
                )
                quote = paper.quote_currency
        return cls(
            initial_cash=paper.initial_cash,
            quote_currency=quote,
            fee_pct=paper.fee_pct,
            slippage_pct=paper.slippage_pct,
            data_source=data_source,
            asset_class=asset_class,
            min_order_value=config.risk.min_order_value,
        )

    # ------------------------------------------------------------------ 속성
    @property
    def cash(self) -> float:
        """현금 잔고 (quote 통화, 미체결 주문 잠금 포함 총액)."""
        return self._cash

    @property
    def initial_cash(self) -> float:
        return self._initial_cash

    @property
    def fee_pct(self) -> float:
        return self._fee_pct

    @property
    def slippage_pct(self) -> float:
        return self._slippage_pct

    @property
    def data_source(self) -> BaseBroker | None:
        return self._data_source

    @property
    def trades(self) -> list[Trade]:
        """청산 완료된 Trade 목록 (복사본, 생성 순)."""
        return list(self._trades)

    @property
    def orders(self) -> list[Order]:
        """지금까지의 모든 주문 스냅샷 (생성 순)."""
        return [self._snapshot(o) for o in self._orders.values()]

    # ------------------------------------------------------------------ 시간
    def _now(self) -> datetime:
        if self._sim_time is not None:
            return self._sim_time
        return ensure_utc(self.clock())

    # ------------------------------------------------------------------ 시세
    def mark_price(self, symbol: str, price: float, timestamp: datetime | None = None) -> None:
        """현재가 설정 (백테스터가 bar 마다 호출). timestamp 를 주면 시뮬레이션 시각도 그 값으로 옮긴다."""
        if not _is_positive_finite(price):
            raise DataError(f"{symbol} mark_price 가 유효하지 않습니다: {price!r}")
        self._prices[symbol] = float(price)
        if timestamp is not None:
            self._sim_time = ensure_utc(timestamp)

    def get_ticker(self, symbol: str) -> float:
        marked = self._prices.get(symbol)
        if marked is not None:
            return marked
        if self._data_source is not None:
            price = float(self._data_source.get_ticker(symbol))
            if not _is_positive_finite(price):
                raise BrokerError(
                    f"data_source 가 {symbol} 에 대해 유효하지 않은 현재가를 반환했습니다: {price!r}"
                )
            return price
        raise BrokerError(f"{symbol} 현재가가 없습니다. mark_price() 로 설정하거나 data_source 를 지정하세요")

    def get_candles(
        self,
        symbol: str,
        interval: str,
        limit: int = 200,
        end: datetime | None = None,
        include_partial: bool = False,
    ) -> list[Candle]:
        if self._data_source is None:
            raise DataError("PaperBroker 에 data_source 가 없어 캔들을 조회할 수 없습니다")
        return self._data_source.get_candles(
            symbol, interval, limit=limit, end=end, include_partial=include_partial
        )

    def is_market_open(self) -> bool:
        if self._data_source is not None:
            return self._data_source.is_market_open()
        return True

    # ------------------------------------------------------------------ 메타
    def _split_symbol(self, symbol: str) -> tuple[str, str]:
        """심볼 → (quote, base). 암호화폐 표기만 파싱하고 그 외는 (계좌 통화, 심볼)."""
        if self.asset_class == AssetClass.CRYPTO:
            if "/" in symbol:
                base, _, quote = symbol.partition("/")
                quote = quote.split(":", 1)[0]  # ccxt 파생상품 표기 BTC/USDT:USDT 방어
                if base and quote:
                    return quote, base
            elif "-" in symbol:
                quote, _, base = symbol.partition("-")
                if quote and base:
                    return quote, base
        return self._quote, symbol

    def quote_currency(self, symbol: str) -> str:
        return self._split_symbol(symbol)[0]

    def base_currency(self, symbol: str) -> str:
        return self._split_symbol(symbol)[1]

    def min_order_value(self, symbol: str) -> float:
        value = self._min_order_value
        if self._data_source is not None:
            value = max(value, float(self._data_source.min_order_value(symbol)))
        return value

    def round_quantity(self, symbol: str, quantity: float) -> float:
        if self._data_source is not None:
            return self._data_source.round_quantity(symbol, quantity)
        return super().round_quantity(symbol, quantity)

    def round_price(self, symbol: str, price: float) -> float:
        if self._data_source is not None:
            return self._data_source.round_price(symbol, price)
        return super().round_price(symbol, price)

    # ------------------------------------------------------------------ 잠금/가용 잔고
    def _reserved_quote(self, exclude: str | None = None) -> float:
        """미체결 LIMIT 매수가 잠근 quote 금액 (수수료 포함)."""
        total = 0.0
        for o in self._orders.values():
            if o.id == exclude or o.status != OrderStatus.OPEN:
                continue
            if o.side == OrderSide.BUY and o.type == OrderType.LIMIT and o.price is not None:
                total += o.price * o.remaining_quantity * (1.0 + self._fee_pct)
        return total

    def _reserved_base(self, symbol: str, exclude: str | None = None) -> float:
        """미체결 매도 주문이 잠근 기초자산 수량."""
        total = 0.0
        for o in self._orders.values():
            if o.id == exclude or o.status != OrderStatus.OPEN or o.symbol != symbol:
                continue
            if o.side == OrderSide.SELL:
                total += o.remaining_quantity
        return total

    def _available_quote(self, exclude: str | None = None) -> float:
        return self._cash - self._reserved_quote(exclude)

    def _available_base(self, symbol: str, exclude: str | None = None) -> float:
        pos = self._positions.get(symbol)
        held = pos.quantity if pos is not None else 0.0
        return held - self._reserved_base(symbol, exclude)

    # ------------------------------------------------------------------ 주문
    def _next_order_id(self) -> str:
        self._order_seq += 1
        return f"paper-{self._order_seq}"

    @staticmethod
    def _snapshot(order: Order) -> Order:
        return replace(order, raw=dict(order.raw))

    def _check_symbol_currency(self, symbol: str) -> None:
        if not symbol or not symbol.strip():
            raise OrderError("심볼이 비어 있습니다")
        quote = self.quote_currency(symbol)
        if quote != self._quote:
            raise OrderError(f"{symbol} 의 결제통화 {quote} 가 모의계좌 통화 {self._quote} 와 다릅니다")

    def place_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        order_type: OrderType = OrderType.MARKET,
        price: float | None = None,
        *,
        stop_offset: float | None = None,
        reason: str = "",
    ) -> Order:
        """주문 접수.

        - MARKET: 즉시 ``get_ticker()*(1±slippage)`` 체결, 수수료 차감. 잔고 부족 → InsufficientFunds.
        - LIMIT: ``price`` 필수. OPEN 으로 보관. 매수는 ``price*qty*(1+fee)`` 를 잠근다 (갭 통과 체결은
          지정가보다 유리한 가격이므로 잠근 금액을 넘지 않는다).
        - STOP: ``price`` 또는 ``stop_offset`` 중 하나. ``stop_offset`` 이면 트리거는
          "다음 캔들 시가 ± offset" (process_candle 에서 결정). 현금 검사는 체결 시점에 한다.
        - ``reason`` 은 매도 체결 시 Trade.reason 으로 기록된다.
        """
        try:
            side = OrderSide(side)
            order_type = OrderType(order_type)
        except ValueError as e:
            raise OrderError(f"잘못된 주문 구분: {e}") from e
        self._check_symbol_currency(symbol)

        if not _is_positive_finite(quantity):
            raise OrderError(f"주문 수량은 0보다 큰 유한한 값이어야 합니다: {quantity!r}")
        quantity = float(quantity)
        if price is not None:
            if not _is_positive_finite(price):
                raise OrderError(f"주문 가격은 0보다 큰 유한한 값이어야 합니다: {price!r}")
            price = float(price)
        if stop_offset is not None:
            if order_type != OrderType.STOP:
                raise OrderError("stop_offset 은 STOP 주문에서만 사용할 수 있습니다")
            if not _is_real(stop_offset) or not math.isfinite(stop_offset) or stop_offset < 0:
                raise OrderError(f"stop_offset 은 0 이상의 유한한 값이어야 합니다: {stop_offset!r}")
            stop_offset = float(stop_offset)

        if order_type == OrderType.MARKET:
            if price is not None:
                logger.debug("%s 시장가 주문의 price=%s 는 무시합니다", symbol, price)
                price = None
        elif order_type == OrderType.LIMIT:
            if price is None:
                raise OrderError("LIMIT 주문에는 price 가 필요합니다")
        elif price is None and stop_offset is None:
            raise OrderError("STOP 주문에는 price 또는 stop_offset 이 필요합니다")
        elif price is not None and stop_offset is not None:
            raise OrderError("STOP 주문에 price 와 stop_offset 을 동시에 지정할 수 없습니다")

        # 최소 주문 금액 (기준가를 알 수 있을 때만)
        min_value = self.min_order_value(symbol)
        if min_value > 0:
            ref_price = price if price is not None else self._try_ticker(symbol)
            if ref_price is not None and ref_price * quantity + _tol(min_value) < min_value:
                raise OrderError(
                    f"{symbol} 주문 금액 {ref_price * quantity:,.2f} {self._quote} 가 "
                    f"최소 주문 금액 {min_value:,.2f} {self._quote} 미만입니다"
                )

        # 매도는 모든 유형에서 접수 시점에 가용 수량 검사
        if side == OrderSide.SELL:
            available = self._available_base(symbol)
            if available + _tol(quantity) < quantity:
                raise InsufficientFunds(
                    f"{symbol} 매도 가능 수량 부족: 요청 {quantity}, 가용 {max(available, 0.0)} "
                    f"(보유 {self._positions[symbol].quantity if symbol in self._positions else 0.0})"
                )

        now = self._now()
        raw: dict[str, Any] = {"reason": reason or ""}
        if stop_offset is not None:
            raw["stop_offset"] = stop_offset

        if order_type == OrderType.MARKET:
            ticker = self.get_ticker(symbol)
            fill_price = ticker * (
                1.0 + self._slippage_pct if side == OrderSide.BUY else 1.0 - self._slippage_pct
            )
            if side == OrderSide.BUY:
                cost = fill_price * quantity
                fee = cost * self._fee_pct
                available = self._available_quote()
                if available + _tol(cost + fee) < cost + fee:
                    raise InsufficientFunds(
                        f"{symbol} 매수 자금 부족: 필요 {cost + fee:,.2f} {self._quote} (수수료 {fee:,.2f} 포함), "
                        f"가용 {available:,.2f} {self._quote}"
                    )
            order = Order(
                id=self._next_order_id(),
                symbol=symbol,
                side=side,
                type=order_type,
                quantity=quantity,
                price=None,
                status=OrderStatus.PENDING,
                created_at=now,
                raw=raw,
            )
            self._orders[order.id] = order
            if not self._apply_fill(order, fill_price, now):
                # 위에서 검사했으므로 정상적으로는 도달하지 않는다
                raise InsufficientFunds(f"{symbol} 시장가 체결 실패: {order.raw.get('reject_reason', '')}")
            return self._snapshot(order)

        if side == OrderSide.BUY and order_type == OrderType.LIMIT:
            assert price is not None
            required = price * quantity * (1.0 + self._fee_pct)
            available = self._available_quote()
            if available + _tol(required) < required:
                raise InsufficientFunds(
                    f"{symbol} 지정가 매수 자금 부족: 필요 {required:,.2f} {self._quote} (수수료 포함), "
                    f"가용 {available:,.2f} {self._quote}"
                )

        order = Order(
            id=self._next_order_id(),
            symbol=symbol,
            side=side,
            type=order_type,
            quantity=quantity,
            price=price,
            status=OrderStatus.OPEN,
            created_at=now,
            raw=raw,
        )
        self._orders[order.id] = order
        logger.info(
            "[paper] 주문 접수 %s %s %s qty=%s price=%s stop_offset=%s",
            order.id,
            symbol,
            f"{side.value}/{order_type.value}",
            quantity,
            price,
            stop_offset,
        )
        return self._snapshot(order)

    def _try_ticker(self, symbol: str) -> float | None:
        try:
            return self.get_ticker(symbol)
        except BrokerError:
            return None

    def _apply_fill(self, order: Order, fill_price: float, when: datetime) -> bool:
        """주문을 fill_price 에 전량 체결하고 현금/포지션/Trade 를 갱신한다.

        자금/수량이 부족하면 상태를 REJECTED 로 바꾸고 False 를 반환한다 (예외를 던지지 않는다).
        """
        symbol = order.symbol
        qty = order.quantity

        if order.side == OrderSide.BUY:
            cost = fill_price * qty
            fee = cost * self._fee_pct
            available = self._available_quote(exclude=order.id)
            if available + _tol(cost + fee) < cost + fee:
                self._reject(
                    order, when, f"자금 부족: 필요 {cost + fee:,.2f}, 가용 {available:,.2f} {self._quote}"
                )
                return False
            self._cash -= cost + fee
            pos = self._positions.get(symbol)
            if pos is None or pos.quantity <= 0:
                self._positions[symbol] = Position(
                    symbol=symbol, quantity=qty, average_price=fill_price, opened_at=when
                )
                self._position_fees[symbol] = fee
            else:
                new_qty = pos.quantity + qty
                pos.average_price = (pos.quantity * pos.average_price + qty * fill_price) / new_qty
                pos.quantity = new_qty
                if pos.opened_at is None:
                    pos.opened_at = when
                self._position_fees[symbol] = self._position_fees.get(symbol, 0.0) + fee
        else:
            pos = self._positions.get(symbol)
            available = self._available_base(symbol, exclude=order.id)
            if pos is None or available + _tol(qty) < qty:
                self._reject(order, when, f"매도 가능 수량 부족: 요청 {qty}, 가용 {max(available, 0.0)}")
                return False
            proceeds = fill_price * qty
            fee = proceeds * self._fee_pct
            self._cash += proceeds - fee
            buy_fees = self._position_fees.get(symbol, 0.0)
            ratio = min(qty / pos.quantity, 1.0) if pos.quantity > 0 else 1.0
            prorated = buy_fees * ratio
            trade = Trade(
                symbol=symbol,
                side=OrderSide.BUY,
                quantity=qty,
                entry_price=pos.average_price,
                exit_price=fill_price,
                entry_time=pos.opened_at if pos.opened_at is not None else when,
                exit_time=when,
                fee=prorated + fee,
                reason=str(order.raw.get("reason", "") or ""),
            )
            self._trades.append(trade)
            remaining = pos.quantity - qty
            if remaining <= _tol(pos.quantity):
                del self._positions[symbol]
                self._position_fees.pop(symbol, None)
            else:
                pos.quantity = remaining
                self._position_fees[symbol] = buy_fees - prorated
            logger.info(
                "[paper] 청산 %s qty=%s entry=%.8g exit=%.8g pnl=%.4f fee=%.4f reason=%s",
                symbol,
                qty,
                trade.entry_price,
                trade.exit_price,
                trade.pnl,
                trade.fee,
                trade.reason,
            )

        order.status = OrderStatus.FILLED
        order.filled_quantity = qty
        order.average_price = fill_price
        order.fee = fee
        order.updated_at = when
        logger.info(
            "[paper] 체결 %s %s %s qty=%s price=%.8g fee=%.4f cash=%.2f",
            order.id,
            symbol,
            f"{order.side.value}/{order.type.value}",
            qty,
            fill_price,
            fee,
            self._cash,
        )
        return True

    @staticmethod
    def _reject(order: Order, when: datetime, why: str) -> None:
        order.status = OrderStatus.REJECTED
        order.updated_at = when
        order.raw["reject_reason"] = why
        logger.warning("[paper] 주문 거부 %s %s: %s", order.id, order.symbol, why)

    def _open_orders_for(self, symbol: str | None) -> list[Order]:
        return [
            o
            for o in self._orders.values()
            if o.status == OrderStatus.OPEN and (symbol is None or o.symbol == symbol)
        ]

    # ------------------------------------------------------------------ 체결 판정
    def _candle_fill_price(self, order: Order, candle: Candle) -> float | None:
        """캔들 OHLC 로 미체결 주문의 체결가를 구한다. 체결되지 않으면 None."""
        if order.type == OrderType.LIMIT:
            assert order.price is not None
            # 갭 통과(gap-through): 시가가 이미 지정가를 지나쳤으면 지정가가 아니라 시가에 체결된다
            # (매수는 시가 이하, 매도는 시가 이상으로만). 따라서 체결가는 항상 [low, high] 안에 있고,
            # 지정가에 체결됐다는 것은 bar 중간(시가 이후)에 체결됐다는 뜻이다.
            if order.side == OrderSide.BUY:
                if candle.low > order.price:
                    return None
                base = min(order.price, candle.open)
            else:
                if candle.high < order.price:
                    return None
                base = max(order.price, candle.open)
            order.raw["fill_basis"] = "open" if base == candle.open else "limit"
            return base

        if order.type == OrderType.STOP:
            offset = float(order.raw.get("stop_offset", 0.0))
            if order.side == OrderSide.BUY:
                trigger = order.price if order.price is not None else candle.open + offset
                if candle.open >= trigger:
                    base = candle.open
                elif candle.high >= trigger:
                    base = trigger
                else:
                    return None
                order.raw["trigger"] = trigger
                order.raw["fill_basis"] = "open" if base == candle.open else "trigger"
                return base * (1.0 + self._slippage_pct)
            trigger = order.price if order.price is not None else candle.open - offset
            if candle.open <= trigger:
                base = candle.open
            elif candle.low <= trigger:
                base = trigger
            else:
                return None
            order.raw["trigger"] = trigger
            order.raw["fill_basis"] = "open" if base == candle.open else "trigger"
            return base * (1.0 - self._slippage_pct)
        return None

    def process_candle(self, symbol: str, candle: Candle) -> list[Order]:
        """캔들 하나로 해당 심볼의 미체결 LIMIT/STOP 주문을 체결 판정한다 (백테스트용).

        체결 시각은 ``candle.timestamp`` 이며 시뮬레이션 시각도 그 값으로 옮긴다. 체결된 주문 스냅샷을
        반환한다. 자금 부족으로 체결하지 못한 주문은 REJECTED 로 바뀐다 (반환 목록에는 없음).

        - LIMIT BUY : ``low <= price`` 면 ``min(price, open)`` 에 체결 (시가가 지정가 아래면 시가).
        - LIMIT SELL: ``high >= price`` 면 ``max(price, open)`` 에 체결 (시가가 지정가 위면 시가).
        - STOP BUY  : ``open >= trigger`` 면 시가, ``high >= trigger`` 면 트리거 (+슬리피지).
        - STOP SELL : ``open <= trigger`` 면 시가, ``low <= trigger`` 면 트리거 (-슬리피지).
        체결 근거는 ``raw["fill_basis"]`` 에 ``open`` / ``limit`` / ``trigger`` 로 남는다.
        """
        when = candle.timestamp
        self._sim_time = when
        filled: list[Order] = []
        for order in self._open_orders_for(symbol):
            fill_price = self._candle_fill_price(order, candle)
            if fill_price is None:
                continue
            if self._apply_fill(order, fill_price, when):
                filled.append(self._snapshot(order))
        return filled

    def check_pending(self, symbol: str, price: float) -> list[Order]:
        """현재가 기준으로 미체결 LIMIT/STOP 주문을 체결 판정한다 (모의투자 폴링용).

        ``stop_offset`` 만 있는 STOP 주문은 기준 시가를 알 수 없으므로 여기서는 건너뛴다
        (``process_candle`` 로만 체결된다). LIMIT 은 현재가가 지정가를 지나쳤으면(매수 ``price < 지정가``,
        매도 ``price > 지정가``) 현재가에, 정확히 지정가면 지정가에 체결한다 — STOP 과 같이 "조건을 만족한
        시점의 시장 가격" 이 체결가이며 지정가 바깥의 가격은 만들지 않는다.
        """
        if not _is_positive_finite(price):
            raise DataError(f"{symbol} 현재가가 유효하지 않습니다: {price!r}")
        price = float(price)
        when = self._now()
        filled: list[Order] = []
        for order in self._open_orders_for(symbol):
            fill_price: float | None = None
            if order.type == OrderType.LIMIT:
                assert order.price is not None
                if order.side == OrderSide.BUY and price <= order.price:
                    fill_price = price
                elif order.side == OrderSide.SELL and price >= order.price:
                    fill_price = price
                if fill_price is not None:
                    order.raw["fill_basis"] = "limit" if fill_price == order.price else "market"
            elif order.type == OrderType.STOP:
                if order.price is None:
                    logger.debug("[paper] %s 는 stop_offset 주문이라 check_pending 에서 건너뜁니다", order.id)
                    continue
                if order.side == OrderSide.BUY and price >= order.price:
                    fill_price = price * (1.0 + self._slippage_pct)
                elif order.side == OrderSide.SELL and price <= order.price:
                    fill_price = price * (1.0 - self._slippage_pct)
                if fill_price is not None:
                    order.raw["trigger"] = order.price
                    order.raw["fill_basis"] = "market"
            if fill_price is None:
                continue
            if self._apply_fill(order, fill_price, when):
                filled.append(self._snapshot(order))
        return filled

    # ------------------------------------------------------------------ 주문 조회/취소
    def _get(self, order_id: str, symbol: str | None) -> Order:
        order = self._orders.get(order_id)
        if order is None:
            raise OrderError(f"알 수 없는 주문 id: {order_id!r}")
        if symbol is not None and order.symbol != symbol:
            raise OrderError(f"주문 {order_id} 의 심볼은 {order.symbol} 입니다 (요청: {symbol})")
        return order

    def cancel_order(self, order_id: str, symbol: str | None = None) -> bool:
        order = self._get(order_id, symbol)
        if order.status != OrderStatus.OPEN:
            logger.debug("[paper] 주문 %s 는 이미 %s 상태라 취소하지 않습니다", order_id, order.status.value)
            return False
        order.status = OrderStatus.CANCELED
        order.updated_at = self._now()
        logger.info("[paper] 주문 취소 %s %s", order_id, order.symbol)
        return True

    def get_order(self, order_id: str, symbol: str | None = None) -> Order:
        return self._snapshot(self._get(order_id, symbol))

    def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        return [self._snapshot(o) for o in self._open_orders_for(symbol)]

    # ------------------------------------------------------------------ 계좌
    def get_balances(self) -> dict[str, Balance]:
        """quote 통화 + 보유 기초자산. locked = 미체결 LIMIT 매수(quote) / 미체결 매도(base) 잠금."""
        out: dict[str, Balance] = {
            self._quote: Balance(
                currency=self._quote,
                total=self._cash,
                available=max(self._cash - self._reserved_quote(), 0.0),
            )
        }
        totals: dict[str, float] = {}
        reserved: dict[str, float] = {}
        for symbol, pos in self._positions.items():
            if pos.quantity <= 0:
                continue
            base = self.base_currency(symbol)
            totals[base] = totals.get(base, 0.0) + pos.quantity
            reserved[base] = reserved.get(base, 0.0) + self._reserved_base(symbol)
        for base, total in totals.items():
            out[base] = Balance(
                currency=base, total=total, available=max(total - reserved.get(base, 0.0), 0.0)
            )
        return out

    def get_positions(self) -> dict[str, Position]:
        """보유 포지션. 반환되는 Position 객체는 브로커 내부 객체와 동일하므로
        RiskManager 가 stop_loss/highest_price/meta 를 갱신하면 to_dict() 에도 반영된다.
        quantity/average_price 는 외부에서 바꾸지 말 것."""
        return {s: p for s, p in self._positions.items() if p.quantity > 0}

    def get_equity(self, symbols: list[str] | None = None) -> float:
        """현금 + Σ 포지션 평가액. 평가 가격: mark 된 가격 > data_source 현재가 > 평균단가(원가)."""
        value = 0.0
        for symbol, pos in self._positions.items():
            if pos.quantity <= 0:
                continue
            price = self._prices.get(symbol)
            if price is None and self._data_source is not None:
                try:
                    price = float(self._data_source.get_ticker(symbol))
                except Exception as e:  # noqa: BLE001 - 평가액 계산은 실패해도 원가로 대체
                    logger.warning("[paper] %s 평가 시세 조회 실패, 원가로 평가합니다: %s", symbol, e)
                    price = None
            if price is None or not _is_positive_finite(price):
                price = pos.average_price
            value += pos.market_value(price)
        return self._cash + value

    # ------------------------------------------------------------------ 상태 저장/복원
    def to_dict(self) -> dict[str, Any]:
        """JSON 직렬화 가능한 상태 (datetime → ISO 문자열, Enum → value)."""
        return {
            "version": STATE_VERSION,
            "name": self.name,
            "initial_cash": self._initial_cash,
            "cash": self._cash,
            "quote_currency": self._quote,
            "fee_pct": self._fee_pct,
            "slippage_pct": self._slippage_pct,
            "asset_class": self.asset_class.value,
            "min_order_value": self._min_order_value,
            "order_seq": self._order_seq,
            "sim_time": _dt_to_iso(self._sim_time),
            "prices": dict(self._prices),
            "positions": {s: self._position_to_dict(p) for s, p in self._positions.items() if p.quantity > 0},
            "position_fees": {s: f for s, f in self._position_fees.items() if s in self._positions},
            "orders": [self._order_to_dict(o) for o in self._orders.values()],
            "trades": [self._trade_to_dict(t) for t in self._trades],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any], data_source: BaseBroker | None = None) -> PaperBroker:
        """``to_dict()`` 결과로부터 복원. 손상된 데이터는 DataError."""
        if not isinstance(d, dict):
            raise DataError(f"PaperBroker 상태는 dict 여야 합니다: {type(d).__name__}")
        version = d.get("version", STATE_VERSION)
        if version != STATE_VERSION:
            raise DataError(f"지원하지 않는 PaperBroker 상태 버전: {version!r} (지원: {STATE_VERSION})")
        try:
            asset_class = AssetClass(d.get("asset_class", AssetClass.CRYPTO.value))
            broker = cls(
                initial_cash=float(d["initial_cash"]),
                quote_currency=str(d["quote_currency"]),
                fee_pct=float(d.get("fee_pct", 0.0)),
                slippage_pct=float(d.get("slippage_pct", 0.0)),
                data_source=data_source,
                asset_class=asset_class,
                min_order_value=float(d.get("min_order_value", 0.0)),
            )
            cash = float(d["cash"])
            if not math.isfinite(cash) or cash < 0:
                raise ValueError(f"cash 가 유효하지 않습니다: {cash!r}")
            broker._cash = cash
            broker._order_seq = int(d.get("order_seq", 0))
            broker._sim_time = _iso_to_dt(d.get("sim_time"))
            broker._prices = {str(s): float(p) for s, p in (d.get("prices") or {}).items()}
            broker._positions = {
                str(s): cls._position_from_dict(str(s), p) for s, p in (d.get("positions") or {}).items()
            }
            broker._position_fees = {str(s): float(f) for s, f in (d.get("position_fees") or {}).items()}
            for o in d.get("orders") or []:
                order = cls._order_from_dict(o)
                broker._orders[order.id] = order
            broker._trades = [cls._trade_from_dict(t) for t in d.get("trades") or []]
        except (KeyError, ValueError, TypeError, AttributeError, ConfigError) as e:
            raise DataError(f"PaperBroker 상태 복원 실패: {e}") from e

        # 주문 id 시퀀스가 저장된 주문들과 어긋나지 않게 보정
        for order_id in broker._orders:
            if order_id.startswith("paper-"):
                try:
                    broker._order_seq = max(broker._order_seq, int(order_id.split("-", 1)[1]))
                except ValueError:
                    continue
        if data_source is not None and AssetClass(data_source.asset_class) != broker.asset_class:
            logger.warning(
                "[paper] 저장된 asset_class=%s 와 data_source asset_class=%s 가 다릅니다",
                broker.asset_class.value,
                AssetClass(data_source.asset_class).value,
            )
        return broker

    @staticmethod
    def _position_to_dict(p: Position) -> dict[str, Any]:
        return {
            "symbol": p.symbol,
            "quantity": p.quantity,
            "average_price": p.average_price,
            "opened_at": _dt_to_iso(p.opened_at),
            "highest_price": p.highest_price,
            "stop_loss": p.stop_loss,
            "take_profit": p.take_profit,
            "meta": _jsonable(p.meta),
        }

    @staticmethod
    def _position_from_dict(symbol: str, p: dict[str, Any]) -> Position:
        quantity = float(p["quantity"])
        average_price = float(p["average_price"])
        if not math.isfinite(quantity) or quantity <= 0:
            raise ValueError(f"{symbol} 포지션 수량이 유효하지 않습니다: {quantity!r}")
        if not math.isfinite(average_price) or average_price <= 0:
            raise ValueError(f"{symbol} 포지션 평균단가가 유효하지 않습니다: {average_price!r}")
        return Position(
            symbol=str(p.get("symbol", symbol)),
            quantity=quantity,
            average_price=average_price,
            opened_at=_iso_to_dt(p.get("opened_at")),
            highest_price=_opt_float(p.get("highest_price")),
            stop_loss=_opt_float(p.get("stop_loss")),
            take_profit=_opt_float(p.get("take_profit")),
            meta=dict(p.get("meta") or {}),
        )

    @staticmethod
    def _order_to_dict(o: Order) -> dict[str, Any]:
        return {
            "id": o.id,
            "symbol": o.symbol,
            "side": o.side.value,
            "type": o.type.value,
            "quantity": o.quantity,
            "price": o.price,
            "status": o.status.value,
            "filled_quantity": o.filled_quantity,
            "average_price": o.average_price,
            "fee": o.fee,
            "created_at": _dt_to_iso(o.created_at),
            "updated_at": _dt_to_iso(o.updated_at),
            "raw": _jsonable(o.raw),
        }

    @staticmethod
    def _order_from_dict(o: dict[str, Any]) -> Order:
        created = _iso_to_dt(o.get("created_at"))
        return Order(
            id=str(o["id"]),
            symbol=str(o["symbol"]),
            side=OrderSide(o["side"]),
            type=OrderType(o["type"]),
            quantity=float(o["quantity"]),
            price=_opt_float(o.get("price")),
            status=OrderStatus(o.get("status", OrderStatus.OPEN.value)),
            filled_quantity=float(o.get("filled_quantity", 0.0)),
            average_price=_opt_float(o.get("average_price")),
            fee=float(o.get("fee", 0.0)),
            created_at=created if created is not None else utcnow(),
            updated_at=_iso_to_dt(o.get("updated_at")),
            raw=dict(o.get("raw") or {}),
        )

    @staticmethod
    def _trade_to_dict(t: Trade) -> dict[str, Any]:
        return {
            "symbol": t.symbol,
            "side": t.side.value,
            "quantity": t.quantity,
            "entry_price": t.entry_price,
            "exit_price": t.exit_price,
            "entry_time": _dt_to_iso(t.entry_time),
            "exit_time": _dt_to_iso(t.exit_time),
            "fee": t.fee,
            "reason": t.reason,
        }

    @staticmethod
    def _trade_from_dict(t: dict[str, Any]) -> Trade:
        entry_time = _iso_to_dt(t["entry_time"])
        exit_time = _iso_to_dt(t["exit_time"])
        if entry_time is None or exit_time is None:
            raise ValueError("Trade 의 entry_time / exit_time 이 없습니다")
        return Trade(
            symbol=str(t["symbol"]),
            side=OrderSide(t.get("side", OrderSide.BUY.value)),
            quantity=float(t["quantity"]),
            entry_price=float(t["entry_price"]),
            exit_price=float(t["exit_price"]),
            entry_time=entry_time,
            exit_time=exit_time,
            fee=float(t.get("fee", 0.0)),
            reason=str(t.get("reason", "") or ""),
        )

    def __repr__(self) -> str:
        return (
            f"<PaperBroker quote={self._quote} cash={self._cash:.2f} positions={len(self._positions)} "
            f"open_orders={len(self._open_orders_for(None))} trades={len(self._trades)}>"
        )


__all__ = ["PaperBroker", "STATE_VERSION"]
