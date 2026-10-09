"""yfinance (Yahoo Finance) 과거 캔들 로더 — 선택 의존성.

설치: ``pip install yfinance`` (또는 ``pip install "tradingbot[yfinance]"``). 이 모듈을 import 하는 것만으로는
yfinance 를 읽지 않으며, ``load_yfinance()`` 호출 시점에 지연 import 한다 (없으면 ConfigError).

설치된 yfinance 1.7.0 소스(``yfinance/multi.py``, ``yfinance/scrapers/history.py``, ``yfinance/utils.py``,
2026-10 확인) 로 검증한 사실:

- ``yf.download(tickers, start=None, end=None, interval="1d", auto_adjust=True, group_by="column",
  multi_level_index=True, progress=True, ...)``.
- 최근 버전은 단일 티커도 **MultiIndex 컬럼** ``(Price, Ticker)`` 를 돌려준다 (``group_by="ticker"`` 면
  ``(Ticker, Price)``). ``multi_level_index=False`` 이거나 구버전이면 단일 레벨 ``Open/High/Low/Close/Volume``
  (``auto_adjust=False`` 면 ``Adj Close`` 추가). 이 모듈은 세 가지 형태를 모두 처리한다.
- 유효 interval: ``1m,2m,5m,15m,30m,60m,90m,1h,1d,5d,1wk,1mo,3mo``. 분봉은 **최근 60일**, 1m 은 최근 약 7~8일만
  제공된다 (Yahoo 제한).
- ``start`` 는 포함, ``end`` 는 **미포함**. 문자열('YYYY-MM-DD')/naive 값은 **거래소 현지 시간대** 로 해석되고
  aware 값은 거래소 시간대로 변환된다 (``utils._parse_user_dt``).
- 결과 인덱스는 거래소 시간대의 tz-aware DatetimeIndex. 일봉/주봉은 현지 자정(``Date``), 분봉은 실제 시각
  (``Datetime``). ``yf.download`` 는 티커 오류를 로그로만 남기고 빈 DataFrame 을 돌려준다.

시간대 처리
- 분봉(1m~1h): 거래소 시간대 → UTC 로 변환한다.
- 일봉/주봉(1d, 1w): Yahoo 의 "현지 자정" 은 사실상 거래일(date) 이므로 **그 날짜의 00:00 UTC** 로 표기한다.
  (Upbit 일봉 ``candle_date_time_utc`` 와 같은 규약. 순수 변환하면 KRX 일봉이 전날 15:00Z 가 되어 날짜 필터와
  리포트가 하루씩 어긋난다.)

한국 주식 티커
- KOSPI 는 ``005930.KS`` (삼성전자), KOSDAQ 은 ``035720.KQ`` 처럼 거래소 접미사가 필요하다.
  ``krx_to_yfinance("005930")`` → ``"005930.KS"``, ``krx_to_yfinance("035720", "KQ")`` → ``"035720.KQ"``.
- 암호화폐는 ``BTC-USD``, ``BTC-KRW`` 처럼 Yahoo 표기를 직접 쓴다.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from types import ModuleType

import pandas as pd

from tradingbot.data.store import DateLike, filter_candles_df, normalize_candles_df, to_utc_datetime
from tradingbot.exceptions import ConfigError, DataError
from tradingbot.models import interval_to_seconds, utcnow

logger = logging.getLogger(__name__)

__all__ = [
    "INTERDAY_INTERVALS",
    "INTRADAY_MAX_DAYS",
    "YF_INTERVAL_MAP",
    "krx_to_yfinance",
    "load_yfinance",
    "to_yfinance_interval",
    "yfinance_to_candles_df",
]

#: 봇 interval → yfinance interval
YF_INTERVAL_MAP: dict[str, str] = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "60m",
    "1d": "1d",
    "1w": "1wk",
}
#: 일 단위 이상 (인덱스가 "현지 자정" 인 interval)
INTERDAY_INTERVALS: frozenset[str] = frozenset({"1d", "1w"})
#: Yahoo 가 분봉을 제공하는 최대 과거 일수
INTRADAY_MAX_DAYS = 60
_KRX_SUFFIX = {"KS": "KS", "KOSPI": "KS", "KQ": "KQ", "KOSDAQ": "KQ"}
_OHLC = ("open", "high", "low", "close")


# ---------------------------------------------------------------------------- 헬퍼
def to_yfinance_interval(interval: str) -> str:
    """봇 interval 을 yfinance interval 로 (``1h`` → ``60m``, ``1w`` → ``1wk``). 미지원이면 DataError."""
    try:
        return YF_INTERVAL_MAP[interval]
    except KeyError as e:
        raise DataError(
            f"yfinance 가 지원하지 않는 캔들 간격: {interval!r} (가능: {', '.join(YF_INTERVAL_MAP)})"
        ) from e


def krx_to_yfinance(code: str, market: str = "KS") -> str:
    """KRX 종목코드(6자리) → Yahoo 티커. ``market`` 은 ``KS``(KOSPI, 기본) 또는 ``KQ``(KOSDAQ).

    이미 ``.KS``/``.KQ`` 접미사가 붙어 있으면 그대로(대문자화) 돌려준다. 잘못된 코드는 ConfigError.
    """
    raw = str(code).strip().upper()
    if "." in raw:
        base, suffix = raw.rsplit(".", 1)
        if suffix in ("KS", "KQ") and _is_krx_code(base):
            return f"{base}.{suffix}"
        raise ConfigError(f"KRX 티커 형식 오류: {code!r} (예: 005930.KS, 035720.KQ)")
    if not _is_krx_code(raw):
        raise ConfigError(f"KRX 종목코드는 6자리 영숫자여야 합니다: {code!r} (예: 005930)")
    key = str(market).strip().upper().lstrip(".")
    if key not in _KRX_SUFFIX:
        raise ConfigError(f"KRX 시장 구분은 KS(KOSPI) 또는 KQ(KOSDAQ) 이어야 합니다: {market!r}")
    return f"{raw}.{_KRX_SUFFIX[key]}"


def _is_krx_code(s: str) -> bool:
    return len(s) == 6 and s.isascii() and s.isalnum()


def _import_yfinance() -> ModuleType:
    try:
        import yfinance
    except ImportError as e:
        raise ConfigError(
            "yfinance 가 설치되어 있지 않습니다. `pip install yfinance` "
            '(또는 `pip install "tradingbot[yfinance]"`) 후 다시 시도하세요'
        ) from e
    return yfinance


def _flatten_columns(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """MultiIndex 컬럼 ``(Price, Ticker)`` / ``(Ticker, Price)`` 를 단일 레벨 가격 컬럼으로."""
    cols = raw.columns
    if not isinstance(cols, pd.MultiIndex):
        return raw
    price_level: int | None = None
    for level in range(cols.nlevels):
        names = {str(v).strip().lower() for v in cols.get_level_values(level)}
        if {"open", "close"} <= names:
            price_level = level
            break
    if price_level is None:
        raise DataError(f"yfinance 응답 컬럼에서 OHLC 를 찾지 못했습니다 ({symbol}): {list(cols)}")
    for level in range(cols.nlevels):
        if level == price_level:
            continue
        tickers = sorted({str(v) for v in cols.get_level_values(level)})
        if len(tickers) != 1:
            raise DataError(f"load_yfinance 는 단일 심볼만 지원합니다 (받은 티커: {tickers})")
    flat = raw.copy()
    flat.columns = pd.Index(cols.get_level_values(price_level))
    return flat


def yfinance_to_candles_df(
    raw: pd.DataFrame | None, *, symbol: str = "", interval: str = "1d"
) -> pd.DataFrame:
    """``yf.download`` 결과를 규약 캔들 DataFrame 으로. 비어 있으면 DataError.

    - 컬럼: 단일/MultiIndex 모두 처리, 대소문자 무시 (``Adj Close`` 는 버림)
    - 인덱스: naive 면 UTC 로 간주. 분봉은 UTC 로 변환, 일/주봉은 현지 날짜를 00:00 UTC 로 표기
    """
    if raw is None or len(raw) == 0:
        raise DataError(f"yfinance 에서 받은 데이터가 없습니다 ({symbol} {interval})")
    df = _flatten_columns(raw, symbol)
    df = df.rename(columns={c: str(c).strip().lower().replace(" ", "_") for c in df.columns})
    if df.columns.duplicated().any():
        raise DataError(f"yfinance 응답에 중복 컬럼이 있습니다 ({symbol}): {list(df.columns)}")
    missing = [c for c in _OHLC if c not in df.columns]
    if missing:
        raise DataError(f"yfinance 응답에 컬럼이 없습니다 ({symbol}): {missing}")
    if "volume" not in df.columns:
        logger.warning("%s: yfinance 응답에 volume 이 없어 0 으로 채웁니다", symbol)
        df = df.assign(volume=0.0)

    idx = df.index
    if not isinstance(idx, pd.DatetimeIndex):
        try:
            idx = pd.DatetimeIndex(pd.to_datetime(idx))
        except (ValueError, TypeError) as e:
            raise DataError(f"yfinance 인덱스를 시각으로 해석할 수 없습니다 ({symbol}): {e}") from e
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    if interval in INTERDAY_INTERVALS:
        # 거래소 현지 자정(거래일) → 같은 날짜의 00:00 UTC
        idx = idx.tz_localize(None).normalize().tz_localize("UTC")
    else:
        idx = idx.tz_convert("UTC")

    out = pd.DataFrame(
        {
            "timestamp": idx,
            "open": df["open"].to_numpy(),
            "high": df["high"].to_numpy(),
            "low": df["low"].to_numpy(),
            "close": df["close"].to_numpy(),
            "volume": df["volume"].to_numpy(),
        }
    )
    result = normalize_candles_df(out)
    if result.empty:
        raise DataError(f"yfinance 응답에 유효한 캔들이 없습니다 ({symbol} {interval})")
    return result


# ---------------------------------------------------------------------------- 로더
def load_yfinance(
    symbol: str,
    interval: str,
    start: DateLike,
    end: DateLike = None,
    *,
    auto_adjust: bool = True,
) -> pd.DataFrame:
    """Yahoo Finance 에서 ``symbol`` 의 캔들을 받아 규약 DataFrame 으로 돌려준다.

    - ``interval``: 1m, 5m, 15m, 30m, 1h, 1d, 1w (그 외는 DataError)
    - ``start``/``end``: datetime(naive 는 UTC), 'YYYY-MM-DD', ISO 문자열. **양끝 포함**. ``end`` None 은 지금.
    - ``auto_adjust=True``: 배당/분할 조정 가격 (기본, 백테스트에 권장)
    - 결과가 비면 DataError (심볼/기간/간격 확인. 분봉은 최근 60일만 가능)
    - yfinance 미설치는 ConfigError
    """
    sym = str(symbol).strip()
    if not sym:
        raise DataError("yfinance 심볼이 비어 있습니다")
    yf_interval = to_yfinance_interval(interval)
    start_dt = to_utc_datetime(start, name="start")
    if start_dt is None:
        raise DataError("load_yfinance 에는 start 가 필요합니다")
    now = utcnow()
    end_dt = to_utc_datetime(end, name="end") or now
    if start_dt > end_dt:
        raise DataError(f"start({start_dt.isoformat()}) 가 end({end_dt.isoformat()}) 보다 늦습니다")
    step = timedelta(seconds=interval_to_seconds(interval))

    interday = interval in INTERDAY_INTERVALS
    if not interday and now - start_dt > timedelta(days=INTRADAY_MAX_DAYS):
        logger.warning(
            "%s %s: Yahoo 분봉은 최근 %d일만 제공합니다. start=%s 이전 구간은 비어 있을 수 있습니다",
            sym,
            interval,
            INTRADAY_MAX_DAYS,
            start_dt.date(),
        )

    yf = _import_yfinance()
    # yfinance 의 end 는 미포함이므로 한 간격(일봉은 하루) 뒤를 넘긴다. 일/주봉은 날짜 문자열로 넘겨
    # 거래소 현지 "거래일" 기준으로 해석되게 하고, 분봉은 aware datetime 을 넘긴다.
    if interday:
        req_start: object = start_dt.date().isoformat()
        req_end: object = (end_dt + timedelta(days=1)).date().isoformat()
    else:
        req_start = start_dt
        req_end = end_dt + step
    logger.info("yfinance 다운로드 %s interval=%s start=%s end=%s", sym, yf_interval, req_start, req_end)
    try:
        raw = yf.download(
            sym,
            start=req_start,
            end=req_end,
            interval=yf_interval,
            auto_adjust=auto_adjust,
            progress=False,
        )
    except Exception as e:  # noqa: BLE001 - yfinance 는 requests/ValueError 등 다양한 예외를 던진다
        raise DataError(f"yfinance 다운로드 실패 ({sym} {interval}): {e}") from e

    df = yfinance_to_candles_df(raw, symbol=sym, interval=interval)
    df = filter_candles_df(df, start_dt, end_dt)
    if df.empty:
        raise DataError(
            f"yfinance 에서 {sym} {interval} {start_dt.date()}~{end_dt.date()} 구간의 캔들을 받지 못했습니다 "
            "(심볼/기간/간격 확인. 분봉은 최근 60일만 제공)"
        )
    logger.info(
        "yfinance %s %s: %d개 캔들 (%s ~ %s)",
        sym,
        interval,
        len(df),
        df["timestamp"].iloc[0],
        df["timestamp"].iloc[-1],
    )
    return df
