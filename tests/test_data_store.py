"""CandleStore / 캔들 DataFrame 헬퍼 테스트.

캔들은 전부 conftest 의 **실제 Upbit 데이터**(daily_candles, daily_df, candles_df) 를 쓴다.
download 테스트의 가짜 브로커는 그 실제 캔들을 Upbit 처럼 잘라 줄 뿐, 가격을 만들어 내지 않는다.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from tradingbot.brokers.base import BaseBroker
from tradingbot.data import store as store_module
from tradingbot.data.store import (
    TIMESTAMP_FORMAT,
    CandleStore,
    empty_candles_df,
    filter_candles_df,
    normalize_candles_df,
    symbol_safe,
    to_utc_datetime,
)
from tradingbot.exceptions import BrokerError, DataError
from tradingbot.models import Balance, Candle, Order, OrderSide, OrderType, Position, utcnow
from tradingbot.strategies.base import CANDLE_COLUMNS, candles_to_df

UTC = timezone.utc
ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


# ---------------------------------------------------------------------------- 테스트용 브로커
class SliceBroker(BaseBroker):
    """conftest 의 실제 캔들을 Upbit 처럼 ``end`` 미만(exclusive) 최신 ``limit`` 개로 잘라 주는 브로커.

    - inclusive_end=True : ``end`` 이하(inclusive) 로 동작하는 브로커 흉내
    - stuck=True         : ``end`` 를 무시하고 항상 같은 최신 페이지를 돌려주는 (페이지네이션 고장) 브로커 흉내
    """

    name = "upbit"

    def __init__(self, candles: list[Candle], *, inclusive_end: bool = False, stuck: bool = False) -> None:
        self._candles = sorted(candles, key=lambda c: c.timestamp)
        self.inclusive_end = inclusive_end
        self.stuck = stuck
        self.calls: list[tuple[int, datetime | None]] = []

    def get_candles(
        self,
        symbol: str,
        interval: str,
        limit: int = 200,
        end: datetime | None = None,
        include_partial: bool = False,
    ) -> list[Candle]:
        self.calls.append((limit, end))
        pool = self._candles
        if end is not None and not self.stuck:
            if self.inclusive_end:
                pool = [c for c in pool if c.timestamp <= end]
            else:
                pool = [c for c in pool if c.timestamp < end]
        return pool[-limit:] if limit > 0 else []

    def get_ticker(self, symbol: str) -> float:
        raise BrokerError("테스트 브로커는 현재가를 제공하지 않습니다")

    def get_balances(self) -> dict[str, Balance]:
        return {}

    def get_positions(self) -> dict[str, Position]:
        return {}

    def place_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        order_type: OrderType = OrderType.MARKET,
        price: float | None = None,
    ) -> Order:
        raise BrokerError("테스트 브로커는 주문을 지원하지 않습니다")

    def cancel_order(self, order_id: str, symbol: str | None = None) -> bool:
        return False

    def get_order(self, order_id: str, symbol: str | None = None) -> Order:
        raise BrokerError("테스트 브로커는 주문을 지원하지 않습니다")

    def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        return []

    def quote_currency(self, symbol: str) -> str:
        return symbol.split("-")[0]


@pytest.fixture
def store(tmp_data_dir: Path) -> CandleStore:
    return CandleStore(tmp_data_dir)


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """store.download 의 time.sleep 을 기록만 하도록 바꾼다."""
    calls: list[float] = []
    monkeypatch.setattr(store_module.time, "sleep", lambda s: calls.append(s))
    return calls


def _ts(df: pd.DataFrame, i: int) -> datetime:
    return df["timestamp"].iloc[i].to_pydatetime()


# ---------------------------------------------------------------------------- symbol_safe / path
@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("BTC/USDT", "BTC_USDT"),
        ("KRW-BTC", "KRW-BTC"),
        ("005930", "005930"),
        ("AAPL", "AAPL"),
        ("  ETH/BTC ", "ETH_BTC"),
        ("A B:C*D?E", "A_B_C_D_E"),
        ("a\\b|c", "a_b_c"),
    ],
)
def test_symbol_safe(symbol: str, expected: str) -> None:
    assert symbol_safe(symbol) == expected


@pytest.mark.parametrize("symbol", ["", "   ", ".", ".."])
def test_symbol_safe_rejects_unusable(symbol: str) -> None:
    with pytest.raises(DataError):
        symbol_safe(symbol)


def test_path_layout(store: CandleStore, tmp_data_dir: Path) -> None:
    assert store.path("binance", "BTC/USDT", "1h") == tmp_data_dir / "binance" / "BTC_USDT_1h.csv"
    assert store.path("upbit", "KRW-BTC", "1d") == tmp_data_dir / "upbit" / "KRW-BTC_1d.csv"
    assert store.path("kis", "005930", "1d") == tmp_data_dir / "kis" / "005930_1d.csv"
    assert str(store) == f"<CandleStore {tmp_data_dir}>"


@pytest.mark.parametrize(
    ("broker", "symbol", "interval"),
    [
        ("upbit", "KRW-BTC", "2h"),  # 지원하지 않는 간격
        ("upbit", "KRW-BTC", ""),
        ("", "KRW-BTC", "1d"),
        ("up/bit", "KRW-BTC", "1d"),  # 경로 구분자
        ("..", "KRW-BTC", "1d"),
        ("upbit", "", "1d"),
    ],
)
def test_path_rejects_bad_components(store: CandleStore, broker: str, symbol: str, interval: str) -> None:
    with pytest.raises(DataError):
        store.path(broker, symbol, interval)


def test_from_config_uses_backtest_data_dir(tmp_data_dir: Path) -> None:
    from tradingbot.config import load_config

    cfg = load_config(None, overrides={"backtest": {"data_dir": str(tmp_data_dir / "cache")}})
    assert CandleStore.from_config(cfg).data_dir == tmp_data_dir / "cache"


# ---------------------------------------------------------------------------- 빈 프레임 / 정규화
def test_empty_candles_df_has_canonical_shape() -> None:
    df = empty_candles_df()
    assert list(df.columns) == CANDLE_COLUMNS
    assert len(df) == 0
    assert str(df["timestamp"].dtype) == "datetime64[us, UTC]"
    assert all(df[c].dtype == "float64" for c in ("open", "high", "low", "close", "volume"))


def test_normalize_sorts_dedups_and_casts(daily_df: pd.DataFrame) -> None:
    # 실제 일봉을 뒤섞고, 문자열 timestamp 로 바꾸고, 잡다한 컬럼을 붙이고, 마지막 행을 한 번 더 넣는다
    messy = daily_df.sample(frac=1.0, random_state=7).copy()
    messy["timestamp"] = messy["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    messy["extra"] = "x"
    messy = pd.concat([messy, messy.iloc[[0]]], ignore_index=True)
    out = normalize_candles_df(messy)
    pd.testing.assert_frame_equal(out, daily_df)


def test_normalize_keeps_last_duplicate(daily_df: pd.DataFrame) -> None:
    # 같은 timestamp 가 두 번 있으면 뒤의 행이 남는다 (volume 0.0 은 '뒤의 행' 표식일 뿐 시세가 아님)
    first = daily_df.iloc[:5].copy()
    later = daily_df.iloc[[2]].copy()
    later["volume"] = 0.0
    out = normalize_candles_df(pd.concat([first, later], ignore_index=True))
    assert len(out) == 5
    assert out.loc[2, "volume"] == 0.0
    assert out.loc[2, "close"] == daily_df.loc[2, "close"]


def test_normalize_accepts_datetime_index(daily_df: pd.DataFrame) -> None:
    indexed = daily_df.set_index("timestamp")
    pd.testing.assert_frame_equal(normalize_candles_df(indexed), daily_df)


def test_normalize_treats_naive_timestamps_as_utc(candles_df: pd.DataFrame) -> None:
    naive = candles_df.copy()
    naive["timestamp"] = naive["timestamp"].dt.tz_localize(None)
    pd.testing.assert_frame_equal(normalize_candles_df(naive), candles_df)


def test_normalize_drops_nan_ohlc_and_fills_volume(daily_df: pd.DataFrame) -> None:
    broken = daily_df.iloc[:10].copy()
    broken.loc[3, "close"] = float("nan")
    broken.loc[5, "volume"] = float("nan")
    out = normalize_candles_df(broken)
    assert len(out) == 9
    assert _ts(daily_df, 3) not in set(out["timestamp"])
    assert out.loc[out["timestamp"] == _ts(daily_df, 5), "volume"].iloc[0] == 0.0


def test_normalize_errors(daily_df: pd.DataFrame) -> None:
    with pytest.raises(DataError, match="컬럼"):
        normalize_candles_df(daily_df.drop(columns=["volume"]))
    bad_price = daily_df.iloc[:3].copy()
    bad_price["open"] = ["a", "b", "c"]
    with pytest.raises(DataError, match="open"):
        normalize_candles_df(bad_price)
    bad_ts = daily_df.iloc[:3].copy()
    bad_ts["timestamp"] = ["not-a-date", "2024-01-01", "2024-01-02"]
    with pytest.raises(DataError, match="timestamp"):
        normalize_candles_df(bad_ts)
    with pytest.raises(DataError):
        normalize_candles_df([1, 2, 3])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------- 날짜 변환 / 필터
def test_to_utc_datetime_variants() -> None:
    assert to_utc_datetime(None) is None
    assert to_utc_datetime("2024-03-01") == datetime(2024, 3, 1, tzinfo=UTC)
    assert to_utc_datetime("2024-03-01T09:00:00+09:00") == datetime(2024, 3, 1, 0, 0, tzinfo=UTC)
    assert to_utc_datetime(datetime(2024, 3, 1, 12)) == datetime(2024, 3, 1, 12, tzinfo=UTC)
    kst = timezone(timedelta(hours=9))
    assert to_utc_datetime(datetime(2024, 3, 1, 9, tzinfo=kst)) == datetime(2024, 3, 1, 0, tzinfo=UTC)
    assert to_utc_datetime(date(2024, 3, 1)) == datetime(2024, 3, 1, tzinfo=UTC)
    assert to_utc_datetime(pd.Timestamp("2024-03-01")) == datetime(2024, 3, 1, tzinfo=UTC)
    assert to_utc_datetime(pd.Timestamp("2024-03-01 09:00", tz="Asia/Seoul")) == datetime(
        2024, 3, 1, tzinfo=UTC
    )
    out = to_utc_datetime("2024-03-01")
    assert out is not None and out.tzinfo is not None


@pytest.mark.parametrize("value", ["", "   ", "2024/03/01", "yesterday", 20240301, 1.5, pd.NaT])
def test_to_utc_datetime_rejects(value: object) -> None:
    with pytest.raises(DataError):
        to_utc_datetime(value)  # type: ignore[arg-type]


def test_filter_inclusive_both_ends(candles_df: pd.DataFrame) -> None:
    start, end = _ts(candles_df, 10), _ts(candles_df, 20)
    out = filter_candles_df(candles_df, start, end)
    pd.testing.assert_frame_equal(out, candles_df.iloc[10:21].reset_index(drop=True))
    assert out["timestamp"].iloc[0] == start
    assert out["timestamp"].iloc[-1] == end


def test_filter_accepts_naive_str_and_timestamp(candles_df: pd.DataFrame, daily_df: pd.DataFrame) -> None:
    start, end = _ts(candles_df, 10), _ts(candles_df, 20)
    expected = candles_df.iloc[10:21].reset_index(drop=True)
    naive = filter_candles_df(candles_df, start.replace(tzinfo=None), end.replace(tzinfo=None))
    pd.testing.assert_frame_equal(naive, expected)
    stamped = filter_candles_df(candles_df, pd.Timestamp(start), pd.Timestamp(end))
    pd.testing.assert_frame_equal(stamped, expected)
    # 일봉은 00:00 UTC 이므로 'YYYY-MM-DD' 문자열로 정확히 잡힌다
    s, e = _ts(daily_df, 5).strftime("%Y-%m-%d"), _ts(daily_df, 9).strftime("%Y-%m-%d")
    pd.testing.assert_frame_equal(
        filter_candles_df(daily_df, s, e), daily_df.iloc[5:10].reset_index(drop=True)
    )


def test_filter_one_sided_and_out_of_range(daily_df: pd.DataFrame) -> None:
    n = len(daily_df)
    pd.testing.assert_frame_equal(
        filter_candles_df(daily_df, start=_ts(daily_df, n - 3)), daily_df.iloc[n - 3 :].reset_index(drop=True)
    )
    pd.testing.assert_frame_equal(filter_candles_df(daily_df, end=_ts(daily_df, 2)), daily_df.iloc[:3])
    far_future = utcnow() + timedelta(days=3650)
    out = filter_candles_df(daily_df, start=far_future)
    assert out.empty and list(out.columns) == CANDLE_COLUMNS
    with pytest.raises(DataError, match="start"):
        filter_candles_df(daily_df, _ts(daily_df, 5), _ts(daily_df, 4))


# ---------------------------------------------------------------------------- load / save
def test_load_missing_file_returns_empty_frame(store: CandleStore) -> None:
    df = store.load("upbit", "KRW-BTC", "1d")
    assert df.empty
    assert list(df.columns) == CANDLE_COLUMNS
    assert str(df["timestamp"].dtype) == "datetime64[us, UTC]"
    assert store.load("upbit", "KRW-BTC", "1d", start="2024-01-01", end="2024-02-01").empty
    assert store.exists("upbit", "KRW-BTC", "1d") is False
    assert store.date_range("upbit", "KRW-BTC", "1d") is None
    with pytest.raises(DataError):
        store.load("upbit", "KRW-BTC", "1d", start="2024-02-01", end="2024-01-01")


@pytest.mark.parametrize(("fixture", "interval"), [("daily_df", "1d"), ("candles_df", "1h")])
def test_save_load_round_trip(
    store: CandleStore, request: pytest.FixtureRequest, fixture: str, interval: str
) -> None:
    df: pd.DataFrame = request.getfixturevalue(fixture)
    path = store.save("upbit", "KRW-BTC", interval, df)
    assert path == store.path("upbit", "KRW-BTC", interval)
    assert path.is_file()
    assert store.exists("upbit", "KRW-BTC", interval)
    assert list(path.parent.glob(".*.tmp")) == []  # 임시 파일 정리됨

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == ",".join(CANDLE_COLUMNS)
    assert len(lines) == len(df) + 1
    for line in lines[1:]:
        assert ISO_Z.match(line.split(",")[0]), line
    assert lines[1].split(",")[0] == df["timestamp"].iloc[0].strftime(TIMESTAMP_FORMAT)

    loaded = store.load("upbit", "KRW-BTC", interval)
    pd.testing.assert_frame_equal(loaded, df)
    assert store.date_range("upbit", "KRW-BTC", interval) == (_ts(df, 0), _ts(df, -1))


def test_save_merges_and_dedups(store: CandleStore, daily_df: pd.DataFrame) -> None:
    store.save("upbit", "KRW-BTC", "1d", daily_df.iloc[:120])
    # 100~119 가 겹치는 두 번째 조각을 (뒤섞어서) 저장
    store.save("upbit", "KRW-BTC", "1d", daily_df.iloc[100:].sample(frac=1.0, random_state=1))
    loaded = store.load("upbit", "KRW-BTC", "1d")
    pd.testing.assert_frame_equal(loaded, daily_df)
    assert loaded["timestamp"].is_monotonic_increasing
    assert not loaded["timestamp"].duplicated().any()


def test_save_new_data_overrides_existing(store: CandleStore, daily_df: pd.DataFrame) -> None:
    store.save("upbit", "KRW-BTC", "1d", daily_df)
    patch = daily_df.iloc[[50]].copy()
    patch["volume"] = 0.0  # '새 데이터가 이긴다' 를 확인하기 위한 표식 값
    store.save("upbit", "KRW-BTC", "1d", patch)
    loaded = store.load("upbit", "KRW-BTC", "1d")
    assert len(loaded) == len(daily_df)
    assert loaded.loc[50, "volume"] == 0.0
    assert loaded.loc[50, "close"] == daily_df.loc[50, "close"]
    assert loaded.loc[49, "volume"] == daily_df.loc[49, "volume"]


def test_save_empty_frame(store: CandleStore, daily_df: pd.DataFrame) -> None:
    path = store.save("upbit", "KRW-BTC", "1d", empty_candles_df())
    assert path.is_file()
    assert store.load("upbit", "KRW-BTC", "1d").empty
    # 기존 데이터가 있을 때 빈 프레임 저장은 아무것도 바꾸지 않는다
    store.save("upbit", "KRW-BTC", "1d", daily_df)
    mtime = path.stat().st_mtime_ns
    store.save("upbit", "KRW-BTC", "1d", empty_candles_df())
    assert path.stat().st_mtime_ns == mtime
    pd.testing.assert_frame_equal(store.load("upbit", "KRW-BTC", "1d"), daily_df)


def test_save_rejects_bad_frame(store: CandleStore, daily_df: pd.DataFrame) -> None:
    with pytest.raises(DataError):
        store.save("upbit", "KRW-BTC", "1d", daily_df.drop(columns=["close"]))
    assert not store.exists("upbit", "KRW-BTC", "1d")


def test_load_filters_by_range(store: CandleStore, candles_df: pd.DataFrame) -> None:
    store.save("upbit", "KRW-BTC", "1h", candles_df)
    start, end = _ts(candles_df, 30), _ts(candles_df, 40)
    out = store.load("upbit", "KRW-BTC", "1h", start=start, end=end)
    pd.testing.assert_frame_equal(out, candles_df.iloc[30:41].reset_index(drop=True))
    out = store.load("upbit", "KRW-BTC", "1h", start=start.isoformat())
    pd.testing.assert_frame_equal(out, candles_df.iloc[30:].reset_index(drop=True))


def test_load_corrupt_files(store: CandleStore) -> None:
    p = store.path("upbit", "KRW-BTC", "1d")
    p.parent.mkdir(parents=True)
    p.write_text("foo,bar\n1,2\n", encoding="utf-8")
    with pytest.raises(DataError, match="형식 오류"):
        store.load("upbit", "KRW-BTC", "1d")
    p.write_text("timestamp,open,high,low,close,volume\nnot-a-date,1,1,1,1,1\n", encoding="utf-8")
    with pytest.raises(DataError, match="timestamp"):
        store.load("upbit", "KRW-BTC", "1d")
    p.write_text("timestamp,open,high,low,close,volume\n", encoding="utf-8")  # 헤더만
    assert store.load("upbit", "KRW-BTC", "1d").empty
    p.write_text("", encoding="utf-8")  # 완전히 빈 파일
    assert store.load("upbit", "KRW-BTC", "1d").empty


def test_list_available(
    store: CandleStore, tmp_data_dir: Path, daily_df: pd.DataFrame, candles_df: pd.DataFrame
) -> None:
    assert CandleStore(tmp_data_dir / "nope").list_available() == []
    assert store.list_available() == []
    store.save("upbit", "KRW-BTC", "1d", daily_df)
    store.save("binance", "BTC/USDT", "1h", candles_df)
    store.save("kis", "005930", "1d", daily_df.iloc[:3])
    # 레이아웃에 맞지 않는 파일들은 무시
    (tmp_data_dir / "upbit" / "notes.txt").write_text("x", encoding="utf-8")
    (tmp_data_dir / "upbit" / "weird.csv").write_text("x", encoding="utf-8")
    (tmp_data_dir / "upbit" / "KRW-BTC_2h.csv").write_text("x", encoding="utf-8")
    (tmp_data_dir / "stray_1d.csv").write_text("x", encoding="utf-8")
    assert store.list_available() == [
        ("binance", "BTC_USDT", "1h"),
        ("kis", "005930", "1d"),
        ("upbit", "KRW-BTC", "1d"),
    ]


# ---------------------------------------------------------------------------- download
@pytest.fixture
def enough_daily(daily_candles: list[Candle]) -> list[Candle]:
    if len(daily_candles) < 160:
        pytest.skip(f"페이지네이션 테스트에는 일봉 160개 이상 필요 (받은 개수 {len(daily_candles)})")
    return daily_candles


def test_download_paginates_backwards(
    store: CandleStore, enough_daily: list[Candle], daily_df: pd.DataFrame, no_sleep: list[float]
) -> None:
    c = enough_daily
    broker = SliceBroker(c)
    progress: list[tuple[int, datetime]] = []
    start, end = c[20].timestamp, c[150].timestamp
    out = store.download(
        broker,
        "KRW-BTC",
        "1d",
        start=start,
        end=end,
        batch=50,
        sleep=0.01,
        progress=lambda n, t: progress.append((n, t)),
    )
    # 페이지: [101..150] → [51..100] → [1..50] (oldest=c[1] <= start 이므로 종료)
    assert [limit for limit, _ in broker.calls] == [50, 50, 50]
    assert broker.calls[0][1] == end + timedelta(days=1)  # end 캔들을 포함하기 위해 한 간격 뒤
    assert broker.calls[1][1] == c[101].timestamp
    assert broker.calls[2][1] == c[51].timestamp
    assert no_sleep == [0.01, 0.01]  # 페이지 사이에만 sleep
    assert progress == [(50, c[101].timestamp), (100, c[51].timestamp), (150, c[1].timestamp)]
    assert all(t.tzinfo is not None for _, t in progress)

    pd.testing.assert_frame_equal(out, daily_df.iloc[20:151].reset_index(drop=True))
    assert out["timestamp"].iloc[-1] == end  # end 포함
    # 파일에는 받은 캔들 전부 (범위 밖 여분 c[1..19] 포함) 저장
    cached = store.load("upbit", "KRW-BTC", "1d")
    pd.testing.assert_frame_equal(cached, daily_df.iloc[1:151].reset_index(drop=True))


def test_download_handles_inclusive_end_broker(
    store: CandleStore, enough_daily: list[Candle], daily_df: pd.DataFrame, no_sleep: list[float]
) -> None:
    c = enough_daily
    broker = SliceBroker(c, inclusive_end=True)
    out = store.download(
        broker, "KRW-BTC", "1d", start=c[20].timestamp, end=c[150].timestamp, batch=50, sleep=0
    )
    pd.testing.assert_frame_equal(out, daily_df.iloc[20:151].reset_index(drop=True))
    assert len(broker.calls) <= 6  # 겹치는 한 개씩 다시 받아도 무한 루프 없이 끝난다
    assert no_sleep == []  # sleep=0 이면 쉬지 않는다


def test_download_stops_when_broker_makes_no_progress(
    store: CandleStore, enough_daily: list[Candle], daily_df: pd.DataFrame, no_sleep: list[float]
) -> None:
    c = enough_daily
    broker = SliceBroker(c, stuck=True)  # end 를 무시하고 항상 최신 50개
    out = store.download(broker, "KRW-BTC", "1d", start=c[20].timestamp, end=c[150].timestamp, batch=50)
    assert len(broker.calls) == 2  # 두 번째 페이지에 새 캔들이 없으면 종료
    expected = filter_candles_df(candles_to_df(c[-50:]), c[20].timestamp, c[150].timestamp)
    pd.testing.assert_frame_equal(out, expected)
    pd.testing.assert_frame_equal(
        store.load("upbit", "KRW-BTC", "1d"), daily_df.iloc[-50:].reset_index(drop=True)
    )


def test_download_nothing_fetched(
    store: CandleStore, enough_daily: list[Candle], no_sleep: list[float]
) -> None:
    broker = SliceBroker([])
    progress: list[tuple[int, datetime]] = []
    out = store.download(
        broker, "KRW-BTC", "1d", start="2024-01-01", end="2024-02-01", progress=progress.append
    )  # type: ignore[arg-type]
    assert out.empty and list(out.columns) == CANDLE_COLUMNS
    assert len(broker.calls) == 1
    assert progress == []
    assert not store.exists("upbit", "KRW-BTC", "1d")


def test_download_end_defaults_to_now(
    store: CandleStore, enough_daily: list[Candle], daily_df: pd.DataFrame, no_sleep: list[float]
) -> None:
    c = enough_daily
    n = len(c)
    broker = SliceBroker(c)
    before = utcnow()
    out = store.download(broker, "KRW-BTC", "1d", start=c[n - 10].timestamp)
    first_end = broker.calls[0][1]
    assert first_end is not None and before <= first_end <= utcnow()  # 현재 시각을 넘지 않음
    pd.testing.assert_frame_equal(out, daily_df.iloc[n - 10 :].reset_index(drop=True))


def test_download_accepts_date_strings(
    store: CandleStore, enough_daily: list[Candle], daily_df: pd.DataFrame, no_sleep: list[float]
) -> None:
    c = enough_daily
    start = c[180].timestamp.strftime("%Y-%m-%d")
    end = c[190].timestamp.strftime("%Y-%m-%d")
    out = store.download(SliceBroker(c), "KRW-BTC", "1d", start=start, end=end, batch=200)
    pd.testing.assert_frame_equal(out, daily_df.iloc[180:191].reset_index(drop=True))


def test_download_merges_with_existing_cache(
    store: CandleStore, enough_daily: list[Candle], daily_df: pd.DataFrame, no_sleep: list[float]
) -> None:
    c = enough_daily
    store.save("upbit", "KRW-BTC", "1d", daily_df.iloc[:10])
    out = store.download(
        SliceBroker(c), "KRW-BTC", "1d", start=c[100].timestamp, end=c[150].timestamp, batch=50
    )
    pd.testing.assert_frame_equal(out, daily_df.iloc[100:151].reset_index(drop=True))
    cached = store.load("upbit", "KRW-BTC", "1d")
    expected = pd.concat([daily_df.iloc[:10], daily_df.iloc[51:151]], ignore_index=True)
    pd.testing.assert_frame_equal(cached, expected)


def test_download_argument_validation(store: CandleStore, enough_daily: list[Candle]) -> None:
    broker = SliceBroker(enough_daily)
    with pytest.raises(DataError, match="start"):
        store.download(broker, "KRW-BTC", "1d", start="2024-02-01", end="2024-01-01")
    with pytest.raises(DataError, match="batch"):
        store.download(broker, "KRW-BTC", "1d", start="2024-01-01", batch=0)
    with pytest.raises(DataError, match="sleep"):
        store.download(broker, "KRW-BTC", "1d", start="2024-01-01", sleep=-1)
    with pytest.raises(DataError):
        store.download(broker, "KRW-BTC", "2h", start="2024-01-01")
    with pytest.raises(DataError):
        store.download(broker, "KRW-BTC", "1d", start=None)  # type: ignore[arg-type]
    with pytest.raises(DataError):
        store.download(broker, "KRW-BTC", "1d", start="not a date")
    assert broker.calls == []  # 검증 실패 시 브로커를 호출하지 않는다


def test_download_propagates_broker_errors(store: CandleStore, enough_daily: list[Candle]) -> None:
    class FailingBroker(SliceBroker):
        def get_candles(self, *args: object, **kwargs: object) -> list[Candle]:
            raise BrokerError("거래소 점검 중")

    with pytest.raises(BrokerError, match="점검"):
        store.download(FailingBroker(enough_daily), "KRW-BTC", "1d", start="2024-01-01")
    assert not store.exists("upbit", "KRW-BTC", "1d")
