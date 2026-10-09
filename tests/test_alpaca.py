"""AlpacaBroker 테스트.

- 네트워크 호출 없음: 모든 HTTP 는 ``responses`` 로 모킹한다 (재시도 대기를 없애기 위해 max_retries=0 클라이언트 주입).
- 봉(bars) 응답의 값은 conftest 의 **실제 Upbit 캔들** 로 채우고, 형식은 공식 문서의 ``/v2/stocks/{symbol}/bars``
  응답 스키마(``{"bars": [{t,o,h,l,c,v,n,vw}], "symbol", "next_page_token"}``)를 따른다.
  (``n`` 체결 건수는 Upbit 원본에 없어 0, ``v`` 는 정수형이라 내림 — 어댑터는 두 필드를 쓰지 않는다.)
- 계좌/주문 응답은 docs.alpaca.markets 레퍼런스의 예시 응답 스키마 그대로이며 이 파일 안에만 둔다.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
import responses
from freezegun import freeze_time

from tradingbot.brokers import create_broker, get_broker_class
from tradingbot.brokers.alpaca import (
    CLOCK_CACHE_TTL,
    DATA_BASE_URL,
    HEADER_KEY_ID,
    HEADER_SECRET,
    LIVE_BASE_URL,
    PAPER_BASE_URL,
    STATUS_MAP,
    TIMEFRAMES,
    AlpacaBroker,
    format_quantity,
    parse_rfc3339,
    to_alpaca_timeframe,
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
from tradingbot.models import INTERVAL_SECONDS, Candle, OrderSide, OrderStatus, OrderType
from tradingbot.utils.http import HttpClient

KEY = "PKTESTKEYID"
SECRET = "testsecretkey"
SYMBOL = "AAPL"

# ---------------------------------------------------------------------------
# 공식 문서 예시 응답 (docs.alpaca.markets/reference/*)
# ---------------------------------------------------------------------------
# GET /v2/account (reference/getaccount-1 예시)
ACCOUNT_EXAMPLE: dict[str, Any] = {
    "account_blocked": False,
    "account_number": "PALPACA_123",
    "accrued_fees": "0",
    "admin_configurations": {},
    "balance_asof": "2023-09-27",
    "buying_power": "245432.61",
    "cash": "122086.5",
    "created_at": "2023-01-01T18:20:20.272275Z",
    "crypto_status": "ACTIVE",
    "crypto_tier": 1,
    "currency": "USD",
    "effective_buying_power": "245432.61",
    "equity": "123346.11",
    "id": "1d9eed04-be39-4e01-9b84-a48ac5bbafcf",
    "initial_margin": "629.8",
    "intraday_adjustments": "0",
    "last_equity": "122011.09751111286868",
    "last_maintenance_margin": "480.73",
    "long_market_value": "1259.61",
    "maintenance_margin": "377.88",
    "multiplier": "2",
    "non_marginable_buying_power": "122086.5",
    "options_buying_power": "122716.305",
    "options_trading_level": 2,
    "pending_reg_taf_fees": "0",
    "pending_transfer_in": "0",
    "portfolio_value": "123346.11",
    "position_market_value": "1259.61",
    "regt_buying_power": "245432.61",
    "short_market_value": "0",
    "shorting_enabled": True,
    "sma": "123346.11",
    "status": "ACTIVE",
    "trade_suspended_by_user": False,
    "trading_blocked": False,
    "transfers_blocked": False,
}

# GET /v2/positions 항목 (reference/getallopenpositions 예시 첫 항목)
POSITION_EXAMPLE: dict[str, Any] = {
    "asset_class": "us_equity",
    "asset_id": "904837e3-3b76-47ec-b432-046db621571b",
    "asset_marginable": True,
    "avg_entry_price": "100.0",
    "change_today": "0.0084",
    "cost_basis": "500.0",
    "current_price": "120.0",
    "exchange": "NASDAQ",
    "lastday_price": "119.0",
    "market_value": "600.0",
    "qty": "5",
    "qty_available": "4",
    "side": "long",
    "symbol": "AAPL",
    "unrealized_intraday_pl": "10.0",
    "unrealized_intraday_plpc": "0.0084",
    "unrealized_pl": "100.0",
    "unrealized_plpc": "0.20",
}

# POST /v2/orders 응답 (reference/postorder "Equity" 예시, Order 스키마)
ORDER_EXAMPLE: dict[str, Any] = {
    "asset_class": "us_equity",
    "asset_id": "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415",
    "canceled_at": None,
    "client_order_id": "5680c4bc-9ac1-4a12-a44c-df427ba53032",
    "created_at": "2023-12-12T22:31:24.668464435Z",
    "expired_at": None,
    "extended_hours": False,
    "failed_at": None,
    "filled_at": None,
    "filled_avg_price": None,
    "filled_qty": "0",
    "hwm": None,
    "id": "7b08df51-c1ac-453c-99f9-323a5f075f0d",
    "legs": None,
    "limit_price": "150",
    "notional": None,
    "order_class": "",
    "order_type": "limit",
    "qty": "2",
    "replaced_at": None,
    "replaced_by": None,
    "replaces": None,
    "side": "buy",
    "source": None,
    "status": "accepted",
    "stop_price": None,
    "submitted_at": "2023-12-12T22:31:24.577215743Z",
    "subtag": None,
    "symbol": "AAPL",
    "time_in_force": "gtc",
    "trail_percent": None,
    "trail_price": None,
    "type": "limit",
    "updated_at": "2023-12-12T22:31:24.668464435Z",
}

# GET /v2/clock (reference/legacyclock 예시)
CLOCK_OPEN: dict[str, Any] = {
    "is_open": True,
    "next_close": "2025-06-24T16:00:00-04:00",
    "next_open": "2025-06-25T09:30:00-04:00",
    "timestamp": "2025-06-24T14:15:22-04:00",
}
CLOCK_CLOSED: dict[str, Any] = {
    "is_open": False,
    "next_close": "2025-06-25T16:00:00-04:00",
    "next_open": "2025-06-25T09:30:00-04:00",
    "timestamp": "2025-06-24T18:15:22-04:00",
}

# GET /v2/assets/{symbol_or_asset_id} (reference/get-v2-assets-symbol_or_asset_id 예시, Asset 스키마)
ASSET_EXAMPLE: dict[str, Any] = {
    "borrow_status": "easy_to_borrow",
    "class": "us_equity",
    "easy_to_borrow": True,
    "exchange": "NASDAQ",
    "fractionable": True,
    "id": "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415",
    "marginable": True,
    "name": "Apple Inc. Common Stock",
    "shortable": True,
    "status": "active",
    "symbol": "AAPL",
    "tradable": True,
}
#: docs/fractional-trading: 소수점 거래는 약 2,000 종목만 가능. 그 밖의 종목은 ``fractionable: false`` 로 응답한다.
NON_FRACTIONABLE_SYMBOL = "BRK.A"


def order_response(**overrides: Any) -> dict[str, Any]:
    out = dict(ORDER_EXAMPLE)
    out.update(overrides)
    return out


def asset_response(**overrides: Any) -> dict[str, Any]:
    out = dict(ASSET_EXAMPLE)
    out.update(overrides)
    return out


def asset_url(symbol: str = SYMBOL) -> str:
    return f"{PAPER_BASE_URL}/v2/assets/{symbol}"


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def bars_from_raw(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Upbit 원본(최신→과거) → Alpaca stock_bar 목록(오래된→최신)."""
    out = []
    for r in sorted(raw, key=lambda r: r["candle_date_time_utc"]):
        ts = datetime.fromisoformat(r["candle_date_time_utc"]).replace(tzinfo=timezone.utc)
        volume = float(r["candle_acc_trade_volume"])
        vw = float(r["candle_acc_trade_price"]) / volume if volume else float(r["trade_price"])
        out.append(
            {
                "t": iso_z(ts),
                "o": float(r["opening_price"]),
                "h": float(r["high_price"]),
                "l": float(r["low_price"]),
                "c": float(r["trade_price"]),
                "v": int(volume),
                "n": 0,
                "vw": round(vw, 6),
            }
        )
    return out


def bars_payload(
    bars: list[dict[str, Any]], token: str | None = None, symbol: str = SYMBOL
) -> dict[str, Any]:
    return {"bars": bars, "symbol": symbol, "next_page_token": token}


def query_of(call: Any) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(call.request.url).query).items()}


def make_broker(
    *, paper: bool = True, feed: str = "iex", key: str | None = KEY, secret: str | None = SECRET
) -> AlpacaBroker:
    client = HttpClient(PAPER_BASE_URL if paper else LIVE_BASE_URL, timeout=5, max_retries=0)
    return AlpacaBroker(key, secret, paper=paper, feed=feed, client=client)


@pytest.fixture
def hourly_bars(real_btc_hourly_raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return bars_from_raw(real_btc_hourly_raw)


@pytest.fixture
def daily_bars(real_btc_daily_raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return bars_from_raw(real_btc_daily_raw)


@pytest.fixture
def broker() -> AlpacaBroker:
    return make_broker()


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """time.monotonic 을 제어해 /v2/clock 캐시를 검증한다."""
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    return now


BARS_URL = f"{DATA_BASE_URL}/v2/stocks/{SYMBOL}/bars"
TRADE_URL = f"{DATA_BASE_URL}/v2/stocks/{SYMBOL}/trades/latest"
QUOTE_URL = f"{DATA_BASE_URL}/v2/stocks/{SYMBOL}/quotes/latest"


# ---------------------------------------------------------------------------
# 생성 / 설정
# ---------------------------------------------------------------------------
class TestConstruction:
    def test_registry(self) -> None:
        assert get_broker_class("alpaca") is AlpacaBroker
        assert AlpacaBroker.name == "alpaca"
        assert AlpacaBroker.asset_class.value == "stock"

    def test_paper_vs_live_base_url(self) -> None:
        assert AlpacaBroker(KEY, SECRET).base_url == PAPER_BASE_URL
        assert AlpacaBroker(KEY, SECRET, paper=True).base_url == PAPER_BASE_URL
        assert AlpacaBroker(KEY, SECRET, paper=False).base_url == LIVE_BASE_URL
        assert AlpacaBroker(KEY, SECRET).data_url == DATA_BASE_URL
        assert AlpacaBroker(KEY, SECRET, feed="SIP").feed == "sip"
        assert AlpacaBroker(KEY, SECRET, feed="").feed == "iex"

    def test_constructible_without_credentials(self) -> None:
        b = AlpacaBroker()
        assert not b.has_credentials
        assert b.paper is True
        assert make_broker(key=None, secret=None).has_credentials is False
        assert make_broker(key="", secret="x").has_credentials is False

    def test_meta(self, broker: AlpacaBroker) -> None:
        assert broker.quote_currency(SYMBOL) == "USD"
        assert broker.base_currency(SYMBOL) == SYMBOL
        assert broker.min_order_value(SYMBOL) == 1.0
        assert set(broker.supported_intervals) == set(INTERVAL_SECONDS)
        assert "10m" in broker.supported_intervals

    def test_close_only_owned_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = HttpClient(PAPER_BASE_URL, max_retries=0)
        closed: list[bool] = []
        monkeypatch.setattr(client.session, "close", lambda: closed.append(True))
        AlpacaBroker(KEY, SECRET, client=client).close()
        assert closed == []
        owned = AlpacaBroker(KEY, SECRET)
        monkeypatch.setattr(owned._client.session, "close", lambda: closed.append(True))
        owned.close()
        assert closed == [True]

    def test_from_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("APCA_API_KEY_ID", "envkey")
        monkeypatch.setenv("APCA_API_SECRET_KEY", "envsecret")
        cfg = AppConfig(broker=BrokerConfig(name="alpaca", sandbox=True, extra={"feed": "SIP", "timeout": 3}))
        b = create_broker("alpaca", cfg)
        assert isinstance(b, AlpacaBroker)
        assert b.base_url == PAPER_BASE_URL
        assert b.feed == "sip"
        assert b.timeout == 3.0
        assert b.has_credentials
        assert b._auth_headers() == {HEADER_KEY_ID: "envkey", HEADER_SECRET: "envsecret"}

        live = AlpacaBroker.from_config(AppConfig(broker=BrokerConfig(name="alpaca", sandbox=False)))
        assert live.base_url == LIVE_BASE_URL
        assert live.feed == "iex"
        assert live.timeout == 10.0

        monkeypatch.delenv("APCA_API_KEY_ID")
        monkeypatch.delenv("APCA_API_SECRET_KEY")
        nokeys = AlpacaBroker.from_config(AppConfig(broker=BrokerConfig(name="alpaca")))
        assert not nokeys.has_credentials

        with pytest.raises(ConfigError):
            AlpacaBroker.from_config(AppConfig(broker=BrokerConfig(name="alpaca", extra={"timeout": "fast"})))


# ---------------------------------------------------------------------------
# 인증 / 공통 오류 매핑
# ---------------------------------------------------------------------------
class TestAuth:
    def test_headers_sent(self, broker: AlpacaBroker) -> None:
        with responses.RequestsMock() as rsps:
            rsps.get(f"{PAPER_BASE_URL}/v2/account", json=ACCOUNT_EXAMPLE)
            broker.get_balances()
            req = rsps.calls[0].request
        assert req.headers[HEADER_KEY_ID] == KEY
        assert req.headers[HEADER_SECRET] == SECRET
        assert "Authorization" not in req.headers
        assert req.url == f"{PAPER_BASE_URL}/v2/account"

    def test_live_base_url_used(self) -> None:
        b = make_broker(paper=False)
        with responses.RequestsMock() as rsps:
            rsps.get(f"{LIVE_BASE_URL}/v2/account", json=ACCOUNT_EXAMPLE)
            b.get_balances()
            assert rsps.calls[0].request.url.startswith(LIVE_BASE_URL)

    def test_missing_credentials_raise_before_http(self) -> None:
        b = make_broker(key=None, secret=None)
        with responses.RequestsMock() as rsps:
            for call in (
                b.get_balances,
                b.get_positions,
                lambda: b.get_candles(SYMBOL, "1h"),
                lambda: b.get_ticker(SYMBOL),
                lambda: b.place_order(SYMBOL, OrderSide.BUY, 1.0),
                lambda: b.cancel_order("x"),
                lambda: b.get_order("x"),
                b.get_open_orders,
                lambda: b.get_asset(SYMBOL),
            ):
                with pytest.raises(AuthenticationError) as ei:
                    call()
                assert "ALPACA_API_KEY" in str(ei.value)
            assert len(rsps.calls) == 0

    @pytest.mark.parametrize(
        ("status", "body", "expected"),
        [
            (401, {"code": 40110000, "message": "request is not authorized"}, AuthenticationError),
            (403, {"code": 40310000, "message": "forbidden"}, AuthenticationError),
            (403, {"code": 40310000, "message": "insufficient buying power"}, InsufficientFunds),
            (429, {"code": 42910000, "message": "too many requests"}, RateLimitError),
            (422, {"code": 42210000, "message": "invalid symbol"}, OrderError),
            (500, {"code": 50010000, "message": "internal server error"}, BrokerError),
            (404, {"code": 40410000, "message": "not found"}, BrokerError),
        ],
    )
    def test_error_mapping_generic_endpoint(
        self, broker: AlpacaBroker, status: int, body: dict[str, Any], expected: type
    ) -> None:
        with responses.RequestsMock() as rsps:
            rsps.get(f"{PAPER_BASE_URL}/v2/account", json=body, status=status)
            with pytest.raises(expected) as ei:
                broker.get_balances()
        assert ei.value.status_code == status
        assert ei.value.payload == body
        assert body["message"] in str(ei.value)

    def test_connection_error_is_broker_error(self, broker: AlpacaBroker) -> None:
        with responses.RequestsMock() as rsps:
            rsps.get(f"{PAPER_BASE_URL}/v2/account", body=requests.ConnectionError("boom"))
            with pytest.raises(BrokerError):
                broker.get_balances()

    def test_non_json_error_body(self, broker: AlpacaBroker) -> None:
        with responses.RequestsMock() as rsps:
            rsps.get(f"{PAPER_BASE_URL}/v2/account", body="<html>bad gateway</html>", status=502)
            with pytest.raises(BrokerError) as ei:
                broker.get_balances()
        assert ei.value.status_code == 502


# ---------------------------------------------------------------------------
# 봉 (bars)
# ---------------------------------------------------------------------------
class TestCandles:
    def test_parses_bars_and_excludes_forming(
        self, broker: AlpacaBroker, hourly_bars: list[dict[str, Any]], candles: list[Candle]
    ) -> None:
        newest = parse_rfc3339(hourly_bars[-1]["t"])
        with responses.RequestsMock() as rsps:
            rsps.get(BARS_URL, json=bars_payload(list(reversed(hourly_bars))))  # sort=desc 응답은 최신→과거
            with freeze_time(newest + timedelta(minutes=30)):
                out = broker.get_candles(SYMBOL, "1h", limit=50)
            q = query_of(rsps.calls[0])
        assert len(out) == 50
        assert out[-1].timestamp == newest - timedelta(hours=1)
        assert all(a.timestamp < b.timestamp for a, b in zip(out, out[1:], strict=False))
        by_ts = {c.timestamp: c for c in candles}
        for c in out:
            real = by_ts[c.timestamp]
            assert (c.open, c.high, c.low, c.close) == (real.open, real.high, real.low, real.close)
            assert c.volume == float(int(real.volume))
            assert c.timestamp.tzinfo is timezone.utc
        assert q["timeframe"] == "1Hour"
        assert q["feed"] == "iex"
        assert q["adjustment"] == "all"
        assert q["sort"] == "desc"
        assert q["limit"] == "51"
        assert q["end"].endswith("Z") and q["start"].endswith("Z")
        assert parse_rfc3339(q["start"]) < parse_rfc3339(q["end"])
        assert parse_rfc3339(q["end"]) == newest + timedelta(minutes=30)
        assert "page_token" not in q

    def test_include_partial(self, broker: AlpacaBroker, hourly_bars: list[dict[str, Any]]) -> None:
        newest = parse_rfc3339(hourly_bars[-1]["t"])
        with responses.RequestsMock() as rsps:
            rsps.get(BARS_URL, json=bars_payload(list(reversed(hourly_bars))))
            with freeze_time(newest + timedelta(minutes=30)):
                out = broker.get_candles(SYMBOL, "1h", limit=10, include_partial=True)
        assert len(out) == 10 and out[-1].timestamp == newest

    def test_completed_bar_included_after_close(
        self, broker: AlpacaBroker, hourly_bars: list[dict[str, Any]]
    ) -> None:
        newest = parse_rfc3339(hourly_bars[-1]["t"])
        with responses.RequestsMock() as rsps:
            rsps.get(BARS_URL, json=bars_payload(list(reversed(hourly_bars))))
            with freeze_time(newest + timedelta(hours=1)):
                out = broker.get_candles(SYMBOL, "1h", limit=10)
        assert out[-1].timestamp == newest

    def test_end_exclusive_and_naive_end(
        self, broker: AlpacaBroker, hourly_bars: list[dict[str, Any]]
    ) -> None:
        newest = parse_rfc3339(hourly_bars[-1]["t"])
        end = newest - timedelta(hours=5)
        with responses.RequestsMock() as rsps:
            rsps.get(BARS_URL, json=bars_payload(list(reversed(hourly_bars))))
            rsps.get(BARS_URL, json=bars_payload(list(reversed(hourly_bars))))
            with freeze_time(newest + timedelta(days=1)):
                aware = broker.get_candles(SYMBOL, "1h", limit=5, end=end)
                naive = broker.get_candles(SYMBOL, "1h", limit=5, end=end.replace(tzinfo=None))
            assert query_of(rsps.calls[0])["end"] == iso_z(end)
        assert len(aware) == 5
        assert aware[-1].timestamp == end - timedelta(hours=1)
        assert [c.timestamp for c in naive] == [c.timestamp for c in aware]

    def test_pagination(self, broker: AlpacaBroker, hourly_bars: list[dict[str, Any]]) -> None:
        newest = parse_rfc3339(hourly_bars[-1]["t"])
        desc = list(reversed(hourly_bars))
        first, second = desc[:30], desc[30:80]
        with responses.RequestsMock() as rsps:
            rsps.get(
                BARS_URL,
                json=bars_payload(first, token="QUFQTHxNfDIwMjItMDEtMDNUMDk6MDA6MDAuMDAwMDAwMDAwWg=="),
            )
            rsps.get(BARS_URL, json=bars_payload(second, token=None))
            with freeze_time(newest + timedelta(minutes=1)):
                out = broker.get_candles(SYMBOL, "1h", limit=70)
            assert len(rsps.calls) == 2
            q1, q2 = query_of(rsps.calls[0]), query_of(rsps.calls[1])
        assert "page_token" not in q1 and q1["limit"] == "71"
        assert q2["page_token"] == "QUFQTHxNfDIwMjItMDEtMDNUMDk6MDA6MDAuMDAwMDAwMDAwWg=="
        assert q2["limit"] == "41"  # 71 - 30
        assert q2["timeframe"] == "1Hour" and q2["sort"] == "desc"
        assert len(out) == 70
        assert out[-1].timestamp == newest - timedelta(hours=1)
        assert all(
            b.timestamp - a.timestamp == timedelta(hours=1) for a, b in zip(out, out[1:], strict=False)
        )

    def test_pagination_stops_when_enough(
        self, broker: AlpacaBroker, hourly_bars: list[dict[str, Any]]
    ) -> None:
        newest = parse_rfc3339(hourly_bars[-1]["t"])
        with responses.RequestsMock() as rsps:
            rsps.get(BARS_URL, json=bars_payload(list(reversed(hourly_bars))[:11], token="more"))
            with freeze_time(newest + timedelta(minutes=1)):
                out = broker.get_candles(SYMBOL, "1h", limit=10)
            assert len(rsps.calls) == 1
        assert len(out) == 10

    def test_pagination_stops_on_empty_page(
        self, broker: AlpacaBroker, hourly_bars: list[dict[str, Any]]
    ) -> None:
        newest = parse_rfc3339(hourly_bars[-1]["t"])
        with responses.RequestsMock() as rsps:
            rsps.get(BARS_URL, json=bars_payload(list(reversed(hourly_bars))[:5], token="more"))
            rsps.get(BARS_URL, json=bars_payload([], token="still-more"))
            with freeze_time(newest + timedelta(minutes=1)):
                out = broker.get_candles(SYMBOL, "1h", limit=10)
            assert len(rsps.calls) == 2
        assert len(out) == 4

    def test_daily_bars(self, broker: AlpacaBroker, daily_bars: list[dict[str, Any]]) -> None:
        newest = parse_rfc3339(daily_bars[-1]["t"])
        with responses.RequestsMock() as rsps:
            rsps.get(BARS_URL, json=bars_payload(list(reversed(daily_bars))))
            rsps.get(BARS_URL, json=bars_payload(list(reversed(daily_bars))))
            with freeze_time(newest + timedelta(hours=12)):
                during = broker.get_candles(SYMBOL, "1d", limit=30)
            with freeze_time(newest + timedelta(hours=25)):
                after = broker.get_candles(SYMBOL, "1d", limit=30)
            q = query_of(rsps.calls[0])
        assert q["timeframe"] == "1Day"
        assert during[-1].timestamp == newest - timedelta(days=1)
        assert after[-1].timestamp == newest
        assert len(during) == 30 and len(after) == 30
        # 일봉은 주말/휴장을 감안해 넉넉히 거슬러 올라간다
        assert parse_rfc3339(q["start"]) <= newest - timedelta(days=60)

    @pytest.mark.parametrize(("interval", "timeframe"), sorted(TIMEFRAMES.items()))
    def test_timeframe_param(
        self, broker: AlpacaBroker, hourly_bars: list[dict[str, Any]], interval: str, timeframe: str
    ) -> None:
        assert to_alpaca_timeframe(interval) == timeframe
        with responses.RequestsMock() as rsps:
            rsps.get(BARS_URL, json=bars_payload([]))
            with freeze_time(parse_rfc3339(hourly_bars[-1]["t"])):
                assert broker.get_candles(SYMBOL, interval, limit=3) == []
            assert query_of(rsps.calls[0])["timeframe"] == timeframe

    def test_invalid_arguments(self, broker: AlpacaBroker) -> None:
        with pytest.raises(ValueError):
            broker.get_candles(SYMBOL, "2h")
        with pytest.raises(ValueError):
            to_alpaca_timeframe("1M")
        with pytest.raises(ValueError):
            broker.get_candles(SYMBOL, "1h", limit=0)

    def test_multi_symbol_shape_tolerated(
        self, broker: AlpacaBroker, hourly_bars: list[dict[str, Any]]
    ) -> None:
        newest = parse_rfc3339(hourly_bars[-1]["t"])
        payload = {"bars": {SYMBOL: list(reversed(hourly_bars))[:6]}, "next_page_token": None}
        with responses.RequestsMock() as rsps:
            rsps.get(BARS_URL, json=payload)
            with freeze_time(newest + timedelta(hours=1)):
                out = broker.get_candles(SYMBOL, "1h", limit=5)
        assert len(out) == 5 and out[-1].timestamp == newest

    def test_malformed_payloads(self, broker: AlpacaBroker, hourly_bars: list[dict[str, Any]]) -> None:
        newest = parse_rfc3339(hourly_bars[-1]["t"])
        bad_bar = dict(hourly_bars[-1])
        bad_bar["o"] = "n/a"
        with responses.RequestsMock() as rsps:
            rsps.get(BARS_URL, json=bars_payload([bad_bar]))
            rsps.get(BARS_URL, json=bars_payload([{"o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]))
            rsps.get(BARS_URL, json=[1, 2, 3])
            with freeze_time(newest + timedelta(hours=1)):
                for _ in range(3):
                    with pytest.raises(BrokerError):
                        broker.get_candles(SYMBOL, "1h", limit=5)

    def test_http_errors(self, broker: AlpacaBroker, hourly_bars: list[dict[str, Any]]) -> None:
        with responses.RequestsMock() as rsps:
            rsps.get(BARS_URL, json={"message": "too many requests"}, status=429)
            with pytest.raises(RateLimitError):
                broker.get_candles(SYMBOL, "1h", limit=5)
            rsps.get(
                BARS_URL,
                json={"message": "subscription does not permit querying recent SIP data"},
                status=403,
            )
            with pytest.raises(AuthenticationError):
                broker.get_candles(SYMBOL, "1h", limit=5)
            rsps.get(BARS_URL, json={"message": "server error"}, status=500)
            with pytest.raises(BrokerError):
                broker.get_candles(SYMBOL, "1h", limit=5)


# ---------------------------------------------------------------------------
# 현재가
# ---------------------------------------------------------------------------
class TestTicker:
    def test_latest_trade(self, broker: AlpacaBroker, candles: list[Candle]) -> None:
        last = candles[-1]
        payload = {
            "symbol": SYMBOL,
            "trade": {
                "c": ["@", "T"],
                "i": 689,
                "p": last.close,
                "s": 100,
                "t": iso_z(last.timestamp),
                "x": "P",
                "z": "C",
            },
        }
        with responses.RequestsMock() as rsps:
            rsps.get(TRADE_URL, json=payload)
            assert broker.get_ticker(SYMBOL) == last.close
            assert query_of(rsps.calls[0]) == {"feed": "iex"}
            assert rsps.calls[0].request.headers[HEADER_KEY_ID] == KEY

    def test_fallback_to_quote_midpoint(self, broker: AlpacaBroker, candles: list[Candle]) -> None:
        last = candles[-1]
        quote = {
            "symbol": SYMBOL,
            "quote": {
                "ap": last.high,
                "as": 1,
                "ax": "Q",
                "bp": last.low,
                "bs": 2,
                "bx": "Q",
                "c": ["R"],
                "t": iso_z(last.timestamp),
                "z": "C",
            },
        }
        with responses.RequestsMock() as rsps:
            rsps.get(
                TRADE_URL,
                json={
                    "symbol": SYMBOL,
                    "trade": {"p": 0, "s": 0, "t": iso_z(last.timestamp), "x": "", "c": [], "i": 0, "z": ""},
                },
            )
            rsps.get(QUOTE_URL, json=quote)
            assert broker.get_ticker(SYMBOL) == pytest.approx((last.high + last.low) / 2)
            assert len(rsps.calls) == 2

    def test_fallback_one_sided_quote_and_none(self, broker: AlpacaBroker, candles: list[Candle]) -> None:
        last = candles[-1]
        with responses.RequestsMock() as rsps:
            rsps.get(TRADE_URL, json={"symbol": SYMBOL, "trade": None})
            rsps.get(
                QUOTE_URL,
                json={
                    "symbol": SYMBOL,
                    "quote": {
                        "ap": last.high,
                        "as": 1,
                        "ax": "Q",
                        "bp": 0,
                        "bs": 0,
                        "bx": "",
                        "c": [],
                        "t": iso_z(last.timestamp),
                        "z": "C",
                    },
                },
            )
            assert broker.get_ticker(SYMBOL) == last.high
            rsps.get(TRADE_URL, json={"symbol": SYMBOL, "trade": None})
            rsps.get(
                QUOTE_URL,
                json={
                    "symbol": SYMBOL,
                    "quote": {
                        "ap": 0,
                        "as": 0,
                        "ax": "",
                        "bp": 0,
                        "bs": 0,
                        "bx": "",
                        "c": [],
                        "t": iso_z(last.timestamp),
                        "z": "C",
                    },
                },
            )
            with pytest.raises(BrokerError):
                broker.get_ticker(SYMBOL)

    def test_ticker_errors(self, broker: AlpacaBroker) -> None:
        with responses.RequestsMock() as rsps:
            rsps.get(TRADE_URL, json={"message": "unauthorized"}, status=401)
            with pytest.raises(AuthenticationError):
                broker.get_ticker(SYMBOL)
            rsps.get(TRADE_URL, json={"message": "rate"}, status=429)
            with pytest.raises(RateLimitError):
                broker.get_ticker(SYMBOL)


# ---------------------------------------------------------------------------
# 계좌
# ---------------------------------------------------------------------------
class TestAccount:
    def test_get_balances(self, broker: AlpacaBroker) -> None:
        with responses.RequestsMock() as rsps:
            rsps.get(f"{PAPER_BASE_URL}/v2/account", json=ACCOUNT_EXAMPLE)
            balances = broker.get_balances()
        assert set(balances) == {"USD"}
        usd = balances["USD"]
        assert usd.currency == "USD"
        # total 은 다른 어댑터처럼 현금(cash) — equity(123346.11, 포지션 평가액 포함) 가 아니다
        assert usd.total == 122086.5
        assert usd.available == 122086.5  # min(cash, non_marginable_buying_power)
        assert usd.locked == 0.0

    def test_get_balances_locked_is_cash_reserved_by_open_orders(self, broker: AlpacaBroker) -> None:
        """locked = cash − 비마진 매수 여력 (미체결 주문에 묶인 현금). equity 를 total 로 두면 locked 가 보유 주식 평가액
        (long_market_value 1259.61) 이 되고 엔진의 fallback equity(현금 + 포지션) 가 포지션을 이중 계산한다."""
        reserved = {**ACCOUNT_EXAMPLE, "non_marginable_buying_power": "121086.5", "buying_power": "242173"}
        no_nmbp = {k: v for k, v in ACCOUNT_EXAMPLE.items() if k != "non_marginable_buying_power"}
        cash_only = {k: v for k, v in no_nmbp.items() if k != "buying_power"}
        with responses.RequestsMock() as rsps:
            rsps.get(f"{PAPER_BASE_URL}/v2/account", json=reserved)
            rsps.get(f"{PAPER_BASE_URL}/v2/account", json=no_nmbp)
            rsps.get(f"{PAPER_BASE_URL}/v2/account", json=cash_only)
            usd = broker.get_balances()["USD"]
            assert usd.total == 122086.5
            assert usd.available == 121086.5
            assert usd.locked == pytest.approx(1000.0)
            assert usd.locked != pytest.approx(float(ACCOUNT_EXAMPLE["long_market_value"]))
            # non_marginable_buying_power 가 없으면 buying_power(마진 2배) 와 현금 중 작은 값, 둘 다 없으면 현금
            assert broker.get_balances()["USD"].available == 122086.5
            assert broker.get_balances()["USD"].available == 122086.5

    def test_base_equity_contract_uses_cash_total(self, broker: AlpacaBroker) -> None:
        """BaseBroker.get_equity 기본 구현(현금 total + 포지션 평가) 에 넣어도 포지션이 이중 계산되지 않는다."""
        from tradingbot.brokers.base import BaseBroker

        with responses.RequestsMock() as rsps:
            rsps.get(f"{PAPER_BASE_URL}/v2/positions", json=[])
            rsps.get(f"{PAPER_BASE_URL}/v2/account", json=ACCOUNT_EXAMPLE)
            assert BaseBroker.get_equity(broker, [SYMBOL]) == 122086.5

    def test_get_equity_uses_account_equity(self, broker: AlpacaBroker) -> None:
        with responses.RequestsMock() as rsps:
            rsps.get(f"{PAPER_BASE_URL}/v2/account", json=ACCOUNT_EXAMPLE)
            assert broker.get_equity() == 123346.11
            assert broker.get_equity([SYMBOL]) == 123346.11
            assert len(rsps.calls) == 2

    def test_account_bad_shape(self, broker: AlpacaBroker) -> None:
        with responses.RequestsMock() as rsps:
            rsps.get(f"{PAPER_BASE_URL}/v2/account", json=[1])
            with pytest.raises(BrokerError):
                broker.get_balances()

    def test_get_positions(self, broker: AlpacaBroker) -> None:
        short = {**POSITION_EXAMPLE, "symbol": "TSLA", "side": "short", "qty": "-3", "qty_available": "-3"}
        zero = {**POSITION_EXAMPLE, "symbol": "MSFT", "qty": "0", "qty_available": "0"}
        with responses.RequestsMock() as rsps:
            rsps.get(f"{PAPER_BASE_URL}/v2/positions", json=[POSITION_EXAMPLE, short, zero])
            positions = broker.get_positions()
        assert set(positions) == {"AAPL"}
        pos = positions["AAPL"]
        assert pos.quantity == 5.0
        assert pos.average_price == 100.0
        assert pos.cost == 500.0
        assert pos.meta["qty_available"] == 4.0
        assert pos.meta["market_value"] == 600.0
        assert pos.meta["current_price"] == 120.0
        assert pos.meta["asset_class"] == "us_equity"
        assert pos.unrealized_pnl(120.0) == pytest.approx(100.0)

    def test_get_positions_empty_and_bad(self, broker: AlpacaBroker) -> None:
        with responses.RequestsMock() as rsps:
            rsps.get(f"{PAPER_BASE_URL}/v2/positions", json=[])
            assert broker.get_positions() == {}
            rsps.get(f"{PAPER_BASE_URL}/v2/positions", json={"oops": 1})
            with pytest.raises(BrokerError):
                broker.get_positions()


# ---------------------------------------------------------------------------
# 장 운영 (clock) / 수량 반올림
# ---------------------------------------------------------------------------
class TestClock:
    def test_clock_cached_30s(self, broker: AlpacaBroker, frozen_clock: list[float]) -> None:
        with responses.RequestsMock() as rsps:
            rsps.get(f"{PAPER_BASE_URL}/v2/clock", json=CLOCK_OPEN)
            rsps.get(f"{PAPER_BASE_URL}/v2/clock", json=CLOCK_CLOSED)
            assert broker.is_market_open() is True
            frozen_clock[0] += CLOCK_CACHE_TTL - 1
            assert broker.is_market_open() is True
            assert len(rsps.calls) == 1
            frozen_clock[0] += 2
            assert broker.is_market_open() is False
            assert len(rsps.calls) == 2
            assert broker.next_market_open == datetime(2025, 6, 25, 13, 30, tzinfo=timezone.utc)
            assert broker.next_market_close == datetime(2025, 6, 25, 20, 0, tzinfo=timezone.utc)
            assert len(rsps.calls) == 2

    def test_fallback_to_nyse_schedule_on_error(
        self, broker: AlpacaBroker, frozen_clock: list[float]
    ) -> None:
        tuesday_open = datetime(2025, 6, 24, 18, 15, 22, tzinfo=timezone.utc)  # 14:15 ET 화요일
        saturday = datetime(2025, 6, 28, 15, 0, tzinfo=timezone.utc)
        with responses.RequestsMock() as rsps:
            rsps.get(f"{PAPER_BASE_URL}/v2/clock", json={"message": "server error"}, status=500)
            rsps.get(f"{PAPER_BASE_URL}/v2/clock", json={"unexpected": True})
            rsps.get(f"{PAPER_BASE_URL}/v2/clock", body=requests.ConnectionError("down"))
            with freeze_time(tuesday_open):
                assert broker.is_market_open() is True
                assert broker.is_market_open() is True  # 실패는 캐시하지 않음 → 재조회
            with freeze_time(saturday):
                assert broker.is_market_open() is False
            assert len(rsps.calls) == 3
            assert broker.next_market_open is None  # 여전히 실패 → None (4번째 호출)

    def test_fallback_without_credentials(self) -> None:
        b = make_broker(key=None, secret=None)
        with (
            responses.RequestsMock() as rsps,
            freeze_time(datetime(2025, 6, 24, 18, 15, tzinfo=timezone.utc)),
        ):
            assert b.is_market_open() is True
            assert len(rsps.calls) == 0

    def test_round_quantity_policy(self, broker: AlpacaBroker, frozen_clock: list[float]) -> None:
        # 명시적 정책: 네트워크 호출 없음
        with responses.RequestsMock() as rsps:
            assert broker.round_quantity(SYMBOL, 3.6541234567891, fractional=True) == 3.654123456
            assert broker.round_quantity(SYMBOL, 3.9999999999, fractional=True) == 3.999999999
            assert broker.round_quantity(SYMBOL, 3.9, fractional=False) == 3.0
            assert broker.round_quantity(SYMBOL, 0.9, fractional=False) == 0.0
            assert broker.round_quantity(SYMBOL, 0.0, fractional=True) == 0.0
            assert broker.round_quantity(SYMBOL, -2.0, fractional=False) == 0.0
            assert len(rsps.calls) == 0
        # 정규장 여부 + 종목의 fractionable 에 따라 (종목 정보는 심볼당 한 번만 조회)
        with responses.RequestsMock() as rsps:
            rsps.get(f"{PAPER_BASE_URL}/v2/clock", json=CLOCK_OPEN)
            rsps.get(asset_url(), json=ASSET_EXAMPLE)
            assert broker.round_quantity(SYMBOL, 2.5) == 2.5
            assert broker.round_quantity(SYMBOL, 2.5) == 2.5
            assert [c.request.url for c in rsps.calls] == [f"{PAPER_BASE_URL}/v2/clock", asset_url()]
            assert rsps.calls[1].request.headers[HEADER_KEY_ID] == KEY
        with responses.RequestsMock() as rsps:
            frozen_clock[0] += CLOCK_CACHE_TTL + 1
            rsps.get(f"{PAPER_BASE_URL}/v2/clock", json=CLOCK_CLOSED)
            assert broker.round_quantity(SYMBOL, 2.5) == 2.0
            assert len(rsps.calls) == 1  # 장외에는 종목 정보도 조회하지 않는다

    def test_round_quantity_non_fractionable_asset_whole_shares(
        self, broker: AlpacaBroker, frozen_clock: list[float]
    ) -> None:
        """정규장이어도 ``fractionable: false`` 종목은 정수 주 (docs/fractional-trading: 소수 주문은
        "requested asset is not fractionable" 로 거부된다)."""
        sym = NON_FRACTIONABLE_SYMBOL
        with responses.RequestsMock() as rsps:
            rsps.get(f"{PAPER_BASE_URL}/v2/clock", json=CLOCK_OPEN)
            rsps.get(asset_url(sym), json=asset_response(symbol=sym, exchange="NYSE", fractionable=False))
            rsps.get(asset_url(), json=ASSET_EXAMPLE)
            assert broker.round_quantity(sym, 1.775221458) == 1.0
            assert broker.round_quantity(sym, 0.9) == 0.0
            assert broker.round_quantity(sym, 0.9, fractional=True) == 0.9  # 명시 정책이 우선
            assert broker.round_quantity(SYMBOL, 1.775221458) == 1.775221458  # 캐시는 심볼별
            assert [c.request.url for c in rsps.calls] == [
                f"{PAPER_BASE_URL}/v2/clock",
                asset_url(sym),
                asset_url(),
            ]
        assert broker.get_asset(sym)["fractionable"] is False
        assert broker.get_asset(SYMBOL) == ASSET_EXAMPLE

    def test_asset_lookup_failure_keeps_fractional_and_retries(
        self, broker: AlpacaBroker, frozen_clock: list[float], caplog: pytest.LogCaptureFixture
    ) -> None:
        with responses.RequestsMock() as rsps:
            rsps.get(f"{PAPER_BASE_URL}/v2/clock", json=CLOCK_OPEN)
            rsps.get(asset_url(), json={"code": 50010000, "message": "internal server error"}, status=500)
            rsps.get(asset_url(), json=[1])  # 형식 오류
            rsps.get(asset_url(), json=ASSET_EXAMPLE)
            with caplog.at_level(logging.WARNING, logger="tradingbot.brokers.alpaca"):
                assert broker.round_quantity(SYMBOL, 2.5) == 2.5
                assert broker.round_quantity(SYMBOL, 2.5) == 2.5
            assert caplog.text.count("종목 정보 조회 실패") == 2
            assert broker.round_quantity(SYMBOL, 2.5) == 2.5
            assert len(rsps.calls) == 4  # 실패는 캐시하지 않고 다음에 다시 조회, 성공 후에는 캐시
            assert broker.round_quantity(SYMBOL, 2.5) == 2.5
            assert len(rsps.calls) == 4

    def test_round_price(self, broker: AlpacaBroker) -> None:
        assert broker.round_price(SYMBOL, 150.255) == 150.26
        assert broker.round_price(SYMBOL, 150.254) == 150.25
        assert broker.round_price(SYMBOL, 1.005) == 1.01
        assert broker.round_price(SYMBOL, 0.12345) == 0.1235
        assert broker.round_price(SYMBOL, 0.99996) == 1.0
        with pytest.raises(OrderError):
            broker.round_price(SYMBOL, 0)
        with pytest.raises(OrderError):
            broker.round_price(SYMBOL, -1)


# ---------------------------------------------------------------------------
# 주문
# ---------------------------------------------------------------------------
class TestPlaceOrder:
    def test_market_buy_fractional_during_session(
        self, broker: AlpacaBroker, frozen_clock: list[float], candles: list[Candle]
    ) -> None:
        fill = candles[-1].close
        resp = order_response(
            type="market",
            order_type="market",
            limit_price=None,
            time_in_force="day",
            qty="3.654123456",
            filled_qty="3.654123456",
            filled_avg_price=str(fill),
            filled_at="2023-12-12T22:31:25.000000Z",
            status="filled",
        )
        with responses.RequestsMock() as rsps:
            rsps.get(f"{PAPER_BASE_URL}/v2/clock", json=CLOCK_OPEN)
            rsps.get(asset_url(), json=ASSET_EXAMPLE)
            rsps.post(f"{PAPER_BASE_URL}/v2/orders", json=resp)
            order = broker.place_order(SYMBOL, OrderSide.BUY, 3.6541234567891)
            body = json.loads(rsps.calls[-1].request.body)
            assert rsps.calls[-1].request.headers[HEADER_KEY_ID] == KEY
            assert [c.request.url for c in rsps.calls] == [
                f"{PAPER_BASE_URL}/v2/clock",
                asset_url(),
                f"{PAPER_BASE_URL}/v2/orders",
            ]
        assert body == {
            "symbol": SYMBOL,
            "side": "buy",
            "type": "market",
            "time_in_force": "day",
            "qty": "3.654123456",
        }
        assert order.id == ORDER_EXAMPLE["id"]
        assert order.symbol == SYMBOL
        assert order.side == OrderSide.BUY
        assert order.type == OrderType.MARKET
        assert order.status == OrderStatus.FILLED
        assert order.quantity == 3.654123456
        assert order.filled_quantity == 3.654123456
        assert order.average_price == fill
        assert order.price is None
        assert order.fee == 0.0
        assert order.created_at == datetime(2023, 12, 12, 22, 31, 24, 668464, tzinfo=timezone.utc)
        assert order.updated_at == datetime(2023, 12, 12, 22, 31, 24, 668464, tzinfo=timezone.utc)
        assert order.raw["client_order_id"] == ORDER_EXAMPLE["client_order_id"]

    def test_market_buy_non_fractionable_asset_sends_whole_shares(
        self, broker: AlpacaBroker, frozen_clock: list[float], candles: list[Candle]
    ) -> None:
        """정규장 시장가 매수라도 fractionable=false 종목은 정수 주를 보낸다 (소수 수량은 422 로 거부되어 진입 불가)."""
        sym = NON_FRACTIONABLE_SYMBOL
        fill = candles[-1].close
        resp = order_response(
            type="market",
            order_type="market",
            limit_price=None,
            time_in_force="day",
            symbol=sym,
            qty="1",
            filled_qty="1",
            filled_avg_price=str(fill),
            filled_at="2023-12-12T22:31:25.000000Z",
            status="filled",
        )
        with responses.RequestsMock() as rsps:
            rsps.get(f"{PAPER_BASE_URL}/v2/clock", json=CLOCK_OPEN)
            rsps.get(asset_url(sym), json=asset_response(symbol=sym, exchange="NYSE", fractionable=False))
            rsps.post(f"{PAPER_BASE_URL}/v2/orders", json=resp)
            order = broker.place_order(sym, OrderSide.BUY, 1.775221458)
            body = json.loads(rsps.calls[-1].request.body)
            assert [c.request.method for c in rsps.calls] == ["GET", "GET", "POST"]
        assert body["qty"] == "1" and body["symbol"] == sym and body["type"] == "market"
        assert order.quantity == 1.0 and order.status == OrderStatus.FILLED
        # 1주 미만이면 HTTP 없이 거부 (clock/종목 정보는 캐시)
        with responses.RequestsMock() as rsps:
            with pytest.raises(OrderError):
                broker.place_order(sym, OrderSide.BUY, 0.9)
            assert len(rsps.calls) == 0

    def test_market_sell_outside_session_whole_shares(
        self, broker: AlpacaBroker, frozen_clock: list[float]
    ) -> None:
        resp = order_response(
            type="market",
            order_type="market",
            limit_price=None,
            time_in_force="day",
            side="sell",
            qty="3",
            status="accepted",
        )
        with responses.RequestsMock() as rsps:
            rsps.get(f"{PAPER_BASE_URL}/v2/clock", json=CLOCK_CLOSED)
            rsps.post(f"{PAPER_BASE_URL}/v2/orders", json=resp)
            order = broker.place_order(SYMBOL, OrderSide.SELL, 3.9)
            body = json.loads(rsps.calls[-1].request.body)
        assert body["qty"] == "3" and body["side"] == "sell" and body["type"] == "market"
        assert order.status == OrderStatus.OPEN
        assert not order.status.is_terminal
        assert order.side == OrderSide.SELL

    def test_market_below_one_share_outside_session(
        self, broker: AlpacaBroker, frozen_clock: list[float]
    ) -> None:
        with responses.RequestsMock() as rsps:
            rsps.get(f"{PAPER_BASE_URL}/v2/clock", json=CLOCK_CLOSED)
            with pytest.raises(OrderError):
                broker.place_order(SYMBOL, OrderSide.BUY, 0.5)
            assert all(c.request.method == "GET" for c in rsps.calls)

    def test_limit_order_body(self, broker: AlpacaBroker) -> None:
        with responses.RequestsMock() as rsps:
            rsps.post(
                f"{PAPER_BASE_URL}/v2/orders", json=order_response(limit_price="150.26", time_in_force="day")
            )
            order = broker.place_order(SYMBOL, OrderSide.BUY, 2.7, OrderType.LIMIT, price=150.255)
            body = json.loads(rsps.calls[0].request.body)
            assert len(rsps.calls) == 1  # 지정가는 clock 조회 없이 정수 주
        assert body == {
            "symbol": SYMBOL,
            "side": "buy",
            "type": "limit",
            "time_in_force": "day",
            "qty": "2",
            "limit_price": "150.26",
        }
        assert order.type == OrderType.LIMIT
        assert order.price == 150.26
        assert order.status == OrderStatus.OPEN
        assert order.quantity == 2.0
        assert order.filled_quantity == 0.0
        assert order.average_price is None

    def test_limit_price_formats(self, broker: AlpacaBroker) -> None:
        with responses.RequestsMock() as rsps:
            rsps.post(f"{PAPER_BASE_URL}/v2/orders", json=order_response(limit_price="150"))
            rsps.post(f"{PAPER_BASE_URL}/v2/orders", json=order_response(limit_price="0.1235"))
            broker.place_order(SYMBOL, OrderSide.BUY, 1, OrderType.LIMIT, price=150.0)
            broker.place_order(SYMBOL, OrderSide.BUY, 1, OrderType.LIMIT, price=0.12345)
            assert json.loads(rsps.calls[0].request.body)["limit_price"] == "150"
            assert json.loads(rsps.calls[1].request.body)["limit_price"] == "0.1235"

    def test_rejected_before_http(self, broker: AlpacaBroker) -> None:
        with responses.RequestsMock() as rsps:
            with pytest.raises(OrderError):
                broker.place_order(SYMBOL, OrderSide.BUY, 1.0, OrderType.STOP, price=100.0)
            with pytest.raises(OrderError):
                broker.place_order(SYMBOL, OrderSide.BUY, 0.0)
            with pytest.raises(OrderError):
                broker.place_order(SYMBOL, OrderSide.BUY, -1.0)
            with pytest.raises(OrderError):
                broker.place_order(SYMBOL, OrderSide.BUY, 1.0, OrderType.LIMIT)
            with pytest.raises(OrderError):
                broker.place_order(SYMBOL, OrderSide.BUY, 1.0, OrderType.LIMIT, price=0)
            with pytest.raises(OrderError):
                broker.place_order(SYMBOL, OrderSide.BUY, 0.5, OrderType.LIMIT, price=10.0)  # 1주 미만
            assert len(rsps.calls) == 0

    @pytest.mark.parametrize(
        ("status", "body", "expected"),
        [
            (403, {"code": 40310000, "message": "insufficient buying power"}, InsufficientFunds),
            (
                403,
                {
                    "code": 40310000,
                    "message": "insufficient qty available for order (requested: 10, available: 5)",
                },
                InsufficientFunds,
            ),
            (403, {"code": 40310000, "message": "account is not authorized to trade"}, AuthenticationError),
            (401, {"code": 40110000, "message": "request is not authorized"}, AuthenticationError),
            (
                422,
                {
                    "code": 42210000,
                    "message": "invalid limit_price 290.123. sub-penny increment does not fulfill minimum pricing criteria",
                },
                OrderError,
            ),
            (400, {"code": 40010001, "message": "qty must be > 0"}, OrderError),
            (429, {"code": 42910000, "message": "too many requests"}, RateLimitError),
            (500, {"code": 50010000, "message": "internal server error"}, BrokerError),
        ],
    )
    def test_order_error_mapping(
        self, broker: AlpacaBroker, status: int, body: dict[str, Any], expected: type
    ) -> None:
        with responses.RequestsMock() as rsps:
            rsps.post(f"{PAPER_BASE_URL}/v2/orders", json=body, status=status)
            with pytest.raises(expected) as ei:
                broker.place_order(SYMBOL, OrderSide.BUY, 1.0, OrderType.LIMIT, price=10.0)
            assert len(rsps.calls) == 1  # 주문은 재시도하지 않는다
        assert ei.value.status_code == status
        assert ei.value.payload == body
        assert body["message"] in str(ei.value)

    def test_order_response_bad_shape(self, broker: AlpacaBroker) -> None:
        with responses.RequestsMock() as rsps:
            rsps.post(f"{PAPER_BASE_URL}/v2/orders", json={"message": "ok"})
            with pytest.raises(BrokerError):
                broker.place_order(SYMBOL, OrderSide.BUY, 1.0, OrderType.LIMIT, price=10.0)


class TestOrderLifecycle:
    def test_cancel_order(self, broker: AlpacaBroker) -> None:
        oid = ORDER_EXAMPLE["id"]
        with responses.RequestsMock() as rsps:
            rsps.delete(f"{PAPER_BASE_URL}/v2/orders/{oid}", status=204)
            assert broker.cancel_order(oid) is True
            assert rsps.calls[0].request.method == "DELETE"
            assert rsps.calls[0].request.headers[HEADER_KEY_ID] == KEY
            rsps.delete(
                f"{PAPER_BASE_URL}/v2/orders/{oid}",
                json={"code": 42210000, "message": "order is not cancelable"},
                status=422,
            )
            assert broker.cancel_order(oid, SYMBOL) is False
            rsps.delete(
                f"{PAPER_BASE_URL}/v2/orders/{oid}",
                json={"code": 40410000, "message": "order not found"},
                status=404,
            )
            with pytest.raises(OrderError):
                broker.cancel_order(oid)
            rsps.delete(f"{PAPER_BASE_URL}/v2/orders/{oid}", json={"message": "unauthorized"}, status=401)
            with pytest.raises(AuthenticationError):
                broker.cancel_order(oid)
            rsps.delete(f"{PAPER_BASE_URL}/v2/orders/{oid}", json={"message": "rate"}, status=429)
            with pytest.raises(RateLimitError):
                broker.cancel_order(oid)

    def test_get_order(self, broker: AlpacaBroker, candles: list[Candle]) -> None:
        oid = ORDER_EXAMPLE["id"]
        fill = candles[-1].close
        with responses.RequestsMock() as rsps:
            rsps.get(
                f"{PAPER_BASE_URL}/v2/orders/{oid}",
                json=order_response(status="partially_filled", filled_qty="1", filled_avg_price=str(fill)),
            )
            order = broker.get_order(oid, SYMBOL)
            rsps.get(
                f"{PAPER_BASE_URL}/v2/orders/{oid}",
                json={"code": 40410000, "message": "order not found"},
                status=404,
            )
            with pytest.raises(OrderError):
                broker.get_order(oid)
        assert order.status == OrderStatus.PARTIALLY_FILLED
        assert order.filled_quantity == 1.0
        assert order.average_price == fill
        assert order.remaining_quantity == 1.0
        assert order.filled_value == pytest.approx(fill)

    def test_get_open_orders(self, broker: AlpacaBroker) -> None:
        other = order_response(id="other-id", symbol="MSFT", status="new")
        with responses.RequestsMock() as rsps:
            rsps.get(f"{PAPER_BASE_URL}/v2/orders", json=[ORDER_EXAMPLE, other])
            orders = broker.get_open_orders()
            q = query_of(rsps.calls[0])
            assert q == {"status": "open", "limit": "500", "direction": "desc"}
            rsps.get(f"{PAPER_BASE_URL}/v2/orders", json=[ORDER_EXAMPLE])
            only = broker.get_open_orders(SYMBOL)
            assert query_of(rsps.calls[1])["symbols"] == SYMBOL
            rsps.get(f"{PAPER_BASE_URL}/v2/orders", json={"not": "a list"})
            with pytest.raises(BrokerError):
                broker.get_open_orders()
        assert [o.id for o in orders] == [ORDER_EXAMPLE["id"], "other-id"]
        assert all(o.status == OrderStatus.OPEN for o in orders)
        assert [o.symbol for o in only] == [SYMBOL]


# ---------------------------------------------------------------------------
# 파싱 / 상태 매핑
# ---------------------------------------------------------------------------
class TestParsing:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            ("new", OrderStatus.OPEN),
            ("accepted", OrderStatus.OPEN),
            ("pending_new", OrderStatus.OPEN),
            ("accepted_for_bidding", OrderStatus.OPEN),
            ("pending_cancel", OrderStatus.OPEN),
            ("pending_replace", OrderStatus.OPEN),
            ("done_for_day", OrderStatus.OPEN),
            ("stopped", OrderStatus.OPEN),
            ("suspended", OrderStatus.OPEN),
            ("calculated", OrderStatus.OPEN),
            ("held", OrderStatus.OPEN),
            ("partially_filled", OrderStatus.PARTIALLY_FILLED),
            ("filled", OrderStatus.FILLED),
            ("canceled", OrderStatus.CANCELED),
            ("replaced", OrderStatus.CANCELED),
            ("expired", OrderStatus.EXPIRED),
            ("rejected", OrderStatus.REJECTED),
            ("unknown_status", OrderStatus.PENDING),
            (None, OrderStatus.PENDING),
        ],
    )
    def test_status_mapping(self, broker: AlpacaBroker, status: str | None, expected: OrderStatus) -> None:
        order = broker._parse_order(order_response(status=status))
        assert order.status == expected
        if status in STATUS_MAP:
            assert STATUS_MAP[status] == expected

    def test_all_documented_statuses_mapped(self) -> None:
        documented = {
            "new",
            "partially_filled",
            "filled",
            "done_for_day",
            "canceled",
            "expired",
            "replaced",
            "pending_cancel",
            "pending_replace",
            "accepted",
            "pending_new",
            "accepted_for_bidding",
            "stopped",
            "rejected",
            "suspended",
            "calculated",
            "held",
        }
        assert documented == set(STATUS_MAP)

    def test_open_with_fills_is_partial(self, broker: AlpacaBroker) -> None:
        assert (
            broker._parse_order(order_response(status="new", filled_qty="1")).status
            == OrderStatus.PARTIALLY_FILLED
        )
        assert (
            broker._parse_order(order_response(status="canceled", filled_qty="1")).status
            == OrderStatus.CANCELED
        )

    def test_notional_order_quantity_from_fill(self, broker: AlpacaBroker, candles: list[Candle]) -> None:
        fill = candles[-1].close
        o = broker._parse_order(
            order_response(
                type="market",
                order_type="market",
                qty=None,
                notional="500",
                filled_qty="2.5",
                filled_avg_price=str(fill),
                status="filled",
                limit_price=None,
            )
        )
        assert o.quantity == 2.5 and o.filled_quantity == 2.5 and o.average_price == fill
        assert o.type == OrderType.MARKET and o.price is None

    def test_stop_types_and_missing_fields(self, broker: AlpacaBroker) -> None:
        o = broker._parse_order(
            order_response(type="stop_limit", order_type="stop_limit", stop_price="140", limit_price="139")
        )
        assert o.type == OrderType.STOP and o.price is None
        o = broker._parse_order(
            order_response(type="trailing_stop", order_type="trailing_stop", trail_percent="1")
        )
        assert o.type == OrderType.STOP
        o = broker._parse_order(
            order_response(created_at=None, submitted_at=None, updated_at=None, filled_at=None)
        )
        assert o.created_at.tzinfo is timezone.utc and o.updated_at is None
        o = broker._parse_order(
            order_response(created_at=None, updated_at=None, filled_at="2023-12-12T22:31:25.5Z")
        )
        assert o.created_at == datetime(2023, 12, 12, 22, 31, 24, 577215, tzinfo=timezone.utc)
        assert o.updated_at == datetime(2023, 12, 12, 22, 31, 25, 500000, tzinfo=timezone.utc)
        with pytest.raises(BrokerError):
            broker._parse_order(order_response(side="short"))
        with pytest.raises(BrokerError):
            broker._parse_order(order_response(id=None))
        with pytest.raises(BrokerError):
            broker._parse_order(["not", "a", "dict"])

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("2022-01-03T09:00:00Z", datetime(2022, 1, 3, 9, 0, tzinfo=timezone.utc)),
            ("2022-08-17T09:53:16.845580544Z", datetime(2022, 8, 17, 9, 53, 16, 845580, tzinfo=timezone.utc)),
            ("2025-06-24T16:00:00-04:00", datetime(2025, 6, 24, 20, 0, tzinfo=timezone.utc)),
            ("2023-12-12T22:31:24.5Z", datetime(2023, 12, 12, 22, 31, 24, 500000, tzinfo=timezone.utc)),
            ("2023-12-12T22:31:24", datetime(2023, 12, 12, 22, 31, 24, tzinfo=timezone.utc)),
            ("2024-01-03", datetime(2024, 1, 3, tzinfo=timezone.utc)),
        ],
    )
    def test_parse_rfc3339(self, value: str, expected: datetime) -> None:
        got = parse_rfc3339(value)
        assert got == expected and got.tzinfo is timezone.utc

    def test_parse_rfc3339_invalid(self) -> None:
        for bad in ("", "not-a-date", None, 123):
            with pytest.raises(ValueError):
                parse_rfc3339(bad)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("qty", "expected"),
        [
            (5, "5"),
            (5.0, "5"),
            (3.654, "3.654"),
            (0.1234567891, "0.123456789"),
            (1e-9, "0.000000001"),
            (100.0, "100"),
            (2.5000000001, "2.5"),
        ],
    )
    def test_format_quantity(self, qty: float, expected: str) -> None:
        assert format_quantity(qty) == expected
