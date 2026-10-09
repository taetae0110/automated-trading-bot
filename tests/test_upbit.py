"""UpbitBroker 테스트.

- 비공개 API(계좌/주문)는 공식 문서 예시 응답 스키마 그대로 `responses` 로 모킹한다 (모킹 데이터는 이 파일 안에만).
- 시세는 가짜 데이터를 만들지 않는다: conftest 의 실제 Upbit 캔들(`real_btc_hourly_raw`) 과 이 파일의
  실제 현재가(`real_btc_ticker_raw`) 를 받아 캐시한 뒤 모킹 서버로 재생하거나, 공개 API 를 직접 호출한다
  (네트워크 없으면 skip).
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
import warnings
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlsplit

import jwt
import pytest
import requests
import responses
from freezegun import freeze_time

from tradingbot.brokers import create_broker, get_broker_class
from tradingbot.brokers.upbit import (
    CANDLE_PATHS,
    KRW_MIN_ORDER_VALUE,
    RATE_LIMITS,
    UpbitBroker,
    _RateLimiter,
    build_query_string,
    krw_tick_size,
    parse_remaining_req,
    query_hash,
    tick_size,
    usdt_tick_size,
)
from tradingbot.config import AppConfig, BrokerConfig
from tradingbot.exceptions import (
    AuthenticationError,
    BrokerError,
    ConfigError,
    DataError,
    InsufficientFunds,
    OrderError,
    RateLimitError,
)
from tradingbot.models import OrderSide, OrderStatus, OrderType, utcnow

BASE = "https://api.upbit.com"
ACCESS = "test-access-key"
SECRET = "test-secret-key-for-unit-tests"

# ----------------------------------------------------------------------------- 공식 문서 예시 응답 (비공개 API)
# 전체 계좌 조회 (GET /v1/accounts) 예시
ACCOUNTS_EXAMPLE = [
    {
        "currency": "KRW",
        "balance": "1000000.0",
        "locked": "0.0",
        "avg_buy_price": "0",
        "avg_buy_price_modified": False,
        "unit_currency": "KRW",
    },
    {
        "currency": "BTC",
        "balance": "2.0",
        "locked": "0.0",
        "avg_buy_price": "140000000",
        "avg_buy_price_modified": False,
        "unit_currency": "KRW",
    },
]

# 주문 가능 정보 (GET /v1/orders/chance) 예시
CHANCE_EXAMPLE = {
    "bid_fee": "0.0005",
    "ask_fee": "0.0005",
    "maker_bid_fee": "0.0005",
    "maker_ask_fee": "0.0005",
    "market": {
        "id": "KRW-BTC",
        "name": "BTC/KRW",
        "order_types": ["limit"],
        "order_sides": ["ask", "bid"],
        "bid_types": ["best_fok", "best_ioc", "limit", "limit_fok", "limit_ioc", "price"],
        "ask_types": ["best_fok", "best_ioc", "limit", "limit_fok", "limit_ioc", "market"],
        "bid": {"currency": "KRW", "min_total": "5000"},
        "ask": {"currency": "BTC", "min_total": "5000"},
        "max_total": "1000000000",
        "state": "active",
    },
    "bid_account": {
        "currency": "KRW",
        "balance": "10000",
        "locked": "0",
        "avg_buy_price": "0",
        "avg_buy_price_modified": True,
        "unit_currency": "KRW",
    },
    "ask_account": {
        "currency": "BTC",
        "balance": "0.001",
        "locked": "0",
        "avg_buy_price": "140000000",
        "avg_buy_price_modified": False,
        "unit_currency": "KRW",
    },
}

# 주문 취소 접수 / 체결 대기 주문 조회 예시 (지정가 매수, wait)
ORDER_LIMIT_WAIT = {
    "uuid": "cdd92199-2897-4e14-9448-f923320408ad",
    "side": "bid",
    "ord_type": "limit",
    "price": "140000000",
    "state": "wait",
    "market": "KRW-BTC",
    "created_at": "2025-07-04T15:00:00+09:00",
    "volume": "1.0",
    "remaining_volume": "1.0",
    "executed_volume": "0.0",
    "reserved_fee": "70000.0",
    "remaining_fee": "70000.0",
    "paid_fee": "0.0",
    "locked": "140070000.0",
    "prevented_volume": "0",
    "prevented_locked": "0",
    "trades_count": 0,
}

# 개별 주문 조회 (GET /v1/order) 예시 (시장가 매도, done, trades 포함)
ORDER_MARKET_DONE = {
    "market": "KRW-USDT",
    "uuid": "3b67e543-8ad3-48d0-8451-0dad315cae73",
    "side": "ask",
    "ord_type": "market",
    "state": "done",
    "created_at": "2025-08-09T16:44:00+09:00",
    "volume": "5.377594",
    "remaining_volume": "0",
    "executed_volume": "5.377594",
    "reserved_fee": "0",
    "remaining_fee": "0",
    "paid_fee": "3.697095875",
    "locked": "0",
    "prevented_volume": "0",
    "prevented_locked": "0",
    "trades_count": 1,
    "trades": [
        {
            "market": "KRW-USDT",
            "uuid": "795dff29-bba6-49b2-baab-63473ab7931c",
            "price": "1375",
            "volume": "5.377594",
            "funds": "7394.19175",
            "trend": "down",
            "created_at": "2025-08-09T16:44:00.597751+09:00",
            "side": "ask",
        }
    ],
}


# 개별 주문 조회 (GET /v1/order) 형식의 시장가 매수(금액 주문, ord_type=price). 공식 문서(주문 목록 조회의 state 설명):
# "시장가 매수 주문은 체결 후 잔량이 발생하는 경우 cancel, 딱 맞아떨어지는 경우 done" — 5,000 KRW 주문이 0.00003 BTC
# (4,800 KRW) 체결되고 호가 단위 미만의 잔돈 200 KRW 가 취소되어 state=cancel 로 끝난 정상 완료 주문. 금액 주문이라
# volume 은 없고 price 는 주문 총액이다. identifier 는 2024-10-18 이후 생성된 주문에만 제공되는 응답 필드.
ORDER_PRICE_CANCEL_FILLED = {
    "market": "KRW-BTC",
    "uuid": "9ca023a5-851b-4fec-9f0a-48cd83c2eaae",
    "side": "bid",
    "ord_type": "price",
    "price": "5000",
    "state": "cancel",
    "created_at": "2025-08-09T16:44:00+09:00",
    "remaining_volume": "0",
    "executed_volume": "0.00003",
    "reserved_fee": "2.5",
    "remaining_fee": "0.1",
    "paid_fee": "2.4",
    "locked": "0",
    "prevented_volume": "0",
    "prevented_locked": "0",
    "trades_count": 1,
    "identifier": "e3b2b7a4-0d1c-4d7e-9a4e-6f5e2c1d0b9a",
    "trades": [
        {
            "market": "KRW-BTC",
            "uuid": "795dff29-bba6-49b2-baab-63473ab7931c",
            "price": "160000000",
            "volume": "0.00003",
            "funds": "4800",
            "trend": "up",
            "created_at": "2025-08-09T16:44:00.597751+09:00",
            "side": "bid",
        }
    ],
}

# POST /v1/orders 의 403 응답 예시 (공식 문서 new-order, "market offline error")
MARKET_OFFLINE_403 = {
    "error": {
        "name": "market_offline",
        "message": "시스템 점검 중입니다. 점검이 완료된 후에 다시 시도해주세요.",
    }
}


def error_body(name: str | int, message: str = "") -> dict:
    """공식 오류 본문 형식 {"error": {"name", "message"}}."""
    return {"error": {"name": name, "message": message}}


def pop_identifier(body: dict) -> str:
    """주문 본문의 클라이언트 주문 ID(identifier) 를 꺼내 검증하고 돌려준다 (uuid4, 최대 64자)."""
    identifier = body.pop("identifier")
    assert isinstance(identifier, str) and len(identifier) <= 64
    assert uuid.UUID(identifier).version == 4
    return identifier


# ----------------------------------------------------------------------------- 픽스처
@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """호출 간격 제한기/재시도 백오프가 테스트를 느리게 하지 않도록."""
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)


@pytest.fixture
def broker() -> UpbitBroker:
    return UpbitBroker(ACCESS, SECRET)


@pytest.fixture
def public_broker() -> UpbitBroker:
    return UpbitBroker()


@pytest.fixture(scope="session")
def real_btc_ticker_raw(request) -> list[dict]:
    """실제 KRW-BTC 현재가 원본 응답 (Upbit 공개 API, pytest 캐시)."""
    key = "tradingbot/upbit/ticker/KRW-BTC"
    cache = getattr(request.config, "cache", None)  # -p no:cacheprovider 환경 대비
    cached = cache.get(key, None) if cache is not None else None
    if cached:
        return cached
    try:
        resp = requests.get(f"{BASE}/v1/ticker", params={"markets": "KRW-BTC"}, timeout=10)
        resp.raise_for_status()
        raw = resp.json()
        if not isinstance(raw, list) or not raw:
            raise RuntimeError(f"Upbit 현재가 응답이 비었습니다: {raw!r}")
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"실제 현재가를 받을 수 없어 건너뜀 (네트워크 필요): {e}")
    if cache is not None:
        cache.set(key, json.loads(json.dumps(raw)))
    return raw


@pytest.fixture
def live_broker() -> UpbitBroker:
    """실제 공개 API 가 되는 환경에서만 (아니면 skip)."""
    b = UpbitBroker()
    try:
        b.get_ticker("KRW-BTC")
    except BrokerError as e:
        pytest.skip(f"Upbit 공개 API 에 접근할 수 없어 건너뜀: {e}")
    return b


def decode_token(req: requests.PreparedRequest, algorithm: str = "HS512") -> dict:
    auth = req.headers["Authorization"]
    assert auth.startswith("Bearer ")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # 테스트용 짧은 키에 대한 PyJWT 키 길이 경고
        return jwt.decode(auth[len("Bearer ") :], SECRET, algorithms=[algorithm])


# ============================================================================= 생성/설정
class TestConstruction:
    def test_registry_and_defaults(self):
        assert get_broker_class("upbit") is UpbitBroker
        b = UpbitBroker()
        assert b.name == "upbit"
        assert b.asset_class.value == "crypto"
        assert not b.has_credentials
        assert b.base_url == BASE
        assert b.jwt_algorithm == "HS512"
        assert set(b.supported_intervals) == set(CANDLE_PATHS)
        assert b.is_market_open() is True

    def test_from_config_reads_env(self, monkeypatch):
        monkeypatch.setenv("UPBIT_ACCESS_KEY", ACCESS)
        monkeypatch.setenv("UPBIT_SECRET_KEY", SECRET)
        cfg = AppConfig(broker=BrokerConfig(name="upbit", extra={"timeout": 5, "jwt_algorithm": "HS256"}))
        b = create_broker("upbit", cfg)
        assert isinstance(b, UpbitBroker)
        assert b.has_credentials
        assert b.timeout == 5.0
        assert b.jwt_algorithm == "HS256"

    def test_from_config_without_keys_is_public_only(self, monkeypatch):
        monkeypatch.delenv("UPBIT_ACCESS_KEY", raising=False)
        monkeypatch.delenv("UPBIT_SECRET_KEY", raising=False)
        b = UpbitBroker.from_config(AppConfig(broker=BrokerConfig(name="upbit")))
        assert not b.has_credentials
        with pytest.raises(AuthenticationError) as ei:
            b.get_balances()
        assert "UPBIT_ACCESS_KEY" in str(ei.value) and "UPBIT_SECRET_KEY" in str(ei.value)

    def test_private_methods_require_credentials(self, public_broker):
        for call in (
            lambda: public_broker.get_positions(),
            lambda: public_broker.place_order("KRW-BTC", OrderSide.BUY, 0.001),
            lambda: public_broker.cancel_order("x"),
            lambda: public_broker.get_order("x"),
            lambda: public_broker.get_open_orders(),
            lambda: public_broker.get_order_chance("KRW-BTC"),
        ):
            with pytest.raises(AuthenticationError):
                call()

    def test_empty_strings_are_not_credentials(self):
        assert not UpbitBroker("", "").has_credentials
        assert not UpbitBroker(ACCESS, None).has_credentials

    def test_bad_jwt_algorithm(self):
        with pytest.raises(ConfigError):
            UpbitBroker(ACCESS, SECRET, jwt_algorithm="RS256")

    def test_symbol_helpers(self, public_broker):
        assert public_broker.quote_currency("KRW-BTC") == "KRW"
        assert public_broker.base_currency("KRW-BTC") == "BTC"
        assert public_broker.quote_currency("BTC-ETH") == "BTC"
        assert public_broker.base_currency("USDT-XRP") == "XRP"
        for bad in ("BTC", "KRW-", "-BTC", "KRW-BTC-X"):
            with pytest.raises(BrokerError):
                public_broker.quote_currency(bad)

    def test_close_only_owned_session(self):
        own = UpbitBroker()
        own.close()  # 예외 없음
        shared = UpbitBroker(client=UpbitBroker()._client)
        shared.close()


# ============================================================================= JWT / query_hash
class TestJwt:
    def test_build_query_string_matches_official_rules(self):
        params = {"market": "KRW-BTC", "states[]": ["wait", "watch"], "limit": 100}
        assert build_query_string(params) == "market=KRW-BTC&states[]=wait&states[]=watch&limit=100"
        assert build_query_string({"markets": "KRW-BTC,KRW-ETH"}) == "markets=KRW-BTC,KRW-ETH"
        assert build_query_string({"to": "2025-06-24T04:56:53Z"}) == "to=2025-06-24T04:56:53Z"
        assert query_hash(params) == hashlib.sha512(build_query_string(params).encode()).hexdigest()

    def test_token_without_params_has_no_query_hash(self, broker):
        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/accounts", json=ACCOUNTS_EXAMPLE)
            broker.get_balances()
            req = rsps.calls[0].request
            payload = decode_token(req)
        assert payload["access_key"] == ACCESS
        assert uuid.UUID(payload["nonce"]).version == 4
        assert "query_hash" not in payload and "query_hash_alg" not in payload
        assert req.url == f"{BASE}/v1/accounts"

    def test_get_query_hash_matches_sent_query(self, broker):
        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/orders/open", json=[ORDER_LIMIT_WAIT])
            broker.get_open_orders("KRW-BTC")
            req = rsps.calls[0].request
            payload = decode_token(req)
        sent_query = urlsplit(req.url).query
        # 배열은 key[]=v 반복, URL 인코딩된 형태로 전송
        assert (
            sent_query == "market=KRW-BTC&states%5B%5D=wait&states%5B%5D=watch&page=1&limit=100&order_by=asc"
        )
        expected = hashlib.sha512(unquote(sent_query).encode()).hexdigest()
        assert payload["query_hash"] == expected
        assert payload["query_hash_alg"] == "SHA512"
        assert len(payload["query_hash"]) == 128

    def test_post_query_hash_is_over_json_body(self, broker):
        with responses.RequestsMock() as rsps:
            rsps.post(f"{BASE}/v1/orders", json=ORDER_LIMIT_WAIT, status=201)
            rsps.get(f"{BASE}/v1/order", json=ORDER_LIMIT_WAIT)
            broker.place_order("KRW-BTC", OrderSide.BUY, 1.0, OrderType.LIMIT, price=140_000_000)
            req = rsps.calls[0].request
            payload = decode_token(req)
        body = json.loads(req.body)
        assert req.headers["Content-Type"] == "application/json"
        assert payload["query_hash"] == query_hash(body)  # identifier 를 포함한 본문 전체가 해시 대상
        assert payload["query_hash_alg"] == "SHA512"
        pop_identifier(body)
        assert body == {
            "market": "KRW-BTC",
            "side": "bid",
            "ord_type": "limit",
            "volume": "1",
            "price": "140000000",
        }

    def test_nonce_is_fresh_per_request(self, broker):
        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/accounts", json=ACCOUNTS_EXAMPLE)
            broker.get_balances()
            broker.get_balances()
            n1 = decode_token(rsps.calls[0].request)["nonce"]
            n2 = decode_token(rsps.calls[1].request)["nonce"]
        assert n1 != n2

    def test_retry_after_5xx_mints_fresh_nonce_per_attempt(self, broker):
        """공식 문서: nonce 는 같은 요청을 반복해도 매번 새 값이어야 한다 (재사용 → 401 nonce_used).
        HttpClient 의 GET 재시도 규칙은 유지하되 시도마다 새 JWT 를 보내야 한다."""
        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/accounts", json={}, status=500)
            rsps.get(f"{BASE}/v1/accounts", json=ACCOUNTS_EXAMPLE)
            balances = broker.get_balances()
            assert len(rsps.calls) == 2
            auths = [c.request.headers["Authorization"] for c in rsps.calls]
            payloads = [decode_token(c.request) for c in rsps.calls]
        assert balances["KRW"].total == 1_000_000.0
        assert auths[0] != auths[1]
        assert payloads[0]["nonce"] != payloads[1]["nonce"]
        assert all(uuid.UUID(p["nonce"]).version == 4 for p in payloads)

    def test_retry_keeps_query_hash_but_changes_nonce(self, broker):
        """쿼리가 있는 GET(/v1/order 폴링) 도 재시도마다 nonce 만 바뀌고 query_hash 는 같은 쿼리의 해시여야 한다."""
        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/order", json={}, status=503)
            rsps.get(f"{BASE}/v1/order", json=error_body(429, "Too Many Requests"), status=429)
            rsps.get(f"{BASE}/v1/order", json=ORDER_MARKET_DONE)
            order = broker.get_order(ORDER_MARKET_DONE["uuid"], "KRW-USDT")
            assert len(rsps.calls) == 3
            payloads = [decode_token(c.request) for c in rsps.calls]
            urls = {c.request.url for c in rsps.calls}
        assert order.status == OrderStatus.FILLED
        assert len(urls) == 1 and urlsplit(urls.pop()).query == f"uuid={ORDER_MARKET_DONE['uuid']}"
        assert len({p["nonce"] for p in payloads}) == 3
        assert {p["query_hash"] for p in payloads} == {query_hash({"uuid": ORDER_MARKET_DONE["uuid"]})}

    def test_retry_after_network_error_mints_fresh_nonce(self, broker):
        """읽기 타임아웃은 서버가 이미 nonce 를 소비했을 수 있는 경우라 더더욱 새 토큰이어야 한다."""
        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/accounts", body=requests.ReadTimeout("read timed out"))
            rsps.get(f"{BASE}/v1/accounts", json=ACCOUNTS_EXAMPLE)
            broker.get_balances()
            nonces = [decode_token(c.request)["nonce"] for c in rsps.calls]
        assert len(nonces) == 2 and nonces[0] != nonces[1]

    def test_private_get_retry_budget_matches_http_client(self, broker):
        """재시도 횟수는 HttpClient(max_retries) 와 같고, 모든 시도의 nonce 가 서로 다르다. 끝까지 429 면 RateLimitError."""
        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/accounts", json=error_body("too_many_requests", "limit"), status=429)
            with pytest.raises(RateLimitError):
                broker.get_balances()
            attempts = len(rsps.calls)
            nonces = {decode_token(c.request)["nonce"] for c in rsps.calls}
        assert attempts == broker._client.max_retries + 1 == 4
        assert len(nonces) == attempts

    def test_private_post_and_delete_are_single_shot(self, broker):
        """주문/취소는 중복 접수 위험 때문에 재시도하지 않는다 (HttpClient 규칙과 동일)."""
        with responses.RequestsMock() as rsps:
            rsps.post(f"{BASE}/v1/orders", json={}, status=503)
            with pytest.raises(BrokerError):
                broker.place_order("KRW-BTC", OrderSide.BUY, 1, OrderType.LIMIT, price=140_000_000)
            assert len(rsps.calls) == 1
        with responses.RequestsMock() as rsps:
            rsps.delete(f"{BASE}/v1/order", json={}, status=503)
            with pytest.raises(BrokerError):
                broker.cancel_order(ORDER_LIMIT_WAIT["uuid"])
            assert len(rsps.calls) == 1

    def test_hs256_option(self):
        b = UpbitBroker(ACCESS, SECRET, jwt_algorithm="HS256")
        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/accounts", json=ACCOUNTS_EXAMPLE)
            b.get_balances()
            req = rsps.calls[0].request
            payload = decode_token(req, algorithm="HS256")
        assert payload["access_key"] == ACCESS
        with pytest.raises(jwt.InvalidAlgorithmError):
            decode_token(req, algorithm="HS512")

    def test_signing_emits_no_key_length_warning(self, broker):
        """업비트 Secret Key(40자)는 HS512 권장 길이(64바이트) 미만이라 PyJWT 가 경고하지만, 로그를 더럽히면 안 된다."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            token = broker._jwt({"market": "KRW-BTC"})
        assert isinstance(token, str) and token.count(".") == 2
        assert caught == []

    def test_token_rejects_wrong_secret(self, broker):
        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/accounts", json=ACCOUNTS_EXAMPLE)
            broker.get_balances()
            auth = rsps.calls[0].request.headers["Authorization"]
        with pytest.raises(jwt.InvalidSignatureError), warnings.catch_warnings():
            warnings.simplefilter("ignore")
            jwt.decode(auth.split(" ", 1)[1], "other-secret", algorithms=["HS512"])


# ============================================================================= 캔들
class TestCandles:
    def test_interval_paths(self):
        assert CANDLE_PATHS["1m"] == "/v1/candles/minutes/1"
        assert CANDLE_PATHS["1h"] == "/v1/candles/minutes/60"
        assert CANDLE_PATHS["4h"] == "/v1/candles/minutes/240"
        assert CANDLE_PATHS["1d"] == "/v1/candles/days"
        assert CANDLE_PATHS["1w"] == "/v1/candles/weeks"
        assert "2h" not in CANDLE_PATHS

    def test_unsupported_interval(self, public_broker):
        with pytest.raises(DataError):
            public_broker.get_candles("KRW-BTC", "2h")

    def test_ordering_and_partial_exclusion(self, public_broker, real_btc_hourly_raw):
        newest = datetime.fromisoformat(real_btc_hourly_raw[0]["candle_date_time_utc"]).replace(
            tzinfo=timezone.utc
        )
        n = len(real_btc_hourly_raw)
        # 최신 캔들이 시작한 지 30분: 아직 진행 중 → 제외
        with freeze_time(newest + timedelta(minutes=30)), responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/candles/minutes/60", json=real_btc_hourly_raw)
            candles = public_broker.get_candles("KRW-BTC", "1h", limit=n)
            sent = parse_qs(urlsplit(rsps.calls[0].request.url).query)
        assert sent["market"] == ["KRW-BTC"] and sent["count"] == [str(min(n + 1, 200))]
        assert "to" not in sent
        assert len(candles) == n - 1
        assert candles[-1].timestamp == newest - timedelta(hours=1)
        assert all(a.timestamp < b.timestamp for a, b in zip(candles, candles[1:], strict=False))
        assert all(c.timestamp.tzinfo is timezone.utc for c in candles)
        # 정확히 1시간 지난 순간: 시작 + 간격 <= now → 완성으로 포함
        with freeze_time(newest + timedelta(hours=1)), responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/candles/minutes/60", json=real_btc_hourly_raw)
            candles = public_broker.get_candles("KRW-BTC", "1h", limit=n)
        assert len(candles) == n and candles[-1].timestamp == newest
        # include_partial=True 는 시간과 무관하게 전부
        with freeze_time(newest + timedelta(minutes=1)), responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/candles/minutes/60", json=real_btc_hourly_raw)
            candles = public_broker.get_candles("KRW-BTC", "1h", limit=n, include_partial=True)
            sent = parse_qs(urlsplit(rsps.calls[0].request.url).query)
        assert sent["count"] == [str(n)]
        assert len(candles) == n

    def test_values_match_raw(self, public_broker, real_btc_hourly_raw):
        newest = datetime.fromisoformat(real_btc_hourly_raw[0]["candle_date_time_utc"]).replace(
            tzinfo=timezone.utc
        )
        with freeze_time(newest + timedelta(hours=2)), responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/candles/minutes/60", json=real_btc_hourly_raw)
            candles = public_broker.get_candles("KRW-BTC", "1h", limit=len(real_btc_hourly_raw))
        by_ts = {c.timestamp: c for c in candles}
        for r in real_btc_hourly_raw:
            ts = datetime.fromisoformat(r["candle_date_time_utc"]).replace(tzinfo=timezone.utc)
            c = by_ts[ts]
            assert c.open == float(r["opening_price"]) and c.high == float(r["high_price"])
            assert c.low == float(r["low_price"]) and c.close == float(r["trade_price"])
            assert c.volume == float(r["candle_acc_trade_volume"])

    def test_limit_trimming_and_to_param(self, public_broker, real_btc_hourly_raw):
        newest = datetime.fromisoformat(real_btc_hourly_raw[0]["candle_date_time_utc"]).replace(
            tzinfo=timezone.utc
        )
        end = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=9)))  # KST → UTC 변환 확인
        with freeze_time(newest + timedelta(days=1)), responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/candles/minutes/60", json=real_btc_hourly_raw)
            candles = public_broker.get_candles("KRW-BTC", "1h", limit=5, end=end)
            sent = parse_qs(urlsplit(rsps.calls[0].request.url).query)
        assert sent["count"] == ["6"] and sent["to"] == ["2026-01-01T18:04:05Z"]
        assert len(candles) == 5
        assert candles[-1].timestamp == newest  # 가장 최신 쪽 5개를 남긴다

    @staticmethod
    def _pager(raw: list[dict]):
        """실제 응답(최신→과거)을 Upbit 처럼 `count`/`to`(배타적) 로 잘라 주는 responses 콜백."""

        def cb(request):
            qs = parse_qs(urlsplit(request.url).query)
            count = int(qs["count"][0])
            rows = raw
            if "to" in qs:
                to_dt = datetime.strptime(qs["to"][0], "%Y-%m-%dT%H:%M:%SZ")
                rows = [r for r in rows if datetime.fromisoformat(r["candle_date_time_utc"]) < to_dt]
            return (200, {}, json.dumps(rows[:count]))

        return cb

    def test_limit_over_page_size_paginates_backwards(self, public_broker, real_btc_hourly_raw, monkeypatch):
        """limit > 페이지 크기면 `to` 를 가장 오래된 캔들로 옮기며 과거 방향으로 이어 받는다."""
        import tradingbot.brokers.upbit as upbit_mod

        monkeypatch.setattr(upbit_mod, "MAX_CANDLE_COUNT", 60)
        newest = datetime.fromisoformat(real_btc_hourly_raw[0]["candle_date_time_utc"]).replace(
            tzinfo=timezone.utc
        )
        with freeze_time(newest + timedelta(days=1)), responses.RequestsMock() as rsps:
            rsps.add_callback(
                responses.GET, f"{BASE}/v1/candles/minutes/60", callback=self._pager(real_btc_hourly_raw)
            )
            candles = public_broker.get_candles("KRW-BTC", "1h", limit=150, include_partial=True)
            sent = [parse_qs(urlsplit(c.request.url).query) for c in rsps.calls]
        assert len(sent) == 3
        assert sent[0]["count"] == ["60"] and "to" not in sent[0]
        # 2·3번째 요청의 to = 직전 페이지의 가장 오래된 캔들 시각 (배타적이므로 겹침 없음)
        assert sent[1]["count"] == ["60"]
        assert sent[1]["to"] == [(newest - timedelta(hours=59)).strftime("%Y-%m-%dT%H:%M:%SZ")]
        assert sent[2]["count"] == ["30"]
        assert sent[2]["to"] == [(newest - timedelta(hours=119)).strftime("%Y-%m-%dT%H:%M:%SZ")]
        expected = sorted(
            datetime.fromisoformat(r["candle_date_time_utc"]).replace(tzinfo=timezone.utc)
            for r in real_btc_hourly_raw[:150]
        )
        assert [c.timestamp for c in candles] == expected
        assert len({c.timestamp for c in candles}) == 150

    def test_pagination_stops_when_history_is_exhausted_and_zero_limit(
        self, public_broker, real_btc_hourly_raw
    ):
        """히스토리가 limit 보다 짧으면 빈 페이지에서 멈추고 받은 만큼만 돌려준다. limit=0 은 호출 없이 []."""
        newest = datetime.fromisoformat(real_btc_hourly_raw[0]["candle_date_time_utc"]).replace(
            tzinfo=timezone.utc
        )
        n = len(real_btc_hourly_raw)
        with freeze_time(newest + timedelta(days=1)), responses.RequestsMock() as rsps:
            rsps.add_callback(
                responses.GET, f"{BASE}/v1/candles/minutes/60", callback=self._pager(real_btc_hourly_raw)
            )
            candles = public_broker.get_candles("KRW-BTC", "1h", limit=1000)
            sent = [parse_qs(urlsplit(c.request.url).query) for c in rsps.calls]
            assert public_broker.get_candles("KRW-BTC", "1h", limit=0) == []
            assert len(rsps.calls) == 2
        assert sent[0]["count"] == ["200"] and "to" not in sent[0]
        oldest = datetime.fromisoformat(real_btc_hourly_raw[-1]["candle_date_time_utc"])
        assert sent[1]["to"] == [oldest.strftime("%Y-%m-%dT%H:%M:%SZ")]
        assert len(candles) == n
        assert all(a.timestamp < b.timestamp for a, b in zip(candles, candles[1:], strict=False))

    def test_short_page_means_last_page(self, public_broker, real_btc_hourly_raw):
        """요청한 count 보다 적게 오면 더 이상 과거 데이터가 없다는 뜻이므로 추가 요청을 하지 않는다."""
        newest = datetime.fromisoformat(real_btc_hourly_raw[0]["candle_date_time_utc"]).replace(
            tzinfo=timezone.utc
        )
        with freeze_time(newest + timedelta(days=1)), responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/candles/minutes/60", json=real_btc_hourly_raw[:50])
            candles = public_broker.get_candles("KRW-BTC", "1h", limit=500, include_partial=True)
            assert len(rsps.calls) == 1
        assert len(candles) == 50

    def test_weekly_partial_rule(self, public_broker, real_btc_hourly_raw):
        """주봉: 월요일 00:00 UTC 시작 + 7일 <= now 여야 완성. 실제 주봉 응답 대신 시간 규칙만 검증."""
        week_start = datetime(2026, 9, 28, tzinfo=timezone.utc)  # 월요일
        raw = [dict(real_btc_hourly_raw[0], candle_date_time_utc=week_start.strftime("%Y-%m-%dT%H:%M:%S"))]
        with freeze_time(week_start + timedelta(days=6, hours=23)), responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/candles/weeks", json=raw)
            assert public_broker.get_candles("KRW-BTC", "1w", limit=1) == []
        with freeze_time(week_start + timedelta(days=7)), responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/candles/weeks", json=raw)
            assert len(public_broker.get_candles("KRW-BTC", "1w", limit=1)) == 1

    def test_malformed_candle_raises_data_error(self, public_broker):
        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/candles/days", json=[{"market": "KRW-BTC"}])
            with pytest.raises(DataError):
                public_broker.get_candles("KRW-BTC", "1d", limit=1)
        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/candles/days", json={"unexpected": True})
            with pytest.raises(DataError):
                public_broker.get_candles("KRW-BTC", "1d", limit=1)

    def test_unknown_market_404(self, public_broker):
        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/candles/days", json=error_body(404, "Code not found"), status=404)
            with pytest.raises(BrokerError) as ei:
                public_broker.get_candles("KRW-NOPE", "1d", limit=1)
        assert ei.value.status_code == 404 and "Code not found" in str(ei.value)
        assert not isinstance(ei.value, (OrderError, AuthenticationError))


# ============================================================================= 현재가
class TestTicker:
    def test_get_ticker_and_batch(self, public_broker, real_btc_ticker_raw):
        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/ticker", json=real_btc_ticker_raw)
            price = public_broker.get_ticker("KRW-BTC")
            sent = parse_qs(urlsplit(rsps.calls[0].request.url).query)
        assert sent == {"markets": ["KRW-BTC"]}
        assert price == float(real_btc_ticker_raw[0]["trade_price"]) > 0
        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/ticker", json=real_btc_ticker_raw)
            tickers = public_broker.get_tickers(["KRW-BTC", "KRW-ETH"])
            sent = parse_qs(urlsplit(rsps.calls[0].request.url).query)
        assert sent == {"markets": ["KRW-BTC,KRW-ETH"]}
        assert tickers == {"KRW-BTC": price}
        assert public_broker.get_tickers([]) == {}

    def test_missing_symbol_in_response(self, public_broker, real_btc_ticker_raw):
        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/ticker", json=real_btc_ticker_raw)
            with pytest.raises(BrokerError):
                public_broker.get_ticker("KRW-ETH")


# ============================================================================= 호가 단위 / 수량 반올림
class TestRounding:
    @pytest.mark.parametrize(
        "price,tick",
        [
            (112_724_000, 1000),
            (2_000_000, 1000),
            (1_999_999, 1000),
            (1_000_000, 1000),
            (999_999, 500),
            (500_000, 500),
            (499_999, 100),
            (100_000, 100),
            (99_999, 50),
            (50_000, 50),
            (49_999, 10),
            (10_000, 10),
            (9_999, 5),
            (5_000, 5),
            (4_999, 1),
            (1_000, 1),
            (999, 1),
            (100, 1),
            (99.9, 0.1),
            (10, 0.1),
            (9.99, 0.01),
            (1, 0.01),
            (0.999, 0.001),
            (0.1, 0.001),
            (0.0999, 0.0001),
            (0.01, 0.0001),
            (0.00999, 0.00001),
            (0.001, 0.00001),
            (0.000999, 0.000001),
            (0.0001, 0.000001),
            (0.0000999, 0.0000001),
            (0.00001, 0.0000001),
            (0.0000099, 0.00000001),
        ],
    )
    def test_krw_tick_table(self, price, tick):
        assert krw_tick_size(price) == tick
        assert tick_size("KRW-ANY", price) == tick

    def test_usdt_and_btc_ticks(self):
        assert usdt_tick_size(10) == 0.01 and usdt_tick_size(9.99) == 0.001
        assert usdt_tick_size(0.5) == 0.0001 and usdt_tick_size(0.05) == 0.00001
        assert usdt_tick_size(0.005) == 0.000001 and usdt_tick_size(0.0005) == 0.0000001
        assert usdt_tick_size(0.00005) == 0.00000001
        assert tick_size("BTC-ETH", 0.05) == 0.00000001
        assert tick_size("BTC-ETH", 123.0) == 0.00000001

    def test_round_price(self, public_broker):
        r = public_broker.round_price
        assert r("KRW-BTC", 112_724_500) == 112_725_000  # 반올림(half-up)
        assert r("KRW-BTC", 112_724_499) == 112_724_000
        assert r("KRW-XRP", 3_452.4) == 3_452
        assert r("KRW-XRP", 15.01) == 15.0
        assert r("KRW-XRP", 15.05) == 15.1
        assert r("KRW-XRP", 15.04) == 15.0
        assert r("KRW-SOL", 149_950) == 150_000
        assert r("KRW-PEPE", 0.00532) == 0.00532
        assert r("KRW-PEPE", 0.005324) == 0.00532
        assert r("USDT-DOGE", 0.123456) == 0.1235
        assert r("BTC-ETH", 0.0312345678) == 0.03123457
        with pytest.raises(OrderError):
            r("KRW-BTC", 0)

    def test_round_quantity_decimal_safe(self, public_broker):
        assert public_broker.round_quantity("KRW-BTC", 0.29) == 0.29  # 0.28999999 가 되면 안 됨
        assert public_broker.round_quantity("KRW-BTC", 1.123456789) == 1.12345678
        assert public_broker.round_quantity("KRW-BTC", 0.000000001) == 0.0
        assert public_broker.round_quantity("KRW-BTC", -1) == 0.0
        assert public_broker.round_quantity("KRW-BTC", 2) == 2.0

    def test_min_order_value_defaults(self, public_broker):
        assert public_broker.min_order_value("KRW-BTC") == KRW_MIN_ORDER_VALUE == 5000.0
        assert public_broker.min_order_value("BTC-ETH") == 0.00005
        assert public_broker.min_order_value("USDT-BTC") == 0.5

    def test_min_order_value_from_chance_cache(self, broker):
        chance = json.loads(json.dumps(CHANCE_EXAMPLE))
        chance["market"]["bid"]["min_total"] = "6000"
        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/orders/chance", json=chance)
            info = broker.get_order_chance("KRW-BTC")
            info2 = broker.get_order_chance("KRW-BTC")  # 캐시
            assert len(rsps.calls) == 1
            sent = parse_qs(urlsplit(rsps.calls[0].request.url).query)
        assert sent == {"market": ["KRW-BTC"]}
        assert info is info2
        assert info["bid_fee"] == 0.0005 and info["min_total_bid"] == 6000.0 and info["max_total"] == 1e9
        assert "price" in info["bid_types"] and "market" in info["ask_types"]
        assert broker.min_order_value("KRW-BTC") == 6000.0
        assert broker.min_order_value("KRW-ETH") == 5000.0  # 다른 마켓은 기본값


# ============================================================================= 계좌
class TestAccounts:
    def test_balances_total_includes_locked(self, broker):
        accounts = json.loads(json.dumps(ACCOUNTS_EXAMPLE))
        accounts[1]["locked"] = "0.5"
        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/accounts", json=accounts)
            balances = broker.get_balances()
        assert balances["KRW"].total == 1_000_000.0 and balances["KRW"].available == 1_000_000.0
        assert (
            balances["BTC"].total == 2.5
            and balances["BTC"].available == 2.0
            and balances["BTC"].locked == 0.5
        )

    def test_positions_keyed_by_unit_currency(self, broker):
        accounts = json.loads(json.dumps(ACCOUNTS_EXAMPLE))
        accounts[1]["locked"] = "0.5"
        accounts.append(  # 평균 매수가 0 (에어드랍 등) → 제외
            {
                "currency": "XRP",
                "balance": "10.0",
                "locked": "0",
                "avg_buy_price": "0",
                "avg_buy_price_modified": False,
                "unit_currency": "KRW",
            }
        )
        accounts.append(  # 잔고 0 → 제외
            {
                "currency": "ETH",
                "balance": "0",
                "locked": "0",
                "avg_buy_price": "3000000",
                "avg_buy_price_modified": False,
                "unit_currency": "KRW",
            }
        )
        accounts.append(  # BTC 마켓에서 산 자산 → BTC-DOGE
            {
                "currency": "DOGE",
                "balance": "100",
                "locked": "0",
                "avg_buy_price": "0.00000123",
                "avg_buy_price_modified": False,
                "unit_currency": "BTC",
            }
        )
        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/accounts", json=accounts)
            positions = broker.get_positions()
        assert set(positions) == {"KRW-BTC", "BTC-DOGE"}
        btc = positions["KRW-BTC"]
        assert btc.quantity == 2.5 and btc.average_price == 140_000_000.0
        assert btc.meta == {"available": 2.0, "locked": 0.5, "avg_buy_price_modified": False}
        assert positions["BTC-DOGE"].average_price == 0.00000123

    def test_equity_single_batched_ticker_call(self, broker, real_btc_ticker_raw):
        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/accounts", json=ACCOUNTS_EXAMPLE)
            rsps.get(f"{BASE}/v1/ticker", json=real_btc_ticker_raw)
            equity = broker.get_equity()
            ticker_calls = [c for c in rsps.calls if "/v1/ticker" in c.request.url]
        assert len(ticker_calls) == 1
        price = float(real_btc_ticker_raw[0]["trade_price"])
        assert equity == pytest.approx(1_000_000.0 + 2.0 * price)

    def test_equity_falls_back_to_cost_when_ticker_fails(self, broker):
        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/accounts", json=ACCOUNTS_EXAMPLE)
            rsps.get(f"{BASE}/v1/ticker", json=error_body(404, "Code not found"), status=404)
            equity = broker.get_equity()
        assert equity == pytest.approx(1_000_000.0 + 2.0 * 140_000_000.0)

    def test_equity_without_positions(self, broker):
        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/accounts", json=ACCOUNTS_EXAMPLE[:1])
            assert broker.get_equity() == 1_000_000.0
            assert len(rsps.calls) == 2  # accounts × 2 (balances, positions), ticker 호출 없음


# ============================================================================= 주문
class TestPlaceOrder:
    def test_stop_not_supported(self, broker):
        with pytest.raises(OrderError, match="STOP"):
            broker.place_order("KRW-BTC", OrderSide.BUY, 0.01, OrderType.STOP, price=1.0)

    def test_invalid_quantity_and_limit_without_price(self, broker):
        with pytest.raises(OrderError):
            broker.place_order("KRW-BTC", OrderSide.BUY, 0)
        with pytest.raises(OrderError):
            broker.place_order("KRW-BTC", OrderSide.SELL, -1)
        with pytest.raises(OrderError):
            broker.place_order("KRW-BTC", OrderSide.BUY, 0.000000001)  # 8자리 내림 → 0
        with pytest.raises(OrderError):
            broker.place_order("KRW-BTC", OrderSide.BUY, 0.01, OrderType.LIMIT)
        with pytest.raises(OrderError):
            broker.place_order("KRW-BTC", OrderSide.BUY, 0.01, OrderType.LIMIT, price=0)

    def test_market_buy_converts_quantity_to_krw_amount(self, broker, real_btc_ticker_raw):
        price = float(real_btc_ticker_raw[0]["trade_price"])
        qty = 0.0123
        expected_amount = int(qty * price / 1.0005)  # 정수 원 내림, 수수료만큼 축소

        def order_callback(request):
            body = json.loads(request.body)
            resp = {  # 금액 주문 응답 스키마: volume 없음
                "uuid": "0c7a1c8f-4b3c-4a4e-9a4e-1f5d6e7a8b9c",
                "side": "bid",
                "ord_type": "price",
                "price": body["price"],
                "state": "wait",
                "market": "KRW-BTC",
                "created_at": "2025-07-04T15:00:00+09:00",
                "remaining_volume": "0",
                "executed_volume": "0",
                "reserved_fee": "0",
                "remaining_fee": "0",
                "paid_fee": "0",
                "locked": "0",
                "prevented_volume": "0",
                "prevented_locked": "0",
                "trades_count": 0,
            }
            return 201, {}, json.dumps(resp)

        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/ticker", json=real_btc_ticker_raw)
            rsps.get(f"{BASE}/v1/orders/chance", json=CHANCE_EXAMPLE)
            rsps.add_callback(responses.POST, f"{BASE}/v1/orders", callback=order_callback)
            rsps.get(
                f"{BASE}/v1/order", json=error_body("order_not_found", "주문을 찾지 못했습니다."), status=404
            )
            order = broker.place_order("KRW-BTC", OrderSide.BUY, qty)
            post = next(c.request for c in rsps.calls if c.request.method == "POST")
            body = json.loads(post.body)
        pop_identifier(body)
        assert body == {
            "market": "KRW-BTC",
            "side": "bid",
            "ord_type": "price",
            "price": str(expected_amount),
        }
        assert expected_amount * 1.0005 <= qty * price  # 수수료 포함 총액이 예산을 넘지 않는다
        assert expected_amount >= KRW_MIN_ORDER_VALUE
        # 조회 실패 시에도 접수 결과를 돌려준다 (volume 이 없으므로 요청 수량 유지)
        assert order.id == "0c7a1c8f-4b3c-4a4e-9a4e-1f5d6e7a8b9c"
        assert order.type == OrderType.MARKET and order.side == OrderSide.BUY
        assert order.quantity == qty and order.price is None and order.status == OrderStatus.OPEN
        assert order.raw["price"] == str(expected_amount)

    def test_market_buy_amount_formula(self, broker):
        # 손계산: 0.5 × 10,000 = 5,000 → /1.0005 = 4997.5 → 4997 (내림)
        assert broker.market_buy_amount("KRW-X", 0.5, 10_000, 0.0005) == 4997.0
        assert broker.market_buy_amount("KRW-X", 1, 7_000, 0.0) == 7000.0
        assert broker.market_buy_amount("BTC-X", 2, 0.0001, 0.0025) == 0.00019950

    def test_market_buy_below_min_total_rejected_before_sending(self, broker, real_btc_ticker_raw):
        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/ticker", json=real_btc_ticker_raw)
            rsps.get(f"{BASE}/v1/orders/chance", json=CHANCE_EXAMPLE)
            with pytest.raises(OrderError, match="최소 주문 금액"):
                broker.place_order("KRW-BTC", OrderSide.BUY, 0.00000001)
            assert not any(c.request.method == "POST" for c in rsps.calls)

    def test_market_buy_uses_default_fee_when_chance_fails(self, broker, real_btc_ticker_raw):
        price = float(real_btc_ticker_raw[0]["trade_price"])
        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/ticker", json=real_btc_ticker_raw)
            rsps.get(f"{BASE}/v1/orders/chance", json=error_body("invalid_parameter", "bad"), status=400)
            rsps.post(f"{BASE}/v1/orders", json=ORDER_LIMIT_WAIT, status=201)
            rsps.get(f"{BASE}/v1/order", json=ORDER_LIMIT_WAIT)
            broker.place_order("KRW-BTC", OrderSide.BUY, 0.01)
            post = next(c.request for c in rsps.calls if c.request.method == "POST")
        assert json.loads(post.body)["price"] == str(int(0.01 * price / 1.0005))

    def test_market_buy_auth_error_from_chance_propagates(self, broker, real_btc_ticker_raw):
        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/ticker", json=real_btc_ticker_raw)
            rsps.get(f"{BASE}/v1/orders/chance", json=error_body("out_of_scope", "권한 없음"), status=403)
            with pytest.raises(AuthenticationError):
                broker.place_order("KRW-BTC", OrderSide.BUY, 0.01)

    def test_market_sell_sends_volume(self, broker):
        with responses.RequestsMock() as rsps:
            rsps.post(f"{BASE}/v1/orders", json=ORDER_MARKET_DONE, status=201)
            rsps.get(f"{BASE}/v1/order", json=ORDER_MARKET_DONE)
            order = broker.place_order("KRW-USDT", OrderSide.SELL, 5.377594)
            post = next(c.request for c in rsps.calls if c.request.method == "POST")
            get = next(c.request for c in rsps.calls if c.request.method == "GET")
        body = json.loads(post.body)
        pop_identifier(body)
        assert body == {
            "market": "KRW-USDT",
            "side": "ask",
            "ord_type": "market",
            "volume": "5.377594",
        }
        assert parse_qs(urlsplit(get.url).query) == {"uuid": [ORDER_MARKET_DONE["uuid"]]}
        assert broker.round_quantity("KRW-USDT", 5.377594123) == 5.37759412  # 9자리째는 내림
        assert order.status == OrderStatus.FILLED and order.is_filled
        assert order.filled_quantity == 5.377594 and order.quantity == 5.377594
        assert order.average_price == pytest.approx(7394.19175 / 5.377594)
        assert order.fee == 3.697095875
        assert order.created_at == datetime(2025, 8, 9, 7, 44, tzinfo=timezone.utc)
        assert order.updated_at == datetime(2025, 8, 9, 7, 44, 0, 597751, tzinfo=timezone.utc)

    def test_limit_order_rounds_price_and_quantity(self, broker):
        with responses.RequestsMock() as rsps:
            rsps.post(f"{BASE}/v1/orders", json=ORDER_LIMIT_WAIT, status=201)
            rsps.get(f"{BASE}/v1/order", json=ORDER_LIMIT_WAIT)
            order = broker.place_order(
                "KRW-BTC", OrderSide.BUY, 1.000000001, OrderType.LIMIT, price=139_999_600
            )
            post = next(c.request for c in rsps.calls if c.request.method == "POST")
        body = json.loads(post.body)
        pop_identifier(body)
        assert body == {
            "market": "KRW-BTC",
            "side": "bid",
            "ord_type": "limit",
            "volume": "1",
            "price": "140000000",
        }
        assert order.type == OrderType.LIMIT and order.price == 140_000_000.0
        assert (
            order.status == OrderStatus.OPEN and order.filled_quantity == 0.0 and order.average_price is None
        )
        assert order.created_at == datetime(2025, 7, 4, 6, 0, tzinfo=timezone.utc)
        assert order.raw["locked"] == "140070000.0"

    def test_identifier_is_unique_per_order(self, broker):
        """공식 문서: identifier 는 계정 내 전체 주문 기준으로 고유해야 하며 재사용할 수 없다 → 주문마다 새 uuid4."""
        with responses.RequestsMock() as rsps:
            rsps.post(f"{BASE}/v1/orders", json=ORDER_LIMIT_WAIT, status=201)
            rsps.get(f"{BASE}/v1/order", json=ORDER_LIMIT_WAIT)
            broker.place_order("KRW-BTC", OrderSide.BUY, 1, OrderType.LIMIT, price=140_000_000)
            broker.place_order("KRW-BTC", OrderSide.BUY, 1, OrderType.LIMIT, price=140_000_000)
            ids = [
                pop_identifier(json.loads(c.request.body)) for c in rsps.calls if c.request.method == "POST"
            ]
        assert len(ids) == 2 and ids[0] != ids[1]

    @pytest.mark.parametrize(
        "exc", [requests.ReadTimeout("read timed out"), requests.ConnectionError("connection reset")]
    )
    def test_lost_response_recovers_accepted_order_by_identifier(self, broker, real_btc_ticker_raw, exc):
        """POST 응답을 받지 못했지만 거래소가 주문을 받은 경우: identifier 로 조회해 그 주문을 돌려준다
        (GET /v1/order?identifier=, 공식 문서). 주문은 재전송하지 않는다."""

        price = float(real_btc_ticker_raw[0]["trade_price"])
        qty = broker.round_quantity("KRW-BTC", 2 * KRW_MIN_ORDER_VALUE / price)  # 약 10,000 KRW 어치
        posted: dict = {}

        def lookup(request):
            qs = parse_qs(urlsplit(request.url).query)
            assert list(qs) == ["identifier"]
            # 거래소에 접수된 주문: 요청한 금액(price) 과 identifier 를 그대로 가진 개별 주문 조회 응답
            raw = dict(ORDER_PRICE_CANCEL_FILLED, identifier=qs["identifier"][0], price=posted["price"])
            return 200, {}, json.dumps(raw)

        def failing_post(request):
            posted.update(json.loads(request.body))
            raise exc

        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/ticker", json=real_btc_ticker_raw)
            rsps.get(f"{BASE}/v1/orders/chance", json=CHANCE_EXAMPLE)
            rsps.add_callback(responses.POST, f"{BASE}/v1/orders", callback=failing_post)
            rsps.add_callback(responses.GET, f"{BASE}/v1/order", callback=lookup)
            order = broker.place_order("KRW-BTC", OrderSide.BUY, qty)
            posts = [c.request for c in rsps.calls if c.request.method == "POST"]
            gets = [c.request for c in rsps.calls if "/v1/order?" in c.request.url]
        assert len(posts) == 1 and len(gets) == 1
        sent_identifier = pop_identifier(posted)
        assert posted == {"market": "KRW-BTC", "side": "bid", "ord_type": "price", "price": posted["price"]}
        assert float(posted["price"]) >= KRW_MIN_ORDER_VALUE
        assert parse_qs(urlsplit(gets[0].url).query) == {"identifier": [sent_identifier]}
        assert decode_token(gets[0])["query_hash"] == query_hash({"identifier": sent_identifier})
        assert order.id == ORDER_PRICE_CANCEL_FILLED["uuid"]
        assert order.raw["identifier"] == sent_identifier and order.raw["price"] == posted["price"]
        assert order.status == OrderStatus.FILLED and order.is_filled
        assert order.filled_quantity == 0.00003 and order.quantity == qty  # 금액 주문: 요청 수량을 기억
        assert order.average_price == pytest.approx(160_000_000.0) and order.fee == 2.4
        assert broker._requested_qty[order.id] == qty

    def test_lost_response_with_order_not_found_reraises_network_error(self, broker):
        """identifier 조회가 order_not_found 면 주문이 접수되지 않은 것 → 원래 네트워크 오류(status 없음)를 던진다."""
        with responses.RequestsMock() as rsps:
            rsps.post(f"{BASE}/v1/orders", body=requests.ReadTimeout("read timed out"))
            rsps.get(
                f"{BASE}/v1/order", json=error_body("order_not_found", "주문을 찾지 못했습니다."), status=404
            )
            with pytest.raises(BrokerError) as ei:
                broker.place_order("KRW-BTC", OrderSide.BUY, 1, OrderType.LIMIT, price=140_000_000)
            assert [c.request.method for c in rsps.calls] == ["POST", "GET"]
        assert type(ei.value) is BrokerError and ei.value.status_code is None
        assert "네트워크 오류" in str(ei.value) and "read timed out" in str(ei.value)

    def test_lost_response_with_failed_lookup_stays_unknown(self, broker):
        """접수 여부 확인 자체가 실패하면 status 없는 BrokerError (엔진이 '결과 미확인' 으로 거래소 기준 확정)."""
        with responses.RequestsMock() as rsps:
            rsps.post(f"{BASE}/v1/orders", body=requests.ConnectionError("reset"))
            rsps.get(f"{BASE}/v1/order", json={}, status=502)
            with pytest.raises(BrokerError) as ei:
                broker.place_order("KRW-BTC", OrderSide.BUY, 1, OrderType.LIMIT, price=140_000_000)
            gets = [c for c in rsps.calls if c.request.method == "GET"]
            assert len(gets) == broker._client.max_retries + 1  # 조회(GET) 는 재시도, 주문(POST) 은 1회
            assert sum(c.request.method == "POST" for c in rsps.calls) == 1
        assert type(ei.value) is BrokerError and ei.value.status_code is None
        assert "접수 여부 확인 실패" in str(ei.value) and "HTTP 502" in str(ei.value)
        assert isinstance(ei.value.__cause__, BrokerError) and ei.value.__cause__.status_code is None

    def test_http_error_on_post_is_not_recovered(self, broker):
        """거래소가 명시적으로 거부한 주문(HTTP 오류 응답) 은 접수되지 않았으므로 identifier 조회를 하지 않는다."""
        with responses.RequestsMock() as rsps:
            rsps.post(
                f"{BASE}/v1/orders", json=error_body("under_min_total_bid", "최소 주문 금액"), status=400
            )
            with pytest.raises(OrderError):
                broker.place_order("KRW-BTC", OrderSide.BUY, 1, OrderType.LIMIT, price=140_000_000)
            assert len(rsps.calls) == 1

    def test_order_throttle_group_is_order(self, broker, monkeypatch):
        waited: list[str] = []
        for name, limiter in broker._limiters.items():
            monkeypatch.setattr(limiter, "wait", lambda n=name: waited.append(n) or 0.0)
        with responses.RequestsMock() as rsps:
            rsps.post(f"{BASE}/v1/orders", json=ORDER_LIMIT_WAIT, status=201)
            rsps.get(f"{BASE}/v1/order", json=ORDER_LIMIT_WAIT)
            broker.place_order("KRW-BTC", OrderSide.BUY, 1, OrderType.LIMIT, price=140_000_000)
        assert waited == ["order", "default"]


class TestOrderStatusMapping:
    def _parse(self, broker, **overrides):
        raw = json.loads(json.dumps(ORDER_LIMIT_WAIT))
        raw.update(overrides)
        return broker._parse_order(raw)

    def test_wait_and_watch_open(self, broker):
        assert self._parse(broker, state="wait").status == OrderStatus.OPEN
        assert self._parse(broker, state="watch").status == OrderStatus.OPEN

    def test_wait_with_partial_execution(self, broker):
        o = self._parse(broker, state="wait", executed_volume="0.4", remaining_volume="0.6")
        assert o.status == OrderStatus.PARTIALLY_FILLED
        assert o.filled_quantity == 0.4 and o.remaining_quantity == pytest.approx(0.6)
        assert o.average_price == 140_000_000.0  # trades 없으면 지정가를 평균가로

    def test_done_full_and_partial(self, broker):
        assert (
            self._parse(broker, state="done", executed_volume="1.0", remaining_volume="0").status
            == OrderStatus.FILLED
        )
        o = self._parse(broker, state="done", executed_volume="0.7", remaining_volume="0.3")
        assert o.status == OrderStatus.PARTIALLY_FILLED

    def test_cancel(self, broker):
        """지정가의 cancel 은 체결분이 있어도 CANCELED (사용자/엔진 취소)."""
        o = self._parse(broker, state="cancel", executed_volume="0.2")
        assert o.status == OrderStatus.CANCELED and o.status.is_terminal and o.filled_quantity == 0.2

    def test_market_buy_cancel_after_fill_is_filled(self, broker):
        """공식 문서: 시장가 매수(ord_type=price) 는 체결 후 잔량이 남으면 cancel, 딱 맞으면 done 으로 끝난다.
        price 는 정수 원으로 내림되므로 봇의 매수는 거의 항상 cancel 로 끝난다 → 정상 완료(FILLED) 로 판정해야 한다."""
        broker._remember_qty(ORDER_PRICE_CANCEL_FILLED["uuid"], 0.00003)
        o = broker._parse_order(json.loads(json.dumps(ORDER_PRICE_CANCEL_FILLED)))
        assert o.status == OrderStatus.FILLED and o.is_filled and o.status.is_terminal
        assert o.side == OrderSide.BUY and o.type == OrderType.MARKET and o.price is None
        assert o.quantity == 0.00003 and o.filled_quantity == 0.00003
        assert o.average_price == pytest.approx(4800 / 0.00003) and o.fee == 2.4
        # 딱 맞아떨어진 done 과 같은 결과
        done = broker._parse_order(dict(ORDER_PRICE_CANCEL_FILLED, state="done"))
        assert done.status == OrderStatus.FILLED
        # 체결 없이 취소된 금액 주문은 CANCELED
        raw = dict(ORDER_PRICE_CANCEL_FILLED, executed_volume="0", trades_count=0, trades=[], paid_fee="0")
        assert broker._parse_order(raw).status == OrderStatus.CANCELED

    def test_market_sell_cancel_with_partial_fill(self, broker):
        """시장가 매도(ord_type=market, volume 있음) 가 일부만 체결되고 잔량이 취소되면 PARTIALLY_FILLED, 전량이면 FILLED."""
        raw = json.loads(json.dumps(ORDER_MARKET_DONE))
        raw.update(state="cancel", executed_volume="3.0", remaining_volume="2.377594")
        raw["trades"][0].update(volume="3.0", funds="4125")
        o = broker._parse_order(raw)
        assert o.status == OrderStatus.PARTIALLY_FILLED and o.filled_quantity == 3.0
        assert o.remaining_quantity == pytest.approx(2.377594)
        full = dict(ORDER_MARKET_DONE, state="cancel")
        assert broker._parse_order(full).status == OrderStatus.FILLED

    def test_market_buy_cancel_after_fill_via_place_order(self, broker, real_btc_ticker_raw):
        """접수(wait) → 조회에서 cancel+체결 로 끝난 시장가 매수가 place_order 에서 체결된 주문으로 돌아온다."""
        price = float(real_btc_ticker_raw[0]["trade_price"])
        qty = broker.round_quantity("KRW-BTC", 2 * KRW_MIN_ORDER_VALUE / price)  # 약 10,000 KRW 어치
        accepted = dict(ORDER_PRICE_CANCEL_FILLED, state="wait", executed_volume="0", trades_count=0)
        accepted.pop("trades")
        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/ticker", json=real_btc_ticker_raw)
            rsps.get(f"{BASE}/v1/orders/chance", json=CHANCE_EXAMPLE)
            rsps.post(f"{BASE}/v1/orders", json=accepted, status=201)
            rsps.get(f"{BASE}/v1/order", json=ORDER_PRICE_CANCEL_FILLED)
            order = broker.place_order("KRW-BTC", OrderSide.BUY, qty)
        assert order.status == OrderStatus.FILLED and order.is_filled
        assert order.filled_quantity == 0.00003 and order.quantity == qty
        assert order.raw["state"] == "cancel"

    def test_unknown_state_pending(self, broker):
        assert self._parse(broker, state="mystery").status == OrderStatus.PENDING

    def test_map_state_signature_uses_ord_type(self):
        m = UpbitBroker._map_state
        assert m("cancel", "price", None, 0.00003) == OrderStatus.FILLED
        assert m("cancel", "price", None, 0.0) == OrderStatus.CANCELED
        assert m("cancel", "market", 5.0, 5.0) == OrderStatus.FILLED
        assert m("cancel", "market", 5.0, 2.0) == OrderStatus.PARTIALLY_FILLED
        assert m("cancel", "limit", 1.0, 0.5) == OrderStatus.CANCELED
        assert m("cancel", "limit", 1.0, 0.0) == OrderStatus.CANCELED
        assert m("done", "price", None, 0.00003) == OrderStatus.FILLED

    def test_price_order_without_volume_uses_remembered_quantity(self, broker):
        raw = json.loads(json.dumps(ORDER_LIMIT_WAIT))
        raw.pop("volume")
        raw.update(ord_type="price", price="50000", state="done", executed_volume="0.00044")
        o = broker._parse_order(raw)
        assert o.quantity == 0.00044 and o.price is None and o.status == OrderStatus.FILLED
        broker._remember_qty(raw["uuid"], 0.00045)
        assert broker._parse_order(raw).quantity == 0.00045

    def test_average_price_from_multiple_trades(self, broker):
        raw = json.loads(json.dumps(ORDER_MARKET_DONE))
        raw["trades"] = [
            dict(raw["trades"][0], price="1000", volume="1", funds="1000"),
            dict(
                raw["trades"][0],
                price="1100",
                volume="3",
                funds="3300",
                created_at="2025-08-09T16:45:00+09:00",
            ),
        ]
        raw.update(volume="4", executed_volume="4")
        o = broker._parse_order(raw)
        assert o.average_price == pytest.approx(4300 / 4)
        assert o.updated_at == datetime(2025, 8, 9, 7, 45, tzinfo=timezone.utc)


class TestOrderQueries:
    def test_get_order(self, broker):
        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/order", json=ORDER_MARKET_DONE)
            o = broker.get_order(ORDER_MARKET_DONE["uuid"])
            req = rsps.calls[0].request
            payload = decode_token(req)
        assert urlsplit(req.url).query == f"uuid={ORDER_MARKET_DONE['uuid']}"
        assert payload["query_hash"] == query_hash({"uuid": ORDER_MARKET_DONE["uuid"]})
        assert o.id == ORDER_MARKET_DONE["uuid"] and o.symbol == "KRW-USDT" and o.side == OrderSide.SELL

    def test_get_order_not_found(self, broker):
        with responses.RequestsMock() as rsps:
            rsps.get(
                f"{BASE}/v1/order", json=error_body("order_not_found", "주문을 찾지 못했습니다."), status=404
            )
            with pytest.raises(OrderError) as ei:
                broker.get_order("nope")
        assert ei.value.status_code == 404 and "order_not_found" in str(ei.value)

    def test_cancel_order(self, broker):
        canceled = dict(ORDER_LIMIT_WAIT, state="cancel")
        with responses.RequestsMock() as rsps:
            rsps.delete(f"{BASE}/v1/order", json=canceled)
            assert broker.cancel_order(ORDER_LIMIT_WAIT["uuid"]) is True
            req = rsps.calls[0].request
            payload = decode_token(req)
        assert req.method == "DELETE" and urlsplit(req.url).query == f"uuid={ORDER_LIMIT_WAIT['uuid']}"
        assert payload["query_hash"] == query_hash({"uuid": ORDER_LIMIT_WAIT["uuid"]})

    def test_cancel_order_not_found_returns_false(self, broker):
        with responses.RequestsMock() as rsps:
            rsps.delete(
                f"{BASE}/v1/order", json=error_body("order_not_found", "주문을 찾지 못했습니다."), status=404
            )
            assert broker.cancel_order("nope") is False

    def test_get_and_cancel_order_by_identifier(self, broker):
        """공식 문서: GET/DELETE /v1/order 는 uuid 또는 identifier 중 하나로 지정 (둘 다면 uuid 기준)."""
        ident = ORDER_PRICE_CANCEL_FILLED["identifier"]
        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/order", json=ORDER_PRICE_CANCEL_FILLED)
            o = broker.get_order(identifier=ident)
            req = rsps.calls[0].request
            payload = decode_token(req)
        assert urlsplit(req.url).query == f"identifier={ident}"
        assert payload["query_hash"] == query_hash({"identifier": ident})
        assert o.id == ORDER_PRICE_CANCEL_FILLED["uuid"] and o.raw["identifier"] == ident
        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/order", json=ORDER_PRICE_CANCEL_FILLED)
            broker.get_order(ORDER_PRICE_CANCEL_FILLED["uuid"], "KRW-BTC", identifier=ident)
            req = rsps.calls[0].request
        assert parse_qs(urlsplit(req.url).query) == {
            "uuid": [ORDER_PRICE_CANCEL_FILLED["uuid"]],
            "identifier": [ident],
        }
        with responses.RequestsMock() as rsps:
            rsps.delete(f"{BASE}/v1/order", json=dict(ORDER_LIMIT_WAIT, state="cancel"))
            assert broker.cancel_order(identifier=ident) is True
            req = rsps.calls[0].request
            payload = decode_token(req)
        assert req.method == "DELETE" and urlsplit(req.url).query == f"identifier={ident}"
        assert payload["query_hash"] == query_hash({"identifier": ident})
        with responses.RequestsMock() as rsps:
            rsps.delete(
                f"{BASE}/v1/order", json=error_body("order_not_found", "주문을 찾지 못했습니다."), status=404
            )
            assert broker.cancel_order(identifier=ident) is False

    def test_order_lookup_requires_uuid_or_identifier(self, broker):
        with responses.RequestsMock() as rsps:
            with pytest.raises(OrderError):
                broker.get_order()
            with pytest.raises(OrderError):
                broker.cancel_order("", "KRW-BTC")
            assert len(rsps.calls) == 0

    def test_cancel_order_other_error_raises(self, broker):
        with responses.RequestsMock() as rsps:
            rsps.delete(f"{BASE}/v1/order", json=error_body("invalid_parameter", "bad"), status=400)
            with pytest.raises(OrderError):
                broker.cancel_order("x")

    def test_open_orders_pagination(self, broker):
        page1 = [dict(ORDER_LIMIT_WAIT, uuid=f"{i:032x}") for i in range(100)]
        page2 = [dict(ORDER_LIMIT_WAIT, uuid="last", state="watch")]
        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/orders/open", json=page1)
            rsps.get(f"{BASE}/v1/orders/open", json=page2)
            orders = broker.get_open_orders()
            q1 = parse_qs(urlsplit(rsps.calls[0].request.url).query)
            q2 = parse_qs(urlsplit(rsps.calls[1].request.url).query)
        assert len(orders) == 101 and orders[-1].id == "last" and orders[-1].status == OrderStatus.OPEN
        assert "market" not in q1 and q1["page"] == ["1"] and q2["page"] == ["2"]
        assert q1["states[]"] == ["wait", "watch"] and q1["limit"] == ["100"]


# ============================================================================= 오류 매핑
class TestErrorMapping:
    @pytest.mark.parametrize(
        "status,name,exc",
        [
            (400, "insufficient_funds_bid", InsufficientFunds),
            (400, "insufficient_funds_ask", InsufficientFunds),
            (400, "under_min_total_bid", OrderError),
            (400, "create_ask_error", OrderError),
            (400, "invalid_parameter", OrderError),  # 주문 엔드포인트 컨텍스트
            (401, "invalid_access_key", AuthenticationError),
            (401, "jwt_verification", AuthenticationError),
            (401, "nonce_used", AuthenticationError),
            (403, "out_of_scope", AuthenticationError),
            (401, "out_of_scope", AuthenticationError),  # 안내 문서: out_of_scope 만 401/403 둘 다 가능
            (403, None, AuthenticationError),  # 이름을 모르면 상태 코드 기준
            (403, "market_offline", OrderError),  # 주문 API 문서의 403 = 시스템 점검, 인증 오류가 아니다
            (429, "too_many_requests", RateLimitError),
            (418, None, RateLimitError),
            (500, None, BrokerError),
        ],
    )
    def test_place_order_errors(self, broker, status, name, exc):
        body = error_body(name, "메시지") if name else {}
        with responses.RequestsMock() as rsps:
            rsps.post(f"{BASE}/v1/orders", json=body, status=status)
            with pytest.raises(exc) as ei:
                broker.place_order("KRW-BTC", OrderSide.BUY, 1, OrderType.LIMIT, price=140_000_000)
            assert len(rsps.calls) == 1  # 주문은 재시도하지 않는다
        assert ei.value.status_code == status
        if name:
            assert name in str(ei.value)
        assert type(ei.value) is exc

    def test_market_offline_official_body_is_order_error_not_auth(self, broker):
        """공식 new-order 문서의 403 예시 본문 그대로: 점검 중인 마켓은 OrderError 여야 운영자가 키 문제로 오해하지 않는다."""
        with responses.RequestsMock() as rsps:
            rsps.post(f"{BASE}/v1/orders", json=MARKET_OFFLINE_403, status=403)
            with pytest.raises(OrderError) as ei:
                broker.place_order("KRW-BTC", OrderSide.BUY, 1, OrderType.LIMIT, price=140_000_000)
        assert not isinstance(ei.value, AuthenticationError)
        assert ei.value.status_code == 403 and "market_offline" in str(ei.value) and "점검" in str(ei.value)

    def test_non_order_endpoint_400_is_plain_broker_error(self, broker):
        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/accounts", json=error_body("validation_error", "bad"), status=400)
            with pytest.raises(OrderError):  # validation_error 는 이름 기준 OrderError
                broker.get_balances()
        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/accounts", json=error_body("pocket_not_found", "no"), status=404)
            with pytest.raises(BrokerError) as ei:
                broker.get_balances()
        assert type(ei.value) is BrokerError and ei.value.status_code == 404

    def test_too_many_requests_name_on_200_family_status_is_rate_limit(self, broker):
        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/accounts", json=error_body("too_many_requests", "limit"), status=400)
            with pytest.raises(RateLimitError):
                broker.get_balances()

    def test_429_on_public_get_retries_then_rate_limit(self, public_broker):
        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/ticker", json=error_body(429, "Too Many Requests"), status=429)
            with pytest.raises(RateLimitError):
                public_broker.get_ticker("KRW-BTC")
            assert len(rsps.calls) == public_broker._client.max_retries + 1

    def test_network_error_is_broker_error(self, public_broker):
        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/ticker", body=requests.ConnectionError("boom"))
            with pytest.raises(BrokerError) as ei:
                public_broker.get_ticker("KRW-BTC")
        assert type(ei.value) is BrokerError and ei.value.status_code is None

    def test_no_authorization_token_body_from_live_api_shape(self, broker):
        body = {"error": {"message": "Please check Authorization Header", "name": "no_authorization_token"}}
        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/accounts", json=body, status=401)
            with pytest.raises(AuthenticationError) as ei:
                broker.get_balances()
        assert "no_authorization_token" in str(ei.value)


# ============================================================================= 요청 수 제한
class TestRateLimit:
    def test_parse_remaining_req(self):
        assert parse_remaining_req("group=default; min=1800; sec=29") == {
            "group": "default",
            "min": 1800,
            "sec": 29,
        }
        assert parse_remaining_req("group=candles; min=600; sec=9") == {
            "group": "candles",
            "min": 600,
            "sec": 9,
        }
        assert parse_remaining_req("group=order; sec=x") == {"group": "order"}
        assert parse_remaining_req("") == {}

    def test_limiter_spacing(self, monkeypatch):
        clock = [100.0]
        slept: list[float] = []
        monkeypatch.setattr(time, "monotonic", lambda: clock[0])

        def fake_sleep(s):
            slept.append(s)
            clock[0] += s

        monkeypatch.setattr(time, "sleep", fake_sleep)
        lim = _RateLimiter(8.0)
        assert lim.wait() == 0.0
        assert lim.wait() == pytest.approx(0.125)
        assert lim.wait() == pytest.approx(0.125)
        assert slept == pytest.approx([0.125, 0.125])
        clock[0] += 10  # 오래 쉬면 바로 통과
        assert lim.wait() == 0.0
        lim.defer(0.5)
        assert lim.wait() == pytest.approx(0.5)
        with pytest.raises(ValueError):
            _RateLimiter(0)

    def test_rate_limit_groups_configured(self, public_broker):
        assert RATE_LIMITS["order"] <= 12 and RATE_LIMITS["default"] <= 30
        assert all(RATE_LIMITS[g] <= 10 for g in ("candles", "ticker", "orderbook", "market"))
        assert set(public_broker._limiters) == set(RATE_LIMITS)

    def test_remaining_req_header_recorded_and_zero_defers(
        self, public_broker, real_btc_ticker_raw, monkeypatch
    ):
        deferred: list[float] = []
        monkeypatch.setattr(public_broker._limiters["ticker"], "defer", lambda s: deferred.append(s))
        with responses.RequestsMock() as rsps:
            rsps.get(
                f"{BASE}/v1/ticker",
                json=real_btc_ticker_raw,
                headers={"Remaining-Req": "group=ticker; min=599; sec=3"},
            )
            public_broker.get_ticker("KRW-BTC")
            assert public_broker.remaining_req["ticker"] == {"group": "ticker", "min": 599, "sec": 3}
            assert deferred == []
        with responses.RequestsMock() as rsps:
            rsps.get(
                f"{BASE}/v1/ticker",
                json=real_btc_ticker_raw,
                headers={"Remaining-Req": "group=ticker; min=598; sec=0"},
            )
            public_broker.get_ticker("KRW-BTC")
        assert len(deferred) == 1 and 0 < deferred[0] <= 1.0

    def test_public_calls_use_group_limiters(self, public_broker, real_btc_ticker_raw, monkeypatch):
        used: list[str] = []
        for name, limiter in public_broker._limiters.items():
            monkeypatch.setattr(limiter, "wait", lambda n=name: used.append(n) or 0.0)
        with responses.RequestsMock() as rsps:
            rsps.get(f"{BASE}/v1/ticker", json=real_btc_ticker_raw)
            rsps.get(f"{BASE}/v1/candles/days", json=[])
            public_broker.get_ticker("KRW-BTC")
            public_broker.get_candles("KRW-BTC", "1d", limit=1)
        assert used == ["ticker", "candles"]


# ============================================================================= 실제 공개 API (네트워크 필요)
class TestLivePublicApi:
    def test_live_ticker(self, live_broker):
        tickers = live_broker.get_tickers(["KRW-BTC", "KRW-ETH"])
        assert set(tickers) == {"KRW-BTC", "KRW-ETH"}
        assert tickers["KRW-BTC"] > tickers["KRW-ETH"] > 0
        assert "ticker" in live_broker.remaining_req

    def test_live_hourly_candles_complete_and_ordered(self, live_broker):
        candles = live_broker.get_candles("KRW-BTC", "1h", limit=5)
        now = utcnow()
        assert len(candles) == 5
        assert all(c.timestamp + timedelta(hours=1) <= now for c in candles)
        assert all(
            b.timestamp - a.timestamp == timedelta(hours=1)
            for a, b in zip(candles, candles[1:], strict=False)
        )
        assert all(c.low <= min(c.open, c.close) <= max(c.open, c.close) <= c.high for c in candles)
        assert all(c.volume >= 0 for c in candles)
        partial = live_broker.get_candles("KRW-BTC", "1h", limit=5, include_partial=True)
        assert partial[-1].timestamp >= candles[-1].timestamp

    def test_live_daily_pagination_with_end_is_exclusive(self, live_broker):
        first = live_broker.get_candles("KRW-BTC", "1d", limit=3)
        older = live_broker.get_candles("KRW-BTC", "1d", limit=3, end=first[0].timestamp)
        assert len(older) == 3
        assert older[-1].timestamp == first[0].timestamp - timedelta(days=1)  # end 미만만 반환 → 겹침 없음
        assert all(c.timestamp < first[0].timestamp for c in older)

    def test_live_weekly_candles_start_monday(self, live_broker):
        candles = live_broker.get_candles("KRW-BTC", "1w", limit=2)
        assert len(candles) == 2
        assert all(c.timestamp.weekday() == 0 and c.timestamp.hour == 0 for c in candles)
        assert candles[1].timestamp - candles[0].timestamp == timedelta(days=7)

    def test_live_tick_table_matches_orderbook_instruments(self, live_broker):
        """공식 호가 정책 조회(/v1/orderbook/instruments)의 tick_size 가 구현한 표와 일치해야 한다."""
        markets = ["KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-DOGE", "KRW-SOL", "KRW-ADA", "KRW-USDT", "KRW-SHIB"]
        try:
            raw = live_broker._public(
                "/v1/orderbook/instruments", {"markets": ",".join(markets)}, group="orderbook"
            )
            tickers = live_broker.get_tickers(markets)
        except BrokerError as e:
            pytest.skip(f"호가 정책 조회 실패: {e}")
        checked = 0
        for item in raw:
            market = item["market"]
            if market not in tickers:
                continue
            assert tick_size(market, tickers[market]) == float(item["tick_size"]), market
            checked += 1
        assert checked >= 5

    def test_live_unknown_market(self, live_broker):
        with pytest.raises(BrokerError) as ei:
            live_broker.get_candles("KRW-THISDOESNOTEXIST", "1d", limit=1)
        assert ei.value.status_code == 404
