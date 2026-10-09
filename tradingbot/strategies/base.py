"""전략 공통 인터페이스.

전략은 pandas DataFrame 으로 캔들을 받는다. 컬럼: timestamp(UTC), open, high, low, close, volume.
인덱스는 0..n-1 RangeIndex, 오래된 → 최신 순. 마지막 행은 "가장 최근에 *완성된* 캔들" 이다.

두 단계로 나뉜다:
1. prepare(df)          : 지표 컬럼을 벡터 연산으로 추가 (미래 데이터 참조 금지! shift/rolling 만 사용)
2. signal_at(symbol, df, i): prepare 된 df 에서 i 번째 행까지의 정보만 보고 신호 생성

백테스터는 prepare 를 1회 호출한 뒤 i 를 증가시키며 signal_at 을 호출하고,
실시간 엔진은 최근 N개 캔들로 generate_signal(= prepare + 마지막 행 signal_at) 을 호출한다.
따라서 백테스트와 실거래가 동일한 코드 경로를 탄다.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

import pandas as pd

from tradingbot.models import Candle, Signal

logger = logging.getLogger(__name__)

CANDLE_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


def candles_to_df(candles: list[Candle]) -> pd.DataFrame:
    """list[Candle] → DataFrame (오래된→최신, RangeIndex)."""
    if not candles:
        return pd.DataFrame(columns=CANDLE_COLUMNS)
    df = pd.DataFrame([c.as_dict() for c in candles], columns=CANDLE_COLUMNS)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values("timestamp").drop_duplicates("timestamp", keep="last").reset_index(drop=True)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    return df


def df_to_candles(df: pd.DataFrame) -> list[Candle]:
    out: list[Candle] = []
    for row in df.itertuples(index=False):
        out.append(
            Candle(
                timestamp=row.timestamp.to_pydatetime(),
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                volume=float(row.volume),
            )
        )
    return out


class BaseStrategy(ABC):
    #: 레지스트리 키 (설정 파일 strategy.name)
    name: str = "base"
    #: 사람이 읽는 설명 (한국어)
    description: str = ""
    #: 기본 파라미터. __init__(**params) 로 덮어쓴다.
    default_params: dict[str, Any] = {}

    def __init__(self, **params: Any) -> None:
        unknown = set(params) - set(self.default_params)
        if unknown:
            raise ValueError(f"{self.name} 전략이 모르는 파라미터: {sorted(unknown)}")
        self.params: dict[str, Any] = {**self.default_params, **params}

    # ------------------------------------------------------------------ 필수 구현
    @property
    @abstractmethod
    def warmup(self) -> int:
        """신호를 내기 위해 필요한 최소 캔들 개수 (지표 기간 중 최댓값 + 여유)."""

    @abstractmethod
    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        """지표 컬럼 추가. 입력을 변경하지 말고 복사본을 반환할 것. 미래 참조 금지."""

    @abstractmethod
    def signal_at(self, symbol: str, df: pd.DataFrame, i: int) -> Signal:
        """prepare 된 df 의 행 i (및 그 이전) 만 보고 신호를 만든다.

        i < warmup-1 이면 HOLD 를 반환해야 한다. 포지션 보유 여부는 전략이 알 필요 없다.
        (엔진/백테스터가 "포지션 없는데 SELL", "이미 보유인데 BUY" 를 무시한다.)
        """

    # ------------------------------------------------------------------ 편의
    def generate_signal(self, symbol: str, df: pd.DataFrame) -> Signal:
        if len(df) < self.warmup:
            return Signal.hold(symbol, reason=f"데이터 부족 ({len(df)}/{self.warmup})")
        prepared = self.prepare(df)
        return self.signal_at(symbol, prepared, len(prepared) - 1)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} {self.params}>"
