"""백테스트 작업 실행기 — ``POST /api/backtest`` 가 등록한 작업을 백그라운드 워커 스레드에서 순서대로 실행한다.

- 데이터 해석은 ``tradingbot.cli.backtest`` 와 같다: ``auto`` 는 저장된 CSV 가 구간을 덮으면 csv, 아니면 브로커 공개 API
  (키 없는 주식 브로커는 yfinance). 데이터를 만들어 내지 않는다 — 없으면 실제 거래소/yfinance 에서 내려받아 캐시한다.
- 작업은 최근 ``MAX_JOBS`` 개만 메모리에 남긴다. 결과는 JSON 직렬화 가능한 dict (``equity`` 는 최대 ``MAX_EQUITY_POINTS`` 점).
- 잘못된 전략/파라미터/구간은 작업의 ``status="error"`` + 한국어 ``error`` 로 보고한다 (요청 본문 구조 오류만 400).
"""

from __future__ import annotations

import logging
import queue
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from tradingbot.backtest import Backtester, BacktestResult, format_metrics
from tradingbot.brokers.base import BaseBroker
from tradingbot.cli import (
    BROKER_INFO,
    Source,
    _asset_class_for,
    _broker_has_credentials,
    _csv_coverage,
    _data_broker_name,
    _default_start,
    _load_csv,
    _load_yfinance_to_store,
    _quiet_backtest_logs,
    _quote_currency_for,
    _require_data_credentials,
    _stock_round_quantity,
    resolve_config,
)
from tradingbot.config import AppConfig
from tradingbot.data import CandleStore, to_utc_datetime
from tradingbot.engine.state import dt_to_iso, serialize_trade, to_jsonable
from tradingbot.exceptions import DataError, TradingBotError
from tradingbot.models import INTERVAL_SECONDS, AssetClass, ensure_utc, utcnow
from tradingbot.notify import mask_secrets
from tradingbot.risk import RiskManager
from tradingbot.strategies import create_strategy

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_EQUITY_POINTS",
    "MAX_JOBS",
    "BacktestJob",
    "BacktestJobRunner",
    "JobRequestError",
    "result_to_dict",
]

#: 메모리에 남기는 최근 작업 수
MAX_JOBS = 20
#: 응답에 담는 자산곡선 최대 점 수 (초과 시 균등 샘플링, 마지막 점은 유지)
MAX_EQUITY_POINTS = 5000
#: 하나의 작업이 다룰 수 있는 최대 심볼 수
MAX_SYMBOLS = 20

JobStatus = str  # "queued" | "running" | "done" | "error"


class JobRequestError(TradingBotError):
    """요청 본문 구조 오류 (HTTP 400)."""


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _equity_points(curve: pd.Series) -> list[dict[str, Any]]:
    n = len(curve)
    if n == 0:
        return []
    if n > MAX_EQUITY_POINTS:
        stride = -(-n // MAX_EQUITY_POINTS)
        idx = list(range(0, n, stride))
        if idx[-1] != n - 1:
            idx.append(n - 1)
    else:
        idx = list(range(n))
    index = curve.index
    values = curve.to_numpy(dtype=float)
    out: list[dict[str, Any]] = []
    for i in idx:
        stamp = pd.Timestamp(index[i])
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        out.append({"t": stamp.isoformat(), "equity": _num(values[i])})
    return out


def result_to_dict(result: BacktestResult, sources: Mapping[str, str] | None = None) -> dict[str, Any]:
    """``BacktestResult`` → 대시보드 응답 (계약: summary, metrics, equity, trades, orders, start, end, ...)."""
    trades = [serialize_trade(t) for t in result.trades]
    for t, row in zip(result.trades, trades, strict=True):
        row["holding_hours"] = _num(t.holding_seconds / 3600.0)
    return {
        "summary": result.summary(),
        "metrics": {k: _num(v) for k, v in result.metrics.items()},
        "metrics_text": format_metrics(result.metrics),
        "equity": _equity_points(result.equity_curve),
        "trades": trades,
        "orders": len(result.orders),
        "start": dt_to_iso(result.start),
        "end": dt_to_iso(result.end),
        "initial_cash": float(result.initial_cash),
        "final_equity": _num(result.final_equity),
        "final_cash": _num(result.final_cash),
        "total_return": _num(result.total_return),
        "strategy": result.strategy,
        "params": to_jsonable(result.params),
        "symbols": list(result.symbols),
        "interval": result.interval,
        "quote_currency": result.quote_currency,
        "asset_class": result.asset_class.value,
        "fill_on": result.fill_on,
        "fee_pct": result.fee_pct,
        "slippage_pct": result.slippage_pct,
        "bars": result.bars,
        "open_positions": len(result.open_positions),
        "skipped_signals": result.skipped_signals,
        "skip_reasons": dict(result.skip_reasons),
        "ignored_signals": result.ignored_signals,
        "rejected_orders": result.rejected_orders,
        "sources": dict(sources or {}),
    }


# ============================================================================ 작업
@dataclass
class BacktestJob:
    job_id: str
    request: dict[str, Any]
    created_at: datetime
    strategy: str | None
    symbols: list[str]
    interval: str | None
    status: JobStatus = "queued"
    progress: str | None = None
    error: str | None = None
    result: dict[str, Any] | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def set_progress(self, text: str) -> None:
        with self._lock:
            self.progress = text

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {
                "job_id": self.job_id,
                "status": self.status,
                "strategy": self.strategy,
                "symbols": list(self.symbols),
                "interval": self.interval,
                "created_at": dt_to_iso(self.created_at),
                "progress": self.progress,
                "error": self.error,
            }

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "job_id": self.job_id,
                "status": self.status,
                "progress": self.progress,
                "error": self.error,
                "result": self.result,
                "strategy": self.strategy,
                "symbols": list(self.symbols),
                "interval": self.interval,
                "request": dict(self.request),
                "created_at": dt_to_iso(self.created_at),
                "started_at": dt_to_iso(self.started_at),
                "finished_at": dt_to_iso(self.finished_at),
            }


def _parse_request(config: AppConfig, body: Any) -> dict[str, Any]:
    """요청 본문 구조 검사 (타입만). 의미 검증(전략 존재 여부 등)은 작업 실행 시 한다."""
    if body is None:
        body = {}
    if not isinstance(body, Mapping):
        raise JobRequestError("요청 본문은 JSON 객체여야 합니다")
    req: dict[str, Any] = {}

    symbols = body.get("symbols")
    if symbols is not None:
        if isinstance(symbols, str):
            symbols = [s for s in symbols.split(",")]
        if not isinstance(symbols, list) or not all(isinstance(s, str) for s in symbols):
            raise JobRequestError("symbols 는 문자열 배열(또는 쉼표로 구분한 문자열)이어야 합니다")
        cleaned = [s.strip() for s in symbols if s and s.strip()]
        if len(cleaned) > MAX_SYMBOLS:
            raise JobRequestError(f"심볼은 최대 {MAX_SYMBOLS}개까지 지정할 수 있습니다")
        req["symbols"] = cleaned or None

    strategy = body.get("strategy")
    if strategy is not None:
        if not isinstance(strategy, str) or not strategy.strip():
            raise JobRequestError("strategy 는 비어 있지 않은 문자열이어야 합니다")
        req["strategy"] = strategy.strip()

    params = body.get("params")
    if params is not None:
        if not isinstance(params, Mapping):
            raise JobRequestError("params 는 객체({key: value})여야 합니다")
        req["params"] = {str(k): v for k, v in params.items()}

    interval = body.get("interval")
    if interval is not None:
        if not isinstance(interval, str):
            raise JobRequestError("interval 은 문자열이어야 합니다")
        if interval not in INTERVAL_SECONDS:
            raise JobRequestError(
                f"지원하지 않는 interval {interval!r} (가능: {', '.join(INTERVAL_SECONDS)})"
            )
        req["interval"] = interval

    for key in ("start", "end"):
        value = body.get(key)
        if value is not None:
            if not isinstance(value, str):
                raise JobRequestError(f"{key} 는 YYYY-MM-DD 문자열이어야 합니다")
            value = value.strip()
            if value:
                try:
                    to_utc_datetime(value, name=key)
                except DataError as e:
                    raise JobRequestError(str(e)) from e
                req[key] = value

    cash = body.get("initial_cash")
    if cash is not None:
        value = _num(cash)
        if value is None or value <= 0:
            raise JobRequestError("initial_cash 는 0보다 큰 숫자여야 합니다")
        req["initial_cash"] = value

    source = body.get("source") or "auto"
    if not isinstance(source, str) or source not in {s.value for s in Source}:
        raise JobRequestError(f"source 는 {', '.join(s.value for s in Source)} 중 하나여야 합니다")
    req["source"] = source

    fill_on = body.get("fill_on")
    if fill_on is not None:
        if fill_on not in ("next_open", "close"):
            raise JobRequestError("fill_on 은 next_open 또는 close 여야 합니다")
        req["fill_on"] = fill_on

    req.setdefault("symbols", None)
    req.setdefault("strategy", None)
    req.setdefault("params", None)
    req.setdefault("interval", None)
    return req


# ============================================================================ 실행기
class BacktestJobRunner:
    """작업 큐 + 단일 워커 스레드. ``broker_factory(config)`` 로 시세 브로커를 만든다 (테스트는 가짜 피드 주입)."""

    def __init__(
        self,
        config: AppConfig,
        *,
        broker_factory: Callable[[AppConfig], BaseBroker],
        max_jobs: int = MAX_JOBS,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.config = config
        self._broker_factory = broker_factory
        self._max_jobs = max(1, int(max_jobs))
        self._clock = clock
        self._lock = threading.Lock()
        self._jobs: OrderedDict[str, BacktestJob] = OrderedDict()
        self._queue: queue.Queue[BacktestJob | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._closed = False

    # ------------------------------------------------------------------ 공개 API
    def submit(self, body: Any) -> BacktestJob:
        req = _parse_request(self.config, body)
        job = BacktestJob(
            job_id=uuid.uuid4().hex[:12],
            request=req,
            created_at=ensure_utc(self._clock()),
            strategy=req.get("strategy") or self.config.strategy.name,
            symbols=list(req.get("symbols") or self.config.symbols),
            interval=req.get("interval") or self.config.interval,
        )
        with self._lock:
            if self._closed:
                raise JobRequestError("서버가 종료 중이라 작업을 받을 수 없습니다")
            self._jobs[job.job_id] = job
            while len(self._jobs) > self._max_jobs:
                old_id, old = next(iter(self._jobs.items()))
                if old.status in ("queued", "running") and len(self._jobs) <= self._max_jobs * 2:
                    break
                self._jobs.pop(old_id)
            self._ensure_worker()
        self._queue.put(job)
        return job

    def get(self, job_id: str) -> BacktestJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = list(self._jobs.values())
        return [j.summary() for j in reversed(jobs)]

    def close(self) -> None:
        with self._lock:
            self._closed = True
            worker = self._worker
        if worker is not None and worker.is_alive():
            self._queue.put(None)

    # ------------------------------------------------------------------ 워커
    def _ensure_worker(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(
                target=self._worker_loop, name="tradingbot-web-backtest", daemon=True
            )
            self._worker.start()

    def _worker_loop(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            self._run(job)

    def _run(self, job: BacktestJob) -> None:
        with job._lock:
            job.status = "running"
            job.started_at = ensure_utc(self._clock())
            job.progress = "준비 중"
        try:
            result = self._execute(job)
        except TradingBotError as e:
            self._fail(job, mask_secrets(str(e)) or type(e).__name__)
        except Exception as e:  # noqa: BLE001 - 워커가 죽지 않게 모든 오류를 작업 실패로
            logger.exception("백테스트 작업 %s 실패", job.job_id)
            self._fail(job, f"{type(e).__name__}: {mask_secrets(str(e))}")
        else:
            with job._lock:
                job.result = result
                job.status = "done"
                job.progress = "완료"
                job.finished_at = ensure_utc(self._clock())
            logger.info(
                "백테스트 작업 %s 완료: %s %s %s",
                job.job_id,
                job.strategy,
                ", ".join(job.symbols),
                job.interval,
            )

    def _fail(self, job: BacktestJob, message: str) -> None:
        with job._lock:
            job.status = "error"
            job.error = message
            job.progress = None
            job.finished_at = ensure_utc(self._clock())
        logger.warning("백테스트 작업 %s 오류: %s", job.job_id, message)

    # ------------------------------------------------------------------ 실행
    def _execute(self, job: BacktestJob) -> dict[str, Any]:
        req = job.request
        config = resolve_config(
            self.config,
            symbols=req.get("symbols"),
            interval=req.get("interval"),
            strategy=req.get("strategy"),
            params=req.get("params"),
            start=req.get("start"),
            end=req.get("end"),
            cash=req.get("initial_cash"),
            fill_on=req.get("fill_on"),
        )
        with job._lock:
            job.strategy = config.strategy.name
            job.symbols = list(config.symbols)
            job.interval = config.interval
        strategy = create_strategy(config.strategy.name, config.strategy.params)
        broker_name = _data_broker_name(config)
        start_dt = to_utc_datetime(config.backtest.start, name="start")
        end_dt = to_utc_datetime(config.backtest.end, name="end")
        if start_dt is not None and end_dt is not None and start_dt > end_dt:
            raise DataError(f"start({config.backtest.start}) 가 end({config.backtest.end}) 보다 늦습니다")

        store = CandleStore(config.backtest.data_dir)
        data, sources = self._load_data(
            job, config, store, Source(req.get("source") or "auto"), start_dt, end_dt
        )

        asset_class = _asset_class_for(broker_name)
        quote = _quote_currency_for(config.symbols[0], broker_name, config)
        bt = Backtester(
            strategy,
            RiskManager(config.risk),
            initial_cash=config.backtest.initial_cash,
            fee_pct=config.backtest.fee_pct,
            slippage_pct=config.backtest.slippage_pct,
            fill_on=config.backtest.fill_on,
            quote_currency=quote,
            interval=config.interval,
            asset_class=asset_class,
            min_order_value=config.risk.min_order_value,
            round_quantity=_stock_round_quantity if asset_class == AssetClass.STOCK else None,
        )
        job.set_progress("백테스트 실행 중")
        with _quiet_backtest_logs():
            result = bt.run(data)
        return result_to_dict(result, sources)

    def _load_data(
        self,
        job: BacktestJob,
        config: AppConfig,
        store: CandleStore,
        source: Source,
        start: datetime | None,
        end: datetime | None,
    ) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
        """``cli._load_backtest_data`` 와 같은 규칙. 브로커는 주입된 factory 로 만들고 진행 상황을 작업에 기록한다."""
        broker_name = _data_broker_name(config)
        interval = config.interval
        data: dict[str, pd.DataFrame] = {}
        used: dict[str, str] = {}
        broker: BaseBroker | None = None

        def get_broker() -> BaseBroker:
            nonlocal broker
            if broker is None:
                broker = self._broker_factory(config)
            return broker

        def needs_yfinance() -> bool:
            if not BROKER_INFO.get(broker_name, {}).get("data_needs_keys"):
                return False
            return _broker_has_credentials(get_broker()) is False

        try:
            for sym in config.symbols:
                chosen = source
                if source == Source.auto:
                    if _csv_coverage(store, broker_name, sym, interval, start, end) is not None:
                        chosen = Source.csv
                    elif needs_yfinance():
                        chosen = Source.yfinance
                    else:
                        chosen = Source.broker
                if chosen == Source.csv:
                    job.set_progress(f"{sym} 저장된 캔들 읽는 중")
                    df, where = _load_csv(store, broker_name, sym, interval, start, end)
                    used[sym] = f"csv ({store.path(where, sym, interval)})"
                elif chosen == Source.broker:
                    b = get_broker()
                    _require_data_credentials(b, broker_name)
                    dl_start = start or _default_start(interval)
                    job.set_progress(f"{sym} {interval} 캔들 다운로드 중 ({b.name})")

                    def on_page(count: int, oldest: datetime, _sym: str = sym) -> None:
                        job.set_progress(
                            f"{_sym} {interval} 다운로드: {count:,}개 (가장 오래된 {oldest:%Y-%m-%d %H:%M}Z)"
                        )

                    df = store.download(b, sym, interval, dl_start, end, progress=on_page)
                    if df.empty:
                        raise DataError(
                            f"{b.name} 에서 {sym} {interval} 구간의 캔들을 받지 못했습니다 (심볼/간격/기간을 확인하세요)"
                        )
                    used[sym] = f"{broker_name} API → {store.path(broker_name, sym, interval)}"
                else:
                    dl_start = start or _default_start(interval)
                    job.set_progress(f"{sym} yfinance 다운로드 중")
                    df, yf_sym = _load_yfinance_to_store(
                        store, config, broker_name, sym, interval, dl_start, end
                    )
                    used[sym] = f"yfinance {yf_sym}"
                if df.empty:
                    raise DataError(f"{sym} {interval}: 백테스트할 캔들이 없습니다")
                data[sym] = df
        finally:
            if broker is not None:
                try:
                    broker.close()
                except Exception as e:  # noqa: BLE001
                    logger.debug("브로커 종료 중 오류 (무시): %s", mask_secrets(str(e)))
        return data, used
