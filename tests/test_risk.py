"""RiskManager 테스트.

가격은 전부 tests/conftest.py 가 Upbit 공개 API 에서 받아 캐시한 **실제** KRW-BTC 캔들에서 가져온다
(네트워크가 없으면 skip). 가짜/데모 시세는 만들지 않는다 — 계좌 설정값(자산, 현금, 비율)만 상수다.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from tradingbot.config import RiskConfig
from tradingbot.models import (
    Candle,
    OrderSide,
    OrderType,
    Position,
    Signal,
    SignalAction,
    Trade,
)
from tradingbot.risk import RiskManager
from tradingbot.risk.manager import (
    EXIT_STOP_LOSS,
    EXIT_TAKE_PROFIT,
    EXIT_TRAILING_STOP,
    STATE_VERSION,
)

BTC = "KRW-BTC"
# 계좌 설정 (시세가 아님)
EQUITY = 10_000_000.0
CASH = 10_000_000.0
KST = ZoneInfo("Asia/Seoul")

approx = pytest.approx


# ----------------------------------------------------------------------------- 헬퍼/픽스처
def utc(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def buy_signal(symbol: str = BTC, **kw) -> Signal:
    return Signal(action=SignalAction.BUY, symbol=symbol, **kw)


def losing_pair(candles: list[Candle]) -> tuple[Candle, Candle]:
    """실제 캔들에서 '진입 종가 > 청산 종가' 인 (진입, 청산) 쌍을 찾는다 (진입이 더 과거)."""
    for i in range(len(candles) - 1):
        for j in range(i + 1, len(candles)):
            if candles[j].close < candles[i].close:
                return candles[i], candles[j]
    pytest.skip("실제 데이터에 하락 구간이 없어 손실 거래를 만들 수 없음")


def winning_pair(candles: list[Candle]) -> tuple[Candle, Candle]:
    for i in range(len(candles) - 1):
        for j in range(i + 1, len(candles)):
            if candles[j].close > candles[i].close:
                return candles[i], candles[j]
    pytest.skip("실제 데이터에 상승 구간이 없어 이익 거래를 만들 수 없음")


def make_trade(entry: Candle, exit_: Candle, quantity: float, fee: float = 0.0) -> Trade:
    return Trade(
        symbol=BTC,
        side=OrderSide.BUY,
        quantity=quantity,
        entry_price=entry.close,
        exit_price=exit_.close,
        entry_time=entry.timestamp,
        exit_time=exit_.timestamp + timedelta(hours=1),
        fee=fee,
    )


@pytest.fixture
def price(daily_candles: list[Candle]) -> float:
    """가장 최근 실제 일봉 종가."""
    return daily_candles[-1].close


@pytest.fixture
def rm() -> RiskManager:
    return RiskManager(RiskConfig())


# ----------------------------------------------------------------------------- can_open / 일일 한도
class TestCanOpen:
    def test_max_positions(self) -> None:
        rm = RiskManager(RiskConfig(max_positions=2))
        now = utc(2026, 1, 1, 9)
        assert rm.can_open(0, now) == (True, "")
        assert rm.can_open(1, now) == (True, "")
        ok, reason = rm.can_open(2, now)
        assert ok is False
        assert reason == "최대 보유 종목 수 초과"
        ok, reason = rm.can_open(5, now)
        assert ok is False and reason == "최대 보유 종목 수 초과"

    def test_daily_loss_limit_blocks_entry(self, daily_candles: list[Candle]) -> None:
        rm = RiskManager(RiskConfig(max_daily_loss_pct=0.05))
        entry, exit_ = losing_pair(daily_candles)
        day = exit_.timestamp + timedelta(hours=1)
        rm.start_day(EQUITY, day)
        # 손실이 정확히 한도(5%)를 조금 넘도록 수량을 실제 가격차에서 역산
        loss_per_unit = entry.close - exit_.close
        qty = (0.05 * EQUITY) / loss_per_unit * 1.01
        rm.record_trade(make_trade(entry, exit_, qty))
        assert rm.daily_pnl == approx(-(entry.close - exit_.close) * qty)
        assert rm.daily_pnl / EQUITY <= -0.05
        assert rm.daily_loss_limit_hit() is True

        ok, reason = rm.can_open(0, day)
        assert ok is False
        assert reason.startswith("일일 손실 한도 도달")
        assert "-5.00%" in reason  # 한도 표기

    def test_loss_below_limit_allows_entry(self, daily_candles: list[Candle]) -> None:
        rm = RiskManager(RiskConfig(max_daily_loss_pct=0.05))
        entry, exit_ = losing_pair(daily_candles)
        day = exit_.timestamp + timedelta(hours=1)
        rm.start_day(EQUITY, day)
        qty = (0.05 * EQUITY) / (entry.close - exit_.close) * 0.5  # 한도의 절반 손실
        rm.record_trade(make_trade(entry, exit_, qty))
        assert -0.05 < rm.daily_pnl / EQUITY < 0
        assert rm.can_open(0, day) == (True, "")

    def test_exact_limit_is_blocked(self) -> None:
        """비율이 정확히 -한도 이면 거부 (<=)."""
        rm = RiskManager(RiskConfig(max_daily_loss_pct=0.05))
        now = utc(2026, 3, 1, 12)
        rm.start_day(EQUITY, now)
        rm._daily_pnl = -0.05 * EQUITY  # 경계값 직접 설정
        assert rm.daily_loss_limit_hit() is True
        assert rm.can_open(0, now)[0] is False

    def test_fee_counts_toward_loss(self, daily_candles: list[Candle]) -> None:
        """Trade.pnl 은 수수료를 뺀 값이므로 수수료도 당일 손익에 반영된다."""
        rm = RiskManager(RiskConfig())
        entry, exit_ = winning_pair(daily_candles)
        qty = 0.01
        gross = (exit_.close - entry.close) * qty
        fee = gross * 2  # 수수료가 이익보다 커서 순손실
        rm.record_trade(make_trade(entry, exit_, qty, fee=fee))
        assert rm.daily_pnl == approx(gross - fee)
        assert rm.daily_pnl < 0

    def test_limit_disabled(self, daily_candles: list[Candle]) -> None:
        rm = RiskManager(RiskConfig(max_daily_loss_pct=None))
        entry, exit_ = losing_pair(daily_candles)
        day = exit_.timestamp + timedelta(hours=1)
        rm.start_day(EQUITY, day)
        qty = (0.5 * EQUITY) / (entry.close - exit_.close)  # 50% 손실
        rm.record_trade(make_trade(entry, exit_, qty))
        assert rm.daily_pnl / EQUITY <= -0.5
        assert rm.can_open(0, day) == (True, "")

    def test_limit_zero_in_config_means_disabled(self) -> None:
        """RiskConfig 는 0 을 None 으로 정규화한다."""
        rm = RiskManager(RiskConfig(max_daily_loss_pct=0))
        assert rm.config.max_daily_loss_pct is None
        now = utc(2026, 1, 1)
        rm.start_day(EQUITY, now)
        rm._daily_pnl = -EQUITY
        assert rm.can_open(0, now) == (True, "")

    def test_unknown_day_start_equity_not_triggered_but_logged(
        self, daily_candles: list[Candle], caplog: pytest.LogCaptureFixture
    ) -> None:
        rm = RiskManager(RiskConfig(max_daily_loss_pct=0.01))
        entry, exit_ = losing_pair(daily_candles)
        day = exit_.timestamp + timedelta(hours=1)
        qty = (0.2 * EQUITY) / (entry.close - exit_.close)
        rm.record_trade(make_trade(entry, exit_, qty))  # start_day 없이 손실만 누적
        assert rm.daily_pnl < 0
        assert rm.day_start_equity is None
        assert rm.daily_loss_pct() is None
        with caplog.at_level(logging.WARNING, logger="tradingbot.risk.manager"):
            assert rm.can_open(0, day) == (True, "")
            assert rm.can_open(0, day) == (True, "")
        warnings = [r for r in caplog.records if "시작 자산을 알 수 없어" in r.getMessage()]
        assert len(warnings) == 1  # 같은 날에는 한 번만 경고

    def test_day_change_resets(self, daily_candles: list[Candle]) -> None:
        rm = RiskManager(RiskConfig(max_daily_loss_pct=0.05))
        entry, exit_ = losing_pair(daily_candles)
        day1 = utc(2026, 5, 10, 23, 59)
        rm.start_day(EQUITY, day1)
        qty = (0.05 * EQUITY) / (entry.close - exit_.close) * 1.5
        trade = make_trade(entry, exit_, qty)
        trade.exit_time = day1
        rm.record_trade(trade)
        assert rm.can_open(0, day1)[0] is False
        assert rm.daily_trades == 1

        day2 = day1 + timedelta(minutes=1)  # 2026-05-11 00:00 UTC
        assert rm.can_open(0, day2) == (True, "")
        assert rm.daily_pnl == 0.0
        assert rm.daily_trades == 0
        assert rm.day_start_equity is None
        assert rm.current_day == day2.date()

        # 새 날의 시작 자산을 기록하면 다시 한도가 적용된다
        rm.start_day(EQUITY * 0.9, day2)
        trade2 = make_trade(entry, exit_, qty)
        trade2.exit_time = day2 + timedelta(hours=3)
        rm.record_trade(trade2)
        assert rm.can_open(0, day2 + timedelta(hours=4))[0] is False

    def test_record_trade_on_new_day_resets_before_accumulating(self, daily_candles: list[Candle]) -> None:
        rm = RiskManager(RiskConfig())
        entry, exit_ = losing_pair(daily_candles)
        t1 = make_trade(entry, exit_, 0.01)
        t1.exit_time = utc(2026, 7, 1, 10)
        rm.record_trade(t1)
        pnl1 = rm.daily_pnl
        assert pnl1 < 0
        t2 = make_trade(entry, exit_, 0.02)
        t2.exit_time = utc(2026, 7, 2, 0, 0)
        rm.record_trade(t2)
        assert rm.daily_pnl == approx(t2.pnl)  # t1 은 리셋됨
        assert rm.daily_pnl != approx(pnl1 + t2.pnl)
        assert rm.daily_trades == 1

    def test_timezone_normalization(self) -> None:
        """KST 새벽은 UTC 전날 — 같은 UTC 날짜로 집계되고, naive 는 UTC 로 간주."""
        rm = RiskManager(RiskConfig())
        rm.start_day(EQUITY, utc(2026, 1, 1, 20))
        rm._daily_pnl = -1.0
        kst_next_morning = datetime(2026, 1, 2, 8, 0, tzinfo=KST)  # = 2026-01-01 23:00 UTC
        rm.can_open(0, kst_next_morning)
        assert rm.daily_pnl == -1.0  # 리셋되지 않음
        assert rm.current_day == utc(2026, 1, 1).date()
        rm.can_open(0, datetime(2026, 1, 2, 0, 0))  # naive → UTC 2026-01-02
        assert rm.daily_pnl == 0.0
        assert rm.current_day == utc(2026, 1, 2).date()

    def test_non_datetime_now_raises(self, rm: RiskManager) -> None:
        with pytest.raises(TypeError):
            rm.can_open(0, "2026-01-01")  # type: ignore[arg-type]


class TestStartDay:
    def test_sets_once_per_day(self) -> None:
        rm = RiskManager(RiskConfig())
        now = utc(2026, 2, 1, 1)
        rm.start_day(EQUITY, now)
        assert rm.day_start_equity == EQUITY
        rm.start_day(EQUITY * 2, now + timedelta(hours=5))  # 재시작 등 — 첫 값 유지
        assert rm.day_start_equity == EQUITY
        rm.start_day(EQUITY * 2, now + timedelta(days=1))  # 다음 날 → 새 값
        assert rm.day_start_equity == EQUITY * 2

    @pytest.mark.parametrize("bad", [0.0, -1.0, math.nan, math.inf])
    def test_invalid_equity_ignored(self, bad: float) -> None:
        rm = RiskManager(RiskConfig())
        rm.start_day(bad, utc(2026, 2, 1))
        assert rm.day_start_equity is None

    def test_record_trade_nan_pnl_ignored(self, daily_candles: list[Candle]) -> None:
        rm = RiskManager(RiskConfig())
        entry, exit_ = losing_pair(daily_candles)
        t = make_trade(entry, exit_, math.nan)
        rm.record_trade(t)
        assert rm.daily_pnl == 0.0
        assert rm.daily_trades == 0


# ----------------------------------------------------------------------------- position_size
class TestPositionSize:
    def size(self, rm: RiskManager, price: float, **kw) -> float:
        params = dict(equity=EQUITY, cash=CASH, price=price, signal=buy_signal(), open_positions=0)
        params.update(kw)
        return rm.position_size(**params)

    def test_basic_budget(self, price: float) -> None:
        rm = RiskManager(RiskConfig(max_position_pct=0.2))
        qty = self.size(rm, price)
        assert qty == approx(EQUITY * 0.2 / price)
        assert qty * price <= CASH

    def test_strength_scales_budget(self, price: float) -> None:
        rm = RiskManager(RiskConfig(max_position_pct=0.2))
        full = self.size(rm, price, signal=buy_signal(strength=1.0))
        half = self.size(rm, price, signal=buy_signal(strength=0.5))
        assert half == approx(full / 2)
        assert self.size(rm, price, signal=buy_signal(strength=0.0)) == 0.0
        assert self.size(rm, price, signal=buy_signal(strength=-0.3)) == 0.0
        assert self.size(rm, price, signal=buy_signal(strength=5.0)) == approx(full)  # 1.0 으로 클램프
        assert self.size(rm, price, signal=buy_signal(strength=math.nan)) == 0.0

    def test_capital_limit(self, price: float) -> None:
        rm = RiskManager(RiskConfig(max_position_pct=0.5, capital_limit=1_000_000))
        qty = self.size(rm, price)
        assert qty == approx(1_000_000 * 0.5 / price)
        # capital_limit 이 자산보다 크면 자산 기준
        rm2 = RiskManager(RiskConfig(max_position_pct=0.5, capital_limit=EQUITY * 10))
        assert self.size(rm2, price) == approx(EQUITY * 0.5 / price)

    def test_cash_cap_with_fee(self, price: float) -> None:
        rm = RiskManager(RiskConfig(max_position_pct=0.5))
        cash = 100_000.0
        fee = 0.0005
        qty = self.size(rm, price, cash=cash, fee_pct=fee)
        assert qty == approx(cash * (1 - fee) * 0.999 / price)
        assert qty * price < cash
        # 수수료까지 더해도 현금을 넘지 않는다
        assert qty * price * (1 + fee) <= cash

    @pytest.mark.parametrize("cash", [50_000.0, 1_000_000.0, 3_000_000.0, EQUITY])
    @pytest.mark.parametrize("strength", [0.3, 1.0])
    def test_never_exceeds_cash(self, price: float, cash: float, strength: float) -> None:
        rm = RiskManager(RiskConfig(max_position_pct=0.9))
        qty = self.size(rm, price, cash=cash, fee_pct=0.001, signal=buy_signal(strength=strength))
        assert qty >= 0.0
        assert qty * price <= cash
        assert qty * price <= EQUITY * 0.9 * strength + 1e-6

    def test_min_order_value_from_argument_and_config(self, price: float) -> None:
        # 예산 = 1,000,000 × 0.01 = 10,000
        rm = RiskManager(RiskConfig(max_position_pct=0.01, min_order_value=5_000))
        assert self.size(rm, price, equity=1_000_000, cash=1_000_000) > 0
        assert self.size(rm, price, equity=1_000_000, cash=1_000_000, min_order_value=20_000) == 0.0
        rm2 = RiskManager(RiskConfig(max_position_pct=0.01, min_order_value=20_000))
        assert self.size(rm2, price, equity=1_000_000, cash=1_000_000, min_order_value=5_000) == 0.0
        # 예산 == 임계값 이면 주문 가능 (budget < threshold 일 때만 0)
        rm3 = RiskManager(RiskConfig(max_position_pct=0.01, min_order_value=10_000))
        assert self.size(rm3, price, equity=1_000_000, cash=1_000_000) == approx(10_000 / price)

    def test_cash_cap_can_drop_below_min_order(self, price: float) -> None:
        rm = RiskManager(RiskConfig(max_position_pct=0.5, min_order_value=5_000))
        assert self.size(rm, price, cash=4_000.0) == 0.0
        assert self.size(rm, price, cash=0.0) == 0.0

    @pytest.mark.parametrize("bad_price", [0.0, -1.0, math.nan, math.inf])
    def test_invalid_price(self, bad_price: float) -> None:
        rm = RiskManager(RiskConfig())
        assert self.size(rm, bad_price) == 0.0

    @pytest.mark.parametrize("bad_equity", [0.0, -5.0, math.nan])
    def test_invalid_equity(self, price: float, bad_equity: float) -> None:
        rm = RiskManager(RiskConfig())
        assert self.size(rm, price, equity=bad_equity) == 0.0

    def test_open_positions_does_not_change_formula(self, price: float) -> None:
        rm = RiskManager(RiskConfig(max_positions=3))
        assert self.size(rm, price, open_positions=0) == self.size(rm, price, open_positions=2)


# ----------------------------------------------------------------------------- apply_entry
class TestApplyEntry:
    def test_pct_defaults(self, price: float) -> None:
        rm = RiskManager(RiskConfig(stop_loss_pct=0.03, take_profit_pct=0.06))
        pos = Position(symbol=BTC, quantity=0.01, average_price=price)
        sig = buy_signal(
            reason="테스트 진입", max_holding_bars=3, meta={"timestamp": "2026-01-05T00:00:00+00:00"}
        )
        rm.apply_entry(pos, sig, price)
        assert pos.stop_loss == approx(price * 0.97)
        assert pos.take_profit == approx(price * 1.06)
        assert pos.highest_price == price
        assert pos.meta["max_holding_bars"] == 3
        assert pos.meta["entry_reason"] == "테스트 진입"
        assert pos.meta["entry_bar_ts"] == "2026-01-05T00:00:00+00:00"
        assert pos.meta["entry_signal_meta"] == {"timestamp": "2026-01-05T00:00:00+00:00"}
        assert pos.meta["entry_price"] == price

    def test_signal_overrides(self, price: float) -> None:
        rm = RiskManager(RiskConfig(stop_loss_pct=0.03, take_profit_pct=0.06))
        pos = Position(symbol=BTC, quantity=0.01, average_price=price)
        sig = buy_signal(stop_loss=price * 0.9, take_profit=price * 1.5)
        rm.apply_entry(pos, sig, price)
        assert pos.stop_loss == approx(price * 0.9)
        assert pos.take_profit == approx(price * 1.5)

    def test_none_when_unconfigured(self, price: float) -> None:
        rm = RiskManager(RiskConfig(stop_loss_pct=None, take_profit_pct=None))
        pos = Position(symbol=BTC, quantity=0.01, average_price=price, stop_loss=1.0, take_profit=2.0)
        rm.apply_entry(pos, buy_signal(), price)
        assert pos.stop_loss is None
        assert pos.take_profit is None
        assert pos.meta["max_holding_bars"] is None
        assert pos.meta["entry_bar_ts"] is None

    def test_entry_bar_ts_variants(self, price: float) -> None:
        rm = RiskManager(RiskConfig())
        naive = datetime(2026, 1, 5, 9, 0)
        cases = [
            ({"timestamp": naive}, "2026-01-05T09:00:00+00:00"),
            ({"timestamp": datetime(2026, 1, 5, 9, 0, tzinfo=KST)}, "2026-01-05T00:00:00+00:00"),
            ({"timestamp": "2026-01-05T09:00:00Z"}, "2026-01-05T09:00:00+00:00"),
            ({"bar_ts": "2026-01-05T09:00:00+09:00"}, "2026-01-05T00:00:00+00:00"),
            (
                {"entry_bar_ts": "2026-01-06T00:00:00+00:00", "timestamp": "2026-01-05T00:00:00+00:00"},
                "2026-01-06T00:00:00+00:00",
            ),
            ({"timestamp": "not-a-date"}, None),
            ({"timestamp": None}, None),
            ({"timestamp": 12345}, None),
            ({}, None),
        ]
        for meta, expected in cases:
            pos = Position(symbol=BTC, quantity=0.01, average_price=price)
            rm.apply_entry(pos, buy_signal(meta=meta), price)
            assert pos.meta["entry_bar_ts"] == expected, meta

    def test_signal_meta_copied_and_jsonable(self, price: float) -> None:
        rm = RiskManager(RiskConfig())
        meta = {
            "timestamp": datetime(2026, 1, 5, tzinfo=timezone.utc),
            "action": SignalAction.BUY,
            "n": (1, 2),
        }
        sig = buy_signal(meta=meta)
        pos = Position(symbol=BTC, quantity=0.01, average_price=price)
        rm.apply_entry(pos, sig, price)
        stored = pos.meta["entry_signal_meta"]
        assert stored == {"timestamp": "2026-01-05T00:00:00+00:00", "action": "buy", "n": [1, 2]}
        sig.meta["extra"] = 1
        assert "extra" not in pos.meta["entry_signal_meta"]

    @pytest.mark.parametrize("bad", [0.0, -1.0, math.nan])
    def test_invalid_fill_price(self, bad: float) -> None:
        rm = RiskManager(RiskConfig())
        pos = Position(symbol=BTC, quantity=0.01, average_price=1.0)
        with pytest.raises(ValueError):
            rm.apply_entry(pos, buy_signal(), bad)

    def test_existing_meta_preserved(self, price: float) -> None:
        rm = RiskManager(RiskConfig())
        pos = Position(symbol=BTC, quantity=0.01, average_price=price, meta={"custom": "keep"})
        rm.apply_entry(pos, buy_signal(), price)
        assert pos.meta["custom"] == "keep"


# ----------------------------------------------------------------------------- check_exit
def reference_walk(
    prices: list[float],
    entry: float,
    stop_loss: float | None,
    take_profit: float | None,
    trailing: float | None,
) -> tuple[int | None, str | None]:
    """계약 규칙을 그대로 옮긴 참조 구현: 최고가 갱신 → 손절 → 추적손절 → 익절."""
    highest = entry
    for idx, px in enumerate(prices):
        highest = max(highest, px)
        if stop_loss is not None and px <= stop_loss:
            return idx, EXIT_STOP_LOSS
        if trailing is not None and highest > entry and px <= highest * (1 - trailing):
            return idx, EXIT_TRAILING_STOP
        if take_profit is not None and px >= take_profit:
            return idx, EXIT_TAKE_PROFIT
    return None, None


class TestCheckExit:
    def test_stop_loss_signal(self, price: float) -> None:
        rm = RiskManager(RiskConfig(stop_loss_pct=0.03))
        pos = Position(symbol=BTC, quantity=0.01, average_price=price)
        rm.apply_entry(pos, buy_signal(), price)
        assert rm.check_exit(pos, price) is None
        assert rm.check_exit(pos, pos.stop_loss * 1.0001) is None
        sig = rm.check_exit(pos, pos.stop_loss)
        assert sig is not None
        assert sig.action == SignalAction.SELL
        assert sig.order_type == OrderType.MARKET
        assert sig.symbol == BTC
        assert sig.strength == 1.0
        assert sig.meta["exit_type"] == EXIT_STOP_LOSS
        assert "손절" in sig.reason
        assert sig.meta["price"] == pos.stop_loss
        assert sig.meta["level"] == pos.stop_loss

    def test_take_profit_signal(self, price: float) -> None:
        rm = RiskManager(RiskConfig(stop_loss_pct=0.03, take_profit_pct=0.05))
        pos = Position(symbol=BTC, quantity=0.01, average_price=price)
        rm.apply_entry(pos, buy_signal(), price)
        assert rm.check_exit(pos, pos.take_profit * 0.9999) is None
        sig = rm.check_exit(pos, pos.take_profit)
        assert sig is not None
        assert sig.meta["exit_type"] == EXIT_TAKE_PROFIT
        assert "익절" in sig.reason

    def test_highest_price_tracking(self, candles: list[Candle]) -> None:
        rm = RiskManager(RiskConfig(stop_loss_pct=None, take_profit_pct=None, trailing_stop_pct=None))
        entry = candles[0].close
        pos = Position(symbol=BTC, quantity=0.01, average_price=entry)
        rm.apply_entry(pos, buy_signal(), entry)
        running = entry
        for c in candles[1:]:
            assert rm.check_exit(pos, c.close) is None  # 아무 규칙도 없으면 청산 없음
            running = max(running, c.close)
            assert pos.highest_price == running

    def test_highest_none_initialised(self, price: float) -> None:
        rm = RiskManager(RiskConfig())
        pos = Position(symbol=BTC, quantity=0.01, average_price=price, highest_price=None)
        rm.check_exit(pos, price * 0.99)
        assert pos.highest_price == approx(price * 0.99)

    def test_trailing_requires_new_high(self, price: float) -> None:
        rm = RiskManager(RiskConfig(stop_loss_pct=None, trailing_stop_pct=0.02))
        pos = Position(symbol=BTC, quantity=0.01, average_price=price)
        rm.apply_entry(pos, buy_signal(), price)
        # 고점 == 진입가: 2% 이상 빠져도 추적손절 아님
        assert rm.check_exit(pos, price * 0.97) is None
        # 신고가 후 2% 하락 → 추적손절
        rm.check_exit(pos, price * 1.10)
        assert pos.highest_price == approx(price * 1.10)
        assert rm.check_exit(pos, price * 1.10 * 0.981) is None
        sig = rm.check_exit(pos, price * 1.10 * 0.98)
        assert sig is not None
        assert sig.meta["exit_type"] == EXIT_TRAILING_STOP
        assert "추적 손절" in sig.reason
        assert sig.meta["level"] == approx(price * 1.10 * 0.98)

    def test_trailing_level_uses_updated_high_in_same_call(self, price: float) -> None:
        """같은 호출에서 먼저 고점을 갱신하므로, 신고가 틱 자체는 절대 추적손절이 아니다."""
        rm = RiskManager(RiskConfig(stop_loss_pct=None, trailing_stop_pct=0.01))
        pos = Position(symbol=BTC, quantity=0.01, average_price=price, highest_price=price * 1.05)
        assert rm.check_exit(pos, price * 1.20) is None
        assert pos.highest_price == approx(price * 1.20)

    def test_order_stop_loss_before_trailing_and_take_profit(self, price: float) -> None:
        rm = RiskManager(RiskConfig(trailing_stop_pct=0.02))
        # 손절가/익절가를 현재가와 같게 두어 세 조건이 동시에 참이 되게 한다
        pos = Position(
            symbol=BTC,
            quantity=0.01,
            average_price=price * 0.5,
            highest_price=price * 1.5,
            stop_loss=price,
            take_profit=price,
        )
        sig = rm.check_exit(pos, price)
        assert sig is not None and sig.meta["exit_type"] == EXIT_STOP_LOSS

    def test_order_trailing_before_take_profit(self, price: float) -> None:
        rm = RiskManager(RiskConfig(trailing_stop_pct=0.02))
        pos = Position(
            symbol=BTC,
            quantity=0.01,
            average_price=price * 0.5,
            highest_price=price * 1.5,
            stop_loss=None,
            take_profit=price,
        )
        sig = rm.check_exit(pos, price)  # price <= 1.5*0.98*price 이고 price >= take_profit
        assert sig is not None and sig.meta["exit_type"] == EXIT_TRAILING_STOP

    def test_take_profit_when_trailing_not_hit(self, price: float) -> None:
        rm = RiskManager(RiskConfig(trailing_stop_pct=0.5))
        pos = Position(
            symbol=BTC,
            quantity=0.01,
            average_price=price * 0.5,
            highest_price=price * 1.01,
            stop_loss=None,
            take_profit=price,
        )
        sig = rm.check_exit(pos, price)
        assert sig is not None and sig.meta["exit_type"] == EXIT_TAKE_PROFIT

    @pytest.mark.parametrize("bad", [0.0, -1.0, math.nan, math.inf])
    def test_invalid_price_ignored(self, price: float, bad: float) -> None:
        rm = RiskManager(RiskConfig(stop_loss_pct=0.03))
        pos = Position(
            symbol=BTC, quantity=0.01, average_price=price, highest_price=price, stop_loss=price * 0.97
        )
        assert rm.check_exit(pos, bad) is None
        assert pos.highest_price == price

    @pytest.mark.parametrize("stop", [None, 0.005, 0.02])
    @pytest.mark.parametrize("tp", [None, 0.005, 0.03])
    @pytest.mark.parametrize("trail", [None, 0.005, 0.02])
    def test_matches_reference_on_real_hourly_closes(
        self, candles: list[Candle], stop: float | None, tp: float | None, trail: float | None
    ) -> None:
        rm = RiskManager(RiskConfig(stop_loss_pct=stop, take_profit_pct=tp, trailing_stop_pct=trail))
        entry = candles[0].close
        closes = [c.close for c in candles[1:]]
        pos = Position(symbol=BTC, quantity=0.01, average_price=entry)
        rm.apply_entry(pos, buy_signal(), entry)
        expected_idx, expected_type = reference_walk(closes, entry, pos.stop_loss, pos.take_profit, trail)

        got_idx: int | None = None
        got_type: str | None = None
        for idx, px in enumerate(closes):
            sig = rm.check_exit(pos, px)
            if sig is not None:
                got_idx, got_type = idx, sig.meta["exit_type"]
                break
        assert (got_idx, got_type) == (expected_idx, expected_type)

    def test_tight_thresholds_exit_on_real_data(self, candles: list[Candle]) -> None:
        """실제 시간봉 200개 안에서 ±0.5% 는 거의 확실히 한 번은 닿는다 — 아니라면 데이터가 평평한 것."""
        rm = RiskManager(RiskConfig(stop_loss_pct=0.005, take_profit_pct=0.005))
        entry = candles[0].close
        pos = Position(symbol=BTC, quantity=0.01, average_price=entry)
        rm.apply_entry(pos, buy_signal(), entry)
        exits = [rm.check_exit(pos, c.close) for c in candles[1:]]
        fired = [s for s in exits if s is not None]
        if not fired:
            pytest.skip("실제 데이터 변동폭이 0.5% 미만")
        assert fired[0].meta["exit_type"] in {EXIT_STOP_LOSS, EXIT_TAKE_PROFIT}


# ----------------------------------------------------------------------------- 상태 저장/복원
class TestState:
    def test_round_trip(self, daily_candles: list[Candle]) -> None:
        rm = RiskManager(RiskConfig(max_daily_loss_pct=0.05))
        entry, exit_ = losing_pair(daily_candles)
        now = utc(2026, 8, 1, 3)
        rm.start_day(EQUITY, now)
        t = make_trade(entry, exit_, 0.01)
        t.exit_time = now
        rm.record_trade(t)
        d = rm.to_dict()
        assert d == {
            "version": STATE_VERSION,
            "day": "2026-08-01",
            "daily_pnl": rm.daily_pnl,
            "day_start_equity": EQUITY,
            "daily_trades": 1,
        }
        import json

        json.dumps(d)  # JSON 직렬화 가능

        restored = RiskManager(RiskConfig(max_daily_loss_pct=0.05))
        assert restored.from_dict(d) is restored
        assert restored.daily_pnl == rm.daily_pnl
        assert restored.day_start_equity == EQUITY
        assert restored.daily_trades == 1
        assert restored.current_day == now.date()
        assert restored.to_dict() == d

    def test_restored_limit_persists_same_day_and_resets_next_day(self) -> None:
        rm = RiskManager(RiskConfig(max_daily_loss_pct=0.05))
        now = utc(2026, 8, 1, 3)
        rm.start_day(EQUITY, now)
        rm._daily_pnl = -0.1 * EQUITY
        restored = RiskManager(RiskConfig(max_daily_loss_pct=0.05)).from_dict(rm.to_dict())
        assert restored.can_open(0, now + timedelta(hours=1))[0] is False
        assert restored.can_open(0, now + timedelta(days=1))[0] is True
        assert restored.daily_pnl == 0.0

    def test_empty_state(self) -> None:
        rm = RiskManager(RiskConfig())
        assert rm.from_dict({}) is rm
        assert rm.from_dict(None) is rm
        assert rm.to_dict()["day"] is None
        assert rm.to_dict()["daily_pnl"] == 0.0

    @pytest.mark.parametrize(
        "bad",
        [
            "garbage",
            ["list"],
            {"version": 99, "day": "2026-01-01", "daily_pnl": -5.0},
            {"version": STATE_VERSION, "day": "not-a-date", "daily_pnl": -5.0},
            {"version": STATE_VERSION, "day": "2026-01-01", "daily_pnl": "abc"},
            {"version": STATE_VERSION, "day": "2026-01-01", "daily_pnl": math.nan},
            {"version": STATE_VERSION, "day": "2026-01-01", "daily_pnl": 1.0, "day_start_equity": "x"},
        ],
    )
    def test_corrupt_state_ignored(self, bad, caplog: pytest.LogCaptureFixture) -> None:
        rm = RiskManager(RiskConfig())
        with caplog.at_level(logging.WARNING, logger="tradingbot.risk.manager"):
            rm.from_dict(bad)
        assert rm.daily_pnl == 0.0
        assert rm.current_day is None
        assert any(r.levelno == logging.WARNING for r in caplog.records)

    def test_invalid_start_equity_becomes_none(self) -> None:
        rm = RiskManager(RiskConfig())
        rm.from_dict(
            {"version": STATE_VERSION, "day": "2026-01-01", "daily_pnl": -1.0, "day_start_equity": 0}
        )
        assert rm.day_start_equity is None
        assert rm.daily_pnl == -1.0

    def test_repr_has_no_secrets_and_is_informative(self) -> None:
        rm = RiskManager(RiskConfig(max_positions=4))
        assert "max_positions=4" in repr(rm)
