"""변동성 돌파 전략 (래리 윌리엄스, 일봉 기준).

행 i (가장 최근에 완성된 캔들) 에서 ``range = high_i - low_i`` 를 구하고,
"다음 캔들 시가 + k × range" 를 상향 돌파하면 매수, 그 다음 캔들 시가에 매도한다.

- 신호: ``Signal(action=BUY, order_type=STOP, price=None, stop_offset=k*range, max_holding_bars=1)``
  트리거 가격(다음 시가 + stop_offset) 은 엔진/백테스터가 다음 캔들의 시가를 알게 된 시점에 계산한다.
- ``ma_period > 0`` 이면 추세 필터: ``close_i < SMA(ma_period)`` 이면 HOLD.
- SELL 신호는 내지 않는다. 청산은 ``max_holding_bars=1`` (다음 캔들 시가) 로 처리된다.
- 포지션 보유 중의 BUY 는 엔진/백테스터가 무시한다.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from tradingbot.exceptions import DataError
from tradingbot.models import OrderType, Signal, SignalAction
from tradingbot.strategies import indicators as ind
from tradingbot.strategies.base import BaseStrategy

logger = logging.getLogger(__name__)

_REQUIRED_INPUT = ("high", "low", "close")
_COLUMNS = ("vb_range", "vb_offset", "vb_ma")


class VolatilityBreakoutStrategy(BaseStrategy):
    name = "volatility_breakout"
    description = "변동성 돌파: 다음 캔들 시가 + k×(전일 고가-저가) 돌파 시 매수, 그 다음 캔들 시가 매도"
    default_params: dict[str, Any] = {"k": 0.5, "ma_period": 0}

    def __init__(self, **params: Any) -> None:
        super().__init__(**params)
        self.k: float = ind.as_float(self.params["k"], "k")
        if self.k <= 0:
            raise ValueError(f"{self.name}: k 는 0 보다 커야 합니다: {self.k}")
        # 0 = 추세 필터 사용 안 함
        self.ma_period: int = ind.as_period(self.params["ma_period"], "ma_period", minimum=0)
        self.params["k"] = self.k
        self.params["ma_period"] = self.ma_period

    @property
    def warmup(self) -> int:
        # 필터 없음: 완성 캔들 1개면 range 를 구할 수 있다. 필터 있음: SMA 유효 행(ma_period-1) + 여유 1.
        return self.ma_period + 1 if self.ma_period > 0 else 1

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        missing = [c for c in _REQUIRED_INPUT if c not in df.columns]
        if missing:
            raise DataError(f"{self.name}: 컬럼 누락 {missing}")
        out = df.copy()
        high = out["high"].astype(float)
        low = out["low"].astype(float)
        close = out["close"].astype(float)
        out["vb_range"] = high - low
        out["vb_offset"] = self.k * out["vb_range"]
        if self.ma_period > 0:
            out["vb_ma"] = ind.sma(close, self.ma_period)
        else:
            # 스키마를 고정하기 위해 항상 컬럼을 둔다 (필터 미사용 시 NaN).
            out["vb_ma"] = np.nan
        return out

    def signal_at(self, symbol: str, df: pd.DataFrame, i: int) -> Signal:
        ind.check_row(df, i, _COLUMNS)
        if i < self.warmup - 1:
            return Signal.hold(symbol, reason=f"워밍업 ({i + 1}/{self.warmup})")

        rng = ind.float_or_none(df["vb_range"].iat[i])
        close = ind.float_or_none(df["close"].iat[i]) if "close" in df.columns else None
        ma = ind.float_or_none(df["vb_ma"].iat[i])
        if rng is None or close is None:
            return Signal.hold(symbol, reason="지표 미산출 (NaN)")

        meta: dict[str, Any] = {
            "k": self.k,
            "ma_period": self.ma_period,
            "range": rng,
            "stop_offset": self.k * rng,
            "high": ind.float_or_none(df["high"].iat[i]) if "high" in df.columns else None,
            "low": ind.float_or_none(df["low"].iat[i]) if "low" in df.columns else None,
            "close": close,
            "ma": ma,
            "timestamp": ind.bar_time(df, i),
        }
        if rng <= 0:
            sig = Signal.hold(symbol, reason="변동폭 0 (고가 = 저가) → 돌파 기준 없음")
            sig.meta = meta
            return sig
        if self.ma_period > 0:
            if ma is None:
                return Signal.hold(symbol, reason="지표 미산출 (NaN)")
            if close < ma:
                sig = Signal.hold(
                    symbol,
                    reason=f"종가 {ind.fmt_price(close)} < SMA{self.ma_period} {ind.fmt_price(ma)} (추세 필터)",
                )
                sig.meta = meta
                return sig

        stop_offset = self.k * rng
        reason = (
            f"변동성 돌파 대기: 다음 시가 + {self.k:g}×{ind.fmt_price(rng)} = +{ind.fmt_price(stop_offset)}"
        )
        if self.ma_period > 0 and ma is not None:
            reason += f" (종가 >= SMA{self.ma_period})"
        logger.debug("%s %s: %s", self.name, symbol, reason)
        return Signal(
            action=SignalAction.BUY,
            symbol=symbol,
            strength=1.0,
            reason=reason,
            order_type=OrderType.STOP,
            price=None,
            stop_offset=stop_offset,
            max_holding_bars=1,
            meta=meta,
        )


__all__ = ["VolatilityBreakoutStrategy"]
