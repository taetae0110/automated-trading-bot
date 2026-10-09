"""볼린저 밴드 전략.

mode="reversion" (평균 회귀, 기본)
- 종가가 하단밴드 아래에 있다가 위로 복귀(상향 돌파) → BUY
- 종가가 중심선(SMA) 이상 → SELL (목표 도달, 청산)
  두 조건이 같은 행에서 동시에 참이면 (큰 갭 상승) SELL 을 우선한다: 이미 목표가에 도달한
  진입은 기대 수익이 없기 때문이다. 포지션이 없으면 엔진/백테스터가 SELL 을 무시한다.

mode="breakout" (추세 추종)
- 종가가 상단밴드를 상향 돌파 → BUY
- 종가가 중심선을 하향 돌파 → SELL
  (상단 위와 중심선 아래는 동시에 성립할 수 없으므로 충돌 없음)

밴드: ``indicators.bollinger`` (SMA ± num_std × 모집단 표준편차, ddof=0).
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

MODES: tuple[str, ...] = ("reversion", "breakout")
_COLUMNS = ("bb_mid", "bb_upper", "bb_lower", "bb_buy", "bb_sell")


class BollingerStrategy(BaseStrategy):
    name = "bollinger"
    description = (
        "볼린저 밴드: reversion(하단 복귀 매수/중심선 청산) 또는 breakout(상단 돌파 매수/중심선 이탈 매도)"
    )
    default_params: dict[str, Any] = {"period": 20, "num_std": 2.0, "mode": "reversion"}

    def __init__(self, **params: Any) -> None:
        super().__init__(**params)
        # 표준편차가 의미 있으려면 최소 2개 관측값이 필요하다.
        self.period: int = ind.as_period(self.params["period"], "period", minimum=2)
        self.num_std: float = ind.as_float(self.params["num_std"], "num_std")
        if self.num_std <= 0:
            raise ValueError(f"{self.name}: num_std 는 0 보다 커야 합니다: {self.num_std}")
        mode = self.params["mode"]
        if not isinstance(mode, str) or mode.strip().lower() not in MODES:
            raise ValueError(f"{self.name}: mode 는 {MODES} 중 하나여야 합니다: {mode!r}")
        self.mode: str = mode.strip().lower()
        self.params["period"] = self.period
        self.params["num_std"] = self.num_std
        self.params["mode"] = self.mode

    @property
    def warmup(self) -> int:
        # 밴드 유효 시작 = 인덱스 period-1 → 교차 판정은 그 다음 행부터.
        return self.period + 1

    @property
    def recommended_candles(self) -> int:
        """실시간 엔진이 넘겨야 할 캔들 수 권장치. rolling 지표라 창 밖의 과거를 보지 않으므로 warmup 과 같다."""
        return self.warmup

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        if "close" not in df.columns:
            raise DataError(f"{self.name}: 'close' 컬럼이 필요합니다")
        out = df.copy()
        close = out["close"].astype(float)
        mid, upper, lower = ind.bollinger(close, self.period, self.num_std)
        out["bb_mid"] = mid
        out["bb_upper"] = upper
        out["bb_lower"] = lower
        if self.mode == "reversion":
            out["bb_buy"] = ind.crossover(close, lower)
            # NaN 비교는 False 이므로 워밍업 구간은 자동으로 False
            out["bb_sell"] = (close >= mid).astype(bool)
        else:
            out["bb_buy"] = ind.crossover(close, upper)
            out["bb_sell"] = ind.crossunder(close, mid)
        return out

    def signal_at(self, symbol: str, df: pd.DataFrame, i: int) -> Signal:
        ind.check_row(df, i, _COLUMNS)
        if i < self.warmup - 1:
            return Signal.hold(symbol, reason=f"워밍업 ({i + 1}/{self.warmup})")

        close = ind.float_or_none(df["close"].iat[i]) if "close" in df.columns else None
        mid = ind.float_or_none(df["bb_mid"].iat[i])
        upper = ind.float_or_none(df["bb_upper"].iat[i])
        lower = ind.float_or_none(df["bb_lower"].iat[i])
        prev_mid = ind.float_or_none(df["bb_mid"].iat[i - 1])
        if close is None or mid is None or upper is None or lower is None or prev_mid is None:
            return Signal.hold(symbol, reason="지표 미산출 (NaN)")

        width = upper - lower
        pct_b = (close - lower) / width if width > 0 else None
        meta: dict[str, Any] = {
            "mode": self.mode,
            "period": self.period,
            "num_std": self.num_std,
            "close": close,
            "bb_mid": mid,
            "bb_upper": upper,
            "bb_lower": lower,
            "pct_b": pct_b,
            "timestamp": ind.bar_time(df, i),
        }
        buy = bool(df["bb_buy"].iat[i])
        sell = bool(df["bb_sell"].iat[i])
        tag = f"BB({self.period},{self.num_std:g})"

        if self.mode == "reversion":
            if sell:
                reason = f"{tag} 종가 {ind.fmt_price(close)} >= 중심선 {ind.fmt_price(mid)} (청산)"
                logger.debug("%s %s: %s", self.name, symbol, reason)
                return Signal(action=SignalAction.SELL, symbol=symbol, strength=1.0, reason=reason, meta=meta)
            if buy:
                reason = f"{tag} 종가 하단밴드 {ind.fmt_price(lower)} 상향 복귀 (매수)"
                logger.debug("%s %s: %s", self.name, symbol, reason)
                return Signal(action=SignalAction.BUY, symbol=symbol, strength=1.0, reason=reason, meta=meta)
        else:
            if buy:
                reason = f"{tag} 종가 상단밴드 {ind.fmt_price(upper)} 돌파 (매수)"
                logger.debug("%s %s: %s", self.name, symbol, reason)
                return Signal(action=SignalAction.BUY, symbol=symbol, strength=1.0, reason=reason, meta=meta)
            if sell:
                reason = f"{tag} 종가 중심선 {ind.fmt_price(mid)} 하향 이탈 (매도)"
                logger.debug("%s %s: %s", self.name, symbol, reason)
                return Signal(action=SignalAction.SELL, symbol=symbol, strength=1.0, reason=reason, meta=meta)

        sig = Signal.hold(symbol, reason=f"{tag} 밴드 내 ({self.mode}) 신호 없음")
        sig.meta = meta
        return sig


__all__ = ["MODES", "BollingerStrategy"]
