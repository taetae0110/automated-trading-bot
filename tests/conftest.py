"""공용 테스트 픽스처.

원칙: **가짜/데모 시세 데이터를 만들지 않는다.**
테스트에 쓰는 캔들은 전부 Upbit 공개 API 에서 받은 실제 데이터이며, pytest 캐시에 저장해 재사용한다.
네트워크가 없으면 해당 테스트는 skip 된다. (CI 는 네트워크가 있으므로 실행된다.)

시간봉 구간 고정: KRW-BTC 시간봉(`candles`/`candles_df`/`real_btc_hourly_raw`) 은 "최신 200개" 가 아니라
`REAL_HOURLY_WINDOW_END` 직전의 **고정된 과거 200개** 를 받는다 (Upbit `to` 파라미터, 배타적).
시간봉 200개는 8일 남짓이라, 최신 구간을 받으면 `.pytest_cache/` 가 없는 CI 는 실행마다 다른 8일을 쓰게 되고
"진입 후 1% 하락/상승" 같은 엔진 손절·익절 테스트의 데이터 전제가 약 5번 중 1번은 성립하지 않아 테스트가 조용히
skip 되었다. 구간을 고정하면 모든 실행이 같은 실제 캔들을 쓰므로 전제 성립 여부가 결정적이다 (구간을 옮길 때는
전체 테스트를 돌려 데이터 전제 skip 이 생기지 않는지 확인한다).
일봉(`daily_candles`/`daily_df`/`eth_daily_df`) 은 200일이라 돌파 일/비돌파 일 같은 전제가 항상 성립하고,
CLI 의 "저장된 CSV 가 최신 구간을 덮는가" 검사처럼 **현재 시각 기준** 동작을 검증하는 테스트가 쓰므로 최신 구간을 유지한다.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
import requests

from tradingbot.models import INTERVAL_SECONDS, Candle
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

#: KRW-BTC 시간봉 고정 구간의 끝 (UTC, 배타적): 2026-08-24 04:00 ~ 2026-09-01 11:00 의 실제 캔들 200개.
REAL_HOURLY_WINDOW_END = "2026-09-01T12:00:00Z"


def _parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def fetch_real_upbit_candles(
    market: str, interval: str, count: int = 200, to: str | None = None
) -> list[dict]:
    """Upbit 공개 API 원본 응답(list[dict], 최신→과거). 실패 시 예외.

    `to`(UTC ISO8601, 배타적) 를 주면 그 직전 `count` 개를, None 이면 최신 `count` 개를 받는다.
    """
    params: dict[str, str | int] = {"market": market, "count": min(count, 200)}
    if to is not None:
        params["to"] = to
    resp = requests.get(f"{UPBIT}{_UPBIT_INTERVAL_PATH[interval]}", params=params, timeout=10)
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


def _check_window(raw: list[dict], market: str, interval: str, count: int, to: str) -> None:
    """받은 구간이 고정 구간과 일치하는지 검사한다. 어긋나면 skip 이 아니라 **실패**시킨다.

    (`to` 가 무시되거나 개수가 모자라면 테스트가 다른 데이터로 조용히 돌거나 skip 되는 것을 막는다.)
    """
    end = _parse_utc(to)
    step = timedelta(seconds=INTERVAL_SECONDS[interval])
    newest = _parse_utc(raw[0]["candle_date_time_utc"])
    oldest = _parse_utc(raw[-1]["candle_date_time_utc"])
    if len(raw) != count or not (end - step <= newest < end) or oldest >= newest:
        raise AssertionError(
            f"Upbit {market} {interval} 고정 구간 불일치: {len(raw)}개 {oldest.isoformat()} ~ "
            f"{newest.isoformat()} (기대: {count}개, 끝 {to} 직전)"
        )


def _cached_real_raw(request, market: str, interval: str, count: int, to: str | None = None) -> list[dict]:
    window = "latest" if to is None else "to_" + to.replace(":", "").replace("-", "")
    key = f"tradingbot/upbit/{market}/{interval}/{count}/{window}"
    cached = request.config.cache.get(key, None)
    if cached:
        return cached
    try:
        raw = fetch_real_upbit_candles(market, interval, count, to=to)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"실제 시세 데이터를 받을 수 없어 건너뜀 (네트워크 필요): {e}")
    if to is not None:
        _check_window(raw, market, interval, count, to)
    request.config.cache.set(key, json.loads(json.dumps(raw)))
    return raw


@pytest.fixture(scope="session")
def real_btc_daily_raw(request) -> list[dict]:
    """KRW-BTC 일봉 최신 200개 원본 응답 (Upbit, 최신→과거)."""
    return _cached_real_raw(request, "KRW-BTC", "1d", 200)


@pytest.fixture(scope="session")
def real_btc_hourly_raw(request) -> list[dict]:
    """KRW-BTC 시간봉 200개 원본 응답 (Upbit, 최신→과거, `REAL_HOURLY_WINDOW_END` 직전 고정 구간)."""
    return _cached_real_raw(request, "KRW-BTC", "1h", 200, to=REAL_HOURLY_WINDOW_END)


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
