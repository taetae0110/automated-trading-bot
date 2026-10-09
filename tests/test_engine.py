"""Trader(실시간 매매 엔진) 테스트.

시세는 전부 tests/conftest.py 가 Upbit 공개 API 에서 받아 캐시한 **실제** KRW-BTC(1h, 1d) / KRW-ETH(1d) 캔들이다
(네트워크가 없으면 skip). ``RealCandleFeed`` 는 주입된 가짜 시계 기준으로 "그 시각까지 완성된" 실제 캔들만
서빙하고, 현재가는 진행중 실제 캔들의 open/high/low/close 중 테스트가 고른 값을 돌려준다.
전략은 신호 시점만 지정하는 ``ScriptedStrategy`` (시세가 아니라 테스트 로직) 또는 실제 변동성 돌파 전략이다.
가짜/데모 가격은 없다 — 계좌 설정값(초기 현금, 수수료율)만 상수다.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import signal
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from tradingbot.brokers.base import BaseBroker
from tradingbot.brokers.paper import PaperBroker
from tradingbot.config import AppConfig
from tradingbot.engine.state import StateStore, serialize_position
from tradingbot.engine.trader import (
    ERROR_NOTIFY_INTERVAL_SEC,
    EXIT_MARKET_CLOSE,
    EXIT_MAX_HOLDING,
    EXIT_SIGNAL,
    MAX_BACKOFF_SEC,
    STATE_VERSION,
    PendingBreakout,
    Trader,
)
from tradingbot.exceptions import AuthenticationError, BrokerError, ConfigError, DataError, OrderError
from tradingbot.models import (
    AssetClass,
    Candle,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Signal,
    SignalAction,
    ensure_utc,
    interval_to_seconds,
)
from tradingbot.notify.base import Notifier
from tradingbot.risk import RiskManager
from tradingbot.strategies.base import BaseStrategy, df_to_candles
from tradingbot.strategies.volatility_breakout import VolatilityBreakoutStrategy
from tradingbot.utils.timeutil import floor_to_interval

BTC = "KRW-BTC"
ETH = "KRW-ETH"
# 계좌 설정 (시세가 아님)
INITIAL_CASH = 10_000_000.0
FEE = 0.0005
SLIP = 0.0005
MAX_POSITION_PCT = 0.2
STOP_LOSS_PCT = 0.03
H = 3600
D = 86400

approx = pytest.approx


# ============================================================================ 테스트 인프라
class FakeClock:
    """주입용 시계. ``advance`` / ``set`` 으로 시간을 옮긴다."""

    def __init__(self, now: datetime) -> None:
        self.now = ensure_utc(now)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)

    def set(self, when: datetime) -> None:
        self.now = ensure_utc(when)


class RealCandleFeed(BaseBroker):
    """conftest 의 실제 Upbit 캔들을 '가짜 시계 기준으로 완성된 것만' 서빙하는 시세 전용 브로커.

    - ``get_candles``: timestamp + interval (+ lag) <= now 인 캔들만 (include_partial 이면 진행중 캔들 포함)
    - ``get_ticker``: 진행중 실제 캔들의 ``ticker_field`` (open/high/low/close) 값
    - ``fail_symbols`` 에 든 심볼은 BrokerError (장애 시뮬레이션), ``market_open`` 으로 장 운영 여부 제어
    """

    name = "feed"
    asset_class = AssetClass.CRYPTO
    supported_intervals = ("1h", "1d")

    def __init__(self, candles_by_symbol: dict[str, list[Candle]], clock: FakeClock, interval: str) -> None:
        self._candles = {s: sorted(cs, key=lambda c: c.timestamp) for s, cs in candles_by_symbol.items()}
        self._clock = clock
        self._step = interval_to_seconds(interval)
        self.ticker_field = "close"
        self.fail_symbols: set[str] = set()
        self.market_open = True
        self.lag = 0.0  # 캔들 완성 후 서버에 반영되기까지의 지연(초)
        self.calls: Counter[str] = Counter()
        self.last_candles_args: tuple[Any, ...] | None = None

    def _check(self, symbol: str) -> None:
        if symbol in self.fail_symbols:
            raise BrokerError(f"{symbol} 시세 조회 실패 (테스트용 장애)")
        if symbol not in self._candles:
            raise BrokerError(f"feed 에 없는 심볼: {symbol}")

    def complete_candles(self, symbol: str) -> list[Candle]:
        now = self._clock()
        delta = timedelta(seconds=self._step + self.lag)
        return [c for c in self._candles[symbol] if c.timestamp + delta <= now]

    def forming_candle(self, symbol: str) -> Candle | None:
        now = self._clock()
        step = timedelta(seconds=self._step)
        for c in self._candles[symbol]:
            if c.timestamp <= now < c.timestamp + step:
                return c
        return None

    def get_candles(self, symbol, interval, limit=200, end=None, include_partial=False):
        self.calls["get_candles"] += 1
        self.last_candles_args = (symbol, interval, limit, end, include_partial)
        self._check(symbol)
        if include_partial:
            now = self._clock()
            out = [c for c in self._candles[symbol] if c.timestamp <= now]
        else:
            out = self.complete_candles(symbol)
        if end is not None:
            out = [c for c in out if c.timestamp < ensure_utc(end)]
        return out[-limit:]

    def get_ticker(self, symbol):
        self.calls["get_ticker"] += 1
        self._check(symbol)
        c = self.forming_candle(symbol)
        if c is None:
            done = self.complete_candles(symbol)
            if not done:
                raise BrokerError(f"{symbol} 현재 시각에 해당하는 캔들이 없습니다")
            c = done[-1]
        return float(getattr(c, self.ticker_field))

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

    def is_market_open(self):
        return self.market_open


class StockFeed(RealCandleFeed):
    """주식 시장 흉내 (장 운영 여부 / 다음 마감 시각 제어). 캔들은 동일한 실제 데이터."""

    name = "stockfeed"
    asset_class = AssetClass.STOCK

    def __init__(self, candles_by_symbol, clock, interval) -> None:
        super().__init__(candles_by_symbol, clock, interval)
        self.next_market_close: datetime | None = None

    def quote_currency(self, symbol):
        return "KRW"

    def base_currency(self, symbol):
        return symbol


class ScriptedStrategy(BaseStrategy):
    """지정한 (심볼, 완성 캔들 시각) 에서만 정해진 신호를 내고 나머지는 HOLD. 시세가 아닌 테스트 로직."""

    name = "scripted"
    description = "테스트용 스크립트 전략"
    default_params: dict[str, Any] = {}

    def __init__(self, warmup: int = 1) -> None:
        super().__init__()
        self._warmup = warmup
        self.script: dict[tuple[str, datetime], Signal] = {}
        self.calls: list[tuple[str, datetime]] = []

    def at(self, symbol: str, ts: datetime, action: SignalAction, **kw: Any) -> Signal:
        reason = kw.pop("reason", f"scripted {action.value}")
        sig = Signal(action=action, symbol=symbol, reason=reason, **kw)
        self.script[(symbol, ensure_utc(ts))] = sig
        return sig

    @property
    def warmup(self) -> int:
        return self._warmup

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        return df.copy()

    def signal_at(self, symbol: str, df: pd.DataFrame, i: int) -> Signal:
        ts = ensure_utc(df["timestamp"].iat[i].to_pydatetime())
        self.calls.append((symbol, ts))
        if i < self._warmup - 1:
            return Signal.hold(symbol, "워밍업")
        sig = self.script.get((symbol, ts))
        if sig is None:
            return Signal.hold(symbol, "scripted hold")
        return dataclasses.replace(sig, meta={**sig.meta, "timestamp": ts.isoformat()})


class RecordingNotifier(Notifier):
    name = "recording"

    def __init__(self) -> None:
        self.messages: list[str] = []

    def _send(self, text: str) -> None:
        self.messages.append(text)

    def find(self, needle: str) -> list[str]:
        return [m for m in self.messages if needle in m]


class StuckBroker(PaperBroker):
    """주문이 OPEN 상태로 머무는 브로커 (체결 대기/타임아웃 경로 테스트용)."""

    def __init__(self, *args: Any, on_cancel: str = "cancel", **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.stuck: dict[str, Order] = {}
        self.cancel_calls: list[str] = []
        self.get_order_calls = 0
        self.on_cancel = on_cancel  # cancel | filled | partial

    def place_order(self, symbol, side, quantity, order_type=OrderType.MARKET, price=None, **kw):
        order = Order(
            id=f"stuck-{len(self.stuck) + 1}",
            symbol=symbol,
            side=side,
            type=order_type,
            quantity=quantity,
            price=price,
            status=OrderStatus.OPEN,
            created_at=self.clock(),
        )
        self.stuck[order.id] = order
        return dataclasses.replace(order)

    def get_order(self, order_id, symbol=None):
        self.get_order_calls += 1
        return dataclasses.replace(self.stuck[order_id])

    def cancel_order(self, order_id, symbol=None):
        self.cancel_calls.append(order_id)
        o = self.stuck[order_id]
        o.updated_at = self.clock()
        if self.on_cancel == "filled":
            o.status = OrderStatus.FILLED
            o.filled_quantity = o.quantity
            o.average_price = self.get_ticker(o.symbol)
            return False
        o.status = OrderStatus.CANCELED
        if self.on_cancel == "partial":
            o.filled_quantity = o.quantity / 2
            o.average_price = self.get_ticker(o.symbol)
        return True


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def make_config(
    symbols: list[str], interval: str, state_file: Path, overrides: dict[str, Any] | None = None
) -> AppConfig:
    base: dict[str, Any] = {
        "mode": "paper",
        "broker": {"name": "upbit", "fill_timeout_sec": 5.0},
        "symbols": list(symbols),
        "interval": interval,
        "strategy": {"name": "scripted", "params": {}},
        "risk": {
            "max_position_pct": MAX_POSITION_PCT,
            "max_positions": 5,
            "stop_loss_pct": STOP_LOSS_PCT,
            "take_profit_pct": None,
            "max_daily_loss_pct": 0.05,
            "min_order_value": 0.0,
        },
        "paper": {
            "initial_cash": INITIAL_CASH,
            "quote_currency": "KRW",
            "fee_pct": FEE,
            "slippage_pct": SLIP,
        },
        "engine": {
            "poll_seconds": 1.0,
            "candle_limit": 60,
            "state_file": str(state_file),
            "sync_positions_on_start": True,
            "stale_data_minutes": 30,
        },
        "notify": {"notify_on_trade": True, "notify_on_error": True, "daily_summary": True},
        "logging": {"file": None},
    }
    return AppConfig.model_validate(_merge(base, overrides or {}))


@dataclasses.dataclass
class Harness:
    trader: Trader
    feed: RealCandleFeed
    clock: FakeClock
    notifier: RecordingNotifier
    strategy: BaseStrategy
    store: StateStore
    config: AppConfig
    sleeps: list[float]

    @property
    def broker(self) -> PaperBroker:
        assert isinstance(self.trader.broker, PaperBroker)
        return self.trader.broker


def build(
    tmp_path: Path,
    candles_by_symbol: dict[str, list[Candle]],
    clock: FakeClock,
    *,
    interval: str = "1h",
    strategy: BaseStrategy | None = None,
    overrides: dict[str, Any] | None = None,
    broker: PaperBroker | None = None,
    feed: RealCandleFeed | None = None,
    notifier: RecordingNotifier | None = None,
    risk: RiskManager | None = None,
    sleep: Any | None = None,
) -> Harness:
    state_file = tmp_path / "state.json"
    config = make_config(list(candles_by_symbol), interval, state_file, overrides)
    feed = feed or RealCandleFeed(candles_by_symbol, clock, interval)
    broker = broker or PaperBroker(
        initial_cash=config.paper.initial_cash,
        quote_currency=config.paper.quote_currency,
        fee_pct=config.paper.fee_pct,
        slippage_pct=config.paper.slippage_pct,
        data_source=feed,
        asset_class=feed.asset_class,
    )
    strategy = strategy or ScriptedStrategy()
    notifier = notifier or RecordingNotifier()
    risk = risk or RiskManager(config.risk)
    store = StateStore(state_file)
    sleeps: list[float] = []

    def advancing_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)

    trader = Trader(config, broker, strategy, risk, notifier, store, clock, sleep=sleep or advancing_sleep)
    return Harness(trader, feed, clock, notifier, strategy, store, config, sleeps)


def after_candle(candles: list[Candle], k: int, step: int = H, offset: float = 5.0) -> datetime:
    """캔들 k 가 막 완성된 시각 (k+1 이 진행중)."""
    return candles[k].timestamp + timedelta(seconds=step + offset)


def expected_qty(price: float, equity: float = INITIAL_CASH, cash: float = INITIAL_CASH) -> float:
    budget = min(equity * MAX_POSITION_PCT, cash * (1 - FEE) * 0.999)
    return math.floor(budget / price * 1e8) / 1e8


def find_index(candles: list[Candle], ts: datetime) -> int | None:
    for i, c in enumerate(candles):
        if c.timestamp == ts:
            return i
    return None


def breakout_days(daily: list[Candle], k: float = 0.5) -> tuple[list[int], list[int]]:
    """실제 일봉에서 (돌파한 날, 돌파하지 않은 날) 인덱스. i-1 범위 기준 트리거 = open_i + k*range_{i-1}."""
    hits, misses = [], []
    for i in range(2, len(daily) - 3):
        rng = daily[i - 1].high - daily[i - 1].low
        if rng <= 0:
            continue
        trigger = daily[i].open + k * rng
        (hits if daily[i].high >= trigger else misses).append(i)
    return hits, misses


@pytest.fixture
def eth_daily(eth_daily_df) -> list[Candle]:
    return df_to_candles(eth_daily_df)


# ============================================================================ 새 캔들 감지
class TestNewCandleDetection:
    K = 60

    def test_first_poll_evaluates_latest_complete_candle(self, tmp_path, candles):
        clock = FakeClock(after_candle(candles, self.K))
        h = build(tmp_path, {BTC: candles}, clock)
        assert not h.trader.started
        h.trader.run_once()
        assert h.trader.started
        assert h.strategy.calls == [(BTC, candles[self.K].timestamp)]
        assert h.trader.last_candle_ts == {BTC: candles[self.K].timestamp}
        sym, interval, limit, end, partial = h.feed.last_candles_args
        assert (sym, interval, limit, end, partial) == (BTC, "1h", h.config.engine.candle_limit, None, False)

    def test_same_candle_is_not_reevaluated(self, tmp_path, candles):
        clock = FakeClock(after_candle(candles, self.K))
        h = build(tmp_path, {BTC: candles}, clock)
        h.trader.run_once()
        clock.advance(600)
        h.trader.run_once()
        assert len(h.strategy.calls) == 1
        assert h.trader.cycles == 2
        assert h.feed.calls["get_ticker"] >= 2  # 매 폴링 현재가는 조회한다

    def test_next_candle_triggers_single_evaluation(self, tmp_path, candles):
        clock = FakeClock(after_candle(candles, self.K))
        h = build(tmp_path, {BTC: candles}, clock)
        h.trader.run_once()
        clock.advance(H)
        h.trader.run_once()
        assert h.strategy.calls[-1] == (BTC, candles[self.K + 1].timestamp)
        clock.advance(3 * H)  # 캔들 3개를 건너뛰어도 최신 캔들 1회만 평가
        h.trader.run_once()
        assert len(h.strategy.calls) == 3
        assert h.strategy.calls[-1] == (BTC, candles[self.K + 4].timestamp)
        assert h.trader.last_candle_ts[BTC] == candles[self.K + 4].timestamp

    def test_state_file_records_last_candle_ts(self, tmp_path, candles):
        clock = FakeClock(after_candle(candles, self.K))
        h = build(tmp_path, {BTC: candles}, clock)
        h.trader.run_once()
        data = json.loads(h.store.path.read_text())
        assert data["version"] == STATE_VERSION
        assert data["last_candle_ts"][BTC] == candles[self.K].timestamp.isoformat()
        assert data["interval"] == "1h" and data["strategy"] == "scripted" and data["broker"] == "paper"
        assert data["paper_broker"]["cash"] == INITIAL_CASH
        assert "updated_at" in data and data["day"] == clock.now.date().isoformat()

    def test_empty_candles_warns_once(self, tmp_path, candles, caplog):
        clock = FakeClock(candles[0].timestamp)  # 아직 완성된 캔들이 없다
        h = build(tmp_path, {BTC: candles}, clock)
        with caplog.at_level(logging.WARNING):
            h.trader.run_once()
            h.trader.run_once()
        assert sum("비어 있습니다" in r.message for r in caplog.records) == 1
        assert h.strategy.calls == []


# ============================================================================ 진입 (BUY)
class TestEntry:
    K = 40

    def test_buy_signal_sizes_fills_and_persists(self, tmp_path, candles):
        clock = FakeClock(after_candle(candles, self.K))
        strat = ScriptedStrategy()
        strat.at(BTC, candles[self.K].timestamp, SignalAction.BUY, reason="테스트 매수")
        h = build(tmp_path, {BTC: candles}, clock, strategy=strat)
        h.trader.run_once()

        price = candles[self.K + 1].close  # 진행중 실제 캔들의 현재가
        qty = expected_qty(price)
        fill = price * (1 + SLIP)
        pos = h.trader.positions[BTC]
        assert pos.quantity == approx(qty)
        assert pos.average_price == approx(fill)
        assert pos.stop_loss == approx(fill * (1 - STOP_LOSS_PCT))
        assert pos.take_profit is None
        assert pos.highest_price == approx(fill)
        assert pos.opened_at == clock.now
        assert pos.meta["entry_bar_ts"] == candles[self.K].timestamp.isoformat()
        assert pos.meta["entry_reason"] == "테스트 매수"
        assert pos.meta["entry_fee"] == approx(fill * qty * FEE)
        assert pos.meta["max_holding_bars"] is None
        assert pos.meta["entry_order_id"] == "paper-1"

        broker = h.broker
        assert broker.cash == approx(INITIAL_CASH - fill * qty * (1 + FEE))
        assert broker.get_positions()[BTC].quantity == approx(qty)
        assert qty * price >= 5000

        msgs = h.notifier.find("[체결]")
        assert len(msgs) == 1 and "매수" in msgs[0] and "테스트 매수" in msgs[0]

        data = json.loads(h.store.path.read_text())
        assert data["positions"][BTC]["quantity"] == approx(qty)
        assert data["positions"][BTC]["meta"]["entry_bar_ts"] == candles[self.K].timestamp.isoformat()
        assert data["paper_broker"]["cash"] == approx(broker.cash)
        assert data["risk"]["day_start_equity"] == approx(INITIAL_CASH)

    def test_buy_ignored_while_holding(self, tmp_path, candles, caplog):
        clock = FakeClock(after_candle(candles, self.K))
        strat = ScriptedStrategy()
        strat.at(BTC, candles[self.K].timestamp, SignalAction.BUY)
        strat.at(BTC, candles[self.K + 1].timestamp, SignalAction.BUY)
        h = build(tmp_path, {BTC: candles}, clock, strategy=strat)
        h.trader.run_once()
        qty = h.trader.positions[BTC].quantity
        clock.advance(H)
        with caplog.at_level(logging.INFO):
            h.trader.run_once()
        assert h.trader.positions[BTC].quantity == approx(qty)
        assert len(h.broker.orders) == 1
        assert any("BUY 신호를 무시" in r.message for r in caplog.records)

    def test_sell_without_position_ignored(self, tmp_path, candles):
        clock = FakeClock(after_candle(candles, self.K))
        strat = ScriptedStrategy()
        strat.at(BTC, candles[self.K].timestamp, SignalAction.SELL)
        h = build(tmp_path, {BTC: candles}, clock, strategy=strat)
        h.trader.run_once()
        assert h.trader.positions == {} and h.broker.orders == [] and h.trader.trades == []

    def test_max_positions_blocks_second_entry(self, tmp_path, daily_candles, eth_daily, caplog):
        k = 100
        ts = daily_candles[k].timestamp
        if find_index(eth_daily, ts) is None:
            pytest.skip("BTC/ETH 일봉 시각이 겹치지 않음")
        clock = FakeClock(after_candle(daily_candles, k, D))
        strat = ScriptedStrategy()
        strat.at(BTC, ts, SignalAction.BUY)
        strat.at(ETH, ts, SignalAction.BUY)
        h = build(
            tmp_path,
            {BTC: daily_candles, ETH: eth_daily},
            clock,
            interval="1d",
            strategy=strat,
            overrides={"risk": {"max_positions": 1}},
        )
        with caplog.at_level(logging.INFO):
            h.trader.run_once()
        assert list(h.trader.positions) == [BTC]
        assert any("최대 보유 종목 수 초과" in r.message for r in caplog.records)

    def test_order_error_is_isolated_and_notified(self, tmp_path, candles):
        class RejectingBroker(PaperBroker):
            def place_order(self, *a, **kw):
                raise OrderError("주문 거부 (테스트)")

        clock = FakeClock(after_candle(candles, self.K))
        feed = RealCandleFeed({BTC: candles}, clock, "1h")
        broker = RejectingBroker(initial_cash=INITIAL_CASH, fee_pct=FEE, slippage_pct=SLIP, data_source=feed)
        strat = ScriptedStrategy()
        strat.at(BTC, candles[self.K].timestamp, SignalAction.BUY)
        h = build(tmp_path, {BTC: candles}, clock, strategy=strat, broker=broker, feed=feed)
        h.trader.run_once()
        assert h.trader.positions == {}
        assert h.trader.cycle_errors == 1
        errs = h.notifier.find("[오류] KRW-BTC")
        assert len(errs) == 1 and "OrderError" in errs[0] and "주문 거부" in errs[0]
        assert h.store.path.exists()  # 오류가 나도 상태는 저장된다

    def test_budget_below_minimum_skips_order(self, tmp_path, candles, caplog):
        clock = FakeClock(after_candle(candles, self.K))
        strat = ScriptedStrategy()
        strat.at(BTC, candles[self.K].timestamp, SignalAction.BUY)
        h = build(
            tmp_path, {BTC: candles}, clock, strategy=strat, overrides={"paper": {"initial_cash": 4000}}
        )
        with caplog.at_level(logging.INFO):
            h.trader.run_once()
        assert h.trader.positions == {} and h.broker.orders == []
        assert any("주문 수량 0" in r.message for r in caplog.records)
        assert h.trader.cycle_errors == 0

    def test_limit_signal_places_limit_order_and_cancels_on_timeout(self, tmp_path, candles):
        clock = FakeClock(after_candle(candles, self.K))
        strat = ScriptedStrategy()
        limit_price = candles[self.K + 1].low  # 실제 캔들 저가
        strat.at(
            BTC, candles[self.K].timestamp, SignalAction.BUY, order_type=OrderType.LIMIT, price=limit_price
        )
        h = build(tmp_path, {BTC: candles}, clock, strategy=strat)
        h.trader.run_once()
        orders = h.broker.orders
        assert len(orders) == 1
        assert orders[0].type == OrderType.LIMIT and orders[0].price == approx(limit_price)
        assert orders[0].status == OrderStatus.CANCELED
        assert orders[0].quantity == approx(expected_qty(limit_price))
        assert sum(h.sleeps) == approx(h.config.broker.fill_timeout_sec)
        assert h.trader.positions == {}
        assert h.notifier.find("매수 미체결")

    def test_notify_on_signal(self, tmp_path, candles):
        clock = FakeClock(after_candle(candles, self.K))
        strat = ScriptedStrategy()
        strat.at(BTC, candles[self.K].timestamp, SignalAction.BUY, reason="신호 알림 테스트")
        h = build(
            tmp_path, {BTC: candles}, clock, strategy=strat, overrides={"notify": {"notify_on_signal": True}}
        )
        h.trader.run_once()
        sigs = h.notifier.find("[신호]")
        assert len(sigs) == 1 and "BUY" in sigs[0] and "신호 알림 테스트" in sigs[0]

    def test_trade_notification_can_be_disabled(self, tmp_path, candles):
        clock = FakeClock(after_candle(candles, self.K))
        strat = ScriptedStrategy()
        strat.at(BTC, candles[self.K].timestamp, SignalAction.BUY)
        h = build(
            tmp_path, {BTC: candles}, clock, strategy=strat, overrides={"notify": {"notify_on_trade": False}}
        )
        h.trader.run_once()
        assert BTC in h.trader.positions
        assert h.notifier.find("[체결]") == []


# ============================================================================ 청산 (SELL / 리스크)
class TestExit:
    K = 40

    def _buy(self, tmp_path, candles, strat=None, **overrides) -> Harness:
        clock = FakeClock(after_candle(candles, self.K))
        strat = strat or ScriptedStrategy()
        strat.at(BTC, candles[self.K].timestamp, SignalAction.BUY)
        h = build(tmp_path, {BTC: candles}, clock, strategy=strat, overrides=overrides)
        h.trader.run_once()
        assert BTC in h.trader.positions
        return h

    def test_sell_signal_creates_trade_and_records_pnl(self, tmp_path, candles):
        strat = ScriptedStrategy()
        strat.at(BTC, candles[self.K + 1].timestamp, SignalAction.SELL, reason="테스트 매도")
        h = self._buy(tmp_path, candles, strat)
        pos = h.trader.positions[BTC]
        qty, entry, entry_fee = pos.quantity, pos.average_price, pos.meta["entry_fee"]
        buy_time = h.clock.now
        cash_before = h.broker.cash

        h.clock.advance(H)
        h.trader.run_once()

        assert BTC not in h.trader.positions
        assert len(h.trader.trades) == 1
        t = h.trader.trades[0]
        exit_price = candles[self.K + 2].close * (1 - SLIP)
        assert t.symbol == BTC and t.side == OrderSide.BUY
        assert t.quantity == approx(qty)
        assert t.entry_price == approx(entry)
        assert t.exit_price == approx(exit_price)
        assert t.entry_time == buy_time and t.exit_time == h.clock.now
        assert t.fee == approx(entry_fee + exit_price * qty * FEE)
        assert t.reason == "테스트 매도"
        assert t.pnl == approx((exit_price - entry) * qty - t.fee)

        assert h.trader.risk.daily_pnl == approx(t.pnl)
        assert h.trader.risk.daily_trades == 1
        assert h.broker.trades[0].pnl == approx(t.pnl)
        assert h.broker.cash == approx(cash_before + exit_price * qty * (1 - FEE))
        assert h.broker.get_positions() == {}

        msgs = h.notifier.find("매도")
        assert msgs and "손익" in msgs[-1] and "테스트 매도" in msgs[-1]
        data = json.loads(h.store.path.read_text())
        assert len(data["trades"]) == 1 and data["trades"][0]["pnl"] == approx(t.pnl)
        assert data["positions"] == {}
        assert data["risk"]["daily_pnl"] == approx(t.pnl)

    def test_stop_loss_exit_on_poll_when_price_drops(self, tmp_path, candles, caplog):
        sl = 0.01
        h = self._buy(tmp_path, candles, risk={"stop_loss_pct": sl})
        pos = h.trader.positions[BTC]
        stop = pos.stop_loss
        assert stop == approx(pos.average_price * (1 - sl))
        first_hit = next((j for j in range(self.K + 2, len(candles)) if candles[j].low <= stop), None)
        if first_hit is None:
            pytest.skip("실제 데이터에 손절 수준까지의 하락이 없음")

        h.feed.ticker_field = "low"  # 각 폴링의 현재가 = 진행중 실제 캔들의 저가
        for j in range(self.K + 2, first_hit + 1):
            h.clock.set(after_candle(candles, j - 1))
            with caplog.at_level(logging.INFO):
                h.trader.run_once()
            if j < first_hit:
                assert BTC in h.trader.positions, f"캔들 {j} 에서 조기 청산"
        assert BTC not in h.trader.positions
        t = h.trader.trades[-1]
        assert t.exit_price == approx(candles[first_hit].low * (1 - SLIP))
        assert t.reason.startswith("손절")
        assert t.exit_time == h.clock.now
        assert any("[stop_loss]" in r.message for r in caplog.records)

    def test_take_profit_exit(self, tmp_path, candles):
        tp = 0.01
        h = self._buy(tmp_path, candles, risk={"take_profit_pct": tp, "stop_loss_pct": None})
        pos = h.trader.positions[BTC]
        assert pos.stop_loss is None and pos.take_profit == approx(pos.average_price * (1 + tp))
        target = pos.take_profit
        first_hit = next((j for j in range(self.K + 2, len(candles)) if candles[j].high >= target), None)
        if first_hit is None:
            pytest.skip("실제 데이터에 익절 수준까지의 상승이 없음")
        h.feed.ticker_field = "high"
        for j in range(self.K + 2, first_hit + 1):
            h.clock.set(after_candle(candles, j - 1))
            h.trader.run_once()
        assert BTC not in h.trader.positions
        t = h.trader.trades[-1]
        assert t.reason.startswith("익절")
        assert t.exit_price == approx(candles[first_hit].high * (1 - SLIP))
        assert t.pnl > 0

    def test_exit_quantity_capped_by_broker_holdings(self, tmp_path, candles, caplog):
        strat = ScriptedStrategy()
        strat.at(BTC, candles[self.K + 1].timestamp, SignalAction.SELL)
        h = self._buy(tmp_path, candles, strat)
        pos = h.trader.positions[BTC]
        held = pos.quantity
        pos.quantity = held * 2  # 상태가 거래소보다 많다고 잘못 알고 있는 상황
        h.clock.advance(H)
        with caplog.at_level(logging.WARNING):
            h.trader.run_once()
        assert BTC not in h.trader.positions
        assert h.trader.trades[-1].quantity == approx(held)
        assert any("매도 수량 조정" in r.message for r in caplog.records)

    def test_exit_when_broker_no_longer_holds_drops_position(self, tmp_path, candles):
        strat = ScriptedStrategy()
        strat.at(BTC, candles[self.K + 1].timestamp, SignalAction.SELL)
        h = self._buy(tmp_path, candles, strat)
        qty = h.trader.positions[BTC].quantity
        h.broker.place_order(BTC, OrderSide.SELL, qty)  # 봇 밖에서 수동 매도
        h.clock.advance(H)
        h.trader.run_once()
        assert BTC not in h.trader.positions
        assert h.trader.trades == []  # 엔진이 체결한 매도가 아니다
        assert h.notifier.find("[동기화]")
        assert h.trader.cycle_errors == 0

    def test_exit_type_recorded(self, tmp_path, candles):
        strat = ScriptedStrategy()
        strat.at(BTC, candles[self.K + 1].timestamp, SignalAction.SELL)
        h = self._buy(tmp_path, candles, strat)
        pos = h.trader.positions[BTC]
        h.clock.advance(H)
        h.trader.run_once()
        assert pos.meta["exit_type"] == EXIT_SIGNAL


# ============================================================================ 변동성 돌파 (STOP BUY)
class TestBreakout:
    def _setup(self, tmp_path, daily, i, **overrides) -> Harness:
        clock = FakeClock(after_candle(daily, i - 1, D))  # 캔들 i-1 완성, i 진행중
        h = build(
            tmp_path,
            {BTC: daily},
            clock,
            interval="1d",
            strategy=VolatilityBreakoutStrategy(k=0.5),
            overrides=overrides,
        )
        h.feed.ticker_field = "open"  # 등록 폴링의 현재가 = 시가 (트리거 미만)
        return h

    def test_registers_pending_with_forming_open(self, tmp_path, daily_candles):
        hits, misses = breakout_days(daily_candles)
        if not misses:
            pytest.skip("실제 데이터에 비돌파 일이 없음")
        i = misses[0]
        h = self._setup(tmp_path, daily_candles, i)
        h.trader.run_once()

        pb = h.trader.pending_breakouts[BTC]
        rng = daily_candles[i - 1].high - daily_candles[i - 1].low
        assert pb.trigger == approx(daily_candles[i].open + 0.5 * rng)
        assert pb.base_open == approx(daily_candles[i].open)
        assert pb.candle_ts == daily_candles[i - 1].timestamp
        assert pb.expires == floor_to_interval(h.clock.now, "1d") + timedelta(days=1)
        assert pb.expires == daily_candles[i].timestamp + timedelta(days=1)
        assert pb.signal.order_type == OrderType.STOP and pb.signal.max_holding_bars == 1
        assert h.trader.positions == {} and h.broker.orders == []
        data = json.loads(h.store.path.read_text())
        assert data["pending_breakouts"][BTC]["trigger"] == approx(pb.trigger)
        assert data["pending_breakouts"][BTC]["signal"]["stop_offset"] == approx(0.5 * rng)
        assert h.trader.status()["pending_breakouts"][BTC]["expires"] == pb.expires.isoformat()

    def test_trigger_buys_at_market_with_max_holding_one(self, tmp_path, daily_candles):
        hits, _ = breakout_days(daily_candles)
        if not hits:
            pytest.skip("실제 데이터에 돌파 일이 없음")
        i = hits[0]
        h = self._setup(tmp_path, daily_candles, i)
        h.trader.run_once()
        trigger = h.trader.pending_breakouts[BTC].trigger

        h.feed.ticker_field = "high"  # 장중 고가 도달
        h.clock.advance(H)
        h.trader.run_once()
        assert BTC not in h.trader.pending_breakouts
        pos = h.trader.positions[BTC]
        price = daily_candles[i].high
        assert price >= trigger
        assert pos.average_price == approx(price * (1 + SLIP))
        assert pos.quantity == approx(expected_qty(price))
        assert pos.meta["max_holding_bars"] == 1
        assert pos.meta["entry_bar_ts"] == daily_candles[i - 1].timestamp.isoformat()
        assert pos.meta["entry_signal_meta"]["trigger"] == approx(trigger)
        assert pos.opened_at == h.clock.now
        assert "변동성 돌파" in pos.meta["entry_reason"]
        assert h.notifier.find("[체결]")

    def test_no_trigger_when_high_below_trigger(self, tmp_path, daily_candles):
        _, misses = breakout_days(daily_candles)
        if not misses:
            pytest.skip("실제 데이터에 비돌파 일이 없음")
        i = misses[0]
        h = self._setup(tmp_path, daily_candles, i)
        h.trader.run_once()
        h.feed.ticker_field = "high"
        h.clock.advance(H)
        h.trader.run_once()
        assert h.trader.positions == {}
        assert BTC in h.trader.pending_breakouts  # 아직 만료 전

    def test_pending_expires_without_new_candle(self, tmp_path, daily_candles, caplog):
        _, misses = breakout_days(daily_candles)
        if not misses:
            pytest.skip("실제 데이터에 비돌파 일이 없음")
        i = misses[0]
        h = self._setup(tmp_path, daily_candles, i)
        h.trader.run_once()
        h.feed.lag = 2 * H  # 다음 캔들이 아직 서버에 반영되지 않은 상황
        h.clock.set(daily_candles[i + 1].timestamp + timedelta(seconds=5))
        with caplog.at_level(logging.INFO):
            h.trader.run_once()
        assert h.trader.positions == {} and h.trader.pending_breakouts == {}
        assert h.trader.last_candle_ts[BTC] == daily_candles[i - 1].timestamp  # 새 캔들 없음
        assert any("돌파 대기 만료" in r.message for r in caplog.records)

    def test_new_candle_replaces_pending(self, tmp_path, daily_candles):
        _, misses = breakout_days(daily_candles)
        if not misses:
            pytest.skip("실제 데이터에 비돌파 일이 없음")
        i = misses[0]
        h = self._setup(tmp_path, daily_candles, i)
        h.trader.run_once()
        h.clock.set(after_candle(daily_candles, i, D))
        h.trader.run_once()
        pb = h.trader.pending_breakouts[BTC]
        rng = daily_candles[i].high - daily_candles[i].low
        assert pb.candle_ts == daily_candles[i].timestamp
        assert pb.trigger == approx(daily_candles[i + 1].open + 0.5 * rng)
        assert h.trader.positions == {}

    def test_max_holding_expiry_sells_on_first_poll_after_candle(self, tmp_path, daily_candles):
        hits, _ = breakout_days(daily_candles)
        if not hits:
            pytest.skip("실제 데이터에 돌파 일이 없음")
        i = hits[0]
        h = self._setup(tmp_path, daily_candles, i)
        h.trader.run_once()
        h.feed.ticker_field = "high"
        h.clock.advance(H)
        h.trader.run_once()
        pos = h.trader.positions[BTC]
        entry, qty, buy_time = pos.average_price, pos.quantity, h.clock.now

        h.feed.ticker_field = "open"  # 다음 날 첫 폴링의 현재가 = 다음 날 시가
        h.clock.set(after_candle(daily_candles, i, D))
        h.trader.run_once()

        assert BTC not in h.trader.positions
        t = h.trader.trades[-1]
        assert t.exit_price == approx(daily_candles[i + 1].open * (1 - SLIP))
        assert t.entry_price == approx(entry) and t.quantity == approx(qty)
        assert t.entry_time == buy_time and t.exit_time == h.clock.now
        assert "최대 보유 기간 만료 (1/1봉)" in t.reason
        assert pos.meta["exit_type"] == EXIT_MAX_HOLDING
        assert h.trader.risk.daily_pnl == approx(t.pnl)  # 청산일(UTC) 기준으로 집계
        assert h.notifier.find("[일일 요약]")  # 날짜가 바뀌었으므로 전일 요약
        # 같은 폴링에서 다음 날 돌파 대기가 새로 등록된다
        pb = h.trader.pending_breakouts[BTC]
        assert pb.candle_ts == daily_candles[i].timestamp
        assert pb.trigger == approx(
            daily_candles[i + 1].open + 0.5 * (daily_candles[i].high - daily_candles[i].low)
        )

    def test_stop_signal_not_registered_while_holding(self, tmp_path, candles):
        k = 40
        clock = FakeClock(after_candle(candles, k))
        strat = ScriptedStrategy()
        strat.at(BTC, candles[k].timestamp, SignalAction.BUY)
        strat.at(
            BTC, candles[k + 1].timestamp, SignalAction.BUY, order_type=OrderType.STOP, stop_offset=1000.0
        )
        h = build(tmp_path, {BTC: candles}, clock, strategy=strat)
        h.trader.run_once()
        clock.advance(H)
        h.trader.run_once()
        assert BTC in h.trader.positions and h.trader.pending_breakouts == {}

    def test_stop_signal_with_absolute_price(self, tmp_path, candles):
        k = 40
        clock = FakeClock(after_candle(candles, k))
        strat = ScriptedStrategy()
        level = candles[k + 1].high  # 실제 캔들 고가를 돌파 기준으로
        strat.at(BTC, candles[k].timestamp, SignalAction.BUY, order_type=OrderType.STOP, price=level)
        h = build(tmp_path, {BTC: candles}, clock, strategy=strat)
        h.feed.ticker_field = "low"
        h.trader.run_once()
        pb = h.trader.pending_breakouts[BTC]
        assert pb.trigger == approx(level) and pb.base_open is None
        assert pb.expires == floor_to_interval(clock.now, "1h") + timedelta(hours=1)
        h.feed.ticker_field = "high"
        clock.advance(60)
        h.trader.run_once()
        assert h.trader.positions[BTC].average_price == approx(level * (1 + SLIP))

    def test_stop_signal_without_price_or_offset_ignored(self, tmp_path, candles, caplog):
        k = 40
        clock = FakeClock(after_candle(candles, k))
        strat = ScriptedStrategy()
        strat.at(BTC, candles[k].timestamp, SignalAction.BUY, order_type=OrderType.STOP)
        h = build(tmp_path, {BTC: candles}, clock, strategy=strat)
        with caplog.at_level(logging.WARNING):
            h.trader.run_once()
        assert h.trader.pending_breakouts == {} and h.trader.positions == {}
        assert any("stop_offset" in r.message for r in caplog.records)

    def test_forming_open_falls_back_to_ticker(self, tmp_path, daily_candles, caplog):
        class NoPartialFeed(RealCandleFeed):
            def get_candles(self, symbol, interval, limit=200, end=None, include_partial=False):
                if include_partial:
                    raise BrokerError("진행중 캔들 조회 실패 (테스트)")
                return super().get_candles(symbol, interval, limit, end, include_partial)

        _, misses = breakout_days(daily_candles)
        if not misses:
            pytest.skip("실제 데이터에 비돌파 일이 없음")
        i = misses[0]
        clock = FakeClock(after_candle(daily_candles, i - 1, D))
        feed = NoPartialFeed({BTC: daily_candles}, clock, "1d")
        feed.ticker_field = "open"
        h = build(
            tmp_path,
            {BTC: daily_candles},
            clock,
            interval="1d",
            strategy=VolatilityBreakoutStrategy(),
            feed=feed,
        )
        with caplog.at_level(logging.WARNING):
            h.trader.run_once()
        pb = h.trader.pending_breakouts[BTC]
        rng = daily_candles[i - 1].high - daily_candles[i - 1].low
        assert pb.base_open == approx(daily_candles[i].open)  # 현재가(=시가) 로 대체
        assert pb.trigger == approx(daily_candles[i].open + 0.5 * rng)
        assert any("현재가" in r.message and "기준 시가" in r.message for r in caplog.records)

    def test_max_holding_uses_opened_at_when_entry_bar_ts_missing(self, tmp_path, candles):
        k = 40
        clock = FakeClock(after_candle(candles, k))
        feed = RealCandleFeed({BTC: candles}, clock, "1h")
        broker = PaperBroker(initial_cash=INITIAL_CASH, fee_pct=FEE, slippage_pct=SLIP, data_source=feed)
        broker.clock = clock
        broker.place_order(BTC, OrderSide.BUY, 0.01)  # 봇 밖에서 매수 (진행중 캔들 k+1 시각)
        h = build(tmp_path, {BTC: candles}, clock, broker=broker, feed=feed)
        h.trader.start()
        pos = h.trader.positions[BTC]
        assert pos.meta["adopted"] and pos.meta["entry_bar_ts"] is None
        pos.meta["max_holding_bars"] = 1
        h.trader.run_once()
        assert BTC in h.trader.positions  # 캔들 k+1 아직 진행중
        clock.advance(H)
        h.trader.run_once()
        assert BTC not in h.trader.positions
        assert "최대 보유 기간 만료 (1/1봉)" in h.trader.trades[-1].reason


# ============================================================================ 시작 시 포지션 동기화
class TestSyncPositions:
    K = 40

    def _feed_and_broker(self, candles, clock, broker_cls=PaperBroker):
        feed = RealCandleFeed({BTC: candles, ETH: candles}, clock, "1h")
        broker = broker_cls(initial_cash=INITIAL_CASH, fee_pct=FEE, slippage_pct=SLIP, data_source=feed)
        return feed, broker

    def test_adopts_unknown_broker_position_with_its_average_price(self, tmp_path, candles):
        clock = FakeClock(after_candle(candles, self.K))
        feed, broker = self._feed_and_broker(candles, clock)
        broker.place_order(BTC, OrderSide.BUY, 0.01)
        bpos = broker.get_positions()[BTC]
        h = build(tmp_path, {BTC: candles}, clock, broker=broker, feed=feed)
        h.trader.start()
        pos = h.trader.positions[BTC]
        assert pos.quantity == approx(bpos.quantity)
        assert pos.average_price == approx(bpos.average_price)
        assert pos.meta["adopted"] is True and pos.meta["entry_fee"] == 0.0
        assert pos.stop_loss == approx(bpos.average_price * (1 - STOP_LOSS_PCT))
        assert pos.highest_price == approx(bpos.average_price)
        assert any("채택" in m for m in h.notifier.find("[동기화]"))
        assert any("보유 포지션: KRW-BTC" in m for m in h.notifier.find("[시작]"))

    def test_drops_state_position_missing_at_broker(self, tmp_path, candles):
        clock = FakeClock(after_candle(candles, self.K))
        state_file = tmp_path / "state.json"
        stale = Position(BTC, 0.5, candles[self.K].close, opened_at=clock.now, stop_loss=candles[self.K].low)
        StateStore(state_file).save({"version": STATE_VERSION, "positions": {BTC: serialize_position(stale)}})
        h = build(tmp_path, {BTC: candles}, clock)
        h.trader.start()
        assert h.trader.positions == {}
        assert any("거래소에 없어" in m for m in h.notifier.find("[동기화]"))

    def test_keeps_state_average_when_broker_reports_zero(self, tmp_path, candles):
        class ZeroAvgBroker(PaperBroker):
            def get_positions(self):
                return {
                    s: dataclasses.replace(p, average_price=0.0, meta=dict(p.meta))
                    for s, p in super().get_positions().items()
                }

        clock = FakeClock(after_candle(candles, self.K))
        feed, broker = self._feed_and_broker(candles, clock, ZeroAvgBroker)
        broker.place_order(BTC, OrderSide.BUY, 0.02)
        true_avg = candles[self.K + 1].close * (1 + SLIP)
        saved = Position(
            BTC, 0.01, true_avg, opened_at=clock.now, stop_loss=true_avg * 0.97, meta={"entry_fee": 1.0}
        )
        StateStore(tmp_path / "state.json").save(
            {"version": STATE_VERSION, "positions": {BTC: serialize_position(saved)}}
        )
        h = build(tmp_path, {BTC: candles}, clock, broker=broker, feed=feed)
        h.trader.start()
        pos = h.trader.positions[BTC]
        assert pos.quantity == approx(0.02)  # 수량은 거래소 값
        assert pos.average_price == approx(true_avg)  # 평균단가는 상태 값 유지
        assert pos.stop_loss == approx(true_avg * 0.97) and pos.meta["entry_fee"] == 1.0

    def test_broker_average_overrides_state_when_positive(self, tmp_path, candles):
        clock = FakeClock(after_candle(candles, self.K))
        feed, broker = self._feed_and_broker(candles, clock)
        broker.place_order(BTC, OrderSide.BUY, 0.02)
        bavg = broker.get_positions()[BTC].average_price
        saved = Position(BTC, 0.02, bavg * 0.9, opened_at=clock.now)
        StateStore(tmp_path / "state.json").save(
            {"version": STATE_VERSION, "positions": {BTC: serialize_position(saved)}}
        )
        h = build(tmp_path, {BTC: candles}, clock, broker=broker, feed=feed)
        h.trader.start()
        assert h.trader.positions[BTC].average_price == approx(bavg)

    def test_unconfigured_symbol_is_not_adopted(self, tmp_path, candles):
        clock = FakeClock(after_candle(candles, self.K))
        feed, broker = self._feed_and_broker(candles, clock)
        broker.place_order(ETH, OrderSide.BUY, 0.01)
        h = build(
            tmp_path,
            {BTC: candles, ETH: candles},
            clock,
            broker=broker,
            feed=feed,
            overrides={"symbols": [BTC]},
        )
        h.trader.start()
        assert h.trader.positions == {}
        assert ETH in broker.get_positions()

    def test_sync_disabled_keeps_state_positions(self, tmp_path, candles):
        clock = FakeClock(after_candle(candles, self.K))
        saved = Position(BTC, 0.5, candles[self.K].close, opened_at=clock.now)
        StateStore(tmp_path / "state.json").save(
            {"version": STATE_VERSION, "positions": {BTC: serialize_position(saved)}}
        )
        h = build(tmp_path, {BTC: candles}, clock, overrides={"engine": {"sync_positions_on_start": False}})
        h.trader.start()
        assert h.trader.positions[BTC].quantity == 0.5

    def test_sync_failure_is_notified_and_start_continues(self, tmp_path, candles):
        class BrokenPositions(PaperBroker):
            def get_positions(self):
                raise BrokerError("포지션 조회 실패 (테스트)")

        clock = FakeClock(after_candle(candles, self.K))
        feed, broker = self._feed_and_broker(candles, clock, BrokenPositions)
        h = build(tmp_path, {BTC: candles}, clock, broker=broker, feed=feed)
        h.trader.start()
        assert h.trader.started
        assert any("포지션 동기화" in m for m in h.notifier.find("[오류]"))


# ============================================================================ 체결 대기 / 타임아웃
class TestFillTimeout:
    K = 40
    TIMEOUT = 5.0

    def _harness(self, tmp_path, candles, on_cancel="cancel", sleep=None) -> tuple[Harness, StuckBroker]:
        clock = FakeClock(after_candle(candles, self.K))
        feed = RealCandleFeed({BTC: candles}, clock, "1h")
        broker = StuckBroker(
            initial_cash=INITIAL_CASH, fee_pct=FEE, slippage_pct=SLIP, data_source=feed, on_cancel=on_cancel
        )
        strat = ScriptedStrategy()
        strat.at(BTC, candles[self.K].timestamp, SignalAction.BUY)
        h = build(
            tmp_path,
            {BTC: candles},
            clock,
            strategy=strat,
            broker=broker,
            feed=feed,
            sleep=sleep,
            overrides={"broker": {"fill_timeout_sec": self.TIMEOUT}},
        )
        return h, broker

    def test_timeout_cancels_order_and_keeps_no_position(self, tmp_path, candles, caplog):
        h, broker = self._harness(tmp_path, candles)
        with caplog.at_level(logging.WARNING):
            h.trader.run_once()
        assert broker.cancel_calls == ["stuck-1"]
        assert broker.stuck["stuck-1"].status == OrderStatus.CANCELED
        assert sum(h.sleeps) == approx(self.TIMEOUT)
        assert broker.get_order_calls == len(h.sleeps) + 1  # 폴링 + 취소 후 재조회
        assert h.trader.positions == {}
        assert h.notifier.find("매수 미체결")
        assert any("체결되지 않아 취소" in r.message for r in caplog.records)
        assert h.trader.cycle_errors == 0

    def test_filled_while_canceling_creates_position(self, tmp_path, candles):
        h, broker = self._harness(tmp_path, candles, on_cancel="filled")
        h.trader.run_once()
        pos = h.trader.positions[BTC]
        order = broker.stuck["stuck-1"]
        assert pos.quantity == approx(order.quantity)
        assert pos.average_price == approx(candles[self.K + 1].close)
        assert pos.opened_at == order.updated_at

    def test_partial_fill_at_timeout_creates_partial_position(self, tmp_path, candles):
        h, broker = self._harness(tmp_path, candles, on_cancel="partial")
        h.trader.run_once()
        order = broker.stuck["stuck-1"]
        assert h.trader.positions[BTC].quantity == approx(order.quantity / 2)

    def test_frozen_clock_and_noop_sleep_still_terminate(self, tmp_path, candles):
        h, broker = self._harness(tmp_path, candles, sleep=lambda s: None)
        h.trader.run_once()
        assert broker.cancel_calls == ["stuck-1"]
        assert broker.get_order_calls == int(self.TIMEOUT) + 2  # max_polls(6) + 재조회
        assert h.trader.positions == {}

    def test_get_order_errors_during_wait_are_tolerated(self, tmp_path, candles, caplog):
        class FlakyBroker(StuckBroker):
            def get_order(self, order_id, symbol=None):
                self.get_order_calls += 1
                if self.get_order_calls <= 2:
                    raise BrokerError("일시 장애")
                o = self.stuck[order_id]
                o.status = OrderStatus.FILLED
                o.filled_quantity = o.quantity
                o.average_price = self.get_ticker(o.symbol)
                o.updated_at = self.clock()
                return dataclasses.replace(o)

        clock = FakeClock(after_candle(candles, self.K))
        feed = RealCandleFeed({BTC: candles}, clock, "1h")
        broker = FlakyBroker(initial_cash=INITIAL_CASH, fee_pct=FEE, slippage_pct=SLIP, data_source=feed)
        strat = ScriptedStrategy()
        strat.at(BTC, candles[self.K].timestamp, SignalAction.BUY)
        h = build(tmp_path, {BTC: candles}, clock, strategy=strat, broker=broker, feed=feed)
        with caplog.at_level(logging.WARNING):
            h.trader.run_once()
        assert BTC in h.trader.positions
        assert broker.cancel_calls == []
        assert sum("상태 조회 실패" in r.message for r in caplog.records) == 2


# ============================================================================ 오류 격리 / 알림 제한
class TestErrorIsolation:
    def _two_symbol(self, tmp_path, daily_candles, eth_daily, **overrides) -> Harness:
        k = 120
        if find_index(eth_daily, daily_candles[k].timestamp) is None:
            pytest.skip("BTC/ETH 일봉 시각이 겹치지 않음")
        clock = FakeClock(after_candle(daily_candles, k, D))
        return build(
            tmp_path, {BTC: daily_candles, ETH: eth_daily}, clock, interval="1d", overrides=overrides
        )

    def test_failing_symbol_does_not_block_others(self, tmp_path, daily_candles, eth_daily):
        h = self._two_symbol(tmp_path, daily_candles, eth_daily)
        h.feed.fail_symbols = {BTC}
        h.trader.run_once()
        assert [s for s, _ in h.strategy.calls] == [ETH]
        assert h.trader.cycle_errors == 1
        assert ETH in h.trader.last_candle_ts and BTC not in h.trader.last_candle_ts
        errs = h.notifier.find("[오류] KRW-BTC")
        assert len(errs) == 1 and "BrokerError" in errs[0] and "테스트용 장애" in errs[0]

    def test_same_error_notified_once_per_interval(self, tmp_path, daily_candles, eth_daily):
        h = self._two_symbol(tmp_path, daily_candles, eth_daily)
        h.feed.fail_symbols = {BTC}
        h.trader.run_once()
        h.clock.advance(60)
        h.trader.run_once()
        assert len(h.notifier.find("[오류] KRW-BTC")) == 1
        h.clock.advance(ERROR_NOTIFY_INTERVAL_SEC)
        h.trader.run_once()
        assert len(h.notifier.find("[오류] KRW-BTC")) == 2

    def test_error_notification_can_be_disabled(self, tmp_path, daily_candles, eth_daily):
        h = self._two_symbol(tmp_path, daily_candles, eth_daily, notify={"notify_on_error": False})
        h.feed.fail_symbols = {BTC}
        h.trader.run_once()
        assert h.notifier.find("[오류]") == []
        assert h.trader.cycle_errors == 1

    def test_unexpected_exception_is_logged_with_traceback(self, tmp_path, candles, caplog):
        class ExplodingFeed(RealCandleFeed):
            def get_ticker(self, symbol):
                raise RuntimeError("예상 밖 오류")

        clock = FakeClock(after_candle(candles, 40))
        feed = ExplodingFeed({BTC: candles}, clock, "1h")
        h = build(tmp_path, {BTC: candles}, clock, feed=feed)
        with caplog.at_level(logging.ERROR):
            h.trader.run_once()
        rec = [r for r in caplog.records if "예기치 못한 오류" in r.message]
        assert rec and rec[0].exc_info is not None
        assert h.notifier.find("RuntimeError")

    def test_market_open_check_failure_skips_cycle(self, tmp_path, candles):
        clock = FakeClock(after_candle(candles, 40))
        feed = RealCandleFeed({BTC: candles}, clock, "1h")
        h = build(tmp_path, {BTC: candles}, clock, feed=feed)

        def broken():
            raise BrokerError("clock api down")

        feed.is_market_open = broken  # type: ignore[assignment]
        h.trader.run_once()
        assert h.trader.cycle_errors == 1 and h.strategy.calls == []
        assert h.notifier.find("장 운영 여부 확인")

    def test_state_save_failure_is_notified(self, tmp_path, candles, monkeypatch, caplog):
        clock = FakeClock(after_candle(candles, 40))
        h = build(tmp_path, {BTC: candles}, clock)

        def failing_save(data):
            raise DataError("디스크 꽉 참 (테스트)")

        monkeypatch.setattr(h.store, "save", failing_save)
        with caplog.at_level(logging.ERROR):
            h.trader.run_once()
        assert any("상태 저장 실패" in r.message for r in caplog.records)
        assert h.notifier.find("상태 저장 실패")


# ============================================================================ 상태 복원
class TestRestore:
    K = 40

    def _buy_then_restart(self, tmp_path, candles) -> tuple[Harness, Harness]:
        clock = FakeClock(after_candle(candles, self.K))
        strat = ScriptedStrategy()
        strat.at(BTC, candles[self.K].timestamp, SignalAction.BUY)
        h1 = build(tmp_path, {BTC: candles}, clock, strategy=strat)
        h1.trader.run_once()
        assert BTC in h1.trader.positions
        h2 = build(tmp_path, {BTC: candles}, clock)
        return h1, h2

    def test_full_round_trip(self, tmp_path, candles):
        h1, h2 = self._buy_then_restart(tmp_path, candles)
        injected = h2.trader.broker
        h2.trader.start()
        assert h2.trader.broker is not injected and isinstance(h2.trader.broker, PaperBroker)
        assert h2.trader.broker.data_source is h2.feed
        assert h2.broker.cash == approx(h1.broker.cash)
        assert h2.broker.fee_pct == FEE and h2.broker.slippage_pct == SLIP
        p1, p2 = h1.trader.positions[BTC], h2.trader.positions[BTC]
        assert (p2.quantity, p2.average_price, p2.stop_loss, p2.highest_price) == approx(
            (p1.quantity, p1.average_price, p1.stop_loss, p1.highest_price)
        )
        assert p2.opened_at == p1.opened_at and p2.meta == p1.meta
        assert h2.trader.last_candle_ts == h1.trader.last_candle_ts
        assert h2.trader.risk.to_dict() == h1.trader.risk.to_dict()
        assert h2.broker.get_positions()[BTC].quantity == approx(p1.quantity)
        assert any("보유 포지션: KRW-BTC" in m for m in h2.notifier.find("[시작]"))

        h2.trader.run_once()
        assert h2.strategy.calls == []  # 같은 캔들은 다시 평가하지 않는다

        assert isinstance(h2.strategy, ScriptedStrategy)
        h2.strategy.at(BTC, candles[self.K + 1].timestamp, SignalAction.SELL)
        cash_before = h2.broker.cash
        h2.clock.advance(H)
        h2.trader.run_once()
        t = h2.trader.trades[-1]
        assert t.entry_price == approx(p1.average_price) and t.entry_time == p1.opened_at
        assert t.fee == approx(p1.meta["entry_fee"] + t.exit_price * t.quantity * FEE)
        assert h2.broker.cash == approx(cash_before + t.exit_price * t.quantity * (1 - FEE))
        assert h2.broker.orders[-1].id == "paper-2"  # 주문 시퀀스 이어짐
        assert h2.trader.risk.daily_pnl == approx(t.pnl)

    def test_restores_pending_breakout_and_skips_expired(self, tmp_path, daily_candles):
        _, misses = breakout_days(daily_candles)
        if not misses:
            pytest.skip("실제 데이터에 비돌파 일이 없음")
        i = misses[0]
        clock = FakeClock(after_candle(daily_candles, i - 1, D))
        h1 = build(
            tmp_path, {BTC: daily_candles}, clock, interval="1d", strategy=VolatilityBreakoutStrategy()
        )
        h1.feed.ticker_field = "open"
        h1.trader.run_once()
        pb1 = h1.trader.pending_breakouts[BTC]

        h2 = build(
            tmp_path, {BTC: daily_candles}, clock, interval="1d", strategy=VolatilityBreakoutStrategy()
        )
        h2.trader.start()
        pb2 = h2.trader.pending_breakouts[BTC]
        assert (pb2.trigger, pb2.expires, pb2.candle_ts, pb2.base_open) == (
            approx(pb1.trigger),
            pb1.expires,
            pb1.candle_ts,
            approx(pb1.base_open),
        )
        assert pb2.signal.stop_offset == approx(pb1.signal.stop_offset) and pb2.signal.max_holding_bars == 1

        clock.set(pb1.expires + timedelta(seconds=1))
        h3 = build(
            tmp_path, {BTC: daily_candles}, clock, interval="1d", strategy=VolatilityBreakoutStrategy()
        )
        h3.trader.start()
        assert h3.trader.pending_breakouts == {}

    def test_version_mismatch_ignores_state(self, tmp_path, candles, caplog):
        clock = FakeClock(after_candle(candles, self.K))
        saved = Position(BTC, 0.5, candles[self.K].close)
        StateStore(tmp_path / "state.json").save(
            {"version": 99, "positions": {BTC: serialize_position(saved)}}
        )
        h = build(tmp_path, {BTC: candles}, clock, overrides={"engine": {"sync_positions_on_start": False}})
        with caplog.at_level(logging.WARNING):
            h.trader.start()
        assert h.trader.positions == {}
        assert any("버전" in r.message for r in caplog.records)

    def test_interval_change_resets_candle_ts_and_pending(self, tmp_path, candles):
        h1, h2 = self._buy_then_restart(tmp_path, candles)
        data = json.loads(h1.store.path.read_text())
        data["interval"] = "1d"
        data["pending_breakouts"] = {
            BTC: PendingBreakout(
                BTC,
                candles[self.K].high,
                Signal(SignalAction.BUY, BTC, order_type=OrderType.STOP),
                expires=h1.clock.now + timedelta(days=1),
                created_at=h1.clock.now,
            ).to_dict()
        }
        h1.store.save(data)
        h2.trader.start()
        assert BTC in h2.trader.positions  # 포지션은 실제 보유이므로 유지
        assert h2.trader.last_candle_ts == {} and h2.trader.pending_breakouts == {}

    def test_corrupt_entries_are_skipped(self, tmp_path, candles, caplog):
        h1, h2 = self._buy_then_restart(tmp_path, candles)
        data = json.loads(h1.store.path.read_text())
        data["positions"]["KRW-XRP"] = {"symbol": "KRW-XRP"}  # quantity 없음
        data["trades"] = [{"symbol": BTC}]  # 필수 필드 없음
        data["last_candle_ts"]["KRW-XRP"] = "not-a-date"
        data["pending_breakouts"] = {"KRW-XRP": {"trigger": "x"}}
        h1.store.save(data)
        with caplog.at_level(logging.WARNING):
            h2.trader.start()
        assert list(h2.trader.positions) == [BTC]
        assert h2.trader.trades == [] and h2.trader.pending_breakouts == {}
        assert h2.trader.last_candle_ts == {BTC: candles[self.K].timestamp}
        assert sum("복원 실패" in r.message for r in caplog.records) >= 3

    def test_paper_state_with_other_quote_is_not_restored(self, tmp_path, candles, caplog):
        h1, h2 = self._buy_then_restart(tmp_path, candles)
        data = json.loads(h1.store.path.read_text())
        data["paper_broker"]["quote_currency"] = "USDT"
        h1.store.save(data)
        with caplog.at_level(logging.WARNING):
            h2.trader.start()
        assert h2.broker.cash == INITIAL_CASH
        assert h2.trader.positions == {}  # 새 모의계좌에는 포지션이 없으므로 동기화에서 제거
        assert any("복원하지 않습니다" in r.message for r in caplog.records)

    def test_restored_paper_broker_uses_current_fee_and_slippage(self, tmp_path, candles):
        h1, h2 = self._buy_then_restart(tmp_path, candles)
        data = json.loads(h1.store.path.read_text())
        data["paper_broker"]["fee_pct"] = 0.01
        data["paper_broker"]["slippage_pct"] = 0.02
        data["paper_broker"]["sim_time"] = h1.clock.now.isoformat()
        data["paper_broker"]["prices"] = {BTC: candles[0].close}
        h1.store.save(data)
        h2.trader.start()
        assert h2.broker.fee_pct == FEE and h2.broker.slippage_pct == SLIP
        assert h2.broker.to_dict()["sim_time"] is None and h2.broker.to_dict()["prices"] == {}
        assert (
            h2.broker.get_ticker(BTC) == candles[self.K + 1].close
        )  # mark 된 가격이 아니라 data_source 현재가

    def test_restart_after_day_change_sends_summary_for_saved_day(self, tmp_path, candles):
        k = next((i for i in range(len(candles) - 3) if candles[i + 1].timestamp.hour <= 20), None)
        if k is None:
            pytest.skip("같은 UTC 날짜에 두 폴링을 넣을 캔들이 없음")
        clock = FakeClock(after_candle(candles, k))
        strat = ScriptedStrategy()
        strat.at(BTC, candles[k].timestamp, SignalAction.BUY)
        strat.at(BTC, candles[k + 1].timestamp, SignalAction.SELL)
        h1 = build(tmp_path, {BTC: candles}, clock, strategy=strat)
        h1.trader.run_once()
        clock.advance(H)
        h1.trader.run_once()
        pnl = h1.trader.trades[-1].pnl
        day = clock.now.date()
        assert h1.trader.risk.daily_pnl == approx(pnl)

        clock.advance(D)
        h2 = build(tmp_path, {BTC: candles}, clock)
        h2.trader.start()
        summary = h2.notifier.find(f"[일일 요약] {day.isoformat()}")
        assert len(summary) == 1 and f"{pnl:+,.0f}" in summary[0] and "거래: 1건" in summary[0]
        assert h2.trader.risk.daily_pnl == 0.0 and h2.trader.risk.current_day == clock.now.date()


# ============================================================================ run_forever / stop / 시그널 / 백오프
class TestRunForever:
    K = 40

    def test_stop_from_sleep_ends_loop_and_saves_state(self, tmp_path, candles):
        clock = FakeClock(after_candle(candles, self.K))
        holder: dict[str, Trader] = {}
        sleeps: list[float] = []

        def sleeping(seconds: float) -> None:
            sleeps.append(seconds)
            clock.advance(seconds)
            if sum(sleeps) >= 3:
                holder["trader"].stop()

        h = build(tmp_path, {BTC: candles}, clock, sleep=sleeping)
        holder["trader"] = h.trader
        before = signal.getsignal(signal.SIGINT)
        h.trader.run_forever()
        assert not h.trader.running and h.trader.stopped
        assert h.trader.cycles >= 3
        assert signal.getsignal(signal.SIGINT) is before
        assert h.notifier.find("[종료]")
        data = json.loads(h.store.path.read_text())
        assert data["updated_at"] == clock.now.isoformat() and data["cycles"] == h.trader.cycles

    def test_sigint_sets_stop_flag(self, tmp_path, candles, caplog):
        clock = FakeClock(after_candle(candles, self.K))
        sleeps: list[float] = []

        def sleeping(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) == 2:
                signal.raise_signal(signal.SIGINT)

        h = build(tmp_path, {BTC: candles}, clock, sleep=sleeping)
        before = signal.getsignal(signal.SIGINT)
        with caplog.at_level(logging.WARNING):
            h.trader.run_forever()
        assert h.trader.stopped and len(sleeps) == 2
        assert signal.getsignal(signal.SIGINT) is before
        assert any("SIGINT" in r.message for r in caplog.records)

    def test_stop_from_another_thread(self, tmp_path, candles):
        clock = FakeClock(after_candle(candles, self.K))
        entered = threading.Event()
        release = threading.Event()

        def blocking_sleep(seconds: float) -> None:
            entered.set()
            release.wait(timeout=10)

        h = build(tmp_path, {BTC: candles}, clock, sleep=blocking_sleep)
        worker = threading.Thread(target=h.trader.run_forever, daemon=True)
        worker.start()
        assert entered.wait(10)
        h.trader.stop()
        release.set()
        worker.join(10)
        assert not worker.is_alive()
        assert h.trader.cycles == 1 and not h.trader.running

    def test_backoff_grows_and_caps(self, tmp_path, candles):
        clock = FakeClock(after_candle(candles, self.K))
        h = build(tmp_path, {BTC: candles}, clock, overrides={"engine": {"poll_seconds": 1.0}})
        tr = h.trader
        assert tr.next_delay() == 1.0
        tr.consecutive_failures = 1
        assert tr.next_delay() == 2.0
        tr.consecutive_failures = 8
        assert tr.next_delay() == 256.0
        tr.consecutive_failures = 9
        assert tr.next_delay() == MAX_BACKOFF_SEC
        tr.consecutive_failures = 40
        assert tr.next_delay() == MAX_BACKOFF_SEC
        tr.consecutive_failures = 0
        h2 = build(tmp_path, {BTC: candles}, clock, overrides={"engine": {"poll_seconds": 600.0}})
        h2.trader.consecutive_failures = 3
        assert h2.trader.next_delay() == 600.0  # 폴링 주기보다 짧아지지 않는다

    def test_consecutive_failures_backoff_and_recovery(self, tmp_path, candles, caplog):
        clock = FakeClock(after_candle(candles, self.K))
        holder: dict[str, Trader] = {}
        waits: list[float] = []

        def sleeping(seconds: float) -> None:
            tr = holder["trader"]
            if tr.cycles >= 4:
                tr.stop()
                return
            waits.append(seconds)
            clock.advance(seconds)

        h = build(
            tmp_path, {BTC: candles}, clock, sleep=sleeping, overrides={"engine": {"poll_seconds": 1.0}}
        )
        holder["trader"] = h.trader
        h.feed.fail_symbols = {BTC}
        with caplog.at_level(logging.INFO):
            h.trader.run_forever()
        assert h.trader.consecutive_failures == 4
        assert sum(waits) == approx(2 + 4 + 8)  # 1·2^1, 1·2^2, 1·2^3 (각 1초 조각으로 대기)
        assert all(w == 1.0 for w in waits)

        h.feed.fail_symbols = set()
        h.trader.run_once()
        # run_once 는 실패 카운터를 건드리지 않고 run_forever 가 관리한다 → 다시 루프로 확인
        holder["trader"] = h.trader
        h.trader._stop_event.clear()
        h.trader.consecutive_failures = 2
        stop_after = {"n": h.trader.cycles + 1}

        def sleeping2(seconds: float) -> None:
            if h.trader.cycles >= stop_after["n"]:
                h.trader.stop()

        h.trader._sleep = sleeping2
        with caplog.at_level(logging.INFO):
            h.trader.run_forever()
        assert h.trader.consecutive_failures == 0
        assert any("정상 동작 복귀" in r.message for r in caplog.records)

    def test_run_once_lazily_starts(self, tmp_path, candles):
        clock = FakeClock(after_candle(candles, self.K))
        h = build(tmp_path, {BTC: candles}, clock)
        h.trader.run_once()
        assert h.trader.started and h.notifier.find("[시작]")
        h.trader.start()  # 멱등
        assert len(h.notifier.find("[시작]")) == 1


# ============================================================================ 장 운영 / 마감 청산
class TestMarketHours:
    K = 40

    def _stock_harness(self, tmp_path, candles, strat=None, **overrides) -> Harness:
        clock = FakeClock(after_candle(candles, self.K))
        feed = StockFeed({BTC: candles}, clock, "1h")
        broker = PaperBroker(
            initial_cash=INITIAL_CASH,
            quote_currency="KRW",
            fee_pct=FEE,
            slippage_pct=SLIP,
            data_source=feed,
            asset_class=AssetClass.STOCK,
        )
        return build(
            tmp_path, {BTC: candles}, clock, strategy=strat, broker=broker, feed=feed, overrides=overrides
        )

    def test_market_closed_skips_symbol(self, tmp_path, candles, caplog):
        h = self._stock_harness(tmp_path, candles)
        assert isinstance(h.feed, StockFeed)
        h.feed.market_open = False
        h.trader.run_once()
        assert h.feed.calls["get_candles"] == 0 and h.strategy.calls == []
        h.feed.market_open = True
        with caplog.at_level(logging.INFO):
            h.trader.run_once()
        assert h.strategy.calls == [(BTC, candles[self.K].timestamp)]
        assert any("장 상태 변경: 개장" in r.message for r in caplog.records)

    def test_close_positions_at_market_close(self, tmp_path, candles):
        strat = ScriptedStrategy()
        strat.at(BTC, candles[self.K].timestamp, SignalAction.BUY)
        strat.at(BTC, candles[self.K + 1].timestamp, SignalAction.BUY)  # 마감 직전 신규 진입은 막혀야 한다
        h = self._stock_harness(
            tmp_path,
            candles,
            strat,
            engine={"close_positions_at_market_close": True, "poll_seconds": 30},
        )
        h.trader.run_once()
        pos = h.trader.positions[BTC]
        h.clock.advance(H)
        assert isinstance(h.feed, StockFeed)
        h.feed.next_market_close = h.clock.now + timedelta(seconds=10)
        h.trader.run_once()
        assert h.trader.positions == {}
        t = h.trader.trades[-1]
        assert t.reason == "장 마감 전 전량 청산" and pos.meta["exit_type"] == EXIT_MARKET_CLOSE
        assert len(h.broker.orders) == 2  # 매수 1 + 청산 매도 1 (신규 매수 없음)

    def test_close_not_triggered_when_close_is_far(self, tmp_path, candles):
        strat = ScriptedStrategy()
        strat.at(BTC, candles[self.K].timestamp, SignalAction.BUY)
        h = self._stock_harness(tmp_path, candles, strat, engine={"close_positions_at_market_close": True})
        assert isinstance(h.feed, StockFeed)
        h.feed.next_market_close = h.clock.now + timedelta(hours=3)
        h.trader.run_once()
        assert BTC in h.trader.positions

    def test_closing_soon_from_exchange_clocks(self, tmp_path, candles):
        h = self._stock_harness(
            tmp_path, candles, engine={"close_positions_at_market_close": True, "poll_seconds": 30}
        )
        tr = h.trader
        krx_last = datetime(2026, 10, 8, 6, 29, 50, tzinfo=timezone.utc)  # 목요일 15:29:50 KST
        assert tr._closing_soon(krx_last) is True
        assert tr._closing_soon(datetime(2026, 10, 8, 5, 0, tzinfo=timezone.utc)) is False  # 14:00 KST
        nyse_last = datetime(2026, 10, 8, 19, 59, 50, tzinfo=timezone.utc)  # 15:59:50 EDT
        assert tr._closing_soon(nyse_last) is True
        assert tr._closing_soon(datetime(2026, 10, 10, 6, 29, 50, tzinfo=timezone.utc)) is False  # 토요일

        crypto = build(
            tmp_path, {BTC: candles}, h.clock, overrides={"engine": {"close_positions_at_market_close": True}}
        )
        assert crypto.trader._closing_soon(krx_last) is False
        off = self._stock_harness(tmp_path, candles)  # 옵션 꺼짐
        assert off.trader._closing_soon(krx_last) is False


# ============================================================================ 일일 요약 / 상태 조회 / 기타
class TestSummaryStatusMisc:
    K = 40

    def test_daily_summary_once_per_utc_day(self, tmp_path, candles):
        k = next((i for i in range(len(candles) - 2) if candles[i + 1].timestamp.hour == 23), None)
        if k is None:
            pytest.skip("23시 캔들이 없음")
        clock = FakeClock(after_candle(candles, k))
        h = build(tmp_path, {BTC: candles}, clock)
        h.trader.run_once()
        day = clock.now.date()
        assert h.notifier.find("[일일 요약]") == []
        clock.advance(H)  # 다음 날 00:00:05 UTC
        h.trader.run_once()
        summary = h.notifier.find("[일일 요약]")
        assert len(summary) == 1 and day.isoformat() in summary[0] and "거래: 0건" in summary[0]
        assert h.trader.risk.day_start_equity == approx(INITIAL_CASH)
        assert h.trader.risk.current_day == clock.now.date()
        clock.advance(H)
        h.trader.run_once()
        assert len(h.notifier.find("[일일 요약]")) == 1

    def test_daily_summary_disabled(self, tmp_path, candles, caplog):
        k = next((i for i in range(len(candles) - 2) if candles[i + 1].timestamp.hour == 23), None)
        if k is None:
            pytest.skip("23시 캔들이 없음")
        clock = FakeClock(after_candle(candles, k))
        h = build(tmp_path, {BTC: candles}, clock, overrides={"notify": {"daily_summary": False}})
        h.trader.run_once()
        clock.advance(H)
        with caplog.at_level(logging.INFO):
            h.trader.run_once()
        assert h.notifier.find("[일일 요약]") == []
        assert any("[일일 요약]" in r.message for r in caplog.records)

    def test_status_summary(self, tmp_path, candles):
        clock = FakeClock(after_candle(candles, self.K))
        strat = ScriptedStrategy()
        strat.at(BTC, candles[self.K].timestamp, SignalAction.BUY)
        h = build(tmp_path, {BTC: candles}, clock, strategy=strat)
        h.trader.run_once()
        st = h.trader.status()
        json.dumps(st)  # JSON 직렬화 가능
        assert st["mode"] == "paper" and st["live"] is False
        assert st["broker"] == "paper" and st["data_source"] == "feed"
        assert st["strategy"] == "scripted" and st["symbols"] == [BTC] and st["interval"] == "1h"
        assert st["quote_currency"] == "KRW"
        assert st["started"] and not st["running"] and st["cycles"] == 1
        assert st["equity"] == approx(h.broker.get_equity())
        assert st["cash"] == approx(h.broker.cash)
        pos = st["positions"][BTC]
        price = candles[self.K + 1].close
        assert pos["quantity"] == approx(expected_qty(price))
        assert pos["last_price"] == price
        assert pos["unrealized_pnl"] == approx((price - pos["average_price"]) * pos["quantity"])
        assert st["pending_breakouts"] == {}
        assert st["last_candle_ts"] == {BTC: candles[self.K].timestamp.isoformat()}
        assert (
            st["daily_pnl"] == 0.0
            and st["daily_trades"] == 0
            and st["trades"] == 0
            and st["last_trade"] is None
        )
        assert st["day_start_equity"] == approx(INITIAL_CASH)

    def test_status_tolerates_broker_failures(self, tmp_path, candles):
        class Broken(PaperBroker):
            def get_equity(self, symbols=None):
                raise BrokerError("equity down")

            def get_balances(self):
                raise BrokerError("balances down")

        clock = FakeClock(after_candle(candles, self.K))
        feed = RealCandleFeed({BTC: candles}, clock, "1h")
        broker = Broken(initial_cash=INITIAL_CASH, data_source=feed)
        h = build(tmp_path, {BTC: candles}, clock, broker=broker, feed=feed)
        h.trader.start()
        st = h.trader.status()
        assert st["equity"] is None and st["cash"] is None
        assert h.trader.risk.day_start_equity is None

    def test_live_mode_banner(self, tmp_path, candles, caplog):
        clock = FakeClock(after_candle(candles, self.K))
        h = build(tmp_path, {BTC: candles}, clock, overrides={"mode": "live", "broker": {"name": "upbit"}})
        with caplog.at_level(logging.WARNING):
            h.trader.start()
        assert any("실거래(LIVE)" in r.message for r in caplog.records)
        start = h.notifier.find("[시작]")
        assert len(start) == 1 and "실거래 (LIVE)" in start[0]

    def test_startup_message_contents(self, tmp_path, candles):
        clock = FakeClock(after_candle(candles, self.K))
        h = build(tmp_path, {BTC: candles}, clock)
        h.trader.start()
        msg = h.notifier.find("[시작]")[0]
        assert "모의투자 (PAPER)" in msg and "paper (시세: feed)" in msg
        assert "심볼: KRW-BTC" in msg and "전략: scripted" in msg and "간격: 1h" in msg
        assert f"자산: {INITIAL_CASH:,.0f} KRW" in msg and "손절 3%" in msg

    def test_unsupported_interval_raises(self, tmp_path, candles):
        clock = FakeClock(after_candle(candles, self.K))
        h = build(tmp_path, {BTC: candles}, clock, overrides={"interval": "5m"})
        with pytest.raises(ConfigError, match="지원하지 않습니다"):
            h.trader.start()

    def test_candle_limit_below_warmup_raises(self, tmp_path, candles):
        clock = FakeClock(after_candle(candles, self.K))
        h = build(tmp_path, {BTC: candles}, clock, strategy=ScriptedStrategy(warmup=100))
        with pytest.raises(ConfigError, match="warmup"):
            h.trader.run_once()

    def test_stale_data_warning_once(self, tmp_path, candles):
        clock = FakeClock(candles[-1].timestamp + timedelta(days=3))
        h = build(tmp_path, {BTC: candles}, clock)
        h.trader.run_once()
        h.trader.run_once()
        warnings = h.notifier.find("[경고]")
        assert len(warnings) == 1 and "오래되었습니다" in warnings[0]
        assert h.trader.cycle_errors == 0

    def test_fee_pct_source(self, tmp_path, candles):
        clock = FakeClock(after_candle(candles, self.K))
        h = build(tmp_path, {BTC: candles}, clock, overrides={"paper": {"fee_pct": 0.002}})
        assert h.trader._fee_pct == 0.002  # PaperBroker 실제 수수료율
        raw = Trader(h.config, h.feed, ScriptedStrategy(), RiskManager(h.config.risk), clock=clock)
        assert raw._fee_pct == 0.002  # 실거래 브로커는 설정값을 가정치로 사용
        assert raw.notifier.name == "null" and raw.state.path == Path(h.config.engine.state_file)

    def test_pending_breakout_dict_round_trip_and_validation(self, candles):
        now = candles[-1].timestamp
        sig = Signal(
            SignalAction.BUY,
            BTC,
            order_type=OrderType.STOP,
            stop_offset=12.5,
            max_holding_bars=1,
            meta={"k": 0.5},
        )
        pb = PendingBreakout(
            BTC,
            candles[-1].high,
            sig,
            expires=now + timedelta(hours=1),
            created_at=now,
            candle_ts=candles[-2].timestamp,
            base_open=candles[-1].open,
        )
        d = json.loads(json.dumps(pb.to_dict()))
        back = PendingBreakout.from_dict(d)
        assert (back.symbol, back.trigger, back.expires, back.created_at, back.candle_ts, back.base_open) == (
            BTC,
            approx(candles[-1].high),
            pb.expires,
            now,
            candles[-2].timestamp,
            approx(candles[-1].open),
        )
        assert back.signal.stop_offset == 12.5 and back.signal.meta == {"k": 0.5}
        minimal = PendingBreakout.from_dict(
            {
                "symbol": BTC,
                "trigger": 1.0,
                "signal": {"action": "buy", "symbol": BTC},
                "expires": now.isoformat(),
            }
        )
        assert minimal.created_at == now and minimal.candle_ts is None and minimal.base_open is None
        for bad in (
            None,
            [],
            {"symbol": BTC},
            {**d, "trigger": "x"},
            {**d, "trigger": 0},
            {**d, "expires": None},
            {**d, "signal": {}},
        ):
            with pytest.raises(DataError):
                PendingBreakout.from_dict(bad)

    def test_repr(self, tmp_path, candles):
        clock = FakeClock(after_candle(candles, self.K))
        h = build(tmp_path, {BTC: candles}, clock)
        assert "Trader" in repr(h.trader) and "scripted" in repr(h.trader)
