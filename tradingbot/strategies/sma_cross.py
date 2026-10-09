"""이동평균 교차 전략 (SMA / EMA).

- 단기선이 장기선을 상향 돌파(골든크로스) → BUY
- 단기선이 장기선을 하향 돌파(데드크로스) → SELL
- 그 외 → HOLD

``SMACrossStrategy`` (레지스트리 ``sma_cross``) 와 ``EMACrossStrategy`` (``ema_cross``) 는
이동평균 함수와 컬럼/사유 라벨만 다르고 로직은 ``_MACrossStrategy`` 를 공유한다.
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from typing import Any, ClassVar

import pandas as pd

from tradingbot.exceptions import DataError
from tradingbot.models import Signal, SignalAction
from tradingbot.strategies import indicators as ind
from tradingbot.strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


class _MACrossStrategy(BaseStrategy):
    """SMA/EMA 교차 공통 구현. 서브클래스는 ``label`` 과 ``_moving_average`` 만 정한다."""

    #: reason / 컬럼 접두어 ("SMA" 또는 "EMA")
    label: ClassVar[str] = "MA"

    def __init__(self, **params: Any) -> None:
        super().__init__(**params)
        self.fast: int = ind.as_period(self.params["fast"], "fast")
        self.slow: int = ind.as_period(self.params["slow"], "slow")
        if self.fast >= self.slow:
            raise ValueError(f"{self.name}: fast({self.fast}) 는 slow({self.slow}) 보다 작아야 합니다")
        # 정규화된 값(문자열/실수 → int)을 params 에 반영
        self.params["fast"] = self.fast
        self.params["slow"] = self.slow
        prefix = self.label.lower()
        self.fast_col: str = f"{prefix}_fast"
        self.slow_col: str = f"{prefix}_slow"

    # ------------------------------------------------------------------ 서브클래스 훅
    @abstractmethod
    def _moving_average(self, s: pd.Series, period: int) -> pd.Series:
        """이동평균 함수 (SMA 또는 EMA)."""

    # ------------------------------------------------------------------ BaseStrategy
    @property
    def warmup(self) -> int:
        # 장기선이 유효해지는 행(slow-1) 다음 행부터 교차를 판정할 수 있다.
        return self.slow + 1

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        if "close" not in df.columns:
            raise DataError(f"{self.name}: 'close' 컬럼이 필요합니다")
        out = df.copy()
        close = out["close"].astype(float)
        out[self.fast_col] = self._moving_average(close, self.fast)
        out[self.slow_col] = self._moving_average(close, self.slow)
        out["cross_up"] = ind.crossover(out[self.fast_col], out[self.slow_col])
        out["cross_down"] = ind.crossunder(out[self.fast_col], out[self.slow_col])
        return out

    def signal_at(self, symbol: str, df: pd.DataFrame, i: int) -> Signal:
        ind.check_row(df, i, (self.fast_col, self.slow_col, "cross_up", "cross_down"))
        if i < self.warmup - 1:
            return Signal.hold(symbol, reason=f"워밍업 ({i + 1}/{self.warmup})")

        fast_v = ind.float_or_none(df[self.fast_col].iat[i])
        slow_v = ind.float_or_none(df[self.slow_col].iat[i])
        prev_fast = ind.float_or_none(df[self.fast_col].iat[i - 1])
        prev_slow = ind.float_or_none(df[self.slow_col].iat[i - 1])
        if None in (fast_v, slow_v, prev_fast, prev_slow):
            return Signal.hold(symbol, reason="지표 미산출 (NaN)")

        meta: dict[str, Any] = {
            "fast": self.fast,
            "slow": self.slow,
            f"{self.label.lower()}_fast": fast_v,
            f"{self.label.lower()}_slow": slow_v,
            "close": ind.float_or_none(df["close"].iat[i]) if "close" in df.columns else None,
            "timestamp": ind.bar_time(df, i),
        }
        tag_fast = f"{self.label}{self.fast}"
        tag_slow = f"{self.label}{self.slow}"
        if bool(df["cross_up"].iat[i]):
            reason = f"{tag_fast} > {tag_slow} 골든크로스"
            logger.debug("%s %s: %s", self.name, symbol, reason)
            return Signal(action=SignalAction.BUY, symbol=symbol, strength=1.0, reason=reason, meta=meta)
        if bool(df["cross_down"].iat[i]):
            reason = f"{tag_fast} < {tag_slow} 데드크로스"
            logger.debug("%s %s: %s", self.name, symbol, reason)
            return Signal(action=SignalAction.SELL, symbol=symbol, strength=1.0, reason=reason, meta=meta)

        sig = Signal.hold(symbol, reason=f"{tag_fast}/{tag_slow} 교차 없음")
        sig.meta = meta
        return sig


class SMACrossStrategy(_MACrossStrategy):
    """단순 이동평균(SMA) 골든/데드크로스."""

    name = "sma_cross"
    description = "단순 이동평균 교차: 단기 SMA 가 장기 SMA 를 상향 돌파하면 매수, 하향 돌파하면 매도"
    default_params: dict[str, Any] = {"fast": 10, "slow": 30}
    label = "SMA"

    def _moving_average(self, s: pd.Series, period: int) -> pd.Series:
        return ind.sma(s, period)


class EMACrossStrategy(_MACrossStrategy):
    """지수 이동평균(EMA) 골든/데드크로스."""

    name = "ema_cross"
    description = "지수 이동평균 교차: 단기 EMA 가 장기 EMA 를 상향 돌파하면 매수, 하향 돌파하면 매도"
    default_params: dict[str, Any] = {"fast": 12, "slow": 26}
    label = "EMA"

    def _moving_average(self, s: pd.Series, period: int) -> pd.Series:
        return ind.ema(s, period)


__all__ = ["EMACrossStrategy", "SMACrossStrategy"]
