"""yfinance 로더 테스트.

네트워크를 쓰지 않는다: ``yfinance.download`` 를 monkeypatch 하여 conftest 의 **실제 Upbit 캔들**(daily_df,
candles_df) 을 yfinance 가 돌려주는 모양(단일 컬럼 / MultiIndex 컬럼, 거래소 시간대 인덱스) 으로 바꿔 돌려준다.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from freezegun import freeze_time

from tradingbot.data.yfinance_feed import (
    INTRADAY_MAX_DAYS,
    YF_INTERVAL_MAP,
    krx_to_yfinance,
    load_yfinance,
    to_yfinance_interval,
    yfinance_to_candles_df,
)
from tradingbot.exceptions import ConfigError, DataError
from tradingbot.models import utcnow
from tradingbot.strategies.base import CANDLE_COLUMNS

yfinance = pytest.importorskip("yfinance")

UTC = timezone.utc
Layout = str  # "single" | "price_ticker" | "ticker_price"


# ---------------------------------------------------------------------------- 헬퍼
def _assemble(df: pd.DataFrame, idx: pd.DatetimeIndex, ticker: str, layout: Layout) -> pd.DataFrame:
    out = pd.DataFrame(
        {
            "Open": df["open"].to_numpy(),
            "High": df["high"].to_numpy(),
            "Low": df["low"].to_numpy(),
            "Close": df["close"].to_numpy(),
            "Volume": df["volume"].to_numpy(),
        },
        index=idx,
    )
    if layout == "price_ticker":  # yfinance 기본 (group_by="column"): (Price, Ticker)
        out.columns = pd.MultiIndex.from_product([out.columns, [ticker]], names=["Price", "Ticker"])
    elif layout == "ticker_price":  # group_by="ticker": (Ticker, Price)
        out.columns = pd.MultiIndex.from_product([[ticker], out.columns], names=["Ticker", "Price"])
    elif layout != "single":
        raise ValueError(layout)
    return out


def as_yf_daily(
    df: pd.DataFrame, *, tz: str = "Asia/Seoul", ticker: str = "005930.KS", layout: Layout = "single"
):
    """실제 일봉(00:00 UTC) 을 yfinance 일봉 모양(거래소 현지 자정, 'Date' 인덱스) 으로."""
    idx = pd.DatetimeIndex(df["timestamp"]).tz_localize(None).tz_localize(tz)
    idx.name = "Date"
    return _assemble(df, idx, ticker, layout)


def as_yf_intraday(
    df: pd.DataFrame, *, tz: str = "America/New_York", ticker: str = "AAPL", layout: Layout = "single"
):
    """실제 시간봉(UTC) 을 yfinance 분봉 모양(거래소 시간대, 'Datetime' 인덱스) 으로."""
    idx = pd.DatetimeIndex(df["timestamp"]).tz_convert(tz)
    idx.name = "Datetime"
    return _assemble(df, idx, ticker, layout)


class FakeDownload:
    """yfinance.download 대역: 호출 인자를 기록하고 미리 정한 결과를 돌려준다."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, dict[str, object]]] = []
        self.result: pd.DataFrame | None = None
        self.error: Exception | None = None

    def __call__(self, tickers: object, **kwargs: object) -> pd.DataFrame | None:
        self.calls.append((tickers, kwargs))
        if self.error is not None:
            raise self.error
        return self.result

    @property
    def last_kwargs(self) -> dict[str, object]:
        return self.calls[-1][1]


@pytest.fixture
def fake_download(monkeypatch: pytest.MonkeyPatch) -> FakeDownload:
    fake = FakeDownload()
    monkeypatch.setattr(yfinance, "download", fake)
    return fake


def _ts(df: pd.DataFrame, i: int) -> datetime:
    return df["timestamp"].iloc[i].to_pydatetime()


# ---------------------------------------------------------------------------- interval / 티커 헬퍼
def test_interval_map() -> None:
    assert YF_INTERVAL_MAP == {
        "1m": "1m",
        "5m": "5m",
        "15m": "15m",
        "30m": "30m",
        "1h": "60m",
        "1d": "1d",
        "1w": "1wk",
    }
    assert to_yfinance_interval("1h") == "60m"
    assert to_yfinance_interval("1w") == "1wk"


@pytest.mark.parametrize("interval", ["3m", "10m", "4h", "2m", "1mo", "", "1D"])
def test_unsupported_interval(interval: str) -> None:
    with pytest.raises(DataError, match="지원하지 않는"):
        to_yfinance_interval(interval)


@pytest.mark.parametrize(
    ("code", "market", "expected"),
    [
        ("005930", "KS", "005930.KS"),
        ("005930", "ks", "005930.KS"),
        ("005930", "KOSPI", "005930.KS"),
        ("035720", "KQ", "035720.KQ"),
        ("035720", "kosdaq", "035720.KQ"),
        ("035720", ".KQ", "035720.KQ"),
        (" 005930 ", "KS", "005930.KS"),
        ("005930.ks", "KQ", "005930.KS"),  # 이미 접미사가 있으면 그대로
        ("035720.KQ", "KS", "035720.KQ"),
        ("00593A", "KS", "00593A.KS"),  # 영숫자 코드
    ],
)
def test_krx_to_yfinance(code: str, market: str, expected: str) -> None:
    assert krx_to_yfinance(code, market) == expected


def test_krx_to_yfinance_default_market() -> None:
    assert krx_to_yfinance("005930") == "005930.KS"


@pytest.mark.parametrize(
    ("code", "market"),
    [
        ("5930", "KS"),
        ("", "KS"),
        ("0059300", "KS"),
        ("005930", "NYSE"),
        ("005930.XX", "KS"),
        ("AAPL.KS", "KS"),
        ("00-930", "KS"),
    ],
)
def test_krx_to_yfinance_rejects(code: str, market: str) -> None:
    with pytest.raises(ConfigError):
        krx_to_yfinance(code, market)


# ---------------------------------------------------------------------------- 일봉
@pytest.mark.parametrize("layout", ["single", "price_ticker", "ticker_price"])
def test_load_daily_layouts(fake_download: FakeDownload, daily_df: pd.DataFrame, layout: Layout) -> None:
    fake_download.result = as_yf_daily(daily_df, layout=layout)
    start_s = _ts(daily_df, 10).strftime("%Y-%m-%d")
    end_s = _ts(daily_df, 100).strftime("%Y-%m-%d")

    out = load_yfinance("005930.KS", "1d", start_s, end_s)

    pd.testing.assert_frame_equal(out, daily_df.iloc[10:101].reset_index(drop=True))
    tickers, kw = fake_download.calls[0]
    assert tickers == "005930.KS"
    assert kw["interval"] == "1d"
    assert kw["auto_adjust"] is True
    assert kw["progress"] is False
    # 일봉은 날짜 문자열로 요청 (거래소 현지 거래일 기준), end 는 미포함이므로 하루 뒤
    assert kw["start"] == start_s
    assert kw["end"] == (_ts(daily_df, 100) + timedelta(days=1)).strftime("%Y-%m-%d")


def test_daily_local_midnight_becomes_utc_date(fake_download: FakeDownload, daily_df: pd.DataFrame) -> None:
    """KRX 일봉 2024-01-02 00:00+09:00 은 2024-01-01T15:00Z 가 아니라 2024-01-02T00:00Z 로 표기된다."""
    for tz in ("Asia/Seoul", "America/New_York", "Europe/London"):
        fake_download.result = as_yf_daily(daily_df, tz=tz)
        out = load_yfinance("X", "1d", _ts(daily_df, 0), _ts(daily_df, -1))
        pd.testing.assert_frame_equal(out, daily_df)


def test_daily_non_midnight_index_is_floored(daily_df: pd.DataFrame) -> None:
    raw = as_yf_daily(daily_df.iloc[:5])
    raw.index = raw.index + pd.Timedelta(hours=9)  # 자정이 아닌 현지 시각이 와도 날짜만 취한다
    out = yfinance_to_candles_df(raw, symbol="X", interval="1d")
    pd.testing.assert_frame_equal(out, daily_df.iloc[:5])


def test_weekly_interval(fake_download: FakeDownload, daily_df: pd.DataFrame) -> None:
    fake_download.result = as_yf_daily(daily_df)
    out = load_yfinance("005930.KS", "1w", _ts(daily_df, 0), _ts(daily_df, -1))
    assert fake_download.last_kwargs["interval"] == "1wk"
    pd.testing.assert_frame_equal(out, daily_df)


def test_daily_end_defaults_to_now(fake_download: FakeDownload, daily_df: pd.DataFrame) -> None:
    # 시계를 고정해 "지금 + 1일" 계산이 UTC 자정을 넘나들며 흔들리지 않게 한다 (실데이터 구간 이후 시각).
    fake_download.result = as_yf_daily(daily_df)
    with freeze_time("2026-10-05 23:59:59+00:00"):
        out = load_yfinance("005930.KS", "1d", _ts(daily_df, 150))
    pd.testing.assert_frame_equal(out, daily_df.iloc[150:].reset_index(drop=True))
    assert fake_download.last_kwargs["end"] == "2026-10-06"


# ---------------------------------------------------------------------------- 분봉
@pytest.mark.parametrize("layout", ["single", "price_ticker", "ticker_price"])
def test_load_intraday_converts_to_utc(
    fake_download: FakeDownload, candles_df: pd.DataFrame, layout: Layout
) -> None:
    fake_download.result = as_yf_intraday(candles_df, layout=layout)
    start, end = _ts(candles_df, 5), _ts(candles_df, 50)

    out = load_yfinance("AAPL", "1h", start, end)

    pd.testing.assert_frame_equal(out, candles_df.iloc[5:51].reset_index(drop=True))
    kw = fake_download.last_kwargs
    assert kw["interval"] == "60m"
    assert kw["start"] == start  # 분봉은 aware datetime 으로 요청
    assert kw["end"] == end + timedelta(hours=1)  # end 캔들 포함을 위해 한 간격 뒤
    assert all(t.tzinfo is not None for t in out["timestamp"])


def test_intraday_naive_index_is_utc(fake_download: FakeDownload, candles_df: pd.DataFrame) -> None:
    raw = as_yf_intraday(candles_df, tz="UTC")
    raw.index = raw.index.tz_localize(None)
    fake_download.result = raw
    out = load_yfinance("AAPL", "1h", _ts(candles_df, 0), _ts(candles_df, -1))
    pd.testing.assert_frame_equal(out, candles_df)


def test_intraday_naive_start_treated_as_utc(fake_download: FakeDownload, candles_df: pd.DataFrame) -> None:
    fake_download.result = as_yf_intraday(candles_df)
    start, end = _ts(candles_df, 5), _ts(candles_df, 50)
    out = load_yfinance("AAPL", "1h", start.replace(tzinfo=None), end.replace(tzinfo=None))
    pd.testing.assert_frame_equal(out, candles_df.iloc[5:51].reset_index(drop=True))


def test_intraday_old_start_warns(
    fake_download: FakeDownload, candles_df: pd.DataFrame, caplog: pytest.LogCaptureFixture
) -> None:
    fake_download.result = as_yf_intraday(candles_df)
    old_start = utcnow() - timedelta(days=INTRADAY_MAX_DAYS + 30)
    with caplog.at_level(logging.WARNING, logger="tradingbot.data.yfinance_feed"):
        load_yfinance("AAPL", "1h", old_start)
    assert any(f"{INTRADAY_MAX_DAYS}일" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------- 오류 경로
def test_empty_result_raises(fake_download: FakeDownload) -> None:
    fake_download.result = pd.DataFrame()
    with pytest.raises(DataError, match="AAPL"):
        load_yfinance("AAPL", "1d", "2024-01-01", "2024-02-01")
    fake_download.result = None
    with pytest.raises(DataError):
        load_yfinance("AAPL", "1d", "2024-01-01", "2024-02-01")


def test_nothing_in_requested_range_raises(fake_download: FakeDownload, daily_df: pd.DataFrame) -> None:
    fake_download.result = as_yf_daily(daily_df)
    start = _ts(daily_df, 0) - timedelta(days=30)
    end = _ts(daily_df, 0) - timedelta(days=20)
    with pytest.raises(DataError, match="받지 못했습니다"):
        load_yfinance("005930.KS", "1d", start, end)


def test_multiple_tickers_rejected(fake_download: FakeDownload, daily_df: pd.DataFrame) -> None:
    a = as_yf_daily(daily_df, ticker="005930.KS", layout="price_ticker")
    b = as_yf_daily(daily_df, ticker="000660.KS", layout="price_ticker")
    fake_download.result = pd.concat([a, b], axis=1)
    with pytest.raises(DataError, match="단일 심볼"):
        load_yfinance("005930.KS 000660.KS", "1d", _ts(daily_df, 0), _ts(daily_df, -1))


def test_download_exception_becomes_data_error(fake_download: FakeDownload) -> None:
    fake_download.error = RuntimeError("Yahoo 응답 없음")
    with pytest.raises(DataError, match="다운로드 실패") as info:
        load_yfinance("AAPL", "1d", "2024-01-01", "2024-02-01")
    assert isinstance(info.value.__cause__, RuntimeError)


def test_missing_yfinance_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "yfinance", None)  # import yfinance → ImportError
    with pytest.raises(ConfigError, match="pip install yfinance"):
        load_yfinance("AAPL", "1d", "2024-01-01", "2024-02-01")


def test_argument_validation(fake_download: FakeDownload) -> None:
    with pytest.raises(DataError):
        load_yfinance("AAPL", "4h", "2024-01-01")
    with pytest.raises(DataError, match="start"):
        load_yfinance("AAPL", "1d", "2024-02-01", "2024-01-01")
    with pytest.raises(DataError):
        load_yfinance("AAPL", "1d", None)  # type: ignore[arg-type]
    with pytest.raises(DataError):
        load_yfinance("   ", "1d", "2024-01-01")
    with pytest.raises(DataError):
        load_yfinance("AAPL", "1d", "2024-13-45")
    assert fake_download.calls == []  # 인자 오류면 yfinance 를 호출하지 않는다


# ---------------------------------------------------------------------------- 변환 함수 단독
def test_convert_ignores_adj_close_and_lowercase(fake_download: FakeDownload, daily_df: pd.DataFrame) -> None:
    raw = as_yf_daily(daily_df)
    raw["Adj Close"] = raw["Close"]  # auto_adjust=False 일 때 붙는 컬럼
    raw = raw.rename(columns={"Volume": "volume"})
    fake_download.result = raw
    out = load_yfinance("005930.KS", "1d", _ts(daily_df, 0), _ts(daily_df, -1), auto_adjust=False)
    assert list(out.columns) == CANDLE_COLUMNS
    pd.testing.assert_frame_equal(out, daily_df)
    assert fake_download.last_kwargs["auto_adjust"] is False


def test_convert_missing_volume_filled_with_zero(
    daily_df: pd.DataFrame, caplog: pytest.LogCaptureFixture
) -> None:
    raw = as_yf_daily(daily_df.iloc[:5]).drop(columns=["Volume"])
    with caplog.at_level(logging.WARNING, logger="tradingbot.data.yfinance_feed"):
        out = yfinance_to_candles_df(raw, symbol="X", interval="1d")
    assert (out["volume"] == 0.0).all()
    expected = daily_df.iloc[:5].copy()
    expected["volume"] = 0.0
    pd.testing.assert_frame_equal(out, expected)
    assert any("volume" in r.getMessage() for r in caplog.records)


def test_convert_missing_ohlc_raises(daily_df: pd.DataFrame) -> None:
    raw = as_yf_daily(daily_df.iloc[:5]).drop(columns=["Close"])
    with pytest.raises(DataError, match="close"):
        yfinance_to_candles_df(raw, symbol="X", interval="1d")
    weird = as_yf_daily(daily_df.iloc[:5], layout="price_ticker")
    weird.columns = pd.MultiIndex.from_product([["Foo", "Bar", "Baz", "Qux", "Quux"], ["X"]])
    with pytest.raises(DataError, match="OHLC"):
        yfinance_to_candles_df(weird, symbol="X", interval="1d")


def test_convert_drops_nan_rows(candles_df: pd.DataFrame) -> None:
    raw = as_yf_intraday(candles_df.iloc[:10])
    raw.iloc[3, raw.columns.get_loc("Close")] = float("nan")
    out = yfinance_to_candles_df(raw, symbol="AAPL", interval="1h")
    assert len(out) == 9
    assert _ts(candles_df, 3) not in set(out["timestamp"])


def test_convert_all_nan_raises(candles_df: pd.DataFrame) -> None:
    raw = as_yf_intraday(candles_df.iloc[:3])
    raw[:] = float("nan")
    with pytest.raises(DataError, match="유효한 캔들"):
        yfinance_to_candles_df(raw, symbol="AAPL", interval="1h")


def test_convert_non_datetime_index(daily_df: pd.DataFrame) -> None:
    raw = as_yf_daily(daily_df.iloc[:5])
    raw.index = pd.Index([d.strftime("%Y-%m-%d") for d in raw.index])  # 문자열 인덱스도 해석
    out = yfinance_to_candles_df(raw, symbol="X", interval="1d")
    pd.testing.assert_frame_equal(out, daily_df.iloc[:5])
    raw.index = pd.Index(["a", "b", "c", "d", "e"])
    with pytest.raises(DataError, match="인덱스"):
        yfinance_to_candles_df(raw, symbol="X", interval="1d")
