"""백테스트: 실제 거래소 캔들 위에서 전략/리스크/모의 체결을 bar 단위로 재생하고 성과 지표를 계산한다."""

from __future__ import annotations

from tradingbot.backtest.engine import FILL_ON_CHOICES, Backtester, BacktestResult
from tradingbot.backtest.metrics import (
    METRIC_KEYS,
    compute_metrics,
    drawdown_series,
    format_metrics,
    periods_per_year,
)

__all__ = [
    "FILL_ON_CHOICES",
    "METRIC_KEYS",
    "Backtester",
    "BacktestResult",
    "compute_metrics",
    "drawdown_series",
    "format_metrics",
    "periods_per_year",
]
