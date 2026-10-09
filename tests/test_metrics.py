"""backtest/metrics.py 테스트.

- 순수 수학 검증은 아주 짧은 손계산 벡터([100, 110, 99, 120] 등)로 한다 (계약 허용 범위).
- 실제 데이터 검증은 conftest 의 실제 Upbit KRW-BTC 일봉(daily_df)으로 만든 buy&hold 자산 곡선을 쓴다.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
from pytest import approx

from tradingbot.backtest.metrics import (
    METRIC_KEYS,
    compute_metrics,
    drawdown_series,
    format_metrics,
    periods_per_year,
)
from tradingbot.exceptions import DataError
from tradingbot.models import AssetClass, OrderSide, Trade

UTC = timezone.utc
T0 = datetime(2024, 1, 1, tzinfo=UTC)
EXPECTED_KEYS = (
    "total_return",
    "cagr",
    "max_drawdown",
    "max_drawdown_duration_bars",
    "volatility",
    "sharpe",
    "sortino",
    "calmar",
    "win_rate",
    "profit_factor",
    "num_trades",
    "avg_pnl",
    "avg_pnl_pct",
    "avg_win",
    "avg_loss",
    "best_trade_pct",
    "worst_trade_pct",
    "avg_holding_hours",
    "exposure",
)


def daily_series(values: list[float], start: datetime = T0) -> pd.Series:
    idx = pd.DatetimeIndex([start + timedelta(days=i) for i in range(len(values))])
    return pd.Series(values, index=idx, dtype=float)


def make_trade(
    entry: float,
    exit_: float,
    *,
    qty: float = 1.0,
    fee: float = 0.0,
    hours: float = 24.0,
    symbol: str = "KRW-BTC",
) -> Trade:
    return Trade(
        symbol=symbol,
        side=OrderSide.BUY,
        quantity=qty,
        entry_price=entry,
        exit_price=exit_,
        entry_time=T0,
        exit_time=T0 + timedelta(hours=hours),
        fee=fee,
    )


# ============================================================================ periods_per_year
class TestPeriodsPerYear:
    @pytest.mark.parametrize(
        "interval, expected",
        [
            ("1d", 365.0),
            ("1h", 8760.0),
            ("4h", 2190.0),
            ("1m", 525600.0),
            ("5m", 105120.0),
            ("1w", 365.0 / 7.0),
        ],
    )
    def test_crypto_table(self, interval, expected):
        assert periods_per_year(interval, AssetClass.CRYPTO) == approx(expected)

    @pytest.mark.parametrize(
        "interval, expected",
        [
            ("1d", 252.0),
            ("1h", 252.0 * 6.5),
            ("1m", 252.0 * 390),
            ("5m", 252.0 * 78),
            ("30m", 252.0 * 13),
            ("1w", 52.0),
        ],
    )
    def test_stock_table(self, interval, expected):
        assert periods_per_year(interval, AssetClass.STOCK) == approx(expected)

    def test_accepts_string_asset_class(self):
        assert periods_per_year("1d", "crypto") == 365.0
        assert periods_per_year("1d", "stock") == 252.0

    def test_unknown_interval_raises(self):
        with pytest.raises(ValueError):
            periods_per_year("2h", AssetClass.CRYPTO)

    def test_unknown_asset_class_raises(self):
        with pytest.raises(ValueError):
            periods_per_year("1d", "bond")


# ============================================================================ compute_metrics (손계산)
class TestHandVectors:
    def test_keys_match_contract_in_order(self):
        m = compute_metrics(daily_series([100, 110, 99, 120]), [], 365)
        assert tuple(m) == EXPECTED_KEYS
        assert METRIC_KEYS == EXPECTED_KEYS

    def test_equity_100_110_99_120(self):
        eq = daily_series([100, 110, 99, 120])
        m = compute_metrics(eq, [], 365, exposure=0.5)

        assert m["total_return"] == approx(0.2)
        assert m["max_drawdown"] == approx(0.1)
        assert m["max_drawdown_duration_bars"] == 1

        rets = np.array([0.1, -0.1, 21 / 99])
        mean = rets.mean()
        std = rets.std(ddof=1)
        assert m["volatility"] == approx(std * math.sqrt(365))
        assert m["sharpe"] == approx(mean / std * math.sqrt(365))
        downside_dev = math.sqrt((0.0**2 + 0.1**2 + 0.0**2) / 2)
        assert m["sortino"] == approx(mean / downside_dev * math.sqrt(365))

        years = 3 / 365.25
        cagr = 1.2 ** (1 / years) - 1
        assert m["cagr"] == approx(cagr, rel=1e-9)
        assert m["calmar"] == approx(cagr / 0.1, rel=1e-9)
        assert m["exposure"] == 0.5
        # 거래 없음
        assert m["num_trades"] == 0
        assert m["win_rate"] == 0.0
        assert m["profit_factor"] == 0.0
        assert m["avg_holding_hours"] == 0.0

    def test_constant_equity_all_zero(self):
        m = compute_metrics(daily_series([100, 100, 100, 100]), [], 365)
        for key in ("total_return", "cagr", "max_drawdown", "volatility", "sharpe", "sortino", "calmar"):
            assert m[key] == 0.0, key
        assert m["max_drawdown_duration_bars"] == 0

    def test_single_point(self):
        m = compute_metrics(daily_series([100]), [], 365)
        assert m["total_return"] == 0.0
        assert m["sharpe"] == 0.0
        assert m["cagr"] == 0.0
        assert m["max_drawdown"] == 0.0

    def test_two_points_no_std(self):
        # 수익률이 1개뿐이면 표준편차를 정의할 수 없다 → 0
        m = compute_metrics(daily_series([100, 120]), [], 365)
        assert m["total_return"] == approx(0.2)
        assert m["sharpe"] == 0.0
        assert m["volatility"] == 0.0

    def test_monotonic_increase_no_drawdown(self):
        m = compute_metrics(daily_series([100, 101, 102, 103]), [], 365)
        assert m["max_drawdown"] == 0.0
        assert m["max_drawdown_duration_bars"] == 0
        assert m["calmar"] == 0.0
        assert m["sortino"] == 0.0  # 하방 수익률 없음
        assert m["sharpe"] > 0

    def test_declining_equity_negative_sharpe(self):
        m = compute_metrics(daily_series([100, 95, 92, 88]), [], 365)
        assert m["sharpe"] < 0
        assert m["sortino"] < 0
        assert m["total_return"] == approx(-0.12)
        assert m["max_drawdown"] == approx(0.12)
        assert m["max_drawdown_duration_bars"] == 3

    def test_drawdown_duration_recovered_and_unrecovered(self):
        assert (
            compute_metrics(daily_series([100, 90, 95, 80, 100, 100, 99]), [], 365)[
                "max_drawdown_duration_bars"
            ]
            == 3
        )
        assert compute_metrics(daily_series([100, 90, 80, 85]), [], 365)["max_drawdown_duration_bars"] == 3
        assert (
            compute_metrics(daily_series([100, 90, 100, 95, 90, 85, 100]), [], 365)[
                "max_drawdown_duration_bars"
            ]
            == 3
        )

    def test_drawdown_series_values(self):
        dd = drawdown_series(daily_series([100, 110, 99, 120]))
        assert list(dd.round(12)) == approx([0.0, 0.0, 0.1, 0.0])
        assert dd.index.equals(daily_series([100, 110, 99, 120]).index)

    def test_cagr_without_datetime_index_uses_bar_count(self):
        m = compute_metrics(pd.Series([100.0, 110.0, 99.0, 120.0]), [], 365)
        assert m["cagr"] == approx(1.2 ** (365 / 3) - 1, rel=1e-9)

    def test_cagr_total_loss(self):
        eq = daily_series([100, 50, 0.0001])
        m = compute_metrics(eq, [], 365)
        assert m["cagr"] == approx(-1.0, abs=1e-6)

    def test_cagr_same_timestamp_is_zero(self):
        idx = pd.DatetimeIndex([T0, T0])
        m = compute_metrics(pd.Series([100.0, 120.0], index=idx), [], 365)
        assert m["cagr"] == 0.0
        assert m["calmar"] == 0.0

    def test_accepts_list_and_ndarray(self):
        assert compute_metrics([100, 110, 99, 120], [], 365)["max_drawdown"] == approx(0.1)
        assert compute_metrics(np.array([100, 110, 99, 120]), [], 365)["total_return"] == approx(0.2)

    def test_elapsed_time_respects_timezone(self):
        # naive index 와 aware index 가 같은 경과 시간을 준다
        aware = daily_series([100, 110, 99, 120])
        naive = pd.Series([100.0, 110.0, 99.0, 120.0], index=aware.index.tz_localize(None))
        assert compute_metrics(aware, [], 365)["cagr"] == approx(compute_metrics(naive, [], 365)["cagr"])


class TestValidation:
    def test_empty_raises(self):
        with pytest.raises(DataError):
            compute_metrics(pd.Series([], dtype=float), [], 365)
        with pytest.raises(DataError):
            compute_metrics([], [], 365)

    def test_nan_raises(self):
        with pytest.raises(DataError):
            compute_metrics(daily_series([100, float("nan"), 120]), [], 365)

    def test_inf_raises(self):
        with pytest.raises(DataError):
            compute_metrics(daily_series([100, float("inf"), 120]), [], 365)

    def test_non_numeric_raises(self):
        with pytest.raises(DataError):
            compute_metrics(pd.Series(["a", "b"]), [], 365)

    @pytest.mark.parametrize("ppy", [0, -1, float("nan"), float("inf"), "365", True, None])
    def test_bad_periods_per_year(self, ppy):
        with pytest.raises(ValueError):
            compute_metrics(daily_series([100, 110]), [], ppy)

    def test_bad_exposure(self):
        with pytest.raises(ValueError):
            compute_metrics(daily_series([100, 110]), [], 365, exposure=float("nan"))

    def test_exposure_clamped(self):
        assert compute_metrics(daily_series([100, 110]), [], 365, exposure=1.7)["exposure"] == 1.0
        assert compute_metrics(daily_series([100, 110]), [], 365, exposure=-0.2)["exposure"] == 0.0


# ============================================================================ 거래 통계 (손계산)
class TestTradeStats:
    def test_hand_vector_trades(self):
        trades = [
            make_trade(100, 110, hours=24),  # +10 (+10%)
            make_trade(100, 95, hours=48),  # -5 (-5%)
            make_trade(200, 220, fee=2.0, hours=12),  # +18 (+9%)
        ]
        m = compute_metrics(daily_series([100, 110, 99, 120]), trades, 365, exposure=0.0)
        assert m["num_trades"] == 3
        assert m["win_rate"] == approx(2 / 3)
        assert m["profit_factor"] == approx(28 / 5)
        assert m["avg_pnl"] == approx(23 / 3)
        assert m["avg_pnl_pct"] == approx((0.10 - 0.05 + 0.09) / 3)
        assert m["avg_win"] == approx(14.0)
        assert m["avg_loss"] == approx(-5.0)
        assert m["best_trade_pct"] == approx(0.10)
        assert m["worst_trade_pct"] == approx(-0.05)
        assert m["avg_holding_hours"] == approx(28.0)

    def test_profit_factor_inf_without_losses(self):
        m = compute_metrics(daily_series([100, 110]), [make_trade(100, 110)], 365)
        assert math.isinf(m["profit_factor"]) and m["profit_factor"] > 0
        assert m["win_rate"] == 1.0
        assert m["avg_loss"] == 0.0

    def test_profit_factor_zero_with_only_losses(self):
        m = compute_metrics(daily_series([100, 110]), [make_trade(100, 90)], 365)
        assert m["profit_factor"] == 0.0
        assert m["win_rate"] == 0.0
        assert m["avg_win"] == 0.0
        assert m["avg_loss"] == approx(-10.0)

    def test_breakeven_trade_is_not_a_win(self):
        m = compute_metrics(daily_series([100, 110]), [make_trade(100, 100), make_trade(100, 101)], 365)
        assert m["win_rate"] == 0.5
        assert m["profit_factor"] == math.inf  # 손실 거래 없음 (0 손익은 손실이 아님)

    def test_exposure_fallback_from_trades(self):
        eq = daily_series([100.0] * 10)
        t = Trade(
            symbol="KRW-BTC",
            side=OrderSide.BUY,
            quantity=1.0,
            entry_price=100.0,
            exit_price=101.0,
            entry_time=T0 + timedelta(days=2),
            exit_time=T0 + timedelta(days=5),
        )
        # 진입 bar 포함, 청산 bar 제외 → 2, 3, 4 = 3 bars / 10
        assert compute_metrics(eq, [t], 365)["exposure"] == approx(0.3)
        # 겹치는 거래는 이중 계산하지 않는다
        t2 = Trade(
            symbol="KRW-ETH",
            side=OrderSide.BUY,
            quantity=1.0,
            entry_price=100.0,
            exit_price=101.0,
            entry_time=T0 + timedelta(days=4),
            exit_time=T0 + timedelta(days=7),
        )
        assert compute_metrics(eq, [t, t2], 365)["exposure"] == approx(0.5)
        # 명시값이 우선
        assert compute_metrics(eq, [t], 365, exposure=0.9)["exposure"] == 0.9

    def test_exposure_fallback_without_datetime_index_is_zero(self):
        assert compute_metrics(pd.Series([100.0, 101.0]), [make_trade(100, 101)], 365)["exposure"] == 0.0


# ============================================================================ 실제 데이터
class TestRealData:
    def test_buy_and_hold_curve_from_real_candles(self, daily_df):
        """실제 KRW-BTC 일봉 종가로 만든 buy&hold 자산 곡선의 지표를 독립 계산과 비교한다."""
        close = daily_df["close"].to_numpy()
        equity = pd.Series(10_000_000 * close / close[0], index=pd.DatetimeIndex(daily_df["timestamp"]))
        m = compute_metrics(equity, [], 365)

        assert m["total_return"] == approx(close[-1] / close[0] - 1)
        rets = np.diff(equity.to_numpy()) / equity.to_numpy()[:-1]
        assert m["sharpe"] == approx(rets.mean() / rets.std(ddof=1) * math.sqrt(365))
        assert m["volatility"] == approx(rets.std(ddof=1) * math.sqrt(365))

        # 최대 낙폭 / 지속 기간을 반복문으로 독립 계산
        peak = -math.inf
        mdd = 0.0
        run = longest = 0
        for v in equity.to_numpy():
            peak = max(peak, v)
            dd = 1 - v / peak
            mdd = max(mdd, dd)
            run = run + 1 if dd > 0 else 0
            longest = max(longest, run)
        assert m["max_drawdown"] == approx(mdd)
        assert m["max_drawdown_duration_bars"] == longest
        assert 0 <= m["max_drawdown"] < 1

        days = (equity.index[-1] - equity.index[0]).total_seconds() / 86400
        assert m["cagr"] == approx((1 + m["total_return"]) ** (365.25 / days) - 1, rel=1e-9)
        assert m["exposure"] == 0.0
        assert all(math.isfinite(v) for v in m.values())
        json.dumps(m)

    def test_trade_stats_from_real_closes(self, daily_df):
        """실제 종가로 진입/청산한 거래들의 통계를 직접 계산한 값과 비교한다."""
        close = daily_df["close"].to_numpy()
        ts = [t.to_pydatetime() for t in daily_df["timestamp"]]
        pairs = [(10, 20), (30, 33), (50, 70), (100, 101), (150, 199)]
        trades = [
            Trade(
                symbol="KRW-BTC",
                side=OrderSide.BUY,
                quantity=0.01,
                entry_price=float(close[a]),
                exit_price=float(close[b]),
                entry_time=ts[a],
                exit_time=ts[b],
                fee=0.01 * (close[a] + close[b]) * 0.0005,
            )
            for a, b in pairs
        ]
        equity = pd.Series(10_000_000 * close / close[0], index=pd.DatetimeIndex(daily_df["timestamp"]))
        m = compute_metrics(equity, trades, 365)
        pnls = [t.pnl for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        assert m["num_trades"] == 5
        assert m["win_rate"] == approx(len(wins) / 5)
        assert m["avg_pnl"] == approx(sum(pnls) / 5)
        assert m["avg_pnl_pct"] == approx(sum(t.pnl_pct for t in trades) / 5)
        assert m["best_trade_pct"] == approx(max(t.pnl_pct for t in trades))
        assert m["worst_trade_pct"] == approx(min(t.pnl_pct for t in trades))
        assert m["avg_holding_hours"] == approx(sum(b - a for a, b in pairs) * 24 / 5)
        if losses:
            assert m["profit_factor"] == approx(sum(wins) / -sum(losses))
            assert m["avg_loss"] == approx(sum(losses) / len(losses))
        else:
            assert m["profit_factor"] == math.inf
        if wins:
            assert m["avg_win"] == approx(sum(wins) / len(wins))
        held = sum(b - a for a, b in pairs)
        assert m["exposure"] == approx(held / len(close))


# ============================================================================ format_metrics
class TestFormat:
    def test_contains_korean_labels_and_values(self):
        m = compute_metrics(daily_series([100, 110, 99, 120]), [make_trade(100, 110)], 365, exposure=0.25)
        text = format_metrics(m)
        lines = text.splitlines()
        assert len(lines) == len(EXPECTED_KEYS)
        assert "총 수익률" in text and "+20.00%" in text
        assert "최대 낙폭(MDD)" in text and "10.00%" in text
        assert "시장 노출 비율" in text and "25.00%" in text
        assert "거래 횟수" in text
        assert "∞" in text  # profit_factor inf
        assert all(" : " in line for line in lines)

    def test_handles_none_nan_and_unknown_keys(self):
        text = format_metrics({"total_return": None, "sharpe": float("nan"), "custom_metric": 1.2345})
        assert "총 수익률" in text and " : -" in text
        assert "custom_metric" in text and "1.23" in text

    def test_negative_infinity_and_empty(self):
        assert "-∞" in format_metrics({"calmar": -math.inf})
        assert format_metrics({}) == "(지표 없음)"

    def test_money_and_hours_formatting(self):
        text = format_metrics({"avg_pnl": 12345.678, "avg_loss": -0.5, "avg_holding_hours": 36.25})
        assert "+12,346" in text
        assert "-0.50" in text
        assert "36.2" in text or "36.3" in text
