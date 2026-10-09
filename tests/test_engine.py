"""Trader(실시간 매매 엔진) 테스트.

시세는 전부 tests/conftest.py 가 Upbit 공개 API 에서 받아 캐시한 **실제** KRW-BTC(1h, 1d) / KRW-ETH(1d) 캔들이다
(네트워크가 없으면 skip). ``RealCandleFeed`` 는 주입된 가짜 시계 기준으로 "그 시각까지 완성된" 실제 캔들만
서빙하고, 현재가는 진행중 실제 캔들의 open/high/low/close 중 테스트가 고른 값을 돌려준다.
전략은 신호 시점만 지정하는 ``ScriptedStrategy`` (시세가 아니라 테스트 로직) 또는 실제 변동성 돌파 전략이다.
가짜/데모 가격은 없다 — 계좌 설정값(초기 현금, 수수료율)만 상수다.
"""

from __future__ import annotations

import copy
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
    InflightEntry,
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
    Trade,
    ensure_utc,
    interval_to_seconds,
)
from tradingbot.notify.base import Notifier
from tradingbot.risk import RiskManager
from tradingbot.strategies.base import BaseStrategy, df_to_candles
from tradingbot.strategies.volatility_breakout import VolatilityBreakoutStrategy
from tradingbot.utils.timeutil import floor_to_interval, is_krx_open

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


class ExchangeLike(BaseBroker):
    """실거래 어댑터처럼 보이는 래퍼. 시세/주문/계좌는 안의 PaperBroker 에 위임하되,

    - PaperBroker 가 **아니므로** 엔진이 상태 파일의 모의계좌로 브로커를 교체/복원하지 않는다 (체결이 "거래소" 에 남는다)
    - ``get_positions`` 는 Upbit 처럼 수량/평균단가만 돌려준다 (손절/메타 없음)
    """

    asset_class = AssetClass.CRYPTO
    supported_intervals = ("1h", "1d")

    def __init__(self, inner: PaperBroker, name: str = "exchange") -> None:
        self.inner = inner
        self.name = name
        self.fail_positions = False
        self.order_calls: list[tuple[str, OrderSide, float]] = []

    def get_candles(self, symbol, interval, limit=200, end=None, include_partial=False):
        return self.inner.get_candles(symbol, interval, limit, end, include_partial)

    def get_ticker(self, symbol):
        return self.inner.get_ticker(symbol)

    def get_balances(self):
        return self.inner.get_balances()

    def get_positions(self):
        if self.fail_positions:
            raise BrokerError("계좌 조회 실패 (테스트)")
        return {s: Position(s, p.quantity, p.average_price) for s, p in self.inner.get_positions().items()}

    def place_order(self, symbol, side, quantity, order_type=OrderType.MARKET, price=None):
        self.order_calls.append((symbol, side, quantity))
        return self.inner.place_order(symbol, side, quantity, order_type, price)

    def cancel_order(self, order_id, symbol=None):
        return self.inner.cancel_order(order_id, symbol)

    def get_order(self, order_id, symbol=None):
        return self.inner.get_order(order_id, symbol)

    def get_open_orders(self, symbol=None):
        return self.inner.get_open_orders(symbol)

    def quote_currency(self, symbol):
        return self.inner.quote_currency(symbol)

    def base_currency(self, symbol):
        return self.inner.base_currency(symbol)

    def min_order_value(self, symbol):
        return self.inner.min_order_value(symbol)

    def get_equity(self, symbols=None):
        return self.inner.get_equity(symbols)


class LostAckBroker(PaperBroker):
    """매수 주문을 체결시킨 뒤 응답이 유실된 것처럼 BrokerError 를 던진다 (POST 읽기 타임아웃 재현).

    ``fail_next`` 회 동안만 실패하고 이후는 정상. 매도는 항상 정상.
    """

    def __init__(self, *args: Any, fail_next: int = 1, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.fail_next = fail_next
        self.buy_calls = 0
        self.fail_positions = False

    def place_order(self, symbol, side, quantity, order_type=OrderType.MARKET, price=None, **kw):
        order = super().place_order(symbol, side, quantity, order_type, price, **kw)
        if side == OrderSide.BUY:
            self.buy_calls += 1
            if self.fail_next > 0:
                self.fail_next -= 1
                raise BrokerError("POST https://api.upbit.com/v1/orders 네트워크 오류: Read timed out")
        return order

    def get_positions(self):
        if self.fail_positions:
            raise BrokerError("계좌 조회 실패 (테스트)")
        return super().get_positions()


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

    def test_max_holding_exit_retried_each_poll_when_sell_unfilled(self, tmp_path, daily_candles):
        """만료 매도가 미체결(타임아웃 취소)로 끝나면 다음 캔들이 아니라 **다음 폴링**에 다시 시도한다."""

        class StuckSellOnceBroker(PaperBroker):
            """첫 매도 주문만 OPEN 으로 남아 타임아웃 취소되고(미체결), 이후 매도는 정상 체결."""

            def __init__(self, *args: Any, **kw: Any) -> None:
                super().__init__(*args, **kw)
                self.sell_calls = 0
                self.stuck: dict[str, Order] = {}

            def place_order(self, symbol, side, quantity, order_type=OrderType.MARKET, price=None, **kw):
                if side == OrderSide.SELL:
                    self.sell_calls += 1
                    if self.sell_calls == 1:
                        o = Order(
                            id="stuck-sell",
                            symbol=symbol,
                            side=side,
                            type=order_type,
                            quantity=quantity,
                            status=OrderStatus.OPEN,
                            created_at=self.clock(),
                        )
                        self.stuck[o.id] = o
                        return dataclasses.replace(o)
                return super().place_order(symbol, side, quantity, order_type, price, **kw)

            def get_order(self, order_id, symbol=None):
                if order_id in self.stuck:
                    return dataclasses.replace(self.stuck[order_id])
                return super().get_order(order_id, symbol)

            def cancel_order(self, order_id, symbol=None):
                if order_id in self.stuck:
                    self.stuck[order_id].status = OrderStatus.CANCELED
                    return True
                return super().cancel_order(order_id, symbol)

        hits, _ = breakout_days(daily_candles)
        if not hits:
            pytest.skip("실제 데이터에 돌파 일이 없음")
        i = hits[0]
        clock = FakeClock(after_candle(daily_candles, i - 1, D))
        feed = RealCandleFeed({BTC: daily_candles}, clock, "1d")
        feed.ticker_field = "open"
        broker = StuckSellOnceBroker(
            initial_cash=INITIAL_CASH, fee_pct=FEE, slippage_pct=SLIP, data_source=feed
        )
        h = build(
            tmp_path,
            {BTC: daily_candles},
            clock,
            interval="1d",
            strategy=VolatilityBreakoutStrategy(k=0.5),
            broker=broker,
            feed=feed,
            overrides={"risk": {"stop_loss_pct": None}},  # 변동성 돌파의 청산은 max_holding_bars 뿐
        )
        h.trader.run_once()
        feed.ticker_field = "high"
        clock.advance(H)
        h.trader.run_once()
        qty = h.trader.positions[BTC].quantity

        feed.ticker_field = "open"
        clock.set(after_candle(daily_candles, i, D))
        h.trader.run_once()  # 만료 → 매도 시도 1 → 미체결(취소) → 포지션 유지, 캔들은 소비됨
        assert broker.sell_calls == 1 and BTC in h.trader.positions
        assert h.notifier.find("매도 미체결")
        assert h.trader.last_candle_ts[BTC] == daily_candles[i].timestamp
        assert h.trader.trades == []

        clock.advance(600)  # 같은 날 다음 폴링 — 새 캔들이 없어도 만료 청산을 다시 시도한다
        h.trader.run_once()
        assert broker.sell_calls == 2 and BTC not in h.trader.positions
        t = h.trader.trades[-1]
        assert "최대 보유 기간 만료 (1/1봉)" in t.reason and t.quantity == approx(qty)
        assert t.exit_price == approx(daily_candles[i + 1].open * (1 - SLIP))


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

    def test_adopting_broker_position_drops_restored_pending_breakout(self, tmp_path, daily_candles):
        """거래소가 이미 보유 중인 심볼의 저장된 돌파 대기 주문은 복원 시 제거한다 (보유 중 재매수 방지)."""
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
        assert BTC in h1.trader.pending_breakouts
        h1.broker.place_order(BTC, OrderSide.BUY, 0.01)  # 봇 밖에서 매수 — 상태 파일에는 대기 주문만 남는다
        clock.advance(1)
        h1.trader.run_once()
        assert json.loads(h1.store.path.read_text())["pending_breakouts"]

        h2 = build(
            tmp_path, {BTC: daily_candles}, clock, interval="1d", strategy=VolatilityBreakoutStrategy()
        )
        h2.trader.start()
        assert h2.trader.positions[BTC].meta["adopted"] is True
        assert h2.trader.pending_breakouts == {}
        assert not any("돌파 대기" in m for m in h2.notifier.find("[시작]"))


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


# ============================================================================ 주문 안전 장치 (전송 후 오류/크래시)
class TestOrderSafety:
    """주문 전송 후 오류/크래시: 같은 캔들 재주문 금지, 결과 미확인 주문의 거래소 기준 확정, 체결 즉시 저장."""

    K = 40

    def _lost_ack(self, tmp_path, candles, broker_cls=LostAckBroker, **kw) -> tuple[Harness, LostAckBroker]:
        clock = FakeClock(after_candle(candles, self.K))
        feed = RealCandleFeed({BTC: candles}, clock, "1h")
        broker = broker_cls(initial_cash=INITIAL_CASH, fee_pct=FEE, slippage_pct=SLIP, data_source=feed, **kw)
        strat = ScriptedStrategy()
        strat.at(BTC, candles[self.K].timestamp, SignalAction.BUY, reason="유실 테스트")
        h = build(tmp_path, {BTC: candles}, clock, strategy=strat, broker=broker, feed=feed)
        return h, broker

    def test_lost_ack_buy_is_adopted_and_not_resent(self, tmp_path, candles):
        """매수 POST 응답 유실: 체결분을 원래 신호로 채택하고, 같은 캔들을 다음 폴링에 재주문하지 않는다."""
        h, broker = self._lost_ack(tmp_path, candles)
        h.trader.run_once()
        assert h.trader.cycle_errors == 1 and h.notifier.find("[오류] KRW-BTC")
        assert broker.buy_calls == 1
        exchange = broker.get_positions()[BTC]
        pos = h.trader.positions[BTC]
        assert pos.quantity == approx(exchange.quantity)
        assert pos.average_price == approx(exchange.average_price)
        assert pos.meta["adopted"] is True and pos.meta["entry_reason"] == "유실 테스트"
        assert pos.meta["entry_bar_ts"] == candles[self.K].timestamp.isoformat()
        assert pos.stop_loss == approx(exchange.average_price * (1 - STOP_LOSS_PCT))
        assert h.trader.last_candle_ts[BTC] == candles[self.K].timestamp  # 전송 시점에 캔들 소비
        assert h.trader.inflight_entries == {}
        assert any("체결분 채택" in m for m in h.notifier.find("[동기화]"))
        data = json.loads(h.store.path.read_text())
        assert data["inflight_entries"] == {} and BTC in data["positions"]

        h.clock.advance(30)
        h.trader.run_once()  # 같은 캔들 → 재평가/재주문 없음
        assert broker.buy_calls == 1 and len(broker.orders) == 1
        assert len(h.strategy.calls) == 1
        assert h.trader.positions[BTC].quantity == approx(exchange.quantity)
        assert h.trader.cycle_errors == 0

    def test_error_while_awaiting_fill_cancels_unknown_order_and_does_not_resend(self, tmp_path, candles):
        """주문은 접수(OPEN)됐는데 get_order 파싱이 BrokerError 가 아닌 예외로 터진 경우."""

        class BrokenGetOrder(StuckBroker):
            def get_order(self, order_id, symbol=None):
                self.get_order_calls += 1
                raise KeyError("trades")

            def get_open_orders(self, symbol=None):
                return [
                    dataclasses.replace(o)
                    for o in self.stuck.values()
                    if o.status == OrderStatus.OPEN and (symbol is None or o.symbol == symbol)
                ]

        clock = FakeClock(after_candle(candles, self.K))
        feed = RealCandleFeed({BTC: candles}, clock, "1h")
        broker = BrokenGetOrder(initial_cash=INITIAL_CASH, fee_pct=FEE, slippage_pct=SLIP, data_source=feed)
        strat = ScriptedStrategy()
        strat.at(BTC, candles[self.K].timestamp, SignalAction.BUY)
        h = build(tmp_path, {BTC: candles}, clock, strategy=strat, broker=broker, feed=feed)
        h.trader.run_once()
        errs = h.notifier.find("[오류] KRW-BTC")
        assert h.trader.cycle_errors == 1 and errs and "KeyError" in errs[0]
        assert broker.cancel_calls == ["stuck-1"]  # 결과 미확인 주문은 취소
        assert broker.stuck["stuck-1"].status == OrderStatus.CANCELED
        assert h.trader.positions == {} and h.trader.inflight_entries == {}
        assert any("취소" in m for m in h.notifier.find("[동기화]"))
        assert h.trader.last_candle_ts[BTC] == candles[self.K].timestamp

        clock.advance(30)
        h.trader.run_once()
        assert len(broker.stuck) == 1 and len(h.strategy.calls) == 1  # 재주문 없음

    def test_unresolved_inflight_blocks_entries_until_reconciled(self, tmp_path, candles):
        """확정(계좌 조회)도 실패하면 기록을 남겨 재시도하고, 그동안 이 심볼의 신규 매수는 보류한다."""
        h, broker = self._lost_ack(tmp_path, candles)
        h.strategy.at(BTC, candles[self.K + 1].timestamp, SignalAction.BUY)  # 다음 캔들에도 매수 신호
        h.trader.start()
        broker.fail_positions = True
        h.trader.run_once()
        assert broker.buy_calls == 1 and h.trader.positions == {}
        ie = h.trader.inflight_entries[BTC]
        assert ie.quantity == approx(expected_qty(candles[self.K + 1].close))
        assert ie.bar_ts == candles[self.K].timestamp and ie.created_at == h.clock.now
        data = json.loads(h.store.path.read_text())
        assert data["inflight_entries"][BTC]["signal"]["reason"] == "유실 테스트"
        assert h.trader.status()["inflight_entries"][BTC]["quantity"] == approx(ie.quantity)

        h.clock.advance(H)  # 새 캔들 + BUY 신호 → 결과 미확인 주문이 있어 보류
        h.trader.run_once()
        assert broker.buy_calls == 1 and BTC in h.trader.inflight_entries
        assert h.trader.last_candle_ts[BTC] == candles[self.K + 1].timestamp

        broker.fail_positions = False
        h.clock.advance(30)
        h.trader.run_once()  # 확정: 체결분 채택
        assert BTC not in h.trader.inflight_entries
        assert h.trader.positions[BTC].quantity == approx(broker.get_positions()[BTC].quantity)
        assert h.trader.positions[BTC].meta["entry_bar_ts"] == candles[self.K].timestamp.isoformat()
        assert broker.buy_calls == 1

    def test_crash_after_order_is_reconciled_on_restart_with_breakout_semantics(
        self, tmp_path, daily_candles
    ):
        """체결 직후 프로세스가 죽어도(상태 저장 전) 재시작 시 체결분을 원래 신호(최대 보유 1봉)로 채택하고,
        저장돼 있던 돌파 대기 주문으로 다시 매수하지 않으며, 다음 날 시가에 만료 청산한다."""
        hits, _ = breakout_days(daily_candles)
        if not hits:
            pytest.skip("실제 데이터에 돌파 일이 없음")
        i = hits[0]
        clock = FakeClock(after_candle(daily_candles, i - 1, D))
        feed = RealCandleFeed({BTC: daily_candles}, clock, "1d")
        feed.ticker_field = "open"
        inner = PaperBroker(initial_cash=INITIAL_CASH, fee_pct=FEE, slippage_pct=SLIP, data_source=feed)
        inner.clock = clock

        class KilledAfterFill(ExchangeLike):
            def place_order(self, *a, **kw):
                super().place_order(*a, **kw)
                raise KeyboardInterrupt  # SIGKILL 흉내: 체결 직후, 상태 저장 전

        h1 = build(
            tmp_path,
            {BTC: daily_candles},
            clock,
            interval="1d",
            strategy=VolatilityBreakoutStrategy(k=0.5),
            broker=KilledAfterFill(inner),
            feed=feed,
        )
        h1.trader.run_once()  # 돌파 대기 등록
        assert BTC in h1.trader.pending_breakouts
        feed.ticker_field = "high"
        clock.advance(H)
        with pytest.raises(KeyboardInterrupt):
            h1.trader.run_once()
        exchange_qty = inner.get_positions()[BTC].quantity
        assert exchange_qty > 0  # 거래소에는 체결됨
        data = json.loads(h1.store.path.read_text())  # 주문 직전 write-ahead 상태
        assert data["positions"] == {} and data["pending_breakouts"] == {}
        assert data["inflight_entries"][BTC]["signal"]["max_holding_bars"] == 1
        assert data["inflight_entries"][BTC]["bar_ts"] == daily_candles[i - 1].timestamp.isoformat()

        exchange = ExchangeLike(inner)
        h2 = build(
            tmp_path,
            {BTC: daily_candles},
            clock,
            interval="1d",
            strategy=VolatilityBreakoutStrategy(k=0.5),
            broker=exchange,
            feed=feed,
        )
        h2.trader.start()
        pos = h2.trader.positions[BTC]
        assert pos.quantity == approx(exchange_qty)
        assert pos.meta["adopted"] is True and pos.meta["max_holding_bars"] == 1
        assert pos.meta["entry_bar_ts"] == daily_candles[i - 1].timestamp.isoformat()
        assert "변동성 돌파" in pos.meta["entry_reason"]
        assert h2.trader.inflight_entries == {} and h2.trader.pending_breakouts == {}
        assert exchange.order_calls == []  # 재매수 없음
        clock.advance(600)
        h2.trader.run_once()
        assert exchange.order_calls == [] and BTC in h2.trader.positions  # 같은 날: 유지

        feed.ticker_field = "open"  # 다음 날 첫 폴링: 시가에 만료 청산 (계약 §2)
        clock.set(after_candle(daily_candles, i, D))
        h2.trader.run_once()
        assert BTC not in h2.trader.positions
        assert [c[1] for c in exchange.order_calls] == [OrderSide.SELL]
        t = h2.trader.trades[-1]
        assert "최대 보유 기간 만료 (1/1봉)" in t.reason and t.quantity == approx(exchange_qty)
        assert t.exit_price == approx(daily_candles[i + 1].open * (1 - SLIP))

    def test_state_is_saved_before_order_and_right_after_fill_and_exit(self, tmp_path, candles, monkeypatch):
        clock = FakeClock(after_candle(candles, self.K))
        strat = ScriptedStrategy()
        strat.at(BTC, candles[self.K].timestamp, SignalAction.BUY)
        strat.at(BTC, candles[self.K + 1].timestamp, SignalAction.SELL)
        h = build(tmp_path, {BTC: candles}, clock, strategy=strat)
        snapshots: list[dict[str, Any]] = []
        original = h.store.save

        def recording_save(data):
            snapshots.append(copy.deepcopy(dict(data)))
            original(data)

        monkeypatch.setattr(h.store, "save", recording_save)
        h.trader.run_once()
        # 시작 → 주문 직전(write-ahead: 캔들 소비 + inflight) → 체결 직후 → 사이클 끝
        kinds = [(bool(s["inflight_entries"]), bool(s["positions"])) for s in snapshots]
        assert kinds == [(False, False), (True, False), (False, True), (False, True)]
        pre = snapshots[1]
        assert pre["last_candle_ts"][BTC] == candles[self.K].timestamp.isoformat()
        assert pre["inflight_entries"][BTC]["quantity"] == approx(h.trader.positions[BTC].quantity)
        assert pre["inflight_entries"][BTC]["order_type"] == "market"

        n = len(snapshots)
        clock.advance(H)
        h.trader.run_once()
        assert len(snapshots) == n + 2  # 청산 직후 + 사이클 끝
        assert snapshots[n]["positions"] == {} and len(snapshots[n]["trades"]) == 1


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

    def test_paper_state_is_not_restored_into_live_run(self, tmp_path, candles):
        """같은 설정/상태 파일로 모의투자 → 실거래 전환: 모의 손익/거래/시작 자산을 실거래에 이어 쓰지 않는다."""
        clock = FakeClock(after_candle(candles, self.K))
        strat = ScriptedStrategy()
        strat.at(BTC, candles[self.K].timestamp, SignalAction.BUY)
        strat.at(BTC, candles[self.K + 1].timestamp, SignalAction.SELL)
        paper = build(tmp_path, {BTC: candles}, clock, strategy=strat)
        paper.trader.run_once()
        clock.advance(H)
        paper.trader.run_once()
        assert len(paper.trader.trades) == 1 and paper.trader.risk.day_start_equity == approx(INITIAL_CASH)
        saved = json.loads(paper.store.path.read_text())
        assert saved["mode"] == "paper" and saved["broker"] == "paper"

        live_cash = 1_000_000.0  # 소액으로 시작하는 실거래 계좌
        feed = RealCandleFeed({BTC: candles}, clock, "1h")
        inner = PaperBroker(initial_cash=live_cash, fee_pct=FEE, slippage_pct=SLIP, data_source=feed)
        inner.clock = clock
        exchange = ExchangeLike(inner, name="upbit")
        live = build(tmp_path, {BTC: candles}, clock, broker=exchange, feed=feed, overrides={"mode": "live"})
        live.trader.start()
        assert live.trader.risk.day_start_equity == approx(live_cash)  # 모의 1천만원이 아니라 실계좌 자산
        assert live.trader.risk.daily_pnl == 0.0 and live.trader.risk.daily_trades == 0
        assert live.trader.trades == [] and live.trader.last_candle_ts == {} and live.trader.positions == {}
        warn = live.notifier.find("[경고]")
        assert warn and "복원하지 않습니다" in warn[0]
        backups = [p.name for p in tmp_path.iterdir() if p.name.startswith("state.json.paper-paper-")]
        assert len(backups) == 1
        assert json.loads((tmp_path / backups[0]).read_text())["mode"] == "paper"  # 모의 상태는 보관

        # 실계좌 기준 -6% 손실이면 일일 손실 한도(-5%) 가 작동한다 (모의 자산 기준이면 -0.6% 로 통과했을 것)
        entry, exit_ = next(
            (a, b) for a in candles for b in candles if b.timestamp > a.timestamp and b.close < a.close
        )
        qty = 0.06 * live_cash / (entry.close - exit_.close)
        trade = Trade(BTC, OrderSide.BUY, qty, entry.close, exit_.close, entry.timestamp, exit_.timestamp)
        live.trader.risk.record_trade(trade, now=clock.now)
        ok, why = live.trader.risk.can_open(0, clock.now)
        assert ok is False and "일일 손실 한도" in why

        live.trader.run_once()
        data = json.loads(live.store.path.read_text())
        assert data["mode"] == "live" and data["broker"] == "upbit"
        assert data["risk"]["day_start_equity"] == approx(live_cash)

    def test_same_mode_and_broker_state_is_restored(self, tmp_path, candles):
        h1, h2 = self._buy_then_restart(tmp_path, candles)
        h2.trader.start()
        assert BTC in h2.trader.positions and h2.notifier.find("[경고]") == []
        assert [p.name for p in tmp_path.iterdir() if p.name.startswith("state.json.")] == []


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

    def test_closing_window_accounts_for_cycle_duration(self, tmp_path, candles):
        """사이클이 3초 걸리면 다음 사이클은 33초 뒤 → 마감 32초 전 폴링이 '마지막 폴링' 이어야 한다."""
        h = self._stock_harness(
            tmp_path, candles, engine={"close_positions_at_market_close": True, "poll_seconds": 30}
        )
        tr = h.trader
        close = datetime(2026, 10, 8, 6, 30, tzinfo=timezone.utc)  # 목요일 15:30 KST
        tr._last_cycle_seconds = 0.0
        assert tr._closing_soon(close - timedelta(seconds=32)) is False
        tr._last_cycle_seconds = 3.0
        assert tr._closing_soon(close - timedelta(seconds=32)) is True
        assert tr._closing_soon(close - timedelta(seconds=34)) is False
        assert isinstance(h.feed, StockFeed)
        h.feed.next_market_close = close  # 거래소 시계(next_market_close) 경로도 동일
        assert tr._closing_soon(close - timedelta(seconds=32)) is True
        tr._last_cycle_seconds = 0.0
        assert tr._closing_soon(close - timedelta(seconds=32)) is False

    def test_close_out_not_skipped_by_cycle_latency(self, tmp_path, candles):
        """run_forever: 시세 호출마다 시간이 흘러 폴링 간격이 poll 보다 길어져도, 어느 위상에서 시작하든
        마감 직전 청산 폴링을 건너뛰지 않는다 (예전에는 위상에 따라 포지션이 다음 장까지 남았다)."""
        k = next(
            (
                i
                for i, c in enumerate(candles)
                if c.timestamp.hour == 5 and c.timestamp.weekday() < 5 and i + 2 < len(candles)
            ),
            None,
        )
        if k is None:
            pytest.skip("평일 05:00 UTC(14:00 KST) 시간봉이 없음")
        close = candles[k].timestamp.replace(hour=6, minute=30)  # 그날 15:30 KST

        class LatencyFeed(StockFeed):
            latency = 1.5  # 시세 호출 1건당 소요 시간(초) → 사이클당 ~3초

            def get_candles(self, *a, **kw):
                self._clock.advance(self.latency)
                return super().get_candles(*a, **kw)

            def get_ticker(self, symbol):
                self._clock.advance(self.latency)
                return super().get_ticker(symbol)

            def is_market_open(self):
                return is_krx_open(self._clock())  # KISBroker.is_market_open 과 동일

        results = []
        for phase in range(0, 33):  # 사이클 시작 위상을 1초 단위로 한 주기(poll + 사이클) 전부 훑는다
            clock = FakeClock(close - timedelta(seconds=90 + phase))
            feed = LatencyFeed({BTC: candles}, clock, "1h")
            broker = PaperBroker(
                initial_cash=INITIAL_CASH,
                quote_currency="KRW",
                fee_pct=FEE,
                slippage_pct=SLIP,
                data_source=feed,
                asset_class=AssetClass.STOCK,
            )
            strat = ScriptedStrategy()
            strat.at(BTC, candles[k].timestamp, SignalAction.BUY)
            holder: dict[str, Trader] = {}

            def sleeping(seconds: float, clock=clock, holder=holder) -> None:
                clock.advance(seconds)
                if clock.now >= close + timedelta(minutes=3):
                    holder["trader"].stop()

            h = build(
                tmp_path / f"phase{phase}",
                {BTC: candles},
                clock,
                strategy=strat,
                broker=broker,
                feed=feed,
                sleep=sleeping,
                overrides={"engine": {"close_positions_at_market_close": True, "poll_seconds": 30}},
            )
            holder["trader"] = h.trader
            h.trader.run_forever()
            assert h.trader.cycles >= 5
            results.append((phase, BTC in h.trader.positions, [t.reason for t in h.trader.trades]))
        assert all(not held for _, held, _ in results), results
        assert all(reasons == ["장 마감 전 전량 청산"] for _, _, reasons in results), results


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

    def _midnight_setup(self, tmp_path, candles, broker_cls, strat=None):
        k = next((i for i in range(len(candles) - 2) if candles[i + 1].timestamp.hour == 23), None)
        if k is None:
            pytest.skip("23시 캔들이 없음")
        clock = FakeClock(after_candle(candles, k))  # 23:00:05 UTC
        feed = RealCandleFeed({BTC: candles}, clock, "1h")
        broker = broker_cls(initial_cash=INITIAL_CASH, fee_pct=FEE, slippage_pct=SLIP, data_source=feed)
        h = build(tmp_path, {BTC: candles}, clock, strategy=strat, broker=broker, feed=feed)
        return k, h, broker

    def test_exchange_fill_time_before_midnight_is_booked_into_engine_day(self, tmp_path, candles):
        """자정 직후 폴링의 매도 체결 시각이 거래소 시계로 전날 23:59:5x 여도 당일 집계를 되감지 않는다
        (되감으면 당일 손익이 사라지고 시작 자산이 지워져 일일 손실 한도가 그날 내내 꺼진다)."""

        class LaggingClockBroker(PaperBroker):
            """거래소 시계가 엔진 시계보다 3초 느리다 (주문/체결 시각만 영향, 가격은 그대로)."""

            def _now(self):
                return super()._now() - timedelta(seconds=3)

        strat = ScriptedStrategy()
        k, h, broker = self._midnight_setup(tmp_path, candles, LaggingClockBroker, strat)
        strat.at(BTC, candles[k].timestamp, SignalAction.BUY)
        strat.at(BTC, candles[k + 1].timestamp, SignalAction.SELL)
        h.trader.run_once()  # 23:00:05 매수
        assert BTC in h.trader.positions
        h.clock.set(candles[k + 1].timestamp + timedelta(hours=1, seconds=1))  # 다음 날 00:00:01 UTC
        h.trader.run_once()  # 날짜 변경 → 매도, 거래소 체결 시각은 전날 23:59:58
        t = h.trader.trades[-1]
        assert t.exit_time == h.clock.now - timedelta(seconds=3)
        assert t.exit_time.date() < h.clock.now.date()
        risk = h.trader.risk
        assert risk.current_day == h.clock.now.date()
        assert risk.daily_pnl == approx(t.pnl) and risk.daily_trades == 1
        start = risk.day_start_equity
        assert start is not None and risk.daily_loss_pct() == approx(t.pnl / start)
        h.clock.advance(30)
        h.trader.run_once()
        assert risk.current_day == h.clock.now.date() and risk.daily_pnl == approx(t.pnl)
        assert risk.day_start_equity == start
        data = json.loads(h.store.path.read_text())
        assert data["risk"]["day"] == h.clock.now.date().isoformat() and data["risk"]["daily_trades"] == 1

    def test_day_start_equity_retried_after_failed_roll_poll(self, tmp_path, candles):
        """날짜 변경 폴링에서 자산 조회가 실패해도 다음 폴링에 당일 시작 자산을 기록한다 (한도 하루 종일 꺼짐 방지)."""

        class FlakyAccountBroker(PaperBroker):
            fail = False

            def get_equity(self, symbols=None):
                if self.fail:
                    raise BrokerError("계좌 조회 실패 (테스트)")
                return super().get_equity(symbols)

            def get_balances(self):
                if self.fail:
                    raise BrokerError("계좌 조회 실패 (테스트)")
                return super().get_balances()

        k, h, broker = self._midnight_setup(tmp_path, candles, FlakyAccountBroker)
        h.trader.run_once()
        day1 = h.clock.now.date()
        assert h.trader.risk.day_start_equity == approx(INITIAL_CASH)

        broker.fail = True
        h.clock.advance(H)  # 다음 날 00:00:05 — 날짜 변경 폴링에서 자산 조회 실패
        h.trader.run_once()
        assert h.notifier.find("[일일 요약]") and h.trader.risk.current_day == day1
        # 그 사이 당일 거래가 먼저 집계된 경우: 시작 자산 = 현재 자산 - 당일 실현 손익
        entry, exit_ = next(
            (a, b) for a in candles for b in candles if b.timestamp > a.timestamp and b.close < a.close
        )
        trade = Trade(BTC, OrderSide.BUY, 0.01, entry.close, exit_.close, entry.timestamp, exit_.timestamp)
        h.trader.risk.record_trade(trade, now=h.clock.now)
        assert h.trader.risk.day_start_equity is None and h.trader.risk.daily_loss_pct() is None

        broker.fail = False
        h.clock.advance(30)
        h.trader.run_once()  # 다음 폴링에 다시 기록
        risk = h.trader.risk
        assert risk.current_day == h.clock.now.date()
        assert risk.day_start_equity == approx(broker.get_equity() - trade.pnl)
        assert risk.daily_pnl == approx(trade.pnl)
        assert risk.daily_loss_pct() == approx(trade.pnl / risk.day_start_equity)
        assert json.loads(h.store.path.read_text())["risk"]["day_start_equity"] == approx(
            risk.day_start_equity
        )

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

    def test_inflight_entry_dict_round_trip_and_validation(self, candles):
        now = candles[-1].timestamp
        sig = Signal(SignalAction.BUY, BTC, reason="r", max_holding_bars=1, meta={"k": 0.5})
        ie = InflightEntry(
            BTC, sig, bar_ts=candles[-2].timestamp, created_at=now, quantity=0.01, order_type="limit"
        )
        d = json.loads(json.dumps(ie.to_dict()))
        back = InflightEntry.from_dict(d)
        assert (back.symbol, back.bar_ts, back.created_at, back.quantity, back.order_type) == (
            BTC,
            candles[-2].timestamp,
            now,
            0.01,
            "limit",
        )
        assert back.signal.max_holding_bars == 1 and back.signal.meta == {"k": 0.5}
        minimal = InflightEntry.from_dict(
            {"symbol": BTC, "signal": {"action": "buy", "symbol": BTC}, "created_at": now.isoformat()}
        )
        assert minimal.bar_ts is None and minimal.quantity == 0.0 and minimal.order_type == "market"
        for bad in (None, [], {"symbol": BTC}, {**d, "created_at": None}, {**d, "signal": {}}):
            with pytest.raises(DataError):
                InflightEntry.from_dict(bad)

    def test_repr(self, tmp_path, candles):
        clock = FakeClock(after_candle(candles, self.K))
        h = build(tmp_path, {BTC: candles}, clock)
        assert "Trader" in repr(h.trader) and "scripted" in repr(h.trader)
