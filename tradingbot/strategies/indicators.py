"""기술적 지표 (pandas 전용, TA-Lib 없음).

규약
- 입력은 ``pd.Series`` (보통 종가) 또는 OHLC 컬럼을 가진 ``pd.DataFrame``.
- 반환은 입력과 **같은 인덱스**의 Series 이며, 워밍업 구간(값을 계산할 수 없는 앞부분)은 NaN 이다.
- 모든 계산은 rolling / ewm / shift 만 사용하므로 행 i 의 값은 행 0..i 만으로 결정된다 (미래 참조 없음).
  따라서 데이터의 **뒤(미래)** 를 행 i 에서 잘라 다시 계산해도 행 i 의 값은 동일하다 (prefix 불변).
- 단, **앞(과거)** 을 잘라낸 슬라이딩 윈도우(실시간 엔진이 넘기는 최근 ``candle_limit`` 개) 에서는 두 부류가 다르다.
  rolling 지표(SMA / 볼린저) 는 창 길이만 확보되면 전체 이력과 같은 값을 내지만, ``adjust=False`` 재귀식
  (첫 관측값으로 시드) 을 쓰는 EMA / RSI / ATR / MACD 는 윈도우의 첫 행으로 시드되므로 시드 가중치
  ``(1-alpha)^n`` 이 무시할 만큼 작아질 때까지(``ewm_settle_bars``) 전체 이력(백테스트) 과 값이 다르다.
  전략은 이 여유를 ``recommended_candles`` 로 알린다 (``warmup`` 은 NaN 이 아닌 값이 나오는 최소 개수일 뿐이다).

이 모듈에는 전략들이 공통으로 쓰는 파라미터 검증 유틸(`as_period`, `as_float`),
`signal_at` 가드(`check_row`), Signal.meta 용 변환 유틸(`float_or_none`),
ewm 수렴 캔들 수(`ewm_settle_bars`) 와 실시간 경로의 짧은 윈도우 경고(`warn_short_window`) 도 둔다.
"""

from __future__ import annotations

import logging
import math
from typing import Any

import numpy as np
import pandas as pd

from tradingbot.exceptions import DataError

__all__ = [
    "EWM_SETTLE_TOL",
    "as_float",
    "as_period",
    "atr",
    "bar_time",
    "bollinger",
    "check_row",
    "crossover",
    "crossunder",
    "ema",
    "ewm_settle_bars",
    "float_or_none",
    "fmt_price",
    "macd",
    "rsi",
    "sma",
    "true_range",
    "warn_short_window",
]

logger = logging.getLogger(__name__)

_OHLC_REQUIRED = ("high", "low", "close")

#: ewm 시드 가중치 ``(1-alpha)^n`` 이 이 값 이하로 떨어지면 "수렴" 으로 본다 (``recommended_candles`` 산출 기준).
#: 실제 KRW-BTC 1d / 1h 각 1000개 캔들에서 슬라이딩 윈도우 신호와 전체 이력 신호를 비교하면 1e-3 은 900 bar 중
#: 1~2개의 불일치를 남겼고 1e-4 부터 0 이었다. 1e-5 는 그 위에 여유를 둔 값이다 (EMA26: 150, RSI14: 156 캔들).
EWM_SETTLE_TOL: float = 1e-5


# ---------------------------------------------------------------------- 파라미터 검증 유틸
def as_period(value: Any, name: str, *, minimum: int = 1) -> int:
    """지표 기간 파라미터를 정수로 정규화한다.

    - int, 정수값 float(10.0), 숫자 문자열("10") 허용. bool 은 거부.
    - ``minimum`` 미만이면 ValueError.
    """
    if isinstance(value, bool):
        raise ValueError(f"{name} 은(는) 정수여야 합니다 (bool 불가): {value!r}")
    if isinstance(value, (int, np.integer)):
        n = int(value)
    elif isinstance(value, (float, np.floating)):
        if not math.isfinite(value) or not float(value).is_integer():
            raise ValueError(f"{name} 은(는) 정수여야 합니다: {value!r}")
        n = int(value)
    elif isinstance(value, str):
        try:
            n = int(value.strip())
        except ValueError as e:
            raise ValueError(f"{name} 은(는) 정수여야 합니다: {value!r}") from e
    else:
        raise ValueError(f"{name} 은(는) 정수여야 합니다: {value!r}")
    if n < minimum:
        raise ValueError(f"{name} 은(는) {minimum} 이상이어야 합니다: {n}")
    return n


def as_float(value: Any, name: str) -> float:
    """실수 파라미터를 float 로 정규화한다 (bool/NaN/inf 거부, 숫자 문자열 허용)."""
    if isinstance(value, bool):
        raise ValueError(f"{name} 은(는) 숫자여야 합니다 (bool 불가): {value!r}")
    if isinstance(value, str):
        try:
            x = float(value.strip())
        except ValueError as e:
            raise ValueError(f"{name} 은(는) 숫자여야 합니다: {value!r}") from e
    elif isinstance(value, (int, float, np.integer, np.floating)):
        x = float(value)
    else:
        raise ValueError(f"{name} 은(는) 숫자여야 합니다: {value!r}")
    if not math.isfinite(x):
        raise ValueError(f"{name} 은(는) 유한한 숫자여야 합니다: {value!r}")
    return x


def check_row(df: pd.DataFrame, i: int, required: tuple[str, ...] = ()) -> None:
    """전략 ``signal_at`` 공통 가드.

    - ``required`` 컬럼(prepare 가 추가한 지표 컬럼)이 없으면 DataError (prepare 누락).
    - ``i`` 가 ``0 <= i < len(df)`` 를 벗어나면 IndexError.
    """
    if not isinstance(df, pd.DataFrame):
        raise DataError(f"df 는 pandas DataFrame 이어야 합니다: {type(df).__name__}")
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise DataError(f"prepare() 되지 않은 DataFrame 입니다. 누락 컬럼: {missing}")
    if isinstance(i, bool) or not isinstance(i, (int, np.integer)):
        raise IndexError(f"행 인덱스 i 는 정수여야 합니다: {i!r}")
    if i < 0 or i >= len(df):
        raise IndexError(f"행 인덱스 범위 초과: i={i}, len={len(df)}")


def float_or_none(value: Any) -> float | None:
    """numpy/pandas 스칼라를 JSON 직렬화 가능한 float 로. NaN/None 은 None."""
    if value is None:
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) else x


def bar_time(df: pd.DataFrame, i: int) -> str | None:
    """행 i 의 timestamp 를 ISO 문자열로 (컬럼이 없거나 NaT 면 None). Signal.meta 기록용."""
    if "timestamp" not in df.columns:
        return None
    ts = df["timestamp"].iat[i]
    if pd.isna(ts):
        return None
    return pd.Timestamp(ts).isoformat()


def fmt_price(x: float) -> str:
    """reason 문자열용 가격 포맷 (KRW 같은 큰 값은 정수, 작은 값은 유효숫자)."""
    ax = abs(x)
    if ax >= 1000:
        return f"{x:,.0f}"
    if ax >= 1:
        return f"{x:,.2f}"
    return f"{x:.6g}"


# ---------------------------------------------------------------------- ewm 수렴 (슬라이딩 윈도우)
def ewm_settle_bars(alpha: float, tol: float = EWM_SETTLE_TOL) -> int:
    """``ewm(adjust=False)`` 재귀에서 시드(윈도우 첫 관측값) 의 가중치 ``(1-alpha)^n`` 이 ``tol`` 이하가 되는 최소 n.

    같은 재귀를 윈도우 첫 행에서 새로 시작하면 전체 이력과의 차이는 정확히 ``(1-alpha)^n × (시드 - 전체 이력값)``
    으로 줄어든다. 따라서 윈도우가 n 번 이상의 갱신을 포함하면 차이가 ``tol`` 배 이하가 된다.
    EMA(span=p) 는 ``alpha = 2/(p+1)``, Wilder RSI / ATR 은 ``alpha = 1/p``. ``alpha = 1`` (기간 1) 은 시드가
    남지 않으므로 0 이다.
    """
    alpha = as_float(alpha, "alpha")
    tol = as_float(tol, "tol")
    if not 0.0 < alpha <= 1.0:
        raise ValueError(f"alpha 는 0 초과 1 이하여야 합니다: {alpha}")
    if not 0.0 < tol < 1.0:
        raise ValueError(f"tol 은 0 초과 1 미만이어야 합니다: {tol}")
    if alpha >= 1.0:
        return 0
    n = max(0, math.ceil(math.log(tol) / math.log(1.0 - alpha)))
    # 경계가 정확히 맞아떨어질 때 부동소수 오차로 한 칸 넘치는 것을 바로잡는다 (예: 0.5^2 == 0.25)
    if n > 0 and (1.0 - alpha) ** (n - 1) <= tol:
        n -= 1
    return n


def warn_short_window(strategy: Any, n_rows: int, log: logging.Logger | None = None) -> None:
    """``generate_signal`` (실시간 경로) 보조: ``warmup <= n_rows < recommended_candles`` 이면 인스턴스당 한 번 경고한다.

    ``warmup`` 미만은 ``BaseStrategy.generate_signal`` 이 "데이터 부족" HOLD 로 알리므로 여기서 다루지 않는다.
    엔진/validate-config 가 ``recommended_candles`` 를 검사하지 않더라도 실거래 로그에 반드시 남기기 위한 안전장치다.
    전략은 ``__init__`` 에서 ``self._short_window_warned = False`` 를 두고 이 함수를 ``generate_signal`` 앞에서 부른다.
    """
    need = int(getattr(strategy, "recommended_candles", 0))
    if n_rows < strategy.warmup or n_rows >= need or getattr(strategy, "_short_window_warned", False):
        return
    strategy._short_window_warned = True
    (log or logger).warning(
        "%s: 전달된 캔들 %d개가 권장치 %d개(recommended_candles) 보다 적습니다. ewm 지표(EMA/RSI) 의 시드 효과로 "
        "전체 이력을 쓰는 백테스트와 다른 신호가 나올 수 있으니 engine.candle_limit 를 %d 이상으로 올리세요",
        strategy.name,
        n_rows,
        need,
        need,
    )


# ---------------------------------------------------------------------- 내부 헬퍼
def _check_series(s: pd.Series, name: str = "s") -> pd.Series:
    if not isinstance(s, pd.Series):
        raise DataError(f"{name} 은(는) pandas Series 여야 합니다: {type(s).__name__}")
    return s.astype(float) if s.dtype != float else s


def _check_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame):
        raise DataError(f"df 는 pandas DataFrame 이어야 합니다: {type(df).__name__}")
    missing = [c for c in _OHLC_REQUIRED if c not in df.columns]
    if missing:
        raise DataError(f"OHLC 컬럼 누락: {missing}")
    return df


# ---------------------------------------------------------------------- 이동평균
def sma(s: pd.Series, period: int) -> pd.Series:
    """단순 이동평균. 앞 period-1 행은 NaN."""
    period = as_period(period, "period")
    s = _check_series(s)
    return s.rolling(window=period, min_periods=period).mean()


def ema(s: pd.Series, period: int) -> pd.Series:
    """지수 이동평균 (``ewm(span=period, adjust=False)``).

    재귀식은 첫 관측값에서 시작하지만, 다른 지표와 같은 "워밍업 = NaN" 규약을 위해
    앞 period-1 행은 NaN 으로 가린다 (``min_periods=period``). 가려지지 않은 값은
    ``s.ewm(span=period, adjust=False).mean()`` 과 정확히 같다.
    """
    period = as_period(period, "period")
    s = _check_series(s)
    return s.ewm(span=period, adjust=False, min_periods=period).mean()


# ---------------------------------------------------------------------- 모멘텀
def rsi(s: pd.Series, period: int = 14) -> pd.Series:
    """Wilder RSI. 상승/하락폭을 ``ewm(alpha=1/period, adjust=False)`` 로 평활한다.

    첫 행의 변화량이 NaN 이므로 유효한 첫 RSI 는 인덱스 ``period`` (0-based) 이다.
    평균 하락폭이 0 이면 100, 평균 상승폭이 0 이면 0, 둘 다 0 (완전 횡보) 이면 NaN.
    """
    period = as_period(period, "period")
    s = _check_series(s)
    delta = s.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    denom = avg_gain + avg_loss
    # gain/(gain+0) → 100, 0/(0+loss) → 0, 0/0 (완전 횡보) → NaN
    with np.errstate(divide="ignore", invalid="ignore"):
        out = (100.0 * avg_gain / denom).mask(denom == 0.0)
    return out.rename(None)


def macd(
    s: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD. 반환 (macd, signal, hist).

    - macd = EMA(fast) - EMA(slow)  (유효 시작: 인덱스 slow-1)
    - signal = macd 선의 EMA(signal). macd 선이 유효해진 시점부터 재귀를 시작하므로
      유효 시작은 인덱스 slow+signal-2 이다.
    - hist = macd - signal
    """
    fast = as_period(fast, "fast")
    slow = as_period(slow, "slow")
    signal = as_period(signal, "signal")
    if fast >= slow:
        raise ValueError(f"fast({fast}) 는 slow({slow}) 보다 작아야 합니다")
    s = _check_series(s)
    macd_line = ema(s, fast) - ema(s, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


# ---------------------------------------------------------------------- 변동성
def bollinger(s: pd.Series, period: int = 20, num_std: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    """볼린저 밴드. 반환 (mid, upper, lower). 표준편차는 모집단(ddof=0)."""
    period = as_period(period, "period")
    num_std = as_float(num_std, "num_std")
    if num_std < 0:
        raise ValueError(f"num_std 는 0 이상이어야 합니다: {num_std}")
    s = _check_series(s)
    mid = s.rolling(window=period, min_periods=period).mean()
    std = s.rolling(window=period, min_periods=period).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    return mid, upper, lower


def true_range(df: pd.DataFrame) -> pd.Series:
    """True Range = max(high-low, |high-prev_close|, |low-prev_close|). 첫 행은 NaN (이전 종가 없음)."""
    df = _check_ohlc(df)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    prev_close = df["close"].astype(float).shift(1)
    hl = high - low
    hc = (high - prev_close).abs()
    lc = (low - prev_close).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1, skipna=False)
    return tr.rename(None)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder ATR = ``true_range.ewm(alpha=1/period, adjust=False)``. 유효 시작: 인덱스 period."""
    period = as_period(period, "period")
    tr = true_range(df)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


# ---------------------------------------------------------------------- 교차
def _align_other(a: pd.Series, b: pd.Series | float) -> pd.Series:
    if isinstance(b, pd.Series):
        if not a.index.equals(b.index):
            raise DataError("crossover/crossunder: 두 Series 의 인덱스가 다릅니다")
        return b.astype(float)
    return pd.Series(float(b), index=a.index)


def crossover(a: pd.Series, b: pd.Series | float) -> pd.Series:
    """a 가 b 를 상향 돌파한 행 (bool). 직전 행 a<=b 이고 현재 행 a>b. NaN 이 섞인 행은 False.

    b 는 스칼라(예: RSI 30 선) 도 허용한다.
    """
    a = _check_series(a, "a")
    b = _align_other(a, b)
    out = (a > b) & (a.shift(1) <= b.shift(1))
    return out.astype(bool).rename(None)


def crossunder(a: pd.Series, b: pd.Series | float) -> pd.Series:
    """a 가 b 를 하향 돌파한 행 (bool). 직전 행 a>=b 이고 현재 행 a<b. NaN 이 섞인 행은 False."""
    a = _check_series(a, "a")
    b = _align_other(a, b)
    out = (a < b) & (a.shift(1) >= b.shift(1))
    return out.astype(bool).rename(None)
