"""MACD 전략.

- MACD 선이 시그널 선을 상향 돌파 → BUY
- MACD 선이 시그널 선을 하향 돌파 → SELL
- 그 외 → HOLD

MACD = EMA(fast) - EMA(slow), 시그널 = MACD 의 EMA(signal), 히스토그램 = MACD - 시그널
(``indicators.macd``, 모든 EMA 는 adjust=False).

``signal`` 은 2 이상이어야 한다: EMA(1) 은 항등이라 시그널 == MACD, 히스토그램 == 0 이 되어 교차가 영원히 없다.
모든 EMA 가 윈도우 첫 행으로 시드되므로 실시간 엔진의 ``candle_limit`` 는 ``warmup``(NaN 이 아닌 최소) 이 아니라
``recommended_candles``(시드 효과가 사라지는 길이) 이상이어야 백테스트와 같은 신호가 난다.
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from tradingbot.exceptions import DataError
from tradingbot.models import Signal, SignalAction
from tradingbot.strategies import indicators as ind
from tradingbot.strategies.base import BaseStrategy

logger = logging.getLogger(__name__)

_COLUMNS = ("macd", "macd_signal", "macd_hist", "cross_up", "cross_down")


class MACDStrategy(BaseStrategy):
    name = "macd"
    description = "MACD 선이 시그널 선을 상향 돌파하면 매수, 하향 돌파하면 매도"
    default_params: dict[str, Any] = {"fast": 12, "slow": 26, "signal": 9}

    def __init__(self, **params: Any) -> None:
        super().__init__(**params)
        self.fast: int = ind.as_period(self.params["fast"], "fast")
        self.slow: int = ind.as_period(self.params["slow"], "slow")
        # signal=1 이면 ema(macd, 1) 이 항등 → 히스토그램이 항상 0 → 신호가 전혀 나오지 않는 죽은 설정
        self.signal: int = ind.as_period(self.params["signal"], "signal", minimum=2)
        if self.fast >= self.slow:
            raise ValueError(f"{self.name}: fast({self.fast}) 는 slow({self.slow}) 보다 작아야 합니다")
        self.params["fast"] = self.fast
        self.params["slow"] = self.slow
        self.params["signal"] = self.signal
        self._short_window_warned = False

    @property
    def warmup(self) -> int:
        # MACD 유효 시작 = slow-1, 시그널 유효 시작 = slow+signal-2 → 교차 판정은 slow+signal-1 부터.
        return self.slow + self.signal

    @property
    def recommended_candles(self) -> int:
        """실시간 엔진이 넘겨야 할 캔들 수 권장치 (백테스트와 같은 신호를 내기 위한 ``engine.candle_limit`` 하한).

        시그널 선은 MACD 선(느린 EMA 에 종속) 의 EMA 이므로 가장 긴 기간의 EMA 시드가 사라질 때까지 기다린다.
        """
        slowest = max(self.slow, self.signal)
        return self.warmup + ind.ewm_settle_bars(2.0 / (slowest + 1))

    def generate_signal(self, symbol: str, df: pd.DataFrame) -> Signal:
        ind.warn_short_window(self, len(df), logger)
        return super().generate_signal(symbol, df)

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        if "close" not in df.columns:
            raise DataError(f"{self.name}: 'close' 컬럼이 필요합니다")
        out = df.copy()
        macd_line, signal_line, hist = ind.macd(out["close"].astype(float), self.fast, self.slow, self.signal)
        out["macd"] = macd_line
        out["macd_signal"] = signal_line
        out["macd_hist"] = hist
        out["cross_up"] = ind.crossover(out["macd"], out["macd_signal"])
        out["cross_down"] = ind.crossunder(out["macd"], out["macd_signal"])
        return out

    def signal_at(self, symbol: str, df: pd.DataFrame, i: int) -> Signal:
        ind.check_row(df, i, _COLUMNS)
        if i < self.warmup - 1:
            return Signal.hold(symbol, reason=f"워밍업 ({i + 1}/{self.warmup})")

        macd_v = ind.float_or_none(df["macd"].iat[i])
        signal_v = ind.float_or_none(df["macd_signal"].iat[i])
        hist_v = ind.float_or_none(df["macd_hist"].iat[i])
        prev_macd = ind.float_or_none(df["macd"].iat[i - 1])
        prev_signal = ind.float_or_none(df["macd_signal"].iat[i - 1])
        if macd_v is None or signal_v is None or hist_v is None or prev_macd is None or prev_signal is None:
            return Signal.hold(symbol, reason="지표 미산출 (NaN)")

        meta: dict[str, Any] = {
            "fast": self.fast,
            "slow": self.slow,
            "signal_period": self.signal,
            "macd": macd_v,
            "macd_signal": signal_v,
            "macd_hist": hist_v,
            "close": ind.float_or_none(df["close"].iat[i]) if "close" in df.columns else None,
            "timestamp": ind.bar_time(df, i),
        }
        tag = f"MACD({self.fast},{self.slow},{self.signal})"
        if bool(df["cross_up"].iat[i]):
            reason = f"{tag} 시그널 상향 돌파 (hist {hist_v:+.4g})"
            logger.debug("%s %s: %s", self.name, symbol, reason)
            return Signal(action=SignalAction.BUY, symbol=symbol, strength=1.0, reason=reason, meta=meta)
        if bool(df["cross_down"].iat[i]):
            reason = f"{tag} 시그널 하향 돌파 (hist {hist_v:+.4g})"
            logger.debug("%s %s: %s", self.name, symbol, reason)
            return Signal(action=SignalAction.SELL, symbol=symbol, strength=1.0, reason=reason, meta=meta)

        sig = Signal.hold(symbol, reason=f"{tag} 교차 없음 (hist {hist_v:+.4g})")
        sig.meta = meta
        return sig


__all__ = ["MACDStrategy"]
