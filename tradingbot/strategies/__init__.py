"""전략 레지스트리.

새 전략을 추가하려면 BaseStrategy 를 상속한 클래스를 만들고 아래 _REGISTRY 에 등록한다.
"""

from __future__ import annotations

import importlib
from typing import Any

from tradingbot.exceptions import ConfigError
from tradingbot.strategies.base import BaseStrategy, candles_to_df, df_to_candles

_REGISTRY: dict[str, tuple[str, str]] = {
    "sma_cross": ("tradingbot.strategies.sma_cross", "SMACrossStrategy"),
    "ema_cross": ("tradingbot.strategies.sma_cross", "EMACrossStrategy"),
    "rsi": ("tradingbot.strategies.rsi", "RSIStrategy"),
    "bollinger": ("tradingbot.strategies.bollinger", "BollingerStrategy"),
    "macd": ("tradingbot.strategies.macd", "MACDStrategy"),
    "volatility_breakout": ("tradingbot.strategies.volatility_breakout", "VolatilityBreakoutStrategy"),
}


def available_strategies() -> list[str]:
    return sorted(_REGISTRY)


def get_strategy_class(name: str) -> type[BaseStrategy]:
    key = name.lower()
    if key not in _REGISTRY:
        raise ConfigError(f"알 수 없는 전략: {name!r}. 가능: {', '.join(available_strategies())}")
    module_name, class_name = _REGISTRY[key]
    module = importlib.import_module(module_name)
    return getattr(module, class_name)


def create_strategy(name: str, params: dict[str, Any] | None = None) -> BaseStrategy:
    cls = get_strategy_class(name)
    try:
        return cls(**(params or {}))
    except (TypeError, ValueError) as e:
        raise ConfigError(f"전략 {name!r} 파라미터 오류: {e}") from e


__all__ = [
    "BaseStrategy",
    "available_strategies",
    "candles_to_df",
    "create_strategy",
    "df_to_candles",
    "get_strategy_class",
]
