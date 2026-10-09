"""시간/시장 운영시간 유틸."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from tradingbot.models import ensure_utc, interval_to_seconds

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")
UTC = timezone.utc


def now_utc() -> datetime:
    return datetime.now(UTC)


def floor_to_interval(dt: datetime, interval: str) -> datetime:
    """dt(UTC) 를 캔들 간격의 시작 시각으로 내림. 1w 는 월요일 00:00 UTC 기준."""
    dt = ensure_utc(dt)
    secs = interval_to_seconds(interval)
    if interval == "1w":
        monday = dt - timedelta(days=dt.weekday())
        return monday.replace(hour=0, minute=0, second=0, microsecond=0)
    epoch = int(dt.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % secs), tz=UTC)


def is_krx_open(dt: datetime | None = None) -> bool:
    """한국거래소 정규장 (평일 09:00~15:30 KST). 공휴일은 고려하지 않는다."""
    t = (dt or now_utc()).astimezone(KST)
    if t.weekday() >= 5:
        return False
    return time(9, 0) <= t.time() <= time(15, 30)


def is_nyse_open(dt: datetime | None = None) -> bool:
    """뉴욕증시 정규장 (평일 09:30~16:00 ET). 공휴일은 고려하지 않는다."""
    t = (dt or now_utc()).astimezone(NY)
    if t.weekday() >= 5:
        return False
    return time(9, 30) <= t.time() <= time(16, 0)


def parse_date(s: str, tz: timezone | ZoneInfo = UTC) -> datetime:
    """'YYYY-MM-DD' 또는 ISO 문자열을 aware datetime 으로."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(UTC)
