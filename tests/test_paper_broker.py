"""PaperBroker 테스트.

시세는 전부 tests/conftest.py 가 Upbit 공개 API 에서 받아 캐시한 **실제** KRW-BTC / KRW-ETH 캔들을 쓴다
(네트워크가 없으면 해당 테스트는 skip). 가짜/데모 가격은 만들지 않는다 — 계좌 설정값(초기 현금, 수수료율)만 상수다.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone

import pytest

from tradingbot.brokers import create_broker
from tradingbot.brokers.base import BaseBroker
from tradingbot.brokers.paper import STATE_VERSION, PaperBroker
from tradingbot.config import AppConfig, PaperConfig, RiskConfig
from tradingbot.exceptions import (
    AuthenticationError,
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
)
from tradingbot.strategies.base import df_to_candles

BTC = "KRW-BTC"
ETH = "KRW-ETH"
# 계좌 설정 (시세가 아님)
INITIAL_CASH = 10_000_000.0
FEE = 0.0005
SLIP = 0.001

approx = pytest.approx


# ---------------------------------------------------------------------------- 테스트용 시세 공급원
class RealCandleFeed(BaseBroker):
    """conftest 의 실제 Upbit 캔들을 그대로 서빙하는 시세 전용 브로커 (주문 기능 없음)."""

    name = "feed"
    asset_class = AssetClass.CRYPTO
    supported_intervals = ("1h", "1d")

    def __init__(self, candles_by_symbol: dict[str, list[Candle]]) -> None:
        self._candles = candles_by_symbol
        self.ticker_calls = 0
        self.last_get_candles_args: tuple | None = None

    def get_candles(self, symbol, interval, limit=200, end=None, include_partial=False):
        self.last_get_candles_args = (symbol, interval, limit, end, include_partial)
        if symbol not in self._candles:
            raise BrokerError(f"feed 에 없는 심볼: {symbol}")
        return list(self._candles[symbol][-limit:])

    def get_ticker(self, symbol):
        self.ticker_calls += 1
        if symbol not in self._candles:
            raise BrokerError(f"feed 에 없는 심볼: {symbol}")
        return self._candles[symbol][-1].close

    def get_balances(self):
        return {}

    def get_positions(self):
        return {}

    def place_order(self, symbol, side, quantity, order_type=OrderType.MARKET, price=None):
        raise AuthenticationError("시세 전용 feed 는 주문을 지원하지 않습니다")

    def cancel_order(self, order_id, symbol=None):
        raise AuthenticationError("시세 전용 feed 는 주문을 지원하지 않습니다")

    def get_order(self, order_id, symbol=None):
        raise AuthenticationError("시세 전용 feed 는 주문을 지원하지 않습니다")

    def get_open_orders(self, symbol=None):
        return []

    def quote_currency(self, symbol):
        return symbol.split("-")[0]

    def base_currency(self, symbol):
        return symbol.split("-")[1]

    def min_order_value(self, symbol):
        return 5000.0

    def round_quantity(self, symbol, quantity):
        return math.floor(quantity * 1e4) / 1e4

    def round_price(self, symbol, price):
        return float(round(price, -3))


class StockFeed(BaseBroker):
    """주식(USD) 시세 공급원 흉내. 시세는 제공하지 않고 메타 정보만 (from_config 테스트용)."""

    name = "stockfeed"
    asset_class = AssetClass.STOCK
    supported_intervals = ("1d",)

    def __init__(self) -> None:
        self.market_open = False

    def get_candles(self, symbol, interval, limit=200, end=None, include_partial=False):
        raise BrokerError("시세 없음")

    def get_ticker(self, symbol):
        raise BrokerError("시세 없음")

    def get_balances(self):
        return {}

    def get_positions(self):
        return {}

    def place_order(self, symbol, side, quantity, order_type=OrderType.MARKET, price=None):
        raise AuthenticationError("주문 불가")

    def cancel_order(self, order_id, symbol=None):
        raise AuthenticationError("주문 불가")

    def get_order(self, order_id, symbol=None):
        raise AuthenticationError("주문 불가")

    def get_open_orders(self, symbol=None):
        return []

    def quote_currency(self, symbol):
        return "USD"

    def is_market_open(self):
        return self.market_open


class BrokenQuoteFeed(StockFeed):
    def quote_currency(self, symbol):
        raise BrokerError("마켓 정보 조회 실패")


# ---------------------------------------------------------------------------- 픽스처 / 헬퍼
@pytest.fixture
def last(daily_candles) -> Candle:
    """가장 최근 실제 일봉."""
    return daily_candles[-1]


@pytest.fixture
def prev(daily_candles) -> Candle:
    return daily_candles[-2]


@pytest.fixture
def broker(last) -> PaperBroker:
    """최근 실제 종가로 mark 된 PaperBroker."""
    b = PaperBroker(initial_cash=INITIAL_CASH, fee_pct=FEE, slippage_pct=SLIP)
    b.mark_price(BTC, last.close, timestamp=last.timestamp)
    return b


def qty_for(broker: PaperBroker, symbol: str, cash_fraction: float, price: float) -> float:
    return broker.round_quantity(symbol, broker.cash * cash_fraction / price)


def buy_fill(price: float) -> float:
    return price * (1 + SLIP)


def sell_fill(price: float) -> float:
    return price * (1 - SLIP)


# ============================================================================ 생성 / 설정
class TestConstruction:
    def test_defaults(self):
        b = PaperBroker(initial_cash=INITIAL_CASH)
        assert b.name == "paper"
        assert b.cash == INITIAL_CASH
        assert b.initial_cash == INITIAL_CASH
        assert b.fee_pct == 0.0005
        assert b.slippage_pct == 0.0005
        assert b.asset_class == AssetClass.CRYPTO
        assert b.data_source is None
        assert b.supported_intervals == ()
        assert b.trades == []
        assert b.orders == []
        assert b.get_positions() == {}
        assert b.get_open_orders() == []
        assert b.get_equity() == INITIAL_CASH
        assert b.is_market_open() is True
        assert b.get_balances() == {"KRW": Balance("KRW", INITIAL_CASH, INITIAL_CASH)}

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"initial_cash": 0},
            {"initial_cash": -1},
            {"initial_cash": float("nan")},
            {"initial_cash": float("inf")},
            {"initial_cash": 1.0, "fee_pct": -0.1},
            {"initial_cash": 1.0, "fee_pct": 1.0},
            {"initial_cash": 1.0, "slippage_pct": -0.01},
            {"initial_cash": 1.0, "slippage_pct": 1.5},
            {"initial_cash": 1.0, "min_order_value": -5},
            {"initial_cash": 1.0, "quote_currency": "  "},
        ],
    )
    def test_invalid_settings_raise_config_error(self, kwargs):
        with pytest.raises(ConfigError):
            PaperBroker(**kwargs)

    def test_from_config_uses_paper_section(self):
        cfg = AppConfig(
            symbols=["BTC/USDT"],
            paper=PaperConfig(
                initial_cash=5_000_000, quote_currency="USDT", fee_pct=0.001, slippage_pct=0.002
            ),
            risk=RiskConfig(min_order_value=1000),
        )
        b = PaperBroker.from_config(cfg)
        assert b.cash == 5_000_000
        assert b.fee_pct == 0.001
        assert b.slippage_pct == 0.002
        assert b.asset_class == AssetClass.CRYPTO
        assert b.quote_currency("BTC/USDT") == "USDT"
        assert b.quote_currency("XYZ") == "USDT"  # 파싱 불가 → 설정 통화
        assert b.min_order_value("BTC/USDT") == 1000

    def test_from_config_follows_data_source(self):
        cfg = AppConfig(symbols=["AAPL"], paper=PaperConfig(quote_currency="KRW"))
        feed = StockFeed()
        b = PaperBroker.from_config(cfg, data_source=feed)
        assert b.asset_class == AssetClass.STOCK
        assert b.quote_currency("AAPL") == "USD"
        assert b.base_currency("AAPL") == "AAPL"
        # 주식에서는 '-' 가 있어도 암호화폐 표기로 파싱하지 않는다
        assert b.quote_currency("BRK-B") == "USD"
        assert b.base_currency("BRK-B") == "BRK-B"
        assert b.supported_intervals == ("1d",)
        assert b.is_market_open() is False
        feed.market_open = True
        assert b.is_market_open() is True

    def test_from_config_falls_back_when_data_source_quote_fails(self, caplog):
        cfg = AppConfig(symbols=["AAPL"], paper=PaperConfig(quote_currency="USD"))
        with caplog.at_level(logging.WARNING, logger="tradingbot.brokers.paper"):
            b = PaperBroker.from_config(cfg, data_source=BrokenQuoteFeed())
        assert b.quote_currency("AAPL") == "USD"
        assert b.asset_class == AssetClass.STOCK
        assert any("결제통화" in r.message for r in caplog.records)

    def test_registry_creates_paper_broker(self):
        cfg = AppConfig(paper=PaperConfig(initial_cash=123_456))
        b = create_broker("paper", cfg)
        assert isinstance(b, PaperBroker)
        assert b.cash == 123_456

    @pytest.mark.parametrize(
        ("symbol", "quote", "base"),
        [
            ("KRW-BTC", "KRW", "BTC"),
            ("BTC/USDT", "USDT", "BTC"),
            ("BTC/USDT:USDT", "USDT", "BTC"),
            ("005930", "KRW", "005930"),
            ("AAPL", "KRW", "AAPL"),
            ("-BTC", "KRW", "-BTC"),
            ("BTC/", "KRW", "BTC/"),
        ],
    )
    def test_symbol_parsing_crypto(self, symbol, quote, base):
        b = PaperBroker(initial_cash=1.0, quote_currency="KRW")
        assert b.quote_currency(symbol) == quote
        assert b.base_currency(symbol) == base

    def test_repr_has_no_secrets_and_summarises(self):
        b = PaperBroker(initial_cash=INITIAL_CASH)
        assert "PaperBroker" in repr(b)
        assert "KRW" in repr(b)


# ============================================================================ 시세
class TestMarketData:
    def test_get_ticker_precedence(self, daily_candles, last, prev):
        b = PaperBroker(initial_cash=INITIAL_CASH)
        with pytest.raises(BrokerError):
            b.get_ticker(BTC)
        feed = RealCandleFeed({BTC: daily_candles})
        b = PaperBroker(initial_cash=INITIAL_CASH, data_source=feed)
        assert b.get_ticker(BTC) == last.close
        assert feed.ticker_calls == 1
        b.mark_price(BTC, prev.close)
        assert b.get_ticker(BTC) == prev.close
        assert feed.ticker_calls == 1  # mark 된 가격이 우선, feed 호출 없음
        with pytest.raises(BrokerError):
            b.get_ticker(ETH)  # feed 에 없는 심볼은 feed 의 BrokerError 전파

    @pytest.mark.parametrize("bad", [0, -1.0, float("nan"), float("inf")])
    def test_mark_price_rejects_invalid(self, bad):
        b = PaperBroker(initial_cash=INITIAL_CASH)
        with pytest.raises(DataError):
            b.mark_price(BTC, bad)

    def test_get_candles_delegates_or_raises(self, daily_candles):
        b = PaperBroker(initial_cash=INITIAL_CASH)
        with pytest.raises(DataError):
            b.get_candles(BTC, "1d")
        feed = RealCandleFeed({BTC: daily_candles})
        b = PaperBroker(initial_cash=INITIAL_CASH, data_source=feed)
        end = daily_candles[-1].timestamp
        out = b.get_candles(BTC, "1d", limit=10, end=end, include_partial=True)
        assert out == daily_candles[-10:]
        assert feed.last_get_candles_args == (BTC, "1d", 10, end, True)

    def test_round_helpers_delegate_to_data_source(self, daily_candles, last):
        b = PaperBroker(initial_cash=INITIAL_CASH)
        assert b.round_quantity(BTC, 0.123456789123) == 0.12345678
        assert b.round_price(BTC, last.close) == last.close
        b = PaperBroker(initial_cash=INITIAL_CASH, data_source=RealCandleFeed({BTC: daily_candles}))
        assert b.round_quantity(BTC, 0.123456789123) == 0.1234
        assert b.round_price(BTC, last.close) == float(round(last.close, -3))
        assert b.min_order_value(BTC) == 5000.0
        b2 = PaperBroker(initial_cash=INITIAL_CASH, data_source=RealCandleFeed({}), min_order_value=7000)
        assert b2.min_order_value(BTC) == 7000.0

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (0.29, 0.29),  # math.floor(0.29 * 1e8) / 1e8 == 0.28999999 (부동소수 오차) 가 나지 않아야 한다
            (2.675, 2.675),
            (0.123456789123, 0.12345678),
            (1e-9, 0.0),
            (0.0, 0.0),
            (123456.789, 123456.789),
        ],
    )
    def test_default_round_quantity_has_no_float_artifacts(self, raw, expected):
        b = PaperBroker(initial_cash=INITIAL_CASH)
        assert b.round_quantity(BTC, raw) == expected
        assert math.isnan(b.round_quantity(BTC, float("nan")))


# ============================================================================ 시장가 체결
class TestMarketFills:
    def test_market_buy_fill_math(self, broker, last):
        qty = qty_for(broker, BTC, 0.1, last.close)
        order = broker.place_order(BTC, OrderSide.BUY, qty)
        fill = buy_fill(last.close)
        fee = fill * qty * FEE

        assert order.id == "paper-1"
        assert order.status == OrderStatus.FILLED
        assert order.is_filled
        assert order.type == OrderType.MARKET
        assert order.side == OrderSide.BUY
        assert order.price is None
        assert order.quantity == qty
        assert order.filled_quantity == qty
        assert order.remaining_quantity == 0.0
        assert order.average_price == approx(fill)
        assert order.fee == approx(fee)
        assert order.created_at == last.timestamp
        assert order.updated_at == last.timestamp
        assert order.raw["reason"] == ""

        assert broker.cash == approx(INITIAL_CASH - fill * qty - fee)
        pos = broker.get_positions()[BTC]
        assert pos.quantity == qty
        assert pos.average_price == approx(fill)  # 수수료 제외 평균단가
        assert pos.opened_at == last.timestamp
        bal = broker.get_balances()
        assert set(bal) == {"KRW", "BTC"}
        assert bal["KRW"].total == approx(broker.cash)
        assert bal["KRW"].available == approx(broker.cash)
        assert bal["BTC"] == Balance("BTC", qty, qty)
        assert broker.get_equity() == approx(broker.cash + qty * last.close)
        assert broker.trades == []
        assert broker.get_open_orders() == []

        second = broker.place_order(BTC, OrderSide.BUY, qty)
        assert second.id == "paper-2"
        assert [o.id for o in broker.orders] == ["paper-1", "paper-2"]

    def test_market_sell_round_trip_trade_math(self, broker, last, prev):
        qty = qty_for(broker, BTC, 0.2, last.close)
        buy = broker.place_order(BTC, OrderSide.BUY, qty)
        cash_after_buy = broker.cash

        # 다른 실제 종가로 이동한 뒤 전량 매도
        later = last.timestamp + timedelta(days=1)
        broker.mark_price(BTC, prev.close, timestamp=later)
        sell = broker.place_order(BTC, OrderSide.SELL, qty, reason="테스트 청산")
        exit_price = sell_fill(prev.close)
        sell_fee = exit_price * qty * FEE

        assert sell.id == "paper-2"
        assert sell.status == OrderStatus.FILLED
        assert sell.average_price == approx(exit_price)
        assert sell.fee == approx(sell_fee)
        assert sell.updated_at == later
        assert broker.cash == approx(cash_after_buy + exit_price * qty - sell_fee)
        assert broker.get_positions() == {}
        assert "BTC" not in broker.get_balances()

        trades = broker.trades
        assert len(trades) == 1
        t = trades[0]
        assert isinstance(t, Trade)
        assert t.symbol == BTC
        assert t.side == OrderSide.BUY
        assert t.quantity == qty
        assert t.entry_price == approx(buy.average_price)
        assert t.exit_price == approx(exit_price)
        assert t.entry_time == last.timestamp
        assert t.exit_time == later
        assert t.holding_seconds == 86400
        assert t.fee == approx(buy.fee + sell_fee)
        assert t.reason == "테스트 청산"
        expected_pnl = (exit_price - buy.average_price) * qty - (buy.fee + sell_fee)
        assert t.pnl == approx(expected_pnl)
        assert t.pnl_pct == approx(expected_pnl / (buy.average_price * qty))
        # 현금 변화 == 실현 손익 (수수료 포함)
        assert broker.cash - INITIAL_CASH == approx(t.pnl)
        assert broker.get_equity() == approx(broker.cash)

    def test_partial_sells_prorate_buy_fee(self, broker, last, prev):
        qty = qty_for(broker, BTC, 0.3, last.close)
        buy = broker.place_order(BTC, OrderSide.BUY, qty)
        broker.mark_price(BTC, prev.close)
        part = broker.round_quantity(BTC, qty * 0.4)
        rest = qty - part

        s1 = broker.place_order(BTC, OrderSide.SELL, part, reason="부분 청산")
        pos = broker.get_positions()[BTC]
        assert pos.quantity == approx(rest)
        assert pos.average_price == approx(buy.average_price)  # 부분 매도는 평단 불변
        assert pos.opened_at == last.timestamp
        assert len(broker.trades) == 1
        t1 = broker.trades[0]
        assert t1.quantity == part
        assert t1.fee == approx(buy.fee * (part / qty) + s1.fee)
        assert t1.reason == "부분 청산"
        assert broker.get_balances()["BTC"].total == approx(rest)

        s2 = broker.place_order(BTC, OrderSide.SELL, rest)
        assert broker.get_positions() == {}
        t2 = broker.trades[1]
        assert t2.quantity == approx(rest)
        assert t2.fee == approx(buy.fee * (rest / qty) + s2.fee)
        assert t2.reason == ""
        assert t1.fee + t2.fee == approx(buy.fee + s1.fee + s2.fee)
        assert broker.cash - INITIAL_CASH == approx(t1.pnl + t2.pnl)

    def test_pyramiding_weighted_average_and_fee_accumulation(self, broker, last, prev):
        q1 = qty_for(broker, BTC, 0.2, last.close)
        b1 = broker.place_order(BTC, OrderSide.BUY, q1)
        later = last.timestamp + timedelta(hours=1)
        broker.mark_price(BTC, prev.close, timestamp=later)
        q2 = qty_for(broker, BTC, 0.2, prev.close)
        b2 = broker.place_order(BTC, OrderSide.BUY, q2)

        pos = broker.get_positions()[BTC]
        assert pos.quantity == approx(q1 + q2)
        assert pos.average_price == approx((q1 * b1.average_price + q2 * b2.average_price) / (q1 + q2))
        assert pos.opened_at == last.timestamp  # 최초 진입 시각 유지

        sell = broker.place_order(BTC, OrderSide.SELL, pos.quantity)
        t = broker.trades[0]
        assert t.entry_price == approx(pos.average_price)
        assert t.fee == approx(b1.fee + b2.fee + sell.fee)
        assert t.entry_time == last.timestamp
        assert t.exit_time == later
        assert broker.cash - INITIAL_CASH == approx(t.pnl)

    def test_market_order_ignores_price_argument(self, broker, last):
        qty = qty_for(broker, BTC, 0.05, last.close)
        order = broker.place_order(BTC, OrderSide.BUY, qty, OrderType.MARKET, price=last.close * 0.5)
        assert order.status == OrderStatus.FILLED
        assert order.price is None
        assert order.average_price == approx(buy_fill(last.close))

    def test_string_side_and_type_are_accepted(self, broker, last):
        qty = qty_for(broker, BTC, 0.05, last.close)
        order = broker.place_order(BTC, "buy", qty, "market")
        assert order.side == OrderSide.BUY and order.type == OrderType.MARKET
        with pytest.raises(OrderError):
            broker.place_order(BTC, "short", qty)
        with pytest.raises(OrderError):
            broker.place_order(BTC, OrderSide.BUY, qty, "iceberg")

    def test_clock_used_until_simulation_time_is_set(self, last):
        b = PaperBroker(initial_cash=INITIAL_CASH, fee_pct=FEE, slippage_pct=SLIP)
        fixed = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        b.clock = lambda: fixed
        b.mark_price(BTC, last.close)  # timestamp 없음 → clock 사용
        qty = qty_for(b, BTC, 0.1, last.close)
        o = b.place_order(BTC, OrderSide.BUY, qty)
        assert o.created_at == fixed
        assert b.get_positions()[BTC].opened_at == fixed
        b.mark_price(BTC, last.close, timestamp=last.timestamp)
        o2 = b.place_order(BTC, OrderSide.SELL, qty)
        assert o2.updated_at == last.timestamp
        assert b.trades[0].entry_time == fixed
        assert b.trades[0].exit_time == last.timestamp

    def test_trades_and_orders_properties_return_copies(self, broker, last):
        qty = qty_for(broker, BTC, 0.1, last.close)
        broker.place_order(BTC, OrderSide.BUY, qty)
        broker.place_order(BTC, OrderSide.SELL, qty)
        trades = broker.trades
        trades.clear()
        assert len(broker.trades) == 1
        orders = broker.orders
        orders[0].status = OrderStatus.CANCELED
        orders[0].raw["x"] = 1
        assert broker.get_order("paper-1").status == OrderStatus.FILLED
        assert "x" not in broker.get_order("paper-1").raw


# ============================================================================ 오류 경로
class TestErrors:
    def test_buy_more_than_cash_is_rejected_without_side_effects(self, broker, last):
        qty = broker.round_quantity(BTC, INITIAL_CASH / last.close)  # 슬리피지+수수료만큼 초과
        with pytest.raises(InsufficientFunds):
            broker.place_order(BTC, OrderSide.BUY, qty)
        assert broker.cash == INITIAL_CASH
        assert broker.get_positions() == {}
        assert broker.orders == []
        ok = broker.place_order(BTC, OrderSide.BUY, broker.round_quantity(BTC, qty * 0.5))
        assert ok.id == "paper-1"  # 거부된 주문은 id 를 소비하지 않는다

    def test_exact_affordable_buy_passes(self, broker, last):
        fill = buy_fill(last.close)
        qty = INITIAL_CASH / (fill * (1 + FEE))  # 수수료 포함 정확히 전액
        order = broker.place_order(BTC, OrderSide.BUY, qty)
        assert order.status == OrderStatus.FILLED
        assert broker.cash == approx(0.0, abs=1e-6)

    def test_sell_without_position(self, broker):
        with pytest.raises(InsufficientFunds):
            broker.place_order(BTC, OrderSide.SELL, 0.001)
        with pytest.raises(InsufficientFunds):
            broker.place_order(BTC, OrderSide.SELL, 0.001, OrderType.LIMIT, price=1.0)

    def test_sell_more_than_held(self, broker, last):
        qty = qty_for(broker, BTC, 0.1, last.close)
        broker.place_order(BTC, OrderSide.BUY, qty)
        with pytest.raises(InsufficientFunds):
            broker.place_order(BTC, OrderSide.SELL, qty * 1.01)
        assert broker.get_positions()[BTC].quantity == qty
        assert broker.trades == []

    @pytest.mark.parametrize("bad_qty", [0, -0.1, float("nan"), float("inf"), True])
    def test_invalid_quantity(self, broker, bad_qty):
        with pytest.raises(OrderError):
            broker.place_order(BTC, OrderSide.BUY, bad_qty)

    @pytest.mark.parametrize("bad_price", [0, -1.0, float("nan"), float("inf")])
    def test_invalid_price(self, broker, bad_price):
        with pytest.raises(OrderError):
            broker.place_order(BTC, OrderSide.BUY, 0.001, OrderType.LIMIT, price=bad_price)

    def test_limit_requires_price(self, broker):
        with pytest.raises(OrderError):
            broker.place_order(BTC, OrderSide.BUY, 0.001, OrderType.LIMIT)

    def test_stop_requires_price_or_offset_but_not_both(self, broker, last):
        with pytest.raises(OrderError):
            broker.place_order(BTC, OrderSide.BUY, 0.001, OrderType.STOP)
        with pytest.raises(OrderError):
            broker.place_order(BTC, OrderSide.BUY, 0.001, OrderType.STOP, price=last.close, stop_offset=1.0)

    @pytest.mark.parametrize("order_type", [OrderType.MARKET, OrderType.LIMIT])
    def test_stop_offset_only_for_stop_orders(self, broker, last, order_type):
        with pytest.raises(OrderError):
            broker.place_order(BTC, OrderSide.BUY, 0.001, order_type, price=last.close, stop_offset=1.0)

    @pytest.mark.parametrize("bad_offset", [-1.0, float("nan"), float("inf"), "10"])
    def test_invalid_stop_offset(self, broker, bad_offset):
        with pytest.raises(OrderError):
            broker.place_order(BTC, OrderSide.BUY, 0.001, OrderType.STOP, stop_offset=bad_offset)

    def test_zero_stop_offset_is_allowed(self, broker):
        order = broker.place_order(BTC, OrderSide.BUY, 0.001, OrderType.STOP, stop_offset=0.0)
        assert order.status == OrderStatus.OPEN
        assert order.raw["stop_offset"] == 0.0

    def test_unknown_symbol_price_raises_broker_error(self, broker):
        with pytest.raises(BrokerError):
            broker.place_order(ETH, OrderSide.BUY, 0.01)
        assert broker.orders == []

    def test_empty_symbol(self, broker):
        with pytest.raises(OrderError):
            broker.place_order("", OrderSide.BUY, 0.01)

    def test_quote_currency_mismatch(self, last):
        b = PaperBroker(initial_cash=INITIAL_CASH, quote_currency="USDT")
        b.mark_price(BTC, last.close)
        with pytest.raises(OrderError):
            b.place_order(BTC, OrderSide.BUY, 0.001)

    def test_min_order_value_enforced(self, daily_candles, last):
        b = PaperBroker(initial_cash=INITIAL_CASH, min_order_value=5000)
        b.mark_price(BTC, last.close)
        tiny = 4999 / last.close
        with pytest.raises(OrderError):
            b.place_order(BTC, OrderSide.BUY, tiny)
        with pytest.raises(OrderError):
            b.place_order(BTC, OrderSide.BUY, tiny, OrderType.LIMIT, price=last.close)
        ok = b.place_order(BTC, OrderSide.BUY, 5001 / last.close)
        assert ok.status == OrderStatus.FILLED
        # data_source 의 최소 주문 금액과 설정값 중 큰 값
        b2 = PaperBroker(initial_cash=INITIAL_CASH, data_source=RealCandleFeed({BTC: daily_candles}))
        with pytest.raises(OrderError):
            b2.place_order(BTC, OrderSide.BUY, tiny)

    def test_min_order_value_skipped_when_no_reference_price(self):
        # stop_offset 주문은 기준가가 없으면 접수 시점 최소금액 검사를 할 수 없다 → 접수된다
        b = PaperBroker(initial_cash=INITIAL_CASH, min_order_value=5000)
        order = b.place_order(BTC, OrderSide.BUY, 1e-8, OrderType.STOP, stop_offset=1.0)
        assert order.status == OrderStatus.OPEN


# ============================================================================ 미체결 주문 관리
class TestOpenOrders:
    def test_limit_buy_is_open_and_locks_cash(self, broker, last):
        price = last.low * 0.9
        qty = qty_for(broker, BTC, 0.5, price)
        order = broker.place_order(BTC, OrderSide.BUY, qty, OrderType.LIMIT, price=price)
        assert order.status == OrderStatus.OPEN
        assert order.price == price
        assert order.filled_quantity == 0.0
        assert order.average_price is None
        assert order.updated_at is None
        assert broker.cash == INITIAL_CASH
        locked = price * qty * (1 + FEE)
        bal = broker.get_balances()["KRW"]
        assert bal.total == INITIAL_CASH
        assert bal.locked == approx(locked)
        assert bal.available == approx(INITIAL_CASH - locked)
        assert [o.id for o in broker.get_open_orders()] == [order.id]
        assert [o.id for o in broker.get_open_orders(BTC)] == [order.id]
        assert broker.get_open_orders(ETH) == []

        # 잠긴 현금은 시장가 매수에 쓸 수 없다
        with pytest.raises(InsufficientFunds):
            broker.place_order(BTC, OrderSide.BUY, qty_for(broker, BTC, 0.6, last.close))
        # 두 번째 지정가 매수도 가용 현금 기준
        with pytest.raises(InsufficientFunds):
            broker.place_order(BTC, OrderSide.BUY, qty * 1.1, OrderType.LIMIT, price=price)

        assert broker.cancel_order(order.id) is True
        assert broker.get_order(order.id).status == OrderStatus.CANCELED
        assert broker.get_order(order.id).updated_at == last.timestamp
        assert broker.cancel_order(order.id) is False
        assert broker.get_open_orders() == []
        assert broker.get_balances()["KRW"].locked == 0.0
        ok = broker.place_order(BTC, OrderSide.BUY, qty_for(broker, BTC, 0.6, last.close))
        assert ok.status == OrderStatus.FILLED

    def test_open_sell_locks_base(self, broker, last):
        qty = qty_for(broker, BTC, 0.2, last.close)
        broker.place_order(BTC, OrderSide.BUY, qty)
        half = broker.round_quantity(BTC, qty / 2)
        limit_sell = broker.place_order(BTC, OrderSide.SELL, half, OrderType.LIMIT, price=last.high * 2)
        stop_sell = broker.place_order(BTC, OrderSide.SELL, qty - half, OrderType.STOP, price=last.low * 0.5)
        bal = broker.get_balances()["BTC"]
        assert bal.total == approx(qty)
        assert bal.locked == approx(qty)
        assert bal.available == approx(0.0)
        with pytest.raises(InsufficientFunds):
            broker.place_order(BTC, OrderSide.SELL, half)
        assert broker.cancel_order(limit_sell.id, BTC) is True
        assert broker.get_balances()["BTC"].available == approx(half)
        sold = broker.place_order(BTC, OrderSide.SELL, half)
        assert sold.status == OrderStatus.FILLED
        assert broker.get_balances()["BTC"].available == approx(0.0)
        assert broker.get_balances()["BTC"].locked == approx(qty - half)
        assert broker.cancel_order(stop_sell.id) is True
        assert broker.get_balances()["BTC"].available == approx(qty - half)

    def test_get_order_and_cancel_errors(self, broker, last):
        with pytest.raises(OrderError):
            broker.get_order("paper-999")
        with pytest.raises(OrderError):
            broker.cancel_order("nope")
        order = broker.place_order(BTC, OrderSide.BUY, 0.001, OrderType.LIMIT, price=last.low * 0.9)
        with pytest.raises(OrderError):
            broker.get_order(order.id, symbol=ETH)
        with pytest.raises(OrderError):
            broker.cancel_order(order.id, symbol=ETH)
        assert broker.get_order(order.id, symbol=BTC).status == OrderStatus.OPEN

    def test_cancel_filled_order_returns_false(self, broker, last):
        order = broker.place_order(BTC, OrderSide.BUY, qty_for(broker, BTC, 0.1, last.close))
        assert broker.cancel_order(order.id) is False
        assert broker.get_order(order.id).status == OrderStatus.FILLED

    def test_snapshots_are_isolated(self, broker, last):
        order = broker.place_order(BTC, OrderSide.BUY, 0.001, OrderType.LIMIT, price=last.low * 0.9)
        order.status = OrderStatus.FILLED
        order.raw["hack"] = True
        again = broker.get_order(order.id)
        assert again.status == OrderStatus.OPEN
        assert "hack" not in again.raw


# ============================================================================ process_candle (실제 캔들)
class TestProcessCandle:
    def test_limit_buy_fills_at_limit_price(self, daily_candles):
        """지정가가 시가 아래·저가 이상이면 지정가에 체결된다 (슬리피지 없음, bar 중간 체결 = fill_basis 'limit')."""
        c = next(c for c in reversed(daily_candles) if c.low < c.open)  # 실제 일봉 중 시가 아래로 내려간 bar
        b = PaperBroker(initial_cash=INITIAL_CASH, fee_pct=FEE, slippage_pct=SLIP)
        price = (c.low + c.open) / 2  # low <= price < open → 가격이 내려와 닿았을 때 지정가 체결
        qty = qty_for(b, BTC, 0.3, price)
        order = b.place_order(BTC, OrderSide.BUY, qty, OrderType.LIMIT, price=price)
        miss = b.place_order(BTC, OrderSide.BUY, qty, OrderType.LIMIT, price=c.low * 0.5)  # 미체결

        filled = b.process_candle(BTC, c)
        assert [o.id for o in filled] == [order.id]
        f = filled[0]
        assert f.status == OrderStatus.FILLED
        assert f.average_price == price  # 지정가는 슬리피지 없음
        assert f.raw["fill_basis"] == "limit"
        assert f.filled_quantity == qty
        assert f.fee == approx(price * qty * FEE)
        assert f.updated_at == c.timestamp
        assert b.get_order(miss.id).status == OrderStatus.OPEN
        assert "fill_basis" not in b.get_order(miss.id).raw
        pos = b.get_positions()[BTC]
        assert pos.average_price == price
        assert pos.opened_at == c.timestamp
        assert b.cash == approx(INITIAL_CASH - price * qty * (1 + FEE))
        # 같은 캔들을 다시 넣어도 이미 체결된 주문은 다시 체결되지 않는다
        assert b.process_candle(BTC, c) == []

    def test_limit_sell_fills_when_high_reaches_price(self, daily_candles):
        """지정가가 시가 위·고가 이하면 지정가에 체결된다."""
        c = next(c for c in reversed(daily_candles) if c.high > c.open)
        b = PaperBroker(initial_cash=INITIAL_CASH, fee_pct=FEE, slippage_pct=SLIP)
        b.mark_price(BTC, c.open, timestamp=c.timestamp)
        qty = qty_for(b, BTC, 0.3, c.open)
        buy = b.place_order(BTC, OrderSide.BUY, qty)
        price = (c.open + c.high) / 2  # open < price <= high → 지정가 체결
        broker_half = b.round_quantity(BTC, qty / 2)
        hit = b.place_order(BTC, OrderSide.SELL, broker_half, OrderType.LIMIT, price=price)
        miss = b.place_order(BTC, OrderSide.SELL, qty - broker_half, OrderType.LIMIT, price=c.high * 2)
        filled = b.process_candle(BTC, c)
        assert [o.id for o in filled] == [hit.id]
        assert filled[0].average_price == price
        assert filled[0].raw["fill_basis"] == "limit"
        assert b.get_order(miss.id).status == OrderStatus.OPEN
        t = b.trades[0]
        assert t.exit_price == price
        assert t.quantity == broker_half
        assert t.entry_price == approx(buy.average_price)
        assert t.fee == approx(buy.fee * (broker_half / qty) + price * broker_half * FEE)

    def test_limit_buy_gap_through_fills_at_open_inside_bar_range(self, daily_candles):
        """시가가 이미 지정가 아래면(갭 통과) 지정가가 아니라 시가에 체결된다 — 고가보다 높은 '체결가' 는 없다."""
        prev_c, c = next(
            (p, c) for p, c in zip(daily_candles, daily_candles[1:], strict=False) if c.open < p.close
        )
        b = PaperBroker(initial_cash=INITIAL_CASH, fee_pct=FEE, slippage_pct=SLIP)
        b.mark_price(BTC, c.open, timestamp=c.timestamp)
        limit = prev_c.close  # 시가보다 높은 지정가 (전봉 종가)
        qty = qty_for(b, BTC, 0.3, limit)
        order = b.place_order(BTC, OrderSide.BUY, qty, OrderType.LIMIT, price=limit)
        filled = b.process_candle(BTC, c)
        assert [o.id for o in filled] == [order.id]
        f = filled[0]
        assert f.price == limit  # 주문의 지정가는 그대로
        assert f.average_price == c.open  # 체결은 시가
        assert f.average_price < f.price
        assert c.low <= f.average_price <= c.high
        assert f.raw["fill_basis"] == "open"
        assert f.fee == approx(c.open * qty * FEE)
        assert b.get_positions()[BTC].average_price == c.open
        # 잠근 금액(지정가 기준) 보다 적게 쓴다
        assert b.cash == approx(INITIAL_CASH - c.open * qty * (1 + FEE))

    def test_limit_sell_gap_through_fills_at_open_inside_bar_range(self, daily_candles):
        """시가가 이미 지정가 위면(갭 통과) 시가에 체결된다 — 저가보다 낮은 '체결가' 는 없다."""
        prev_c, c = next(
            (p, c) for p, c in zip(daily_candles, daily_candles[1:], strict=False) if c.open > p.close
        )
        b = PaperBroker(initial_cash=INITIAL_CASH, fee_pct=FEE, slippage_pct=0.0)
        b.mark_price(BTC, c.open, timestamp=c.timestamp)
        qty = qty_for(b, BTC, 0.3, c.open)
        b.place_order(BTC, OrderSide.BUY, qty)  # 시가 시장가 매수 (슬리피지 0)
        limit = prev_c.close  # 시가보다 낮은 지정가
        order = b.place_order(BTC, OrderSide.SELL, qty, OrderType.LIMIT, price=limit)
        filled = b.process_candle(BTC, c)
        assert [o.id for o in filled] == [order.id]
        f = filled[0]
        assert f.price == limit and f.average_price == c.open
        assert c.low <= f.average_price <= c.high
        assert f.raw["fill_basis"] == "open"
        t = b.trades[0]
        assert t.exit_price == c.open and t.entry_price == c.open
        assert t.pnl == approx(-2 * c.open * qty * FEE)  # 가격 차 0, 수수료만

    def test_limit_fills_never_leave_the_candle_range(self, candles):
        """실제 1h 캔들 200개: 전봉 종가에 건 지정가의 체결가는 항상 [low, high] 안이고 min/max(지정가, 시가) 다."""
        basis_buy: set[str] = set()
        basis_sell: set[str] = set()
        for prev_c, c in zip(candles, candles[1:], strict=False):
            limit = prev_c.close
            # 매수
            b = PaperBroker(initial_cash=INITIAL_CASH, fee_pct=FEE, slippage_pct=SLIP)
            b.mark_price(BTC, c.open, timestamp=c.timestamp)
            qty = qty_for(b, BTC, 0.3, limit)
            buy = b.place_order(BTC, OrderSide.BUY, qty, OrderType.LIMIT, price=limit)
            filled = b.process_candle(BTC, c)
            if c.low <= limit:
                assert [o.id for o in filled] == [buy.id]
                f = filled[0]
                assert f.average_price == min(limit, c.open)
                assert c.low <= f.average_price <= c.high
                assert f.raw["fill_basis"] == ("open" if c.open <= limit else "limit")
                basis_buy.add(f.raw["fill_basis"])
            else:
                assert filled == [] and b.get_order(buy.id).status == OrderStatus.OPEN
            # 매도: 시가에 산 포지션을 전봉 종가 지정가로 판다
            b2 = PaperBroker(initial_cash=INITIAL_CASH, fee_pct=FEE, slippage_pct=0.0)
            b2.mark_price(BTC, c.open, timestamp=c.timestamp)
            qty2 = qty_for(b2, BTC, 0.3, c.open)
            b2.place_order(BTC, OrderSide.BUY, qty2)
            sell = b2.place_order(BTC, OrderSide.SELL, qty2, OrderType.LIMIT, price=limit)
            filled2 = b2.process_candle(BTC, c)
            if c.high >= limit:
                assert [o.id for o in filled2] == [sell.id]
                f2 = filled2[0]
                assert f2.average_price == max(limit, c.open)
                assert c.low <= f2.average_price <= c.high
                assert f2.raw["fill_basis"] == ("open" if c.open >= limit else "limit")
                assert b2.trades[0].exit_price == f2.average_price
                basis_sell.add(f2.raw["fill_basis"])
            else:
                assert filled2 == [] and b2.get_order(sell.id).status == OrderStatus.OPEN
        # 실제 데이터에서 갭 통과(시가 체결) 와 지정가 체결이 모두 나와야 두 경로가 다 검증된 것이다
        assert basis_buy == {"open", "limit"}
        assert basis_sell == {"open", "limit"}

    def test_stop_buy_with_offset_fills_on_first_crossing_candle(self, daily_candles):
        b = PaperBroker(initial_cash=INITIAL_CASH, fee_pct=FEE, slippage_pct=SLIP)
        k = 0.5
        ref = daily_candles[0]
        offset = k * (ref.high - ref.low)
        assert offset > 0
        max_high = max(c.high for c in daily_candles)
        qty = b.round_quantity(BTC, INITIAL_CASH * 0.3 / max_high)  # 어떤 체결가에도 자금이 충분하도록
        b.mark_price(BTC, ref.close, timestamp=ref.timestamp)
        order = b.place_order(
            BTC, OrderSide.BUY, qty, OrderType.STOP, stop_offset=offset, reason="변동성 돌파"
        )
        assert order.status == OrderStatus.OPEN
        assert order.price is None
        assert order.raw["stop_offset"] == offset
        assert order.created_at == ref.timestamp

        crossing = [
            i for i in range(1, len(daily_candles)) if daily_candles[i].high >= daily_candles[i].open + offset
        ]
        if not crossing:
            pytest.skip("실데이터 구간에 돌파 캔들이 없어 검증 불가")
        first = crossing[0]

        for i in range(1, first + 1):
            c = daily_candles[i]
            filled = b.process_candle(BTC, c)
            if i < first:
                assert filled == []
                assert b.get_order(order.id).status == OrderStatus.OPEN
                assert b.get_positions() == {}
                assert b.cash == INITIAL_CASH
                continue
            trigger = c.open + offset
            base = c.open if c.open >= trigger else trigger
            expected = base * (1 + SLIP)
            assert [o.id for o in filled] == [order.id]
            f = filled[0]
            assert f.status == OrderStatus.FILLED
            assert f.average_price == approx(expected)
            assert f.filled_quantity == qty
            assert f.fee == approx(expected * qty * FEE)
            assert f.updated_at == c.timestamp
            assert f.raw["trigger"] == approx(trigger)
            pos = b.get_positions()[BTC]
            assert pos.quantity == qty
            assert pos.average_price == approx(expected)
            assert pos.opened_at == c.timestamp
            assert b.cash == approx(INITIAL_CASH - expected * qty * (1 + FEE))
            assert b.get_open_orders() == []

        # 다음 캔들 시가에 청산 → Trade 시각이 캔들 시각과 일치
        nxt = daily_candles[first + 1]
        b.mark_price(BTC, nxt.open, timestamp=nxt.timestamp)
        b.place_order(BTC, OrderSide.SELL, qty, reason="다음날 시가")
        t = b.trades[0]
        assert t.entry_time == daily_candles[first].timestamp
        assert t.exit_time == nxt.timestamp
        assert t.reason == "다음날 시가"
        assert t.exit_price == approx(sell_fill(nxt.open))

    def test_stop_buy_explicit_price_gap_fills_at_open(self, daily_candles):
        b = PaperBroker(initial_cash=INITIAL_CASH, fee_pct=FEE, slippage_pct=SLIP)
        c = daily_candles[-1]
        qty = qty_for(b, BTC, 0.2, c.high)
        # 트리거가 시가 아래 → 갭 → 시가 체결
        gap = b.place_order(BTC, OrderSide.BUY, qty, OrderType.STOP, price=c.open * 0.99)
        filled = b.process_candle(BTC, c)
        assert [o.id for o in filled] == [gap.id]
        assert filled[0].average_price == approx(c.open * (1 + SLIP))
        assert filled[0].raw["trigger"] == approx(c.open * 0.99)

    def test_stop_buy_explicit_price_inside_range_fills_at_trigger(self, daily_candles):
        b = PaperBroker(initial_cash=INITIAL_CASH, fee_pct=FEE, slippage_pct=SLIP)
        c = next((x for x in reversed(daily_candles) if x.high > x.open), None)
        if c is None:
            pytest.skip("고가 > 시가 인 캔들이 없음")
        qty = qty_for(b, BTC, 0.2, c.high)
        trigger = (c.open + c.high) / 2
        inside = b.place_order(BTC, OrderSide.BUY, qty, OrderType.STOP, price=trigger)
        above = b.place_order(BTC, OrderSide.BUY, qty, OrderType.STOP, price=c.high * 1.01)
        filled = b.process_candle(BTC, c)
        assert [o.id for o in filled] == [inside.id]
        assert filled[0].average_price == approx(trigger * (1 + SLIP))
        assert b.get_order(above.id).status == OrderStatus.OPEN

    def test_stop_sell_rules(self, daily_candles):
        c = next((x for x in reversed(daily_candles) if x.low < x.open), None)
        if c is None:
            pytest.skip("저가 < 시가 인 캔들이 없음")
        b = PaperBroker(initial_cash=INITIAL_CASH, fee_pct=FEE, slippage_pct=SLIP)
        b.mark_price(BTC, c.open, timestamp=c.timestamp - timedelta(days=1))
        qty = qty_for(b, BTC, 0.6, c.open)
        b.place_order(BTC, OrderSide.BUY, qty)
        third = b.round_quantity(BTC, qty / 3)
        gap = b.place_order(
            BTC, OrderSide.SELL, third, OrderType.STOP, price=c.open * 1.01
        )  # open <= trigger → open
        inside_trigger = (c.low + c.open) / 2
        inside = b.place_order(BTC, OrderSide.SELL, third, OrderType.STOP, price=inside_trigger)
        below = b.place_order(BTC, OrderSide.SELL, third, OrderType.STOP, price=c.low * 0.5)
        filled = b.process_candle(BTC, c)
        assert [o.id for o in filled] == [gap.id, inside.id]
        assert filled[0].average_price == approx(c.open * (1 - SLIP))
        assert filled[1].average_price == approx(inside_trigger * (1 - SLIP))
        assert b.get_order(below.id).status == OrderStatus.OPEN
        assert len(b.trades) == 2
        assert all(t.exit_time == c.timestamp for t in b.trades)
        assert b.get_positions()[BTC].quantity == approx(qty - 2 * third)

    def test_stop_sell_with_offset_uses_open_minus_offset(self, daily_candles):
        c = next((x for x in reversed(daily_candles) if x.low < x.open), None)
        if c is None:
            pytest.skip("저가 < 시가 인 캔들이 없음")
        b = PaperBroker(initial_cash=INITIAL_CASH, fee_pct=FEE, slippage_pct=SLIP)
        b.mark_price(BTC, c.open)
        qty = qty_for(b, BTC, 0.3, c.open)
        b.place_order(BTC, OrderSide.BUY, qty)
        offset = (c.open - c.low) / 2
        order = b.place_order(BTC, OrderSide.SELL, qty, OrderType.STOP, stop_offset=offset)
        filled = b.process_candle(BTC, c)
        assert [o.id for o in filled] == [order.id]
        assert filled[0].raw["trigger"] == approx(c.open - offset)
        assert filled[0].average_price == approx((c.open - offset) * (1 - SLIP))

    def test_stop_buy_rejected_when_cash_insufficient_at_trigger(self, daily_candles, caplog):
        b = PaperBroker(initial_cash=INITIAL_CASH, fee_pct=FEE, slippage_pct=SLIP)
        c = daily_candles[-1]
        qty = b.round_quantity(BTC, INITIAL_CASH * 2 / c.open)  # 체결가 기준 현금의 2배
        order = b.place_order(BTC, OrderSide.BUY, qty, OrderType.STOP, price=c.open * 0.99)
        with caplog.at_level(logging.WARNING, logger="tradingbot.brokers.paper"):
            filled = b.process_candle(BTC, c)
        assert filled == []
        rejected = b.get_order(order.id)
        assert rejected.status == OrderStatus.REJECTED
        assert "자금 부족" in rejected.raw["reject_reason"]
        assert rejected.updated_at == c.timestamp
        assert b.cash == INITIAL_CASH
        assert b.get_positions() == {}
        assert b.get_open_orders() == []
        assert any("주문 거부" in r.message for r in caplog.records)

    def test_process_candle_only_affects_that_symbol(self, daily_candles, eth_daily_df):
        eth = df_to_candles(eth_daily_df)
        b = PaperBroker(initial_cash=INITIAL_CASH, fee_pct=FEE, slippage_pct=SLIP)
        btc_c = daily_candles[-1]
        btc_order = b.place_order(
            BTC, OrderSide.BUY, 0.001, OrderType.LIMIT, price=(btc_c.low + btc_c.high) / 2
        )
        assert b.process_candle(ETH, eth[-1]) == []
        assert b.get_order(btc_order.id).status == OrderStatus.OPEN
        assert [o.id for o in b.process_candle(BTC, btc_c)] == [btc_order.id]

    def test_process_candle_advances_simulation_time(self, daily_candles):
        b = PaperBroker(initial_cash=INITIAL_CASH)
        c = daily_candles[-3]
        b.process_candle(BTC, c)  # 미체결 주문이 없어도 시각은 전진
        b.mark_price(BTC, c.close)
        o = b.place_order(BTC, OrderSide.BUY, qty_for(b, BTC, 0.1, c.close))
        assert o.created_at == c.timestamp
        assert o.updated_at == c.timestamp


# ============================================================================ check_pending (현재가 기준)
class TestCheckPending:
    def test_limit_orders(self, broker, last):
        limit_price = last.close * 0.98
        qty = qty_for(broker, BTC, 0.3, limit_price)
        buy = broker.place_order(BTC, OrderSide.BUY, qty, OrderType.LIMIT, price=limit_price)
        assert broker.check_pending(BTC, limit_price * 1.001) == []
        assert broker.get_ticker(BTC) == last.close  # check_pending 은 mark 하지 않는다
        through = limit_price * 0.999
        filled = broker.check_pending(BTC, through)
        assert [o.id for o in filled] == [buy.id]
        # 현재가가 지정가를 지나쳤으면 현재가에 체결 (STOP 폴링과 같은 규칙; 지정가보다 유리한 쪽)
        assert filled[0].average_price == through
        assert filled[0].price == limit_price and filled[0].raw["fill_basis"] == "market"
        assert filled[0].updated_at == last.timestamp
        assert broker.cash == approx(INITIAL_CASH - through * qty * (1 + FEE))
        sell_price = last.close * 1.02
        sell = broker.place_order(BTC, OrderSide.SELL, qty, OrderType.LIMIT, price=sell_price)
        assert broker.check_pending(BTC, sell_price * 0.999) == []
        filled = broker.check_pending(BTC, sell_price)  # 정확히 지정가 → 지정가
        assert [o.id for o in filled] == [sell.id]
        assert filled[0].average_price == sell_price
        assert filled[0].raw["fill_basis"] == "limit"
        assert broker.trades[0].exit_price == sell_price
        assert broker.get_positions() == {}

    def test_limit_sell_through_fills_at_current_price(self, broker, last):
        qty = qty_for(broker, BTC, 0.3, last.close)
        broker.place_order(BTC, OrderSide.BUY, qty)
        sell_price = last.close * 1.01
        sell = broker.place_order(BTC, OrderSide.SELL, qty, OrderType.LIMIT, price=sell_price)
        through = sell_price * 1.004
        filled = broker.check_pending(BTC, through)
        assert [o.id for o in filled] == [sell.id]
        assert filled[0].average_price == through
        assert filled[0].raw["fill_basis"] == "market"
        assert broker.trades[0].exit_price == through

    def test_stop_orders_fill_at_current_price_with_slippage(self, broker, last):
        trigger = last.close * 1.01
        qty = qty_for(broker, BTC, 0.3, trigger)
        buy = broker.place_order(BTC, OrderSide.BUY, qty, OrderType.STOP, price=trigger)
        assert broker.check_pending(BTC, trigger * 0.999) == []
        hit = trigger * 1.002
        filled = broker.check_pending(BTC, hit)
        assert [o.id for o in filled] == [buy.id]
        assert filled[0].average_price == approx(hit * (1 + SLIP))
        assert filled[0].raw["trigger"] == trigger
        stop_price = last.close * 0.97
        sell = broker.place_order(BTC, OrderSide.SELL, qty, OrderType.STOP, price=stop_price)
        assert broker.check_pending(BTC, stop_price * 1.001) == []
        low = stop_price * 0.995
        filled = broker.check_pending(BTC, low)
        assert [o.id for o in filled] == [sell.id]
        assert filled[0].average_price == approx(low * (1 - SLIP))
        assert broker.trades[0].exit_price == approx(low * (1 - SLIP))

    def test_offset_stop_is_skipped(self, broker, last):
        order = broker.place_order(BTC, OrderSide.BUY, 0.001, OrderType.STOP, stop_offset=1.0)
        assert broker.check_pending(BTC, last.close * 10) == []
        assert broker.get_order(order.id).status == OrderStatus.OPEN

    @pytest.mark.parametrize("bad", [0, -1.0, float("nan"), float("inf")])
    def test_invalid_price(self, broker, bad):
        with pytest.raises(DataError):
            broker.check_pending(BTC, bad)

    def test_stop_buy_rejected_when_funds_locked_elsewhere(self, broker, last):
        # 지정가 매수가 현금을 거의 다 잠근 상태에서 STOP 매수가 트리거되면 REJECTED
        limit_price = last.close * 0.9
        broker.place_order(
            BTC, OrderSide.BUY, qty_for(broker, BTC, 0.9, limit_price), OrderType.LIMIT, price=limit_price
        )
        stop = broker.place_order(
            BTC, OrderSide.BUY, qty_for(broker, BTC, 0.5, last.close), OrderType.STOP, price=last.close
        )
        assert broker.check_pending(BTC, last.close) == []
        assert broker.get_order(stop.id).status == OrderStatus.REJECTED
        assert broker.cash == INITIAL_CASH


# ============================================================================ 잔고 / 포지션 / 자산
class TestAccount:
    def test_balances_multi_symbol(self, broker, last, eth_daily_df):
        eth_last = df_to_candles(eth_daily_df)[-1]
        broker.mark_price(ETH, eth_last.close)
        btc_qty = qty_for(broker, BTC, 0.2, last.close)
        broker.place_order(BTC, OrderSide.BUY, btc_qty)
        eth_qty = qty_for(broker, ETH, 0.2, eth_last.close)
        broker.place_order(ETH, OrderSide.BUY, eth_qty)
        bal = broker.get_balances()
        assert set(bal) == {"KRW", "BTC", "ETH"}
        assert bal["BTC"].total == btc_qty
        assert bal["ETH"].total == eth_qty
        assert bal["KRW"].total == approx(broker.cash)
        positions = broker.get_positions()
        assert set(positions) == {BTC, ETH}
        assert broker.get_equity() == approx(broker.cash + btc_qty * last.close + eth_qty * eth_last.close)
        assert broker.get_equity([BTC]) == broker.get_equity()

    def test_positions_are_live_objects_for_risk_manager(self, broker, last):
        qty = qty_for(broker, BTC, 0.1, last.close)
        broker.place_order(BTC, OrderSide.BUY, qty)
        pos = broker.get_positions()[BTC]
        assert isinstance(pos, Position)
        pos.stop_loss = pos.average_price * 0.97
        pos.highest_price = pos.average_price
        pos.meta["entry_reason"] = "테스트"
        again = broker.get_positions()[BTC]
        assert again.stop_loss == pos.stop_loss
        assert again.meta == {"entry_reason": "테스트"}
        assert broker.to_dict()["positions"][BTC]["stop_loss"] == pos.stop_loss

    def test_equity_fallbacks(self, daily_candles, last, prev, caplog):
        feed = RealCandleFeed({BTC: daily_candles})
        b = PaperBroker(initial_cash=INITIAL_CASH, fee_pct=FEE, slippage_pct=SLIP, data_source=feed)
        qty = qty_for(b, BTC, 0.2, last.close)
        o = b.place_order(BTC, OrderSide.BUY, qty)  # mark 없음 → feed 현재가로 체결
        assert o.average_price == approx(buy_fill(last.close))
        assert b.get_equity() == approx(b.cash + qty * last.close)  # feed 현재가 평가
        b.mark_price(BTC, prev.close)
        assert b.get_equity() == approx(b.cash + qty * prev.close)  # mark 우선

        state = b.to_dict()
        state["prices"] = {}
        restored = PaperBroker.from_dict(state)  # 시세 없음 → 원가 평가
        assert restored.get_equity() == approx(restored.cash + qty * o.average_price)

        broken = PaperBroker.from_dict(state, data_source=RealCandleFeed({}))
        with caplog.at_level(logging.WARNING, logger="tradingbot.brokers.paper"):
            assert broken.get_equity() == approx(broken.cash + qty * o.average_price)
        assert any("원가로 평가" in r.message for r in caplog.records)


# ============================================================================ 상태 저장 / 복원
class TestSerialization:
    def _build(self, last: Candle, prev: Candle) -> PaperBroker:
        b = PaperBroker(initial_cash=INITIAL_CASH, fee_pct=FEE, slippage_pct=SLIP, min_order_value=100)
        b.mark_price(BTC, last.close, timestamp=last.timestamp)
        qty = qty_for(b, BTC, 0.3, last.close)
        b.place_order(BTC, OrderSide.BUY, qty, reason="진입")
        b.mark_price(BTC, prev.close, timestamp=last.timestamp + timedelta(hours=1))
        b.place_order(BTC, OrderSide.SELL, b.round_quantity(BTC, qty / 3), reason="부분 청산")
        pos = b.get_positions()[BTC]
        pos.stop_loss = pos.average_price * 0.97
        pos.meta["entry_bar_ts"] = last.timestamp  # RiskManager 가 datetime 을 넣는 경우
        pos.meta["max_holding_bars"] = 1
        canceled = b.place_order(BTC, OrderSide.BUY, 0.0001, OrderType.LIMIT, price=last.low * 0.8)
        b.cancel_order(canceled.id)
        b.place_order(BTC, OrderSide.BUY, 0.0002, OrderType.LIMIT, price=last.low * 0.9)
        b.place_order(BTC, OrderSide.BUY, 0.0003, OrderType.STOP, stop_offset=(last.high - last.low) / 2)
        b.place_order(
            BTC, OrderSide.SELL, b.round_quantity(BTC, pos.quantity / 2), OrderType.LIMIT, price=last.high * 2
        )
        return b

    def test_round_trip(self, last, prev):
        original = self._build(last, prev)
        d = original.to_dict()
        text = json.dumps(d, ensure_ascii=False)  # JSON 직렬화 가능해야 한다
        assert d["version"] == STATE_VERSION
        assert d["asset_class"] == "crypto"
        assert d["positions"][BTC]["meta"]["entry_bar_ts"] == last.timestamp.isoformat()
        assert all(isinstance(o["side"], str) and isinstance(o["created_at"], str) for o in d["orders"])
        assert d["trades"][0]["entry_time"] == last.timestamp.isoformat()

        restored = PaperBroker.from_dict(json.loads(text))
        assert restored.to_dict() == d
        assert restored.cash == original.cash
        assert restored.initial_cash == original.initial_cash
        assert restored.fee_pct == original.fee_pct
        assert restored.slippage_pct == original.slippage_pct
        assert restored.min_order_value(BTC) == 100
        assert restored.trades == original.trades
        assert restored.orders == original.orders
        assert restored.get_balances() == original.get_balances()
        assert restored.get_equity() == approx(original.get_equity())
        assert [o.id for o in restored.get_open_orders()] == [o.id for o in original.get_open_orders()]
        rp, op = restored.get_positions()[BTC], original.get_positions()[BTC]
        assert (rp.symbol, rp.quantity, rp.average_price, rp.opened_at, rp.stop_loss) == (
            op.symbol,
            op.quantity,
            op.average_price,
            op.opened_at,
            op.stop_loss,
        )
        assert rp.meta["max_holding_bars"] == 1
        assert rp.meta["entry_bar_ts"] == last.timestamp.isoformat()

        # 주문 id 시퀀스가 이어진다
        nxt_r = restored.place_order(BTC, OrderSide.BUY, 0.0001, OrderType.LIMIT, price=last.low * 0.9)
        nxt_o = original.place_order(BTC, OrderSide.BUY, 0.0001, OrderType.LIMIT, price=last.low * 0.9)
        assert nxt_r.id == nxt_o.id
        assert nxt_r.created_at == nxt_o.created_at  # sim_time 복원

        # 복원 후 매도 수수료 배분도 동일 (position_fees 복원)
        restored_pos = restored.get_positions()[BTC]
        restored.place_order(BTC, OrderSide.SELL, restored.round_quantity(BTC, restored_pos.quantity / 4))
        original.place_order(BTC, OrderSide.SELL, original.round_quantity(BTC, op.quantity / 4))
        assert restored.trades[-1] == original.trades[-1]

    def test_round_trip_with_data_source(self, daily_candles, last, prev):
        original = self._build(last, prev)
        feed = RealCandleFeed({BTC: daily_candles})
        restored = PaperBroker.from_dict(original.to_dict(), data_source=feed)
        assert restored.data_source is feed
        assert restored.min_order_value(BTC) == 5000.0  # max(설정 100, feed 5000)
        assert restored.round_quantity(BTC, 0.123456) == 0.1234

    def test_from_dict_recovers_order_sequence(self, last, prev):
        d = self._build(last, prev).to_dict()
        d["order_seq"] = 0
        restored = PaperBroker.from_dict(d)
        o = restored.place_order(BTC, OrderSide.BUY, 0.0001, OrderType.LIMIT, price=last.low * 0.9)
        assert o.id == f"paper-{len(d['orders']) + 1}"

    def test_from_dict_with_mismatched_asset_class_warns(self, last, prev, caplog):
        d = self._build(last, prev).to_dict()
        with caplog.at_level(logging.WARNING, logger="tradingbot.brokers.paper"):
            restored = PaperBroker.from_dict(d, data_source=StockFeed())
        assert restored.asset_class == AssetClass.CRYPTO  # 저장된 값 유지
        assert any("asset_class" in r.message for r in caplog.records)

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda d: d.__setitem__("version", 99),
            lambda d: d.pop("cash"),
            lambda d: d.pop("initial_cash"),
            lambda d: d.__setitem__("cash", -1.0),
            lambda d: d.__setitem__("cash", "많이"),
            lambda d: d.__setitem__("asset_class", "futures"),
            lambda d: d.__setitem__("initial_cash", 0),
            lambda d: d["orders"][0].__setitem__("side", "short"),
            lambda d: d["orders"][0].pop("id"),
            lambda d: d["positions"][BTC].__setitem__("quantity", 0),
            lambda d: d["positions"][BTC].__setitem__("average_price", "x"),
            lambda d: d["trades"][0].pop("entry_time"),
            lambda d: d["trades"][0].__setitem__("exit_time", "not-a-date"),
        ],
    )
    def test_from_dict_rejects_corrupt_state(self, last, prev, mutate):
        d = self._build(last, prev).to_dict()
        mutate(d)
        with pytest.raises(DataError):
            PaperBroker.from_dict(d)

    def test_from_dict_rejects_non_dict(self):
        with pytest.raises(DataError):
            PaperBroker.from_dict(["not", "a", "dict"])  # type: ignore[arg-type]

    def test_from_dict_minimal_state(self):
        b = PaperBroker.from_dict({"initial_cash": 1000.0, "cash": 1000.0, "quote_currency": "USDT"})
        assert b.cash == 1000.0
        assert b.quote_currency("XRP/USDT") == "USDT"
        assert b.orders == [] and b.trades == [] and b.get_positions() == {}
        assert b.fee_pct == 0.0


# ============================================================================ 회계 불변식 (실제 캔들 시뮬레이션)
class TestAccountingInvariants:
    @pytest.mark.parametrize("fixture_name", ["daily_candles", "candles"])
    def test_alternating_buy_sell_over_real_candles(self, request, fixture_name):
        series: list[Candle] = request.getfixturevalue(fixture_name)
        b = PaperBroker(initial_cash=INITIAL_CASH, fee_pct=FEE, slippage_pct=SLIP)
        holding = False
        open_buy_fee = 0.0
        expected_times: list[tuple[datetime, datetime]] = []
        entry_ts: datetime | None = None
        filled_orders: list[Order] = []

        for c in series:
            b.mark_price(BTC, c.open, timestamp=c.timestamp)
            if holding:
                pos = b.get_positions()[BTC]
                o = b.place_order(BTC, OrderSide.SELL, pos.quantity, reason="다음 캔들 시가 청산")
                assert o.average_price == approx(sell_fill(c.open))
                assert entry_ts is not None
                expected_times.append((entry_ts, c.timestamp))
                holding, open_buy_fee = False, 0.0
            else:
                qty = qty_for(b, BTC, 0.5, buy_fill(c.open))
                o = b.place_order(BTC, OrderSide.BUY, qty)
                assert o.average_price == approx(buy_fill(c.open))
                open_buy_fee, holding, entry_ts = o.fee, True, c.timestamp
            filled_orders.append(o)
            b.mark_price(BTC, c.close)

            realized = sum(t.pnl for t in b.trades)
            unrealized = sum(p.unrealized_pnl(c.close) for p in b.get_positions().values())
            # equity == 초기자금 + 실현손익(수수료 차감) + 미실현손익 - 아직 Trade 에 반영되지 않은 매수 수수료
            assert b.get_equity() == approx(INITIAL_CASH + realized + unrealized - open_buy_fee, rel=1e-9)
            assert b.cash >= 0

        trades = b.trades
        assert len(trades) == len(series) // 2
        assert [(t.entry_time, t.exit_time) for t in trades] == expected_times
        assert all(t.holding_seconds > 0 for t in trades)
        assert all(t.reason == "다음 캔들 시가 청산" for t in trades)
        if not holding:
            assert b.cash - INITIAL_CASH == approx(sum(t.pnl for t in trades), rel=1e-9)
            # 모든 주문 수수료가 Trade 수수료로 흘러들어간다
            assert sum(t.fee for t in trades) == approx(sum(o.fee for o in filled_orders), rel=1e-9)
        assert len(b.orders) == len(series)
        assert all(o.status == OrderStatus.FILLED for o in b.orders)

    def test_breakout_backtest_loop_invariants(self, daily_candles):
        """백테스터가 하는 것처럼: bar 시작에 시가 mark + process_candle, 종가에 STOP 매수 접수, 다음 bar 시가 청산."""
        b = PaperBroker(initial_cash=INITIAL_CASH, fee_pct=FEE, slippage_pct=SLIP)
        k = 0.5
        pending: Order | None = None
        for i, c in enumerate(daily_candles):
            b.mark_price(BTC, c.open, timestamp=c.timestamp)
            # 이전 bar 중에 진입한 포지션(max_holding_bars=1)을 이번 bar 시가에 청산
            if BTC in b.get_positions():
                b.place_order(BTC, OrderSide.SELL, b.get_positions()[BTC].quantity, reason="max_holding_bars")
            if pending is not None:
                filled = b.process_candle(BTC, c)
                if not filled:
                    assert b.cancel_order(pending.id) is True
                pending = None
            b.mark_price(BTC, c.close)
            if i < len(daily_candles) - 1:
                offset = k * (c.high - c.low)
                qty = qty_for(b, BTC, 0.3, c.close * 2)  # 갭 상승에도 자금이 충분하도록 보수적 수량
                pending = b.place_order(BTC, OrderSide.BUY, qty, OrderType.STOP, stop_offset=offset)
            realized = sum(t.pnl for t in b.trades)
            unrealized = sum(p.unrealized_pnl(c.close) for p in b.get_positions().values())
            open_fee = sum(o.fee for o in b.orders if o.is_filled and o.side == OrderSide.BUY) - sum(
                t.fee - (t.exit_price * t.quantity * FEE) for t in b.trades
            )
            assert b.get_equity() == approx(INITIAL_CASH + realized + unrealized - open_fee, rel=1e-9)

        statuses = {o.status for o in b.orders}
        assert OrderStatus.REJECTED not in statuses
        assert statuses <= {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.OPEN}
        # 돌파가 일어난 날 수 == Trade 수 (마지막 포지션은 당일 시가에 청산되므로 열린 포지션이 있어도 1개 이하)
        crossings = sum(
            1
            for i in range(1, len(daily_candles))
            if daily_candles[i].high
            >= daily_candles[i].open + k * (daily_candles[i - 1].high - daily_candles[i - 1].low)
        )
        assert len(b.trades) + len(b.get_positions()) == crossings
        for t in b.trades:
            assert t.holding_seconds == 86400
            assert t.reason == "max_holding_bars"
