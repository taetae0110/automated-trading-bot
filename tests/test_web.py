"""웹 대시보드(FastAPI) 테스트.

시세는 전부 tests/conftest.py 가 Upbit 공개 API 에서 받아 캐시한 **실제** KRW-BTC 캔들(1h, 1d) 이다 (네트워크 없으면 skip).
``FeedBroker`` 는 그 실제 캔들을 서빙하는 시세 전용 브로커이며 ``tradingbot.web.service.create_broker`` 자리에
monkeypatch 된다. 가짜/데모 가격은 없다 — 계좌 설정값(초기 현금, 수수료율)만 상수다.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from tradingbot import __version__
from tradingbot.brokers.base import BaseBroker
from tradingbot.brokers.paper import PaperBroker
from tradingbot.config import AppConfig
from tradingbot.data import CandleStore
from tradingbot.engine.state import StateStore
from tradingbot.engine.trader import Trader
from tradingbot.exceptions import AuthenticationError, BrokerError
from tradingbot.models import (
    AssetClass,
    Candle,
    OrderType,
    Signal,
    SignalAction,
    ensure_utc,
    interval_to_seconds,
    utcnow,
)
from tradingbot.risk import RiskManager
from tradingbot.strategies.base import BaseStrategy
from tradingbot.web import service as web_service
from tradingbot.web.app import create_app
from tradingbot.web.service import (
    EQUITY_HISTORY_FILE,
    LIVE_START_MESSAGE,
    DashboardService,
    is_loopback_host,
    mask_log_line,
    tail_lines,
)

BTC = "KRW-BTC"
INITIAL_CASH = 10_000_000.0
FEE = 0.0005
SLIP = 0.0005
H = 3600

SECRET_KEY_RE = re.compile(r"(?i)key|secret|token|password|webhook")


# ============================================================================ 테스트 인프라
class FakeClock:
    def __init__(self, now: datetime | None = None) -> None:
        self.now = ensure_utc(now) if now is not None else None  # None 이면 실제 시각

    def __call__(self) -> datetime:
        return self.now if self.now is not None else utcnow()

    def advance(self, seconds: float) -> None:
        assert self.now is not None
        self.now = self.now + timedelta(seconds=seconds)


class FeedBroker(BaseBroker):
    """conftest 의 실제 Upbit 캔들을 서빙하는 시세 전용 브로커 (``create_broker`` 대체용).

    - ``get_candles``: 시계 기준으로 완성된 캔들만 (``include_partial`` 이면 진행중 캔들 포함), ``end`` 미만, 마지막 ``limit`` 개
    - ``get_ticker``: 진행중 실제 캔들의 종가, 없으면 마지막 완성 캔들 종가
    - 주문/잔고 없음 (PaperBroker 가 계좌 역할)
    """

    name = "upbit"
    asset_class = AssetClass.CRYPTO
    supported_intervals = ("1h", "1d")

    def __init__(self, candles: dict[tuple[str, str], list[Candle]], clock: FakeClock | None = None) -> None:
        self._candles = {k: sorted(v, key=lambda c: c.timestamp) for k, v in candles.items()}
        self._clock = clock or FakeClock()
        self.calls: Counter[str] = Counter()
        self.closed = False

    def _series(self, symbol: str, interval: str) -> list[Candle]:
        try:
            return self._candles[(symbol, interval)]
        except KeyError as e:
            raise BrokerError(f"feed 에 없는 심볼/간격: {symbol} {interval}") from e

    def get_candles(self, symbol, interval, limit=200, end=None, include_partial=False):
        self.calls["get_candles"] += 1
        now = self._clock()
        step = timedelta(seconds=interval_to_seconds(interval))
        series = self._series(symbol, interval)
        if include_partial:
            out = [c for c in series if c.timestamp <= now]
        else:
            out = [c for c in series if c.timestamp + step <= now]
        if end is not None:
            out = [c for c in out if c.timestamp < ensure_utc(end)]
        return out[-limit:]

    def get_ticker(self, symbol):
        self.calls["get_ticker"] += 1
        now = self._clock()
        newest: Candle | None = None
        for (sym, interval), series in self._candles.items():
            if sym != symbol:
                continue
            step = timedelta(seconds=interval_to_seconds(interval))
            for c in series:
                if c.timestamp <= now < c.timestamp + step:
                    return float(c.close)
            done = [c for c in series if c.timestamp + step <= now]
            if done and (newest is None or done[-1].timestamp > newest.timestamp):
                newest = done[-1]
        if newest is None:
            raise BrokerError(f"feed 에 없는 심볼: {symbol}")
        return float(newest.close)

    def get_balances(self):
        return {}

    def get_positions(self):
        return {}

    def place_order(self, symbol, side, quantity, order_type=OrderType.MARKET, price=None):
        raise AuthenticationError("시세 전용 feed 는 주문을 지원하지 않습니다")

    def cancel_order(self, order_id, symbol=None):
        raise AuthenticationError("시세 전용 feed 는 주문을 지원하지 않습니다")

    def get_order(self, order_id, symbol=None):
        raise AuthenticationError("시세 전용 feed 는 주문을 지원하지 않습니다")

    def get_open_orders(self, symbol=None):
        return []

    def quote_currency(self, symbol):
        return symbol.split("-", 1)[0]

    def base_currency(self, symbol):
        return symbol.split("-", 1)[1]

    def min_order_value(self, symbol):
        return 5000.0

    def close(self):
        self.closed = True


class QueuedStrategy(BaseStrategy):
    """마지막 행에서 미리 정한 신호를 순서대로 내는 테스트 전략 (시세가 아닌 테스트 로직)."""

    name = "queued"
    description = "테스트용"
    default_params: dict[str, Any] = {}

    def __init__(self, actions: list[SignalAction]) -> None:
        super().__init__()
        self.actions = list(actions)

    @property
    def warmup(self) -> int:
        return 1

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        return df.copy()

    def signal_at(self, symbol: str, df: pd.DataFrame, i: int) -> Signal:
        if i != len(df) - 1 or not self.actions:
            return Signal.hold(symbol, "대기")
        action = self.actions.pop(0)
        if action == SignalAction.HOLD:
            return Signal.hold(symbol, "대기")
        return Signal(action=action, symbol=symbol, reason=f"테스트 {action.value}")


def make_config(tmp_path: Path, **overrides: Any) -> AppConfig:
    base: dict[str, Any] = {
        "mode": "paper",
        "broker": {"name": "upbit", "sandbox": True, "fill_timeout_sec": 5},
        "symbols": [BTC],
        "interval": "1h",
        "strategy": {"name": "sma_cross", "params": {"fast": 5, "slow": 20}},
        "risk": {
            "max_position_pct": 0.2,
            "max_positions": 2,
            "stop_loss_pct": 0.03,
            "take_profit_pct": None,
            "max_daily_loss_pct": 0.05,
            "min_order_value": 0.0,
        },
        "paper": {
            "initial_cash": INITIAL_CASH,
            "quote_currency": "KRW",
            "fee_pct": FEE,
            "slippage_pct": SLIP,
        },
        "engine": {
            "poll_seconds": 1.0,
            "candle_limit": 60,
            "state_file": str(tmp_path / "data" / "state.json"),
            "stale_data_minutes": 30,
        },
        "notify": {"notify_on_trade": False, "notify_on_error": False, "daily_summary": False},
        "backtest": {
            "data_dir": str(tmp_path / "candles"),
            "start": None,
            "end": None,
            "initial_cash": INITIAL_CASH,
        },
        "logging": {"level": "INFO", "file": str(tmp_path / "logs" / "bot.log")},
    }
    data = _merge(base, overrides)
    return AppConfig.model_validate(data)


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def complete_candles(candles: list[Candle], interval: str) -> list[Candle]:
    """실제 시각 기준으로 완성된 캔들만 (Upbit 응답에는 진행중 캔들이 포함될 수 있다)."""
    step = timedelta(seconds=interval_to_seconds(interval))
    now = utcnow()
    return [c for c in candles if c.timestamp + step <= now]


def after_candle(candles: list[Candle], k: int, offset: float = 5.0) -> datetime:
    """캔들 k 가 막 완성된 시각 (k+1 이 진행중)."""
    return candles[k].timestamp + timedelta(seconds=H + offset)


def write_state_via_trader(
    config: AppConfig, candles: list[Candle], actions: list[SignalAction], k: int = 150
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """짧은 프로세스 내 Trader(PaperBroker + 실제 캔들 피드) 를 run_once 로 돌려 상태 파일을 만든다.

    각 ``actions`` 항목마다 시계를 1시간 전진시켜 새 캔들 이벤트를 만든다. (거래 dict 목록, 상태 dict) 반환.
    """
    clock = FakeClock(after_candle(candles, k))
    feed = FeedBroker({(BTC, "1h"): candles}, clock)
    broker = PaperBroker(
        initial_cash=config.paper.initial_cash,
        quote_currency=config.paper.quote_currency,
        fee_pct=config.paper.fee_pct,
        slippage_pct=config.paper.slippage_pct,
        data_source=feed,
        asset_class=feed.asset_class,
    )
    trader = Trader(
        config,
        broker,
        QueuedStrategy(actions),
        RiskManager(config.risk),
        None,
        StateStore(config.engine.state_file),
        clock,
    )
    for _ in actions:
        trader.run_once()
        clock.advance(H)
    state = StateStore(config.engine.state_file).load()
    return [t for t in state.get("trades", [])], state


def touch_state(config: AppConfig, when: datetime) -> None:
    """상태 파일의 updated_at 을 바꿔 '방금 갱신된(외부 엔진 실행 중)' / '오래된' 파일을 흉내낸다."""
    store = StateStore(config.engine.state_file)
    data = store.load()
    data["updated_at"] = ensure_utc(when).isoformat()
    store.save(data)


def poll_job(client: TestClient, job_id: str, timeout: float = 60.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = client.get(f"/api/backtest/{job_id}")
        assert r.status_code == 200, r.text
        body = r.json()
        if body["status"] in ("done", "error"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"백테스트 작업 {job_id} 이 {timeout}초 안에 끝나지 않았습니다")


def wait_until(predicate: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


# ============================================================================ 픽스처
@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """저장소의 .env 와 실제 키가 테스트에 섞이지 않게 한다."""
    for name in list(__import__("os").environ):
        if any(
            tag in name
            for tag in (
                "UPBIT_",
                "CCXT_",
                "BINANCE_",
                "KIS_",
                "ALPACA_",
                "APCA_",
                "TELEGRAM_",
                "SLACK_",
                "DISCORD_",
            )
        ):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("tradingbot.config.load_dotenv", lambda *a, **k: False)


@pytest.fixture
def feed_factory(
    monkeypatch: pytest.MonkeyPatch, candles: list[Candle], daily_candles: list[Candle]
) -> Callable[[], list[FeedBroker]]:
    """``tradingbot.web.service.create_broker`` 를 실제 캔들 피드로 바꾼다. 만들어진 피드 목록을 돌려준다."""
    made: list[FeedBroker] = []

    def fake_create(name: str, config: AppConfig) -> BaseBroker:
        assert name == "upbit"
        feed = FeedBroker({(BTC, "1h"): candles, (BTC, "1d"): daily_candles})
        made.append(feed)
        return feed

    monkeypatch.setattr(web_service, "create_broker", fake_create)
    return lambda: made


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    return make_config(tmp_path)


@pytest.fixture
def client(config: AppConfig, feed_factory: Any, tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(config, config_path=tmp_path / "config.yaml")
    with TestClient(app) as c:
        yield c


@pytest.fixture
def traded_config(config: AppConfig, candles: list[Candle]) -> AppConfig:
    """실제 캔들 가격으로 매수→매도→매수→매도→매수 를 돌린 상태 파일 (거래 2건, 포지션 1개)."""
    B, S = SignalAction.BUY, SignalAction.SELL
    trades, state = write_state_via_trader(config, candles, [B, S, B, S, B])
    assert len(trades) == 2 and len(state["positions"]) == 1
    return config


# ============================================================================ 기본 엔드포인트
class TestBasics:
    def test_health(self, client: TestClient) -> None:
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "version": __version__}

    def test_config_has_no_secret_like_keys(
        self, client: TestClient, tmp_path: Path, config: AppConfig
    ) -> None:
        r = client.get("/api/config")
        assert r.status_code == 200
        body = r.json()
        assert body["config_path"] == str(tmp_path / "config.yaml")
        cfg = body["config"]
        assert cfg["symbols"] == [BTC] and cfg["broker"]["name"] == "upbit"
        assert cfg == config.model_dump(mode="json")

        def keys(obj: Any) -> Iterator[str]:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    yield str(k)
                    yield from keys(v)
            elif isinstance(obj, list):
                for v in obj:
                    yield from keys(v)

        assert not [k for k in keys(cfg) if SECRET_KEY_RE.search(k)]

    def test_strategies(self, client: TestClient) -> None:
        r = client.get("/api/strategies")
        assert r.status_code == 200
        items = {s["name"]: s for s in r.json()["strategies"]}
        assert {"sma_cross", "ema_cross", "rsi", "bollinger", "macd", "volatility_breakout"} <= set(items)
        sma = items["sma_cross"]
        assert sma["default_params"] == {"fast": 10, "slow": 30}
        assert sma["description"] and sma["warmup"] >= 31

    def test_unknown_path_is_korean_json_error(self, client: TestClient) -> None:
        r = client.get("/api/nope")
        assert r.status_code == 404
        assert "error" in r.json()

    def test_index_serves_static_or_503(self, client: TestClient) -> None:
        r = client.get("/")
        assert r.status_code in (200, 503)
        if r.status_code == 503:
            assert "error" in r.json()
        else:
            assert "<html" in r.text.lower()


# ============================================================================ 상태: none → state_file → engine
class TestStatus:
    def test_none_without_state_file(self, client: TestClient) -> None:
        r = client.get("/api/status")
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "none" and body["external_running"] is False
        assert body["status"] is None and body["state_updated_at"] is None
        assert client.get("/api/positions").json()["positions"] == []
        assert client.get("/api/trades").json() == {"trades": [], "total": 0, "source": "none"}

    def test_state_file_after_trader_run_once(
        self, client: TestClient, traded_config: AppConfig, candles: list[Candle]
    ) -> None:
        r = client.get("/api/status")
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "state_file"
        # Trader 의 가짜 시계(실제 캔들 시각) 로 저장된 파일 → 갱신 시각이 오래돼 외부 실행으로 보지 않는다
        assert body["external_running"] is False
        state = StateStore(traded_config.engine.state_file).load()
        assert body["state_updated_at"] == state["updated_at"]
        st = body["status"]
        assert st["mode"] == "paper" and st["broker"] == "paper" and st["data_source"] == "upbit"
        assert st["strategy"] == "queued" and st["symbols"] == [BTC] and st["interval"] == "1h"
        assert st["quote_currency"] == "KRW" and st["cycles"] == 5 and st["day"]
        assert st["trades"] == 2 and st["running"] is False
        assert st["day_start_equity"] == pytest.approx(INITIAL_CASH)
        assert isinstance(st["daily_pnl"], float)
        # 자산 = 현금 + 수량 × 현재가 (현재가는 실제 캔들 피드)
        pos = st["positions"][BTC]
        assert pos["quantity"] > 0 and pos["average_price"] > 0
        assert pos["last_price"] == pytest.approx(candles[-1].close)
        assert pos["unrealized_pnl"] == pytest.approx(
            (pos["last_price"] - pos["average_price"]) * pos["quantity"]
        )
        assert pos["unrealized_pnl_pct"] == pytest.approx(pos["last_price"] / pos["average_price"] - 1)
        assert pos["stop_loss"] == pytest.approx(pos["average_price"] * 0.97)
        assert pos["entry_reason"] == "테스트 buy"
        assert st["cash"] > 0
        assert st["equity"] == pytest.approx(st["cash"] + pos["quantity"] * pos["last_price"])
        assert st["pending_breakouts"] == {}

    def test_fresh_state_file_is_external(self, client: TestClient, traded_config: AppConfig) -> None:
        touch_state(traded_config, utcnow())
        body = client.get("/api/status").json()
        assert body["source"] == "state_file" and body["external_running"] is True
        assert body["status"]["running"] is True
        # 신선도 창: max(3 × poll_seconds, 120초)
        touch_state(traded_config, utcnow() - timedelta(seconds=100))
        assert client.get("/api/status").json()["external_running"] is True
        touch_state(traded_config, utcnow() - timedelta(seconds=130))
        body = client.get("/api/status").json()
        assert body["external_running"] is False and body["status"]["running"] is False

    def test_engine_start_stop_lifecycle(self, client: TestClient, config: AppConfig) -> None:
        assert client.get("/api/status").json()["source"] == "none"
        r = client.post("/api/engine/start")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["source"] == "engine" and body["external_running"] is False
        st = body["status"]
        assert st["strategy"] == "sma_cross" and st["broker"] == "paper" and st["data_source"] == "upbit"
        assert st["started"] is True and st["equity"] == pytest.approx(INITIAL_CASH, rel=0.05)

        # 이미 실행 중 → 409
        r = client.post("/api/engine/start")
        assert r.status_code == 409 and "실행 중" in r.json()["error"]

        service: DashboardService = client.app.state.service
        assert wait_until(lambda: service._trader is not None and service._trader.cycles >= 1, timeout=15)
        assert client.get("/api/status").json()["source"] == "engine"
        assert client.get("/api/trades").json()["source"] == "engine"
        assert client.get("/api/positions").json()["source"] == "engine"

        r = client.post("/api/engine/stop")
        assert r.status_code == 200, r.text
        body = r.json()
        assert not service.engine_alive()
        # 정지 후: 상태 파일은 남고, 우리 엔진이 저장한 파일이므로 외부 실행으로 보지 않는다
        assert body["source"] == "state_file" and body["external_running"] is False
        assert body["status"]["cycles"] >= 1 and body["status"]["strategy"] == "sma_cross"
        assert Path(config.engine.state_file).is_file()

        r = client.post("/api/engine/stop")
        assert r.status_code == 409

        # 바로 다시 시작할 수 있어야 한다 (자기 상태 파일에 막히지 않음)
        r = client.post("/api/engine/start")
        assert r.status_code == 200, r.text
        assert r.json()["source"] == "engine"
        assert client.post("/api/engine/stop").status_code == 200

    def test_start_409_when_live_config(self, tmp_path: Path, feed_factory: Any) -> None:
        cfg = make_config(tmp_path, mode="live")
        with TestClient(create_app(cfg)) as c:
            r = c.post("/api/engine/start")
            assert r.status_code == 409
            assert r.json()["error"] == LIVE_START_MESSAGE
            assert c.get("/api/status").json()["source"] == "none"

    def test_start_409_when_external_running(self, client: TestClient, traded_config: AppConfig) -> None:
        # 방금 갱신된 상태 파일 = 다른 프로세스가 갱신 중인 것으로 간주
        touch_state(traded_config, utcnow())
        r = client.post("/api/engine/start")
        assert r.status_code == 409
        assert "외부" in r.json()["error"] or "다른 프로세스" in r.json()["error"]
        assert client.get("/api/status").json()["external_running"] is True

    def test_start_400_when_broker_is_paper(self, tmp_path: Path, feed_factory: Any) -> None:
        cfg = make_config(tmp_path, broker={"name": "paper"})
        with TestClient(create_app(cfg)) as c:
            r = c.post("/api/engine/start")
            assert r.status_code == 400 and "paper" in r.json()["error"]
            r = c.get("/api/candles")
            assert r.status_code == 400


# ============================================================================ 포지션 / 거래 / 시세 / 캔들
class TestMarketData:
    POSITION_KEYS = {
        "symbol",
        "quantity",
        "average_price",
        "last_price",
        "unrealized_pnl",
        "unrealized_pnl_pct",
        "stop_loss",
        "take_profit",
        "opened_at",
        "entry_reason",
    }
    TRADE_KEYS = {
        "symbol",
        "side",
        "quantity",
        "entry_price",
        "exit_price",
        "entry_time",
        "exit_time",
        "fee",
        "pnl",
        "pnl_pct",
        "reason",
    }

    def test_positions_shape(
        self, client: TestClient, traded_config: AppConfig, candles: list[Candle]
    ) -> None:
        r = client.get("/api/positions")
        assert r.status_code == 200
        positions = r.json()["positions"]
        assert len(positions) == 1
        p = positions[0]
        assert self.POSITION_KEYS <= set(p)
        assert p["symbol"] == BTC and p["quantity"] > 0
        assert p["last_price"] == pytest.approx(candles[-1].close)
        assert p["take_profit"] is None and p["stop_loss"] < p["average_price"]
        assert p["opened_at"].endswith("+00:00")

    def test_trades_shape_newest_first(self, client: TestClient, traded_config: AppConfig) -> None:
        r = client.get("/api/trades?limit=100")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 2 and len(body["trades"]) == 2
        first, second = body["trades"]
        assert self.TRADE_KEYS <= set(first)
        assert first["exit_time"] > second["exit_time"]  # 최신 먼저
        for t in (first, second):
            assert t["symbol"] == BTC and t["side"] == "buy" and t["quantity"] > 0
            assert t["fee"] > 0 and t["reason"]
            gross = (t["exit_price"] - t["entry_price"]) * t["quantity"]
            assert t["pnl"] == pytest.approx(gross - t["fee"])
            assert t["pnl_pct"] == pytest.approx(t["pnl"] / (t["entry_price"] * t["quantity"]))
        r = client.get("/api/trades?limit=1")
        assert len(r.json()["trades"]) == 1 and r.json()["total"] == 2
        assert client.get("/api/trades?limit=0").status_code == 400

    def test_prices_from_feed_with_cache(
        self, client: TestClient, feed_factory: Any, candles: list[Candle]
    ) -> None:
        r = client.get("/api/prices")
        assert r.status_code == 200
        body = r.json()
        assert body["prices"] == {BTC: pytest.approx(candles[-1].close)}
        assert datetime.fromisoformat(body["at"]).tzinfo is not None
        feeds = feed_factory()
        assert len(feeds) == 1
        calls = feeds[0].calls["get_ticker"]
        client.get("/api/prices")  # 5초 캐시 → 브로커 재호출 없음
        assert feeds[0].calls["get_ticker"] == calls

    def test_candles_shape_and_defaults(
        self, client: TestClient, candles: list[Candle], daily_candles
    ) -> None:
        r = client.get("/api/candles", params={"symbol": BTC, "interval": "1h", "limit": 50})
        assert r.status_code == 200
        body = r.json()
        assert body["symbol"] == BTC and body["interval"] == "1h"
        rows = body["candles"]
        assert len(rows) == 50
        assert set(rows[0]) == {"t", "o", "h", "l", "c", "v"}
        assert [c["t"] for c in rows] == sorted(c["t"] for c in rows)
        last = complete_candles(candles, "1h")[-1]
        assert rows[-1] == {
            "t": last.timestamp.isoformat(),
            "o": last.open,
            "h": last.high,
            "l": last.low,
            "c": last.close,
            "v": last.volume,
        }
        # 기본값: 설정의 첫 심볼 / interval
        r = client.get("/api/candles")
        assert r.status_code == 200 and r.json()["interval"] == "1h" and r.json()["symbol"] == BTC
        r = client.get("/api/candles", params={"interval": "1d", "limit": 3})
        assert r.status_code == 200 and len(r.json()["candles"]) == 3
        assert r.json()["candles"][-1]["c"] == complete_candles(daily_candles, "1d")[-1].close

    @pytest.mark.parametrize(
        "params",
        [
            {"symbol": BTC, "interval": "2h"},
            {"symbol": BTC, "interval": "nope"},
            {"symbol": "??", "interval": "1h"},
            {"symbol": "KRW-NOPE", "interval": "1h"},
            {"symbol": BTC, "interval": "1h", "limit": 0},
            {"symbol": BTC, "interval": "1h", "limit": "abc"},
        ],
    )
    def test_candles_400(self, client: TestClient, params: dict[str, Any]) -> None:
        r = client.get("/api/candles", params=params)
        assert r.status_code == 400, r.text
        assert r.json()["error"]


# ============================================================================ 자산 이력
class TestEquity:
    def test_status_samples_once_per_minute(
        self, client: TestClient, traded_config: AppConfig, tmp_path: Path
    ) -> None:
        path = Path(traded_config.engine.state_file).parent / EQUITY_HISTORY_FILE
        assert client.get("/api/equity").json()["points"] == []
        first = client.get("/api/status").json()["status"]["equity"]
        client.get("/api/status")
        client.get("/api/positions")  # 내부적으로 status → 역시 샘플 안 함
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        point = json.loads(lines[0])
        assert point["equity"] == pytest.approx(first)
        r = client.get("/api/equity?limit=500")
        assert r.status_code == 200
        assert r.json()["points"] == [{"t": point["t"], "equity": pytest.approx(first)}]

    def test_no_sample_when_equity_unknown(self, client: TestClient, config: AppConfig) -> None:
        client.get("/api/status")  # source none → 자산 모름
        assert not (Path(config.engine.state_file).parent / EQUITY_HISTORY_FILE).exists()

    def test_sampler_rate_limit_and_trim(self, tmp_path: Path, feed_factory: Any) -> None:
        cfg = make_config(tmp_path)
        svc = DashboardService(cfg)
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert svc.sample_equity(100.0, t0) is True
        assert svc.sample_equity(101.0, t0 + timedelta(seconds=59)) is False
        assert svc.sample_equity(102.0, t0 + timedelta(seconds=60)) is True
        assert svc.sample_equity(float("nan"), t0 + timedelta(seconds=200)) is False
        assert svc.sample_equity(None, t0 + timedelta(seconds=200)) is False
        points = svc.equity_history(10)["points"]
        assert [p["equity"] for p in points] == [100.0, 102.0]
        # 상한 초과 시 최근 EQUITY_MAX_LINES 줄만 남긴다
        svc._equity_lines = web_service.EQUITY_TRIM_AT
        with open(svc.equity_path, "a", encoding="utf-8") as f:
            for i in range(web_service.EQUITY_TRIM_AT - 2):
                f.write(
                    json.dumps({"t": (t0 + timedelta(minutes=10 + i)).isoformat(), "equity": float(i)}) + "\n"
                )
        assert svc.sample_equity(999.0, t0 + timedelta(days=1)) is True
        n = sum(1 for _ in open(svc.equity_path, encoding="utf-8"))
        assert n == web_service.EQUITY_MAX_LINES
        assert svc.equity_history(1)["points"][-1]["equity"] == 999.0
        # 새 서비스는 파일에서 마지막 샘플 시각을 복원한다
        svc2 = DashboardService(cfg)
        assert svc2.sample_equity(5.0, t0 + timedelta(days=1, seconds=30)) is False
        svc.close()
        svc2.close()


# ============================================================================ 로그
class TestLogs:
    def test_tail_and_masking(self, client: TestClient, config: AppConfig) -> None:
        log = Path(config.logging.file)
        log.parent.mkdir(parents=True)
        secret = "AbCdEf0123456789AbCdEf0123456789AbCdEf01"
        lines = [f"줄 {i}" for i in range(10)] + [
            f"2026-01-01 00:00:00+0000 INFO tradingbot: access_key={secret} 로 로그인",
            "2026-01-01 00:00:01+0000 WARNING tradingbot.utils.http: POST https://api.telegram.org/bot123456:ABC-def_GHI/sendMessage 재시도",
            "2026-01-01 00:00:02+0000 ERROR x: Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
            "2026-01-01 00:00:03+0000 INFO n: webhook https://hooks.slack.com/services/T000/B000/XXXXXXXX 전송",
            "2026-01-01 00:00:04+0000 INFO tradingbot.engine.trader: KRW-BTC 새 캔들 2026-10-09T15:00:00+00:00 종가 113,200,000",
        ]
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        r = client.get("/api/logs?lines=5")
        assert r.status_code == 200
        body = r.json()
        assert body["file"] == str(log)
        out = body["lines"]
        assert len(out) == 5
        joined = "\n".join(out)
        assert secret not in joined and "123456:ABC-def_GHI" not in joined
        assert "eyJhbGciOiJIUzI1NiJ9" not in joined and "T000/B000" not in joined
        assert "access_key=***" in out[0] and "bot***" in out[1]
        assert "Bearer ***" in out[2] and "services/***" in out[3]
        # 일반 로그(가격/시각)는 그대로
        assert out[4].endswith("종가 113,200,000") and "2026-10-09T15:00:00+00:00" in out[4]
        r = client.get("/api/logs?lines=3")
        assert [ln[:10] for ln in r.json()["lines"]] == ["2026-01-01"] * 3

    def test_missing_log_file(self, client: TestClient, config: AppConfig) -> None:
        body = client.get("/api/logs").json()
        assert body["file"] == config.logging.file and body["lines"] == []

    def test_no_log_file_configured(self, tmp_path: Path, feed_factory: Any) -> None:
        cfg = make_config(tmp_path, logging={"file": None})
        with TestClient(create_app(cfg)) as c:
            body = c.get("/api/logs").json()
            assert body["file"] is None and body["lines"] == []

    def test_mask_log_line_units(self) -> None:
        assert mask_log_line("UPBIT_SECRET_KEY: abcdef1234") == "UPBIT_SECRET_KEY: ***"
        assert mask_log_line('token="xyz-123456"') == 'token="***"'
        assert (
            mask_log_line("https://discord.com/api/webhooks/1/abc") == "https://discord.com/api/webhooks/***"
        )
        assert (
            mask_log_line("KRW-BTC 113,050,000 2026-10-09T15:00:00+00:00")
            == "KRW-BTC 113,050,000 2026-10-09T15:00:00+00:00"
        )
        assert mask_log_line("상태 파일 data/state.json 저장") == "상태 파일 data/state.json 저장"
        # 숫자만/문자만 긴 문자열은 토큰으로 보지 않는다
        assert mask_log_line("x" * 40) == "x" * 40 and mask_log_line("1" * 40) == "1" * 40
        assert mask_log_line("a1" * 20) == "***"

    def test_tail_lines(self, tmp_path: Path) -> None:
        p = tmp_path / "t.log"
        assert tail_lines(p, 3) == []
        p.write_text("", encoding="utf-8")
        assert tail_lines(p, 3) == []
        p.write_text("\n".join(f"line{i}" for i in range(1000)) + "\n", encoding="utf-8")
        assert tail_lines(p, 3) == ["line997", "line998", "line999"]
        assert tail_lines(p, 2000)[0] == "line0"
        assert tail_lines(p, 3, block_size=7) == ["line997", "line998", "line999"]
        assert tail_lines(p, 0) == []


# ============================================================================ 백테스트 작업
class TestBacktest:
    RESULT_KEYS = {
        "summary",
        "metrics",
        "equity",
        "trades",
        "orders",
        "start",
        "end",
        "initial_cash",
        "final_equity",
        "strategy",
        "params",
        "symbols",
        "interval",
    }

    def test_job_done_from_csv(self, client: TestClient, config: AppConfig, daily_df: pd.DataFrame) -> None:
        store = CandleStore(config.backtest.data_dir)
        store.save("upbit", BTC, "1d", daily_df)
        r = client.post(
            "/api/backtest",
            json={
                "strategy": "sma_cross",
                "params": {"fast": 5, "slow": 20},
                "interval": "1d",
                "source": "csv",
            },
        )
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]
        assert r.json()["status"] in ("queued", "running")
        body = poll_job(client, job_id)
        assert body["status"] == "done", body
        assert body["error"] is None and body["job_id"] == job_id
        res = body["result"]
        assert self.RESULT_KEYS <= set(res)
        assert res["strategy"] == "sma_cross" and res["params"] == {"fast": 5, "slow": 20}
        assert res["symbols"] == [BTC] and res["interval"] == "1d"
        assert res["initial_cash"] == INITIAL_CASH and res["final_equity"] > 0
        assert "총 수익률" in res["summary"]
        assert set(res["metrics"]) >= {"total_return", "max_drawdown", "num_trades", "sharpe", "win_rate"}
        assert res["metrics"]["num_trades"] == len(res["trades"])
        assert isinstance(res["orders"], int)
        eq = res["equity"]
        assert len(eq) == len(daily_df) and set(eq[0]) == {"t", "equity"}
        assert eq[0]["equity"] == pytest.approx(INITIAL_CASH, rel=0.01)
        assert res["start"] == daily_df["timestamp"].iloc[0].isoformat()
        assert res["end"] == daily_df["timestamp"].iloc[-1].isoformat()
        for t in res["trades"]:
            assert TestMarketData.TRADE_KEYS <= set(t)
        assert BTC in res["sources"] and res["sources"][BTC].startswith("csv")
        # 목록
        jobs = client.get("/api/backtest").json()["jobs"]
        assert jobs[0]["job_id"] == job_id and jobs[0]["status"] == "done"
        assert jobs[0]["strategy"] == "sma_cross" and jobs[0]["symbols"] == [BTC] and jobs[0]["created_at"]

    def test_job_downloads_from_broker_feed(
        self, client: TestClient, config: AppConfig, feed_factory: Any, daily_candles: list[Candle]
    ) -> None:
        r = client.post(
            "/api/backtest",
            json={
                "strategy": "rsi",
                "interval": "1d",
                "symbols": [BTC],
                "source": "auto",
                "initial_cash": 5_000_000,
            },
        )
        assert r.status_code == 202
        body = poll_job(client, r.json()["job_id"])
        assert body["status"] == "done", body
        res = body["result"]
        assert res["strategy"] == "rsi" and res["initial_cash"] == 5_000_000
        assert res["sources"][BTC].startswith("upbit API")
        assert len(res["equity"]) == len(complete_candles(daily_candles, "1d"))
        assert any(f.calls["get_candles"] for f in feed_factory())
        # 캐시 CSV 가 만들어졌다
        assert CandleStore(config.backtest.data_dir).exists("upbit", BTC, "1d")

    def test_job_error_on_bad_strategy(self, client: TestClient) -> None:
        r = client.post("/api/backtest", json={"strategy": "no_such_strategy", "source": "csv"})
        assert r.status_code == 202
        body = poll_job(client, r.json()["job_id"])
        assert body["status"] == "error" and body["result"] is None
        assert "no_such_strategy" in body["error"] and "전략" in body["error"]
        jobs = client.get("/api/backtest").json()["jobs"]
        assert jobs[0]["status"] == "error" and jobs[0]["error"] == body["error"]

    def test_job_error_on_bad_params_and_missing_data(self, client: TestClient) -> None:
        r = client.post(
            "/api/backtest", json={"strategy": "sma_cross", "params": {"nope": 1}, "source": "csv"}
        )
        body = poll_job(client, r.json()["job_id"])
        assert body["status"] == "error" and "파라미터" in body["error"]
        r = client.post("/api/backtest", json={"source": "csv"})  # 저장된 CSV 없음 → 데이터 오류
        body = poll_job(client, r.json()["job_id"])
        assert body["status"] == "error" and "저장된 데이터가 없습니다" in body["error"]

    @pytest.mark.parametrize(
        "body",
        [
            {"symbols": 5},
            {"symbols": [1, 2]},
            {"strategy": ""},
            {"params": [1]},
            {"interval": "2h"},
            {"start": "2025/01/01"},
            {"start": 20250101},
            {"initial_cash": -1},
            {"initial_cash": "abc"},
            {"source": "magic"},
            {"fill_on": "later"},
            [1, 2],
        ],
    )
    def test_submit_400_on_bad_body(self, client: TestClient, body: Any) -> None:
        r = client.post("/api/backtest", json=body)
        assert r.status_code == 400, r.text
        assert r.json()["error"]
        assert client.get("/api/backtest").json()["jobs"] == []

    def test_submit_malformed_json(self, client: TestClient) -> None:
        r = client.post("/api/backtest", content=b"{not json", headers={"content-type": "application/json"})
        assert r.status_code == 400 and "JSON" in r.json()["error"]

    def test_unknown_job_404(self, client: TestClient) -> None:
        r = client.get("/api/backtest/doesnotexist")
        assert r.status_code == 404 and "error" in r.json()

    def test_keeps_last_20_jobs(self, client: TestClient) -> None:
        ids = []
        for _ in range(23):
            r = client.post("/api/backtest", json={"strategy": "nope", "source": "csv"})
            ids.append(r.json()["job_id"])
        for job_id in ids[-3:]:
            poll_job(client, job_id)
        jobs = client.get("/api/backtest").json()["jobs"]
        assert len(jobs) == 20
        assert [j["job_id"] for j in jobs][:3] == ids[-1:-4:-1]
        assert client.get(f"/api/backtest/{ids[0]}").status_code == 404


# ============================================================================ CLI / 유틸
class TestCli:
    def test_module_help(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "tradingbot.web", "--help"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert "--host" in proc.stdout and "--port" in proc.stdout and "--no-open" in proc.stdout
        assert "--config" in proc.stdout

    def test_is_loopback(self) -> None:
        assert is_loopback_host("127.0.0.1") and is_loopback_host("localhost") and is_loopback_host("::1")
        assert is_loopback_host("127.5.5.5")
        assert (
            not is_loopback_host("0.0.0.0")
            and not is_loopback_host("192.168.0.2")
            and not is_loopback_host("")
        )

    def test_web_command_warns_when_not_loopback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import yaml

        from tradingbot.web import cli as web_cli

        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(
            yaml.safe_dump(
                {
                    "symbols": [BTC],
                    "engine": {"state_file": str(tmp_path / "state.json")},
                    "logging": {"file": None},
                    "backtest": {"data_dir": str(tmp_path / "c")},
                }
            ),
            encoding="utf-8",
        )
        runs: list[dict[str, Any]] = []

        class FakeUvicorn:
            @staticmethod
            def run(app: Any, **kw: Any) -> None:
                runs.append({"app": app, **kw})

        monkeypatch.setitem(sys.modules, "uvicorn", FakeUvicorn)
        monkeypatch.setattr("tradingbot.web.cli.setup_logging", lambda *a, **k: None)
        with caplog.at_level("WARNING"):
            web_cli.web(config_path=cfg_path, host="0.0.0.0", port=8123, no_open=True)
        assert runs and runs[0]["host"] == "0.0.0.0" and runs[0]["port"] == 8123
        assert runs[0]["log_config"] is None
        assert any("인증" in rec.getMessage() for rec in caplog.records)
        runs[0]["app"].state.service.close()
        caplog.clear()
        with caplog.at_level("WARNING"):
            web_cli.web(config_path=cfg_path, host="127.0.0.1", port=8124, no_open=True)
        assert not any("인증" in rec.getMessage() for rec in caplog.records)
        runs[1]["app"].state.service.close()
