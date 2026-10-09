"""RiskManager — ARCHITECTURE.md 4절 계약 구현.

역할
- 신규 진입 가능 여부 (최대 보유 종목 수, 일일 손실 한도)
- 포지션 크기 산정 (총자산/운용한도 × 비중 × 신호 강도, 현금·최소 주문 금액 제한)
- 진입 시 손절/익절 가격과 메타 정보 기록
- 매 폴링마다 손절 → 추적손절 → 익절 순으로 청산 판정
- 당일 실현 손익 누적 (UTC 날짜 기준, 날짜가 바뀌면 리셋)

이 모듈은 네트워크/브로커에 의존하지 않는 순수 로직이다. 모든 datetime 은 UTC aware 로 다룬다.
"""

from __future__ import annotations

import logging
import math
import numbers
from collections.abc import Mapping
from datetime import date, datetime
from enum import Enum
from typing import Any

from tradingbot.config import RiskConfig
from tradingbot.models import (
    OrderType,
    Position,
    Signal,
    SignalAction,
    Trade,
    ensure_utc,
)

logger = logging.getLogger(__name__)

#: to_dict() 포맷 버전
STATE_VERSION = 1

#: 체결 금액을 현금으로 나눌 때 잔고 초과를 막기 위한 안전 계수 (계약: cash*(1-fee_pct)*0.999)
CASH_SAFETY_FACTOR = 0.999

#: check_exit 가 meta["exit_type"] 에 넣는 값
EXIT_STOP_LOSS = "stop_loss"
EXIT_TRAILING_STOP = "trailing_stop"
EXIT_TAKE_PROFIT = "take_profit"

#: apply_entry 가 Signal.meta 에서 진입 캔들 시각을 찾을 때 순서대로 조회하는 키
_ENTRY_TS_KEYS = ("entry_bar_ts", "bar_ts", "timestamp", "candle_ts")


def _is_finite_positive(x: Any) -> bool:
    """bool 을 제외한 실수(numpy 스칼라 포함)이고 유한·양수인가."""
    return isinstance(x, numbers.Real) and not isinstance(x, bool) and math.isfinite(x) and x > 0


def _fmt(x: float) -> str:
    """로그/사유 문자열용 숫자 포맷 (큰 값은 천 단위 구분, 작은 값은 유효숫자)."""
    ax = abs(x)
    if ax >= 1000:
        return f"{x:,.0f}"
    if ax >= 1:
        return f"{x:,.2f}"
    return f"{x:.6g}"


def _jsonable(value: Any) -> Any:
    """meta 저장용: datetime → ISO, Enum → value, 컨테이너는 재귀, 그 외 str."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return ensure_utc(value).isoformat()
    if isinstance(value, Mapping):
        return {(str(k.value) if isinstance(k, Enum) else str(k)): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    item = getattr(value, "item", None)  # numpy 스칼라
    if callable(item):
        try:
            return _jsonable(item())
        except (TypeError, ValueError):
            pass
    return str(value)


def _to_iso(value: Any) -> str | None:
    """datetime / ISO 문자열 / pandas Timestamp 를 UTC ISO 문자열로. 해석 불가면 None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_utc(value).isoformat()
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return ensure_utc(datetime.fromisoformat(s.replace("Z", "+00:00"))).isoformat()
        except ValueError:
            return None
    to_py = getattr(value, "to_pydatetime", None)  # pandas.Timestamp
    if callable(to_py):
        try:
            return _to_iso(to_py())
        except (TypeError, ValueError):
            return None
    return None


class RiskManager:
    """리스크 한도 적용기. 하나의 인스턴스가 계좌 전체(모든 심볼)를 담당한다."""

    def __init__(self, config: RiskConfig) -> None:
        self.config = config
        self._day: date | None = None  # 현재 추적 중인 UTC 날짜
        self._daily_pnl: float = 0.0  # 당일 실현 손익 (수수료 차감)
        self._day_start_equity: float | None = None  # 당일 시작 자산 (모르면 None)
        self._daily_trades: int = 0
        self._warned_unknown_equity = False

    # ------------------------------------------------------------------ 일자 추적
    @staticmethod
    def _utc_date(now: datetime) -> date:
        if not isinstance(now, datetime):
            raise TypeError(f"now 는 datetime 이어야 합니다: {type(now).__name__}")
        return ensure_utc(now).date()

    def _roll_day(self, now: datetime) -> bool:
        """now 의 UTC 날짜가 추적 중인 날짜와 다르면 당일 집계를 리셋한다. 리셋했으면 True."""
        today = self._utc_date(now)
        if self._day == today:
            return False
        if self._day is not None:
            logger.info(
                "UTC 날짜 변경 %s → %s: 당일 손익 %s (거래 %d건) 리셋",
                self._day.isoformat(),
                today.isoformat(),
                _fmt(self._daily_pnl),
                self._daily_trades,
            )
        self._day = today
        self._daily_pnl = 0.0
        self._daily_trades = 0
        self._day_start_equity = None
        self._warned_unknown_equity = False
        return True

    def start_day(self, equity: float, now: datetime) -> None:
        """당일 시작 자산을 기록한다. 같은 날 이미 기록돼 있으면 (재시작 등) 첫 값을 유지한다."""
        self._roll_day(now)
        if not _is_finite_positive(equity):
            logger.warning("당일 시작 자산이 유효하지 않아 기록하지 않습니다: %r", equity)
            return
        if self._day_start_equity is not None:
            logger.debug(
                "당일(%s) 시작 자산은 이미 %s 로 기록됨 — %s 무시",
                self._day,
                _fmt(self._day_start_equity),
                _fmt(float(equity)),
            )
            return
        self._day_start_equity = float(equity)
        logger.info("당일(%s) 시작 자산 기록: %s", self._day, _fmt(self._day_start_equity))

    def record_trade(self, trade: Trade) -> None:
        """청산 완료 Trade 의 손익을 당일 실현 손익에 누적한다 (날짜 기준: trade.exit_time)."""
        self._roll_day(trade.exit_time)
        pnl = float(trade.pnl)
        if not math.isfinite(pnl):
            logger.warning("%s 거래 손익이 유효하지 않아 누적하지 않습니다: %r", trade.symbol, pnl)
            return
        self._daily_pnl += pnl
        self._daily_trades += 1
        logger.info(
            "%s 청산 손익 %s (%.2f%%) → 당일 누적 %s (%d건)",
            trade.symbol,
            _fmt(pnl),
            trade.pnl_pct * 100,
            _fmt(self._daily_pnl),
            self._daily_trades,
        )

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def daily_trades(self) -> int:
        return self._daily_trades

    @property
    def day_start_equity(self) -> float | None:
        return self._day_start_equity

    @property
    def current_day(self) -> date | None:
        """추적 중인 UTC 날짜 (아직 아무 호출도 없었으면 None)."""
        return self._day

    def daily_loss_pct(self) -> float | None:
        """당일 손익 / 당일 시작 자산. 시작 자산을 모르면 None."""
        if self._day_start_equity is None or self._day_start_equity <= 0:
            return None
        return self._daily_pnl / self._day_start_equity

    def daily_loss_limit_hit(self) -> bool:
        """일일 손실 한도 도달 여부 (한도 미설정 또는 시작 자산 미상이면 False)."""
        limit = self.config.max_daily_loss_pct
        if limit is None:
            return False
        ratio = self.daily_loss_pct()
        if ratio is None:
            if not self._warned_unknown_equity:
                logger.warning(
                    "당일 시작 자산을 알 수 없어 일일 손실 한도(%.2f%%)를 적용하지 못합니다 (start_day 미호출)",
                    limit * 100,
                )
                self._warned_unknown_equity = True
            return False
        return ratio <= -limit

    # ------------------------------------------------------------------ 진입 가능 여부
    def can_open(self, open_positions: int, now: datetime) -> tuple[bool, str]:
        """신규 포지션 진입 가능 여부. (허용 여부, 거부 사유)."""
        self._roll_day(now)
        if open_positions >= self.config.max_positions:
            return False, "최대 보유 종목 수 초과"
        if self.daily_loss_limit_hit():
            ratio = self.daily_loss_pct() or 0.0
            limit = self.config.max_daily_loss_pct or 0.0
            reason = (
                f"일일 손실 한도 도달 (당일 손익 {_fmt(self._daily_pnl)} / 시작 자산 "
                f"{_fmt(self._day_start_equity or 0.0)} = {ratio * 100:.2f}%, 한도 -{limit * 100:.2f}%)"
            )
            return False, reason
        return True, ""

    # ------------------------------------------------------------------ 포지션 크기
    def position_size(
        self,
        *,
        equity: float,
        cash: float,
        price: float,
        signal: Signal,
        open_positions: int,
        min_order_value: float = 0.0,
        fee_pct: float = 0.0,
    ) -> float:
        """매수 수량 산정.

        budget = min(equity, capital_limit) * max_position_pct * strength
        budget = min(budget, cash * (1 - fee_pct) * 0.999)
        budget < max(min_order_value, config.min_order_value) → 0.0, 아니면 qty = budget / price
        """
        if not _is_finite_positive(price):
            logger.warning("%s 가격이 유효하지 않아 수량 0: %r", signal.symbol, price)
            return 0.0
        strength = float(signal.strength)
        if not math.isfinite(strength):
            strength = 0.0
        strength = min(max(strength, 0.0), 1.0)
        if strength <= 0.0:
            logger.debug("%s 신호 강도 %r → 수량 0", signal.symbol, signal.strength)
            return 0.0

        equity_f = float(equity) if _is_finite_positive(equity) else 0.0
        cash_f = float(cash) if _is_finite_positive(cash) else 0.0
        base = equity_f
        if self.config.capital_limit is not None:
            base = min(base, self.config.capital_limit)

        budget = base * self.config.max_position_pct * strength
        fee = float(fee_pct) if math.isfinite(fee_pct) and fee_pct > 0 else 0.0
        cash_cap = cash_f * (1.0 - fee) * CASH_SAFETY_FACTOR
        budget = min(budget, cash_cap)

        threshold = max(float(min_order_value or 0.0), float(self.config.min_order_value))
        if budget <= 0.0 or budget < threshold:
            logger.info(
                "%s 주문 예산 %s 가 최소 주문 금액 %s 미만 (equity %s, cash %s, 보유 %d) → 수량 0",
                signal.symbol,
                _fmt(budget),
                _fmt(threshold),
                _fmt(equity_f),
                _fmt(cash_f),
                open_positions,
            )
            return 0.0

        qty = budget / price
        logger.debug(
            "%s 사이징: base %s × %.4f × strength %.3f → 예산 %s (현금 상한 %s), 가격 %s → 수량 %.8g (보유 %d)",
            signal.symbol,
            _fmt(base),
            self.config.max_position_pct,
            strength,
            _fmt(budget),
            _fmt(cash_cap),
            _fmt(price),
            qty,
            open_positions,
        )
        return qty

    # ------------------------------------------------------------------ 진입/청산
    def apply_entry(self, position: Position, signal: Signal, fill_price: float) -> None:
        """체결 직후 포지션에 손절/익절/최고가/메타를 기록한다."""
        if not _is_finite_positive(fill_price):
            raise ValueError(f"{position.symbol} 체결가가 유효하지 않습니다: {fill_price!r}")
        fill = float(fill_price)

        if signal.stop_loss is not None:
            position.stop_loss = float(signal.stop_loss)
        elif self.config.stop_loss_pct is not None:
            position.stop_loss = fill * (1.0 - self.config.stop_loss_pct)
        else:
            position.stop_loss = None

        if signal.take_profit is not None:
            position.take_profit = float(signal.take_profit)
        elif self.config.take_profit_pct is not None:
            position.take_profit = fill * (1.0 + self.config.take_profit_pct)
        else:
            position.take_profit = None

        position.highest_price = fill

        entry_ts: str | None = None
        for key in _ENTRY_TS_KEYS:
            if key in signal.meta:
                entry_ts = _to_iso(signal.meta.get(key))
                if entry_ts is not None:
                    break

        position.meta["max_holding_bars"] = signal.max_holding_bars
        position.meta["entry_reason"] = signal.reason
        position.meta["entry_bar_ts"] = entry_ts
        position.meta["entry_signal_meta"] = _jsonable(dict(signal.meta))
        position.meta["entry_price"] = fill
        logger.info(
            "%s 진입 %s: 손절 %s, 익절 %s, 최대 보유 %s봉, 사유 %s",
            position.symbol,
            _fmt(fill),
            "없음" if position.stop_loss is None else _fmt(position.stop_loss),
            "없음" if position.take_profit is None else _fmt(position.take_profit),
            "∞" if signal.max_holding_bars is None else signal.max_holding_bars,
            signal.reason or "-",
        )

    def check_exit(self, position: Position, price: float) -> Signal | None:
        """현재가로 청산 판정. 최고가를 갱신한 뒤 손절 → 추적손절 → 익절 순으로 검사한다."""
        if not _is_finite_positive(price):
            logger.warning("%s 현재가가 유효하지 않아 청산 판정을 건너뜁니다: %r", position.symbol, price)
            return None
        px = float(price)

        if position.highest_price is None or px > position.highest_price:
            position.highest_price = px
        highest = position.highest_price

        if position.stop_loss is not None and px <= position.stop_loss:
            return self._exit_signal(
                position,
                px,
                EXIT_STOP_LOSS,
                f"손절: 현재가 {_fmt(px)} <= 손절가 {_fmt(position.stop_loss)}",
                level=position.stop_loss,
            )

        trailing = self.config.trailing_stop_pct
        if trailing is not None and highest > position.average_price:
            trail_level = highest * (1.0 - trailing)
            if px <= trail_level:
                return self._exit_signal(
                    position,
                    px,
                    EXIT_TRAILING_STOP,
                    f"추적 손절: 현재가 {_fmt(px)} <= 고점 {_fmt(highest)} × (1 - {trailing:g}) = {_fmt(trail_level)}",
                    level=trail_level,
                )

        if position.take_profit is not None and px >= position.take_profit:
            return self._exit_signal(
                position,
                px,
                EXIT_TAKE_PROFIT,
                f"익절: 현재가 {_fmt(px)} >= 익절가 {_fmt(position.take_profit)}",
                level=position.take_profit,
            )
        return None

    @staticmethod
    def _exit_signal(
        position: Position, price: float, exit_type: str, reason: str, *, level: float
    ) -> Signal:
        logger.info("%s 청산 신호 [%s] %s", position.symbol, exit_type, reason)
        return Signal(
            action=SignalAction.SELL,
            symbol=position.symbol,
            strength=1.0,
            reason=reason,
            order_type=OrderType.MARKET,
            meta={
                "exit_type": exit_type,
                "price": price,
                "level": level,
                "highest_price": position.highest_price,
                "average_price": position.average_price,
                "unrealized_pnl_pct": position.unrealized_pnl_pct(price),
            },
        )

    # ------------------------------------------------------------------ 상태 저장/복원
    def to_dict(self) -> dict[str, Any]:
        """당일 집계 상태 (JSON 직렬화 가능)."""
        return {
            "version": STATE_VERSION,
            "day": self._day.isoformat() if self._day is not None else None,
            "daily_pnl": self._daily_pnl,
            "day_start_equity": self._day_start_equity,
            "daily_trades": self._daily_trades,
        }

    def from_dict(self, d: Mapping[str, Any] | None) -> RiskManager:
        """``to_dict()`` 결과를 현재 인스턴스에 복원한다 (in-place, self 반환).

        손상된 값은 경고 후 무시한다 — 리스크 상태 때문에 봇이 기동하지 못하는 일은 없어야 한다.
        """
        if not d:
            return self
        if not isinstance(d, Mapping):
            logger.warning("리스크 상태가 dict 가 아니어서 무시합니다: %s", type(d).__name__)
            return self
        version = d.get("version", STATE_VERSION)
        if version != STATE_VERSION:
            logger.warning("지원하지 않는 리스크 상태 버전 %r (지원: %d) → 무시", version, STATE_VERSION)
            return self

        day_raw = d.get("day")
        day: date | None = None
        if day_raw is not None:
            try:
                day = date.fromisoformat(str(day_raw))
            except ValueError:
                logger.warning("리스크 상태의 day 값이 잘못되어 무시합니다: %r", day_raw)
                return self

        try:
            pnl = float(d.get("daily_pnl", 0.0) or 0.0)
            if not math.isfinite(pnl):
                raise ValueError(pnl)
            start_raw = d.get("day_start_equity")
            start = float(start_raw) if start_raw is not None else None
            if start is not None and not _is_finite_positive(start):
                start = None
            trades = int(d.get("daily_trades", 0) or 0)
        except (TypeError, ValueError) as e:
            logger.warning("리스크 상태 복원 실패, 무시합니다: %s", e)
            return self

        self._day = day
        self._daily_pnl = pnl
        self._day_start_equity = start
        self._daily_trades = max(trades, 0)
        self._warned_unknown_equity = False
        logger.debug(
            "리스크 상태 복원: day=%s pnl=%s start_equity=%s trades=%d",
            day,
            _fmt(pnl),
            "None" if start is None else _fmt(start),
            self._daily_trades,
        )
        return self

    def __repr__(self) -> str:
        return (
            f"<RiskManager day={self._day} daily_pnl={self._daily_pnl:.2f} "
            f"day_start_equity={self._day_start_equity} max_positions={self.config.max_positions}>"
        )


__all__ = [
    "EXIT_STOP_LOSS",
    "EXIT_TAKE_PROFIT",
    "EXIT_TRAILING_STOP",
    "STATE_VERSION",
    "RiskManager",
]
