"""backtest/engine.py 테스트.

모든 시세는 conftest 의 **실제 Upbit 캔들**(KRW-BTC 일봉/시간봉, KRW-ETH 일봉)이다. 데모 데이터는 없다.
상수는 계좌 설정(초기 현금, 수수료, 슬리피지, 리스크 비율)뿐이다.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import pytest
from pytest import approx

from tradingbot.backtest import METRIC_KEYS, Backtester, BacktestResult, compute_metrics, periods_per_year
from tradingbot.backtest.engine import (
    REASON_MAX_HOLDING,
    SKIP_BUDGET,
    SKIP_INVALID,
    SKIP_RISK,
)
from tradingbot.config import RiskConfig
from tradingbot.exceptions import ConfigError, DataError
from tradingbot.models import (
    AssetClass,
    OrderSide,
    OrderStatus,
    OrderType,
    Signal,
    SignalAction,
)
from tradingbot.risk import RiskManager
from tradingbot.strategies import available_strategies, create_strategy
from tradingbot.strategies.base import BaseStrategy
from tradingbot.strategies.sma_cross import SMACrossStrategy

BTC = "KRW-BTC"
ETH = "KRW-ETH"
INITIAL = 10_000_000.0
FEE = 0.0005
SLIP = 0.0005
UTC = timezone.utc

#: 200개 캔들에서 거래가 충분히 나오도록 짧게 잡은 파라미터
SHORT_PARAMS: dict[str, dict[str, Any]] = {
    "sma_cross": {"fast": 3, "slow": 8},
    "ema_cross": {"fast": 3, "slow": 8},
    "rsi": {"period": 5, "oversold": 40, "overbought": 60},
    "bollinger": {"period": 10},
    "macd": {"fast": 5, "slow": 12, "signal": 4},
    "volatility_breakout": {"k": 0.5},
}


# ---------------------------------------------------------------------------- 헬퍼
def make_risk(**overrides: Any) -> RiskManager:
    cfg: dict[str, Any] = {
        "max_position_pct": 0.2,
        "max_positions": 5,
        "stop_loss_pct": None,
        "take_profit_pct": None,
        "trailing_stop_pct": None,
        "max_daily_loss_pct": None,
        "min_order_value": 0.0,
    }
    cfg.update(overrides)
    return RiskManager(RiskConfig(**cfg))


def run_bt(
    name: str,
    data: dict[str, pd.DataFrame],
    *,
    params: dict[str, Any] | None = None,
    fill_on: str = "next_open",
    risk: RiskManager | None = None,
    interval: str = "1d",
    strategy: BaseStrategy | None = None,
    **kw: Any,
) -> BacktestResult:
    if strategy is None:
        strategy = create_strategy(name, params if params is not None else SHORT_PARAMS.get(name, {}))
    strat = strategy
    bt = Backtester(
        strat,
        risk or make_risk(),
        initial_cash=INITIAL,
        fee_pct=FEE,
        slippage_pct=SLIP,
        fill_on=fill_on,
        interval=interval,
        **kw,
    )
    return bt.run(data)


def ts_list(df: pd.DataFrame) -> list[datetime]:
    return [t.to_pydatetime() for t in df["timestamp"]]


def index_of(df: pd.DataFrame, when: datetime) -> int:
    rows = df.index[df["timestamp"] == pd.Timestamp(when)]
    assert len(rows) == 1, f"timestamp {when} 가 데이터에 없습니다"
    return int(rows[0])


def filled(result: BacktestResult, side: OrderSide | None = None) -> list:
    return [o for o in result.orders if o.status == OrderStatus.FILLED and (side is None or o.side == side)]


def reconstruct_cash(result: BacktestResult) -> tuple[float, float]:
    """체결 주문만으로 현금을 재구성한다. (최종 현금, 재구성 중 최소 현금)."""
    cash = INITIAL
    lowest = cash
    for o in result.orders:
        if o.status != OrderStatus.FILLED:
            continue
        assert o.average_price is not None and o.filled_quantity == o.quantity
        if o.side == OrderSide.BUY:
            cash -= o.average_price * o.filled_quantity + o.fee
        else:
            cash += o.average_price * o.filled_quantity - o.fee
        lowest = min(lowest, cash)
    return cash, lowest


def holdings_timeline(result: BacktestResult) -> list[dict[str, float]]:
    """체결 순서대로 심볼별 보유 수량 스냅샷."""
    held: dict[str, float] = {}
    out = []
    for o in result.orders:
        if o.status != OrderStatus.FILLED:
            continue
        if o.side == OrderSide.BUY:
            held[o.symbol] = held.get(o.symbol, 0.0) + o.filled_quantity
        else:
            held[o.symbol] = held.get(o.symbol, 0.0) - o.filled_quantity
            if abs(held[o.symbol]) <= 1e-9 * max(1.0, o.filled_quantity):
                del held[o.symbol]
        out.append(dict(held))
    return out


def check_invariants(result: BacktestResult, data: dict[str, pd.DataFrame], risk: RiskManager | None = None):
    """모든 백테스트 결과가 만족해야 하는 회계/구조 불변식."""
    all_ts = sorted({t for df in data.values() for t in ts_list(df)})
    assert list(result.equity_curve.index) == [pd.Timestamp(t) for t in all_ts]
    assert result.equity_curve.index.tz is not None
    assert result.bars == len(all_ts)
    assert result.start == all_ts[0] and result.end == all_ts[-1]
    assert result.symbols == list(data)
    assert result.final_equity == approx(float(result.equity_curve.iloc[-1]))
    assert tuple(result.metrics) == METRIC_KEYS
    assert result.metrics["num_trades"] == len(result.trades)
    assert 0.0 <= result.metrics["exposure"] <= 1.0
    assert result.initial_cash == INITIAL

    # 주문은 전부 체결 또는 취소 상태로 끝난다 (미체결/거부 없음)
    statuses = {o.status for o in result.orders}
    assert statuses <= {OrderStatus.FILLED, OrderStatus.CANCELED}, statuses
    assert result.rejected_orders == 0

    # 현금 회계: 체결 주문으로 재구성한 현금 == 최종 현금, 음수가 된 적 없음
    cash, lowest = reconstruct_cash(result)
    assert cash == approx(result.final_cash, rel=1e-9)
    assert lowest >= -1e-6

    # 최종 자산 == 현금 + 포지션 × 마지막 종가
    value = 0.0
    for p in result.open_positions:
        value += p.quantity * float(data[p.symbol]["close"].iloc[-1])
    assert result.final_equity == approx(result.final_cash + value, rel=1e-9)
    if not result.open_positions:
        assert result.final_equity - INITIAL == approx(sum(t.pnl for t in result.trades), rel=1e-9, abs=1e-6)

    # 피라미딩 없음, 음수 보유 없음, 최대 보유 종목 수
    for snap in holdings_timeline(result):
        assert all(q > 0 for q in snap.values())
        if risk is not None:
            assert len(snap) <= risk.config.max_positions
    buys_in_a_row: dict[str, bool] = {}
    for o in filled(result):
        if o.side == OrderSide.BUY:
            assert not buys_in_a_row.get(o.symbol, False), "보유 중 추가 매수(피라미딩) 발생"
            buys_in_a_row[o.symbol] = True
        else:
            buys_in_a_row[o.symbol] = False

    # Trade 시각/심볼
    for t in result.trades:
        assert t.symbol in data
        assert t.entry_time <= t.exit_time
        sym_ts = set(ts_list(data[t.symbol]))
        assert t.entry_time in sym_ts and t.exit_time in sym_ts
        assert t.fee > 0
        assert t.quantity > 0 and t.entry_price > 0 and t.exit_price > 0
    # 지표는 compute_metrics 와 일치
    ppy = periods_per_year(result.interval, result.asset_class)
    ref = compute_metrics(result.equity_curve, result.trades, ppy, exposure=result.metrics["exposure"])
    for k in METRIC_KEYS:
        assert result.metrics[k] == approx(ref[k]), k


def signal_actions(strategy: BaseStrategy, df: pd.DataFrame, symbol: str = BTC) -> list[Signal]:
    prepared = strategy.prepare(df)
    return [strategy.signal_at(symbol, prepared, i) for i in range(len(prepared))]


# ============================================================================ 전 전략 실행
@pytest.mark.parametrize("name", available_strategies())
@pytest.mark.parametrize("fill_on", ["next_open", "close"])
def test_every_registered_strategy_runs_on_real_daily(daily_df, name, fill_on):
    risk = make_risk()
    res = run_bt(name, {BTC: daily_df}, fill_on=fill_on, risk=risk)
    check_invariants(res, {BTC: daily_df}, risk)
    assert res.strategy == name
    assert res.fill_on == fill_on
    assert float(res.equity_curve.iloc[0]) == INITIAL  # 첫 bar 에서는 체결이 일어날 수 없다
    assert len(res.trades) > 0  # 짧은 파라미터로 200개 봉이면 반드시 거래가 있다
    assert res.skipped_signals == 0
    text = res.summary()
    assert name in text and "총 수익률" in text and "최종 자산" in text


@pytest.mark.parametrize("name", available_strategies())
def test_every_strategy_on_real_hourly(candles_df, name):
    risk = make_risk()
    res = run_bt(name, {BTC: candles_df}, risk=risk, interval="1h")
    check_invariants(res, {BTC: candles_df}, risk)
    assert res.interval == "1h"
    ref = compute_metrics(res.equity_curve, res.trades, 8760.0, exposure=res.metrics["exposure"])
    assert res.metrics["sharpe"] == approx(ref["sharpe"])


def test_eth_daily_and_default_params(eth_daily_df):
    risk = make_risk()
    res = run_bt("sma_cross", {ETH: eth_daily_df}, params={}, risk=risk)
    check_invariants(res, {ETH: eth_daily_df}, risk)
    assert res.params == {"fast": 10, "slow": 30}


# ============================================================================ 체결 시점 / 가격
def test_next_open_fills_at_next_bar_open(daily_df):
    strat = create_strategy("sma_cross", SHORT_PARAMS["sma_cross"])
    res = run_bt("sma_cross", {BTC: daily_df}, strategy=strat)
    sigs = signal_actions(strat, daily_df)
    buys = filled(res, OrderSide.BUY)
    sells = filled(res, OrderSide.SELL)
    assert buys and sells
    for o in buys:
        j = index_of(daily_df, o.created_at)
        assert o.type == OrderType.MARKET
        assert o.average_price == approx(daily_df["open"].iat[j] * (1 + SLIP))
        assert sigs[j - 1].action == SignalAction.BUY  # 직전 bar 종가의 신호
        assert o.raw["reason"] == sigs[j - 1].reason
    for o in sells:
        j = index_of(daily_df, o.created_at)
        assert o.average_price == approx(daily_df["open"].iat[j] * (1 - SLIP))
        assert sigs[j - 1].action == SignalAction.SELL
    # Trade 는 매수 평균단가 → 매도가, 진입/청산 시각은 체결 bar
    for t, b, s in zip(res.trades, buys, sells, strict=False):
        assert t.entry_price == approx(b.average_price)
        assert t.exit_price == approx(s.average_price)
        assert t.entry_time == b.created_at and t.exit_time == s.created_at
        assert t.reason == s.raw["reason"]


def test_close_fills_at_signal_bar_close(daily_df):
    strat = create_strategy("sma_cross", SHORT_PARAMS["sma_cross"])
    res = run_bt("sma_cross", {BTC: daily_df}, strategy=strat, fill_on="close")
    sigs = signal_actions(strat, daily_df)
    for o in filled(res, OrderSide.BUY):
        j = index_of(daily_df, o.created_at)
        assert o.average_price == approx(daily_df["close"].iat[j] * (1 + SLIP))
        assert sigs[j].action == SignalAction.BUY
    for o in filled(res, OrderSide.SELL):
        j = index_of(daily_df, o.created_at)
        assert o.average_price == approx(daily_df["close"].iat[j] * (1 - SLIP))
        assert sigs[j].action == SignalAction.SELL


def test_fill_modes_differ_in_prices_but_not_in_signal_bars(daily_df):
    a = run_bt("sma_cross", {BTC: daily_df}, fill_on="next_open")
    b = run_bt("sma_cross", {BTC: daily_df}, fill_on="close")
    assert len(a.trades) == len(b.trades) > 0
    ts = ts_list(daily_df)
    for ta, tb in zip(a.trades, b.trades, strict=True):
        # next_open 은 신호 bar 의 다음 bar 에 체결
        assert ts.index(ta.entry_time) == ts.index(tb.entry_time) + 1
        assert ts.index(ta.exit_time) == ts.index(tb.exit_time) + 1
    assert any(ta.entry_price != tb.entry_price for ta, tb in zip(a.trades, b.trades, strict=True))


# ============================================================================ 변동성 돌파 (STOP + max_holding_bars=1)
@pytest.mark.parametrize("fill_on", ["next_open", "close"])
def test_volatility_breakout_stop_fill_and_next_open_exit(daily_df, fill_on):
    k = 0.5
    risk = make_risk()
    res = run_bt("volatility_breakout", {BTC: daily_df}, params={"k": k}, fill_on=fill_on, risk=risk)
    check_invariants(res, {BTC: daily_df}, risk)
    o_, h, lo = (daily_df[c].to_numpy() for c in ("open", "high", "low"))
    n = len(daily_df)

    buys = filled(res, OrderSide.BUY)
    assert buys
    for o in buys:
        j = index_of(daily_df, o.created_at)
        assert o.type == OrderType.STOP and o.price is None
        offset = k * (h[j - 1] - lo[j - 1])
        assert o.raw["stop_offset"] == approx(offset)
        trigger = o_[j] + offset
        assert o.raw["trigger"] == approx(trigger)
        assert h[j] >= trigger
        assert o.average_price == approx(trigger * (1 + SLIP))  # 시가 < 트리거이므로 트리거 체결
    # 돌파가 없었던 날의 STOP 주문은 취소된다
    for o in res.orders:
        if o.status == OrderStatus.CANCELED:
            j = index_of(daily_df, o.created_at)
            assert h[j] < o_[j] + k * (h[j - 1] - lo[j - 1])

    # 독립 재현: bar i(1..n-1) 에서 high >= open + k*range[i-1] 이면 진입 (만료 청산 후 같은 bar 재진입 허용)
    breakouts = [
        i for i in range(1, n) if (h[i - 1] - lo[i - 1]) > 0 and h[i] >= o_[i] + k * (h[i - 1] - lo[i - 1])
    ]
    assert [index_of(daily_df, o.created_at) for o in buys] == breakouts
    # 마지막 bar 의 진입은 청산할 다음 bar 가 없어 포지션으로 남는다
    expected_trades = len(breakouts) - (1 if breakouts and breakouts[-1] == n - 1 else 0)
    assert len(res.trades) == expected_trades
    assert len(res.open_positions) == (1 if breakouts and breakouts[-1] == n - 1 else 0)

    sells = filled(res, OrderSide.SELL)
    for b, s, t in zip(buys, sells, res.trades, strict=False):
        jb = index_of(daily_df, b.created_at)
        js = index_of(daily_df, s.created_at)
        assert js == jb + 1
        assert s.type == OrderType.MARKET
        assert s.average_price == approx(o_[js] * (1 - SLIP))
        assert REASON_MAX_HOLDING in s.raw["reason"]
        assert t.holding_seconds == 86400
        assert REASON_MAX_HOLDING in t.reason
    assert res.ignored_signals == 0  # 만료 예정 포지션은 "없음" 으로 보고 재진입 신호를 받는다


def test_volatility_breakout_identical_for_both_fill_modes(daily_df):
    a = run_bt("volatility_breakout", {BTC: daily_df}, fill_on="next_open")
    b = run_bt("volatility_breakout", {BTC: daily_df}, fill_on="close")
    assert [(t.entry_time, t.entry_price, t.exit_price) for t in a.trades] == [
        (t.entry_time, t.entry_price, t.exit_price) for t in b.trades
    ]


# ============================================================================ 리스크 청산 (bar 중)
def _first_market_entry_bar(res: BacktestResult, df: pd.DataFrame, predicate) -> tuple[int, float] | None:
    """조건을 만족하는 첫 시장가 매수 체결 (bar index, 체결가)."""
    for o in filled(res, OrderSide.BUY):
        j = index_of(df, o.created_at)
        if predicate(j):
            return j, o.average_price
    return None


def test_stop_loss_triggers_intrabar_at_stop_price(daily_df):
    base = run_bt("sma_cross", {BTC: daily_df})
    o_, lo = daily_df["open"].to_numpy(), daily_df["low"].to_numpy()
    found = _first_market_entry_bar(base, daily_df, lambda j: lo[j] < o_[j])
    assert found is not None
    f, entry = found
    stop = (o_[f] + lo[f]) / 2  # 시가 아래, 저가 위 → bar 중간에 손절
    sl_pct = 1 - stop / entry
    assert 0 < sl_pct < 1

    risk = make_risk(stop_loss_pct=sl_pct)
    res = run_bt("sma_cross", {BTC: daily_df}, risk=risk)
    check_invariants(res, {BTC: daily_df}, risk)
    t = res.trades[0]
    assert t.entry_price == approx(entry)
    assert t.entry_time == ts_list(daily_df)[f]
    assert t.exit_time == ts_list(daily_df)[f]  # 진입 bar 안에서 손절
    assert t.exit_price == approx(stop * (1 - SLIP), rel=1e-9)
    assert "손절" in t.reason
    assert t.pnl < 0


def test_stop_loss_gap_fills_at_open(daily_df):
    base = run_bt("sma_cross", {BTC: daily_df})
    o_ = daily_df["open"].to_numpy()
    f, entry = _first_market_entry_bar(base, daily_df, lambda j: True)
    # 손절가를 시가보다 살짝 위에 두면 (entry = open*(1+slip) 이므로 가능) 시가가 이미 손절가 이하 → 시가 체결
    stop = o_[f] * (1 + SLIP / 2)
    sl_pct = 1 - stop / entry
    assert 0 < sl_pct < SLIP
    res = run_bt("sma_cross", {BTC: daily_df}, risk=make_risk(stop_loss_pct=sl_pct))
    t = res.trades[0]
    assert t.exit_time == ts_list(daily_df)[f]
    assert t.exit_price == approx(o_[f] * (1 - SLIP), rel=1e-9)
    assert "손절" in t.reason


def test_take_profit_triggers_intrabar_at_target(daily_df):
    base = run_bt("sma_cross", {BTC: daily_df})
    o_, h = daily_df["open"].to_numpy(), daily_df["high"].to_numpy()
    found = _first_market_entry_bar(base, daily_df, lambda j: h[j] > o_[j] * (1 + 4 * SLIP))
    assert found is not None
    f, entry = found
    target = (o_[f] + h[f]) / 2
    tp_pct = target / entry - 1
    assert tp_pct > 0
    risk = make_risk(take_profit_pct=tp_pct)
    res = run_bt("sma_cross", {BTC: daily_df}, risk=risk)
    check_invariants(res, {BTC: daily_df}, risk)
    t = res.trades[0]
    assert t.entry_price == approx(entry)
    assert t.exit_time == ts_list(daily_df)[f]
    assert t.exit_price == approx(target * (1 - SLIP), rel=1e-9)
    assert "익절" in t.reason
    assert t.pnl > 0


def test_stop_loss_has_priority_over_take_profit(daily_df):
    """같은 bar 에서 손절가와 익절가가 모두 닿으면 손절이 우선한다."""
    base = run_bt("sma_cross", {BTC: daily_df})
    o_, h, lo = (daily_df[c].to_numpy() for c in ("open", "high", "low"))
    found = _first_market_entry_bar(base, daily_df, lambda j: lo[j] < o_[j] and h[j] > o_[j] * (1 + 4 * SLIP))
    assert found is not None
    f, entry = found
    stop = (o_[f] + lo[f]) / 2
    target = (o_[f] + h[f]) / 2
    res = run_bt(
        "sma_cross",
        {BTC: daily_df},
        risk=make_risk(stop_loss_pct=1 - stop / entry, take_profit_pct=target / entry - 1),
    )
    t = res.trades[0]
    assert t.exit_time == ts_list(daily_df)[f]
    assert t.exit_price == approx(stop * (1 - SLIP), rel=1e-9)
    assert "손절" in t.reason


def test_trailing_stop_matches_reference_for_first_trade(daily_df):
    trail = 0.02
    strat = create_strategy("sma_cross", SHORT_PARAMS["sma_cross"])
    risk = make_risk(trailing_stop_pct=trail)
    res = run_bt("sma_cross", {BTC: daily_df}, strategy=strat, risk=risk)
    check_invariants(res, {BTC: daily_df}, risk)
    sigs = signal_actions(strat, daily_df)
    o_, h, lo = (daily_df[c].to_numpy() for c in ("open", "high", "low"))
    ts = ts_list(daily_df)

    first_buy = filled(res, OrderSide.BUY)[0]
    f = index_of(daily_df, first_buy.created_at)
    entry = first_buy.average_price
    highest = entry
    expected: tuple[datetime, float, str] | None = None
    for g in range(f, len(daily_df)):
        if g > f and sigs[g - 1].action == SignalAction.SELL:
            expected = (ts[g], o_[g] * (1 - SLIP), "strategy")
            break
        highest = max(highest, o_[g])
        if highest > entry and o_[g] <= highest * (1 - trail):
            expected = (ts[g], o_[g] * (1 - SLIP), "trailing")
            break
        if highest > entry and lo[g] <= highest * (1 - trail):
            expected = (ts[g], highest * (1 - trail) * (1 - SLIP), "trailing")
            break
        highest = max(highest, h[g])
        if highest > entry and lo[g] <= highest * (1 - trail):
            expected = (ts[g], highest * (1 - trail) * (1 - SLIP), "trailing")
            break
    assert expected is not None
    t = res.trades[0]
    assert t.exit_time == expected[0]
    assert t.exit_price == approx(expected[1], rel=1e-9)
    if expected[2] == "trailing":
        assert "추적 손절" in t.reason
    # 전체 run 에서 추적 손절이 실제로 발생했는지 (2% 추적이면 200일 중 반드시 발생)
    assert any("추적 손절" in t.reason for t in res.trades)


def test_tight_stop_loss_produces_stop_exits_and_apply_entry_levels(daily_df):
    risk = make_risk(stop_loss_pct=0.01, take_profit_pct=0.05)
    res = run_bt("sma_cross", {BTC: daily_df}, risk=risk)
    check_invariants(res, {BTC: daily_df}, risk)
    reasons = [t.reason for t in res.trades]
    assert any("손절" in r for r in reasons)
    for p in res.open_positions:
        assert p.stop_loss == approx(p.average_price * 0.99)
        assert p.take_profit == approx(p.average_price * 1.05)
        assert p.meta["entry_bar_ts"] == p.opened_at.isoformat()


# ============================================================================ 미래 참조 없음
@pytest.mark.parametrize("name", available_strategies())
@pytest.mark.parametrize("fill_on", ["next_open", "close"])
def test_no_lookahead_truncated_data_gives_identical_prefix(daily_df, name, fill_on):
    cut = 120
    risk_kw = {"stop_loss_pct": 0.03, "trailing_stop_pct": 0.05}
    full = run_bt(name, {BTC: daily_df}, fill_on=fill_on, risk=make_risk(**risk_kw))
    part = run_bt(name, {BTC: daily_df.iloc[:cut].copy()}, fill_on=fill_on, risk=make_risk(**risk_kw))
    last_ts = ts_list(daily_df)[cut - 1]

    def trade_key(t):
        return (t.symbol, t.quantity, t.entry_price, t.exit_price, t.entry_time, t.exit_time, t.fee, t.reason)

    def order_key(o):
        return (
            o.id,
            o.symbol,
            o.side,
            o.type,
            o.quantity,
            o.price,
            o.status,
            o.average_price,
            o.fee,
            o.created_at,
        )

    assert [trade_key(t) for t in part.trades] == [
        trade_key(t) for t in full.trades if t.exit_time <= last_ts
    ]
    assert [order_key(o) for o in part.orders] == [
        order_key(o) for o in full.orders if o.created_at <= last_ts
    ]
    pd.testing.assert_series_equal(part.equity_curve, full.equity_curve.iloc[:cut])


def test_future_rows_do_not_change_past_equity(daily_df):
    """뒤쪽 캔들을 바꿔도 앞쪽 자산 곡선은 그대로다 (미래 참조 없음)."""
    half = 100
    base = run_bt("macd", {BTC: daily_df})
    swapped = daily_df.copy()
    # 뒤 절반의 행들을 앞 절반의 실제 캔들 행으로 교체 (timestamp 는 유지) → 값은 여전히 실제 시세
    for col in ("open", "high", "low", "close", "volume"):
        swapped.loc[half:, col] = daily_df[col].iloc[:half].to_numpy()
    other = run_bt("macd", {BTC: swapped})
    pd.testing.assert_series_equal(base.equity_curve.iloc[:half], other.equity_curve.iloc[:half])


# ============================================================================ 멀티 심볼
def test_multi_symbol_shares_cash_all_in(daily_df, eth_daily_df):
    data = {BTC: daily_df, ETH: eth_daily_df}
    risk = make_risk(max_position_pct=1.0, max_positions=2, min_order_value=100_000)
    res = run_bt("sma_cross", data, risk=risk)
    check_invariants(res, data, risk)
    assert res.symbols == [BTC, ETH]
    # 전액 투자: 모든 매수는 현금 상한(cash*(1-fee)*0.999) 을 거의 다 쓴다 → 두 번째 심볼은 예산 부족으로 스킵
    cash = INITIAL
    for o in filled(res):
        if o.side == OrderSide.BUY:
            cost = o.average_price * o.filled_quantity
            cap = cash * (1 - FEE) * 0.999
            assert cost == approx(cap, rel=1e-6)
            cash -= cost + o.fee
        else:
            cash += o.average_price * o.filled_quantity - o.fee
    assert max(len(s) for s in holdings_timeline(res)) == 1
    assert res.skip_reasons.get(SKIP_BUDGET, 0) > 0
    assert res.skipped_signals == sum(res.skip_reasons.values())


def test_multi_symbol_both_trade_within_limits(daily_df, eth_daily_df):
    data = {BTC: daily_df, ETH: eth_daily_df}
    risk = make_risk(max_position_pct=0.3, max_positions=2)
    res = run_bt("sma_cross", data, risk=risk)
    check_invariants(res, data, risk)
    symbols_traded = {t.symbol for t in res.trades}
    assert symbols_traded == {BTC, ETH}
    assert max(len(s) for s in holdings_timeline(res)) == 2  # 동시 보유가 실제로 일어난다
    # 심볼별로 떼어 돌린 결과와 비교하면 각 심볼의 진입 bar 는 같다 (사이징만 다름)
    solo = run_bt("sma_cross", {ETH: eth_daily_df}, risk=make_risk(max_position_pct=0.3, max_positions=2))
    assert [t.entry_time for t in res.trades if t.symbol == ETH] == [t.entry_time for t in solo.trades]


def test_max_positions_one_blocks_second_symbol(daily_df, eth_daily_df):
    data = {BTC: daily_df, ETH: eth_daily_df}
    risk = make_risk(max_position_pct=0.3, max_positions=1)
    res = run_bt("sma_cross", data, risk=risk)
    check_invariants(res, data, risk)
    assert max(len(s) for s in holdings_timeline(res)) == 1
    assert res.skip_reasons.get(SKIP_RISK, 0) > 0


def test_non_overlapping_symbols(daily_df, eth_daily_df):
    data = {BTC: daily_df.iloc[:100].copy(), ETH: eth_daily_df.iloc[100:].copy()}
    risk = make_risk()
    res = run_bt("sma_cross", data, risk=risk)
    check_invariants(res, data, risk)
    assert res.bars == 200
    assert {t.symbol for t in res.trades} == {BTC, ETH}
    # 앞 절반은 BTC 단독 결과와 동일
    solo = run_bt("sma_cross", {BTC: daily_df.iloc[:100].copy()})
    pd.testing.assert_series_equal(res.equity_curve.iloc[:100], solo.equity_curve, check_names=False)


# ============================================================================ 리스크 매니저 연동
class SpyRisk(RiskManager):
    def __init__(self, config: RiskConfig) -> None:
        super().__init__(config)
        self.start_days: list[datetime] = []
        self.recorded = 0
        self.entries: list[tuple[str, float]] = []

    def start_day(self, equity: float, now: datetime) -> None:
        self.start_days.append(now)
        super().start_day(equity, now)

    def record_trade(self, trade) -> None:
        self.recorded += 1
        super().record_trade(trade)

    def apply_entry(self, position, signal, fill_price: float) -> None:
        self.entries.append((position.symbol, fill_price))
        super().apply_entry(position, signal, fill_price)


def test_risk_hooks_called(candles_df):
    risk = SpyRisk(
        RiskConfig(max_position_pct=0.2, max_positions=5, stop_loss_pct=None, max_daily_loss_pct=None)
    )
    res = run_bt("sma_cross", {BTC: candles_df}, risk=risk, interval="1h")
    days = sorted({t.date() for t in ts_list(candles_df)})
    assert [d.date() for d in risk.start_days] == days
    assert risk.recorded == len(res.trades)
    assert len(risk.entries) == len(filled(res, OrderSide.BUY))
    assert [p for _, p in risk.entries] == [o.average_price for o in filled(res, OrderSide.BUY)]


def test_daily_loss_limit_blocks_entries_same_utc_day(candles_df):
    risk = make_risk(max_daily_loss_pct=1e-6, max_position_pct=0.5)
    res = run_bt("sma_cross", {BTC: candles_df}, risk=risk, interval="1h")
    check_invariants(res, {BTC: candles_df}, risk)
    losing_exits = [t.exit_time for t in res.trades if t.pnl < 0]
    assert losing_exits
    for o in filled(res, OrderSide.BUY):
        for ex in losing_exits:
            assert not (o.created_at.date() == ex.date() and o.created_at > ex), (
                f"손실 후 같은 날 재진입: {o.created_at} > {ex}"
            )
    assert res.skip_reasons.get(SKIP_RISK, 0) > 0
    assert any("일일 손실 한도" in k or "risk" in k for k in res.skip_reasons)


def test_skipped_when_budget_below_min_order_value(daily_df):
    strat = create_strategy("sma_cross", SHORT_PARAMS["sma_cross"])
    risk = make_risk(min_order_value=1e12)
    res = run_bt("sma_cross", {BTC: daily_df}, strategy=strat, risk=risk)
    assert res.orders == [] and res.trades == []
    buy_signals = sum(
        1
        for i, s in enumerate(signal_actions(strat, daily_df))
        if i >= strat.warmup - 1 and s.action == SignalAction.BUY
    )
    assert buy_signals > 0
    assert res.skipped_signals == buy_signals
    assert res.skip_reasons == {SKIP_BUDGET: buy_signals}
    assert res.final_equity == INITIAL
    assert (res.equity_curve == INITIAL).all()


def test_ignored_signals_match_position_state_machine(daily_df):
    strat = create_strategy("sma_cross", SHORT_PARAMS["sma_cross"])
    res = run_bt("sma_cross", {BTC: daily_df}, strategy=strat)
    holding = False
    ignored = accepted_sells = 0
    for i, s in enumerate(signal_actions(strat, daily_df)):
        if i < strat.warmup - 1 or s.is_hold:
            continue
        if s.action == SignalAction.BUY:
            if holding:
                ignored += 1
            elif i < len(daily_df) - 1:
                holding = True
        else:
            if not holding:
                ignored += 1
            elif i < len(daily_df) - 1:
                holding = False
                accepted_sells += 1
    assert res.ignored_signals == ignored
    assert len(res.trades) == accepted_sells


# ============================================================================ 커스텀 신호 (LIMIT / STOP 매도 / max_holding_bars)
class LimitDipStrategy(BaseStrategy):
    """매 bar 종가 0.3% 아래 지정가 매수, 다음 bar 시가 청산 (LIMIT 체결/취소 경로 검증용)."""

    name = "limit_dip"
    default_params: dict[str, Any] = {"dip": 0.003}

    @property
    def warmup(self) -> int:
        return 1

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        return df.copy()

    def signal_at(self, symbol: str, df: pd.DataFrame, i: int) -> Signal:
        price = float(df["close"].iat[i]) * (1 - self.params["dip"])
        return Signal(
            action=SignalAction.BUY,
            symbol=symbol,
            order_type=OrderType.LIMIT,
            price=price,
            max_holding_bars=1,
            reason="지정가 눌림목",
        )


def test_limit_buy_fills_at_price_or_is_canceled(daily_df):
    risk = make_risk()
    res = run_bt("limit_dip", {BTC: daily_df}, strategy=LimitDipStrategy(), risk=risk)
    check_invariants(res, {BTC: daily_df}, risk)
    c, lo, o_ = (daily_df[col].to_numpy() for col in ("close", "low", "open"))
    limit_orders = [o for o in res.orders if o.type == OrderType.LIMIT]
    assert len(limit_orders) == len(daily_df) - 1  # 마지막 bar 신호는 다음 bar 가 없어 접수되지 않는다
    n_filled = n_canceled = 0
    for o in limit_orders:
        j = index_of(daily_df, o.created_at)
        assert o.price == approx(c[j - 1] * 0.997)
        if lo[j] <= o.price:
            assert o.status == OrderStatus.FILLED
            assert o.average_price == approx(o.price)  # 지정가는 슬리피지 없음
            n_filled += 1
        else:
            assert o.status == OrderStatus.CANCELED
            n_canceled += 1
    assert n_filled > 0 and n_canceled > 0
    for t in res.trades:
        j = index_of(daily_df, t.exit_time)
        assert t.exit_price == approx(o_[j] * (1 - SLIP))
        assert index_of(daily_df, t.entry_time) == j - 1
        assert REASON_MAX_HOLDING in t.reason


class AlternatingLimitSellStrategy(BaseStrategy):
    """4 bar 주기: BUY MARKET → (2 bar 뒤) SELL LIMIT 종가+0.2%. STOP/LIMIT 매도 경로 검증용."""

    name = "alt_limit_sell"
    default_params: dict[str, Any] = {}

    @property
    def warmup(self) -> int:
        return 1

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        return df.copy()

    def signal_at(self, symbol: str, df: pd.DataFrame, i: int) -> Signal:
        if i % 4 == 0:
            return Signal(action=SignalAction.BUY, symbol=symbol, reason="주기 매수")
        if i % 4 == 2:
            return Signal(
                action=SignalAction.SELL,
                symbol=symbol,
                order_type=OrderType.LIMIT,
                price=float(df["close"].iat[i]) * 1.002,
                reason="지정가 매도",
            )
        return Signal.hold(symbol)


def test_limit_sell_fills_when_high_reaches_price(daily_df):
    risk = make_risk()
    res = run_bt("alt", {BTC: daily_df}, strategy=AlternatingLimitSellStrategy(), risk=risk)
    check_invariants(res, {BTC: daily_df}, risk)
    h, c = daily_df["high"].to_numpy(), daily_df["close"].to_numpy()
    sells = [o for o in res.orders if o.side == OrderSide.SELL]
    assert sells and all(o.type == OrderType.LIMIT for o in sells)
    for o in sells:
        j = index_of(daily_df, o.created_at)
        assert o.price == approx(c[j - 1] * 1.002)
        if h[j] >= o.price:
            assert o.status == OrderStatus.FILLED and o.average_price == approx(o.price)
        else:
            assert o.status == OrderStatus.CANCELED
    assert {o.status for o in sells} == {OrderStatus.FILLED, OrderStatus.CANCELED}
    # 미체결 지정가 매도 뒤에도 포지션은 유지되고 다음 주기 매수는 무시된다
    assert res.ignored_signals > 0


class StopSellStrategy(BaseStrategy):
    """BUY MARKET 후 다음 bar 부터 'SELL STOP(stop_offset)' = 다음 시가 - offset 하향 돌파 시 매도."""

    name = "stop_sell"
    default_params: dict[str, Any] = {"offset_pct": 0.01}

    @property
    def warmup(self) -> int:
        return 1

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        return df.copy()

    def signal_at(self, symbol: str, df: pd.DataFrame, i: int) -> Signal:
        if i % 6 == 0:
            return Signal(action=SignalAction.BUY, symbol=symbol, reason="주기 매수")
        return Signal(
            action=SignalAction.SELL,
            symbol=symbol,
            order_type=OrderType.STOP,
            stop_offset=float(df["close"].iat[i]) * self.params["offset_pct"],
            reason="스탑 매도",
        )


def test_stop_sell_offset_triggers_below_next_open(daily_df):
    risk = make_risk()
    res = run_bt("stop_sell", {BTC: daily_df}, strategy=StopSellStrategy(), risk=risk)
    check_invariants(res, {BTC: daily_df}, risk)
    o_, lo, c = (daily_df[col].to_numpy() for col in ("open", "low", "close"))
    stop_sells = [o for o in res.orders if o.side == OrderSide.SELL and o.type == OrderType.STOP]
    assert stop_sells
    for o in stop_sells:
        j = index_of(daily_df, o.created_at)
        offset = c[j - 1] * 0.01
        assert o.raw["stop_offset"] == approx(offset)
        trigger = o_[j] - offset
        if lo[j] <= trigger:
            assert o.status == OrderStatus.FILLED
            assert o.raw["trigger"] == approx(trigger)
            assert o.average_price == approx(trigger * (1 - SLIP))
        else:
            assert o.status == OrderStatus.CANCELED


class PeriodicHoldStrategy(BaseStrategy):
    """10 bar 마다 BUY MARKET, max_holding_bars=3."""

    name = "periodic_hold"
    default_params: dict[str, Any] = {"every": 10, "hold": 3}

    @property
    def warmup(self) -> int:
        return 1

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        return df.copy()

    def signal_at(self, symbol: str, df: pd.DataFrame, i: int) -> Signal:
        if i % self.params["every"] == 0:
            return Signal(
                action=SignalAction.BUY, symbol=symbol, max_holding_bars=self.params["hold"], reason="주기"
            )
        return Signal.hold(symbol)


@pytest.mark.parametrize("fill_on, offset", [("next_open", 1), ("close", 0)])
def test_max_holding_bars_exit_at_nth_next_open(daily_df, fill_on, offset):
    risk = make_risk()
    res = run_bt("periodic", {BTC: daily_df}, strategy=PeriodicHoldStrategy(), risk=risk, fill_on=fill_on)
    check_invariants(res, {BTC: daily_df}, risk)
    ts = ts_list(daily_df)
    o_ = daily_df["open"].to_numpy()
    assert len(res.trades) >= 19
    for t in res.trades:
        e = ts.index(t.entry_time)
        x = ts.index(t.exit_time)
        assert e % 10 == offset  # next_open: 신호 bar 다음 bar, close: 신호 bar
        assert x == e + 3
        assert t.exit_price == approx(o_[x] * (1 - SLIP))
        assert REASON_MAX_HOLDING in t.reason


class BuyAtLastBarStrategy(BaseStrategy):
    name = "buy_last"
    default_params: dict[str, Any] = {}

    @property
    def warmup(self) -> int:
        return 1

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        return df.copy()

    def signal_at(self, symbol: str, df: pd.DataFrame, i: int) -> Signal:
        if i == len(df) - 1:
            return Signal(action=SignalAction.BUY, symbol=symbol, reason="마지막 bar")
        return Signal.hold(symbol)


def test_open_position_at_end_is_marked_to_close(daily_df):
    risk = make_risk(max_position_pct=0.5, stop_loss_pct=0.03)
    res = run_bt("buy_last", {BTC: daily_df}, strategy=BuyAtLastBarStrategy(), risk=risk, fill_on="close")
    check_invariants(res, {BTC: daily_df}, risk)
    assert len(res.open_positions) == 1 and res.trades == []
    p = res.open_positions[0]
    last_close = float(daily_df["close"].iloc[-1])
    assert p.average_price == approx(last_close * (1 + SLIP))
    assert res.final_equity == approx(res.final_cash + p.quantity * last_close)
    assert res.final_equity < INITIAL  # 수수료 + 슬리피지만큼 손실
    assert p.stop_loss == approx(p.average_price * 0.97)
    assert res.metrics["exposure"] == approx(1 / len(daily_df))
    assert "미청산 포지션: KRW-BTC" in res.summary()
    # next_open 이면 마지막 bar 의 매수는 체결될 다음 bar 가 없다
    res2 = run_bt("buy_last", {BTC: daily_df}, strategy=BuyAtLastBarStrategy(), risk=make_risk())
    assert res2.orders == [] and res2.open_positions == []


class InvalidLimitStrategy(BaseStrategy):
    name = "bad_limit"
    default_params: dict[str, Any] = {}

    @property
    def warmup(self) -> int:
        return 1

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        return df.copy()

    def signal_at(self, symbol: str, df: pd.DataFrame, i: int) -> Signal:
        return Signal(action=SignalAction.BUY, symbol=symbol, order_type=OrderType.LIMIT, price=None)


def test_invalid_signal_is_skipped_not_crashed(daily_df):
    res = run_bt("bad", {BTC: daily_df}, strategy=InvalidLimitStrategy())
    assert res.orders == []
    assert res.skip_reasons == {SKIP_INVALID: len(daily_df)}


class RecordingSMA(SMACrossStrategy):
    def __init__(self, **params: Any) -> None:
        super().__init__(**params)
        self.calls: list[int] = []

    def signal_at(self, symbol: str, df: pd.DataFrame, i: int) -> Signal:
        self.calls.append(i)
        return super().signal_at(symbol, df, i)


def test_signals_requested_only_from_warmup_row(daily_df):
    strat = RecordingSMA(fast=10, slow=30)
    run_bt("sma", {BTC: daily_df}, strategy=strat)
    assert strat.calls == list(range(strat.warmup - 1, len(daily_df)))


def test_too_little_data_runs_without_signals(daily_df):
    strat = create_strategy("sma_cross", {"fast": 10, "slow": 30})
    small = daily_df.iloc[:20].copy()
    res = run_bt("sma_cross", {BTC: small}, strategy=strat)
    assert res.orders == [] and res.trades == []
    assert res.bars == 20 and res.final_equity == INITIAL


# ============================================================================ 옵션
def test_round_quantity_callable_is_applied(daily_df):
    res = run_bt("sma_cross", {BTC: daily_df}, round_quantity=lambda s, q: math.floor(q * 1e4) / 1e4)
    buys = filled(res, OrderSide.BUY)
    assert buys
    for o in buys:
        assert o.quantity == approx(round(o.quantity, 4))


def test_stock_asset_class_uses_252_periods(daily_df):
    res = run_bt("sma_cross", {BTC: daily_df}, asset_class=AssetClass.STOCK)
    ref = compute_metrics(res.equity_curve, res.trades, 252.0, exposure=res.metrics["exposure"])
    assert res.metrics["sharpe"] == approx(ref["sharpe"])
    assert res.asset_class == AssetClass.STOCK
    assert res.to_dict()["asset_class"] == "stock"


def test_min_order_value_option(daily_df):
    res = run_bt("sma_cross", {BTC: daily_df}, min_order_value=1e12)
    assert res.orders == []
    assert res.skip_reasons == {SKIP_BUDGET: res.skipped_signals}


def test_strength_scales_position_size(daily_df):
    class HalfStrength(SMACrossStrategy):
        def signal_at(self, symbol: str, df: pd.DataFrame, i: int) -> Signal:
            s = super().signal_at(symbol, df, i)
            if s.action == SignalAction.BUY:
                s.strength = 0.5
            return s

    full = run_bt("sma", {BTC: daily_df}, strategy=SMACrossStrategy(fast=3, slow=8))
    half = run_bt("sma", {BTC: daily_df}, strategy=HalfStrength(fast=3, slow=8))
    b_full = filled(full, OrderSide.BUY)[0]
    b_half = filled(half, OrderSide.BUY)[0]
    assert b_full.created_at == b_half.created_at
    assert b_half.quantity == approx(b_full.quantity / 2, rel=1e-6)


# ============================================================================ 입력 검증
def test_empty_data_raises():
    bt = Backtester(create_strategy("sma_cross"), make_risk())
    with pytest.raises(DataError):
        bt.run({})
    with pytest.raises(DataError):
        bt.run([])  # type: ignore[arg-type]


def test_empty_dataframe_raises(daily_df):
    with pytest.raises(DataError):
        run_bt("sma_cross", {BTC: daily_df.iloc[0:0]})


def test_missing_column_raises(daily_df):
    with pytest.raises(DataError, match="컬럼"):
        run_bt("sma_cross", {BTC: daily_df.drop(columns=["volume"])})


def test_nan_and_nonpositive_prices_raise(daily_df):
    bad = daily_df.copy()
    bad.loc[5, "close"] = float("nan")
    with pytest.raises(DataError):
        run_bt("sma_cross", {BTC: bad})
    bad = daily_df.copy()
    bad.loc[5, "low"] = 0.0
    with pytest.raises(DataError):
        run_bt("sma_cross", {BTC: bad})


def test_inconsistent_high_low_raise(daily_df):
    bad = daily_df.copy()
    bad.loc[3, "high"] = bad.loc[3, "low"] - 1.0
    with pytest.raises(DataError, match="high/low"):
        run_bt("sma_cross", {BTC: bad})


def test_bad_timestamp_raises(daily_df):
    bad = daily_df.copy()
    bad["timestamp"] = bad["timestamp"].astype(str)
    bad.loc[0, "timestamp"] = "not-a-date"
    with pytest.raises(DataError):
        run_bt("sma_cross", {BTC: bad})


def test_non_dataframe_raises(daily_df):
    with pytest.raises(DataError):
        run_bt("sma_cross", {BTC: daily_df.to_dict("records")})  # type: ignore[dict-item]


def test_empty_symbol_raises(daily_df):
    with pytest.raises(DataError):
        run_bt("sma_cross", {"": daily_df})


def test_quote_currency_mismatch_raises(daily_df):
    with pytest.raises(ConfigError, match="결제통화"):
        run_bt("sma_cross", {"BTC/USDT": daily_df})


def test_duplicate_timestamps_are_collapsed(daily_df):
    dup = pd.concat([daily_df, daily_df.iloc[[50]]], ignore_index=True)
    res = run_bt("sma_cross", {BTC: dup})
    ref = run_bt("sma_cross", {BTC: daily_df})
    pd.testing.assert_series_equal(res.equity_curve, ref.equity_curve)


def test_unsorted_input_is_sorted(daily_df):
    shuffled = daily_df.sample(frac=1.0, random_state=7).reset_index(drop=True)
    res = run_bt("sma_cross", {BTC: shuffled})
    ref = run_bt("sma_cross", {BTC: daily_df})
    pd.testing.assert_series_equal(res.equity_curve, ref.equity_curve)
    assert [t.exit_price for t in res.trades] == [t.exit_price for t in ref.trades]


def test_input_dataframe_not_mutated(daily_df):
    snapshot = daily_df.copy(deep=True)
    run_bt("macd", {BTC: daily_df})
    pd.testing.assert_frame_equal(daily_df, snapshot)


@pytest.mark.parametrize(
    "kw",
    [
        {"fill_on": "open"},
        {"interval": "2h"},
        {"initial_cash": 0},
        {"initial_cash": -1},
        {"initial_cash": float("nan")},
        {"fee_pct": 1.0},
        {"slippage_pct": -0.1},
        {"quote_currency": ""},
        {"asset_class": "bond"},
        {"min_order_value": -1},
    ],
)
def test_constructor_validation(kw):
    with pytest.raises(ConfigError):
        Backtester(create_strategy("sma_cross"), make_risk(), **kw)


def test_constructor_type_checks():
    with pytest.raises(ConfigError):
        Backtester("sma_cross", make_risk())  # type: ignore[arg-type]
    with pytest.raises(ConfigError):
        Backtester(create_strategy("sma_cross"), RiskConfig())  # type: ignore[arg-type]


# ============================================================================ 결과 객체
def test_result_to_dict_is_json_safe_and_round_trips_fields(daily_df):
    risk = make_risk(stop_loss_pct=0.03)
    res = run_bt("rsi", {BTC: daily_df}, risk=risk)
    d = res.to_dict()
    text = json.dumps(d, ensure_ascii=False)
    back = json.loads(text)
    assert back["strategy"] == "rsi"
    assert back["params"] == {"period": 5, "oversold": 40.0, "overbought": 60.0}
    assert back["symbols"] == [BTC] and back["interval"] == "1d"
    assert back["start"] == res.start.isoformat() and back["end"] == res.end.isoformat()
    assert back["initial_cash"] == INITIAL
    assert back["final_equity"] == approx(res.final_equity)
    assert len(back["equity_curve"]) == len(daily_df)
    first_ts, first_val = back["equity_curve"][0]
    assert first_ts == ts_list(daily_df)[0].isoformat() and first_val == INITIAL
    assert len(back["trades"]) == len(res.trades) and len(back["orders"]) == len(res.orders)
    assert set(back["metrics"]) == set(METRIC_KEYS)
    for t_dict, t in zip(back["trades"], res.trades, strict=True):
        assert t_dict["pnl"] == approx(t.pnl)
        assert t_dict["entry_time"] == t.entry_time.isoformat()
        assert t_dict["side"] == "buy"
    for o_dict, o in zip(back["orders"], res.orders, strict=True):
        assert o_dict["id"] == o.id and o_dict["status"] == o.status.value
    assert back["fill_on"] == "next_open" and back["quote_currency"] == "KRW"
    assert back["skipped_signals"] == res.skipped_signals


def test_to_dict_converts_non_finite_metrics_to_none(daily_df):
    res = run_bt("sma_cross", {BTC: daily_df})
    res.metrics["profit_factor"] = math.inf
    res.metrics["calmar"] = math.nan
    d = res.to_dict()
    assert d["metrics"]["profit_factor"] is None and d["metrics"]["calmar"] is None
    json.dumps(d)


def test_summary_is_plain_korean_text(daily_df):
    res = run_bt("bollinger", {BTC: daily_df})
    text = res.summary()
    for needle in (
        "백테스트 결과",
        "bollinger",
        "심볼",
        BTC,
        "기간",
        "초기 자산",
        "최종 자산",
        "거래",
        "성과 지표",
        "샤프 비율",
    ):
        assert needle in text
    assert "[" not in text.splitlines()[-1]  # rich 마크업 없음
    assert res.total_return == approx(res.final_equity / INITIAL - 1)


def test_equity_curve_dtype_and_index(daily_df):
    res = run_bt("sma_cross", {BTC: daily_df})
    assert res.equity_curve.dtype == float
    assert res.equity_curve.index.name == "timestamp"
    assert str(res.equity_curve.index.tz) == "UTC"
    assert res.equity_curve.is_monotonic_increasing or True  # 값은 단조가 아니어도 됨; index 가 단조
    assert res.equity_curve.index.is_monotonic_increasing
    assert (res.equity_curve > 0).all()


def test_backtester_reusable_for_multiple_runs(daily_df, eth_daily_df):
    bt = Backtester(
        create_strategy("sma_cross", SHORT_PARAMS["sma_cross"]), make_risk(), initial_cash=INITIAL
    )
    a = bt.run({BTC: daily_df})
    b = bt.run({ETH: eth_daily_df})
    c = bt.run({BTC: daily_df})
    assert a.symbols == [BTC] and b.symbols == [ETH]
    pd.testing.assert_series_equal(a.equity_curve, c.equity_curve)
    assert float(b.equity_curve.iloc[0]) == INITIAL
