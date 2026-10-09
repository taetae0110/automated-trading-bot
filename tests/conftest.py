"""공용 테스트 픽스처.

원칙: **가짜/데모 시세 데이터를 만들지 않는다.**
테스트에 쓰는 캔들은 전부 Upbit 공개 API 에서 받은 실제 데이터이며, pytest 캐시에 저장해 재사용한다.
네트워크가 없으면 해당 테스트는 skip 된다. (CI 는 네트워크가 있으므로 실행된다.)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd
import pytest
import requests

from tradingbot.models import Candle
from tradingbot.strategies.base import candles_to_df

UPBIT = "https://api.upbit.com"
_UPBIT_INTERVAL_PATH = {
    "1m": "/v1/candles/minutes/1",
    "5m": "/v1/candles/minutes/5",
    "15m": "/v1/candles/minutes/15",
    "1h": "/v1/candles/minutes/60",
    "4h": "/v1/candles/minutes/240",
    "1d": "/v1/candles/days",
    "1w": "/v1/candles/weeks",
}


def fetch_real_upbit_candles(market: str, interval: str, count: int = 200) -> list[dict]:
    """Upbit 공개 API 원본 응답(list[dict], 최신→과거). 실패 시 예외."""
    resp = requests.get(
        f"{UPBIT}{_UPBIT_INTERVAL_PATH[interval]}",
        params={"market": market, "count": min(count, 200)},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list) or not data:
        raise RuntimeError(f"Upbit 응답이 비었습니다: {data!r}")
    return data


def upbit_raw_to_candles(raw: list[dict]) -> list[Candle]:
    out = []
    for r in raw:
        ts = datetime.fromisoformat(r["candle_date_time_utc"]).replace(tzinfo=timezone.utc)
        out.append(
            Candle(
                timestamp=ts,
                open=float(r["opening_price"]),
                high=float(r["high_price"]),
                low=float(r["low_price"]),
                close=float(r["trade_price"]),
                volume=float(r["candle_acc_trade_volume"]),
            )
        )
    out.sort(key=lambda c: c.timestamp)
    return out


def _cached_real_raw(request, market: str, interval: str, count: int) -> list[dict]:
    key = f"tradingbot/upbit/{market}/{interval}/{count}"
    cached = request.config.cache.get(key, None)
    if cached:
        return cached
    try:
        raw = fetch_real_upbit_candles(market, interval, count)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"실제 시세 데이터를 받을 수 없어 건너뜀 (네트워크 필요): {e}")
    request.config.cache.set(key, json.loads(json.dumps(raw)))
    return raw


@pytest.fixture(scope="session")
def real_btc_daily_raw(request) -> list[dict]:
    """KRW-BTC 일봉 200개 원본 응답 (Upbit, 최신→과거)."""
    return _cached_real_raw(request, "KRW-BTC", "1d", 200)


@pytest.fixture(scope="session")
def real_btc_hourly_raw(request) -> list[dict]:
    return _cached_real_raw(request, "KRW-BTC", "1h", 200)


@pytest.fixture(scope="session")
def real_eth_daily_raw(request) -> list[dict]:
    return _cached_real_raw(request, "KRW-ETH", "1d", 200)


@pytest.fixture
def daily_candles(real_btc_daily_raw) -> list[Candle]:
    return upbit_raw_to_candles(real_btc_daily_raw)


@pytest.fixture
def daily_df(daily_candles) -> pd.DataFrame:
    return candles_to_df(daily_candles)


@pytest.fixture
def candles(real_btc_hourly_raw) -> list[Candle]:
    return upbit_raw_to_candles(real_btc_hourly_raw)


@pytest.fixture
def candles_df(candles) -> pd.DataFrame:
    return candles_to_df(candles)


@pytest.fixture
def eth_daily_df(real_eth_daily_raw) -> pd.DataFrame:
    return candles_to_df(upbit_raw_to_candles(real_eth_daily_raw))


@pytest.fixture
def tmp_data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    return d
