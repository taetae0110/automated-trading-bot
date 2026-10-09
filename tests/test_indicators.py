"""지표 단위 테스트.

- 손계산 검증 벡터 (아주 짧은 수열, 순수 수학) 로 정확성 확인
- 실제 Upbit 캔들(conftest 픽스처) 로 pandas 참조 계산과 비교
- 미래 참조 없음 (prefix 로 잘라 계산해도 마지막 값이 동일)
- 오류 경로 (잘못된 파라미터, 컬럼 누락)
"""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from tradingbot.exceptions import DataError
from tradingbot.strategies import indicators as ind

# ---------------------------------------------------------------------- 헬퍼


def _series(values: list[float]) -> pd.Series:
    return pd.Series(values, dtype=float)


def _manual_ewm(values: list[float], alpha: float) -> list[float]:
    """adjust=False 재귀식 (NaN 은 건너뜀, 첫 유효값으로 시드)."""
    out: list[float] = []
    prev: float | None = None
    for v in values:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            out.append(math.nan)
            continue
        prev = v if prev is None else prev + alpha * (v - prev)
        out.append(prev)
    return out


def _manual_wilder_rsi(closes: list[float], period: int) -> list[float]:
    """Wilder RSI 를 명시적 루프로 (ewm alpha=1/period, adjust=False, 첫 변화량으로 시드)."""
    alpha = 1.0 / period
    out = [math.nan] * len(closes)
    avg_gain: float | None = None
    avg_loss: float | None = None
    n_obs = 0
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        if avg_gain is None or avg_loss is None:
            avg_gain, avg_loss = gain, loss
        else:
            avg_gain = avg_gain + alpha * (gain - avg_gain)
            avg_loss = avg_loss + alpha * (loss - avg_loss)
        n_obs += 1
        if n_obs < period:
            continue
        denom = avg_gain + avg_loss
        out[i] = math.nan if denom == 0 else 100.0 * avg_gain / denom
    return out


def _same_value(x: float, y: float) -> bool:
    """둘 다 NaN 이거나 (상대오차 1e-12 로) 같으면 True."""
    if math.isnan(x) or math.isnan(y):
        return math.isnan(x) and math.isnan(y)
    return math.isclose(x, y, rel_tol=1e-12, abs_tol=1e-9)


def _assert_nan_prefix(s: pd.Series, n: int) -> None:
    """앞 n 개는 NaN, 그 이후는 전부 유한값."""
    assert s.iloc[:n].isna().all(), f"앞 {n}개가 NaN 이어야 함"
    assert np.isfinite(s.iloc[n:]).all(), f"인덱스 {n} 이후에 NaN/inf 가 있음"


# ====================================================================== 손계산 벡터


class TestSMA:
    def test_hand_vector(self):
        out = ind.sma(_series([1, 2, 3, 4, 5]), 3)
        assert out.isna().tolist() == [True, True, False, False, False]
        assert out.iloc[2:].tolist() == [2.0, 3.0, 4.0]

    def test_period_one_is_identity(self):
        s = _series([3, 1, 4, 1, 5])
        pd.testing.assert_series_equal(ind.sma(s, 1), s)

    def test_period_longer_than_series_is_all_nan(self):
        assert ind.sma(_series([1, 2, 3]), 5).isna().all()

    def test_accepts_integer_dtype_input(self):
        out = ind.sma(pd.Series([1, 2, 3, 4], dtype="int64"), 2)
        assert out.dtype == float
        assert out.iloc[1:].tolist() == [1.5, 2.5, 3.5]


class TestEMA:
    def test_hand_vector_adjust_false(self):
        # span=3 → alpha=0.5: 1, 1.5, 2.25, 3.125, 4.0625 (앞 2개는 워밍업 NaN)
        out = ind.ema(_series([1, 2, 3, 4, 5]), 3)
        assert out.isna().tolist() == [True, True, False, False, False]
        assert out.iloc[2:].tolist() == pytest.approx([2.25, 3.125, 4.0625])

    def test_matches_manual_recursion(self):
        vals = [10.0, 11.0, 10.5, 12.0, 13.0, 12.5, 11.0]
        out = ind.ema(_series(vals), 4)
        ref = _manual_ewm(vals, 2.0 / (4 + 1))
        assert out.iloc[3:].tolist() == pytest.approx(ref[3:])
        assert out.iloc[:3].isna().all()


class TestRSI:
    def test_all_up_is_100(self):
        out = ind.rsi(_series([1, 2, 3, 4]), 2)
        assert out.isna().tolist() == [True, True, False, False]
        assert out.iloc[2:].tolist() == [100.0, 100.0]

    def test_all_down_is_0(self):
        out = ind.rsi(_series([4, 3, 2, 1]), 2)
        assert out.iloc[2:].tolist() == [0.0, 0.0]

    def test_mixed_hand_vector(self):
        # deltas [nan, +1, -1, +2], alpha=0.5
        # avg_gain: 1, 0.5, 1.25 / avg_loss: 0, 0.5, 0.25 → RSI idx2 = 50, idx3 = 83.33
        out = ind.rsi(_series([10, 11, 10, 12]), 2)
        assert out.isna().tolist() == [True, True, False, False]
        assert out.iloc[2] == pytest.approx(50.0)
        assert out.iloc[3] == pytest.approx(100.0 * 1.25 / 1.5)

    def test_flat_series_is_nan(self):
        out = ind.rsi(_series([5, 5, 5, 5, 5]), 2)
        assert out.isna().all()

    def test_range_bounds_on_random_walk_free_vector(self):
        vals = [100, 101, 99, 102, 98, 103, 97, 104, 96, 105]
        out = ind.rsi(_series(vals), 3).dropna()
        assert ((out >= 0) & (out <= 100)).all()


class TestMACD:
    def test_structure_and_manual_recursion(self):
        vals = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
        m, sig, hist = ind.macd(_series(vals), fast=2, slow=3, signal=2)
        ema2 = _manual_ewm(vals, 2 / 3)
        ema3 = _manual_ewm(vals, 2 / 4)
        macd_ref = [math.nan, math.nan] + [a - b for a, b in zip(ema2[2:], ema3[2:], strict=True)]
        sig_ref = _manual_ewm(macd_ref, 2 / 3)
        sig_ref[2] = math.nan  # min_periods=2 → 첫 유효 macd 행은 NaN
        assert m.iloc[:2].isna().all()
        assert m.iloc[2:].tolist() == pytest.approx(macd_ref[2:])
        assert sig.iloc[:3].isna().all()
        assert sig.iloc[3:].tolist() == pytest.approx(sig_ref[3:])
        assert hist.iloc[3:].tolist() == pytest.approx(
            [a - b for a, b in zip(m.iloc[3:], sig.iloc[3:], strict=True)]
        )

    def test_default_warmup_lengths(self):
        s = _series(list(range(1, 60)))
        m, sig, hist = ind.macd(s)
        _assert_nan_prefix(m, 25)  # slow-1
        _assert_nan_prefix(sig, 33)  # slow+signal-2
        _assert_nan_prefix(hist, 33)

    def test_fast_must_be_less_than_slow(self):
        with pytest.raises(ValueError):
            ind.macd(_series([1, 2, 3]), fast=26, slow=12)
        with pytest.raises(ValueError):
            ind.macd(_series([1, 2, 3]), fast=12, slow=12)


class TestBollinger:
    def test_hand_vector_ddof0(self):
        mid, upper, lower = ind.bollinger(_series([1, 2, 3, 4, 5]), period=3, num_std=2.0)
        std = math.sqrt(2.0 / 3.0)  # 모집단 표준편차 of [1,2,3]
        assert mid.isna().tolist() == [True, True, False, False, False]
        assert mid.iloc[2:].tolist() == [2.0, 3.0, 4.0]
        assert upper.iloc[2:].tolist() == pytest.approx([2 + 2 * std, 3 + 2 * std, 4 + 2 * std])
        assert lower.iloc[2:].tolist() == pytest.approx([2 - 2 * std, 3 - 2 * std, 4 - 2 * std])

    def test_num_std_zero_collapses_to_mid(self):
        mid, upper, lower = ind.bollinger(_series([1, 2, 3, 4]), period=2, num_std=0.0)
        pd.testing.assert_series_equal(upper, mid)
        pd.testing.assert_series_equal(lower, mid)

    def test_negative_num_std_rejected(self):
        with pytest.raises(ValueError):
            ind.bollinger(_series([1, 2, 3]), period=2, num_std=-1.0)


class TestTrueRangeATR:
    @pytest.fixture
    def ohlc(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "high": [10.0, 12.0, 15.0, 11.0],
                "low": [8.0, 9.0, 13.0, 7.0],
                "close": [9.0, 11.0, 14.0, 8.0],
            }
        )

    def test_true_range_hand_vector(self, ohlc):
        tr = ind.true_range(ohlc)
        assert math.isnan(tr.iloc[0])
        # row1: max(3, |12-9|=3, |9-9|=0)=3 / row2: max(2, |15-11|=4, |13-11|=2)=4 / row3: max(4, 3, |7-14|=7)=7
        assert tr.iloc[1:].tolist() == [3.0, 4.0, 7.0]

    def test_atr_hand_vector(self, ohlc):
        # alpha=0.5, TR=[nan,3,4,7] → 시드 3, 3.5, 5.25 ; min_periods=2 → idx2 부터 유효
        atr = ind.atr(ohlc, 2)
        assert atr.isna().tolist() == [True, True, False, False]
        assert atr.iloc[2:].tolist() == pytest.approx([3.5, 5.25])

    def test_missing_columns_raise_data_error(self):
        with pytest.raises(DataError):
            ind.true_range(pd.DataFrame({"high": [1.0], "low": [0.5]}))
        with pytest.raises(DataError):
            ind.atr(pd.DataFrame({"close": [1.0]}), 2)

    def test_non_dataframe_raises_data_error(self):
        with pytest.raises(DataError):
            ind.true_range(_series([1, 2, 3]))  # type: ignore[arg-type]


class TestCross:
    def test_crossover_crossunder_hand_vector(self):
        a = _series([1, 2, 3, 2, 1])
        b = _series([2, 2, 2, 2, 2])
        assert ind.crossover(a, b).tolist() == [False, False, True, False, False]
        assert ind.crossunder(a, b).tolist() == [False, False, False, False, True]

    def test_scalar_other(self):
        a = _series([1, 2, 3, 2, 1])
        assert ind.crossover(a, 2).tolist() == [False, False, True, False, False]
        assert ind.crossunder(a, 2.0).tolist() == [False, False, False, False, True]

    def test_touch_then_cross_counts(self):
        # 직전 행이 정확히 같아도(<=, >=) 다음 행에 넘어서면 돌파. 같은 값에 '닿기만' 한 행은 돌파가 아니다.
        a = _series([3, 2, 3])
        assert ind.crossover(a, 2).tolist() == [False, False, True]
        assert ind.crossunder(a, 2).tolist() == [False, False, False]
        b = _series([1, 2, 1])
        assert ind.crossunder(b, 2).tolist() == [False, False, True]
        assert ind.crossover(b, 2).tolist() == [False, False, False]

    def test_nan_rows_are_false(self):
        a = _series([math.nan, 3, 1, math.nan, 3])
        assert ind.crossover(a, 2).tolist() == [False, False, False, False, False]
        assert ind.crossunder(a, 2).tolist() == [False, False, True, False, False]

    def test_dtype_is_bool_and_index_preserved(self):
        idx = pd.Index([10, 20, 30])
        a = pd.Series([1.0, 3.0, 1.0], index=idx)
        out = ind.crossover(a, 2)
        assert out.dtype == bool
        assert out.index.equals(idx)

    def test_misaligned_index_raises(self):
        a = pd.Series([1.0, 2.0], index=[0, 1])
        b = pd.Series([1.0, 2.0], index=[1, 2])
        with pytest.raises(DataError):
            ind.crossover(a, b)

    def test_non_series_input_raises(self):
        with pytest.raises(DataError):
            ind.crossover([1, 2, 3], 2)  # type: ignore[arg-type]


# ====================================================================== 파라미터 검증


class TestParamValidation:
    @pytest.mark.parametrize("fn", [ind.sma, ind.ema, ind.rsi])
    @pytest.mark.parametrize("bad", [0, -1, True, False, 1.5, "abc", None, math.nan, math.inf])
    def test_bad_period_rejected(self, fn, bad):
        with pytest.raises(ValueError):
            fn(_series([1, 2, 3]), bad)

    def test_as_period_coercion(self):
        assert ind.as_period(10, "p") == 10
        assert ind.as_period(10.0, "p") == 10
        assert ind.as_period("10", "p") == 10
        assert ind.as_period(" 7 ", "p") == 7
        assert ind.as_period(np.int64(3), "p") == 3
        assert ind.as_period(0, "p", minimum=0) == 0
        with pytest.raises(ValueError):
            ind.as_period(0, "p")
        with pytest.raises(ValueError):
            ind.as_period(-1, "p", minimum=0)

    def test_as_float_coercion(self):
        assert ind.as_float(1, "k") == 1.0
        assert ind.as_float("0.5", "k") == 0.5
        assert ind.as_float(np.float32(0.25), "k") == pytest.approx(0.25)
        for bad in (True, "x", None, math.nan, math.inf, [1]):
            with pytest.raises(ValueError):
                ind.as_float(bad, "k")

    def test_float_or_none(self):
        assert ind.float_or_none(None) is None
        assert ind.float_or_none(math.nan) is None
        assert ind.float_or_none(np.float64("nan")) is None
        assert ind.float_or_none(np.float64(1.5)) == 1.5
        assert ind.float_or_none("abc") is None
        assert isinstance(ind.float_or_none(np.int64(3)), float)

    def test_fmt_price(self):
        assert ind.fmt_price(112_662_000.0) == "112,662,000"
        assert ind.fmt_price(1234.5) == "1,234"
        assert ind.fmt_price(12.345) == "12.35"
        assert ind.fmt_price(0.000123456) == "0.000123456"

    def test_check_row(self):
        df = pd.DataFrame({"close": [1.0, 2.0, 3.0], "x": [1, 2, 3]})
        ind.check_row(df, 0, ("x",))
        ind.check_row(df, 2, ("x",))
        with pytest.raises(DataError):
            ind.check_row(df, 0, ("missing",))
        with pytest.raises(IndexError):
            ind.check_row(df, 3)
        with pytest.raises(IndexError):
            ind.check_row(df, -1)
        with pytest.raises(IndexError):
            ind.check_row(df, 1.0)  # type: ignore[arg-type]
        with pytest.raises(IndexError):
            ind.check_row(df, True)  # type: ignore[arg-type]
        with pytest.raises(DataError):
            ind.check_row([1, 2], 0)  # type: ignore[arg-type]

    def test_bar_time(self):
        ts = pd.Timestamp("2024-01-02T03:04:05Z")
        df = pd.DataFrame({"timestamp": [ts, pd.NaT], "close": [1.0, 2.0]})
        assert ind.bar_time(df, 0) == ts.isoformat()
        assert ind.bar_time(df, 1) is None
        assert ind.bar_time(pd.DataFrame({"close": [1.0]}), 0) is None


# ====================================================================== 실제 데이터 vs pandas 참조


class TestAgainstPandasOnRealData:
    def test_sma_matches_rolling_mean(self, daily_df):
        close = daily_df["close"]
        out = ind.sma(close, 20)
        pd.testing.assert_series_equal(out, close.rolling(20).mean(), check_names=False)
        _assert_nan_prefix(out, 19)

    def test_ema_matches_ewm_after_warmup(self, daily_df):
        close = daily_df["close"]
        out = ind.ema(close, 12)
        ref = close.ewm(span=12, adjust=False).mean()
        _assert_nan_prefix(out, 11)
        pd.testing.assert_series_equal(out.iloc[11:], ref.iloc[11:], check_names=False)

    def test_rsi_matches_manual_wilder_loop(self, daily_df):
        close = daily_df["close"]
        out = ind.rsi(close, 14)
        ref = _manual_wilder_rsi(close.tolist(), 14)
        _assert_nan_prefix(out, 14)
        assert out.iloc[14:].tolist() == pytest.approx(ref[14:], rel=1e-12)
        assert ((out.dropna() >= 0) & (out.dropna() <= 100)).all()

    def test_rsi_matches_pure_pandas_formula(self, candles_df):
        close = candles_df["close"]
        delta = close.diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
        loss = (-delta).clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
        ref = 100 - 100 / (1 + gain / loss)
        out = ind.rsi(close, 14)
        pd.testing.assert_series_equal(out.iloc[14:], ref.iloc[14:], check_names=False)

    def test_macd_matches_ema_difference(self, daily_df):
        close = daily_df["close"]
        m, sig, hist = ind.macd(close)
        ref_m = ind.ema(close, 12) - ind.ema(close, 26)
        pd.testing.assert_series_equal(m, ref_m, check_names=False)
        ref_sig = ref_m.ewm(span=9, adjust=False, min_periods=9).mean()
        pd.testing.assert_series_equal(sig, ref_sig, check_names=False)
        pd.testing.assert_series_equal(hist, m - sig, check_names=False)

    def test_bollinger_matches_rolling(self, eth_daily_df):
        close = eth_daily_df["close"]
        mid, upper, lower = ind.bollinger(close, 20, 2.0)
        ref_mid = close.rolling(20).mean()
        ref_std = close.rolling(20).std(ddof=0)
        pd.testing.assert_series_equal(mid, ref_mid, check_names=False)
        pd.testing.assert_series_equal(upper, ref_mid + 2.0 * ref_std, check_names=False)
        pd.testing.assert_series_equal(lower, ref_mid - 2.0 * ref_std, check_names=False)
        valid = mid.notna()
        assert (upper[valid] >= mid[valid]).all() and (mid[valid] >= lower[valid]).all()

    def test_true_range_and_atr_on_real_data(self, daily_df):
        tr = ind.true_range(daily_df)
        prev_close = daily_df["close"].shift(1)
        ref = pd.concat(
            [
                daily_df["high"] - daily_df["low"],
                (daily_df["high"] - prev_close).abs(),
                (daily_df["low"] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        assert math.isnan(tr.iloc[0])
        pd.testing.assert_series_equal(tr.iloc[1:], ref.iloc[1:], check_names=False)
        # TR 은 항상 고가-저가 이상
        assert (tr.iloc[1:] >= (daily_df["high"] - daily_df["low"]).iloc[1:] - 1e-9).all()
        a = ind.atr(daily_df, 14)
        _assert_nan_prefix(a, 14)
        ref_atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
        pd.testing.assert_series_equal(a.iloc[14:], ref_atr.iloc[14:], check_names=False)
        assert (a.dropna() > 0).all()

    def test_crossover_consistent_with_shift_logic(self, candles_df):
        close = candles_df["close"]
        fast, slow = ind.sma(close, 5), ind.sma(close, 20)
        up = ind.crossover(fast, slow)
        down = ind.crossunder(fast, slow)
        ref_up = (fast > slow) & (fast.shift(1) <= slow.shift(1))
        ref_down = (fast < slow) & (fast.shift(1) >= slow.shift(1))
        assert up.tolist() == ref_up.tolist()
        assert down.tolist() == ref_down.tolist()
        assert not (up & down).any()
        # 워밍업 구간(NaN) 에서는 교차 없음
        assert not up.iloc[:20].any() and not down.iloc[:20].any()

    def test_index_is_preserved_for_datetime_index(self, daily_df):
        close = daily_df.set_index("timestamp")["close"]
        for out in (
            ind.sma(close, 5),
            ind.ema(close, 5),
            ind.rsi(close, 5),
            *ind.macd(close),
            *ind.bollinger(close),
        ):
            assert out.index.equals(close.index)
        df = daily_df.set_index("timestamp")
        assert ind.true_range(df).index.equals(df.index)
        assert ind.atr(df).index.equals(df.index)

    def test_inputs_are_not_mutated(self, daily_df):
        before = daily_df.copy(deep=True)
        close = daily_df["close"]
        ind.sma(close, 5)
        ind.ema(close, 5)
        ind.rsi(close, 5)
        ind.macd(close)
        ind.bollinger(close)
        ind.true_range(daily_df)
        ind.atr(daily_df)
        ind.crossover(close, ind.sma(close, 5))
        pd.testing.assert_frame_equal(daily_df, before)

    def test_meta_values_are_json_serializable(self, daily_df):
        close = daily_df["close"]
        payload = {
            "sma": ind.float_or_none(ind.sma(close, 5).iat[-1]),
            "rsi": ind.float_or_none(ind.rsi(close, 14).iat[-1]),
            "nan": ind.float_or_none(ind.sma(close, 5).iat[0]),
        }
        json.dumps(payload)


# ====================================================================== 미래 참조 없음 (prefix 불변)


@pytest.mark.parametrize("cut", [30, 60, 100, 150, 199])
def test_series_indicators_have_no_lookahead(daily_df, cut):
    close = daily_df["close"]
    prefix = close.iloc[: cut + 1].reset_index(drop=True)
    full_sma, pre_sma = ind.sma(close, 20), ind.sma(prefix, 20)
    full_ema, pre_ema = ind.ema(close, 20), ind.ema(prefix, 20)
    full_rsi, pre_rsi = ind.rsi(close, 14), ind.rsi(prefix, 14)
    full_bb, pre_bb = ind.bollinger(close), ind.bollinger(prefix)
    full_macd, pre_macd = ind.macd(close), ind.macd(prefix)

    assert _same_value(full_sma.iat[cut], pre_sma.iat[-1])
    assert _same_value(full_ema.iat[cut], pre_ema.iat[-1])
    assert _same_value(full_rsi.iat[cut], pre_rsi.iat[-1])
    for f, p in zip(full_bb, pre_bb, strict=True):
        assert _same_value(f.iat[cut], p.iat[-1])
    for f, p in zip(full_macd, pre_macd, strict=True):
        assert _same_value(f.iat[cut], p.iat[-1])
    # 워밍업이 끝난 컷에서는 실제 값(비 NaN)이 비교되었는지 확인
    if cut >= 33:
        assert not math.isnan(full_macd[1].iat[cut])


@pytest.mark.parametrize("cut", [20, 75, 199])
def test_ohlc_indicators_have_no_lookahead(candles_df, cut):
    prefix = candles_df.iloc[: cut + 1].reset_index(drop=True)
    assert ind.true_range(candles_df).iat[cut] == pytest.approx(ind.true_range(prefix).iat[-1], rel=1e-12)
    assert ind.atr(candles_df, 14).iat[cut] == pytest.approx(ind.atr(prefix, 14).iat[-1], rel=1e-12)


def test_prefix_cut_in_warmup_is_nan(daily_df):
    """워밍업이 안 끝난 prefix 는 NaN 이어야 한다 (값을 '만들어내지' 않음)."""
    prefix = daily_df["close"].iloc[:10].reset_index(drop=True)
    assert ind.sma(prefix, 20).isna().all()
    assert ind.ema(prefix, 20).isna().all()
    assert ind.rsi(prefix, 14).isna().all()
    assert all(s.isna().all() for s in ind.bollinger(prefix, 20))
