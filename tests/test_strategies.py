"""전략 테스트.

- 레지스트리 (create_strategy) 가 모든 키에 대해 동작
- 파라미터 검증 (ValueError / ConfigError)
- prepare: 복사본 반환, 입력 불변, 지표 컬럼 추가
- 워밍업 / NaN → HOLD
- 미래 참조 없음: 행 i 에서 잘라 generate_signal 한 결과 == 전체 prepare 후 signal_at(i)
- 실시간 윈도우 vs 백테스트: 엔진처럼 최근 W 개만 넘긴 generate_signal 이 전체 이력 signal_at(i) 와 같은지.
  rolling 전략은 W=warmup 에서 이미 같고, ewm 전략(ema_cross/rsi/macd) 은 W=recommended_candles 에서 같다
  (W=warmup 에서는 시드 효과로 지표값이 달라진다 → recommended_candles 가 필요한 이유)
- 각 전략의 규칙을 실제 Upbit 캔들(conftest 픽스처) 위에서 독립적으로 재계산해 비교
- 변동성 돌파: STOP BUY + stop_offset == k*(high-low) + max_holding_bars == 1, MA 필터 HOLD, SELL 없음
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any

import numpy as np
import pandas as pd
import pytest

from tradingbot.config import EngineConfig
from tradingbot.exceptions import ConfigError, DataError
from tradingbot.models import OrderType, Signal, SignalAction
from tradingbot.strategies import (
    BaseStrategy,
    available_strategies,
    candles_to_df,
    create_strategy,
    get_strategy_class,
)
from tradingbot.strategies import indicators as ind
from tradingbot.strategies.bollinger import BollingerStrategy
from tradingbot.strategies.macd import MACDStrategy
from tradingbot.strategies.rsi import RSIStrategy
from tradingbot.strategies.sma_cross import EMACrossStrategy, SMACrossStrategy
from tradingbot.strategies.volatility_breakout import VolatilityBreakoutStrategy

SYM = "KRW-BTC"

EXPECTED_CLASSES: dict[str, type[BaseStrategy]] = {
    "sma_cross": SMACrossStrategy,
    "ema_cross": EMACrossStrategy,
    "rsi": RSIStrategy,
    "bollinger": BollingerStrategy,
    "macd": MACDStrategy,
    "volatility_breakout": VolatilityBreakoutStrategy,
}

EXPECTED_DEFAULTS: dict[str, dict[str, Any]] = {
    "sma_cross": {"fast": 10, "slow": 30},
    "ema_cross": {"fast": 12, "slow": 26},
    "rsi": {"period": 14, "oversold": 30, "overbought": 70},
    "bollinger": {"period": 20, "num_std": 2.0, "mode": "reversion"},
    "macd": {"fast": 12, "slow": 26, "signal": 9},
    "volatility_breakout": {"k": 0.5, "ma_period": 0},
}

# 200개 캔들 안에서 교차가 충분히 발생하도록 짧은 기간을 쓰는 설정 + 기본값
SHORT_CONFIGS: list[tuple[str, dict[str, Any]]] = [
    ("sma_cross", {"fast": 5, "slow": 12}),
    ("ema_cross", {"fast": 5, "slow": 12}),
    ("rsi", {"period": 7, "oversold": 40, "overbought": 60}),
    ("bollinger", {"period": 10, "num_std": 1.5, "mode": "reversion"}),
    ("bollinger", {"period": 10, "num_std": 1.5, "mode": "breakout"}),
    ("macd", {"fast": 5, "slow": 13, "signal": 4}),
    ("volatility_breakout", {"k": 0.5, "ma_period": 0}),
    ("volatility_breakout", {"k": 0.6, "ma_period": 10}),
]
ALL_CONFIGS: list[tuple[str, dict[str, Any]]] = SHORT_CONFIGS + [(n, {}) for n in EXPECTED_DEFAULTS]


def _cfg_id(cfg: tuple[str, dict[str, Any]]) -> str:
    name, params = cfg
    return name if not params else f"{name}[{','.join(f'{k}={v}' for k, v in params.items())}]"


def _approx_equal(a: Any, b: Any) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-9)
    return a == b


def assert_same_signal(a: Signal, b: Signal, *, compare_details: bool = True) -> None:
    assert a.action == b.action
    assert a.order_type == b.order_type
    assert a.price == b.price
    assert _approx_equal(a.stop_offset, b.stop_offset)
    assert a.stop_loss == b.stop_loss and a.take_profit == b.take_profit
    assert a.max_holding_bars == b.max_holding_bars
    assert a.strength == b.strength
    assert a.symbol == b.symbol
    if compare_details:
        assert a.reason == b.reason
        assert set(a.meta) == set(b.meta)
        for k in a.meta:
            assert _approx_equal(a.meta[k], b.meta[k]), f"meta[{k}] {a.meta[k]!r} != {b.meta[k]!r}"


# ====================================================================== 레지스트리


class TestRegistry:
    def test_available_strategies_lists_all_six(self):
        assert available_strategies() == sorted(EXPECTED_CLASSES)

    @pytest.mark.parametrize("name", sorted(EXPECTED_CLASSES))
    def test_create_strategy_every_key(self, name):
        strat = create_strategy(name)
        assert isinstance(strat, EXPECTED_CLASSES[name])
        assert isinstance(strat, BaseStrategy)
        assert strat.name == name
        assert strat.params == EXPECTED_DEFAULTS[name]
        assert strat.default_params == EXPECTED_DEFAULTS[name]
        assert strat.description
        assert strat.warmup >= 1
        assert get_strategy_class(name) is EXPECTED_CLASSES[name]
        assert name in repr(strat) or type(strat).__name__ in repr(strat)

    def test_create_strategy_is_case_insensitive(self):
        assert isinstance(create_strategy("SMA_CROSS"), SMACrossStrategy)

    def test_unknown_strategy_raises_config_error(self):
        with pytest.raises(ConfigError):
            create_strategy("does_not_exist")

    @pytest.mark.parametrize("name", sorted(EXPECTED_CLASSES))
    def test_unknown_param_raises(self, name):
        with pytest.raises(ValueError):
            EXPECTED_CLASSES[name](bogus=1)
        with pytest.raises(ConfigError):
            create_strategy(name, {"bogus": 1})

    def test_params_are_normalized(self):
        s = create_strategy("sma_cross", {"fast": "5", "slow": 12.0})
        assert s.params == {"fast": 5, "slow": 12}
        assert isinstance(s.params["fast"], int) and isinstance(s.params["slow"], int)
        v = create_strategy("volatility_breakout", {"k": "0.7", "ma_period": "20"})
        assert v.params == {"k": 0.7, "ma_period": 20}
        b = create_strategy("bollinger", {"mode": " Breakout "})
        assert b.params["mode"] == "breakout"

    def test_default_params_not_mutated_by_instance(self):
        before = dict(SMACrossStrategy.default_params)
        SMACrossStrategy(fast=3, slow=7)
        assert SMACrossStrategy.default_params == before


# ====================================================================== 파라미터 검증


INVALID_PARAMS: list[tuple[str, dict[str, Any]]] = [
    ("sma_cross", {"fast": 30, "slow": 10}),
    ("sma_cross", {"fast": 10, "slow": 10}),
    ("sma_cross", {"fast": 0}),
    ("sma_cross", {"slow": -5}),
    ("sma_cross", {"fast": True}),
    ("sma_cross", {"fast": "abc"}),
    ("sma_cross", {"fast": 2.5}),
    ("ema_cross", {"fast": 26, "slow": 12}),
    ("ema_cross", {"fast": 0, "slow": 5}),
    ("rsi", {"period": 0}),
    ("rsi", {"period": 1}),  # RSI 가 0/100 만 취한다 (docs: 2 이상)
    ("rsi", {"oversold": 70, "overbought": 30}),
    ("rsi", {"oversold": 50, "overbought": 50}),
    ("rsi", {"oversold": -1}),
    ("rsi", {"oversold": 0}),  # RSI 는 0 에 사실상 닿지 않아 신호가 영원히 없다 (docs: 0 < oversold)
    ("rsi", {"overbought": 101}),
    ("rsi", {"overbought": 100}),  # docs: overbought < 100
    ("rsi", {"oversold": 0, "overbought": 100}),
    ("rsi", {"oversold": "x"}),
    ("bollinger", {"period": 1}),
    ("bollinger", {"num_std": 0}),
    ("bollinger", {"num_std": -2}),
    ("bollinger", {"mode": "foo"}),
    ("bollinger", {"mode": 3}),
    ("macd", {"fast": 26, "slow": 12}),
    ("macd", {"fast": 12, "slow": 12}),
    ("macd", {"signal": 0}),
    ("macd", {"signal": 1}),  # EMA(1) 항등 → 시그널 == MACD → 교차가 영원히 없는 죽은 설정
    ("macd", {"fast": None}),
    ("volatility_breakout", {"k": 0}),
    ("volatility_breakout", {"k": -0.1}),
    ("volatility_breakout", {"k": "x"}),
    ("volatility_breakout", {"k": True}),
    ("volatility_breakout", {"ma_period": -1}),
    ("volatility_breakout", {"ma_period": 1.5}),
]


@pytest.mark.parametrize("cfg", INVALID_PARAMS, ids=_cfg_id)
def test_invalid_params_raise(cfg):
    name, params = cfg
    with pytest.raises(ValueError):
        EXPECTED_CLASSES[name](**params)
    with pytest.raises(ConfigError):
        create_strategy(name, params)


def test_boundary_params_just_inside_limits_are_accepted():
    """거부 경계 바로 안쪽은 받아야 한다 (minimum=2, 열린 구간 (0, 100))."""
    assert MACDStrategy(signal=2).signal == 2
    assert RSIStrategy(period=2).period == 2
    r = RSIStrategy(oversold=0.5, overbought=99.5)
    assert (r.oversold, r.overbought) == (0.5, 99.5)


def test_macd_signal_one_would_never_fire(daily_df):
    """signal=1 을 거부하는 이유: 지표 수준에서는 허용되지만 히스토그램이 항상 0 이라 교차가 없다."""
    macd_line, sig_line, hist = ind.macd(daily_df["close"], 12, 26, 1)
    assert np.nanmax(np.abs(hist.to_numpy())) == 0.0
    assert not ind.crossover(macd_line, sig_line).any() and not ind.crossunder(macd_line, sig_line).any()


# ====================================================================== prepare


@pytest.mark.parametrize("cfg", ALL_CONFIGS, ids=_cfg_id)
def test_prepare_returns_copy_and_adds_columns(cfg, daily_df):
    name, params = cfg
    strat = create_strategy(name, params)
    before = daily_df.copy(deep=True)
    prepared = strat.prepare(daily_df)
    assert prepared is not daily_df
    pd.testing.assert_frame_equal(daily_df, before)  # 입력 불변
    assert len(prepared) == len(daily_df)
    assert prepared.index.equals(daily_df.index)
    for c in daily_df.columns:
        pd.testing.assert_series_equal(prepared[c], daily_df[c])
    added = [c for c in prepared.columns if c not in daily_df.columns]
    assert added, "지표 컬럼이 추가되어야 함"
    assert all(c == c.lower() and " " not in c for c in added), "지표 컬럼은 소문자_스네이크"


@pytest.mark.parametrize("name", sorted(EXPECTED_CLASSES))
def test_prepare_requires_price_columns(name):
    strat = create_strategy(name)
    with pytest.raises(DataError):
        strat.prepare(pd.DataFrame({"volume": [1.0, 2.0, 3.0]}))


@pytest.mark.parametrize("name", sorted(EXPECTED_CLASSES))
def test_prepare_on_empty_frame(name):
    strat = create_strategy(name)
    empty = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    out = strat.prepare(empty)
    assert len(out) == 0
    with pytest.raises(IndexError):
        strat.signal_at(SYM, out, 0)


@pytest.mark.parametrize("name", sorted(EXPECTED_CLASSES))
def test_signal_at_requires_prepared_frame(name, daily_df):
    strat = create_strategy(name)
    with pytest.raises(DataError):
        strat.signal_at(SYM, daily_df, len(daily_df) - 1)


@pytest.mark.parametrize("name", sorted(EXPECTED_CLASSES))
def test_signal_at_index_out_of_range(name, daily_df):
    strat = create_strategy(name)
    prepared = strat.prepare(daily_df)
    with pytest.raises(IndexError):
        strat.signal_at(SYM, prepared, len(prepared))
    with pytest.raises(IndexError):
        strat.signal_at(SYM, prepared, -1)


# ====================================================================== 워밍업 / NaN


@pytest.mark.parametrize("cfg", ALL_CONFIGS, ids=_cfg_id)
def test_warmup_rows_are_hold(cfg, daily_df):
    name, params = cfg
    strat = create_strategy(name, params)
    prepared = strat.prepare(daily_df)
    for i in range(min(strat.warmup - 1, len(prepared))):
        sig = strat.signal_at(SYM, prepared, i)
        assert sig.is_hold and sig.action == SignalAction.HOLD
        assert sig.strength == 0.0
        assert sig.symbol == SYM
        assert "워밍업" in sig.reason
    # 워밍업 직후 행부터는 지표가 유효하므로 NaN 사유 HOLD 가 없어야 한다
    for i in range(strat.warmup - 1, len(prepared)):
        assert "NaN" not in strat.signal_at(SYM, prepared, i).reason


@pytest.mark.parametrize("cfg", ALL_CONFIGS, ids=_cfg_id)
def test_generate_signal_with_too_little_data_is_hold(cfg, daily_df):
    name, params = cfg
    strat = create_strategy(name, params)
    short = daily_df.iloc[: strat.warmup - 1].reset_index(drop=True)
    sig = strat.generate_signal(SYM, short)
    assert sig.is_hold and sig.strength == 0.0
    assert "데이터 부족" in sig.reason
    assert strat.generate_signal(SYM, short.iloc[:0]).is_hold


@pytest.mark.parametrize(
    ("name", "params", "column"),
    [
        ("sma_cross", {"fast": 5, "slow": 12}, "sma_slow"),
        ("ema_cross", {"fast": 5, "slow": 12}, "ema_fast"),
        ("rsi", {"period": 7}, "rsi"),
        ("bollinger", {"period": 10}, "bb_mid"),
        ("macd", {"fast": 5, "slow": 13, "signal": 4}, "macd_signal"),
        ("volatility_breakout", {"ma_period": 10}, "vb_ma"),
        ("volatility_breakout", {}, "vb_range"),
    ],
)
def test_nan_indicator_is_hold(name, params, column, daily_df):
    strat = create_strategy(name, params)
    prepared = strat.prepare(daily_df)
    i = len(prepared) - 1
    prepared.loc[i, column] = np.nan
    sig = strat.signal_at(SYM, prepared, i)
    assert sig.is_hold
    assert "NaN" in sig.reason


# ====================================================================== 미래 참조 없음


@pytest.mark.parametrize("cfg", ALL_CONFIGS, ids=_cfg_id)
def test_no_lookahead_truncated_equals_full(cfg, daily_df):
    """행 i 까지 자른 df 로 generate_signal == 전체 prepare 후 signal_at(i)."""
    name, params = cfg
    strat = create_strategy(name, params)
    prepared = strat.prepare(daily_df)
    n = len(prepared)
    for i in range(n):
        truncated = daily_df.iloc[: i + 1].reset_index(drop=True)
        live = strat.generate_signal(SYM, truncated)
        back = strat.signal_at(SYM, prepared, i)
        # 워밍업 이전에는 둘 다 HOLD 이지만 사유 문구("데이터 부족" vs "워밍업") 는 다를 수 있다
        assert_same_signal(live, back, compare_details=i >= strat.warmup - 1)


@pytest.mark.parametrize("cfg", SHORT_CONFIGS, ids=_cfg_id)
def test_no_lookahead_on_hourly_data(cfg, candles_df):
    name, params = cfg
    strat = create_strategy(name, params)
    prepared = strat.prepare(candles_df)
    for i in range(strat.warmup - 1, len(prepared), 7):
        truncated = candles_df.iloc[: i + 1].reset_index(drop=True)
        assert_same_signal(strat.generate_signal(SYM, truncated), strat.signal_at(SYM, prepared, i))


@pytest.mark.parametrize("cfg", SHORT_CONFIGS, ids=_cfg_id)
def test_future_rows_do_not_change_past_signals(cfg, daily_df):
    """뒤쪽 행의 값을 바꿔도 앞쪽 행의 신호는 그대로여야 한다 (prepare 가 shift/rolling 만 사용)."""
    name, params = cfg
    strat = create_strategy(name, params)
    base = strat.prepare(daily_df)
    cut = len(daily_df) - 30
    altered = daily_df.copy()
    # 미래 구간만 뒤집어서 (실제 값의 순서만 바꿈) 과거 행 신호가 영향을 받지 않는지 확인
    for c in ("open", "high", "low", "close", "volume"):
        altered.loc[cut:, c] = altered.loc[cut:, c].to_numpy()[::-1]
    altered_prepared = strat.prepare(altered)
    for i in range(cut):
        assert_same_signal(strat.signal_at(SYM, base, i), strat.signal_at(SYM, altered_prepared, i))


# ====================================================================== 실시간 윈도우 vs 백테스트 (recommended_candles)
#
# 실시간 엔진은 최근 candle_limit 개만 generate_signal 에 넘기고, 백테스터는 전체 이력을 prepare 한 뒤 signal_at(i)
# 를 부른다. prefix 테스트(위) 는 "미래를 잘라도 같다" 만 보장한다. 여기서는 "과거를 잘라도(슬라이딩 윈도우) 같은가"
# 를 본다: rolling 전략은 W=warmup 이면 같고, ewm 전략은 시드 효과 때문에 W=recommended_candles 가 필요하다.

EWM_NAMES = ("ema_cross", "rsi", "macd")
EWM_SHORT_CONFIGS = [cfg for cfg in SHORT_CONFIGS if cfg[0] in EWM_NAMES]
ROLLING_SHORT_CONFIGS = [cfg for cfg in SHORT_CONFIGS if cfg[0] not in EWM_NAMES]
_META_KEYS = {
    "ema_cross": ("ema_fast", "ema_slow"),
    "macd": ("macd", "macd_signal", "macd_hist"),
    "rsi": ("rsi", "prev_rsi"),
}


def _window(df: pd.DataFrame, i: int, w: int) -> pd.DataFrame:
    """엔진이 넘기는 것과 같은 최근 w 개 캔들 (행 i 가 마지막)."""
    return df.iloc[i - w + 1 : i + 1].reset_index(drop=True)


def _window_bound(name: str, df: pd.DataFrame) -> float:
    """recommended_candles 윈도우에서 허용되는 지표 오차 상한.

    EMA 의 윈도우 오차 = (1-α)^(W-1) × |시드 - 전체 이력값| ≤ EWM_SETTLE_TOL × (종가 범위).
    ema_cross 는 두 EMA 의 차이(×2), macd 히스토그램은 MACD 와 그 EMA 의 차이이므로 여유 있게 ×8.
    RSI 는 0~100 척도의 절대 오차 (평균 상승/하락폭의 상대 오차 ≤ tol 이 RSI 0.1 포인트를 넘지 않는다).
    """
    if name == "rsi":
        return 0.1
    price_range = float(df["close"].max() - df["close"].min())
    return ind.EWM_SETTLE_TOL * price_range * (8.0 if name == "macd" else 2.0)


def _indicator_values(name: str, sig: Signal) -> list[float]:
    assert sig.meta, "워밍업 이후 신호에는 지표값 meta 가 있어야 함"
    return [float(sig.meta[k]) for k in _META_KEYS[name]]


def _cross_margin(name: str, strat: BaseStrategy, prepared: pd.DataFrame, i: int) -> float:
    """행 i-1, i 에서 교차 기준선까지의 최소 거리. 이보다 작은 오차로도 교차 판정은 뒤집힐 수 있다."""
    rows = (i - 1, i)
    if name == "ema_cross":
        return min(abs(prepared["ema_fast"].iat[j] - prepared["ema_slow"].iat[j]) for j in rows)
    if name == "macd":
        return min(abs(prepared["macd_hist"].iat[j]) for j in rows)
    levels = (strat.oversold, strat.overbought)
    return min(abs(prepared["rsi"].iat[j] - level) for j in rows for level in levels)


class TestRecommendedCandles:
    def test_default_values(self):
        # rolling 전략: warmup 과 같다
        assert SMACrossStrategy().recommended_candles == SMACrossStrategy().warmup == 31
        assert BollingerStrategy().recommended_candles == 21
        assert VolatilityBreakoutStrategy().recommended_candles == 1
        assert VolatilityBreakoutStrategy(ma_period=20).recommended_candles == 21
        # ewm 전략: warmup + 시드 가중치가 1e-5 이하가 되는 캔들 수
        # EMA(26): ceil(ln(1e-5) / ln(25/27)) = 150, Wilder RSI(14): ceil(ln(1e-5) / ln(13/14)) = 156
        assert EMACrossStrategy().recommended_candles == 27 + 150
        assert MACDStrategy().recommended_candles == 35 + 150
        assert RSIStrategy().recommended_candles == 16 + 156
        # MACD 는 slow 와 signal 중 긴 쪽의 EMA 시드가 가장 오래 남는다
        assert MACDStrategy(fast=5, slow=13, signal=20).recommended_candles == 33 + ind.ewm_settle_bars(
            2 / 21
        )
        assert EMACrossStrategy(fast=5, slow=12).recommended_candles == 13 + ind.ewm_settle_bars(2 / 13)
        assert RSIStrategy(period=7).recommended_candles == 9 + ind.ewm_settle_bars(1 / 7)

    @pytest.mark.parametrize("cfg", ALL_CONFIGS, ids=_cfg_id)
    def test_at_least_warmup_and_fits_default_candle_limit(self, cfg):
        name, params = cfg
        strat = create_strategy(name, params)
        assert isinstance(strat.recommended_candles, int)
        assert strat.recommended_candles >= strat.warmup
        if name in EWM_NAMES:
            assert strat.recommended_candles > strat.warmup
        else:
            assert strat.recommended_candles == strat.warmup
        # 기본 설정(candle_limit 300) 은 모든 내장 전략의 기본 파라미터에서 경고 없이 동작해야 한다
        assert strat.recommended_candles <= EngineConfig().candle_limit


@pytest.mark.parametrize("cfg", ROLLING_SHORT_CONFIGS, ids=_cfg_id)
def test_rolling_strategies_match_backtest_with_warmup_window(cfg, daily_df):
    """SMA / 볼린저 / 변동성 돌파: W=warmup 슬라이딩 윈도우의 신호가 전체 이력 신호와 같다 (사유/meta 까지)."""
    name, params = cfg
    strat = create_strategy(name, params)
    w = strat.warmup
    assert strat.recommended_candles == w
    prepared = strat.prepare(daily_df)
    for i in range(w - 1, len(daily_df)):
        assert_same_signal(
            strat.generate_signal(SYM, _window(daily_df, i, w)), strat.signal_at(SYM, prepared, i)
        )


@pytest.mark.parametrize("fixture", ["daily_df", "candles_df"])
@pytest.mark.parametrize("cfg", EWM_SHORT_CONFIGS, ids=_cfg_id)
def test_ewm_strategies_match_backtest_with_recommended_window(cfg, fixture, request):
    """ema_cross / rsi / macd: W=recommended_candles 슬라이딩 윈도우의 신호가 전체 이력 신호와 같다.

    지표값은 수렴 오차 상한 안에서 같아야 하고, 행동(action) 은 그 오차보다 작은 차이로 기준선에 걸린
    아슬아슬한 교차(near-tie) 를 빼면 같아야 한다 (그 밖의 행동 불일치 = 수렴 부족).
    """
    df = request.getfixturevalue(fixture)
    name, params = cfg
    strat = create_strategy(name, params)
    w = strat.recommended_candles
    assert strat.warmup < w <= len(df) - 50, "픽스처(200개) 안에서 비교할 bar 가 충분해야 한다"
    prepared = strat.prepare(df)
    bound = _window_bound(name, df)
    compared = 0
    for i in range(w - 1, len(df)):
        live = strat.generate_signal(SYM, _window(df, i, w))
        back = strat.signal_at(SYM, prepared, i)
        for lv, bv in zip(_indicator_values(name, live), _indicator_values(name, back), strict=True):
            assert abs(lv - bv) <= bound, (
                f"{name} bar {i}: live {lv!r} vs backtest {bv!r} (허용 오차 {bound:g})"
            )
        if live.action != back.action:
            margin = _cross_margin(name, strat, prepared, i)
            assert margin <= bound, (
                f"{name} bar {i}: live {live.action.value} vs backtest {back.action.value}, "
                f"기준선까지 거리 {margin:g} > 허용 오차 {bound:g} (수렴 부족)"
            )
        else:
            assert live.order_type == back.order_type and live.strength == back.strength
        compared += 1
    assert compared >= 50


@pytest.mark.parametrize("cfg", EWM_SHORT_CONFIGS, ids=_cfg_id)
def test_ewm_strategies_diverge_from_backtest_with_warmup_window(cfg, daily_df):
    """W=warmup (validate-config 가 통과시키는 최솟값) 은 수렴을 보장하지 않는다 → recommended_candles 가 필요한 이유.

    warmup 크기 윈도우에서는 시드 가중치가 (1-α)^(warmup-1) ≈ 10% 수준이라 지표값이 전체 이력과 뚜렷이 다르다.
    """
    name, params = cfg
    strat = create_strategy(name, params)
    w = strat.warmup
    prepared = strat.prepare(daily_df)
    bound = _window_bound(name, daily_df)
    worst = 0.0
    for i in range(w - 1, len(daily_df)):
        live = strat.generate_signal(SYM, _window(daily_df, i, w))
        back = strat.signal_at(SYM, prepared, i)
        worst = max(
            worst,
            max(
                abs(a - b)
                for a, b in zip(_indicator_values(name, live), _indicator_values(name, back), strict=True)
            ),
        )
    assert worst > bound, f"{name}: warmup 윈도우의 최대 지표 오차 {worst:g} 가 수렴 오차 {bound:g} 이하?"


def test_generate_signal_warns_once_below_recommended_candles(daily_df, caplog):
    """실시간 경로(generate_signal) 에 warmup 이상 recommended_candles 미만의 캔들이 오면 인스턴스당 1회 WARNING."""
    strat = EMACrossStrategy(fast=5, slow=12)
    short = daily_df.iloc[: strat.warmup + 5].reset_index(drop=True)
    with caplog.at_level(logging.WARNING, logger="tradingbot.strategies"):
        strat.generate_signal(SYM, short)
        strat.generate_signal(SYM, short)
    warned = [r for r in caplog.records if "recommended_candles" in r.getMessage()]
    assert len(warned) == 1
    assert warned[0].levelno == logging.WARNING and warned[0].name == "tradingbot.strategies.sma_cross"
    assert "ema_cross" in warned[0].getMessage() and f"{strat.recommended_candles}" in warned[0].getMessage()
    caplog.clear()

    with caplog.at_level(logging.WARNING, logger="tradingbot.strategies"):
        # 권장치 이상이면 경고 없음
        EMACrossStrategy(fast=5, slow=12).generate_signal(SYM, daily_df.iloc[: strat.recommended_candles])
        # warmup 미만은 "데이터 부족" HOLD 가 대신 알린다
        sig = EMACrossStrategy(fast=5, slow=12).generate_signal(SYM, daily_df.iloc[: strat.warmup - 1])
        assert sig.is_hold and "데이터 부족" in sig.reason
        # rolling 전략은 warmup == recommended_candles 라 경고할 일이 없다
        SMACrossStrategy(fast=5, slow=12).generate_signal(SYM, short)
        # 다른 ewm 전략도 같은 안전장치를 쓴다
        RSIStrategy(period=7).generate_signal(SYM, daily_df.iloc[:20])
        MACDStrategy(fast=5, slow=13, signal=4).generate_signal(SYM, daily_df.iloc[:30])
    warned = [r for r in caplog.records if "recommended_candles" in r.getMessage()]
    assert sorted(r.name for r in warned) == ["tradingbot.strategies.macd", "tradingbot.strategies.rsi"]


# ====================================================================== 공통 신호 규약


@pytest.mark.parametrize("cfg", ALL_CONFIGS, ids=_cfg_id)
def test_signal_contract_on_real_data(cfg, daily_df):
    name, params = cfg
    strat = create_strategy(name, params)
    prepared = strat.prepare(daily_df)
    for i in range(len(prepared)):
        sig = strat.signal_at(SYM, prepared, i)
        assert sig.symbol == SYM
        assert isinstance(sig.reason, str) and sig.reason
        json.dumps(sig.meta)  # 엔진 상태 저장(JSON) 을 위해 직렬화 가능해야 함
        if sig.is_hold:
            assert sig.strength == 0.0
        else:
            assert sig.strength == 1.0
            assert sig.action in (SignalAction.BUY, SignalAction.SELL)
            assert sig.meta, "매매 신호에는 지표값 meta 가 있어야 함"
            assert sig.meta.get("timestamp") == prepared["timestamp"].iat[i].isoformat()
            if name != "volatility_breakout":
                assert sig.order_type == OrderType.MARKET
                assert sig.price is None and sig.stop_offset is None and sig.max_holding_bars is None


def test_generate_signal_from_candle_list(daily_candles):
    strat = create_strategy("sma_cross", {"fast": 5, "slow": 12})
    df = candles_to_df(daily_candles)
    sig = strat.generate_signal(SYM, df)
    prepared = strat.prepare(df)
    assert_same_signal(sig, strat.signal_at(SYM, prepared, len(prepared) - 1))


# ====================================================================== 전략별 규칙 재검증 (실제 데이터)


def _expected_cross_actions(fast: pd.Series, slow: pd.Series, start: int) -> list[SignalAction]:
    out: list[SignalAction] = []
    for i in range(start, len(fast)):
        f, s, pf, ps = fast.iat[i], slow.iat[i], fast.iat[i - 1], slow.iat[i - 1]
        if f > s and pf <= ps:
            out.append(SignalAction.BUY)
        elif f < s and pf >= ps:
            out.append(SignalAction.SELL)
        else:
            out.append(SignalAction.HOLD)
    return out


def _actions(strat: BaseStrategy, prepared: pd.DataFrame, start: int) -> list[SignalAction]:
    return [strat.signal_at(SYM, prepared, i).action for i in range(start, len(prepared))]


class TestSMACross:
    @pytest.mark.parametrize("fixture", ["daily_df", "candles_df"])
    def test_rules_match_independent_rolling_mean(self, fixture, request):
        df = request.getfixturevalue(fixture)
        strat = SMACrossStrategy(fast=5, slow=12)
        prepared = strat.prepare(df)
        fast = df["close"].rolling(5).mean()
        slow = df["close"].rolling(12).mean()
        start = strat.warmup - 1
        actions = _actions(strat, prepared, start)
        assert actions == _expected_cross_actions(fast, slow, start)
        assert SignalAction.BUY in actions and SignalAction.SELL in actions
        pd.testing.assert_series_equal(prepared["sma_fast"], fast, check_names=False)
        pd.testing.assert_series_equal(prepared["sma_slow"], slow, check_names=False)

    def test_reason_and_meta(self, daily_df):
        strat = SMACrossStrategy(fast=5, slow=12)
        prepared = strat.prepare(daily_df)
        buys = [
            i for i in range(len(prepared)) if strat.signal_at(SYM, prepared, i).action == SignalAction.BUY
        ]
        sells = [
            i for i in range(len(prepared)) if strat.signal_at(SYM, prepared, i).action == SignalAction.SELL
        ]
        b = strat.signal_at(SYM, prepared, buys[0])
        assert b.reason == "SMA5 > SMA12 골든크로스"
        assert b.meta["sma_fast"] == pytest.approx(prepared["sma_fast"].iat[buys[0]])
        assert b.meta["sma_slow"] == pytest.approx(prepared["sma_slow"].iat[buys[0]])
        assert b.meta["close"] == pytest.approx(prepared["close"].iat[buys[0]])
        assert b.meta["sma_fast"] > b.meta["sma_slow"]
        s = strat.signal_at(SYM, prepared, sells[0])
        assert s.reason == "SMA5 < SMA12 데드크로스"
        assert s.meta["sma_fast"] < s.meta["sma_slow"]
        h = next(
            strat.signal_at(SYM, prepared, i)
            for i in range(strat.warmup - 1, len(prepared))
            if i not in buys + sells
        )
        assert h.is_hold and "교차 없음" in h.reason and h.meta["fast"] == 5

    def test_warmup_value(self):
        assert SMACrossStrategy(fast=10, slow=30).warmup == 31
        assert SMACrossStrategy().warmup == 31


class TestEMACross:
    @pytest.mark.parametrize("fixture", ["daily_df", "candles_df"])
    def test_rules_match_independent_ewm(self, fixture, request):
        df = request.getfixturevalue(fixture)
        strat = EMACrossStrategy(fast=5, slow=12)
        prepared = strat.prepare(df)
        fast = df["close"].ewm(span=5, adjust=False).mean()
        slow = df["close"].ewm(span=12, adjust=False).mean()
        start = strat.warmup - 1
        actions = _actions(strat, prepared, start)
        assert actions == _expected_cross_actions(fast, slow, start)
        assert SignalAction.BUY in actions and SignalAction.SELL in actions
        pd.testing.assert_series_equal(prepared["ema_slow"].iloc[11:], slow.iloc[11:], check_names=False)

    def test_reason_labels(self, daily_df):
        strat = EMACrossStrategy(fast=5, slow=12)
        prepared = strat.prepare(daily_df)
        sigs = [strat.signal_at(SYM, prepared, i) for i in range(len(prepared))]
        buy = next(s for s in sigs if s.action == SignalAction.BUY)
        sell = next(s for s in sigs if s.action == SignalAction.SELL)
        assert buy.reason == "EMA5 > EMA12 골든크로스" and "ema_fast" in buy.meta
        assert sell.reason == "EMA5 < EMA12 데드크로스"
        assert EMACrossStrategy().warmup == 27


class TestRSI:
    @staticmethod
    def _ref_rsi(close: pd.Series, period: int) -> pd.Series:
        delta = close.diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
        loss = (-delta).clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
        return 100 * gain / (gain + loss)

    @pytest.mark.parametrize("fixture", ["daily_df", "candles_df"])
    def test_rules_match_independent_rsi(self, fixture, request):
        df = request.getfixturevalue(fixture)
        strat = RSIStrategy(period=7, oversold=40, overbought=60)
        prepared = strat.prepare(df)
        rsi = self._ref_rsi(df["close"], 7)
        start = strat.warmup - 1
        expected: list[SignalAction] = []
        for i in range(start, len(df)):
            r, pr = rsi.iat[i], rsi.iat[i - 1]
            if r > 40 and pr <= 40:
                expected.append(SignalAction.BUY)
            elif r < 60 and pr >= 60:
                expected.append(SignalAction.SELL)
            else:
                expected.append(SignalAction.HOLD)
        actions = _actions(strat, prepared, start)
        assert actions == expected
        assert SignalAction.BUY in actions and SignalAction.SELL in actions

    def test_reason_and_meta(self, candles_df):
        strat = RSIStrategy(period=7, oversold=40, overbought=60)
        prepared = strat.prepare(candles_df)
        sigs = [strat.signal_at(SYM, prepared, i) for i in range(len(prepared))]
        buy = next(s for s in sigs if s.action == SignalAction.BUY)
        sell = next(s for s in sigs if s.action == SignalAction.SELL)
        assert buy.reason.startswith("RSI(7) 40 상향 돌파")
        assert buy.meta["rsi"] > 40 >= buy.meta["prev_rsi"]
        assert sell.reason.startswith("RSI(7) 60 하향 돌파")
        assert sell.meta["rsi"] < 60 <= sell.meta["prev_rsi"]
        assert buy.meta["oversold"] == 40.0 and buy.meta["overbought"] == 60.0
        hold = next(s for s in sigs[strat.warmup - 1 :] if s.is_hold)
        assert "신호 없음" in hold.reason and 0 <= hold.meta["rsi"] <= 100

    def test_warmup_value(self):
        assert RSIStrategy().warmup == 16
        assert RSIStrategy(period=7).warmup == 9


class TestBollinger:
    @pytest.mark.parametrize("fixture", ["daily_df", "candles_df"])
    def test_reversion_rules(self, fixture, request):
        df = request.getfixturevalue(fixture)
        strat = BollingerStrategy(period=10, num_std=1.5, mode="reversion")
        prepared = strat.prepare(df)
        close = df["close"]
        mid = close.rolling(10).mean()
        lower = mid - 1.5 * close.rolling(10).std(ddof=0)
        start = strat.warmup - 1
        expected: list[SignalAction] = []
        for i in range(start, len(df)):
            if close.iat[i] >= mid.iat[i]:
                expected.append(SignalAction.SELL)
            elif close.iat[i] > lower.iat[i] and close.iat[i - 1] <= lower.iat[i - 1]:
                expected.append(SignalAction.BUY)
            else:
                expected.append(SignalAction.HOLD)
        actions = _actions(strat, prepared, start)
        assert actions == expected
        assert SignalAction.SELL in actions

    @pytest.mark.parametrize("fixture", ["daily_df", "candles_df"])
    def test_breakout_rules(self, fixture, request):
        df = request.getfixturevalue(fixture)
        strat = BollingerStrategy(period=10, num_std=1.5, mode="breakout")
        prepared = strat.prepare(df)
        close = df["close"]
        mid = close.rolling(10).mean()
        std = close.rolling(10).std(ddof=0)
        upper = mid + 1.5 * std
        start = strat.warmup - 1
        expected: list[SignalAction] = []
        for i in range(start, len(df)):
            if close.iat[i] > upper.iat[i] and close.iat[i - 1] <= upper.iat[i - 1]:
                expected.append(SignalAction.BUY)
            elif close.iat[i] < mid.iat[i] and close.iat[i - 1] >= mid.iat[i - 1]:
                expected.append(SignalAction.SELL)
            else:
                expected.append(SignalAction.HOLD)
        actions = _actions(strat, prepared, start)
        assert actions == expected
        assert SignalAction.BUY in actions and SignalAction.SELL in actions

    def test_reversion_sell_takes_precedence_over_buy(self, daily_df):
        strat = BollingerStrategy(period=10, num_std=1.5, mode="reversion")
        prepared = strat.prepare(daily_df)
        i = len(prepared) - 1
        prepared.loc[i, "bb_buy"] = True
        prepared.loc[i, "bb_sell"] = True
        assert strat.signal_at(SYM, prepared, i).action == SignalAction.SELL

    def test_reasons_and_meta(self, daily_df):
        strat = BollingerStrategy(period=10, num_std=1.5, mode="breakout")
        prepared = strat.prepare(daily_df)
        sigs = [strat.signal_at(SYM, prepared, i) for i in range(len(prepared))]
        buy = next(s for s in sigs if s.action == SignalAction.BUY)
        sell = next(s for s in sigs if s.action == SignalAction.SELL)
        assert "상단밴드" in buy.reason and buy.meta["close"] > buy.meta["bb_upper"]
        assert "중심선" in sell.reason and sell.meta["close"] < sell.meta["bb_mid"]
        assert buy.meta["mode"] == "breakout" and buy.meta["pct_b"] > 1.0
        rev = BollingerStrategy(period=10, num_std=1.5, mode="reversion")
        rev_prepared = rev.prepare(daily_df)
        rsigs = [rev.signal_at(SYM, rev_prepared, i) for i in range(len(rev_prepared))]
        rsell = next(s for s in rsigs if s.action == SignalAction.SELL)
        assert "중심선" in rsell.reason and "청산" in rsell.reason
        assert rsell.meta["close"] >= rsell.meta["bb_mid"]
        for s in rsigs:
            if s.action == SignalAction.BUY:
                assert "하단밴드" in s.reason and s.meta["close"] > s.meta["bb_lower"]

    def test_warmup_value(self):
        assert BollingerStrategy().warmup == 21


class TestMACD:
    @pytest.mark.parametrize("fixture", ["daily_df", "candles_df"])
    def test_rules_match_independent_macd(self, fixture, request):
        df = request.getfixturevalue(fixture)
        strat = MACDStrategy(fast=5, slow=13, signal=4)
        prepared = strat.prepare(df)
        close = df["close"]
        macd = close.ewm(span=5, adjust=False).mean() - close.ewm(span=13, adjust=False).mean()
        macd[:12] = np.nan  # slow-1 행 전까지는 워밍업
        signal = macd.ewm(span=4, adjust=False).mean()
        start = strat.warmup - 1
        actions = _actions(strat, prepared, start)
        assert actions == _expected_cross_actions(macd, signal, start)
        assert SignalAction.BUY in actions and SignalAction.SELL in actions
        pd.testing.assert_series_equal(prepared["macd"].iloc[12:], macd.iloc[12:], check_names=False)
        pd.testing.assert_series_equal(prepared["macd_signal"].iloc[15:], signal.iloc[15:], check_names=False)

    def test_reason_and_meta(self, daily_df):
        strat = MACDStrategy(fast=5, slow=13, signal=4)
        prepared = strat.prepare(daily_df)
        sigs = [strat.signal_at(SYM, prepared, i) for i in range(len(prepared))]
        buy = next(s for s in sigs if s.action == SignalAction.BUY)
        sell = next(s for s in sigs if s.action == SignalAction.SELL)
        assert buy.reason.startswith("MACD(5,13,4) 시그널 상향 돌파")
        assert buy.meta["macd"] > buy.meta["macd_signal"] and buy.meta["macd_hist"] > 0
        assert sell.reason.startswith("MACD(5,13,4) 시그널 하향 돌파")
        assert sell.meta["macd_hist"] < 0
        assert buy.meta["macd_hist"] == pytest.approx(buy.meta["macd"] - buy.meta["macd_signal"])

    def test_warmup_value(self):
        assert MACDStrategy().warmup == 35
        assert MACDStrategy(fast=5, slow=13, signal=4).warmup == 17


class TestVolatilityBreakout:
    def test_last_row_emits_stop_buy_with_k_times_range(self, daily_df):
        strat = VolatilityBreakoutStrategy(k=0.5, ma_period=0)
        sig = strat.generate_signal(SYM, daily_df)
        last = daily_df.iloc[-1]
        assert sig.action == SignalAction.BUY
        assert sig.order_type == OrderType.STOP
        assert sig.price is None
        assert sig.stop_offset == pytest.approx(0.5 * (last["high"] - last["low"]))
        assert sig.max_holding_bars == 1
        assert sig.strength == 1.0
        assert isinstance(sig.stop_offset, float)
        assert sig.meta["range"] == pytest.approx(last["high"] - last["low"])
        assert sig.meta["stop_offset"] == pytest.approx(sig.stop_offset)
        assert sig.meta["high"] == last["high"] and sig.meta["low"] == last["low"]
        assert sig.meta["k"] == 0.5 and sig.meta["ma_period"] == 0 and sig.meta["ma"] is None
        assert "변동성 돌파" in sig.reason

    @pytest.mark.parametrize("k", [0.3, 0.5, 0.8])
    @pytest.mark.parametrize("fixture", ["daily_df", "candles_df", "eth_daily_df"])
    def test_every_row_without_filter(self, k, fixture, request):
        df = request.getfixturevalue(fixture)
        strat = VolatilityBreakoutStrategy(k=k)
        assert strat.warmup == 1
        prepared = strat.prepare(df)
        for i in range(len(prepared)):
            sig = strat.signal_at(SYM, prepared, i)
            rng = df["high"].iat[i] - df["low"].iat[i]
            if rng <= 0:
                assert sig.is_hold
                continue
            assert sig.action == SignalAction.BUY and sig.order_type == OrderType.STOP
            assert sig.stop_offset == pytest.approx(k * rng)
            assert sig.max_holding_bars == 1 and sig.price is None

    def test_never_emits_sell(self, daily_df, candles_df):
        for df in (daily_df, candles_df):
            for params in ({"ma_period": 0}, {"ma_period": 20}):
                strat = VolatilityBreakoutStrategy(**params)
                prepared = strat.prepare(df)
                assert all(
                    strat.signal_at(SYM, prepared, i).action != SignalAction.SELL
                    for i in range(len(prepared))
                )

    @pytest.mark.parametrize("fixture", ["daily_df", "candles_df"])
    def test_ma_filter_rules(self, fixture, request):
        df = request.getfixturevalue(fixture)
        strat = VolatilityBreakoutStrategy(k=0.5, ma_period=20)
        assert strat.warmup == 21
        prepared = strat.prepare(df)
        sma20 = df["close"].rolling(20).mean()
        below = above = 0
        for i in range(strat.warmup - 1, len(prepared)):
            sig = strat.signal_at(SYM, prepared, i)
            if df["close"].iat[i] < sma20.iat[i]:
                below += 1
                assert sig.is_hold
                assert "추세 필터" in sig.reason and f"SMA{20}" in sig.reason
                assert sig.meta["ma"] == pytest.approx(sma20.iat[i])
            else:
                above += 1
                assert sig.action == SignalAction.BUY and sig.order_type == OrderType.STOP
                assert sig.stop_offset == pytest.approx(0.5 * (df["high"].iat[i] - df["low"].iat[i]))
                assert sig.max_holding_bars == 1
                assert sig.meta["ma"] == pytest.approx(sma20.iat[i])
                assert "SMA20" in sig.reason
        assert below > 0 and above > 0, "200개 캔들 안에 SMA20 위/아래 구간이 모두 있어야 함"

    def test_ma_filter_warmup_holds(self, daily_df):
        strat = VolatilityBreakoutStrategy(ma_period=20)
        prepared = strat.prepare(daily_df)
        for i in range(20):
            assert strat.signal_at(SYM, prepared, i).is_hold
        short = strat.generate_signal(SYM, daily_df.iloc[:20])
        assert short.is_hold and "데이터 부족" in short.reason
        # 21개면 워밍업 완료: 필터 HOLD 또는 BUY 이지 "데이터 부족" 은 아니다
        enough = strat.generate_signal(SYM, daily_df.iloc[:21])
        assert "데이터 부족" not in enough.reason and "워밍업" not in enough.reason

    def test_zero_range_is_hold(self, daily_df):
        strat = VolatilityBreakoutStrategy()
        prepared = strat.prepare(daily_df)
        i = len(prepared) - 1
        prepared.loc[i, "vb_range"] = 0.0
        sig = strat.signal_at(SYM, prepared, i)
        assert sig.is_hold and "변동폭 0" in sig.reason

    def test_prepare_columns(self, daily_df):
        strat = VolatilityBreakoutStrategy(k=0.7, ma_period=5)
        prepared = strat.prepare(daily_df)
        pd.testing.assert_series_equal(
            prepared["vb_range"], daily_df["high"] - daily_df["low"], check_names=False
        )
        pd.testing.assert_series_equal(
            prepared["vb_offset"], 0.7 * (daily_df["high"] - daily_df["low"]), check_names=False
        )
        pd.testing.assert_series_equal(
            prepared["vb_ma"], daily_df["close"].rolling(5).mean(), check_names=False
        )
        assert VolatilityBreakoutStrategy().prepare(daily_df)["vb_ma"].isna().all()

    def test_requires_high_low(self, daily_df):
        strat = VolatilityBreakoutStrategy()
        with pytest.raises(DataError):
            strat.prepare(daily_df[["timestamp", "close"]])
