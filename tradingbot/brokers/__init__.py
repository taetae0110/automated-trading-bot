"""브로커 레지스트리.

사용: broker = create_broker("upbit", config)   # config: tradingbot.config.AppConfig
키/시크릿은 환경변수에서 읽는다 (config.py 의 Credentials 참고).
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from tradingbot.brokers.base import BaseBroker
from tradingbot.exceptions import ConfigError

if TYPE_CHECKING:  # pragma: no cover
    from tradingbot.config import AppConfig

# name -> (module, class). 모듈은 지연 로딩되어 선택적 의존성(ccxt 등)이 없어도 다른 브로커를 쓸 수 있다.
_REGISTRY: dict[str, tuple[str, str]] = {
    "paper": ("tradingbot.brokers.paper", "PaperBroker"),
    "upbit": ("tradingbot.brokers.upbit", "UpbitBroker"),
    "binance": ("tradingbot.brokers.ccxt_broker", "CCXTBroker"),
    "ccxt": ("tradingbot.brokers.ccxt_broker", "CCXTBroker"),
    "kis": ("tradingbot.brokers.kis", "KISBroker"),
    "alpaca": ("tradingbot.brokers.alpaca", "AlpacaBroker"),
}


def available_brokers() -> list[str]:
    return sorted(_REGISTRY)


def get_broker_class(name: str) -> type[BaseBroker]:
    key = name.lower()
    if key not in _REGISTRY:
        raise ConfigError(f"알 수 없는 브로커: {name!r}. 가능: {', '.join(available_brokers())}")
    module_name, class_name = _REGISTRY[key]
    module = importlib.import_module(module_name)
    return getattr(module, class_name)


def create_broker(name: str, config: AppConfig) -> BaseBroker:
    """설정으로부터 브로커 인스턴스를 만든다. 각 어댑터는 `from_config(config)` 클래스메서드를 구현한다."""
    cls = get_broker_class(name)
    factory = getattr(cls, "from_config", None)
    if factory is None:
        raise ConfigError(f"{cls.__name__} 에 from_config 가 없습니다")
    return factory(config)


__all__ = ["BaseBroker", "available_brokers", "create_broker", "get_broker_class"]
