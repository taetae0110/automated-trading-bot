"""백테스터 (ARCHITECTURE §5).

실제 거래소에서 내려받은 캔들 DataFrame(``{symbol: df}``) 위에서 전략 → 리스크 → 모의 체결(PaperBroker)
을 bar 단위로 돌린다. 체결/수수료/Trade 생성은 전부 ``PaperBroker`` 가, 사이징·손절·익절 판정은 전부
``RiskManager`` 가 담당하며 이 모듈은 둘을 **시간 순서대로 연결**만 한다.

bar 알고리즘 (심볼별 timestamp 합집합을 시간순으로):
1. 각 심볼 df 를 ``strategy.prepare()`` 로 1회 준비한다.
2. bar 시작 — ``mark_price(sym, open)``:
   a. 새 UTC 날짜면 ``risk.start_day(equity)``.
   b. ``max_holding_bars`` 가 만료된 포지션은 시가에 시장가 청산 (엔진 §6 과 같은 순서: 만료 청산이
      먼저, 그 다음 대기 주문).
   c. 이전 bar 종가에서 만들어진 대기 주문 처리 — MARKET 은 시가 체결, STOP/LIMIT 은 이 시점에
      브로커에 접수한 뒤 ``process_candle`` 로 체결 판정하고, 이 bar 안에 체결되지 않으면 취소한다
      (변동성 돌파의 "다음 캔들" 유효 규칙). 매수 수량은 **체결 기준가(시가/트리거/지정가)** 로 이 시점에
      산정하므로 갭 상승으로 자금이 모자라는 일이 없다.
   d. 리스크 청산(bar 중): 시가 → 저가 → 고가(최고가 갱신) → 저가(갱신된 고점 기준 추적손절) 순으로
      ``risk.check_exit`` 를 호출해 갭이면 시가, 아니면 손절/익절/추적 레벨에 체결한다 (손절 우선).
      이 bar 중간(STOP/LIMIT 트리거)에 진입한 포지션은 체결 이후의 가격 경로를 알 수 없으므로
      종가에서만 판정한다.
3. bar 종료 — ``mark_price(sym, close)``: (bar 중 진입 포지션의 종가 리스크 판정) → ``signal_at`` →
   BUY & 포지션 없음 & ``risk.can_open`` & 예산 > 0 → ``fill_on == "close"`` 면 즉시 시장가 체결,
   아니면 다음 bar 대기 주문. STOP/LIMIT 매수는 항상 다음 bar. SELL & 포지션 있음 → 같은 규칙.
   포지션 보유 중 BUY 와 포지션 없는 SELL 은 무시한다 (단, 다음 bar 시가에 ``max_holding_bars`` 로
   청산될 포지션은 "없음" 으로 간주해 재진입 신호를 받는다 — 엔진 §6 a→b→d 순서와 동일).
4. bar 종료 자산(equity) 기록. 마지막 bar 의 잔여 포지션은 종가로 평가만 한다.
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Any

import numpy as np
import pandas as pd

from tradingbot.backtest.metrics import compute_metrics, format_metrics, periods_per_year
from tradingbot.brokers.paper import PaperBroker
from tradingbot.exceptions import ConfigError, DataError, InsufficientFunds, OrderError
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
)
from tradingbot.risk.manager import RiskManager
from tradingbot.strategies.base import CANDLE_COLUMNS, BaseStrategy

logger = logging.getLogger(__name__)

__all__ = ["FILL_ON_CHOICES", "Backtester", "BacktestResult"]

#: ``fill_on`` 허용값
FILL_ON_CHOICES: tuple[str, ...] = ("next_open", "close")

#: 스킵 사유 키 (BacktestResult.skip_reasons)
SKIP_RISK = "risk.can_open 거부"
SKIP_BUDGET = "예산 부족 (수량 0)"
SKIP_INVALID = "신호 형식 오류"
SKIP_ORDER_ERROR = "주문 접수 실패"
SKIP_ALREADY_HELD = "대기 주문 실행 시 이미 보유"

#: 청산 사유 접두어
REASON_MAX_HOLDING = "최대 보유 기간 만료"


# ---------------------------------------------------------------------------- 직렬화 헬퍼
def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else ensure_utc(dt).isoformat()


def _json_number(x: Any) -> float | int | None:
    """JSON 저장용 숫자: NaN/inf → None."""
    if x is None:
        return None
    if isinstance(x, bool):
        return int(x)
    if isinstance(x, int):
        return x
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _jsonable(value: Any) -> Any:
    """meta/params 저장용 재귀 변환 (datetime → ISO, Enum → value, numpy → python, NaN → None)."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, pd.Timestamp):
        return _iso(value.to_pydatetime())
    if isinstance(value, Mapping):
        return {str(k.value) if isinstance(k, Enum) else str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if isinstance(value, np.generic):
        return _jsonable(value.item())
    if isinstance(value, np.ndarray):
        return [_jsonable(v) for v in value.tolist()]
    return str(value)


def _trade_to_dict(t: Trade) -> dict[str, Any]:
    return {
        "symbol": t.symbol,
        "side": t.side.value,
        "quantity": t.quantity,
        "entry_price": t.entry_price,
        "exit_price": t.exit_price,
        "entry_time": _iso(t.entry_time),
        "exit_time": _iso(t.exit_time),
        "fee": t.fee,
        "reason": t.reason,
        "pnl": _json_number(t.pnl),
        "pnl_pct": _json_number(t.pnl_pct),
        "holding_hours": _json_number(t.holding_seconds / 3600.0),
    }


def _order_to_dict(o: Order) -> dict[str, Any]:
    return {
        "id": o.id,
        "symbol": o.symbol,
        "side": o.side.value,
        "type": o.type.value,
        "quantity": o.quantity,
        "price": o.price,
        "status": o.status.value,
        "filled_quantity": o.filled_quantity,
        "average_price": o.average_price,
        "fee": o.fee,
        "created_at": _iso(o.created_at),
        "updated_at": _iso(o.updated_at),
        "raw": _jsonable(o.raw),
    }


def _position_to_dict(p: Position) -> dict[str, Any]:
    return {
        "symbol": p.symbol,
        "quantity": p.quantity,
        "average_price": p.average_price,
        "opened_at": _iso(p.opened_at),
        "highest_price": p.highest_price,
        "stop_loss": p.stop_loss,
        "take_profit": p.take_profit,
        "meta": _jsonable(p.meta),
    }


def _fmt_money(x: float) -> str:
    return f"{x:,.0f}" if abs(x) >= 1000 else f"{x:,.2f}"


# ---------------------------------------------------------------------------- 결과
@dataclass
class BacktestResult:
    """백테스트 결과 (ARCHITECTURE §5). 계약 필드 뒤에 부가 정보 필드가 붙는다."""

    strategy: str
    params: dict[str, Any]
    symbols: list[str]
    interval: str
    start: datetime
    end: datetime
    initial_cash: float
    final_equity: float
    equity_curve: pd.Series
    trades: list[Trade]
    orders: list[Order]
    metrics: dict[str, float]
    # ---- 부가 정보
    fill_on: str = "next_open"
    fee_pct: float = 0.0
    slippage_pct: float = 0.0
    quote_currency: str = "KRW"
    asset_class: AssetClass = AssetClass.CRYPTO
    final_cash: float = 0.0
    open_positions: list[Position] = field(default_factory=list)
    bars: int = 0
    skipped_signals: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)
    ignored_signals: int = 0
    rejected_orders: int = 0

    # ------------------------------------------------------------------
    @property
    def total_return(self) -> float:
        return self.final_equity / self.initial_cash - 1.0 if self.initial_cash else 0.0

    def summary(self) -> str:
        """사람이 읽는 한국어 요약 (plain text)."""
        q = self.quote_currency
        lines = [
            f"=== 백테스트 결과: {self.strategy} {self.params} ===",
            f"심볼        : {', '.join(self.symbols)}",
            f"간격/체결   : {self.interval} / {self.fill_on} (자산 구분 {self.asset_class.value})",
            f"기간        : {_iso(self.start)} ~ {_iso(self.end)} ({self.bars} bars)",
            f"초기 자산   : {_fmt_money(self.initial_cash)} {q}",
            f"최종 자산   : {_fmt_money(self.final_equity)} {q} ({self.total_return * 100:+.2f}%)",
            f"수수료/슬리피지 : {self.fee_pct * 100:.3f}% / {self.slippage_pct * 100:.3f}%",
            (
                f"거래 {len(self.trades)}건, 주문 {len(self.orders)}건, 스킵된 신호 {self.skipped_signals}건, "
                f"무시된 신호 {self.ignored_signals}건, 거부된 주문 {self.rejected_orders}건"
            ),
        ]
        if self.skip_reasons:
            parts = ", ".join(f"{k} {v}건" for k, v in sorted(self.skip_reasons.items()))
            lines.append(f"스킵 사유   : {parts}")
        if self.open_positions:
            for p in self.open_positions:
                lines.append(
                    f"미청산 포지션: {p.symbol} {p.quantity:.8g} @ {_fmt_money(p.average_price)} {q} (종가 평가)"
                )
        else:
            lines.append("미청산 포지션: 없음")
        lines.append("--- 성과 지표 ---")
        lines.append(format_metrics(self.metrics))
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """JSON 저장용 dict (equity_curve 는 ``[[iso, value], ...]``, 비유한 값은 None)."""
        curve: list[list[Any]] = []
        for ts, value in self.equity_curve.items():
            stamp = pd.Timestamp(ts)
            if stamp.tzinfo is None:
                stamp = stamp.tz_localize("UTC")
            curve.append([stamp.isoformat(), _json_number(value)])
        return {
            "strategy": self.strategy,
            "params": _jsonable(self.params),
            "symbols": list(self.symbols),
            "interval": self.interval,
            "start": _iso(self.start),
            "end": _iso(self.end),
            "initial_cash": self.initial_cash,
            "final_equity": _json_number(self.final_equity),
            "total_return": _json_number(self.total_return),
            "equity_curve": curve,
            "trades": [_trade_to_dict(t) for t in self.trades],
            "orders": [_order_to_dict(o) for o in self.orders],
            "metrics": {k: _json_number(v) for k, v in self.metrics.items()},
            "fill_on": self.fill_on,
            "fee_pct": self.fee_pct,
            "slippage_pct": self.slippage_pct,
            "quote_currency": self.quote_currency,
            "asset_class": self.asset_class.value,
            "final_cash": _json_number(self.final_cash),
            "open_positions": [_position_to_dict(p) for p in self.open_positions],
            "bars": self.bars,
            "skipped_signals": self.skipped_signals,
            "skip_reasons": dict(self.skip_reasons),
            "ignored_signals": self.ignored_signals,
            "rejected_orders": self.rejected_orders,
        }


# ---------------------------------------------------------------------------- 내부 상태
@dataclass
class _SymbolData:
    symbol: str
    prepared: pd.DataFrame
    ts: list[datetime]
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    index_of: dict[datetime, int]

    def candle(self, i: int) -> Candle:
        return Candle(
            timestamp=self.ts[i],
            open=float(self.open[i]),
            high=float(self.high[i]),
            low=float(self.low[i]),
            close=float(self.close[i]),
            volume=float(self.volume[i]),
        )


@dataclass
class _Pending:
    """bar 종가에서 만들어져 다음 bar 시작에 실행되는 대기 주문."""

    signal: Signal
    side: OrderSide
    signal_bar: int


@dataclass
class _SymbolState:
    pending: _Pending | None = None
    entry_bar: int | None = None  # 진입 체결이 일어난 bar (row index)
    entry_intrabar: bool = False  # bar 중간(트리거) 체결 여부
    max_holding_bars: int | None = None

    def clear_entry(self) -> None:
        self.entry_bar = None
        self.entry_intrabar = False
        self.max_holding_bars = None


# ---------------------------------------------------------------------------- 백테스터
class Backtester:
    """전략 + 리스크 + PaperBroker 로 bar 단위 백테스트를 수행한다."""

    def __init__(
        self,
        strategy: BaseStrategy,
        risk: RiskManager,
        *,
        initial_cash: float = 10_000_000,
        fee_pct: float = 0.0005,
        slippage_pct: float = 0.0005,
        fill_on: str = "next_open",
        quote_currency: str = "KRW",
        interval: str = "1d",
        asset_class: AssetClass | str = AssetClass.CRYPTO,
        min_order_value: float = 0.0,
        round_quantity: Callable[[str, float], float] | None = None,
    ) -> None:
        if not isinstance(strategy, BaseStrategy):
            raise ConfigError(f"strategy 는 BaseStrategy 구현이어야 합니다: {type(strategy).__name__}")
        if not isinstance(risk, RiskManager):
            raise ConfigError(f"risk 는 RiskManager 여야 합니다: {type(risk).__name__}")
        if fill_on not in FILL_ON_CHOICES:
            raise ConfigError(f"fill_on 은 {FILL_ON_CHOICES} 중 하나여야 합니다: {fill_on!r}")
        try:
            interval_to_seconds(interval)
        except ValueError as e:
            raise ConfigError(str(e)) from e
        try:
            self.asset_class = AssetClass(asset_class)
        except ValueError as e:
            raise ConfigError(f"알 수 없는 asset_class: {asset_class!r}") from e
        if isinstance(initial_cash, bool) or not isinstance(initial_cash, (int, float)):
            raise ConfigError(f"initial_cash 는 숫자여야 합니다: {initial_cash!r}")
        if not math.isfinite(initial_cash) or initial_cash <= 0:
            raise ConfigError(f"initial_cash 는 0 보다 큰 유한한 값이어야 합니다: {initial_cash!r}")
        if not (0 <= fee_pct < 1) or not (0 <= slippage_pct < 1):
            raise ConfigError(
                f"fee_pct/slippage_pct 는 0 이상 1 미만이어야 합니다: {fee_pct!r}, {slippage_pct!r}"
            )
        if min_order_value < 0:
            raise ConfigError(f"min_order_value 는 0 이상이어야 합니다: {min_order_value!r}")
        quote = (quote_currency or "").strip()
        if not quote:
            raise ConfigError("quote_currency 가 비어 있습니다")

        self.strategy = strategy
        self.risk = risk
        self.initial_cash = float(initial_cash)
        self.fee_pct = float(fee_pct)
        self.slippage_pct = float(slippage_pct)
        self.fill_on = fill_on
        self.quote_currency = quote
        self.interval = interval
        self.min_order_value = float(min_order_value)
        self._round_quantity = round_quantity

        # run() 마다 초기화되는 실행 상태
        self._broker: PaperBroker | None = None
        self._state: dict[str, _SymbolState] = {}
        self._skip_reasons: Counter[str] = Counter()
        self._ignored = 0
        self._rejected = 0
        self._trades_seen = 0
        self._trades_dirty = False

    # ------------------------------------------------------------------ 입력 준비
    def _prepare_symbol(self, symbol: str, df: Any) -> _SymbolData:
        if not isinstance(symbol, str) or not symbol.strip():
            raise DataError(f"심볼이 비어 있습니다: {symbol!r}")
        if not isinstance(df, pd.DataFrame):
            raise DataError(f"{symbol}: 캔들 데이터는 DataFrame 이어야 합니다 ({type(df).__name__})")
        missing = [c for c in CANDLE_COLUMNS if c not in df.columns]
        if missing:
            raise DataError(f"{symbol}: 캔들 컬럼 누락 {missing}")
        if len(df) == 0:
            raise DataError(f"{symbol}: 캔들 데이터가 비어 있습니다")

        work = df.loc[:, CANDLE_COLUMNS].copy()
        try:
            work["timestamp"] = pd.to_datetime(work["timestamp"], utc=True, format="ISO8601")
        except (TypeError, ValueError) as e:
            raise DataError(f"{symbol}: timestamp 변환 실패: {e}") from e
        if work["timestamp"].isna().any():
            raise DataError(f"{symbol}: timestamp 에 결측(NaT) 이 있습니다")
        for col in ("open", "high", "low", "close", "volume"):
            try:
                work[col] = pd.to_numeric(work[col], errors="raise").astype(float)
            except (TypeError, ValueError) as e:
                raise DataError(f"{symbol}: {col} 컬럼이 숫자가 아닙니다: {e}") from e
        ohlc = work[["open", "high", "low", "close"]].to_numpy()
        if not np.isfinite(ohlc).all() or (ohlc <= 0).any():
            raise DataError(f"{symbol}: OHLC 에 NaN/inf/0 이하 값이 있습니다")
        work["volume"] = work["volume"].replace([np.inf, -np.inf], np.nan).fillna(0.0)
        hi = work["high"].to_numpy()
        lo = work["low"].to_numpy()
        oc_max = np.maximum(work["open"].to_numpy(), work["close"].to_numpy())
        oc_min = np.minimum(work["open"].to_numpy(), work["close"].to_numpy())
        if (hi < oc_max).any() or (lo > oc_min).any():
            raise DataError(f"{symbol}: high/low 가 open/close 범위를 포함하지 않는 행이 있습니다")

        before = len(work)
        work = work.sort_values("timestamp", kind="mergesort").drop_duplicates("timestamp", keep="last")
        if len(work) != before:
            logger.warning("%s: 중복 timestamp %d개 제거 (마지막 행 유지)", symbol, before - len(work))
        work = work.reset_index(drop=True)

        prepared = self.strategy.prepare(work)
        if not isinstance(prepared, pd.DataFrame) or len(prepared) != len(work):
            raise DataError(f"{symbol}: strategy.prepare() 가 행 수가 다른 결과를 반환했습니다")
        prepared = prepared.reset_index(drop=True)

        ts_list = [ensure_utc(t.to_pydatetime()) for t in work["timestamp"]]
        if len(work) < self.strategy.warmup:
            logger.warning(
                "%s: 캔들 %d개 < 전략 워밍업 %d개 → 신호가 나오지 않습니다",
                symbol,
                len(work),
                self.strategy.warmup,
            )
        return _SymbolData(
            symbol=symbol,
            prepared=prepared,
            ts=ts_list,
            open=work["open"].to_numpy(dtype=float),
            high=work["high"].to_numpy(dtype=float),
            low=work["low"].to_numpy(dtype=float),
            close=work["close"].to_numpy(dtype=float),
            volume=work["volume"].to_numpy(dtype=float),
            index_of={t: i for i, t in enumerate(ts_list)},
        )

    # ------------------------------------------------------------------ 실행
    def run(self, data: Mapping[str, pd.DataFrame]) -> BacktestResult:
        """``{symbol: 캔들 df}`` 로 백테스트를 수행한다. 데이터가 비었거나 잘못되면 ``DataError``."""
        if not isinstance(data, Mapping) or len(data) == 0:
            raise DataError("백테스트 데이터가 비어 있습니다 ({symbol: DataFrame} 필요)")

        broker = PaperBroker(
            initial_cash=self.initial_cash,
            quote_currency=self.quote_currency,
            fee_pct=self.fee_pct,
            slippage_pct=self.slippage_pct,
            asset_class=self.asset_class,
            min_order_value=self.min_order_value,
        )
        for symbol in data:
            if not isinstance(symbol, str) or not symbol.strip():
                raise DataError(f"심볼이 비어 있습니다: {symbol!r}")
            q = broker.quote_currency(symbol)
            if q != self.quote_currency:
                raise ConfigError(
                    f"{symbol} 의 결제통화 {q} 가 백테스트 계좌 통화 {self.quote_currency} 와 다릅니다"
                )

        symbols_data = [self._prepare_symbol(sym, df) for sym, df in data.items()]
        timeline = sorted({t for sd in symbols_data for t in sd.ts})

        self._broker = broker
        self._state = {sd.symbol: _SymbolState() for sd in symbols_data}
        self._skip_reasons = Counter()
        self._ignored = 0
        self._rejected = 0
        self._trades_seen = 0
        self._trades_dirty = False

        warmup_start = max(self.strategy.warmup - 1, 0)
        equity_values: list[float] = []
        held_bars = 0
        current_day = None

        logger.info(
            "백테스트 시작: %s %s, 심볼 %s, %d bars, 초기 자산 %s %s, fill_on=%s",
            self.strategy.name,
            self.strategy.params,
            [sd.symbol for sd in symbols_data],
            len(timeline),
            _fmt_money(self.initial_cash),
            self.quote_currency,
            self.fill_on,
        )

        for ts in timeline:
            bars = [(sd, sd.index_of[ts]) for sd in symbols_data if ts in sd.index_of]

            # ---- 2. bar 시작: 시가 mark
            for sd, i in bars:
                broker.mark_price(sd.symbol, float(sd.open[i]), timestamp=ts)
            day = ts.date()
            if day != current_day:
                current_day = day
                self.risk.start_day(broker.get_equity(), ts)

            for sd, i in bars:
                self._expire_position(sd, i, ts)
            self._sync_trades()
            for sd, i in bars:
                self._execute_pending(sd, i, ts)
            self._sync_trades()
            for sd, i in bars:
                self._risk_exit_intrabar(sd, i, ts)
            self._sync_trades()

            # ---- 3. bar 종료: 종가 mark → 종가 리스크 판정(bar 중 진입분) → 신호
            for sd, i in bars:
                broker.mark_price(sd.symbol, float(sd.close[i]), timestamp=ts)
            for sd, i in bars:
                self._risk_exit_close(sd, i, ts)
            self._sync_trades()
            for sd, i in bars:
                if i < warmup_start:
                    continue
                sig = self.strategy.signal_at(sd.symbol, sd.prepared, i)
                self._handle_signal(sd, i, ts, sig)
            self._sync_trades()

            # ---- 4. 자산 기록
            equity_values.append(float(broker.get_equity()))
            if broker.get_positions():
                held_bars += 1

        index = pd.DatetimeIndex(pd.to_datetime(timeline, utc=True), name="timestamp")
        equity_curve = pd.Series(equity_values, index=index, name="equity", dtype=float)
        trades = broker.trades
        n_bars = len(timeline)
        exposure = held_bars / n_bars if n_bars else 0.0
        metrics = compute_metrics(
            equity_curve,
            trades,
            periods_per_year(self.interval, self.asset_class),
            exposure=exposure,
        )
        open_positions = [replace(p, meta=dict(p.meta)) for p in broker.get_positions().values()]
        result = BacktestResult(
            strategy=self.strategy.name,
            params=dict(self.strategy.params),
            symbols=[sd.symbol for sd in symbols_data],
            interval=self.interval,
            start=timeline[0],
            end=timeline[-1],
            initial_cash=self.initial_cash,
            final_equity=float(equity_values[-1]),
            equity_curve=equity_curve,
            trades=trades,
            orders=broker.orders,
            metrics=metrics,
            fill_on=self.fill_on,
            fee_pct=self.fee_pct,
            slippage_pct=self.slippage_pct,
            quote_currency=self.quote_currency,
            asset_class=self.asset_class,
            final_cash=float(broker.cash),
            open_positions=open_positions,
            bars=n_bars,
            skipped_signals=int(sum(self._skip_reasons.values())),
            skip_reasons=dict(self._skip_reasons),
            ignored_signals=self._ignored,
            rejected_orders=self._rejected,
        )
        logger.info(
            "백테스트 종료: 최종 자산 %s %s (%+.2f%%), 거래 %d건, 스킵 %d, 거부 %d",
            _fmt_money(result.final_equity),
            self.quote_currency,
            result.total_return * 100,
            len(trades),
            result.skipped_signals,
            result.rejected_orders,
        )
        self._broker = None
        return result

    # ------------------------------------------------------------------ 공통 헬퍼
    @property
    def _b(self) -> PaperBroker:
        assert self._broker is not None, "run() 밖에서 호출되었습니다"
        return self._broker

    def _sync_trades(self) -> None:
        """브로커가 새로 만든 Trade 를 RiskManager 에 반영한다 (매도 체결이 있었을 때만 조회)."""
        if not self._trades_dirty:
            return
        self._trades_dirty = False
        trades = self._b.trades
        while self._trades_seen < len(trades):
            self.risk.record_trade(trades[self._trades_seen])
            self._trades_seen += 1

    def _skip(self, symbol: str, reason_key: str, detail: str) -> None:
        self._skip_reasons[reason_key] += 1
        logger.info("%s 신호 스킵 [%s] %s", symbol, reason_key, detail)

    def _open_count(self, symbol: str, will_exit: bool) -> int:
        """risk.can_open 에 넘길 보유 종목 수.

        다른 심볼은 "보유 중이거나 다음 bar 에 매수가 대기 중" 이면 1 (둘 다여도 1 — 만료 청산 후 재진입).
        평가 대상 심볼은 다음 시가에 만료 청산될 예정(``will_exit``)이면 0, 아니면 보유 여부.
        """
        positions = self._b.get_positions()
        count = 0
        for sym, st in self._state.items():
            held = sym in positions
            if sym == symbol:
                if held and not will_exit:
                    count += 1
                continue
            if held or (st.pending is not None and st.pending.side == OrderSide.BUY):
                count += 1
        return count

    def _buy_reference_price(self, sig: Signal, base_open: float) -> float | None:
        """매수 수량 산정용 체결 기준가 (슬리피지 포함). 형식이 잘못된 신호면 None."""
        if sig.order_type == OrderType.MARKET:
            return base_open * (1.0 + self.slippage_pct)
        if sig.order_type == OrderType.LIMIT:
            if sig.price is None or not math.isfinite(sig.price) or sig.price <= 0:
                return None
            return float(sig.price)
        if sig.order_type == OrderType.STOP:
            if sig.price is not None:
                if not math.isfinite(sig.price) or sig.price <= 0:
                    return None
                trigger = float(sig.price)
            elif sig.stop_offset is not None:
                if not math.isfinite(sig.stop_offset) or sig.stop_offset < 0:
                    return None
                trigger = base_open + float(sig.stop_offset)
            else:
                return None
            return max(base_open, trigger) * (1.0 + self.slippage_pct)
        return None

    def _size(self, sd: _SymbolData, sig: Signal, ref_price: float, open_count: int) -> float:
        b = self._b
        qty = self.risk.position_size(
            equity=b.get_equity(),
            cash=b.cash,
            price=ref_price,
            signal=sig,
            open_positions=open_count,
            min_order_value=b.min_order_value(sd.symbol),
            fee_pct=self.fee_pct,
        )
        if qty <= 0 or not math.isfinite(qty):
            return 0.0
        if self._round_quantity is not None:
            qty = float(self._round_quantity(sd.symbol, qty))
        else:
            qty = float(b.round_quantity(sd.symbol, qty))
        return qty if qty > 0 and math.isfinite(qty) else 0.0

    def _clamp(self, sd: _SymbolData, i: int, price: float) -> float:
        return float(min(max(price, sd.low[i]), sd.high[i]))

    def _market_exit(
        self, sd: _SymbolData, i: int, ts: datetime, *, price: float, reason: str, mark_after: float
    ) -> Order | None:
        """포지션 전량을 ``price`` 에 시장가(슬리피지 적용) 청산한다. 청산 후 현재가를 ``mark_after`` 로 되돌린다."""
        b = self._b
        pos = b.get_positions().get(sd.symbol)
        if pos is None:
            return None
        px = self._clamp(sd, i, price)
        b.mark_price(sd.symbol, px, timestamp=ts)
        try:
            order = b.place_order(sd.symbol, OrderSide.SELL, pos.quantity, OrderType.MARKET, reason=reason)
        except (InsufficientFunds, OrderError) as e:
            self._rejected += 1
            logger.warning("%s 청산 주문 실패 (%s): %s", sd.symbol, reason, e)
            return None
        finally:
            b.mark_price(sd.symbol, mark_after, timestamp=ts)
        self._state[sd.symbol].clear_entry()
        self._trades_dirty = True
        return order

    # ------------------------------------------------------------------ 2-b. 보유 기간 만료
    def _will_expire_next_open(self, symbol: str, i: int) -> bool:
        st = self._state[symbol]
        return (
            st.entry_bar is not None
            and st.max_holding_bars is not None
            and (i + 1 - st.entry_bar) >= st.max_holding_bars
        )

    def _expire_position(self, sd: _SymbolData, i: int, ts: datetime) -> None:
        st = self._state[sd.symbol]
        if sd.symbol not in self._b.get_positions() or st.entry_bar is None or st.max_holding_bars is None:
            return
        if i - st.entry_bar >= st.max_holding_bars:
            reason = f"{REASON_MAX_HOLDING} ({st.max_holding_bars}봉) → 시가 청산"
            self._market_exit(sd, i, ts, price=float(sd.open[i]), reason=reason, mark_after=float(sd.open[i]))

    # ------------------------------------------------------------------ 2-c. 대기 주문
    def _execute_pending(self, sd: _SymbolData, i: int, ts: datetime) -> None:
        st = self._state[sd.symbol]
        pending = st.pending
        if pending is None:
            return
        st.pending = None
        sig = pending.signal
        b = self._b
        open_px = float(sd.open[i])

        if pending.side == OrderSide.SELL:
            pos = b.get_positions().get(sd.symbol)
            if pos is None:
                logger.debug("%s 대기 매도 실행 시 포지션 없음 (이미 청산) → 무시", sd.symbol)
                return
            if sig.order_type == OrderType.MARKET:
                self._market_exit(sd, i, ts, price=open_px, reason=sig.reason, mark_after=open_px)
                return
            self._place_and_process(sd, i, ts, sig, OrderSide.SELL, pos.quantity)
            return

        # ---- 매수
        if sd.symbol in b.get_positions():
            self._skip(sd.symbol, SKIP_ALREADY_HELD, "대기 매수 실행 시점에 이미 포지션 보유")
            return
        ref = self._buy_reference_price(sig, open_px)
        if ref is None:
            self._skip(sd.symbol, SKIP_INVALID, f"{sig.order_type.value} 주문의 가격/오프셋이 없거나 잘못됨")
            return
        qty = self._size(sd, sig, ref, self._open_count(sd.symbol, will_exit=False))
        if qty <= 0:
            self._skip(sd.symbol, SKIP_BUDGET, f"기준가 {ref:,.2f} 에서 예산 부족")
            return
        if sig.order_type == OrderType.MARKET:
            try:
                order = b.place_order(sd.symbol, OrderSide.BUY, qty, OrderType.MARKET, reason=sig.reason)
            except (InsufficientFunds, OrderError) as e:
                self._skip(sd.symbol, SKIP_ORDER_ERROR, str(e))
                return
            self._on_entry_fill(sd, i, ts, sig, order, intrabar=False)
            return
        self._place_and_process(sd, i, ts, sig, OrderSide.BUY, qty)

    def _place_and_process(
        self, sd: _SymbolData, i: int, ts: datetime, sig: Signal, side: OrderSide, qty: float
    ) -> None:
        """STOP/LIMIT 주문을 접수하고 이번 bar 캔들로 체결 판정한다. 미체결이면 취소한다."""
        b = self._b
        price = sig.price
        stop_offset = None
        if sig.order_type == OrderType.STOP:
            if price is None:
                stop_offset = sig.stop_offset
            elif sig.stop_offset is not None:
                logger.debug(
                    "%s STOP 신호에 price 와 stop_offset 이 모두 있어 price 를 사용합니다", sd.symbol
                )
        try:
            order = b.place_order(
                sd.symbol,
                side,
                qty,
                sig.order_type,
                price=price,
                stop_offset=stop_offset,
                reason=sig.reason,
            )
        except (InsufficientFunds, OrderError) as e:
            self._skip(sd.symbol, SKIP_ORDER_ERROR, str(e))
            return
        filled = b.process_candle(sd.symbol, sd.candle(i))
        final = b.get_order(order.id)
        if filled:
            fill = filled[0]
            if side == OrderSide.BUY:
                trigger = fill.raw.get("trigger", fill.price)
                intrabar = trigger is not None and float(trigger) > float(sd.open[i])
                self._on_entry_fill(sd, i, ts, sig, fill, intrabar=intrabar)
            else:
                self._state[sd.symbol].clear_entry()
                self._trades_dirty = True
            return
        if final.status == OrderStatus.OPEN:
            b.cancel_order(order.id)
            logger.debug("%s %s 주문 %s 이번 bar 미체결 → 취소", sd.symbol, sig.order_type.value, order.id)
        elif final.status == OrderStatus.REJECTED:
            self._rejected += 1
            logger.warning("%s 주문 %s 거부: %s", sd.symbol, order.id, final.raw.get("reject_reason", ""))

    def _on_entry_fill(
        self, sd: _SymbolData, i: int, ts: datetime, sig: Signal, order: Order, *, intrabar: bool
    ) -> None:
        pos = self._b.get_positions().get(sd.symbol)
        if pos is None or order.average_price is None:  # pragma: no cover - 방어 코드
            logger.error("%s 매수 체결 후 포지션을 찾을 수 없습니다: %s", sd.symbol, order)
            return
        risk_sig = replace(sig, meta={**sig.meta, "entry_bar_ts": ts.isoformat()})
        self.risk.apply_entry(pos, risk_sig, float(order.average_price))
        st = self._state[sd.symbol]
        st.entry_bar = i
        st.entry_intrabar = intrabar
        hold = sig.max_holding_bars
        st.max_holding_bars = (
            int(hold) if isinstance(hold, int) and not isinstance(hold, bool) and hold > 0 else None
        )
        logger.debug(
            "%s 진입 bar=%d price=%.8g intrabar=%s max_holding=%s",
            sd.symbol,
            i,
            order.average_price,
            intrabar,
            sig.max_holding_bars,
        )

    # ------------------------------------------------------------------ 2-d. 리스크 청산 (bar 중)
    def _exit_price_from(self, sig: Signal, fallback: float) -> float:
        level = sig.meta.get("level") if isinstance(sig.meta, dict) else None
        if (
            isinstance(level, (int, float))
            and not isinstance(level, bool)
            and math.isfinite(level)
            and level > 0
        ):
            return float(level)
        return fallback

    def _risk_exit_intrabar(self, sd: _SymbolData, i: int, ts: datetime) -> None:
        b = self._b
        pos = b.get_positions().get(sd.symbol)
        if pos is None:
            return
        st = self._state[sd.symbol]
        if st.entry_bar == i and st.entry_intrabar:
            return  # 트리거 체결 이후의 가격 경로를 알 수 없음 → 종가에서 판정
        o, h, lo = float(sd.open[i]), float(sd.high[i]), float(sd.low[i])

        sig = self.risk.check_exit(pos, o)  # 갭
        if sig is not None:
            self._market_exit(sd, i, ts, price=o, reason=sig.reason, mark_after=o)
            return
        sig = self.risk.check_exit(pos, lo)  # 손절 / 이전 고점 기준 추적손절
        if sig is not None:
            self._market_exit(
                sd, i, ts, price=self._exit_price_from(sig, lo), reason=sig.reason, mark_after=o
            )
            return
        sig = self.risk.check_exit(pos, h)  # 최고가 갱신 + 익절
        if sig is not None:
            self._market_exit(sd, i, ts, price=self._exit_price_from(sig, h), reason=sig.reason, mark_after=o)
            return
        if self.risk.config.trailing_stop_pct is not None:
            sig = self.risk.check_exit(pos, lo)  # 갱신된 고점 기준 추적손절
            if sig is not None:
                self._market_exit(
                    sd, i, ts, price=self._exit_price_from(sig, lo), reason=sig.reason, mark_after=o
                )

    def _risk_exit_close(self, sd: _SymbolData, i: int, ts: datetime) -> None:
        pos = self._b.get_positions().get(sd.symbol)
        if pos is None:
            return
        st = self._state[sd.symbol]
        if not (st.entry_bar == i and st.entry_intrabar):
            return
        c = float(sd.close[i])
        sig = self.risk.check_exit(pos, c)
        if sig is not None:
            self._market_exit(sd, i, ts, price=c, reason=sig.reason, mark_after=c)

    # ------------------------------------------------------------------ 3. 신호 처리 (bar 종가)
    def _handle_signal(self, sd: _SymbolData, i: int, ts: datetime, sig: Signal) -> None:
        if not isinstance(sig, Signal):
            raise DataError(f"{sd.symbol}: 전략이 Signal 이 아닌 값을 반환했습니다: {type(sig).__name__}")
        if sig.is_hold:
            return
        b = self._b
        st = self._state[sd.symbol]
        pos = b.get_positions().get(sd.symbol)
        close_px = float(sd.close[i])

        if sig.action == SignalAction.BUY:
            will_exit = pos is not None and self._will_expire_next_open(sd.symbol, i)
            if pos is not None and not will_exit:
                self._ignored += 1
                logger.debug("%s 포지션 보유 중 BUY 무시: %s", sd.symbol, sig.reason)
                return
            ref = self._buy_reference_price(sig, close_px)
            if ref is None:
                self._skip(
                    sd.symbol, SKIP_INVALID, f"{sig.order_type.value} 주문의 가격/오프셋이 없거나 잘못됨"
                )
                return
            ok, why = self.risk.can_open(self._open_count(sd.symbol, will_exit), ts)
            if not ok:
                self._skip(sd.symbol, SKIP_RISK, why)
                return
            qty = self._size(sd, sig, ref, self._open_count(sd.symbol, will_exit))
            if qty <= 0:
                self._skip(sd.symbol, SKIP_BUDGET, f"기준가 {ref:,.2f} 에서 예산 부족")
                return
            if self.fill_on == "close" and sig.order_type == OrderType.MARKET and not will_exit:
                try:
                    order = b.place_order(sd.symbol, OrderSide.BUY, qty, OrderType.MARKET, reason=sig.reason)
                except (InsufficientFunds, OrderError) as e:
                    self._skip(sd.symbol, SKIP_ORDER_ERROR, str(e))
                    return
                self._on_entry_fill(sd, i, ts, sig, order, intrabar=False)
                return
            st.pending = _Pending(signal=sig, side=OrderSide.BUY, signal_bar=i)
            return

        if sig.action == SignalAction.SELL:
            if pos is None:
                self._ignored += 1
                logger.debug("%s 포지션 없음 SELL 무시: %s", sd.symbol, sig.reason)
                return
            if sig.order_type == OrderType.LIMIT and (sig.price is None or sig.price <= 0):
                self._skip(sd.symbol, SKIP_INVALID, "LIMIT 매도 신호에 price 가 없습니다")
                return
            if sig.order_type == OrderType.STOP and sig.price is None and sig.stop_offset is None:
                self._skip(sd.symbol, SKIP_INVALID, "STOP 매도 신호에 price/stop_offset 이 없습니다")
                return
            if self.fill_on == "close" and sig.order_type == OrderType.MARKET:
                self._market_exit(sd, i, ts, price=close_px, reason=sig.reason, mark_after=close_px)
                return
            st.pending = _Pending(signal=sig, side=OrderSide.SELL, signal_bar=i)
            return

        raise DataError(f"{sd.symbol}: 알 수 없는 신호 action: {sig.action!r}")
