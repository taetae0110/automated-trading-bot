"""RSI 전략.

- RSI 가 과매도선(oversold) 을 **상향 돌파** → BUY (과매도 탈출)
- RSI 가 과매수선(overbought) 을 **하향 돌파** → SELL (과매수 이탈)
- 그 외 → HOLD

RSI 는 Wilder 방식 (``indicators.rsi``: ewm alpha=1/period, adjust=False).

파라미터 제약 (docs/STRATEGIES.md 와 동일): ``period >= 2`` (1 이면 RSI 가 0/100 만 취한다),
``0 < oversold < overbought < 100`` (RSI 는 0 또는 100 에 정확히 닿는 일이 사실상 없어 경계값은 신호가 전혀 없다).
Wilder 평활이 윈도우 첫 변화량으로 시드되므로 실시간 엔진의 ``candle_limit`` 는 ``recommended_candles`` 이상이어야
백테스트와 같은 신호가 난다.
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

_COLUMNS = ("rsi", "rsi_cross_up", "rsi_cross_down")


class RSIStrategy(BaseStrategy):
    name = "rsi"
    description = "RSI 가 과매도선을 상향 돌파하면 매수, 과매수선을 하향 돌파하면 매도"
    default_params: dict[str, Any] = {"period": 14, "oversold": 30, "overbought": 70}

    def __init__(self, **params: Any) -> None:
        super().__init__(**params)
        # period=1 이면 평활이 없어 RSI 가 0/100 만 취한다 (돌파 판정이 무의미)
        self.period: int = ind.as_period(self.params["period"], "period", minimum=2)
        self.oversold: float = ind.as_float(self.params["oversold"], "oversold")
        self.overbought: float = ind.as_float(self.params["overbought"], "overbought")
        # 경계(0, 100) 는 RSI 가 사실상 닿지 않으므로 신호가 영원히 없다 → 열린 구간만 허용
        if not (0.0 < self.oversold < self.overbought < 100.0):
            raise ValueError(
                f"{self.name}: 0 < oversold({self.oversold:g}) < overbought({self.overbought:g}) < 100 이어야 합니다"
            )
        self.params["period"] = self.period
        self.params["oversold"] = self.oversold
        self.params["overbought"] = self.overbought
        self._short_window_warned = False

    @property
    def warmup(self) -> int:
        # RSI 유효 시작 = 인덱스 period (첫 변화량이 NaN) → 교차 판정은 그 다음 행부터.
        return self.period + 2

    @property
    def recommended_candles(self) -> int:
        """실시간 엔진이 넘겨야 할 캔들 수 권장치 (백테스트와 같은 신호를 내기 위한 ``engine.candle_limit`` 하한)."""
        return self.warmup + ind.ewm_settle_bars(1.0 / self.period)

    def generate_signal(self, symbol: str, df: pd.DataFrame) -> Signal:
        ind.warn_short_window(self, len(df), logger)
        return super().generate_signal(symbol, df)

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        if "close" not in df.columns:
            raise DataError(f"{self.name}: 'close' 컬럼이 필요합니다")
        out = df.copy()
        out["rsi"] = ind.rsi(out["close"].astype(float), self.period)
        out["rsi_cross_up"] = ind.crossover(out["rsi"], self.oversold)
        out["rsi_cross_down"] = ind.crossunder(out["rsi"], self.overbought)
        return out

    def signal_at(self, symbol: str, df: pd.DataFrame, i: int) -> Signal:
        ind.check_row(df, i, _COLUMNS)
        if i < self.warmup - 1:
            return Signal.hold(symbol, reason=f"워밍업 ({i + 1}/{self.warmup})")

        rsi_v = ind.float_or_none(df["rsi"].iat[i])
        prev_rsi = ind.float_or_none(df["rsi"].iat[i - 1])
        if rsi_v is None or prev_rsi is None:
            return Signal.hold(symbol, reason="지표 미산출 (NaN)")

        meta: dict[str, Any] = {
            "period": self.period,
            "rsi": rsi_v,
            "prev_rsi": prev_rsi,
            "oversold": self.oversold,
            "overbought": self.overbought,
            "close": ind.float_or_none(df["close"].iat[i]) if "close" in df.columns else None,
            "timestamp": ind.bar_time(df, i),
        }
        tag = f"RSI({self.period})"
        if bool(df["rsi_cross_up"].iat[i]):
            reason = f"{tag} {self.oversold:g} 상향 돌파 (RSI {rsi_v:.1f})"
            logger.debug("%s %s: %s", self.name, symbol, reason)
            return Signal(action=SignalAction.BUY, symbol=symbol, strength=1.0, reason=reason, meta=meta)
        if bool(df["rsi_cross_down"].iat[i]):
            reason = f"{tag} {self.overbought:g} 하향 돌파 (RSI {rsi_v:.1f})"
            logger.debug("%s %s: %s", self.name, symbol, reason)
            return Signal(action=SignalAction.SELL, symbol=symbol, strength=1.0, reason=reason, meta=meta)

        sig = Signal.hold(symbol, reason=f"{tag}={rsi_v:.1f} 신호 없음")
        sig.meta = meta
        return sig


__all__ = ["RSIStrategy"]
