"""핵심 도메인 모델. 모든 모듈이 공유하는 계약(contract)이므로 함부로 바꾸지 말 것.

규약
- 모든 datetime 은 timezone-aware UTC 이다.
- 가격/수량은 float. 거래소별 정밀도 반올림은 브로커 어댑터가 담당한다.
- 심볼은 각 브로커의 "네이티브 표기"를 그대로 쓴다.
    Upbit  : "KRW-BTC"      ccxt/Binance : "BTC/USDT"
    KIS    : "005930"        Alpaca       : "AAPL"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class AssetClass(str, Enum):
    CRYPTO = "crypto"
    STOCK = "stock"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    # STOP: 지정한 가격을 상향 돌파(매수)/하향 돌파(매도)하면 시장가로 체결.
    # 거래소가 지원하지 않으면 엔진/백테스터가 폴링으로 흉내낸다 (변동성 돌파 전략용).
    STOP = "stop"


class OrderStatus(str, Enum):
    PENDING = "pending"  # 아직 거래소에 접수 전/접수 중
    OPEN = "open"  # 미체결 (대기)
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"

    @property
    def is_terminal(self) -> bool:
        return self in (
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        )


class SignalAction(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(dt: datetime) -> datetime:
    """naive datetime 은 UTC 로 간주하고, aware 는 UTC 로 변환한다."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class Candle:
    """OHLCV 캔들. timestamp 는 캔들 *시작* 시각(UTC)."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", ensure_utc(self.timestamp))

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


@dataclass
class Signal:
    """전략이 내는 매매 신호.

    - order_type MARKET : 다음 가능한 시점에 시장가 체결
    - order_type LIMIT  : price 에 지정가
    - order_type STOP   : price 가 있으면 그 가격 돌파 시, price 가 None 이고 stop_offset 이 있으면
                          "다음 캔들 시가 + stop_offset" 돌파 시 체결 (변동성 돌파)
    - stop_loss / take_profit : 절대 가격. None 이면 RiskManager 의 % 설정을 사용.
    - strength : 0.0 ~ 1.0, 포지션 크기 가중치 (1.0 = 리스크 설정의 최대 비중)
    - max_holding_bars : 진입 후 N 캔들이 지나면 (N번째 다음 캔들 시가에) 강제 청산.
                         변동성 돌파 전략의 "다음날 시가 매도" 는 1 이다. None 이면 제한 없음.
    """

    action: SignalAction
    symbol: str
    strength: float = 1.0
    reason: str = ""
    order_type: OrderType = OrderType.MARKET
    price: float | None = None
    stop_offset: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    max_holding_bars: int | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def hold(cls, symbol: str, reason: str = "") -> Signal:
        return cls(action=SignalAction.HOLD, symbol=symbol, strength=0.0, reason=reason)

    @property
    def is_hold(self) -> bool:
        return self.action == SignalAction.HOLD


@dataclass
class Order:
    id: str
    symbol: str
    side: OrderSide
    type: OrderType
    quantity: float
    price: float | None = None  # 지정가/스탑 가격. 시장가는 None
    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: float = 0.0
    average_price: float | None = None
    fee: float = 0.0  # quote 통화 기준 수수료
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_filled(self) -> bool:
        return self.status == OrderStatus.FILLED

    @property
    def remaining_quantity(self) -> float:
        return max(self.quantity - self.filled_quantity, 0.0)

    @property
    def filled_value(self) -> float:
        if self.average_price is None:
            return 0.0
        return self.average_price * self.filled_quantity


@dataclass
class Position:
    symbol: str
    quantity: float
    average_price: float
    opened_at: datetime | None = None
    # 추적 손절(trailing stop) 계산용 최고가. RiskManager 가 갱신한다.
    highest_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def cost(self) -> float:
        return self.quantity * self.average_price

    def market_value(self, price: float) -> float:
        return self.quantity * price

    def unrealized_pnl(self, price: float) -> float:
        return (price - self.average_price) * self.quantity

    def unrealized_pnl_pct(self, price: float) -> float:
        if self.average_price == 0:
            return 0.0
        return (price - self.average_price) / self.average_price


@dataclass
class Balance:
    currency: str
    total: float
    available: float

    @property
    def locked(self) -> float:
        return max(self.total - self.available, 0.0)


@dataclass
class Trade:
    """청산이 완료된 한 번의 왕복 매매(진입→청산)."""

    symbol: str
    side: OrderSide  # 진입 방향 (현재는 BUY=롱 만 지원)
    quantity: float
    entry_price: float
    exit_price: float
    entry_time: datetime
    exit_time: datetime
    fee: float = 0.0
    reason: str = ""

    @property
    def pnl(self) -> float:
        gross = (self.exit_price - self.entry_price) * self.quantity
        if self.side == OrderSide.SELL:
            gross = -gross
        return gross - self.fee

    @property
    def pnl_pct(self) -> float:
        cost = self.entry_price * self.quantity
        return self.pnl / cost if cost else 0.0

    @property
    def holding_seconds(self) -> float:
        return (self.exit_time - self.entry_time).total_seconds()


# 지원하는 캔들 간격 (문자열 → 초)
INTERVAL_SECONDS: dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "10m": 600,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
    "1w": 604800,
}


def interval_to_seconds(interval: str) -> int:
    try:
        return INTERVAL_SECONDS[interval]
    except KeyError as e:
        raise ValueError(
            f"지원하지 않는 캔들 간격: {interval!r} (가능: {', '.join(INTERVAL_SECONDS)})"
        ) from e
