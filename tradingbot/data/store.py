"""캔들 데이터 저장소 (CSV 캐시) 와 과거 데이터 내려받기 (ARCHITECTURE §8).

파일 레이아웃
    ``{data_dir}/{broker}/{symbol_safe}_{interval}.csv``
    예) ``data/candles/upbit/KRW-BTC_1d.csv``, ``data/candles/binance/BTC_USDT_1h.csv``

CSV 형식
    컬럼 ``timestamp,open,high,low,close,volume``. ``timestamp`` 는 ISO 8601 UTC (``2024-01-02T00:00:00Z``),
    나머지는 float. 오래된→최신 순, timestamp 중복 없음.

규약 DataFrame (``normalize_candles_df`` 가 보장)
    ``tradingbot.strategies.base.candles_to_df`` 와 동일: 컬럼 ``timestamp(datetime64[us, UTC]), open, high,
    low, close, volume(float64)``, RangeIndex, 오래된→최신, timestamp 중복 제거(마지막 값 유지).

원칙: 이 모듈은 **실제 거래소에서 받은 캔들만** 저장한다. 샘플/데모 데이터를 만들지 않는다.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from tradingbot.exceptions import DataError
from tradingbot.models import INTERVAL_SECONDS, Candle, ensure_utc, interval_to_seconds, utcnow
from tradingbot.strategies.base import CANDLE_COLUMNS, candles_to_df
from tradingbot.utils.timeutil import parse_date

if TYPE_CHECKING:  # pragma: no cover
    from tradingbot.brokers.base import BaseBroker
    from tradingbot.config import AppConfig

logger = logging.getLogger(__name__)

__all__ = [
    "TIMESTAMP_FORMAT",
    "CandleStore",
    "DateLike",
    "ProgressCallback",
    "empty_candles_df",
    "filter_candles_df",
    "normalize_candles_df",
    "symbol_safe",
    "to_utc_datetime",
]

#: start/end 인자로 받는 타입. 문자열은 'YYYY-MM-DD' 또는 ISO 8601, naive 값은 UTC 로 간주.
DateLike = datetime | date | str | pd.Timestamp | None
#: download() 진행 콜백: (지금까지 받은 캔들 수, 가장 오래된 캔들 시각 UTC)
ProgressCallback = Callable[[int, datetime], None]

#: CSV 에 기록하는 timestamp 형식 (ISO 8601, UTC 'Z' 접미사)
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
#: 규약 DataFrame 의 timestamp dtype (candles_to_df 와 동일)
TIMESTAMP_DTYPE = "datetime64[us, UTC]"
_PRICE_COLUMNS = ("open", "high", "low", "close", "volume")
#: 파일명에 쓸 수 없는 문자 → '_'
_UNSAFE_CHARS = frozenset('/\\:*?"<>|\t\n\r')


# ---------------------------------------------------------------------------- 헬퍼
def symbol_safe(symbol: str) -> str:
    """심볼을 파일명에 안전한 형태로. ``'/'`` → ``'_'`` (``BTC/USDT`` → ``BTC_USDT``), 그 외 금지 문자도 ``'_'``."""
    s = str(symbol).strip()
    if not s:
        raise DataError("심볼이 비어 있습니다")
    out = "".join("_" if ch in _UNSAFE_CHARS or ch.isspace() else ch for ch in s)
    if out in (".", ".."):
        raise DataError(f"파일명으로 쓸 수 없는 심볼: {symbol!r}")
    return out


def empty_candles_df() -> pd.DataFrame:
    """행이 없는 규약 DataFrame (컬럼/dtype 은 정확히 갖춤)."""
    data: dict[str, pd.Series] = {"timestamp": pd.Series(dtype=TIMESTAMP_DTYPE)}
    for col in _PRICE_COLUMNS:
        data[col] = pd.Series(dtype="float64")
    return pd.DataFrame(data, columns=CANDLE_COLUMNS)


def to_utc_datetime(value: DateLike, *, name: str = "날짜") -> datetime | None:
    """start/end 류 인자를 UTC aware datetime 으로. None 은 그대로.

    - datetime: naive 는 UTC 로 간주, aware 는 UTC 로 변환
    - date: 그 날 00:00 UTC
    - str: 'YYYY-MM-DD' 또는 ISO 8601 (naive 면 UTC)
    - pd.Timestamp: 위와 동일
    """
    if value is None:
        return None
    if value is pd.NaT:  # NaT 는 datetime 의 인스턴스로 잡히므로 먼저 거른다
        raise DataError(f"{name} 가 NaT 입니다")
    if isinstance(value, pd.Timestamp):  # datetime 의 서브클래스이므로 먼저 검사
        ts = value.tz_localize("UTC") if value.tzinfo is None else value.tz_convert("UTC")
        return ts.to_pydatetime()
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            raise DataError(f"{name} 문자열이 비어 있습니다")
        try:
            return parse_date(s)
        except ValueError as e:
            raise DataError(f"{name} 형식 오류: {value!r} ('YYYY-MM-DD' 또는 ISO 8601 필요)") from e
    raise DataError(f"{name} 타입을 지원하지 않습니다: {type(value).__name__}")


def normalize_candles_df(df: pd.DataFrame) -> pd.DataFrame:
    """임의의 OHLCV DataFrame 을 규약 DataFrame 으로 정규화한다.

    - ``timestamp`` 컬럼이 없고 인덱스가 DatetimeIndex 면 인덱스를 timestamp 로 사용
    - timestamp → tz-aware UTC (naive 는 UTC 로 간주), 가격/거래량 → float64
    - timestamp 또는 OHLC 가 NaN 인 행 제거, volume NaN 은 0.0
    - 시간순 정렬, timestamp 중복은 마지막 행 유지, RangeIndex, 규약 컬럼만 남김
    """
    if not isinstance(df, pd.DataFrame):
        raise DataError(f"DataFrame 이 필요합니다: {type(df).__name__}")
    work = df
    if "timestamp" not in work.columns and isinstance(work.index, pd.DatetimeIndex):
        index_name = work.index.name or "index"
        work = work.reset_index().rename(columns={index_name: "timestamp"})
    missing = [c for c in CANDLE_COLUMNS if c not in work.columns]
    if missing:
        raise DataError(f"캔들 DataFrame 에 컬럼이 없습니다: {missing} (필요: {CANDLE_COLUMNS})")
    work = work.reset_index(drop=True)

    try:
        ts = pd.to_datetime(work["timestamp"], utc=True)
    except (ValueError, TypeError, OverflowError) as e:
        raise DataError(f"timestamp 파싱 실패: {e}") from e
    out = pd.DataFrame({"timestamp": ts.dt.as_unit("us")})
    for col in _PRICE_COLUMNS:
        try:
            out[col] = pd.to_numeric(work[col], errors="raise").astype("float64")
        except (ValueError, TypeError) as e:
            raise DataError(f"{col} 컬럼을 숫자로 변환할 수 없습니다: {e}") from e

    before = len(out)
    out = out.dropna(subset=["timestamp", "open", "high", "low", "close"])
    dropped = before - len(out)
    if dropped:
        logger.debug("NaN 이 포함된 캔들 %d행 제거", dropped)
    out["volume"] = out["volume"].fillna(0.0)
    out = (
        out.sort_values("timestamp", kind="mergesort")
        .drop_duplicates("timestamp", keep="last")
        .reset_index(drop=True)
    )
    return out[CANDLE_COLUMNS]


def filter_candles_df(df: pd.DataFrame, start: DateLike = None, end: DateLike = None) -> pd.DataFrame:
    """``start <= timestamp <= end`` (양끝 포함) 로 필터. None 이면 해당 경계 없음."""
    start_dt = to_utc_datetime(start, name="start")
    end_dt = to_utc_datetime(end, name="end")
    if start_dt is not None and end_dt is not None and start_dt > end_dt:
        raise DataError(f"start({start_dt.isoformat()}) 가 end({end_dt.isoformat()}) 보다 늦습니다")
    out = df
    if start_dt is not None:
        out = out[out["timestamp"] >= start_dt]
    if end_dt is not None:
        out = out[out["timestamp"] <= end_dt]
    return out.reset_index(drop=True)


def _check_path_component(value: str, name: str) -> str:
    s = str(value).strip()
    if not s or s in (".", "..") or any(ch in _UNSAFE_CHARS for ch in s):
        raise DataError(f"{name} 이름으로 쓸 수 없습니다: {value!r}")
    return s


def _atomic_write_csv(df: pd.DataFrame, path: Path) -> None:
    """같은 디렉터리의 임시 파일에 쓴 뒤 ``os.replace`` 로 교체 (쓰다 죽어도 기존 파일 보존)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            df.to_csv(
                fh, index=False, columns=CANDLE_COLUMNS, date_format=TIMESTAMP_FORMAT, lineterminator="\n"
            )
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------- 저장소
class CandleStore:
    """브로커별 캔들 CSV 캐시.

    사용::

        store = CandleStore("data/candles")
        df = store.download(upbit, "KRW-BTC", "1d", start="2024-01-01")   # 받아서 저장 + 반환
        df = store.load("upbit", "KRW-BTC", "1d", start="2024-06-01", end="2024-12-31")
    """

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)

    @classmethod
    def from_config(cls, config: AppConfig) -> CandleStore:
        """``config.backtest.data_dir`` 를 사용하는 저장소."""
        return cls(config.backtest.data_dir)

    def __repr__(self) -> str:
        return f"<CandleStore {self.data_dir}>"

    # ------------------------------------------------------------------ 경로
    def path(self, broker: str, symbol: str, interval: str) -> Path:
        """``{data_dir}/{broker}/{symbol_safe}_{interval}.csv``. interval 은 INTERVAL_SECONDS 에 있어야 한다."""
        broker_name = _check_path_component(broker, "브로커")
        try:
            interval_to_seconds(interval)
        except ValueError as e:
            raise DataError(str(e)) from e
        return self.data_dir / broker_name / f"{symbol_safe(symbol)}_{interval}.csv"

    def exists(self, broker: str, symbol: str, interval: str) -> bool:
        return self.path(broker, symbol, interval).is_file()

    def list_available(self) -> list[tuple[str, str, str]]:
        """저장된 (broker, symbol_safe, interval) 목록 (정렬). 레이아웃에 맞지 않는 파일은 무시.

        심볼은 파일명 형태(``symbol_safe``)로 돌려준다: ccxt 의 ``BTC/USDT`` 는 ``BTC_USDT`` 로 나온다.
        """
        if not self.data_dir.is_dir():
            return []
        out: list[tuple[str, str, str]] = []
        for broker_dir in sorted(p for p in self.data_dir.iterdir() if p.is_dir()):
            for f in sorted(broker_dir.glob("*.csv")):
                if not f.is_file() or "_" not in f.stem:
                    continue
                symbol, interval = f.stem.rsplit("_", 1)
                if not symbol or interval not in INTERVAL_SECONDS:
                    continue
                out.append((broker_dir.name, symbol, interval))
        return out

    # ------------------------------------------------------------------ 읽기/쓰기
    def load(
        self,
        broker: str,
        symbol: str,
        interval: str,
        start: DateLike = None,
        end: DateLike = None,
    ) -> pd.DataFrame:
        """CSV 를 읽어 규약 DataFrame 으로. ``start <= timestamp <= end`` 필터(양끝 포함).

        파일이 없으면 컬럼만 갖춘 빈 DataFrame. 손상된 파일은 DataError.
        """
        p = self.path(broker, symbol, interval)
        if not p.is_file():
            logger.debug("캔들 파일 없음: %s", p)
            return filter_candles_df(empty_candles_df(), start, end)
        return filter_candles_df(self._read_csv(p), start, end)

    def date_range(self, broker: str, symbol: str, interval: str) -> tuple[datetime, datetime] | None:
        """저장된 데이터의 (가장 오래된, 가장 최신) timestamp. 파일이 없거나 비어 있으면 None."""
        df = self.load(broker, symbol, interval)
        if df.empty:
            return None
        first = df["timestamp"].iloc[0].to_pydatetime()
        last = df["timestamp"].iloc[-1].to_pydatetime()
        return first, last

    def save(self, broker: str, symbol: str, interval: str, df: pd.DataFrame) -> Path:
        """기존 파일과 병합(새 데이터 우선), timestamp 중복 제거, 정렬 후 원자적으로 저장. 파일 경로 반환."""
        p = self.path(broker, symbol, interval)
        new = normalize_candles_df(df)
        if p.is_file():
            existing = self._read_csv(p)
            if new.empty:
                logger.debug("저장할 새 캔들이 없어 %s 를 그대로 둡니다", p)
                return p
            parts = [part for part in (existing, new) if not part.empty]
            merged = normalize_candles_df(pd.concat(parts, ignore_index=True))
            added = len(merged) - len(existing)
        else:
            merged = new
            added = len(merged)
        _atomic_write_csv(merged, p)
        logger.info("캔들 저장 %s: 총 %d행 (신규/갱신 %d행)", p, len(merged), added)
        return p

    def _read_csv(self, p: Path) -> pd.DataFrame:
        try:
            raw = pd.read_csv(p)
        except pd.errors.EmptyDataError:
            logger.warning("빈 캔들 파일: %s", p)
            return empty_candles_df()
        except (OSError, pd.errors.ParserError, ValueError) as e:
            raise DataError(f"캔들 파일을 읽을 수 없습니다 ({p}): {e}") from e
        try:
            return normalize_candles_df(raw)
        except DataError as e:
            raise DataError(f"캔들 파일 형식 오류 ({p}): {e}") from e

    # ------------------------------------------------------------------ 다운로드
    def download(
        self,
        broker: BaseBroker,
        symbol: str,
        interval: str,
        start: DateLike,
        end: DateLike = None,
        batch: int = 200,
        sleep: float = 0.1,
        progress: ProgressCallback | None = None,
    ) -> pd.DataFrame:
        """``broker.get_candles(end=cursor)`` 를 과거 방향으로 페이지네이션하여 ``start`` 까지 모은 뒤 저장.

        - ``end`` 가 None 이면 지금까지. 브로커의 ``end`` 는 미포함(exclusive)이므로 ``end`` 캔들 자체를
          포함하기 위해 첫 cursor 는 ``end + interval`` (단, 현재 시각을 넘지 않게) 로 잡는다.
        - 한 페이지가 비거나, 새 캔들이 없거나(진행 없음), 가장 오래된 캔들이 ``start`` 이하이면 멈춘다.
        - 페이지 사이에 ``sleep`` 초 쉬고, ``progress(받은 캔들 수, 가장 오래된 시각)`` 을 페이지마다 호출한다.
        - 받은 캔들 전부(범위 밖 여분 포함)를 캐시에 병합한 뒤, 캐시에서 ``[start, end]`` 구간을 읽어 돌려준다.
          아무것도 못 받으면 빈 DataFrame 을 돌려주고 파일은 건드리지 않는다.
        - 네트워크/거래소 오류는 브로커가 던지는 BrokerError 가 그대로 전파된다.
        """
        start_dt = to_utc_datetime(start, name="start")
        if start_dt is None:
            raise DataError("download 에는 start 가 필요합니다")
        now = utcnow()
        end_dt = to_utc_datetime(end, name="end") or now
        if start_dt > end_dt:
            raise DataError(f"start({start_dt.isoformat()}) 가 end({end_dt.isoformat()}) 보다 늦습니다")
        if batch < 1:
            raise DataError(f"batch 는 1 이상이어야 합니다: {batch}")
        if sleep < 0:
            raise DataError(f"sleep 은 0 이상이어야 합니다: {sleep}")
        broker_name = str(getattr(broker, "name", "") or "")
        target = self.path(broker_name, symbol, interval)  # 인자 검증 (interval/broker/symbol)
        step = timedelta(seconds=interval_to_seconds(interval))

        cursor = min(end_dt + step, now)
        collected: list[Candle] = []
        seen: set[datetime] = set()
        pages = 0
        logger.info(
            "캔들 다운로드 시작 %s %s %s: %s ~ %s (batch=%d)",
            broker_name,
            symbol,
            interval,
            start_dt.isoformat(),
            end_dt.isoformat(),
            batch,
        )
        while True:
            page = broker.get_candles(symbol, interval, limit=batch, end=cursor)
            pages += 1
            if not page:
                logger.debug("%s %s: 페이지 %d 가 비어 있어 종료", symbol, interval, pages)
                break
            fresh = [c for c in page if c.timestamp not in seen]
            if not fresh:
                logger.warning(
                    "%s %s: 페이지 %d 에 새 캔들이 없어 종료 (브로커 페이지네이션 진행 안 됨)",
                    symbol,
                    interval,
                    pages,
                )
                break
            seen.update(c.timestamp for c in fresh)
            collected.extend(fresh)
            oldest = min(c.timestamp for c in page)
            if progress is not None:
                progress(len(collected), oldest)
            logger.debug(
                "%s %s: 페이지 %d, 누적 %d개, 가장 오래된 %s", symbol, interval, pages, len(collected), oldest
            )
            if oldest <= start_dt:
                break
            if oldest >= cursor:
                logger.warning(
                    "%s %s: 커서가 과거로 이동하지 않아 종료 (oldest=%s, cursor=%s)",
                    symbol,
                    interval,
                    oldest,
                    cursor,
                )
                break
            cursor = oldest
            if sleep > 0:
                time.sleep(sleep)

        if not collected:
            logger.warning(
                "%s %s %s: 내려받은 캔들이 없습니다 (%s ~ %s)",
                broker_name,
                symbol,
                interval,
                start_dt,
                end_dt,
            )
            return empty_candles_df()

        self.save(broker_name, symbol, interval, candles_to_df(collected))
        result = self.load(broker_name, symbol, interval, start_dt, end_dt)
        logger.info(
            "캔들 다운로드 완료 %s %s %s: %d페이지, 받은 %d개, 구간 내 %d개 → %s",
            broker_name,
            symbol,
            interval,
            pages,
            len(collected),
            len(result),
            target,
        )
        return result
