"""실시간 매매 엔진 (Trader) — ARCHITECTURE.md 6절.

모의투자(PaperBroker)와 실거래(Upbit/ccxt/KIS/Alpaca) 가 같은 코드 경로를 탄다. 브로커 차이는
``BaseBroker`` 인터페이스 뒤에 숨고, 엔진은 "폴링 사이클" 단위로 동작한다.

한 사이클(``run_once``) 의 흐름 (심볼별):
1. ``broker.is_market_open()`` 이 거짓이면(주식 장외) 건너뛴다. ``close_positions_at_market_close`` 가 켜져 있고
   장 마감 직전(다음 폴링이 마감 이후)이면 보유 포지션을 전량 청산하고 신규 진입을 막는다.
   마감 판정 창은 ``poll_seconds + 직전 사이클 소요 시간`` 이다 (사이클 시작 간격은 poll 보다 길 수 있다).
2. ``get_candles`` 로 완성 캔들을 받는다.
   a. ``max_holding_bars`` 가 지난 포지션을 시장가 청산 (진입 신호 캔들 이후 완성된 캔들 수로 계산).
      **매 폴링** 판정하므로 매도가 미체결로 끝나도 다음 폴링에 다시 시도한다.
   마지막 완성 캔들이 상태의 ``last_candle_ts`` 보다 새로우면 "새 캔들" 이벤트:
   b. ``strategy.generate_signal`` (prepare + 마지막 행 signal_at)
   c. BUY MARKET/LIMIT 이고 포지션이 없으면 ``risk.can_open`` → ``position_size`` → 주문 → 체결 대기 → ``risk.apply_entry``
   d. BUY STOP 이면 ``pending_breakouts[sym]`` 에 "진행중 캔들 시가 + stop_offset" 트리거를 등록 (다음 캔들 시작에 만료)
   e. SELL 이고 포지션이 있으면 전량 시장가 매도 → ``Trade`` 기록 → ``risk.record_trade``
3. 매 폴링: 현재가 조회 → 돌파 대기 주문 트리거 판정 → ``risk.check_exit`` (손절/추적손절/익절)
4. 상태 저장 (positions, pending_breakouts, last_candle_ts, trades, risk, paper broker, inflight_entries).

주문 안전 장치 (실거래 중복 주문 방지)
- 매수 주문은 **전송 직전**에 신호 캔들을 소비 처리하고(``last_candle_ts`` 갱신) ``inflight_entries`` 에 기록한 뒤
  상태를 저장한다. 전송 후 오류(응답 유실/타임아웃/파싱 오류)나 크래시가 나도 같은 캔들을 다시 평가해 재주문하지
  않는다. 돌파 대기(STOP) 경로도 같은 ``_enter`` 를 타므로 동일하다.
- 결과를 모르는 매수 주문은 ``_reconcile_inflight`` 가 거래소 기준으로 확정한다: 남은 미체결 매수는 취소하고
  (엔진은 ``fill_timeout_sec`` 뒤 취소하는 정책이므로 동일), 체결된 수량은 원래 신호(손절/최대 보유 기간/진입 캔들)로
  포지션에 채택한다. 확정될 때까지 그 심볼의 신규 매수는 보류한다. 재시작 시에도 가장 먼저 수행한다.
- 체결/청산 직후마다 상태를 저장해 크래시로 잃는 구간을 최소화한다.
- 상태 파일의 ``mode``/``broker`` 가 현재와 다르면(모의투자 → 실거래 전환 등) 복원하지 않고 보관만 한다.

설계 원칙
- ``run_once`` 는 주입된 ``clock`` / ``broker`` / ``sleep`` 만으로 결정적으로 동작한다 (내부 sleep 없음.
  체결 대기 폴링만 주입된 ``sleep`` 을 쓴다).
- 심볼 하나의 오류가 다른 심볼 처리를 막지 않는다. 오류는 로그 + 알림(같은 내용은 10분에 1회) 후 계속.
- 연속 실패 시 ``run_forever`` 가 지수 백오프(최대 5분) 한다. SIGINT/SIGTERM → 상태 저장 후 종료.
- ``run_forever`` 는 사이클 소요 시간을 대기에서 빼서 사이클 시작 간격을 ``poll_seconds`` 로 유지한다.
- 당일 시작 자산 기록(``risk.start_day``)이 실패하면 기록될 때까지 매 폴링 다시 시도한다 (일일 손실 한도 유지).
- 비밀값(키/토큰/웹훅 URL)은 로그/알림에 남기지 않는다 (``mask_secrets``).
- 데모/샘플 시세는 없다. 모든 가격은 브로커(실거래 API 또는 PaperBroker 의 data_source)에서 온다.
- 모의투자(PaperBroker)의 LIMIT/STOP 주문은 거래소가 체결해 주지 않으므로 체결 대기 폴링마다
  ``check_pending`` 으로 현재가 기준 체결 판정을 돌린다 (``_simulate_paper_fill``, ARCHITECTURE §3).
"""

from __future__ import annotations

import logging
import math
import signal as signal_module
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from tradingbot.brokers.base import BaseBroker
from tradingbot.brokers.paper import PaperBroker
from tradingbot.config import AppConfig
from tradingbot.engine.state import (
    StateStore,
    deserialize_position,
    deserialize_signal,
    deserialize_trade,
    dt_from_iso,
    dt_to_iso,
    serialize_position,
    serialize_signal,
    serialize_trade,
    to_jsonable,
)
from tradingbot.exceptions import BrokerError, ConfigError, DataError, TradingBotError
from tradingbot.models import (
    AssetClass,
    Candle,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Signal,
    SignalAction,
    Trade,
    ensure_utc,
    interval_to_seconds,
    utcnow,
)
from tradingbot.notify.base import Notifier, NullNotifier, mask_secrets
from tradingbot.risk.manager import RiskManager
from tradingbot.strategies.base import BaseStrategy, candles_to_df
from tradingbot.utils.timeutil import floor_to_interval, is_krx_open, is_nyse_open

logger = logging.getLogger(__name__)

__all__ = [
    "ERROR_NOTIFY_INTERVAL_SEC",
    "EXIT_MARKET_CLOSE",
    "EXIT_MAX_HOLDING",
    "EXIT_SIGNAL",
    "FILL_POLL_SEC",
    "MAX_BACKOFF_SEC",
    "MAX_SAVED_TRADES",
    "STATE_VERSION",
    "InflightEntry",
    "PendingBreakout",
    "Trader",
]

#: 상태 파일 포맷 버전
STATE_VERSION = 1
#: 상태 파일에 남기는 최근 거래 수
MAX_SAVED_TRADES = 1000
#: 같은 오류 알림의 최소 간격 (초)
ERROR_NOTIFY_INTERVAL_SEC = 600.0
#: 연속 실패 시 백오프 상한 (초)
MAX_BACKOFF_SEC = 300.0
#: 체결 확인 폴링 간격 (초)
FILL_POLL_SEC = 1.0
#: ``run_forever`` 대기 중 종료 플래그를 확인하는 간격 (초)
STOP_CHECK_SEC = 1.0

#: 엔진이 만든 청산 사유 종류 (RiskManager 의 stop_loss / trailing_stop / take_profit 에 추가)
EXIT_SIGNAL = "signal"
EXIT_MAX_HOLDING = "max_holding"
EXIT_MARKET_CLOSE = "market_close"


def _fmt(x: float | None) -> str:
    """로그/알림용 숫자 포맷 (큰 값은 천 단위 구분, 작은 값은 유효숫자)."""
    if x is None or not isinstance(x, (int, float)) or not math.isfinite(x):
        return "-"
    ax = abs(x)
    if ax >= 1000:
        return f"{x:,.0f}"
    if ax >= 1:
        return f"{x:,.2f}"
    return f"{x:.6g}"


def _fmt_qty(q: float) -> str:
    return f"{q:.8g}"


def _tol(x: float) -> float:
    """부동소수 비교 허용 오차 (값 크기에 비례, 최소 1e-9)."""
    return 1e-9 * max(1.0, abs(x))


def _positive(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


@dataclass
class PendingBreakout:
    """STOP BUY 신호(변동성 돌파)를 엔진이 폴링으로 흉내내기 위한 대기 항목.

    - ``trigger``: 현재가가 이 값 이상이면 시장가 매수
    - ``expires``: 이 시각(다음 캔들 시작) 이후에는 무효
    - ``candle_ts``: 신호를 낸 완성 캔들 시각 (포지션의 ``entry_bar_ts`` 로 기록)
    - ``base_open``: 트리거 계산에 쓴 진행중 캔들 시가 (절대 가격 STOP 이면 None)
    """

    symbol: str
    trigger: float
    signal: Signal
    expires: datetime
    created_at: datetime
    candle_ts: datetime | None = None
    base_open: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "trigger": float(self.trigger),
            "signal": serialize_signal(self.signal),
            "expires": dt_to_iso(self.expires),
            "created_at": dt_to_iso(self.created_at),
            "candle_ts": dt_to_iso(self.candle_ts),
            "base_open": None if self.base_open is None else float(self.base_open),
        }

    @classmethod
    def from_dict(cls, d: Any) -> PendingBreakout:
        if not isinstance(d, dict):
            raise DataError(f"PendingBreakout 데이터는 dict 여야 합니다: {type(d).__name__}")
        try:
            expires = dt_from_iso(d.get("expires"))
            created = dt_from_iso(d.get("created_at"))
            if expires is None:
                raise DataError("PendingBreakout 에 expires 가 없습니다")
            trigger = float(d["trigger"])
            if not _positive(trigger):
                raise DataError(f"PendingBreakout 의 trigger 가 유효하지 않습니다: {trigger!r}")
            base_open = d.get("base_open")
            return cls(
                symbol=str(d["symbol"]),
                trigger=trigger,
                signal=deserialize_signal(d["signal"]),
                expires=expires,
                created_at=created if created is not None else expires,
                candle_ts=dt_from_iso(d.get("candle_ts")),
                base_open=None if base_open is None else float(base_open),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise DataError(f"PendingBreakout 복원 실패: {e}") from e


@dataclass
class InflightEntry:
    """전송했지만 결과(체결/취소)를 아직 상태에 반영하지 못한 매수 주문.

    ``_enter`` 가 ``place_order`` 직전에 기록·저장하고, 체결 확인이 끝나면 지운다. 전송 후 오류나 크래시가 나면
    ``_reconcile_inflight`` 가 거래소 기준으로 확정(미체결 취소 / 체결분 채택)할 때까지 남는다.

    - ``signal``: 원래 진입 신호 (채택 시 손절/익절/최대 보유 기간을 이 신호로 적용)
    - ``bar_ts``: 신호 캔들 시각 (포지션의 ``entry_bar_ts``)
    - ``quantity`` / ``order_type``: 전송한 주문 (로그/점검용)
    """

    symbol: str
    signal: Signal
    bar_ts: datetime | None
    created_at: datetime
    quantity: float
    order_type: str = OrderType.MARKET.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "signal": serialize_signal(self.signal),
            "bar_ts": dt_to_iso(self.bar_ts),
            "created_at": dt_to_iso(self.created_at),
            "quantity": float(self.quantity),
            "order_type": self.order_type,
        }

    @classmethod
    def from_dict(cls, d: Any) -> InflightEntry:
        if not isinstance(d, dict):
            raise DataError(f"InflightEntry 데이터는 dict 여야 합니다: {type(d).__name__}")
        try:
            created = dt_from_iso(d.get("created_at"))
            if created is None:
                raise DataError("InflightEntry 에 created_at 이 없습니다")
            return cls(
                symbol=str(d["symbol"]),
                signal=deserialize_signal(d["signal"]),
                bar_ts=dt_from_iso(d.get("bar_ts")),
                created_at=created,
                quantity=float(d.get("quantity", 0.0) or 0.0),
                order_type=str(d.get("order_type") or OrderType.MARKET.value),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise DataError(f"InflightEntry 복원 실패: {e}") from e


class Trader:
    """실시간(모의/실거래) 매매 루프.

    ``broker`` 는 PaperBroker(모의) 또는 실거래 어댑터. ``clock`` 과 ``sleep`` 을 주입하면 ``run_once`` /
    ``run_forever`` 를 결정적으로 테스트할 수 있다. ``state`` 가 None 이면 ``config.engine.state_file`` 을 쓴다.
    """

    def __init__(
        self,
        config: AppConfig,
        broker: BaseBroker,
        strategy: BaseStrategy,
        risk: RiskManager,
        notifier: Notifier | None = None,
        state: StateStore | None = None,
        clock: Callable[[], datetime] = utcnow,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.broker = broker
        self.strategy = strategy
        self.risk = risk
        self.notifier: Notifier = notifier if notifier is not None else NullNotifier()
        self.state: StateStore = state if state is not None else StateStore(config.engine.state_file)
        self._clock = clock
        self._sleep = sleep

        self.symbols: list[str] = list(config.symbols)
        self.interval: str = config.interval
        self._step: int = interval_to_seconds(self.interval)

        # 엔진 상태
        self._positions: dict[str, Position] = {}
        self._pending: dict[str, PendingBreakout] = {}
        self._last_candle_ts: dict[str, datetime] = {}
        self._trades: list[Trade] = []
        self._last_prices: dict[str, float] = {}
        # 결과 미확인 매수 주문 (심볼별 최대 1건)
        self._inflight: dict[str, InflightEntry] = {}
        # 지금 "새 캔들" 이벤트로 평가 중인 캔들 시각 (주문 전송 시 소비 처리용)
        self._evaluating: dict[str, datetime] = {}

        # 루프 제어
        self._stop_event = threading.Event()
        self._started = False
        self._running = False
        self._started_at: datetime | None = None
        self._last_cycle_at: datetime | None = None
        self._last_cycle_seconds = 0.0
        self._cycles = 0
        self._cycle_errors = 0
        self.consecutive_failures = 0

        # 보조 상태
        self._error_notified_at: dict[str, datetime] = {}
        self._current_day: date | None = None
        self._market_was_open: bool | None = None
        self._stale_symbols: set[str] = set()
        self._warned: set[str] = set()

        # 사이징에 쓰는 수수료율: PaperBroker 는 실제 수수료, 그 외는 설정의 paper.fee_pct 를 가정치로 사용
        fee = getattr(broker, "fee_pct", None)
        self._fee_pct: float = (
            float(fee)
            if isinstance(fee, (int, float)) and not isinstance(fee, bool)
            else float(config.paper.fee_pct)
        )

    # ------------------------------------------------------------------ 속성
    @property
    def positions(self) -> dict[str, Position]:
        """엔진이 관리하는 포지션 (복사본)."""
        return dict(self._positions)

    @property
    def pending_breakouts(self) -> dict[str, PendingBreakout]:
        return dict(self._pending)

    @property
    def last_candle_ts(self) -> dict[str, datetime]:
        return dict(self._last_candle_ts)

    @property
    def trades(self) -> list[Trade]:
        return list(self._trades)

    @property
    def inflight_entries(self) -> dict[str, InflightEntry]:
        """결과를 아직 확정하지 못한 매수 주문 (복사본)."""
        return dict(self._inflight)

    @property
    def started(self) -> bool:
        return self._started

    @property
    def running(self) -> bool:
        return self._running

    @property
    def stopped(self) -> bool:
        return self._stop_event.is_set()

    @property
    def cycles(self) -> int:
        return self._cycles

    @property
    def cycle_errors(self) -> int:
        """마지막 ``run_once`` 에서 실패한 심볼(또는 단계) 수."""
        return self._cycle_errors

    def _now(self) -> datetime:
        return ensure_utc(self._clock())

    def _quote(self) -> str:
        try:
            return self.broker.quote_currency(self.symbols[0])
        except Exception:
            return self.config.paper.quote_currency

    def _data_source_name(self) -> str | None:
        ds = getattr(self.broker, "data_source", None)
        return getattr(ds, "name", None) if ds is not None else None

    # ------------------------------------------------------------------ 시작/복원
    def start(self) -> None:
        """상태 복원 → 거래소 포지션 동기화 → 당일 시작 자산 기록 → 시작 배너. 두 번 호출해도 한 번만 수행."""
        if self._started:
            return
        self._validate()
        if isinstance(self.broker, PaperBroker):
            self.broker.clock = self._clock
        now = self._now()
        if self.config.is_live:
            self._log_live_banner()

        data = self.state.load()
        self._restore(data, now)
        # 크래시 전에 전송한 매수 주문은 동기화 옵션과 무관하게 먼저 확정한다 (중복 주문 방지)
        for sym in list(self._inflight):
            self._reconcile_inflight(sym, now)
        if self.config.engine.sync_positions_on_start:
            self._sync_positions(now)
        self._init_day(data, now)

        self._started = True
        self._started_at = now
        banner = self._startup_message()
        logger.info(banner.replace("\n", " | "))
        self.notifier.send(banner)
        self._save_state()

    def _validate(self) -> None:
        supported = tuple(self.broker.supported_intervals or ())
        if supported and self.interval not in supported:
            raise ConfigError(
                f"브로커 {self.broker.name} 는 interval {self.interval!r} 를 지원하지 않습니다 (가능: {', '.join(supported)})"
            )
        warmup = int(self.strategy.warmup)
        if self.config.engine.candle_limit < warmup:
            raise ConfigError(
                f"engine.candle_limit({self.config.engine.candle_limit}) 가 전략 {self.strategy.name} 의 "
                f"warmup({warmup}) 보다 작습니다"
            )

    def _log_live_banner(self) -> None:
        bar = "=" * 72
        logger.warning(bar)
        logger.warning("!!! 실거래(LIVE) 모드 — 실제 자금으로 주문이 전송됩니다 !!!")
        logger.warning(
            "브로커 %s (sandbox=%s) | 심볼 %s | 전략 %s %s | interval %s",
            self.broker.name,
            self.config.broker.sandbox,
            ", ".join(self.symbols),
            self.strategy.name,
            self.strategy.params,
            self.interval,
        )
        logger.warning(
            "리스크: 종목당 %.1f%% | 최대 %d종목 | 손절 %s | 익절 %s | 추적손절 %s | 일일 손실 한도 %s",
            self.config.risk.max_position_pct * 100,
            self.config.risk.max_positions,
            _pct(self.config.risk.stop_loss_pct),
            _pct(self.config.risk.take_profit_pct),
            _pct(self.config.risk.trailing_stop_pct),
            _pct(self.config.risk.max_daily_loss_pct),
        )
        logger.warning(bar)

    def _restore(self, data: dict[str, Any], now: datetime) -> None:
        if not data:
            logger.info("저장된 상태가 없어 새로 시작합니다 (%s)", self.state.path)
            return
        version = data.get("version", STATE_VERSION)
        if version != STATE_VERSION:
            logger.warning(
                "상태 파일 버전 %r 을 지원하지 않습니다 (지원: %d) → 상태를 무시하고 새로 시작",
                version,
                STATE_VERSION,
            )
            return

        saved_mode = data.get("mode")
        saved_broker = data.get("broker")
        if saved_mode not in (None, self.config.mode) or saved_broker not in (None, self.broker.name):
            # 모의투자 상태(모의 손익/거래/포지션/당일 시작 자산)를 실거래에 이어 쓰면 일일 손실 한도가 모의 자산
            # 기준으로 계산되는 등 리스크 통제가 어긋난다 → 복원하지 않고 파일만 보관한다.
            label = f"{saved_mode or 'unknown'}-{saved_broker or 'unknown'}"
            backup = self.state.archive(label)
            msg = (
                f"상태 파일은 {saved_mode or '?'} 모드 / 브로커 {saved_broker or '?'} 에서 저장된 것이라 "
                f"현재 {self.config.mode} / {self.broker.name} 에 복원하지 않습니다"
                + (f" (보관: {backup.name})" if backup is not None else "")
                + " — 포지션은 거래소 동기화로, 당일 시작 자산은 현재 계좌로 새로 기록합니다"
            )
            logger.warning(msg)
            self.notifier.send(f"[경고] {msg}")
            return

        saved_interval = data.get("interval")
        saved_strategy = data.get("strategy")
        same_interval = saved_interval in (None, self.interval)
        same_strategy = saved_strategy in (None, self.strategy.name)
        if not same_interval:
            logger.warning(
                "상태의 interval %s 와 설정 %s 가 다릅니다 → last_candle_ts / 돌파 대기 주문 초기화",
                saved_interval,
                self.interval,
            )
        if not same_strategy:
            logger.warning(
                "상태의 전략 %s 와 설정 %s 가 다릅니다 → 돌파 대기 주문 초기화",
                saved_strategy,
                self.strategy.name,
            )
        if isinstance(self.broker, PaperBroker):
            paper = data.get("paper_broker")
            if isinstance(paper, dict) and paper:
                self._restore_paper_broker(paper)

        for sym, pd_ in (data.get("positions") or {}).items():
            try:
                pos = deserialize_position(pd_)
            except DataError as e:
                logger.warning("%s 포지션 상태 복원 실패, 건너뜀: %s", sym, e)
                continue
            if pos.quantity <= 0:
                continue
            self._positions[str(sym)] = pos

        if same_interval:
            for sym, ts in (data.get("last_candle_ts") or {}).items():
                try:
                    parsed = dt_from_iso(ts)
                except ValueError:
                    logger.warning("%s last_candle_ts 값이 잘못되어 무시합니다: %r", sym, ts)
                    continue
                if parsed is not None:
                    self._last_candle_ts[str(sym)] = parsed

        if same_interval and same_strategy:
            for sym, pb_ in (data.get("pending_breakouts") or {}).items():
                try:
                    pb = PendingBreakout.from_dict(pb_)
                except DataError as e:
                    logger.warning("%s 돌파 대기 주문 복원 실패, 건너뜀: %s", sym, e)
                    continue
                if now >= pb.expires:
                    logger.info("%s 저장된 돌파 대기 주문은 이미 만료됨 (%s)", sym, dt_to_iso(pb.expires))
                    continue
                self._pending[str(sym)] = pb

        # 결과 미확인 매수 주문은 실제 거래소 상태이므로 interval/전략과 무관하게 복원한다
        for sym, ie_ in (data.get("inflight_entries") or {}).items():
            try:
                self._inflight[str(sym)] = InflightEntry.from_dict(ie_)
            except DataError as e:
                logger.error("%s 결과 미확인 주문 기록 복원 실패 — 거래소에서 직접 확인하세요: %s", sym, e)

        for t in data.get("trades") or []:
            try:
                self._trades.append(deserialize_trade(t))
            except DataError as e:
                logger.warning("거래 기록 복원 실패, 건너뜀: %s", e)
        self._trim_trades()

        self.risk.from_dict(data.get("risk"))
        logger.info(
            "상태 복원 (%s): 포지션 %d, 돌파 대기 %d, 거래 %d, 결과 미확인 주문 %d, 마지막 캔들 %s",
            data.get("updated_at", "?"),
            len(self._positions),
            len(self._pending),
            len(self._trades),
            len(self._inflight),
            {s: dt_to_iso(t) for s, t in self._last_candle_ts.items()},
        )

    def _restore_paper_broker(self, saved: dict[str, Any]) -> None:
        """저장된 모의계좌(현금/포지션/주문/거래)를 복원한다. 수수료·슬리피지 등 설정값은 현재 설정을 따른다."""
        assert isinstance(self.broker, PaperBroker)
        current = self.broker.to_dict()
        if saved.get("quote_currency") != current["quote_currency"]:
            logger.warning(
                "저장된 모의계좌 통화 %s 가 현재 %s 와 달라 복원하지 않습니다",
                saved.get("quote_currency"),
                current["quote_currency"],
            )
            return
        merged = {
            **saved,
            "fee_pct": current["fee_pct"],
            "slippage_pct": current["slippage_pct"],
            "min_order_value": current["min_order_value"],
            "asset_class": current["asset_class"],
            # 실시간 모의투자는 항상 현재 시각/현재가를 쓴다 (백테스트 잔재 제거)
            "sim_time": None,
            "prices": {},
        }
        try:
            restored = PaperBroker.from_dict(merged, data_source=self.broker.data_source)
        except DataError as e:
            logger.error("모의계좌 상태 복원 실패 → 새 계좌로 시작합니다: %s", e)
            return
        restored.clock = self._clock
        self.broker = restored
        logger.info(
            "모의계좌 복원: 현금 %s %s, 포지션 %d, 거래 %d (초기 자금 %s)",
            _fmt(restored.cash),
            current["quote_currency"],
            len(restored.get_positions()),
            len(restored.trades),
            _fmt(restored.initial_cash),
        )

    def _sync_positions(self, now: datetime) -> None:
        """거래소 포지션과 상태 포지션을 맞춘다.

        - 거래소에만 있는 (설정 심볼의) 포지션 → 거래소 평균단가로 채택하고 리스크 기본값(손절/익절) 적용
        - 둘 다 있으면 수량은 거래소 값, 평균단가는 거래소 값이 0 보다 클 때만 거래소 값
        - 상태에만 있는 포지션 → 제거 (이미 수동 매도된 것으로 간주)
        """
        try:
            broker_positions = self.broker.get_positions()
        except Exception as e:
            self._handle_error("포지션 동기화", e, now)
            return

        configured = set(self.symbols)
        for sym, bpos in broker_positions.items():
            if bpos.quantity <= 0:
                continue
            if sym not in configured:
                logger.info(
                    "설정에 없는 심볼 %s 를 %s 보유 중 — 봇이 관리하지 않습니다", sym, _fmt_qty(bpos.quantity)
                )
                continue
            mine = self._positions.get(sym)
            if mine is None:
                self._adopt_position(sym, bpos, now)
                if self._pending.pop(sym, None) is not None:
                    logger.info("%s 보유 중이라 저장된 돌파 대기 주문을 제거합니다", sym)
                continue
            if abs(bpos.quantity - mine.quantity) > _tol(mine.quantity):
                logger.warning(
                    "%s 수량 불일치: 상태 %s, 거래소 %s → 거래소 값 사용",
                    sym,
                    _fmt_qty(mine.quantity),
                    _fmt_qty(bpos.quantity),
                )
                mine.quantity = float(bpos.quantity)
            if _positive(bpos.average_price) and abs(bpos.average_price - mine.average_price) > _tol(
                mine.average_price
            ):
                logger.info(
                    "%s 평균단가 갱신: 상태 %s → 거래소 %s",
                    sym,
                    _fmt(mine.average_price),
                    _fmt(bpos.average_price),
                )
                mine.average_price = float(bpos.average_price)

        for sym in list(self._positions):
            held = broker_positions.get(sym)
            if held is None or held.quantity <= 0:
                pos = self._positions.pop(sym)
                self._pending.pop(sym, None)
                msg = f"{sym} 포지션 {_fmt_qty(pos.quantity)} 이(가) 거래소에 없어 상태에서 제거합니다 (수동 매도?)"
                logger.warning(msg)
                self.notifier.send(f"[동기화] {msg}")

    def _adopt_position(
        self,
        sym: str,
        bpos: Position,
        now: datetime,
        *,
        signal: Signal | None = None,
        bar_ts: datetime | None = None,
        label: str = "거래소 보유 포지션 채택",
    ) -> bool:
        """거래소 포지션을 상태에 채택한다. ``signal`` 이 있으면 그 신호(손절/익절/최대 보유 기간)로 진입 처리."""
        avg = float(bpos.average_price) if _positive(bpos.average_price) else None
        note = ""
        if avg is None:
            try:
                avg = float(self.broker.get_ticker(sym))
            except Exception as e:
                logger.error(
                    "%s 평균단가와 현재가를 모두 알 수 없어 포지션을 채택하지 못했습니다: %s", sym, e
                )
                return False
            if not _positive(avg):
                logger.error("%s 현재가가 유효하지 않아 포지션을 채택하지 못했습니다: %r", sym, avg)
                return False
            note = " (평균단가 미상 → 현재가 사용)"
        pos = Position(
            symbol=sym,
            quantity=float(bpos.quantity),
            average_price=avg,
            opened_at=bpos.opened_at or now,
            highest_price=bpos.highest_price,
            stop_loss=bpos.stop_loss,
            take_profit=bpos.take_profit,
            meta=dict(bpos.meta),
        )
        if signal is not None:
            if bar_ts is not None:
                signal.meta["entry_bar_ts"] = dt_to_iso(bar_ts)
            self.risk.apply_entry(pos, signal, avg)
        elif pos.stop_loss is None and pos.take_profit is None:
            self.risk.apply_entry(
                pos, Signal(action=SignalAction.BUY, symbol=sym, reason="거래소 보유 포지션 동기화"), avg
            )
        pos.meta["adopted"] = True
        pos.meta.setdefault("entry_fee", 0.0)
        self._positions[sym] = pos
        msg = f"{sym} {label}: {_fmt_qty(pos.quantity)} @ {_fmt(avg)}{note}"
        logger.warning(msg)
        self.notifier.send(f"[동기화] {msg}")
        return True

    def _init_day(self, data: dict[str, Any], now: datetime) -> None:
        today = now.date()
        saved_day = data.get("day") if data else None
        if saved_day and str(saved_day) != today.isoformat():
            try:
                prev = date.fromisoformat(str(saved_day))
            except ValueError:
                prev = None
            if prev is not None and self.risk.current_day == prev:
                self._send_daily_summary(prev)
        equity = self._safe_equity()
        if equity is not None:
            self.risk.start_day(equity, now)
        else:
            logger.warning("자산 조회에 실패해 당일 시작 자산을 기록하지 못했습니다 (일일 손실 한도 미적용)")
        self._current_day = today

    # ------------------------------------------------------------------ 루프
    def run_once(self) -> None:
        """폴링 사이클 1회. 심볼별 예외는 잡아서 로그/알림 후 다음 심볼로 넘어간다."""
        if not self._started:
            self.start()
        now = self._now()
        self._roll_day(now)
        self._ensure_day_start(now)
        self._cycle_errors = 0

        try:
            market_open = bool(self.broker.is_market_open())
        except Exception as e:
            self._cycle_errors += 1
            self._handle_error("장 운영 여부 확인", e, now)
            market_open = False
        if self._market_was_open is not None and self._market_was_open != market_open:
            logger.info("장 상태 변경: %s", "개장" if market_open else "마감")
        self._market_was_open = market_open
        closing = market_open and self._closing_soon(now)
        if closing:
            logger.info("장 마감 직전 폴링: 보유 포지션 청산 / 신규 진입 중단")

        for sym in self.symbols:
            if self._stop_event.is_set():
                logger.info("종료 요청으로 남은 심볼 처리를 건너뜁니다")
                break
            try:
                self._process_symbol(sym, market_open=market_open, closing=closing)
            except Exception as e:
                self._cycle_errors += 1
                self._handle_error(sym, e, self._now())

        self._cycles += 1
        self._last_cycle_at = self._now()
        self._last_cycle_seconds = max((self._last_cycle_at - now).total_seconds(), 0.0)
        self._save_state()

    def run_forever(self) -> None:
        """``poll_seconds`` 간격 루프. SIGINT/SIGTERM 또는 ``stop()`` 시 상태를 저장하고 종료한다."""
        self._stop_event.clear()
        self._running = True
        previous = self._install_signal_handlers()
        try:
            if not self._started:
                self.start()
            while not self._stop_event.is_set():
                cycle_start = self._now()
                try:
                    self.run_once()
                except Exception as e:
                    self.consecutive_failures += 1
                    self._handle_error("run_once", e, self._now())
                else:
                    if self._cycle_errors > 0:
                        self.consecutive_failures += 1
                    elif self.consecutive_failures:
                        logger.info("정상 동작 복귀 (연속 실패 %d회 후)", self.consecutive_failures)
                        self.consecutive_failures = 0
                if self._stop_event.is_set():
                    break
                # 사이클 소요 시간(HTTP 호출, 체결 대기)을 빼서 사이클 "시작" 간격을 poll_seconds 로 유지한다.
                # 그래야 장 마감 직전 폴링 창(_closing_soon)을 건너뛰지 않는다.
                elapsed = max((self._now() - cycle_start).total_seconds(), 0.0)
                delay = max(self.next_delay() - elapsed, 0.0)
                if self.consecutive_failures:
                    logger.warning("연속 실패 %d회 → %.0f초 후 재시도", self.consecutive_failures, delay)
                self._wait(delay)
        finally:
            self._restore_signal_handlers(previous)
            self._running = False
            self._shutdown()

    def stop(self) -> None:
        """루프 종료 요청 (스레드/시그널 핸들러에서 호출 가능)."""
        if not self._stop_event.is_set():
            logger.info("종료 요청 수신")
        self._stop_event.set()

    def next_delay(self) -> float:
        """다음 폴링까지 대기 시간. 연속 실패 시 지수 백오프 (상한 MAX_BACKOFF_SEC)."""
        poll = float(self.config.engine.poll_seconds)
        if self.consecutive_failures <= 0:
            return poll
        backoff = poll * (2.0 ** min(self.consecutive_failures, 30))
        return max(poll, min(backoff, MAX_BACKOFF_SEC))

    def _wait(self, delay: float) -> None:
        remaining = float(delay)
        while remaining > 0 and not self._stop_event.is_set():
            chunk = min(remaining, STOP_CHECK_SEC)
            self._sleep(chunk)
            remaining -= chunk

    def _shutdown(self) -> None:
        if not self._started:
            return
        logger.info("엔진 종료: 상태 저장 (%s)", self.state.path)
        self._save_state()
        self.notifier.send(
            f"[종료] 트레이딩 봇 종료 ({self.config.mode}, {self.broker.name})\n"
            f"보유 포지션: {', '.join(self._positions) or '없음'}\n"
            f"당일 손익: {self.risk.daily_pnl:+,.0f} {self._quote()} ({self.risk.daily_trades}건)"
        )

    # ------------------------------------------------------------------ 시그널
    def _install_signal_handlers(self) -> dict[int, Any]:
        previous: dict[int, Any] = {}
        if threading.current_thread() is not threading.main_thread():
            logger.debug("메인 스레드가 아니라 시그널 핸들러를 설치하지 않습니다")
            return previous
        for sig in (signal_module.SIGINT, signal_module.SIGTERM):
            try:
                previous[sig] = signal_module.signal(sig, self._on_signal)
            except (ValueError, OSError, AttributeError) as e:
                logger.debug("시그널 %s 핸들러 설치 실패: %s", sig, e)
        return previous

    @staticmethod
    def _restore_signal_handlers(previous: dict[int, Any]) -> None:
        for sig, handler in previous.items():
            try:
                signal_module.signal(sig, handler)
            except (ValueError, OSError, TypeError) as e:
                logger.debug("시그널 %s 핸들러 복원 실패: %s", sig, e)

    def _on_signal(self, signum: int, _frame: Any) -> None:
        try:
            name = signal_module.Signals(signum).name
        except ValueError:
            name = str(signum)
        logger.warning("%s 수신 → 상태 저장 후 종료합니다", name)
        self.stop()

    # ------------------------------------------------------------------ 일자 관리
    def _roll_day(self, now: datetime) -> None:
        today = now.date()
        if self._current_day is None:
            self._current_day = today
            return
        if today == self._current_day:
            return
        logger.info("UTC 날짜 변경 %s → %s", self._current_day, today)
        self._send_daily_summary(self._current_day)
        self._current_day = today

    def _ensure_day_start(self, now: datetime) -> None:
        """당일 시작 자산이 아직 기록되지 않았으면 기록한다 (자산 조회 실패 시 매 폴링 재시도).

        한 번 실패했다고 그날 내내 일일 손실 한도가 꺼진 채 돌면 안 된다. 당일 거래가 이미 있었으면
        (``risk.current_day == today``) 현재 자산에서 당일 실현 손익을 뺀 값이 시작 자산이다.
        """
        today = now.date()
        if self.risk.current_day == today and self.risk.day_start_equity is not None:
            return
        equity = self._safe_equity()
        if equity is None:
            logger.warning("당일(%s) 시작 자산을 기록하지 못했습니다 — 다음 폴링에 다시 시도합니다", today)
            return
        if self.risk.current_day == today:
            equity -= self.risk.daily_pnl
        self.risk.start_day(equity, now)

    def _send_daily_summary(self, day: date) -> None:
        pnl = self.risk.daily_pnl
        start = self.risk.day_start_equity
        quote = self._quote()
        equity = self._safe_equity()
        lines = [
            f"[일일 요약] {day.isoformat()} (UTC)",
            f"자산: {_fmt(equity)} {quote}",
            f"당일 실현 손익: {pnl:+,.0f} {quote}"
            + (f" ({pnl / start * 100:+.2f}%)" if start else "")
            + (f" / 시작 자산 {_fmt(start)}" if start else ""),
            f"거래: {self.risk.daily_trades}건",
            f"보유 포지션: {', '.join(self._positions) or '없음'}",
        ]
        text = "\n".join(lines)
        logger.info(text.replace("\n", " | "))
        if self.config.notify.daily_summary:
            self.notifier.send(text)

    # ------------------------------------------------------------------ 심볼 처리
    def _process_symbol(self, sym: str, *, market_open: bool, closing: bool) -> None:
        now = self._now()
        if not market_open:
            logger.debug("%s 장 마감 상태 → 건너뜀", sym)
            return

        candles = self.broker.get_candles(sym, self.interval, limit=self.config.engine.candle_limit)
        if not candles:
            self._warn_once(f"nocandles:{sym}", "%s 캔들 데이터가 비어 있습니다", sym)
            return
        self._warned.discard(f"nocandles:{sym}")
        last = candles[-1]
        self._check_stale(sym, last, now)

        price = float(self.broker.get_ticker(sym))
        if not _positive(price):
            raise DataError(f"{sym} 현재가가 유효하지 않습니다: {price!r}")
        self._last_prices[sym] = price

        # 결과를 모르는 이전 매수 주문이 있으면 먼저 거래소 기준으로 확정한다 (확정 전에는 신규 매수 보류)
        if sym in self._inflight:
            self._reconcile_inflight(sym, now)

        # (a) 최대 보유 기간 만료 청산 — 매 폴링 판정 (매도가 미체결로 끝나도 다음 폴링에 재시도)
        self._check_max_holding(sym, candles, price)

        prev = self._last_candle_ts.get(sym)
        if prev is None or last.timestamp > prev:
            logger.info(
                "%s 새 캔들 %s (이전 %s) 종가 %s, 현재가 %s",
                sym,
                dt_to_iso(last.timestamp),
                dt_to_iso(prev) if prev else "없음",
                _fmt(last.close),
                _fmt(price),
            )
            # 평가 중 매수 주문을 전송하면 _enter 가 _consume_candle 로 이 캔들을 즉시 소비 처리한다
            # (전송 후 오류가 나도 다음 폴링에 같은 캔들을 재평가해 재주문하지 않는다).
            self._evaluating[sym] = last.timestamp
            try:
                self._on_new_candle(sym, candles, price, now, allow_entry=not closing)
            finally:
                self._evaluating.pop(sym, None)
            self._last_candle_ts[sym] = last.timestamp

        if closing:
            if self._pending.pop(sym, None) is not None:
                logger.info("%s 장 마감 직전이라 돌파 대기 주문을 취소합니다", sym)
            pos = self._positions.get(sym)
            if pos is not None:
                self._exit(sym, pos, reason="장 마감 전 전량 청산", exit_type=EXIT_MARKET_CLOSE, price=price)
            return

        self._check_breakout(sym, price, now)

        pos = self._positions.get(sym)
        if pos is not None:
            exit_sig = self.risk.check_exit(pos, price)
            if exit_sig is not None:
                self._exit(
                    sym,
                    pos,
                    reason=exit_sig.reason or "리스크 청산",
                    exit_type=str(exit_sig.meta.get("exit_type", "risk")),
                    price=price,
                )

    def _on_new_candle(
        self, sym: str, candles: list[Candle], price: float, now: datetime, *, allow_entry: bool
    ) -> None:
        last = candles[-1]

        # 이전 캔들에서 등록한 돌파 대기 주문은 그 캔들이 끝났으므로 무효
        stale = self._pending.pop(sym, None)
        if stale is not None:
            logger.info("%s 이전 돌파 대기 주문 만료 (트리거 %s 미도달)", sym, _fmt(stale.trigger))

        # (b) 전략 신호
        df = candles_to_df(candles)
        sig = self.strategy.generate_signal(sym, df)
        if sig.is_hold:
            logger.debug("%s HOLD: %s", sym, sig.reason)
            return
        logger.info(
            "%s 신호 %s/%s [%s] %s",
            sym,
            sig.action.value.upper(),
            sig.order_type.value,
            dt_to_iso(last.timestamp),
            sig.reason,
        )
        if self.config.notify.notify_on_signal:
            self.notifier.send(
                f"[신호] {sym} {sig.action.value.upper()} ({sig.order_type.value})\n"
                f"캔들: {dt_to_iso(last.timestamp)}\n사유: {sig.reason}"
            )

        if sig.action == SignalAction.BUY:
            if sym in self._positions:
                logger.info("%s 보유 중이라 BUY 신호를 무시합니다", sym)
                return
            if not allow_entry:
                logger.info("%s 장 마감 직전이라 신규 진입을 생략합니다", sym)
                return
            if sig.order_type == OrderType.STOP:
                self._register_breakout(sym, sig, last, price, now)
            else:
                self._enter(sym, sig, price, bar_ts=last.timestamp, now=now)
        elif sig.action == SignalAction.SELL:
            pos = self._positions.get(sym)
            if pos is None:
                logger.debug("%s 포지션이 없어 SELL 신호를 무시합니다", sym)
                return
            self._exit(sym, pos, reason=sig.reason or "전략 매도 신호", exit_type=EXIT_SIGNAL, price=price)

    # ------------------------------------------------------------------ 보유 기간
    def _check_max_holding(self, sym: str, candles: list[Candle], price: float) -> None:
        """(a) ``max_holding_bars`` 가 지난 포지션을 시장가 청산. 매 폴링 호출되므로 미체결이면 자연히 재시도된다."""
        pos = self._positions.get(sym)
        if pos is None:
            return
        mhb = self._max_holding_bars(pos)
        if mhb is None:
            return
        held = self._bars_held(pos, candles)
        if held is not None and held >= mhb:
            self._exit(
                sym,
                pos,
                reason=f"최대 보유 기간 만료 ({held}/{mhb}봉)",
                exit_type=EXIT_MAX_HOLDING,
                price=price,
            )

    @staticmethod
    def _max_holding_bars(pos: Position) -> int | None:
        raw = pos.meta.get("max_holding_bars")
        if raw is None or isinstance(raw, bool):
            return None
        try:
            n = int(raw)
        except (TypeError, ValueError):
            return None
        return n if n > 0 else None

    def _entry_bar_ts(self, pos: Position) -> datetime | None:
        raw = pos.meta.get("entry_bar_ts")
        if raw:
            try:
                parsed = dt_from_iso(raw)
            except ValueError:
                parsed = None
            if parsed is not None:
                return parsed
        if pos.opened_at is not None:
            # 진입 체결 캔들의 직전(완성) 캔들을 신호 캔들로 본다
            return floor_to_interval(pos.opened_at, self.interval) - timedelta(seconds=self._step)
        return None

    def _bars_held(self, pos: Position, candles: list[Candle]) -> int | None:
        """진입 신호 캔들 이후 완성된 캔들 수. 기준 시각을 모르면 None."""
        entry_ts = self._entry_bar_ts(pos)
        if entry_ts is None:
            self._warn_once(
                f"nobarts:{pos.symbol}",
                "%s 진입 캔들 시각을 알 수 없어 max_holding_bars 를 적용하지 못합니다",
                pos.symbol,
            )
            return None
        held = sum(1 for c in candles if c.timestamp > entry_ts)
        if candles and candles[0].timestamp > entry_ts:
            # 목록이 잘려 진입 이전 캔들이 없다 → 시간 차이로 하한 보정
            by_time = int((candles[-1].timestamp - entry_ts).total_seconds() // self._step)
            held = max(held, by_time)
        return held

    # ------------------------------------------------------------------ 돌파 대기 (STOP BUY)
    def _register_breakout(self, sym: str, sig: Signal, last: Candle, price: float, now: datetime) -> None:
        base_open: float | None
        if sig.price is not None and _positive(sig.price):
            trigger = float(sig.price)
            base_open = None
        else:
            offset = sig.stop_offset
            if (
                offset is None
                or not isinstance(offset, (int, float))
                or not math.isfinite(offset)
                or offset < 0
            ):
                logger.warning("%s STOP 신호에 유효한 price/stop_offset 이 없어 무시합니다: %r", sym, offset)
                return
            base_open = self._forming_open(sym, last, price)
            trigger = base_open + float(offset)
        expires = floor_to_interval(now, self.interval) + timedelta(seconds=self._step)
        pb = PendingBreakout(
            symbol=sym,
            trigger=trigger,
            signal=sig,
            expires=expires,
            created_at=now,
            candle_ts=last.timestamp,
            base_open=base_open,
        )
        self._pending[sym] = pb
        logger.info(
            "%s 돌파 대기 등록: 트리거 %s (기준 시가 %s + %s), 만료 %s",
            sym,
            _fmt(trigger),
            _fmt(base_open),
            _fmt(sig.stop_offset) if base_open is not None else "-",
            dt_to_iso(expires),
        )
        if self.config.notify.notify_on_signal:
            self.notifier.send(
                f"[신호] {sym} 돌파 대기: 현재가 >= {_fmt(trigger)} 이면 매수 (만료 {dt_to_iso(expires)})"
            )

    def _forming_open(self, sym: str, last: Candle, price: float) -> float:
        """진행중 캔들의 시가. 받지 못하면 현재가."""
        try:
            partial = self.broker.get_candles(sym, self.interval, limit=2, include_partial=True)
        except Exception as e:
            logger.warning("%s 진행중 캔들 조회 실패, 현재가 %s 를 기준 시가로 사용: %s", sym, _fmt(price), e)
            return price
        if partial:
            c = partial[-1]
            if c.timestamp > last.timestamp and _positive(c.open):
                return float(c.open)
        logger.warning("%s 진행중 캔들을 받지 못해 현재가 %s 를 기준 시가로 사용합니다", sym, _fmt(price))
        return price

    def _check_breakout(self, sym: str, price: float, now: datetime) -> None:
        pb = self._pending.get(sym)
        if pb is None:
            return
        if now >= pb.expires:
            del self._pending[sym]
            logger.info("%s 돌파 대기 만료 (트리거 %s 미도달, 현재가 %s)", sym, _fmt(pb.trigger), _fmt(price))
            return
        if sym in self._positions:
            del self._pending[sym]
            logger.debug("%s 보유 중이라 돌파 대기 주문을 제거합니다", sym)
            return
        if price < pb.trigger:
            return
        # 한 번만 시도한다 (주문 전송 후 오류가 나도 재시도하지 않는다 — 실거래 중복 주문 방지)
        del self._pending[sym]
        sig = pb.signal
        sig.meta["trigger"] = pb.trigger
        sig.meta["breakout_price"] = price
        logger.info("%s 돌파: 현재가 %s >= 트리거 %s → 시장가 매수", sym, _fmt(price), _fmt(pb.trigger))
        self._enter(sym, sig, price, bar_ts=pb.candle_ts, now=now)

    # ------------------------------------------------------------------ 진입
    def _consume_candle(self, sym: str) -> None:
        """평가 중인 신호 캔들을 지금 소비 처리한다 (주문 전송 직전에 호출)."""
        ts = self._evaluating.get(sym)
        if ts is None:
            return
        prev = self._last_candle_ts.get(sym)
        if prev is None or ts > prev:
            self._last_candle_ts[sym] = ts

    def _enter(self, sym: str, sig: Signal, price: float, *, bar_ts: datetime | None, now: datetime) -> None:
        if sym in self._inflight:
            logger.warning("%s 결과를 확인하지 못한 이전 매수 주문이 있어 신규 진입을 보류합니다", sym)
            return
        open_count = len(self._positions)
        ok, why = self.risk.can_open(open_count, now)
        if not ok:
            logger.info("%s 진입 거부: %s", sym, why)
            return

        order_type = OrderType.MARKET
        limit_price: float | None = None
        ref_price = price
        if sig.order_type == OrderType.LIMIT and sig.price is not None and _positive(sig.price):
            order_type = OrderType.LIMIT
            limit_price = float(self.broker.round_price(sym, float(sig.price)))
            ref_price = limit_price

        equity = self._equity()
        cash = self._available_cash(sym)
        min_value = max(float(self.broker.min_order_value(sym)), float(self.config.risk.min_order_value))
        qty = self.risk.position_size(
            equity=equity,
            cash=cash,
            price=ref_price,
            signal=sig,
            open_positions=open_count,
            min_order_value=min_value,
            fee_pct=self._fee_pct,
        )
        qty = float(self.broker.round_quantity(sym, qty)) if qty > 0 else 0.0
        if qty <= 0:
            logger.info(
                "%s 주문 수량 0 → 진입 생략 (자산 %s, 가용 현금 %s, 가격 %s, 최소 주문 %s)",
                sym,
                _fmt(equity),
                _fmt(cash),
                _fmt(ref_price),
                _fmt(min_value),
            )
            return
        if min_value > 0 and qty * ref_price + _tol(min_value) < min_value:
            logger.info(
                "%s 주문 금액 %s 가 최소 주문 금액 %s 미만 → 진입 생략",
                sym,
                _fmt(qty * ref_price),
                _fmt(min_value),
            )
            return

        logger.info(
            "%s 매수 주문 %s %s @ %s (예상 금액 %s %s) 사유: %s",
            sym,
            order_type.value,
            _fmt_qty(qty),
            _fmt(ref_price),
            _fmt(qty * ref_price),
            self._quote(),
            sig.reason,
        )
        # 전송 직전: 신호 캔들 소비 처리 + 진행중 주문 기록 + 상태 저장 (write-ahead).
        # 전송 후 오류/크래시가 나도 같은 캔들을 재평가해 재주문하지 않고, 재시작 시 결과를 확정할 수 있다.
        self._consume_candle(sym)
        self._inflight[sym] = InflightEntry(
            symbol=sym,
            signal=sig,
            bar_ts=bar_ts,
            created_at=now,
            quantity=qty,
            order_type=order_type.value,
        )
        self._save_state()
        try:
            order = self.broker.place_order(sym, OrderSide.BUY, qty, order_type, limit_price)
            order = self._await_fill(order, sym)
        except Exception:
            # 거래소가 주문을 받았을 수 있다 (응답 유실/타임아웃/조회 파싱 오류) → 거래소 기준으로 확정
            self._reconcile_inflight(sym, self._now())
            self._save_state()
            raise
        self._inflight.pop(sym, None)
        filled_qty, fill_price = self._fill_result(order, ref_price)
        if filled_qty <= 0:
            msg = f"{sym} 매수 미체결 (주문 {order.id}, 상태 {order.status.value})"
            logger.warning(msg)
            if self.config.notify.notify_on_error:
                self._notify_error(f"[오류] {msg}", self._now())
            self._save_state()
            return

        position = Position(
            symbol=sym,
            quantity=filled_qty,
            average_price=fill_price,
            opened_at=order.updated_at or self._now(),
        )
        if bar_ts is not None:
            sig.meta["entry_bar_ts"] = dt_to_iso(bar_ts)
        self.risk.apply_entry(position, sig, fill_price)
        position.meta["entry_fee"] = float(order.fee or 0.0)
        position.meta["entry_order_id"] = order.id
        self._positions[sym] = position
        self._save_state()  # 체결 즉시 저장 (사이클 끝까지 기다리지 않는다)
        self._notify_fill(sym, OrderSide.BUY, filled_qty, fill_price, order, sig.reason)

    def _reconcile_inflight(self, sym: str, now: datetime) -> None:
        """결과를 모르는 매수 주문을 거래소 상태로 확정한다.

        1. 해당 심볼의 미체결 매수 주문은 취소한다 (엔진은 ``fill_timeout_sec`` 뒤 취소하는 정책이므로 동일).
        2. 거래소 보유 수량이 있고 상태에 포지션이 없으면 원래 신호로 포지션을 채택한다.
        조회/취소에 실패하면 기록을 남겨 다음 폴링에 다시 시도하고, 그동안 이 심볼의 신규 매수는 막는다.
        예외를 밖으로 던지지 않는다.
        """
        entry = self._inflight.get(sym)
        if entry is None:
            return
        try:
            open_orders = self.broker.get_open_orders(sym)
        except Exception as e:
            logger.error(
                "%s 결과 미확인 매수 주문 확정 실패 (미체결 조회): %s — 다음 폴링에 재시도",
                sym,
                mask_secrets(str(e)),
            )
            return
        for o in open_orders:
            if o.symbol != sym or OrderSide(o.side) != OrderSide.BUY:
                continue
            try:
                self.broker.cancel_order(o.id, sym)
            except Exception as e:
                logger.error(
                    "%s 결과 미확인 매수 주문 %s 취소 실패: %s — 다음 폴링에 재시도",
                    sym,
                    o.id,
                    mask_secrets(str(e)),
                )
                return
            msg = f"{sym} 결과 미확인 매수 주문 {o.id} 취소 (미체결 {_fmt_qty(o.quantity)} @ {_fmt(o.price)})"
            logger.warning(msg)
            self.notifier.send(f"[동기화] {msg}")
        try:
            bpos = self._broker_position(sym)
        except Exception as e:
            logger.error(
                "%s 결과 미확인 매수 주문 확정 실패 (보유 조회): %s — 다음 폴링에 재시도",
                sym,
                mask_secrets(str(e)),
            )
            return
        del self._inflight[sym]
        if bpos is None or bpos.quantity <= 0:
            msg = f"{sym} 결과 미확인 매수 주문 확정: 거래소 보유 없음 (체결되지 않음)"
            logger.warning(msg)
            self.notifier.send(f"[동기화] {msg}")
            return
        if sym in self._positions:
            logger.info("%s 결과 미확인 매수 주문 확정: 이미 상태에 포지션이 있어 유지합니다", sym)
            return
        adopted = self._adopt_position(
            sym,
            bpos,
            now,
            signal=entry.signal,
            bar_ts=entry.bar_ts,
            label="결과 미확인 매수 주문 확정 — 거래소 체결분 채택",
        )
        if not adopted:
            # 평균단가/현재가 모두 알 수 없는 드문 경우: 다음 폴링에 다시 시도한다
            self._inflight[sym] = entry

    # ------------------------------------------------------------------ 청산
    def _exit(self, sym: str, pos: Position, *, reason: str, exit_type: str, price: float | None) -> None:
        qty = float(pos.quantity)
        held = self._broker_held(sym)
        if held is not None:
            if held <= 0:
                self._positions.pop(sym, None)
                msg = f"{sym} 거래소에 보유 수량이 없어 포지션을 제거합니다 (청산 사유: {reason})"
                logger.warning(msg)
                self.notifier.send(f"[동기화] {msg}")
                return
            if held + _tol(qty) < qty:
                logger.warning(
                    "%s 매도 수량 조정: 상태 %s → 거래소 보유 %s", sym, _fmt_qty(qty), _fmt_qty(held)
                )
                pos.quantity = held  # 상태를 거래소 보유량에 맞춘다 (없는 수량을 잔여로 남기지 않는다)
                qty = held
        qty = float(self.broker.round_quantity(sym, qty))
        if qty <= 0:
            logger.error("%s 매도 가능 수량이 0 입니다 (보유 %s)", sym, _fmt_qty(pos.quantity))
            return

        logger.info("%s 매도 주문 %s @ ~%s [%s] %s", sym, _fmt_qty(qty), _fmt(price), exit_type, reason)
        if isinstance(self.broker, PaperBroker):
            order = self.broker.place_order(sym, OrderSide.SELL, qty, OrderType.MARKET, reason=reason)
        else:
            order = self.broker.place_order(sym, OrderSide.SELL, qty, OrderType.MARKET)
        order = self._await_fill(order, sym)
        fallback = price if price is not None else self._last_prices.get(sym, pos.average_price)
        filled_qty, exit_price = self._fill_result(order, fallback)
        if filled_qty <= 0:
            msg = f"{sym} 매도 미체결 (주문 {order.id}, 상태 {order.status.value}) — 포지션 유지"
            logger.warning(msg)
            if self.config.notify.notify_on_error:
                self._notify_error(f"[오류] {msg}", self._now())
            return

        ratio = min(filled_qty / pos.quantity, 1.0) if pos.quantity > 0 else 1.0
        entry_fee = float(pos.meta.get("entry_fee", 0.0) or 0.0)
        fee = entry_fee * ratio + float(order.fee or 0.0)
        exit_time = order.updated_at or self._now()
        trade = Trade(
            symbol=sym,
            side=OrderSide.BUY,
            quantity=filled_qty,
            entry_price=float(pos.average_price),
            exit_price=exit_price,
            entry_time=pos.opened_at or exit_time,
            exit_time=exit_time,
            fee=fee,
            reason=reason,
        )
        self._trades.append(trade)
        self._trim_trades()
        # 당일 집계의 날짜는 엔진 시계 기준 (거래소 체결 시각은 Trade.exit_time 에 그대로 남긴다)
        self.risk.record_trade(trade, now=self._now())

        remaining = pos.quantity - filled_qty
        if remaining <= _tol(pos.quantity):
            # exit_type 은 포지션이 완전히 닫혔을 때만 기록한다 — 부분 체결로 아직 보유 중인 포지션에 남기면
            # 상태 파일/대시보드가 "청산된 포지션" 으로 오해한다
            pos.meta["exit_type"] = exit_type
            self._positions.pop(sym, None)
        else:
            pos.quantity = remaining
            pos.meta["entry_fee"] = entry_fee * (1.0 - ratio)
            logger.warning("%s 부분 체결: 잔여 %s 는 포지션으로 유지합니다", sym, _fmt_qty(remaining))
        self._save_state()  # 청산 즉시 저장
        self._notify_fill(sym, OrderSide.SELL, filled_qty, exit_price, order, reason, trade=trade)

    def _broker_position(self, sym: str) -> Position | None:
        """거래소가 보고하는 포지션 (없으면 None). 조회 실패는 예외로 전파."""
        return self.broker.get_positions().get(sym)

    def _broker_held(self, sym: str) -> float | None:
        """거래소가 보고하는 보유 수량 (조회 실패 시 None)."""
        try:
            p = self._broker_position(sym)
        except Exception as e:
            logger.warning(
                "%s 거래소 보유 수량 조회 실패, 상태 수량으로 매도합니다: %s", sym, mask_secrets(str(e))
            )
            return None
        return float(p.quantity) if p is not None else 0.0

    # ------------------------------------------------------------------ 체결 확인
    def _await_fill(self, order: Order, sym: str) -> Order:
        """terminal 상태가 될 때까지 ``get_order`` 폴링. ``fill_timeout_sec`` 초과 시 취소 후 최종 상태 반환.

        모의투자(PaperBroker)는 거래소 체결 엔진이 없으므로 매 ``get_order`` 폴링 직전에 ``_simulate_paper_fill`` 로
        현재가 기준 체결 판정(``check_pending``)을 돌린다 — 이게 없으면 시장가보다 유리한 LIMIT 매수도 영원히 OPEN 이라
        타임아웃 취소된다. 매수(``_enter``)/매도(``_exit``) 모두 이 함수를 타므로 비시장가 청산 주문이 생겨도 같은 훅이
        적용된다.
        """
        if order.status.is_terminal:
            return order
        timeout = max(float(self.config.broker.fill_timeout_sec), 0.0)
        start = self._now()
        deadline = start + timedelta(seconds=timeout)
        poll = FILL_POLL_SEC if timeout <= 0 else min(FILL_POLL_SEC, timeout)
        max_polls = int(math.ceil(timeout / poll)) + 1 if poll > 0 else 1
        polls = 0
        while not order.status.is_terminal:
            if polls >= max_polls or self._now() >= deadline:
                break
            self._sleep(poll)
            polls += 1
            self._simulate_paper_fill(sym, order)
            try:
                order = self.broker.get_order(order.id, sym)
            except BrokerError as e:
                logger.warning(
                    "%s 주문 %s 상태 조회 실패 (%d/%d): %s",
                    sym,
                    order.id,
                    polls,
                    max_polls,
                    mask_secrets(str(e)),
                )
        if order.status.is_terminal:
            return order

        logger.warning(
            "%s 주문 %s 가 %.0f초 내 체결되지 않아 취소합니다 (상태 %s, 체결 %s/%s)",
            sym,
            order.id,
            timeout,
            order.status.value,
            _fmt_qty(order.filled_quantity),
            _fmt_qty(order.quantity),
        )
        try:
            canceled = self.broker.cancel_order(order.id, sym)
        except BrokerError as e:
            canceled = False
            logger.error("%s 주문 %s 취소 실패: %s", sym, order.id, mask_secrets(str(e)))
        try:
            order = self.broker.get_order(order.id, sym)
        except BrokerError as e:
            logger.warning("%s 주문 %s 취소 후 상태 조회 실패: %s", sym, order.id, mask_secrets(str(e)))
        if not order.status.is_terminal:
            msg = (
                f"{sym} 주문 {order.id} 상태를 확정하지 못했습니다 (취소 {'성공' if canceled else '실패'}, "
                f"상태 {order.status.value}) — 거래소에서 직접 확인하세요"
            )
            logger.error(msg)
            if self.config.notify.notify_on_error:
                self._notify_error(f"[오류] {msg}", self._now())
        return order

    def _simulate_paper_fill(self, sym: str, order: Order) -> None:
        """모의투자 체결 훅 (ARCHITECTURE §3 ``PaperBroker.check_pending`` — 모의투자 폴링용 체결 판정).

        PaperBroker 는 LIMIT/STOP 주문을 OPEN 으로 보관만 하므로 엔진이 현재가로 체결 판정을 돌려 줘야 한다.
        실거래 브로커는 거래소가 체결하므로 아무것도 하지 않는다. 시세 조회/판정 실패는 ``get_order`` 조회 실패와
        같이 경고만 남기고 다음 폴링에 다시 시도한다 (예외를 밖으로 던지지 않는다).
        """
        if not isinstance(self.broker, PaperBroker):
            return
        try:
            price = float(self.broker.get_ticker(sym))
            filled = self.broker.check_pending(sym, price)
        except (BrokerError, DataError) as e:
            logger.warning("%s 주문 %s 모의 체결 판정 실패: %s", sym, order.id, mask_secrets(str(e)))
            return
        for f in filled:
            logger.info(
                "%s 주문 %s 모의 체결: 현재가 %s → 체결가 %s (%s)",
                sym,
                f.id,
                _fmt(price),
                _fmt(f.average_price),
                f.raw.get("fill_basis", "-"),
            )

    @staticmethod
    def _fill_result(order: Order, fallback_price: float) -> tuple[float, float]:
        """(체결 수량, 체결 평균가). 미체결이면 (0.0, fallback)."""
        filled = float(order.filled_quantity or 0.0)
        if filled <= 0 and order.status == OrderStatus.FILLED:
            filled = float(order.quantity)
        if filled <= 0:
            return 0.0, float(fallback_price)
        avg = order.average_price
        if avg is None or not _positive(avg):
            avg = fallback_price
        return filled, float(avg)

    # ------------------------------------------------------------------ 자산/현금
    def _equity(self) -> float:
        try:
            eq = float(self.broker.get_equity(self.symbols))
            if math.isfinite(eq) and eq >= 0:
                return eq
            logger.warning("브로커 자산 평가액이 유효하지 않아 대체 계산합니다: %r", eq)
        except Exception as e:
            logger.warning("자산 조회 실패, 현금 + 포지션 평가로 대체합니다: %s", mask_secrets(str(e)))
        return self._fallback_equity()

    def _fallback_equity(self) -> float:
        try:
            balances = self.broker.get_balances()
        except Exception as e:
            raise BrokerError(f"잔고 조회 실패: {mask_secrets(str(e))}") from e
        quote = self._quote()
        bal = balances.get(quote)
        cash = float(bal.total) if bal is not None else 0.0
        value = 0.0
        for sym, pos in self._positions.items():
            px = self._last_prices.get(sym)
            if px is None:
                try:
                    px = float(self.broker.get_ticker(sym))
                except Exception:
                    px = pos.average_price
            value += pos.market_value(px)
        return cash + value

    def _safe_equity(self) -> float | None:
        try:
            return self._equity()
        except Exception as e:
            logger.warning("자산 평가 실패: %s", mask_secrets(str(e)))
            return None

    def _available_cash(self, sym: str) -> float:
        balances = self.broker.get_balances()
        quote = self.broker.quote_currency(sym)
        bal = balances.get(quote)
        if bal is None:
            logger.warning("%s 결제통화 %s 잔고가 없습니다", sym, quote)
            return 0.0
        return max(float(bal.available), 0.0)

    # ------------------------------------------------------------------ 장 마감
    def _closing_soon(self, now: datetime) -> bool:
        """다음 폴링이 장 마감 이후가 될 수 있는 '마지막 폴링' 인지 (주식 + close_positions_at_market_close).

        다음 사이클은 ``poll_seconds`` 뒤가 아니라 "poll_seconds + 이번 사이클 소요 시간" 뒤에 시작할 수 있으므로
        (HTTP 호출, 체결 대기) 직전 사이클 소요 시간만큼 창을 넓힌다. 창이 넓어 두 번 연속 '마감 직전' 으로 판정돼도
        청산은 멱등이라 무해하지만, 창이 좁아 한 번도 판정되지 않으면 포지션이 다음 장까지 남는다.
        """
        if not self.config.engine.close_positions_at_market_close:
            return False
        if AssetClass(self.broker.asset_class) != AssetClass.STOCK:
            return False
        poll = float(self.config.engine.poll_seconds)
        horizon = poll + max(self._last_cycle_seconds, 0.0)
        for obj in (self.broker, getattr(self.broker, "data_source", None)):
            if obj is None:
                continue
            nxt = getattr(obj, "next_market_close", None)
            if isinstance(nxt, datetime):
                remaining = (ensure_utc(nxt) - now).total_seconds()
                return -poll < remaining <= horizon
        later = now + timedelta(seconds=horizon)
        for fn in (is_krx_open, is_nyse_open):
            if fn(now) and not fn(later):
                return True
        return False

    # ------------------------------------------------------------------ 데이터 신선도
    def _check_stale(self, sym: str, last: Candle, now: datetime) -> None:
        age = (now - last.timestamp).total_seconds() - self._step
        threshold = max(float(self.config.engine.stale_data_minutes) * 60.0, float(self._step))
        if age > threshold:
            if sym not in self._stale_symbols:
                self._stale_symbols.add(sym)
                msg = (
                    f"{sym} 캔들 데이터가 오래되었습니다: 마지막 완성 캔들 {dt_to_iso(last.timestamp)} "
                    f"({age / 60:.0f}분 경과, 허용 {threshold / 60:.0f}분)"
                )
                logger.warning(msg)
                if self.config.notify.notify_on_error:
                    self._notify_error(f"[경고] {msg}", now)
        elif sym in self._stale_symbols:
            self._stale_symbols.discard(sym)
            logger.info("%s 캔들 데이터가 다시 정상 갱신됩니다", sym)

    # ------------------------------------------------------------------ 오류/알림
    def _handle_error(self, context: str, exc: BaseException, now: datetime) -> None:
        text = mask_secrets(f"{type(exc).__name__}: {exc}")
        if isinstance(exc, TradingBotError):
            logger.error("[%s] 처리 오류: %s", context, text)
        else:
            logger.exception("[%s] 예기치 못한 오류: %s", context, text)
        if self.config.notify.notify_on_error:
            self._notify_error(f"[오류] {context}: {text}", now)

    def _notify_error(self, text: str, now: datetime) -> None:
        """같은 오류 알림은 ERROR_NOTIFY_INTERVAL_SEC 에 한 번만 보낸다."""
        key = text[:300]
        last = self._error_notified_at.get(key)
        if last is not None and (now - last).total_seconds() < ERROR_NOTIFY_INTERVAL_SEC:
            logger.debug("동일 오류 알림 생략 (%.0f초 전 전송)", (now - last).total_seconds())
            return
        self._error_notified_at[key] = now
        if len(self._error_notified_at) > 200:
            cutoff = now - timedelta(seconds=ERROR_NOTIFY_INTERVAL_SEC)
            self._error_notified_at = {k: t for k, t in self._error_notified_at.items() if t >= cutoff}
        self.notifier.send(text)

    def _notify_fill(
        self,
        sym: str,
        side: OrderSide,
        qty: float,
        price: float,
        order: Order,
        reason: str,
        *,
        trade: Trade | None = None,
    ) -> None:
        quote = self._quote()
        label = "매수" if side == OrderSide.BUY else "매도"
        lines = [
            f"[체결] {sym} {label} {_fmt_qty(qty)} @ {_fmt(price)} {quote}",
            f"금액: {_fmt(qty * price)} {quote} (수수료 {_fmt(order.fee)})",
            f"사유: {reason or '-'}",
        ]
        if trade is not None:
            lines.append(f"손익: {trade.pnl:+,.0f} {quote} ({trade.pnl_pct * 100:+.2f}%)")
            lines.append(f"당일 누적: {self.risk.daily_pnl:+,.0f} {quote} ({self.risk.daily_trades}건)")
        text = "\n".join(lines)
        logger.info(text.replace("\n", " | "))
        if self.config.notify.notify_on_trade:
            self.notifier.send(text)

    def _warn_once(self, key: str, msg: str, *args: Any) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        logger.warning(msg, *args)

    def _startup_message(self) -> str:
        mode = self.config.mode
        mode_label = {"live": "실거래 (LIVE)", "paper": "모의투자 (PAPER)"}.get(mode, mode)
        ds = self._data_source_name()
        broker_label = self.broker.name + (f" (시세: {ds})" if ds else "")
        equity = self._safe_equity()
        risk = self.config.risk
        lines = [
            "[시작] 트레이딩 봇",
            f"모드: {mode_label}",
            f"브로커: {broker_label}",
            f"심볼: {', '.join(self.symbols)}",
            f"간격: {self.interval} (폴링 {self.config.engine.poll_seconds:g}초)",
            f"전략: {self.strategy.name} {self.strategy.params}",
            f"리스크: 종목당 {risk.max_position_pct * 100:.1f}%, 최대 {risk.max_positions}종목, "
            f"손절 {_pct(risk.stop_loss_pct)}, 익절 {_pct(risk.take_profit_pct)}, "
            f"추적손절 {_pct(risk.trailing_stop_pct)}, 일일 손실 한도 {_pct(risk.max_daily_loss_pct)}",
            f"자산: {_fmt(equity)} {self._quote()}",
            f"보유 포지션: {', '.join(self._positions) or '없음'}",
        ]
        if self._pending:
            lines.append(f"돌파 대기: {', '.join(self._pending)}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ 상태 저장/조회
    def _trim_trades(self) -> None:
        if len(self._trades) > MAX_SAVED_TRADES:
            del self._trades[: len(self._trades) - MAX_SAVED_TRADES]

    def state_dict(self) -> dict[str, Any]:
        """저장용 상태 (JSON 직렬화 가능)."""
        data: dict[str, Any] = {
            "version": STATE_VERSION,
            "updated_at": dt_to_iso(self._now()),
            "started_at": dt_to_iso(self._started_at),
            "mode": self.config.mode,
            "broker": self.broker.name,
            "data_source": self._data_source_name(),
            "strategy": self.strategy.name,
            "strategy_params": to_jsonable(self.strategy.params),
            "interval": self.interval,
            "symbols": list(self.symbols),
            "day": self._current_day.isoformat() if self._current_day is not None else None,
            "cycles": self._cycles,
            "positions": {s: serialize_position(p) for s, p in self._positions.items()},
            "pending_breakouts": {s: pb.to_dict() for s, pb in self._pending.items()},
            "inflight_entries": {s: ie.to_dict() for s, ie in self._inflight.items()},
            "last_candle_ts": {s: dt_to_iso(t) for s, t in self._last_candle_ts.items()},
            "trades": [serialize_trade(t) for t in self._trades[-MAX_SAVED_TRADES:]],
            "risk": self.risk.to_dict(),
        }
        if isinstance(self.broker, PaperBroker):
            data["paper_broker"] = self.broker.to_dict()
        return data

    def _save_state(self) -> None:
        try:
            self.state.save(self.state_dict())
        except DataError as e:
            logger.error("상태 저장 실패: %s", e)
            if self.config.notify.notify_on_error:
                self._notify_error(f"[오류] 상태 저장 실패: {e}", self._now())

    def status(self) -> dict[str, Any]:
        """CLI 표시용 요약. 브로커 조회가 실패하면 해당 값은 None."""
        equity = self._safe_equity()
        cash: float | None = None
        try:
            balances = self.broker.get_balances()
            bal = balances.get(self._quote())
            cash = float(bal.available) if bal is not None else None
        except Exception as e:
            logger.debug("status: 잔고 조회 실패: %s", mask_secrets(str(e)))

        positions: dict[str, Any] = {}
        for sym, pos in self._positions.items():
            px = self._last_prices.get(sym)
            positions[sym] = {
                "quantity": pos.quantity,
                "average_price": pos.average_price,
                "opened_at": dt_to_iso(pos.opened_at),
                "stop_loss": pos.stop_loss,
                "take_profit": pos.take_profit,
                "highest_price": pos.highest_price,
                "last_price": px,
                "unrealized_pnl": pos.unrealized_pnl(px) if px is not None else None,
                "unrealized_pnl_pct": pos.unrealized_pnl_pct(px) if px is not None else None,
                "max_holding_bars": pos.meta.get("max_holding_bars"),
                "entry_reason": pos.meta.get("entry_reason"),
                "entry_bar_ts": pos.meta.get("entry_bar_ts"),
            }
        pending = {
            sym: {
                "trigger": pb.trigger,
                "expires": dt_to_iso(pb.expires),
                "candle_ts": dt_to_iso(pb.candle_ts),
                "reason": pb.signal.reason,
            }
            for sym, pb in self._pending.items()
        }
        last_trade = self._trades[-1] if self._trades else None
        return {
            "mode": self.config.mode,
            "live": self.config.is_live,
            "broker": self.broker.name,
            "data_source": self._data_source_name(),
            "strategy": self.strategy.name,
            "params": to_jsonable(self.strategy.params),
            "symbols": list(self.symbols),
            "interval": self.interval,
            "quote_currency": self._quote(),
            "started": self._started,
            "running": self._running,
            "started_at": dt_to_iso(self._started_at),
            "last_cycle_at": dt_to_iso(self._last_cycle_at),
            "cycles": self._cycles,
            "consecutive_failures": self.consecutive_failures,
            "equity": equity,
            "cash": cash,
            "daily_pnl": self.risk.daily_pnl,
            "daily_trades": self.risk.daily_trades,
            "day_start_equity": self.risk.day_start_equity,
            "positions": positions,
            "pending_breakouts": pending,
            "inflight_entries": {
                sym: {
                    "quantity": ie.quantity,
                    "order_type": ie.order_type,
                    "created_at": dt_to_iso(ie.created_at),
                    "reason": ie.signal.reason,
                }
                for sym, ie in self._inflight.items()
            },
            "last_candle_ts": {s: dt_to_iso(t) for s, t in self._last_candle_ts.items()},
            "last_prices": dict(self._last_prices),
            "trades": len(self._trades),
            "last_trade": serialize_trade(last_trade) if last_trade is not None else None,
        }

    def __repr__(self) -> str:
        return (
            f"<Trader mode={self.config.mode} broker={self.broker.name} strategy={self.strategy.name} "
            f"symbols={self.symbols} positions={len(self._positions)} pending={len(self._pending)}>"
        )


def _pct(v: float | None) -> str:
    return "없음" if v is None else f"{v * 100:g}%"
