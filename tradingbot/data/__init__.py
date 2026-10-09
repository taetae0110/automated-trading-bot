"""데이터 모듈: 캔들 CSV 저장소(CandleStore) 와 yfinance 로더.

- ``CandleStore``   : ``{data_dir}/{broker}/{symbol_safe}_{interval}.csv`` 에 실제 거래소 캔들을 캐시하고,
                      브로커 ``get_candles`` 를 과거 방향으로 페이지네이션하여 기간 전체를 내려받는다.
- ``load_yfinance`` : Yahoo Finance(yfinance, 선택 의존성) 에서 캔들을 받아 규약 DataFrame 으로 변환한다.

원칙: 샘플/데모 시세는 절대 만들지 않는다. 모든 데이터는 실제 거래소/데이터 제공자에서 받는다.
"""

from __future__ import annotations

from tradingbot.data.store import (
    CandleStore,
    empty_candles_df,
    filter_candles_df,
    normalize_candles_df,
    symbol_safe,
    to_utc_datetime,
)
from tradingbot.data.yfinance_feed import (
    YF_INTERVAL_MAP,
    krx_to_yfinance,
    load_yfinance,
    to_yfinance_interval,
    yfinance_to_candles_df,
)

__all__ = [
    "YF_INTERVAL_MAP",
    "CandleStore",
    "empty_candles_df",
    "filter_candles_df",
    "krx_to_yfinance",
    "load_yfinance",
    "normalize_candles_df",
    "symbol_safe",
    "to_utc_datetime",
    "to_yfinance_interval",
    "yfinance_to_candles_df",
]
