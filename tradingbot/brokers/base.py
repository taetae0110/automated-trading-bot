"""브로커(거래소/증권사) 공통 인터페이스.

모든 어댑터는 BaseBroker 를 상속하고 아래 추상 메서드를 구현한다.
엔진/백테스터/리스크 모듈은 이 인터페이스에만 의존한다.

규약
- 심볼은 브로커 네이티브 표기 (models.py 참고).
- get_candles 는 **오래된 → 최신** 순으로 정렬된 list[Candle] 을 반환하고,
  아직 진행 중인(미완성) 캔들은 include_partial=False 이면 제외한다.
- 금액(quote) 단위: Upbit "KRW", ccxt "BTC/USDT" 의 "USDT", KIS "KRW", Alpaca "USD".
- 모든 네트워크 오류는 BrokerError(또는 하위 클래스) 로 변환한다.
"""

from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal, localcontext

from tradingbot.models import (
    AssetClass,
    Balance,
    Candle,
    Order,
    OrderSide,
    OrderType,
    Position,
)

logger = logging.getLogger(__name__)


class BaseBroker(ABC):
    #: 어댑터 식별자 (설정 파일의 broker.name 과 일치)
    name: str = "base"
    asset_class: AssetClass = AssetClass.CRYPTO
    #: 지원하는 캔들 간격. 비어 있으면 모두 지원한다고 가정.
    supported_intervals: tuple[str, ...] = ()

    # ------------------------------------------------------------------ 시세
    @abstractmethod
    def get_candles(
        self,
        symbol: str,
        interval: str,
        limit: int = 200,
        end: datetime | None = None,
        include_partial: bool = False,
    ) -> list[Candle]:
        """과거 캔들 조회. end(UTC) 이전의 캔들 limit 개를 오래된→최신 순으로 반환.

        end 는 **배타적**이다: 반환되는 캔들은 모두 `candle.timestamp < end` 를 만족한다
        (Upbit `to` 와 동일). 따라서 `end=candles[0].timestamp` 로 호출하면 겹침 없이 더 과거 페이지를 받는다.
        """

    @abstractmethod
    def get_ticker(self, symbol: str) -> float:
        """현재가(최근 체결가)."""

    # ------------------------------------------------------------------ 계좌
    @abstractmethod
    def get_balances(self) -> dict[str, Balance]:
        """통화별 잔고. key = 통화 코드 (예: "KRW", "BTC", "USDT", "USD")."""

    @abstractmethod
    def get_positions(self) -> dict[str, Position]:
        """보유 포지션. key = 심볼. 수량 0 은 포함하지 않는다."""

    # ------------------------------------------------------------------ 주문
    @abstractmethod
    def place_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        order_type: OrderType = OrderType.MARKET,
        price: float | None = None,
    ) -> Order:
        """주문 접수. quantity 는 기초자산 수량(코인 개수/주식 주수).

        시장가 매수에서 금액 기준 주문만 지원하는 거래소(Upbit)는 quantity * 현재가 로 환산한다.
        반환된 Order 의 status 가 terminal 이 아니면 호출자는 get_order 로 상태를 추적한다.
        """

    @abstractmethod
    def cancel_order(self, order_id: str, symbol: str | None = None) -> bool: ...

    @abstractmethod
    def get_order(self, order_id: str, symbol: str | None = None) -> Order: ...

    @abstractmethod
    def get_open_orders(self, symbol: str | None = None) -> list[Order]: ...

    # ------------------------------------------------------------------ 메타
    @abstractmethod
    def quote_currency(self, symbol: str) -> str:
        """해당 심볼의 결제 통화 (예: KRW-BTC → KRW, BTC/USDT → USDT, 005930 → KRW, AAPL → USD)."""

    def base_currency(self, symbol: str) -> str:
        """기초자산 코드 (KRW-BTC → BTC, BTC/USDT → BTC, 주식은 심볼 그대로)."""
        return symbol

    def min_order_value(self, symbol: str) -> float:
        """최소 주문 금액(quote 통화). 기본 0."""
        return 0.0

    def round_quantity(self, symbol: str, quantity: float) -> float:
        """거래소 수량 정밀도로 내림. 기본은 소수 8자리 내림.

        `math.floor(q * 1e8) / 1e8` 은 0.29 → 0.28999999 같은 부동소수 오차를 내므로 Decimal 로 계산한다.
        NaN/inf 는 그대로 돌려준다 (검증은 호출자 몫).
        """
        if not math.isfinite(quantity):
            return quantity
        with localcontext() as ctx:
            ctx.prec = 60
            return float(Decimal(str(quantity)).quantize(Decimal("1e-8"), rounding=ROUND_FLOOR))

    def round_price(self, symbol: str, price: float) -> float:
        """거래소 호가 단위로 반올림. 기본은 그대로."""
        return price

    def is_market_open(self) -> bool:
        """장 운영 여부. 암호화폐는 항상 True."""
        return True

    def get_equity(self, symbols: list[str] | None = None) -> float:
        """총 자산 평가액(quote 통화 기준). 기본 구현: 현금 잔고 + 포지션 평가.

        quote 통화가 여러 개인 혼합 포트폴리오는 어댑터가 재정의한다.
        """
        positions = self.get_positions()
        balances = self.get_balances()
        quote = None
        for sym in symbols or list(positions):
            quote = self.quote_currency(sym)
            break
        if quote is None and balances:
            quote = next(iter(balances))
        cash = balances[quote].total if quote and quote in balances else 0.0
        value = 0.0
        for sym, pos in positions.items():
            try:
                value += pos.market_value(self.get_ticker(sym))
            except Exception as e:  # noqa: BLE001
                logger.warning("평가액 계산 중 시세 조회 실패 %s: %s", sym, e)
                value += pos.cost
        return cash + value

    def close(self) -> None:
        """세션/리소스 정리. 필요 시 재정의."""

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name} asset={self.asset_class.value}>"
