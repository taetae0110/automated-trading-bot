"""CCXTBroker 테스트.

- 네트워크 호출 없음: ccxt 통합 API 와 같은 메서드 이름을 가진 ``FakeExchange`` 를 ``exchange=`` 로 주입한다.
- 시세(OHLCV/ticker)는 conftest 의 **실제 Upbit 캔들** 을 ccxt 형식 ``[ts_ms, o, h, l, c, v]`` 로 바꿔 쓴다.
- 비공개 API(잔고/주문) 응답은 ccxt 통합 구조(https://docs.ccxt.com, order/balance structure) 를 그대로 따르는
  모킹 데이터이며 이 파일 안에만 둔다. 마켓 메타데이터(정밀도/최소주문)는 Binance BTCUSDT/ETHUSDT 필터 값이다.
"""

from __future__ import annotations

import importlib
import math
import types
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any

import ccxt
import pytest
from freezegun import freeze_time

from tradingbot.brokers import create_broker, get_broker_class
from tradingbot.brokers.ccxt_broker import (
    CCXT_INSTALL_HINT,
    DEFAULT_QUOTE_CURRENCIES,
    TIMEFRAMES,
    CCXTBroker,
    split_symbol,
    to_ccxt_timeframe,
    translate_ccxt_error,
)
from tradingbot.config import AppConfig, BrokerConfig
from tradingbot.exceptions import (
    AuthenticationError,
    BrokerError,
    ConfigError,
    InsufficientFunds,
    OrderError,
    RateLimitError,
)
from tradingbot.models import Candle, OrderSide, OrderStatus, OrderType

# ---------------------------------------------------------------------------
# 마켓 메타데이터 (ccxt market structure, Binance 현물 필터 값)
# ---------------------------------------------------------------------------
MARKETS: dict[str, dict[str, Any]] = {
    "BTC/USDT": {
        "id": "BTCUSDT",
        "symbol": "BTC/USDT",
        "base": "BTC",
        "quote": "USDT",
        "active": True,
        "type": "spot",
        "spot": True,
        "precision": {"amount": 0.00001, "price": 0.01},
        "limits": {
            "amount": {"min": 0.00001, "max": 9000.0},
            "price": {"min": 0.01, "max": 1000000.0},
            "cost": {"min": 5.0, "max": 9000000.0},
        },
        "info": {},
    },
    "ETH/USDT": {
        "id": "ETHUSDT",
        "symbol": "ETH/USDT",
        "base": "ETH",
        "quote": "USDT",
        "active": True,
        "type": "spot",
        "spot": True,
        "precision": {"amount": 0.0001, "price": 0.01},
        "limits": {
            "amount": {"min": 0.0001, "max": 9000.0},
            "price": {"min": 0.01, "max": 1000000.0},
            "cost": {"min": 5.0, "max": 9000000.0},
        },
        "info": {},
    },
    "ETH/BTC": {
        "id": "ETHBTC",
        "symbol": "ETH/BTC",
        "base": "ETH",
        "quote": "BTC",
        "active": True,
        "type": "spot",
        "spot": True,
        "precision": {"amount": 0.0001, "price": 0.00001},
        "limits": {
            "amount": {"min": 0.0001, "max": 9000.0},
            "price": {"min": 0.00001, "max": 1000.0},
            "cost": {"min": 0.0001, "max": 1000.0},
        },
        "info": {},
    },
}

SYMBOL = "BTC/USDT"
API_KEY = "test-api-key"
SECRET = "test-secret"


def candles_to_ohlcv(candles: list[Candle]) -> list[list[float]]:
    """실제 Candle → ccxt fetch_ohlcv 행 형식."""
    return [
        [int(c.timestamp.timestamp() * 1000), c.open, c.high, c.low, c.close, c.volume]
        for c in sorted(candles, key=lambda c: c.timestamp)
    ]


class FakeExchange:
    """ccxt 통합 API 모양의 최소 가짜 거래소 (동기). 응답 구조는 ccxt 문서의 통합 구조를 따른다."""

    def __init__(
        self,
        ohlcv: list[list[float]],
        *,
        exchange_id: str = "binance",
        markets: dict[str, dict[str, Any]] | None = None,
        balance: dict[str, Any] | None = None,
        batch_cap: int | None = None,
        options: dict[str, Any] | None = None,
        sandbox_error: Exception | None = None,
    ) -> None:
        self.id = exchange_id
        self.options: dict[str, Any] = dict(options or {})
        self.ohlcv = sorted(ohlcv, key=lambda r: r[0])
        self._markets_data = markets if markets is not None else MARKETS
        self.markets: dict[str, Any] | None = None
        self.balance = balance
        self.batch_cap = batch_cap
        self.sandbox_error = sandbox_error
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.sandbox_calls: list[bool] = []
        self.orders: dict[str, dict[str, Any]] = {}
        self.fail: dict[str, Exception] = {}
        self.load_markets_count = 0
        self.closed = False
        self._seq = 0

    # -- 내부 --------------------------------------------------------------
    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))
        if name in self.fail:
            raise self.fail[name]

    def _market(self, symbol: str) -> dict[str, Any]:
        if self.markets is None:  # 실제 ccxt 처럼 필요 시 마켓을 자동 로드
            self.load_markets()
        assert self.markets is not None
        if symbol not in self.markets:
            raise ccxt.BadSymbol(f"{self.id} does not have market symbol {symbol}")
        return self.markets[symbol]

    @property
    def last_close(self) -> float:
        return float(self.ohlcv[-1][4])

    # -- ccxt 통합 API ------------------------------------------------------
    def set_sandbox_mode(self, enabled: bool) -> None:
        if self.sandbox_error is not None:
            raise self.sandbox_error
        self.sandbox_calls.append(enabled)

    def load_markets(self, reload: bool = False, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if "load_markets" in self.fail:
            raise self.fail["load_markets"]
        self.load_markets_count += 1  # calls 에는 기록하지 않는다 (내부 자동 로드와 구분)
        self.markets = {k: dict(v) for k, v in self._markets_data.items()}
        return self.markets

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1m",
        since: int | None = None,
        limit: int | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[list[float]]:
        self._record("fetch_ohlcv", symbol, timeframe, since, limit)
        self._market(symbol)
        rows = [r for r in self.ohlcv if since is None or r[0] >= since]
        cap = limit or 500
        if self.batch_cap is not None:
            cap = min(cap, self.batch_cap)
        return [list(r) for r in rows[:cap]]

    def fetch_ticker(self, symbol: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._record("fetch_ticker", symbol)
        self._market(symbol)
        last = self.last_close
        return {
            "symbol": symbol,
            "timestamp": int(self.ohlcv[-1][0]),
            "datetime": None,
            "high": float(self.ohlcv[-1][2]),
            "low": float(self.ohlcv[-1][3]),
            "bid": None,
            "ask": None,
            "open": float(self.ohlcv[-1][1]),
            "close": last,
            "last": last,
            "baseVolume": float(self.ohlcv[-1][5]),
            "quoteVolume": None,
            "info": {},
        }

    def fetch_balance(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._record("fetch_balance")
        if self.balance is None:
            raise ccxt.AuthenticationError(f"{self.id} requires apiKey credential")
        return self.balance

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        step = Decimal(str(self._market(symbol)["precision"]["amount"]))
        result = (Decimal(str(amount)) / step).to_integral_value(rounding=ROUND_DOWN) * step
        if result == 0:
            raise ccxt.InvalidOrder(
                f"{self.id} amount of {symbol} must be greater than minimum amount precision of {step}"
            )
        return format(result.normalize(), "f")

    def price_to_precision(self, symbol: str, price: float) -> str:
        step = Decimal(str(self._market(symbol)["precision"]["price"]))
        result = (Decimal(str(price)) / step).to_integral_value(rounding=ROUND_HALF_UP) * step
        return format(result.normalize(), "f")

    def create_order(
        self,
        symbol: str,
        type: str,  # noqa: A002 - ccxt 시그니처와 동일하게
        side: str,
        amount: float,
        price: float | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._record("create_order", symbol, type, side, amount, price, dict(params or {}))
        market = self._market(symbol)
        self._seq += 1
        order_id = str(1000 + self._seq)
        ts = int(self.ohlcv[-1][0])
        if type == "market":
            fill_price = self.last_close
            quote_qty = (params or {}).get("quoteOrderQty")
            filled = float(quote_qty) / fill_price if quote_qty is not None else float(amount)
            order = {
                "id": order_id,
                "clientOrderId": f"x-{order_id}",
                "timestamp": ts,
                "datetime": None,
                "lastTradeTimestamp": ts,
                "lastUpdateTimestamp": ts,
                "status": "closed",
                "symbol": symbol,
                "type": "market",
                "timeInForce": "GTC",
                "postOnly": False,
                "side": side,
                "price": None,
                "triggerPrice": None,
                "average": fill_price,
                "amount": filled,
                "filled": filled,
                "remaining": 0.0,
                "cost": filled * fill_price,
                "trades": [],
                "fee": {"cost": filled * fill_price * 0.001, "currency": market["quote"]},
                "fees": [{"cost": filled * fill_price * 0.001, "currency": market["quote"]}],
                "info": {},
            }
        else:
            order = {
                "id": order_id,
                "clientOrderId": f"x-{order_id}",
                "timestamp": ts,
                "datetime": None,
                "lastTradeTimestamp": None,
                "lastUpdateTimestamp": ts,
                "status": "open",
                "symbol": symbol,
                "type": "limit",
                "timeInForce": "GTC",
                "postOnly": False,
                "side": side,
                "price": float(price),
                "triggerPrice": None,
                "average": None,
                "amount": float(amount),
                "filled": 0.0,
                "remaining": float(amount),
                "cost": 0.0,
                "trades": [],
                "fee": None,
                "fees": [],
                "info": {},
            }
        self.orders[order_id] = order
        return dict(order)

    def cancel_order(
        self, id: str, symbol: str | None = None, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:  # noqa: A002
        self._record("cancel_order", id, symbol)
        order = self.orders.get(id)
        if order is None:
            raise ccxt.OrderNotFound(f"{self.id} order {id} not found")
        if order["status"] != "open":
            raise ccxt.OrderNotFound(f"{self.id} Unknown order sent.")
        order["status"] = "canceled"
        return dict(order)

    def fetch_order(
        self, id: str, symbol: str | None = None, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:  # noqa: A002
        self._record("fetch_order", id, symbol)
        order = self.orders.get(id)
        if order is None:
            raise ccxt.OrderNotFound(f"{self.id} order {id} not found")
        return dict(order)

    def fetch_open_orders(
        self,
        symbol: str | None = None,
        since: int | None = None,
        limit: int | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        self._record("fetch_open_orders", symbol)
        return [
            dict(o)
            for o in self.orders.values()
            if o["status"] == "open" and (symbol is None or o["symbol"] == symbol)
        ]

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------
@pytest.fixture
def ohlcv(candles: list[Candle]) -> list[list[float]]:
    return candles_to_ohlcv(candles)


@pytest.fixture
def fake(ohlcv: list[list[float]]) -> FakeExchange:
    return FakeExchange(ohlcv)


@pytest.fixture
def broker(fake: FakeExchange) -> CCXTBroker:
    return CCXTBroker("binance", API_KEY, SECRET, exchange=fake)


@pytest.fixture
def public_broker(fake: FakeExchange) -> CCXTBroker:
    return CCXTBroker("binance", exchange=fake)


def last_ts(ohlcv: list[list[float]]) -> datetime:
    return datetime.fromtimestamp(ohlcv[-1][0] / 1000, tz=timezone.utc)


# ---------------------------------------------------------------------------
# 레지스트리 / 생성
# ---------------------------------------------------------------------------
class TestConstruction:
    def test_registered_under_both_names(self) -> None:
        assert get_broker_class("binance") is CCXTBroker
        assert get_broker_class("ccxt") is CCXTBroker
        assert CCXTBroker.name == "ccxt"

    def test_injected_exchange_no_ccxt_import(
        self, fake: FakeExchange, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(name: str, *a: Any, **k: Any) -> Any:
            raise AssertionError(f"import_module({name}) 호출되면 안 됨")

        monkeypatch.setattr(importlib, "import_module", boom)
        b = CCXTBroker("binance", exchange=fake)
        assert b.exchange is fake
        assert b.exchange_id == "binance"
        assert not b.has_credentials

    def test_missing_ccxt_raises_config_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_import = importlib.import_module

        def fake_import(name: str, *a: Any, **k: Any) -> Any:
            if name == "ccxt":
                raise ImportError("No module named 'ccxt'")
            return real_import(name, *a, **k)

        monkeypatch.setattr(importlib, "import_module", fake_import)
        with pytest.raises(ConfigError) as ei:
            CCXTBroker("binance", API_KEY, SECRET)
        assert "pip install tradingbot[ccxt]" in str(ei.value)
        assert CCXT_INSTALL_HINT in str(ei.value)

    def test_creates_exchange_with_expected_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        created: list[dict[str, Any]] = []

        class StubBinance:
            def __init__(self, config: dict[str, Any]) -> None:
                created.append(config)
                self.options = dict(config.get("options") or {})

            def set_sandbox_mode(self, enabled: bool) -> None:
                self.options["sandboxMode"] = enabled

        stub_ccxt = types.SimpleNamespace(binance=StubBinance, exchanges=["binance", "bybit"])
        real_import = importlib.import_module
        monkeypatch.setattr(
            importlib,
            "import_module",
            lambda name, *a, **k: stub_ccxt if name == "ccxt" else real_import(name, *a, **k),
        )

        b = CCXTBroker("Binance", API_KEY, SECRET, "pass", sandbox=True, options={"defaultType": "spot"})
        assert created == [
            {
                "enableRateLimit": True,
                "apiKey": API_KEY,
                "secret": SECRET,
                "password": "pass",
                "options": {"defaultType": "spot"},
            }
        ]
        assert b.exchange.options["sandboxMode"] is True
        assert b.has_credentials

        # 키 없이 생성 가능 (공개 시세용)
        CCXTBroker("binance")
        assert created[-1] == {"enableRateLimit": True}

        with pytest.raises(ConfigError):
            CCXTBroker("nonexistent-exchange")
        with pytest.raises(ConfigError):
            CCXTBroker("")

    def test_sandbox_mode_guarded(self, ohlcv: list[list[float]]) -> None:
        ok = FakeExchange(ohlcv)
        CCXTBroker("binance", exchange=ok, sandbox=True)
        assert ok.sandbox_calls == [True]

        unsupported = FakeExchange(
            ohlcv, sandbox_error=ccxt.NotSupported("bithumb does not have a sandbox URL")
        )
        b = CCXTBroker("bithumb", exchange=unsupported, sandbox=True)  # 예외 없이 경고만
        assert b.sandbox is True
        assert unsupported.sandbox_calls == []

        no_sandbox = FakeExchange(ohlcv)
        CCXTBroker("binance", exchange=no_sandbox, sandbox=False)
        assert no_sandbox.sandbox_calls == []

    def test_options_merged_into_injected_exchange(self, ohlcv: list[list[float]]) -> None:
        ex = FakeExchange(ohlcv, options={"a": 1})
        CCXTBroker("binance", exchange=ex, options={"b": 2})
        assert ex.options == {"a": 1, "b": 2}

    def test_close_delegates(self, broker: CCXTBroker, fake: FakeExchange) -> None:
        broker.close()
        assert fake.closed is True


class TestFromConfig:
    @pytest.fixture
    def stub_ccxt(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
        created: list[tuple[str, dict[str, Any]]] = []

        def make(exchange_id: str) -> type:
            class Stub:
                def __init__(self, config: dict[str, Any]) -> None:
                    created.append((exchange_id, config))
                    self.options = dict(config.get("options") or {})
                    self.sandbox: list[bool] = []

                def set_sandbox_mode(self, enabled: bool) -> None:
                    self.sandbox.append(enabled)

            return Stub

        stub = types.SimpleNamespace(
            binance=make("binance"),
            bybit=make("bybit"),
            okx=make("okx"),
            exchanges=["binance", "bybit", "okx"],
        )
        real_import = importlib.import_module
        monkeypatch.setattr(
            importlib,
            "import_module",
            lambda name, *a, **k: stub if name == "ccxt" else real_import(name, *a, **k),
        )
        for var in (
            "CCXT_API_KEY",
            "CCXT_SECRET",
            "CCXT_PASSWORD",
            "BINANCE_API_KEY",
            "BINANCE_SECRET_KEY",
            "BINANCE_API_SECRET",
        ):
            monkeypatch.delenv(var, raising=False)
        return created

    def test_binance_name_defaults_exchange_id(
        self, stub_ccxt: list[tuple[str, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BINANCE_API_KEY", "bk")
        monkeypatch.setenv("BINANCE_SECRET_KEY", "bs")
        cfg = AppConfig(broker=BrokerConfig(name="binance", sandbox=False))
        b = create_broker("binance", cfg)
        assert isinstance(b, CCXTBroker)
        assert b.exchange_id == "binance"
        assert stub_ccxt[-1] == ("binance", {"enableRateLimit": True, "apiKey": "bk", "secret": "bs"})
        assert b.exchange.sandbox == []

    def test_ccxt_name_with_exchange_id_and_options(
        self, stub_ccxt: list[tuple[str, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CCXT_API_KEY", "k")
        monkeypatch.setenv("CCXT_SECRET", "s")
        monkeypatch.setenv("CCXT_PASSWORD", "p")
        cfg = AppConfig(
            broker=BrokerConfig(
                name="ccxt", exchange_id="okx", sandbox=True, extra={"options": {"defaultType": "spot"}}
            )
        )
        b = CCXTBroker.from_config(cfg)
        assert b.exchange_id == "okx"
        assert stub_ccxt[-1] == (
            "okx",
            {
                "enableRateLimit": True,
                "apiKey": "k",
                "secret": "s",
                "password": "p",
                "options": {"defaultType": "spot"},
            },
        )
        assert b.exchange.sandbox == [True]

    def test_ccxt_name_without_exchange_id_is_binance(
        self, stub_ccxt: list[tuple[str, dict[str, Any]]]
    ) -> None:
        cfg = AppConfig(broker=BrokerConfig(name="ccxt", sandbox=False))
        b = CCXTBroker.from_config(cfg)
        assert b.exchange_id == "binance"
        assert not b.has_credentials
        assert stub_ccxt[-1] == ("binance", {"enableRateLimit": True})

    def test_other_name_used_as_exchange_id(self, stub_ccxt: list[tuple[str, dict[str, Any]]]) -> None:
        cfg = AppConfig(broker=BrokerConfig(name="bybit", sandbox=False))
        b = CCXTBroker.from_config(cfg)
        assert b.exchange_id == "bybit"

    def test_invalid_options_type(self, stub_ccxt: list[tuple[str, dict[str, Any]]]) -> None:
        cfg = AppConfig(broker=BrokerConfig(name="binance", extra={"options": "spot"}))
        with pytest.raises(ConfigError):
            CCXTBroker.from_config(cfg)


# ---------------------------------------------------------------------------
# 간격 / 심볼 / 예외 변환
# ---------------------------------------------------------------------------
class TestHelpers:
    @pytest.mark.parametrize(
        ("interval", "expected"),
        [
            ("1m", "1m"),
            ("3m", "3m"),
            ("5m", "5m"),
            ("15m", "15m"),
            ("30m", "30m"),
            ("1h", "1h"),
            ("4h", "4h"),
            ("1d", "1d"),
            ("1w", "1w"),
        ],
    )
    def test_interval_mapping(self, interval: str, expected: str) -> None:
        assert to_ccxt_timeframe(interval) == expected
        assert interval in CCXTBroker.supported_intervals

    @pytest.mark.parametrize("interval", ["10m", "2h", "1M", "", "60"])
    def test_unsupported_interval(self, interval: str) -> None:
        with pytest.raises(ValueError):
            to_ccxt_timeframe(interval)
        assert interval not in TIMEFRAMES

    def test_split_symbol(self) -> None:
        assert split_symbol("BTC/USDT") == ("BTC", "USDT")
        assert split_symbol("BTC/USDT:USDT") == ("BTC", "USDT")
        for bad in ("BTCUSDT", "/USDT", "BTC/", ""):
            with pytest.raises(ValueError):
                split_symbol(bad)

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (ccxt.AuthenticationError("bad key"), AuthenticationError),
            (ccxt.PermissionDenied("no perm"), AuthenticationError),
            (ccxt.AccountSuspended("suspended"), AuthenticationError),
            (ccxt.InsufficientFunds("insufficient"), InsufficientFunds),
            (ccxt.RateLimitExceeded("too many"), RateLimitError),
            (ccxt.DDoSProtection("ddos"), RateLimitError),
            (ccxt.InvalidOrder("bad order"), OrderError),
            (ccxt.OrderNotFound("missing"), OrderError),
            (ccxt.NetworkError("net"), BrokerError),
            (ccxt.RequestTimeout("timeout"), BrokerError),
            (ccxt.ExchangeNotAvailable("down"), BrokerError),
            (ccxt.BadSymbol("sym"), BrokerError),
            (ccxt.ExchangeError("ex"), BrokerError),
            (ccxt.BaseError("base"), BrokerError),
        ],
    )
    def test_translate_ccxt_error(self, exc: Exception, expected: type[BrokerError]) -> None:
        mapped = translate_ccxt_error(exc)
        assert type(mapped) is expected
        assert type(exc).__name__ in str(mapped)
        assert mapped.payload == str(exc)

    def test_translate_non_ccxt_returns_none(self) -> None:
        assert translate_ccxt_error(ValueError("x")) is None
        assert translate_ccxt_error(KeyError("x")) is None
        # 이미 봇 예외면 그대로
        own = RateLimitError("already")
        assert translate_ccxt_error(own) is own
        assert translate_ccxt_error(ConfigError("cfg")) is None


# ---------------------------------------------------------------------------
# 시세
# ---------------------------------------------------------------------------
class TestCandles:
    def test_excludes_forming_candle_and_orders_oldest_first(
        self, public_broker: CCXTBroker, fake: FakeExchange, ohlcv: list[list[float]]
    ) -> None:
        newest = last_ts(ohlcv)
        with freeze_time(newest + timedelta(minutes=30)):
            out = public_broker.get_candles(SYMBOL, "1h", limit=50)
        assert len(out) == 50
        assert all(a.timestamp < b.timestamp for a, b in zip(out, out[1:], strict=False))
        assert out[-1].timestamp == newest - timedelta(hours=1)
        # 값은 실제 캔들 그대로
        expected = {r[0]: r for r in ohlcv}
        for c in out:
            row = expected[int(c.timestamp.timestamp() * 1000)]
            assert (c.open, c.high, c.low, c.close, c.volume) == tuple(row[1:6])
        # 요청 범위: limit+1 개, since = 마지막 완성 캔들 - limit*step
        name, args, _ = fake.calls[-1]
        assert name == "fetch_ohlcv"
        assert args[0:2] == (SYMBOL, "1h")
        assert args[3] == 51
        assert args[2] == int((newest - timedelta(hours=50)).timestamp() * 1000)

    def test_include_partial_keeps_forming_candle(
        self, public_broker: CCXTBroker, ohlcv: list[list[float]]
    ) -> None:
        newest = last_ts(ohlcv)
        with freeze_time(newest + timedelta(minutes=30)):
            out = public_broker.get_candles(SYMBOL, "1h", limit=10, include_partial=True)
        assert len(out) == 10
        assert out[-1].timestamp == newest

    def test_completed_candle_included_after_close(
        self, public_broker: CCXTBroker, ohlcv: list[list[float]]
    ) -> None:
        newest = last_ts(ohlcv)
        with freeze_time(newest + timedelta(hours=1, minutes=1)):
            out = public_broker.get_candles(SYMBOL, "1h", limit=10)
        assert out[-1].timestamp == newest

    def test_end_is_exclusive(self, public_broker: CCXTBroker, ohlcv: list[list[float]]) -> None:
        newest = last_ts(ohlcv)
        with freeze_time(newest + timedelta(days=1)):
            on_boundary = public_broker.get_candles(SYMBOL, "1h", limit=5, end=newest - timedelta(hours=5))
            mid_candle = public_broker.get_candles(
                SYMBOL, "1h", limit=5, end=newest - timedelta(hours=5, minutes=-20)
            )
        assert on_boundary[-1].timestamp == newest - timedelta(hours=6)
        assert len(on_boundary) == 5
        assert mid_candle[-1].timestamp == newest - timedelta(hours=5)
        # naive end 는 UTC 로 간주
        with freeze_time(newest + timedelta(days=1)):
            naive = public_broker.get_candles(
                SYMBOL, "1h", limit=3, end=(newest - timedelta(hours=5)).replace(tzinfo=None)
            )
        assert [c.timestamp for c in naive] == [c.timestamp for c in on_boundary[-3:]]

    def test_pagination_when_exchange_caps_batch(self, ohlcv: list[list[float]]) -> None:
        fake = FakeExchange(ohlcv, batch_cap=40)
        b = CCXTBroker("binance", exchange=fake)
        newest = last_ts(ohlcv)
        with freeze_time(newest + timedelta(minutes=5)):
            out = b.get_candles(SYMBOL, "1h", limit=120)
        assert len(out) == 120
        assert out[-1].timestamp == newest - timedelta(hours=1)
        assert all(
            b_.timestamp - a.timestamp == timedelta(hours=1) for a, b_ in zip(out, out[1:], strict=False)
        )
        fetches = [c for c in fake.calls if c[0] == "fetch_ohlcv"]
        assert len(fetches) >= 3
        sinces = [c[1][2] for c in fetches]
        assert sinces == sorted(sinces) and len(set(sinces)) == len(sinces)

    def test_fewer_candles_than_limit(self, public_broker: CCXTBroker, ohlcv: list[list[float]]) -> None:
        newest = last_ts(ohlcv)
        with freeze_time(newest + timedelta(hours=2)):
            out = public_broker.get_candles(SYMBOL, "1h", limit=5000)
        assert len(out) == len(ohlcv)

    def test_daily_candles_real_data(self, daily_candles: list[Candle]) -> None:
        rows = candles_to_ohlcv(daily_candles)
        fake = FakeExchange(rows)
        b = CCXTBroker("binance", exchange=fake)
        newest = last_ts(rows)
        with freeze_time(newest + timedelta(hours=12)):
            out = b.get_candles(SYMBOL, "1d", limit=30)
        assert len(out) == 30
        assert out[-1].timestamp == newest - timedelta(days=1)
        assert fake.calls[-1][1][1] == "1d"

    def test_invalid_arguments(self, public_broker: CCXTBroker) -> None:
        with pytest.raises(ValueError):
            public_broker.get_candles(SYMBOL, "10m")
        with pytest.raises(ValueError):
            public_broker.get_candles(SYMBOL, "1h", limit=0)

    def test_unknown_symbol_is_broker_error(
        self, public_broker: CCXTBroker, ohlcv: list[list[float]]
    ) -> None:
        with freeze_time(last_ts(ohlcv)), pytest.raises(BrokerError):
            public_broker.get_candles("XXX/USDT", "1h")

    def test_malformed_row(self, ohlcv: list[list[float]]) -> None:
        fake = FakeExchange(ohlcv)
        fake.ohlcv[-3] = [fake.ohlcv[-3][0], "bad", 1, 1, 1, 1]
        b = CCXTBroker("binance", exchange=fake)
        with freeze_time(last_ts(ohlcv) + timedelta(hours=2)), pytest.raises(BrokerError):
            b.get_candles(SYMBOL, "1h", limit=10)

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (ccxt.NetworkError("conn reset"), BrokerError),
            (ccxt.RateLimitExceeded("429"), RateLimitError),
            (ccxt.DDoSProtection("418"), RateLimitError),
        ],
    )
    def test_fetch_errors_translated(
        self, fake: FakeExchange, public_broker: CCXTBroker, exc: Exception, expected: type
    ) -> None:
        fake.fail["fetch_ohlcv"] = exc
        with pytest.raises(expected):
            public_broker.get_candles(SYMBOL, "1h", limit=5)

    def test_non_ccxt_exception_propagates(self, fake: FakeExchange, public_broker: CCXTBroker) -> None:
        fake.fail["fetch_ohlcv"] = RuntimeError("bug")
        with pytest.raises(RuntimeError):
            public_broker.get_candles(SYMBOL, "1h", limit=5)


class TestTicker:
    def test_last_price(self, public_broker: CCXTBroker, fake: FakeExchange) -> None:
        assert public_broker.get_ticker(SYMBOL) == fake.last_close
        assert fake.calls[-1][0] == "fetch_ticker"

    def test_falls_back_to_close_then_error(
        self, public_broker: CCXTBroker, fake: FakeExchange, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        close = fake.last_close
        monkeypatch.setattr(
            fake,
            "fetch_ticker",
            lambda symbol, params=None: {"symbol": symbol, "last": None, "close": close, "info": {}},
        )
        assert public_broker.get_ticker(SYMBOL) == close
        monkeypatch.setattr(
            fake,
            "fetch_ticker",
            lambda symbol, params=None: {"symbol": symbol, "last": None, "close": None, "info": {}},
        )
        with pytest.raises(BrokerError):
            public_broker.get_ticker(SYMBOL)
        monkeypatch.setattr(fake, "fetch_ticker", lambda symbol, params=None: [1, 2])
        with pytest.raises(BrokerError):
            public_broker.get_ticker(SYMBOL)

    def test_error_translation(self, public_broker: CCXTBroker, fake: FakeExchange) -> None:
        fake.fail["fetch_ticker"] = ccxt.ExchangeNotAvailable("maintenance")
        with pytest.raises(BrokerError):
            public_broker.get_ticker(SYMBOL)


# ---------------------------------------------------------------------------
# 메타: 정밀도 / 최소 주문 / 통화
# ---------------------------------------------------------------------------
class TestMeta:
    def test_round_quantity_truncates_to_market_precision(self, public_broker: CCXTBroker) -> None:
        assert public_broker.round_quantity(SYMBOL, 0.123456789) == pytest.approx(0.12345)
        assert public_broker.round_quantity("ETH/USDT", 1.23456789) == pytest.approx(1.2345)
        assert public_broker.round_quantity(SYMBOL, 2.0) == 2.0
        assert public_broker.round_quantity(SYMBOL, 0.000001) == 0.0  # 최소 정밀도 미만 → 0
        assert public_broker.round_quantity(SYMBOL, 0.0) == 0.0
        assert public_broker.round_quantity(SYMBOL, -1.0) == 0.0
        assert isinstance(public_broker.round_quantity(SYMBOL, 1.5), float)

    def test_round_price(self, public_broker: CCXTBroker) -> None:
        assert public_broker.round_price(SYMBOL, 65432.126) == pytest.approx(65432.13)
        assert public_broker.round_price("ETH/BTC", 0.0512345) == pytest.approx(0.05123)
        with pytest.raises(OrderError):
            public_broker.round_price(SYMBOL, 0)

    def test_unknown_symbol_precision(self, public_broker: CCXTBroker) -> None:
        with pytest.raises(BrokerError):
            public_broker.round_quantity("XXX/USDT", 1.0)
        with pytest.raises(BrokerError):
            public_broker.round_price("XXX/USDT", 1.0)

    def test_min_order_value(self, public_broker: CCXTBroker, fake: FakeExchange) -> None:
        assert public_broker.min_order_value(SYMBOL) == 5.0
        assert public_broker.min_order_value("ETH/BTC") == 0.0001
        with pytest.raises(BrokerError):
            public_broker.min_order_value("XXX/USDT")
        # 마켓은 한 번만 로드
        assert fake.load_markets_count == 1

    def test_min_order_value_missing_limits(self, ohlcv: list[list[float]]) -> None:
        markets = {SYMBOL: {**MARKETS[SYMBOL], "limits": {}}}
        b = CCXTBroker("binance", exchange=FakeExchange(ohlcv, markets=markets))
        assert b.min_order_value(SYMBOL) == 0.0

    def test_quote_and_base_currency(self, public_broker: CCXTBroker, fake: FakeExchange) -> None:
        # 마켓 로드 전: 문자열 분리 (네트워크 호출 없음)
        assert public_broker.quote_currency(SYMBOL) == "USDT"
        assert public_broker.base_currency(SYMBOL) == "BTC"
        assert fake.load_markets_count == 0
        with pytest.raises(ValueError):
            public_broker.quote_currency("BTCUSDT")
        # 마켓 로드 후: 마켓 정보 사용
        public_broker.min_order_value("ETH/BTC")
        assert public_broker.quote_currency("ETH/BTC") == "BTC"
        assert public_broker.base_currency("ETH/BTC") == "ETH"

    def test_market_open_always(self, public_broker: CCXTBroker) -> None:
        assert public_broker.is_market_open() is True

    def test_load_markets_bad_response(
        self, ohlcv: list[list[float]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeExchange(ohlcv)
        monkeypatch.setattr(fake, "load_markets", lambda *a, **k: None)
        b = CCXTBroker("binance", exchange=fake)
        with pytest.raises(BrokerError):
            b.min_order_value(SYMBOL)


# ---------------------------------------------------------------------------
# 계좌
# ---------------------------------------------------------------------------
# ccxt 통합 balance structure (fetch_balance 응답)
BALANCE_EXAMPLE: dict[str, Any] = {
    "info": {},
    "timestamp": None,
    "datetime": None,
    "free": {"USDT": 1500.25, "BTC": 0.5, "ETH": 0.0, "BNB": 0.0, "DOGE": 12.0, "SHIB": 0.00001},
    "used": {"USDT": 100.0, "BTC": 0.1, "ETH": 0.0, "BNB": 0.0, "DOGE": 0.0, "SHIB": 0.0},
    "total": {"USDT": 1600.25, "BTC": 0.6, "ETH": 0.0, "BNB": 0.0, "DOGE": 12.0, "SHIB": 0.00001},
    "USDT": {"free": 1500.25, "used": 100.0, "total": 1600.25},
    "BTC": {"free": 0.5, "used": 0.1, "total": 0.6},
    "ETH": {"free": 0.0, "used": 0.0, "total": 0.0},
    "BNB": {"free": 0.0, "used": 0.0, "total": 0.0},
    "DOGE": {"free": 12.0, "used": 0.0, "total": 12.0},
    "SHIB": {"free": 0.00001, "used": 0.0, "total": 0.00001},
}


class TestAccount:
    def test_requires_credentials(self, public_broker: CCXTBroker) -> None:
        with pytest.raises(AuthenticationError) as ei:
            public_broker.get_balances()
        assert "CCXT_API_KEY" in str(ei.value)
        with pytest.raises(AuthenticationError):
            public_broker.get_positions()
        with pytest.raises(AuthenticationError):
            public_broker.place_order(SYMBOL, OrderSide.BUY, 1.0)
        with pytest.raises(AuthenticationError):
            public_broker.cancel_order("1")
        with pytest.raises(AuthenticationError):
            public_broker.get_order("1")
        with pytest.raises(AuthenticationError):
            public_broker.get_open_orders()

    def test_get_balances_skips_zero(self, ohlcv: list[list[float]]) -> None:
        fake = FakeExchange(ohlcv, balance=BALANCE_EXAMPLE)
        b = CCXTBroker("binance", API_KEY, SECRET, exchange=fake)
        balances = b.get_balances()
        assert set(balances) == {"USDT", "BTC", "DOGE", "SHIB"}
        assert balances["USDT"].total == 1600.25
        assert balances["USDT"].available == 1500.25
        assert balances["USDT"].locked == pytest.approx(100.0)
        assert balances["BTC"].currency == "BTC"
        assert balances["BTC"].total == pytest.approx(0.6)

    def test_get_balances_total_missing(self, ohlcv: list[list[float]]) -> None:
        raw = {
            "info": {},
            "free": {},
            "used": {},
            "total": {},
            "BTC": {"free": 0.2, "used": 0.1, "total": None},
        }
        fake = FakeExchange(ohlcv, balance=raw)
        b = CCXTBroker("binance", API_KEY, SECRET, exchange=fake)
        assert b.get_balances()["BTC"].total == pytest.approx(0.3)

    def test_balance_auth_error(self, broker: CCXTBroker, fake: FakeExchange) -> None:
        fake.fail["fetch_balance"] = ccxt.AuthenticationError("Invalid API-key")
        with pytest.raises(AuthenticationError):
            broker.get_balances()

    def test_balance_bad_shape(self, ohlcv: list[list[float]], monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakeExchange(ohlcv, balance=BALANCE_EXAMPLE)
        monkeypatch.setattr(fake, "fetch_balance", lambda params=None: ["nope"])
        with pytest.raises(BrokerError):
            CCXTBroker("binance", API_KEY, SECRET, exchange=fake).get_balances()

    def test_get_positions_from_balances(self, ohlcv: list[list[float]]) -> None:
        fake = FakeExchange(ohlcv, balance=BALANCE_EXAMPLE)
        b = CCXTBroker("binance", API_KEY, SECRET, exchange=fake)
        positions = b.get_positions()
        # USDT 는 결제통화, ETH/BNB 는 0, DOGE/SHIB 는 마켓 없음 → BTC 만
        assert set(positions) == {SYMBOL}
        pos = positions[SYMBOL]
        assert pos.quantity == pytest.approx(0.6)
        assert pos.average_price == 0.0
        assert pos.meta["average_price_known"] is False
        assert pos.meta["free"] == 0.5 and pos.meta["used"] == 0.1
        assert pos.meta["source"] == "balance"

    def test_get_positions_with_known_average(self, ohlcv: list[list[float]]) -> None:
        fake = FakeExchange(ohlcv, balance=BALANCE_EXAMPLE)
        b = CCXTBroker("binance", API_KEY, SECRET, exchange=fake)
        price = fake.last_close
        positions = b.get_positions(average_prices={SYMBOL: price, "ETH/USDT": 1.0})
        assert positions[SYMBOL].average_price == price
        assert positions[SYMBOL].meta["average_price_known"] is True
        assert positions[SYMBOL].cost == pytest.approx(0.6 * price)

    def test_get_positions_quote_priority_and_dust(self, ohlcv: list[list[float]]) -> None:
        raw = {
            "info": {},
            "free": {},
            "used": {},
            "total": {},
            "ETH": {"free": 0.00005, "used": 0.0, "total": 0.00005},  # ETH/USDT 최소 0.0001 미만 → dust
            "BTC": {"free": 0.01, "used": 0.0, "total": 0.01},
            "USDT": {"free": 10.0, "used": 0.0, "total": 10.0},
        }
        fake = FakeExchange(ohlcv, balance=raw)
        b = CCXTBroker("binance", API_KEY, SECRET, exchange=fake)
        assert set(b.get_positions()) == {SYMBOL}
        # 결제통화를 BTC 로만 보면 BTC 는 현금, ETH 는 ETH/BTC 마켓이지만 dust
        assert b.get_positions(quote_currencies=("BTC",)) == {}
        # USDT 를 결제통화에서 빼면 USDT 는 마켓이 없어 제외
        assert set(b.get_positions(quote_currencies=("BTC", "KRW"))) == set()
        assert DEFAULT_QUOTE_CURRENCIES[0] == "USDT"


# ---------------------------------------------------------------------------
# 주문
# ---------------------------------------------------------------------------
class TestPlaceOrder:
    def test_stop_not_supported(self, broker: CCXTBroker) -> None:
        with pytest.raises(OrderError):
            broker.place_order(SYMBOL, OrderSide.BUY, 1.0, OrderType.STOP, price=1.0)

    def test_invalid_quantity_and_price(self, broker: CCXTBroker, fake: FakeExchange) -> None:
        with pytest.raises(OrderError):
            broker.place_order(SYMBOL, OrderSide.BUY, 0.0)
        with pytest.raises(OrderError):
            broker.place_order(SYMBOL, OrderSide.BUY, -1.0)
        with pytest.raises(OrderError):
            broker.place_order(SYMBOL, OrderSide.BUY, 1.0, OrderType.LIMIT)  # price 없음
        with pytest.raises(OrderError):
            broker.place_order(SYMBOL, OrderSide.BUY, 1.0, OrderType.LIMIT, price=0)
        with pytest.raises(OrderError):
            broker.place_order(SYMBOL, OrderSide.BUY, 0.000001)  # 정밀도 미만
        with pytest.raises(BrokerError):
            broker.place_order("XXX/USDT", OrderSide.BUY, 1.0)
        assert not [c for c in fake.calls if c[0] == "create_order"]

    def test_market_sell(self, broker: CCXTBroker, fake: FakeExchange) -> None:
        order = broker.place_order(SYMBOL, OrderSide.SELL, 0.123456789)
        name, args, _ = fake.calls[-1]
        assert name == "create_order"
        assert args == (SYMBOL, "market", "sell", 0.12345, None, {})
        assert order.id == "1001"
        assert order.symbol == SYMBOL
        assert order.side == OrderSide.SELL
        assert order.type == OrderType.MARKET
        assert order.status == OrderStatus.FILLED
        assert order.is_filled
        assert order.quantity == pytest.approx(0.12345)
        assert order.filled_quantity == pytest.approx(0.12345)
        assert order.average_price == fake.last_close
        assert order.price is None
        assert order.fee == pytest.approx(0.12345 * fake.last_close * 0.001)
        assert order.created_at.tzinfo is timezone.utc
        assert order.updated_at is not None and order.updated_at.tzinfo is timezone.utc
        assert order.raw["status"] == "closed"

    def test_market_buy_default_uses_base_amount(self, broker: CCXTBroker, fake: FakeExchange) -> None:
        broker.place_order(SYMBOL, OrderSide.BUY, 0.01)
        assert fake.calls[-1][1] == (SYMBOL, "market", "buy", 0.01, None, {})
        assert not any(c[0] == "fetch_ticker" for c in fake.calls)

    def test_market_buy_binance_quote_order_qty(self, ohlcv: list[list[float]]) -> None:
        fake = FakeExchange(ohlcv, options={"createMarketBuyOrderRequiresPrice": True})
        b = CCXTBroker("binance", API_KEY, SECRET, exchange=fake)
        order = b.place_order(SYMBOL, OrderSide.BUY, 0.01)
        name, args, _ = fake.calls[-1]
        expected_cost = float(fake.price_to_precision(SYMBOL, 0.01 * fake.last_close))
        assert name == "create_order"
        assert args[:5] == (SYMBOL, "market", "buy", 0.01, None)
        assert args[5] == {"quoteOrderQty": expected_cost}
        assert any(c[0] == "fetch_ticker" for c in fake.calls)
        assert order.status == OrderStatus.FILLED
        assert order.filled_quantity == pytest.approx(expected_cost / fake.last_close)

    def test_market_buy_other_exchange_passes_price(self, ohlcv: list[list[float]]) -> None:
        fake = FakeExchange(ohlcv, exchange_id="kucoin", options={"createMarketBuyOrderRequiresPrice": True})
        b = CCXTBroker("kucoin", API_KEY, SECRET, exchange=fake)
        b.place_order(SYMBOL, OrderSide.BUY, 0.01)
        assert fake.calls[-1][1] == (SYMBOL, "market", "buy", 0.01, fake.last_close, {})

    def test_market_sell_never_needs_price(self, ohlcv: list[list[float]]) -> None:
        fake = FakeExchange(ohlcv, exchange_id="kucoin", options={"createMarketBuyOrderRequiresPrice": True})
        b = CCXTBroker("kucoin", API_KEY, SECRET, exchange=fake)
        b.place_order(SYMBOL, OrderSide.SELL, 0.01)
        assert fake.calls[-1][1] == (SYMBOL, "market", "sell", 0.01, None, {})

    def test_limit_order_rounds_amount_and_price(self, broker: CCXTBroker, fake: FakeExchange) -> None:
        order = broker.place_order(SYMBOL, OrderSide.BUY, 0.123456789, OrderType.LIMIT, price=12345.678)
        assert fake.calls[-1][1] == (SYMBOL, "limit", "buy", 0.12345, 12345.68, {})
        assert order.type == OrderType.LIMIT
        assert order.status == OrderStatus.OPEN
        assert order.price == pytest.approx(12345.68)
        assert order.quantity == pytest.approx(0.12345)
        assert order.filled_quantity == 0.0
        assert order.average_price is None
        assert order.fee == 0.0
        assert order.remaining_quantity == pytest.approx(0.12345)

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (ccxt.InsufficientFunds("Account has insufficient balance"), InsufficientFunds),
            (ccxt.InvalidOrder("Filter failure: MIN_NOTIONAL"), OrderError),
            (ccxt.AuthenticationError("Invalid API-key"), AuthenticationError),
            (ccxt.PermissionDenied("no trading"), AuthenticationError),
            (ccxt.RateLimitExceeded("Too many requests"), RateLimitError),
            (ccxt.DDoSProtection("418"), RateLimitError),
            (ccxt.NetworkError("timeout"), BrokerError),
            (ccxt.ExchangeError("unknown"), BrokerError),
        ],
    )
    def test_create_order_errors(
        self, broker: CCXTBroker, fake: FakeExchange, exc: Exception, expected: type
    ) -> None:
        fake.fail["create_order"] = exc
        with pytest.raises(expected) as ei:
            broker.place_order(SYMBOL, OrderSide.BUY, 0.01)
        assert ei.value.__cause__ is exc

    def test_non_ccxt_error_propagates(self, broker: CCXTBroker, fake: FakeExchange) -> None:
        fake.fail["create_order"] = RuntimeError("bug")
        with pytest.raises(RuntimeError):
            broker.place_order(SYMBOL, OrderSide.BUY, 0.01)

    def test_order_response_without_id(
        self, broker: CCXTBroker, fake: FakeExchange, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(fake, "create_order", lambda *a, **k: {"id": None, "status": "open"})
        with pytest.raises(BrokerError):
            broker.place_order(SYMBOL, OrderSide.BUY, 0.01)
        monkeypatch.setattr(fake, "create_order", lambda *a, **k: "oops")
        with pytest.raises(BrokerError):
            broker.place_order(SYMBOL, OrderSide.BUY, 0.01)


class TestOrderLifecycle:
    def test_cancel_get_open_orders(self, broker: CCXTBroker, fake: FakeExchange) -> None:
        o1 = broker.place_order(SYMBOL, OrderSide.BUY, 0.01, OrderType.LIMIT, price=1000.0)
        o2 = broker.place_order("ETH/USDT", OrderSide.BUY, 0.5, OrderType.LIMIT, price=100.0)
        opened = broker.get_open_orders()
        assert {o.id for o in opened} == {o1.id, o2.id}
        assert [o.id for o in broker.get_open_orders(SYMBOL)] == [o1.id]
        assert fake.calls[-1] == ("fetch_open_orders", (SYMBOL,), {})

        assert broker.cancel_order(o1.id, SYMBOL) is True
        assert fake.calls[-1] == ("cancel_order", (o1.id, SYMBOL), {})
        got = broker.get_order(o1.id, SYMBOL)
        assert got.status == OrderStatus.CANCELED
        assert got.status.is_terminal
        assert [o.id for o in broker.get_open_orders()] == [o2.id]

        # 이미 종료된 주문 재취소 / 없는 주문 → False
        assert broker.cancel_order(o1.id, SYMBOL) is False
        assert broker.cancel_order("does-not-exist", SYMBOL) is False

    def test_cancel_other_invalid_order_raises(self, broker: CCXTBroker, fake: FakeExchange) -> None:
        fake.fail["cancel_order"] = ccxt.InvalidOrder("cannot cancel")
        with pytest.raises(OrderError):
            broker.cancel_order("1", SYMBOL)
        fake.fail["cancel_order"] = ccxt.NetworkError("down")
        with pytest.raises(BrokerError):
            broker.cancel_order("1", SYMBOL)
        fake.fail["cancel_order"] = RuntimeError("bug")
        with pytest.raises(RuntimeError):
            broker.cancel_order("1", SYMBOL)

    def test_get_order_not_found(self, broker: CCXTBroker) -> None:
        with pytest.raises(OrderError):
            broker.get_order("nope", SYMBOL)

    def test_get_order_passes_symbol(self, broker: CCXTBroker, fake: FakeExchange) -> None:
        o = broker.place_order(SYMBOL, OrderSide.SELL, 0.01)
        got = broker.get_order(o.id, SYMBOL)
        assert got.id == o.id and got.symbol == SYMBOL and got.side == OrderSide.SELL
        assert fake.calls[-1] == ("fetch_order", (o.id, SYMBOL), {})
        # symbol 생략도 허용 (거래소가 요구하면 ccxt 예외 → BrokerError)
        assert broker.get_order(o.id).id == o.id


# ---------------------------------------------------------------------------
# 통합 주문 구조 파싱 / 상태 매핑
# ---------------------------------------------------------------------------
def unified_order(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "12345",
        "clientOrderId": "abc",
        "timestamp": 1700000000000,
        "datetime": "2023-11-14T22:13:20.000Z",
        "lastTradeTimestamp": None,
        "lastUpdateTimestamp": None,
        "status": "open",
        "symbol": SYMBOL,
        "type": "limit",
        "timeInForce": "GTC",
        "postOnly": False,
        "side": "buy",
        "price": 100.0,
        "triggerPrice": None,
        "average": None,
        "amount": 2.0,
        "filled": 0.0,
        "remaining": 2.0,
        "cost": 0.0,
        "trades": [],
        "fee": None,
        "fees": [],
        "info": {},
    }
    base.update(overrides)
    return base


class TestParseOrder:
    @pytest.mark.parametrize(
        ("status", "filled", "expected"),
        [
            ("open", 0.0, OrderStatus.OPEN),
            ("open", 0.5, OrderStatus.PARTIALLY_FILLED),
            ("canceling", 0.0, OrderStatus.OPEN),
            ("closed", 2.0, OrderStatus.FILLED),
            ("closed", 1.0, OrderStatus.FILLED),  # IOC 부분 체결 후 종료
            ("canceled", 0.0, OrderStatus.CANCELED),
            ("canceled", 0.5, OrderStatus.CANCELED),
            ("cancelled", 0.0, OrderStatus.CANCELED),
            ("expired", 0.0, OrderStatus.EXPIRED),
            ("rejected", 0.0, OrderStatus.REJECTED),
            (None, 0.0, OrderStatus.PENDING),
            ("", 0.0, OrderStatus.PENDING),
            ("weird", 0.0, OrderStatus.PENDING),
        ],
    )
    def test_status_mapping(
        self, broker: CCXTBroker, status: str | None, filled: float, expected: OrderStatus
    ) -> None:
        order = broker._parse_order(
            unified_order(status=status, filled=filled), SYMBOL, None, None, None, None
        )
        assert order.status == expected
        assert order.filled_quantity == filled

    def test_average_from_cost(self, broker: CCXTBroker) -> None:
        o = broker._parse_order(
            unified_order(status="closed", type="market", price=None, average=None, filled=2.0, cost=250.0),
            SYMBOL,
            None,
            None,
            None,
            None,
        )
        assert o.average_price == pytest.approx(125.0)
        assert o.type == OrderType.MARKET
        assert o.price is None
        assert o.created_at == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
        assert o.updated_at is None

    def test_fee_conversion(self, broker: CCXTBroker) -> None:
        # quote 통화 수수료는 그대로
        o = broker._parse_order(
            unified_order(status="closed", filled=2.0, average=100.0, fee={"cost": 0.2, "currency": "USDT"}),
            SYMBOL,
            None,
            None,
            None,
            None,
        )
        assert o.fee == pytest.approx(0.2)
        # base 통화 수수료는 평균가로 환산
        o = broker._parse_order(
            unified_order(status="closed", filled=2.0, average=100.0, fee={"cost": 0.002, "currency": "BTC"}),
            SYMBOL,
            None,
            None,
            None,
            None,
        )
        assert o.fee == pytest.approx(0.2)
        # 제3 통화(BNB) 는 환산 불가 → 0, raw 에 보존
        o = broker._parse_order(
            unified_order(status="closed", filled=2.0, average=100.0, fee={"cost": 0.001, "currency": "BNB"}),
            SYMBOL,
            None,
            None,
            None,
            None,
        )
        assert o.fee == 0.0 and o.raw["fee"]["currency"] == "BNB"
        # fee 가 없고 fees 리스트만 있을 때 합산
        o = broker._parse_order(
            unified_order(
                status="closed",
                filled=2.0,
                average=100.0,
                fee=None,
                fees=[{"cost": 0.1, "currency": "USDT"}, {"cost": 0.001, "currency": "BTC"}],
            ),
            SYMBOL,
            None,
            None,
            None,
            None,
        )
        assert o.fee == pytest.approx(0.2)

    def test_fallbacks_when_fields_missing(self, broker: CCXTBroker) -> None:
        raw = {
            "id": "x",
            "status": None,
            "symbol": None,
            "side": None,
            "type": None,
            "amount": None,
            "filled": None,
        }
        o = broker._parse_order(raw, "ETH/USDT", OrderSide.SELL, OrderType.LIMIT, 1.5, 200.0)
        assert o.symbol == "ETH/USDT"
        assert o.side == OrderSide.SELL
        assert o.type == OrderType.LIMIT
        assert o.quantity == 1.5
        assert o.price == 200.0
        assert o.status == OrderStatus.PENDING
        assert o.created_at.tzinfo is timezone.utc
        with pytest.raises(BrokerError):
            broker._parse_order({"id": "x", "status": "open"}, SYMBOL, None, None, None, None)  # side 모름

    def test_quantity_from_filled_plus_remaining(self, broker: CCXTBroker) -> None:
        o = broker._parse_order(
            unified_order(amount=None, filled=0.5, remaining=1.5), SYMBOL, None, None, None, None
        )
        assert o.quantity == pytest.approx(2.0)
        assert o.remaining_quantity == pytest.approx(1.5)

    def test_stop_like_types(self, broker: CCXTBroker) -> None:
        o = broker._parse_order(
            unified_order(type="stop_loss_limit", price=90.0), SYMBOL, None, None, None, None
        )
        assert o.type == OrderType.STOP
        assert o.price == 90.0


# ---------------------------------------------------------------------------
# 실제 ccxt 인스턴스 (오프라인: set_markets 로 마켓 주입, 네트워크 메서드는 monkeypatch)
# ---------------------------------------------------------------------------
class TestRealCcxtInstance:
    @pytest.fixture
    def real_exchange(self, ohlcv: list[list[float]], monkeypatch: pytest.MonkeyPatch) -> Any:
        ex = ccxt.binance({"enableRateLimit": True})
        ex.set_markets(MARKETS)
        fake = FakeExchange(ohlcv)
        fake.markets = {k: dict(v) for k, v in MARKETS.items()}
        monkeypatch.setattr(ex, "fetch_ohlcv", fake.fetch_ohlcv)
        monkeypatch.setattr(ex, "fetch_ticker", fake.fetch_ticker)
        monkeypatch.setattr(ex, "fetch_balance", lambda params=None: BALANCE_EXAMPLE)
        ex.set_sandbox_mode(True)  # testnet URL 로만 바뀜, 호출 없음
        return ex

    def test_precision_and_candles_with_real_helpers(
        self, real_exchange: Any, ohlcv: list[list[float]]
    ) -> None:
        b = CCXTBroker("binance", API_KEY, SECRET, exchange=real_exchange)
        assert b.round_quantity(SYMBOL, 0.123456789) == 0.12345
        assert b.round_quantity(SYMBOL, 0.000001) == 0.0
        assert b.round_price(SYMBOL, 65432.126) == 65432.13
        assert b.min_order_value(SYMBOL) == 5.0
        assert b.quote_currency(SYMBOL) == "USDT"
        with pytest.raises(BrokerError):
            b.round_quantity("XXX/USDT", 1.0)  # BadSymbol → BrokerError
        newest = last_ts(ohlcv)
        with freeze_time(newest + timedelta(minutes=10)):
            out = b.get_candles(SYMBOL, "1h", limit=20)
        assert len(out) == 20 and out[-1].timestamp == newest - timedelta(hours=1)
        assert b.get_ticker(SYMBOL) == float(ohlcv[-1][4])
        assert set(b.get_balances()) == {"USDT", "BTC", "DOGE", "SHIB"}
        assert set(b.get_positions()) == {SYMBOL}
        assert math.isclose(b.get_positions()[SYMBOL].quantity, 0.6)

    def test_real_timeframes_supported(self, real_exchange: Any) -> None:
        assert all(tf in real_exchange.timeframes for tf in TIMEFRAMES.values())
        assert "10m" not in real_exchange.timeframes
