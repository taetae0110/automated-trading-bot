"""한국투자증권 KIS Developers (국내주식) 브로커 어댑터.

공식 샘플 저장소(koreainvestment/open-trading-api, examples_llm/*)와 KIS Developers 문서를 기준으로 구현했다.

인증
- ``POST /oauth2/tokenP`` {grant_type, appkey, appsecret} → access_token (유효기간 약 1일).
  토큰 발급은 1분당 1회로 제한되므로 JSON 파일(기본 ``~/.tradingbot/kis_token_<env>.json``, chmod 600)에 캐시해
  만료 전까지 재사용한다. 401/403 또는 ``msg_cd == EGW00123``(만료 토큰)이면 1회 재발급 후 재시도한다.
- 모든 REST API 는 Bearer 토큰이 필요하므로(시세 포함) 자격증명 없이 생성은 가능하지만 호출 시
  ``AuthenticationError`` 가 난다.
- hashkey 헤더: 공식 샘플(kis_auth._url_fetch)이 "현재는 hash key 필수 사항 아님, 생략가능" 이라 명시하고
  실제로 사용하지 않으므로 여기서도 생략한다.

시세
- 현재가: ``inquire-price`` (FHKST01010100) → ``output.stck_prpr``
- 일봉/주봉: ``inquire-daily-itemchartprice`` (FHKST03010100), 한 번에 최대 100행(최신→과거) 이므로
  종료일을 과거로 옮기며 페이지네이션. 수정주가(FID_ORG_ADJ_PRC=0).
- 분봉: 당일은 ``inquire-time-itemchartprice`` (FHKST03010200, 30행/호출), 과거 일자는
  ``inquire-time-dailychartprice`` (FHKST03010230, 120행/호출, 최대 1년 보관). 1분봉을 받아 요청 간격
  (3m/5m/10m/15m/30m/1h) 으로 직접 합성한다. 정규장(09:00~15:30 KST) 체결만 쓰고 15:30 종가(동시호가)
  체결은 마지막 정규장 봉에 합친다. 완성된 과거 일자 분봉은 메모리에 캐시한다.
- 타임스탬프: KST → UTC. 일봉/주봉의 시작 시각은 해당 (주의 월요일) 날짜 09:00 KST(=00:00 UTC) 로 두어
  ``floor_to_interval`` 과 정합되게 한다.

주문/계좌
- 현금주문 ``order-cash`` (실전 매수 TTTC0012U / 매도 TTTC0011U, 모의 VTTC0012U / VTTC0011U),
  시장가 ORD_DVSN=01 & ORD_UNPR=0, 지정가 ORD_DVSN=00 & KRX 호가단위 반올림, EXCG_ID_DVSN_CD=KRX.
- 취소 ``order-rvsecncl`` (TTTC0013U / VTTC0013U) — KRX_FWDG_ORD_ORGNO(주문 응답) + ORGN_ODNO 필요.
  잔량 전부 취소: QTY_ALL_ORD_YN=Y, ORD_QTY=0.
- 잔고 ``inquire-balance`` (TTTC8434R / VTTC8434R, ctx_area_fk100/nk100 + tr_cont 연속조회).
- 매수가능조회 ``inquire-psbl-order`` (TTTC8908R / VTTC8908R): 매수 주문 직전에 호출해 수량을
  ``nrcvb_buy_qty``(미수없는매수수량) 이하로 맞춘다. 시장가 주문은 ORD_UNPR 없이 나가므로 서버가 **상한가 × 수량**을
  주문가능금액에서 잡는다 (공식 order_cash 안내: "ORD_UNPR 이 없는 주문은 상한가로 주문금액을 선정"). 현재가로
  사이징한 수량을 그대로 보내면 예수금의 약 77% 를 넘는 순간 APBK0918(주문가능금액 초과) 로 거부된다.
- 주문 조회 ``inquire-daily-ccld`` (TTTC0081R / VTTC0081R, 최근 3개월).
- 휴장일 ``chk-holiday`` (CTCA0903R, 일 1회 권장 → 일자별 캐시).
- 호출 간격: 공식 샘플(kis_auth.py)과 같이 실전 0.05초, 모의 0.5초 (``request_interval`` 로 조정).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests

from tradingbot.brokers.base import BaseBroker
from tradingbot.exceptions import (
    AuthenticationError,
    BrokerError,
    ConfigError,
    InsufficientFunds,
    OrderError,
    RateLimitError,
)
from tradingbot.models import (
    AssetClass,
    Balance,
    Candle,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    ensure_utc,
    interval_to_seconds,
)
from tradingbot.utils.http import DEFAULT_TIMEOUT, RETRY_STATUS, HttpClient
from tradingbot.utils.timeutil import KST, UTC, floor_to_interval, is_krx_open, now_utc

if TYPE_CHECKING:  # pragma: no cover
    from tradingbot.config import AppConfig

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------------- 상수
REAL_BASE_URL = "https://openapi.koreainvestment.com:9443"
PAPER_BASE_URL = "https://openapivts.koreainvestment.com:29443"

PATH_TOKEN = "/oauth2/tokenP"
PATH_PRICE = "/uapi/domestic-stock/v1/quotations/inquire-price"
PATH_DAILY_CHART = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
PATH_TODAY_MINUTE_CHART = "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
PATH_PAST_MINUTE_CHART = "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice"
PATH_ORDER_CASH = "/uapi/domestic-stock/v1/trading/order-cash"
PATH_ORDER_RVSECNCL = "/uapi/domestic-stock/v1/trading/order-rvsecncl"
PATH_BALANCE = "/uapi/domestic-stock/v1/trading/inquire-balance"
PATH_PSBL_ORDER = "/uapi/domestic-stock/v1/trading/inquire-psbl-order"
PATH_DAILY_CCLD = "/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
PATH_HOLIDAY = "/uapi/domestic-stock/v1/quotations/chk-holiday"

TR_PRICE = "FHKST01010100"
TR_DAILY_CHART = "FHKST03010100"
TR_TODAY_MINUTE_CHART = "FHKST03010200"
TR_PAST_MINUTE_CHART = "FHKST03010230"
TR_HOLIDAY = "CTCA0903R"

# 실전/모의 분리 tr_id
_TR_IDS: dict[str, dict[str, str]] = {
    "real": {
        "buy": "TTTC0012U",
        "sell": "TTTC0011U",
        "cancel": "TTTC0013U",
        "balance": "TTTC8434R",
        "psbl_order": "TTTC8908R",
        "daily_ccld": "TTTC0081R",
    },
    "paper": {
        "buy": "VTTC0012U",
        "sell": "VTTC0011U",
        "cancel": "VTTC0013U",
        "balance": "VTTC8434R",
        "psbl_order": "VTTC8908R",
        "daily_ccld": "VTTC0081R",
    },
}

# 응답 msg_cd
TOKEN_ERROR_CODES = frozenset({"EGW00121", "EGW00123"})  # 유효하지 않은 / 기간이 만료된 token
RATE_LIMIT_CODES = frozenset({"EGW00201"})  # 초당 거래건수 초과

KRX_OPEN = dtime(9, 0)
KRX_CLOSE = dtime(15, 30)
KRX_SESSION_LAST_MINUTE = dtime(15, 29)

TOKEN_REFRESH_MARGIN = timedelta(minutes=5)
TOKEN_DEFAULT_LIFETIME = timedelta(hours=23)
DAILY_CLOSE_GRACE = timedelta(minutes=5)  # 종가 확정 여유
MINUTE_CLOSE_GRACE = timedelta(seconds=5)

DAILY_PAGE_ROWS = 100
TODAY_MINUTE_PAGE_ROWS = 30
PAST_MINUTE_PAGE_ROWS = 120
DAILY_MAX_PAGES = 60
MINUTE_MAX_PAGES_PER_DAY = 40
MINUTE_MAX_LOOKBACK_DAYS = 120
MINUTE_MAX_EMPTY_DAYS = 12  # 연휴 등 연속 무데이터 일수 한도
MINUTE_CACHE_DAYS = 40
BALANCE_MAX_PAGES = 20
CCLD_MAX_PAGES = 30
ORDER_LOOKUP_DAYS = 7
RATE_LIMIT_RETRIES = 3
# 연속 호출 최소 간격(초). 공식 샘플 kis_auth.py: 실전 0.05s, 모의 0.5s (모의 서버는 초당 한도가 낮다)
REAL_REQUEST_INTERVAL = 0.05
PAPER_REQUEST_INTERVAL = 0.5

# KRX 호가단위 (2023-01-25 통합, 코스피/코스닥 동일): (미만 가격, 호가단위)
KRX_TICK_TABLE: tuple[tuple[int, int], ...] = (
    (2_000, 1),
    (5_000, 5),
    (20_000, 10),
    (50_000, 50),
    (200_000, 100),
    (500_000, 500),
)
KRX_TICK_MAX = 1_000

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/114.0.0.0 Safari/537.36"
)

_SYMBOL_RE = re.compile(r"[0-9A-Z]{6,7}")
_ACCOUNT_RE = re.compile(r"(\d{8})-?(\d{2})")


# ----------------------------------------------------------------------------- 유틸
def krx_tick_size(price: float) -> int:
    """KRX 호가단위 (2023-01-25 통합 기준)."""
    for upper, tick in KRX_TICK_TABLE:
        if price < upper:
            return tick
    return KRX_TICK_MAX


def round_to_tick(price: float) -> float:
    """호가단위로 반올림 (0.5 는 올림)."""
    tick = krx_tick_size(price)
    return float(math.floor(price / tick + 0.5) * tick)


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _lower_keys(d: Mapping[str, Any]) -> dict[str, Any]:
    return {str(k).lower(): v for k, v in d.items()}


def _kst_datetime(date_str: str, time_str: str = "000000") -> datetime | None:
    """'YYYYMMDD' + 'HHMMSS' (KST) → aware UTC datetime. 형식이 틀리면 None."""
    ds = (date_str or "").strip()
    ts = (time_str or "").strip()
    if len(ds) != 8 or not ds.isdigit():
        return None
    if len(ts) == 4 and ts.isdigit():
        ts += "00"
    if len(ts) != 6 or not ts.isdigit():
        ts = "000000"
    try:
        local = datetime(
            int(ds[:4]), int(ds[4:6]), int(ds[6:8]), int(ts[:2]), int(ts[2:4]), int(ts[4:6]), tzinfo=KST
        )
    except ValueError:
        return None
    return local.astimezone(UTC)


def _kst_session_open(d: date) -> datetime:
    return datetime.combine(d, KRX_OPEN, tzinfo=KST).astimezone(UTC)


def _kst_session_close(d: date) -> datetime:
    return datetime.combine(d, KRX_CLOSE, tzinfo=KST).astimezone(UTC)


def _hhmmss_minus_one_minute(hhmmss: str) -> str:
    t = datetime(2000, 1, 1, int(hhmmss[:2]), int(hhmmss[2:4]), int(hhmmss[4:6])) - timedelta(minutes=1)
    return t.strftime("%H%M%S")


def _norm_odno(s: Any) -> str:
    return str(s or "").strip().lstrip("0") or "0"


@dataclass(frozen=True)
class _MinuteRow:
    ts: datetime  # 1분봉 시작 시각 (UTC)
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class _KISResponse:
    body: dict[str, Any]
    tr_cont: str

    @property
    def has_next(self) -> bool:
        return self.tr_cont in ("F", "M")


@dataclass
class _Bucket:
    start: datetime
    close_time: datetime  # 이 봉이 "완성" 되는 시각 (UTC)
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class KISBuyingPower:
    """매수가능조회(``inquire-psbl-order``, TTTC8908R / VTTC8908R) 결과. 금액은 KRW, 수량은 주.

    미수를 쓰지 않으므로 ``amount``/``quantity``(미수없는매수금액/수량) 가 기준값이다. 시장가(ORD_DVSN=01) 조회의
    ``calc_price`` 는 상한가다 — 시장가 주문은 ORD_UNPR 없이 나가 서버가 상한가 × 수량을 주문가능금액에서 잡는다.
    """

    symbol: str
    cash: float  # ord_psbl_cash 주문가능현금
    amount: float  # nrcvb_buy_amt 미수없는매수금액
    quantity: int  # nrcvb_buy_qty 미수없는매수수량
    max_amount: float  # max_buy_amt 최대매수금액 (미수 포함)
    max_quantity: int  # max_buy_qty 최대매수수량 (미수 포함)
    calc_price: float | None  # psbl_qty_calc_unpr 가능수량계산단가
    raw: dict[str, Any]


# ----------------------------------------------------------------------------- 브로커
class KISBroker(BaseBroker):
    """한국투자증권 KIS Developers 국내주식 어댑터. 심볼은 6자리 종목코드 (예: 005930)."""

    name = "kis"
    asset_class = AssetClass.STOCK
    supported_intervals = ("1m", "3m", "5m", "10m", "15m", "30m", "1h", "1d", "1w")

    def __init__(
        self,
        app_key: str | None = None,
        app_secret: str | None = None,
        account_no: str | None = None,
        *,
        sandbox: bool = True,
        token_path: str | os.PathLike[str] | None = None,
        client: HttpClient | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        request_interval: float | None = None,
        holiday_check: bool = True,
        user_agent: str | None = None,
    ) -> None:
        self.sandbox = bool(sandbox)
        self._env = "paper" if self.sandbox else "real"
        self._base_url = PAPER_BASE_URL if self.sandbox else REAL_BASE_URL
        self._app_key = (app_key or "").strip() or None
        self._app_secret = (app_secret or "").strip() or None
        self._cano: str | None = None
        self._acnt_prdt_cd: str | None = None
        if account_no is not None and str(account_no).strip():
            self._cano, self._acnt_prdt_cd = self.parse_account_no(str(account_no))
        self._client = client or HttpClient(self._base_url, timeout=timeout)
        self._timeout = float(client.timeout) if client is not None else float(timeout)
        self._user_agent = user_agent or DEFAULT_USER_AGENT
        # 모의투자 서버는 초당 호출 한도가 낮다. 공식 샘플(kis_auth.py)과 동일하게 실전 0.05s, 모의 0.5s.
        self._request_interval = (
            (PAPER_REQUEST_INTERVAL if self.sandbox else REAL_REQUEST_INTERVAL)
            if request_interval is None
            else float(request_interval)
        )
        self._holiday_check = bool(holiday_check)
        self._last_request_at = 0.0

        default_dir = Path.home() / ".tradingbot"
        self._token_path = (
            Path(token_path).expanduser()
            if token_path is not None
            else default_dir / f"kis_token_{self._env}.json"
        )
        self._token: str | None = None
        self._token_expires_at: datetime | None = None

        # 세션 중 접수한 주문 (ODNO → 취소에 필요한 정보)
        self._order_cache: dict[str, dict[str, str]] = {}
        # 분봉 캐시: (symbol, KST date) → {HHMMSS: _MinuteRow}
        self._minute_cache: dict[tuple[str, date], dict[str, _MinuteRow]] = {}
        self._past_minutes_unavailable = False
        # 휴장일 캐시: KST date → 개장일 여부
        self._holiday_cache: dict[date, bool] = {}
        self._holiday_warned_on: date | None = None

    # ------------------------------------------------------------------ 생성/설정
    @classmethod
    def from_config(cls, config: AppConfig) -> KISBroker:
        """``config.broker.sandbox`` 와 환경변수(KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT_NO)로 생성."""
        from tradingbot.config import Credentials

        creds = Credentials.from_env()
        extra = config.broker.extra or {}
        timeout = extra.get("timeout", DEFAULT_TIMEOUT)
        try:
            timeout = float(timeout)
        except (TypeError, ValueError) as e:
            raise ConfigError(f"broker.extra.timeout 값이 올바르지 않습니다: {timeout!r}") from e
        interval = extra.get("request_interval")
        return cls(
            app_key=creds.kis_app_key,
            app_secret=creds.kis_app_secret,
            account_no=creds.kis_account_no,
            sandbox=config.broker.sandbox,
            token_path=extra.get("token_path"),
            timeout=timeout,
            request_interval=float(interval) if interval is not None else None,
            holiday_check=bool(extra.get("holiday_check", True)),
            user_agent=extra.get("user_agent"),
        )

    @staticmethod
    def parse_account_no(account_no: str) -> tuple[str, str]:
        """'12345678-01' 또는 '1234567801' → (CANO 8자리, ACNT_PRDT_CD 2자리)."""
        s = re.sub(r"\s+", "", str(account_no))
        m = _ACCOUNT_RE.fullmatch(s)
        if not m:
            raise ConfigError(
                "KIS_ACCOUNT_NO 형식 오류: '12345678-01' 또는 '1234567801' (종합계좌 8자리 + 상품코드 2자리)"
            )
        return m.group(1), m.group(2)

    @property
    def account_no(self) -> str | None:
        if self._cano is None or self._acnt_prdt_cd is None:
            return None
        return f"{self._cano}-{self._acnt_prdt_cd}"

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def token_path(self) -> Path:
        return self._token_path

    @property
    def request_interval(self) -> float:
        """연속 호출 사이 최소 간격(초). 기본 실전 0.05s, 모의 0.5s (공식 샘플 kis_auth.py 와 동일)."""
        return self._request_interval

    def _tr(self, key: str) -> str:
        return _TR_IDS[self._env][key]

    def _has_credentials(self) -> bool:
        return bool(self._app_key and self._app_secret)

    def _require_credentials(self) -> None:
        if not self._has_credentials():
            raise AuthenticationError("KIS API 호출에는 KIS_APP_KEY / KIS_APP_SECRET 환경변수 필요")

    def _require_account(self) -> None:
        self._require_credentials()
        if self._cano is None or self._acnt_prdt_cd is None:
            raise AuthenticationError("주문/잔고 조회에는 KIS_ACCOUNT_NO 환경변수 필요 (예: 12345678-01)")

    @staticmethod
    def _check_symbol(symbol: str, exc: type[BrokerError] = BrokerError) -> str:
        s = str(symbol or "").strip().upper()
        if not _SYMBOL_RE.fullmatch(s):
            raise exc(f"KIS 종목코드 형식 오류: {symbol!r} (예: 005930)")
        return s

    def close(self) -> None:
        try:
            self._client.session.close()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ 토큰
    def _app_key_fingerprint(self) -> str:
        return hashlib.sha256((self._app_key or "").encode("utf-8")).hexdigest()[:16]

    def _token_is_fresh(self, expires_at: datetime | None, now: datetime) -> bool:
        return expires_at is not None and expires_at - now > TOKEN_REFRESH_MARGIN

    def _load_token_file(self) -> tuple[str, datetime] | None:
        p = self._token_path
        try:
            if not p.is_file():
                return None
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("KIS 토큰 캐시 파일을 읽을 수 없어 무시합니다 (%s): %s", p, e)
            return None
        if not isinstance(data, dict):
            return None
        token = data.get("access_token")
        expires = data.get("expires_at")
        if not isinstance(token, str) or not token or not isinstance(expires, str):
            return None
        if data.get("app_key_fingerprint") not in (None, self._app_key_fingerprint()):
            logger.info("KIS 토큰 캐시가 다른 앱키로 발급된 것이라 무시합니다")
            return None
        if data.get("env") not in (None, self._env):
            return None
        try:
            expires_at = ensure_utc(datetime.fromisoformat(expires))
        except ValueError:
            return None
        return token, expires_at

    def _save_token_file(self, token: str, expires_at: datetime) -> None:
        p = self._token_path
        payload = {
            "access_token": token,
            "token_type": "Bearer",
            "expires_at": expires_at.astimezone(UTC).isoformat(),
            "issued_at": now_utc().isoformat(),
            "env": self._env,
            "app_key_fingerprint": self._app_key_fingerprint(),
        }
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(p.parent, 0o700)
            except OSError:
                pass
            tmp = p.with_name(p.name + ".tmp")
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, p)
        except OSError as e:
            logger.warning("KIS 토큰 캐시 저장 실패 (%s): %s", p, e)

    def _invalidate_token(self) -> None:
        self._token = None
        self._token_expires_at = None
        try:
            if self._token_path.is_file():
                self._token_path.unlink()
        except OSError as e:
            logger.warning("KIS 토큰 캐시 삭제 실패 (%s): %s", self._token_path, e)

    def _get_token(self) -> str:
        """메모리 → 파일 → 신규 발급 순으로 유효한 토큰을 얻는다."""
        self._require_credentials()
        now = now_utc()
        if self._token and self._token_is_fresh(self._token_expires_at, now):
            return self._token
        loaded = self._load_token_file()
        if loaded is not None and self._token_is_fresh(loaded[1], now):
            self._token, self._token_expires_at = loaded
            logger.debug("KIS 토큰 캐시 재사용 (만료 %s)", loaded[1].isoformat())
            return self._token
        return self._issue_token()

    def _issue_token(self) -> str:
        self._require_credentials()
        url = self._base_url + PATH_TOKEN
        headers = {
            "content-type": "application/json",
            "accept": "text/plain",
            "charset": "UTF-8",
            "user-agent": self._user_agent,
        }
        body = {"grant_type": "client_credentials", "appkey": self._app_key, "appsecret": self._app_secret}
        self._throttle()
        try:
            resp = self._client.session.request(
                "POST", url, json=body, headers=headers, timeout=self._timeout
            )
        except requests.RequestException as e:
            raise BrokerError(f"KIS 토큰 발급 네트워크 오류: {e}") from e
        payload = self._parse_json(resp)
        if not isinstance(payload, dict) or resp.status_code != 200 or not payload.get("access_token"):
            code = ""
            desc = ""
            if isinstance(payload, dict):
                code = str(payload.get("error_code") or payload.get("msg_cd") or "")
                desc = str(payload.get("error_description") or payload.get("msg1") or "").strip()
            raise AuthenticationError(
                f"KIS 토큰 발급 실패 (HTTP {resp.status_code}) [{code}] {desc}".rstrip(),
                status_code=resp.status_code,
                payload=payload,
            )
        token = str(payload["access_token"])
        now = now_utc()
        expires_at: datetime | None = None
        expired_str = payload.get("access_token_token_expired")
        if isinstance(expired_str, str) and expired_str.strip():
            try:
                expires_at = datetime.strptime(expired_str.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
                expires_at = expires_at.astimezone(UTC)
            except ValueError:
                expires_at = None
        if expires_at is None:
            secs = _to_float(payload.get("expires_in"))
            expires_at = now + (timedelta(seconds=secs) if secs and secs > 0 else TOKEN_DEFAULT_LIFETIME)
        self._token = token
        self._token_expires_at = expires_at
        self._save_token_file(token, expires_at)
        logger.info("KIS 접근토큰 발급 완료 (%s, 만료 %s)", self._env, expires_at.astimezone(KST).isoformat())
        return token

    # ------------------------------------------------------------------ HTTP 공통
    def _headers(self, token: str, tr_id: str, tr_cont: str = "") -> dict[str, str]:
        assert self._app_key and self._app_secret
        return {
            "content-type": "application/json; charset=utf-8",
            "accept": "text/plain",
            "charset": "UTF-8",
            "user-agent": self._user_agent,
            "authorization": f"Bearer {token}",
            "appkey": self._app_key,
            "appsecret": self._app_secret,
            "tr_id": tr_id,
            "custtype": "P",
            "tr_cont": tr_cont,
        }

    def _throttle(self) -> None:
        if self._request_interval <= 0:
            return
        wait = self._request_interval - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    @staticmethod
    def _parse_json(resp: requests.Response) -> Any:
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    @staticmethod
    def _backoff(attempt: int) -> None:
        time.sleep(min(0.5 * (2 ** (attempt - 1)), 5.0))

    def _error_for(self, payload: Any, status: int, tr_id: str, path: str) -> BrokerError:
        msg_cd = ""
        msg1 = ""
        if isinstance(payload, dict):
            msg_cd = str(payload.get("msg_cd") or payload.get("error_code") or "").strip()
            msg1 = str(payload.get("msg1") or payload.get("error_description") or "").strip()
        detail = f"[{msg_cd}] {msg1}".strip() if (msg_cd or msg1) else str(payload)[:200]
        message = f"KIS {tr_id} 실패 (HTTP {status}) {detail}"
        if msg_cd in TOKEN_ERROR_CODES or status in (401, 403):
            return AuthenticationError(message, status_code=status, payload=payload)
        if msg_cd in RATE_LIMIT_CODES or status == 429:
            return RateLimitError(message, status_code=status, payload=payload)
        if path == PATH_ORDER_CASH and any(k in msg1 for k in ("부족", "가능금액", "가능수량", "초과")):
            return InsufficientFunds(message, status_code=status, payload=payload)
        if path in (PATH_ORDER_CASH, PATH_ORDER_RVSECNCL):
            return OrderError(message, status_code=status, payload=payload)
        return BrokerError(message, status_code=status, payload=payload)

    def _call(
        self,
        method: str,
        path: str,
        tr_id: str,
        *,
        params: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
        tr_cont: str = "",
        _auth_retry: bool = True,
    ) -> _KISResponse:
        """인증 헤더를 붙여 호출하고 ``rt_cd == '0'`` 인 본문과 응답 tr_cont 헤더를 돌려준다.

        - GET 은 네트워크 오류/5xx 를 지수 백오프로 재시도, POST(주문)는 중복 접수 위험 때문에 재시도하지 않는다.
        - 초당 호출 한도(EGW00201/429)는 GET/POST 모두 짧게 대기 후 재시도 (거부된 주문은 접수되지 않았으므로 안전).
        - 토큰 만료(401/403/EGW00123)는 1회 재발급 후 재시도.
        """
        method = method.upper()
        token = self._get_token()
        headers = self._headers(token, tr_id, tr_cont)
        url = self._base_url + path
        is_get = method == "GET"
        net_attempts = self._client.max_retries + 1 if is_get else 1
        net_attempt = 0
        rate_attempt = 0
        while True:
            self._throttle()
            try:
                resp = self._client.session.request(
                    method,
                    url,
                    params=dict(params) if params else None,
                    json=dict(body) if body is not None else None,
                    headers=headers,
                    timeout=self._timeout,
                )
            except requests.RequestException as e:
                net_attempt += 1
                if is_get and net_attempt < net_attempts:
                    logger.warning(
                        "KIS %s %s 네트워크 오류, 재시도 %d/%d: %s",
                        method,
                        path,
                        net_attempt,
                        net_attempts - 1,
                        e,
                    )
                    self._backoff(net_attempt)
                    continue
                raise BrokerError(f"KIS {method} {path} 네트워크 오류: {e}") from e

            payload = self._parse_json(resp)
            status = resp.status_code
            is_kis_body = isinstance(payload, dict) and ("rt_cd" in payload or "msg_cd" in payload)
            msg_cd = str(payload.get("msg_cd") or "").strip() if isinstance(payload, dict) else ""

            # 토큰 만료/무효 → 1회 재발급
            if status in (401, 403) or msg_cd in TOKEN_ERROR_CODES:
                if _auth_retry:
                    logger.info("KIS 토큰 무효/만료 (HTTP %s %s) → 재발급 후 재시도", status, msg_cd or "-")
                    self._invalidate_token()
                    return self._call(
                        method, path, tr_id, params=params, body=body, tr_cont=tr_cont, _auth_retry=False
                    )
                raise self._error_for(payload, status, tr_id, path)

            # 초당 호출 한도
            if status == 429 or msg_cd in RATE_LIMIT_CODES:
                rate_attempt += 1
                if rate_attempt <= RATE_LIMIT_RETRIES:
                    logger.warning(
                        "KIS 호출 한도 초과 (%s), %d/%d 재시도",
                        msg_cd or status,
                        rate_attempt,
                        RATE_LIMIT_RETRIES,
                    )
                    time.sleep(min(0.5 * rate_attempt, 2.0))
                    continue
                raise self._error_for(payload, status, tr_id, path)

            # 서버 장애 (KIS 업무 오류 본문이 아닌 5xx) → GET 만 재시도
            if status in RETRY_STATUS and not is_kis_body:
                net_attempt += 1
                if is_get and net_attempt < net_attempts:
                    logger.warning(
                        "KIS %s %s -> HTTP %s, 재시도 %d/%d",
                        method,
                        path,
                        status,
                        net_attempt,
                        net_attempts - 1,
                    )
                    self._backoff(net_attempt)
                    continue
                raise self._error_for(payload, status, tr_id, path)

            if not 200 <= status < 300:
                raise self._error_for(payload, status, tr_id, path)
            if not isinstance(payload, dict):
                raise BrokerError(
                    f"KIS {tr_id} 응답 형식 오류: {str(payload)[:200]}", status_code=status, payload=payload
                )
            if str(payload.get("rt_cd", "")).strip() != "0":
                raise self._error_for(payload, status, tr_id, path)
            return _KISResponse(body=payload, tr_cont=str(resp.headers.get("tr_cont", "") or "").strip())

    # ------------------------------------------------------------------ 메타
    def quote_currency(self, symbol: str) -> str:
        return "KRW"

    def base_currency(self, symbol: str) -> str:
        return symbol

    def min_order_value(self, symbol: str) -> float:
        return 0.0

    def round_quantity(self, symbol: str, quantity: float) -> float:
        """주식은 정수 주 단위: 내림. 0 이하/비정상 값은 0."""
        try:
            q = float(quantity)
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(q) or q <= 0:
            return 0.0
        return float(math.floor(q + 1e-9))

    def round_price(self, symbol: str, price: float) -> float:
        """KRX 호가단위로 반올림."""
        try:
            p = float(price)
        except (TypeError, ValueError) as e:
            raise OrderError(f"가격이 올바르지 않습니다: {price!r}") from e
        if not math.isfinite(p) or p <= 0:
            raise OrderError(f"가격이 올바르지 않습니다: {price!r}")
        return round_to_tick(p)

    # ------------------------------------------------------------------ 휴장일 / 장 운영
    def is_trading_day(self, day: date | None = None) -> bool:
        """KIS 휴장일조회(opnd_yn) 기준 개장일 여부. 일자별로 캐시한다 (API 권장: 1일 1회)."""
        d = day or now_utc().astimezone(KST).date()
        cached = self._holiday_cache.get(d)
        if cached is not None:
            return cached
        res = self._call(
            "GET",
            PATH_HOLIDAY,
            TR_HOLIDAY,
            params={"BASS_DT": d.strftime("%Y%m%d"), "CTX_AREA_NK": "", "CTX_AREA_FK": ""},
        )
        rows = res.body.get("output") or []
        if isinstance(rows, dict):
            rows = [rows]
        for row in rows:
            if not isinstance(row, dict):
                continue
            dt = _kst_datetime(str(row.get("bass_dt", "")))
            if dt is None:
                continue
            self._holiday_cache[dt.astimezone(KST).date()] = (
                str(row.get("opnd_yn", "")).strip().upper() == "Y"
            )
        if d not in self._holiday_cache:
            raise BrokerError(f"휴장일 응답에 기준일 {d.isoformat()} 이 없습니다", payload=res.body)
        return self._holiday_cache[d]

    def is_market_open(self) -> bool:
        """정규장(평일 09:00~15:30 KST) 여부. 자격증명이 있으면 휴장일조회도 반영 (실패 시 시계 기준)."""
        now = now_utc()
        if not is_krx_open(now):
            return False
        if not self._holiday_check or not self._has_credentials():
            return True
        today = now.astimezone(KST).date()
        try:
            return self.is_trading_day(today)
        except BrokerError as e:
            if self._holiday_warned_on != today:
                self._holiday_warned_on = today
                logger.warning("KIS 휴장일조회 실패, 시계 기준으로 개장 판단: %s", e)
            return True

    # ------------------------------------------------------------------ 시세
    def get_ticker(self, symbol: str) -> float:
        sym = self._check_symbol(symbol)
        res = self._call(
            "GET", PATH_PRICE, TR_PRICE, params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": sym}
        )
        out = res.body.get("output") or {}
        price = _to_float(out.get("stck_prpr")) if isinstance(out, dict) else None
        if price is None or price <= 0:
            raise BrokerError(f"{sym} 현재가 응답 오류 (stck_prpr 없음)", payload=res.body)
        return price

    def get_candles(
        self,
        symbol: str,
        interval: str,
        limit: int = 200,
        end: datetime | None = None,
        include_partial: bool = False,
    ) -> list[Candle]:
        sym = self._check_symbol(symbol)
        interval_to_seconds(interval)  # 알 수 없는 간격이면 ValueError
        if interval not in self.supported_intervals:
            raise ValueError(
                f"KIS 는 {interval!r} 캔들을 지원하지 않습니다 (가능: {', '.join(self.supported_intervals)})"
            )
        if limit <= 0:
            return []
        now = now_utc()
        end_utc = ensure_utc(end) if end is not None else None
        if interval in ("1d", "1w"):
            ref = now - DAILY_CLOSE_GRACE
            if end_utc is not None:
                ref = min(ref, end_utc)
            buckets = self._daily_buckets(sym, interval, limit, ref, end_utc or now, include_partial)
        else:
            ref = now - MINUTE_CLOSE_GRACE
            if end_utc is not None:
                ref = min(ref, end_utc)
            buckets = self._minute_buckets(sym, interval, limit, ref, end_utc or now, include_partial)
        if not include_partial:
            buckets = [b for b in buckets if b.close_time <= ref]
        else:
            buckets = [b for b in buckets if b.start <= (end_utc or now)]
        buckets.sort(key=lambda b: b.start)
        buckets = buckets[-limit:]
        return [Candle(b.start, b.open, b.high, b.low, b.close, b.volume) for b in buckets]

    # ---------------------------------------------------------- 일봉/주봉
    def _daily_buckets(
        self, symbol: str, interval: str, limit: int, ref: datetime, upper: datetime, include_partial: bool
    ) -> list[_Bucket]:
        period = "D" if interval == "1d" else "W"
        span_days = 200 if period == "D" else 800
        date2 = upper.astimezone(KST).date()
        by_date: dict[date, _Bucket] = {}
        for _page in range(DAILY_MAX_PAGES):
            date1 = date2 - timedelta(days=span_days)
            rows = self._fetch_daily_rows(symbol, period, date1, date2)
            if not rows:
                break
            oldest: date | None = None
            for row in rows:
                b, d = self._bucket_from_daily_row(row, period)
                if b is None or d is None:
                    continue
                if b.start > upper:
                    continue
                by_date.setdefault(d, b)
                if oldest is None or d < oldest:
                    oldest = d
            if oldest is None:
                break
            complete = sum(1 for b in by_date.values() if include_partial or b.close_time <= ref)
            if complete >= limit:
                break
            next_date2 = oldest - timedelta(days=1)
            if next_date2 >= date2:
                break
            date2 = next_date2
        return list(by_date.values())

    def _fetch_daily_rows(self, symbol: str, period: str, date1: date, date2: date) -> list[dict[str, Any]]:
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
            "FID_INPUT_DATE_1": date1.strftime("%Y%m%d"),
            "FID_INPUT_DATE_2": date2.strftime("%Y%m%d"),
            "FID_PERIOD_DIV_CODE": period,
            "FID_ORG_ADJ_PRC": "0",  # 수정주가
        }
        res = self._call("GET", PATH_DAILY_CHART, TR_DAILY_CHART, params=params)
        rows = res.body.get("output2") or []
        if not isinstance(rows, list):
            raise BrokerError(f"일봉 응답 형식 오류 ({symbol})", payload=res.body)
        return [r for r in rows if isinstance(r, dict) and str(r.get("stck_bsop_date", "")).strip()]

    @staticmethod
    def _bucket_from_daily_row(row: Mapping[str, Any], period: str) -> tuple[_Bucket | None, date | None]:
        dt = _kst_datetime(str(row.get("stck_bsop_date", "")))
        o = _to_float(row.get("stck_oprc"))
        h = _to_float(row.get("stck_hgpr"))
        lo = _to_float(row.get("stck_lwpr"))
        c = _to_float(row.get("stck_clpr"))
        v = _to_float(row.get("acml_vol"))
        if dt is None or None in (o, h, lo, c):
            return None, None
        assert o is not None and h is not None and lo is not None and c is not None
        d = dt.astimezone(KST).date()
        if period == "W":
            monday = d - timedelta(days=d.weekday())
            start = _kst_session_open(monday)
            close_time = _kst_session_close(monday + timedelta(days=4))
            key = monday
        else:
            start = _kst_session_open(d)
            close_time = _kst_session_close(d)
            key = d
        return _Bucket(start, close_time, o, h, lo, c, v or 0.0), key

    # ---------------------------------------------------------- 분봉
    def _minute_buckets(
        self, symbol: str, interval: str, limit: int, ref: datetime, upper: datetime, include_partial: bool
    ) -> list[_Bucket]:
        upper_kst = upper.astimezone(KST)
        today_kst = now_utc().astimezone(KST).date()
        rows: dict[datetime, _MinuteRow] = {}
        day = upper_kst.date()
        empty_streak = 0
        for _ in range(MINUTE_MAX_LOOKBACK_DAYS):
            if day.weekday() < 5 and not (day < today_kst and self._past_minutes_unavailable):
                if day == upper_kst.date():
                    until = min(upper_kst.time(), KRX_CLOSE).strftime("%H%M%S")
                else:
                    until = KRX_CLOSE.strftime("%H%M%S")
                day_rows = self._day_minute_rows(symbol, day, until, today_kst)
                if day_rows:
                    empty_streak = 0
                    for r in day_rows:
                        if r.ts <= upper:
                            rows[r.ts] = r
                else:
                    empty_streak += 1
                buckets = self._aggregate_minutes(rows.values(), interval)
                complete = sum(1 for b in buckets if include_partial or b.close_time <= ref)
                if complete >= limit:
                    return buckets
                if empty_streak >= MINUTE_MAX_EMPTY_DAYS:
                    break
            day -= timedelta(days=1)
        return self._aggregate_minutes(rows.values(), interval)

    def _day_minute_rows(self, symbol: str, day: date, until: str, today_kst: date) -> list[_MinuteRow]:
        """해당 KST 일자의 1분봉(정규장, until 'HHMMSS' 이하). 과거 일자는 전체를 캐시한다."""
        key = (symbol, day)
        cached = self._minute_cache.get(key)
        if day < today_kst:
            if cached is None:
                try:
                    cached = self._fetch_minute_rows(symbol, day, KRX_CLOSE.strftime("%H%M%S"), past=True)
                except BrokerError as e:
                    # 모의투자 서버 등에서 일별분봉조회가 안 되면 당일 분봉만 사용한다.
                    self._past_minutes_unavailable = True
                    logger.warning(
                        "KIS 과거 일자 분봉 조회 불가 (%s) — 당일 분봉만 사용합니다: %s",
                        TR_PAST_MINUTE_CHART,
                        e,
                    )
                    return []
                self._store_minute_cache(key, cached)
            return [r for r in cached.values() if self._hhmmss_kst(r.ts) <= until]
        # 당일: 최신 페이지부터 받되 캐시된 구간에 닿으면 중단
        known_max = max(cached) if cached else None
        fresh = self._fetch_minute_rows(symbol, day, until, past=False, stop_at=known_max)
        merged = dict(cached) if cached else {}
        merged.update(fresh)
        self._store_minute_cache(key, merged)
        return [r for r in merged.values() if self._hhmmss_kst(r.ts) <= until]

    def _store_minute_cache(self, key: tuple[str, date], rows: dict[str, _MinuteRow]) -> None:
        self._minute_cache[key] = rows
        if len(self._minute_cache) > MINUTE_CACHE_DAYS:
            oldest = min(self._minute_cache, key=lambda k: k[1])
            self._minute_cache.pop(oldest, None)

    @staticmethod
    def _hhmmss_kst(ts: datetime) -> str:
        return ts.astimezone(KST).strftime("%H%M%S")

    def _fetch_minute_rows(
        self, symbol: str, day: date, until: str, *, past: bool, stop_at: str | None = None
    ) -> dict[str, _MinuteRow]:
        """시간을 거슬러 올라가며 페이지네이션. 반환: {HHMMSS: row} (정규장 09:00~15:30 만)."""
        out: dict[str, _MinuteRow] = {}
        hour = until
        page_rows = PAST_MINUTE_PAGE_ROWS if past else TODAY_MINUTE_PAGE_ROWS
        for _ in range(MINUTE_MAX_PAGES_PER_DAY):
            raw = self._fetch_minute_page(symbol, day, hour, past)
            parsed: list[tuple[str, _MinuteRow]] = []
            for row in raw:
                item = self._minute_row(row, day)
                if item is not None:
                    parsed.append(item)
            for hhmmss, r in parsed:
                out.setdefault(hhmmss, r)
            if not parsed:
                break
            min_time = min(h for h, _ in parsed)
            if min_time <= "090000" or len(raw) < page_rows:
                break
            if stop_at is not None and min_time <= stop_at:
                break
            next_hour = _hhmmss_minus_one_minute(min_time)
            if next_hour >= hour:
                break
            hour = next_hour
        return out

    def _fetch_minute_page(self, symbol: str, day: date, hour: str, past: bool) -> list[dict[str, Any]]:
        if past:
            params = {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_HOUR_1": hour,
                "FID_INPUT_DATE_1": day.strftime("%Y%m%d"),
                "FID_PW_DATA_INCU_YN": "Y",
                "FID_FAKE_TICK_INCU_YN": "",
            }
            res = self._call("GET", PATH_PAST_MINUTE_CHART, TR_PAST_MINUTE_CHART, params=params)
        else:
            params = {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_HOUR_1": hour,
                "FID_PW_DATA_INCU_YN": "Y",
                "FID_ETC_CLS_CODE": "",
            }
            res = self._call("GET", PATH_TODAY_MINUTE_CHART, TR_TODAY_MINUTE_CHART, params=params)
        rows = res.body.get("output2") or []
        if not isinstance(rows, list):
            raise BrokerError(f"분봉 응답 형식 오류 ({symbol})", payload=res.body)
        return [r for r in rows if isinstance(r, dict) and str(r.get("stck_cntg_hour", "")).strip()]

    @staticmethod
    def _minute_row(row: Mapping[str, Any], day: date) -> tuple[str, _MinuteRow] | None:
        date_str = str(row.get("stck_bsop_date", "")).strip() or day.strftime("%Y%m%d")
        hhmmss = str(row.get("stck_cntg_hour", "")).strip()
        if len(hhmmss) == 4:
            hhmmss += "00"
        ts = _kst_datetime(date_str, hhmmss)
        if ts is None or ts.astimezone(KST).date() != day:
            return None
        # 정규장 체결(09:00~15:30 동시호가 종가)만 사용. 시간외 단일가/장전 체결은 제외.
        if not ("090000" <= hhmmss <= "153059"):
            return None
        o = _to_float(row.get("stck_oprc"))
        h = _to_float(row.get("stck_hgpr"))
        lo = _to_float(row.get("stck_lwpr"))
        c = _to_float(row.get("stck_prpr"))
        v = _to_float(row.get("cntg_vol"))
        if None in (o, h, lo, c):
            return None
        assert o is not None and h is not None and lo is not None and c is not None
        return hhmmss, _MinuteRow(ts, o, h, lo, c, v or 0.0)

    @staticmethod
    def _aggregate_minutes(rows: Iterable[_MinuteRow], interval: str) -> list[_Bucket]:
        """1분봉 → interval 봉. 경계는 KST 시계 기준(=UTC epoch 내림, KST 는 정시 오프셋), 15:30 종가 체결은
        마지막 정규장 봉에 합친다. close_time = min(봉 종료, 그날 15:30 KST)."""
        secs = interval_to_seconds(interval)
        buckets: dict[datetime, _Bucket] = {}
        for r in sorted(rows, key=lambda x: x.ts):
            kst = r.ts.astimezone(KST)
            anchor = r.ts
            if kst.time() >= KRX_CLOSE:
                anchor = datetime.combine(kst.date(), KRX_SESSION_LAST_MINUTE, tzinfo=KST).astimezone(UTC)
            start = floor_to_interval(anchor, interval)
            session_close = _kst_session_close(kst.date())
            close_time = min(start + timedelta(seconds=secs), session_close)
            b = buckets.get(start)
            if b is None:
                buckets[start] = _Bucket(start, close_time, r.open, r.high, r.low, r.close, r.volume)
            else:
                b.high = max(b.high, r.high)
                b.low = min(b.low, r.low)
                b.close = r.close
                b.volume += r.volume
        return [buckets[k] for k in sorted(buckets)]

    # ------------------------------------------------------------------ 계좌
    def _inquire_balance(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        self._require_account()
        assert self._cano and self._acnt_prdt_cd
        holdings: list[dict[str, Any]] = []
        summary: dict[str, Any] = {}
        fk = ""
        nk = ""
        tr_cont = ""
        for _ in range(BALANCE_MAX_PAGES):
            params = {
                "CANO": self._cano,
                "ACNT_PRDT_CD": self._acnt_prdt_cd,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",  # 종목별
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "00",  # 전일매매 포함
                "CTX_AREA_FK100": fk,
                "CTX_AREA_NK100": nk,
            }
            res = self._call("GET", PATH_BALANCE, self._tr("balance"), params=params, tr_cont=tr_cont)
            out1 = res.body.get("output1") or []
            if isinstance(out1, dict):
                out1 = [out1]
            holdings.extend(r for r in out1 if isinstance(r, dict))
            out2 = res.body.get("output2") or []
            if isinstance(out2, dict):
                out2 = [out2]
            if not summary:
                for r in out2:
                    if isinstance(r, dict) and r:
                        summary = dict(r)
                        break
            if not res.has_next:
                break
            fk = str(res.body.get("ctx_area_fk100") or "")
            nk = str(res.body.get("ctx_area_nk100") or "")
            tr_cont = "N"
        return holdings, summary

    def get_balances(self) -> dict[str, Balance]:
        """KRW 예수금. total=예수금총금액(dnca_tot_amt), available=D+2 가수도정산금액(prvs_rcdl_excc_amt).

        ``available`` 은 주문에 쓸 수 있는 현금의 상한일 뿐이다. 시장가 매수는 서버가 상한가 × 수량을 주문가능금액에서
        잡으므로 실제 매수 가능 수량은 ``place_order`` 가 매수가능조회(``get_buying_power``) 로 맞춘다.
        """
        _, summary = self._inquire_balance()
        total = _to_float(summary.get("dnca_tot_amt"))
        available = _to_float(summary.get("prvs_rcdl_excc_amt"))
        if available is None:
            available = _to_float(summary.get("nxdy_excc_amt"))
        if total is None:
            total = available
        if total is None:
            raise BrokerError("잔고 응답에 예수금(dnca_tot_amt) 이 없습니다", payload=summary)
        if available is None:
            available = total
        return {"KRW": Balance(currency="KRW", total=total, available=available)}

    def get_positions(self) -> dict[str, Position]:
        holdings, _ = self._inquire_balance()
        return self._positions_from_rows(holdings)

    @staticmethod
    def _positions_from_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, Position]:
        out: dict[str, Position] = {}
        for row in rows:
            qty = _to_float(row.get("hldg_qty")) or 0.0
            symbol = str(row.get("pdno", "")).strip()
            if qty <= 0 or not symbol:
                continue
            avg = _to_float(row.get("pchs_avg_pric")) or 0.0
            meta = {
                "name": str(row.get("prdt_name", "")).strip(),
                "current_price": _to_float(row.get("prpr")),
                "evaluation": _to_float(row.get("evlu_amt")),
                "ord_psbl_qty": _to_float(row.get("ord_psbl_qty")),
            }
            prev = out.get(symbol)
            if prev is None:
                out[symbol] = Position(symbol=symbol, quantity=qty, average_price=avg, meta=meta)
            else:  # 같은 종목이 여러 행(대출일별 등)이면 가중평균으로 합친다
                total = prev.quantity + qty
                prev.average_price = (
                    (prev.average_price * prev.quantity + avg * qty) / total if total else avg
                )
                prev.quantity = total
        return out

    def get_equity(self, symbols: list[str] | None = None) -> float:
        """총평가금액(tot_evlu_amt). 없으면 예수금 + 보유종목 평가금액 합."""
        holdings, summary = self._inquire_balance()
        total = _to_float(summary.get("tot_evlu_amt"))
        if total is not None and total > 0:
            return total
        cash = _to_float(summary.get("dnca_tot_amt")) or 0.0
        value = 0.0
        for row in holdings:
            qty = _to_float(row.get("hldg_qty")) or 0.0
            if qty <= 0:
                continue
            ev = _to_float(row.get("evlu_amt"))
            if ev is None:
                px = _to_float(row.get("prpr")) or _to_float(row.get("pchs_avg_pric")) or 0.0
                ev = qty * px
            value += ev
        return cash + value

    # ------------------------------------------------------------------ 매수가능조회
    def get_buying_power(
        self, symbol: str, order_type: OrderType = OrderType.MARKET, price: float | None = None
    ) -> KISBuyingPower:
        """매수가능조회 (``inquire-psbl-order``, 실전 TTTC8908R / 모의 VTTC8908R). 한 번에 한 종목.

        시장가는 공식 안내대로 ``ORD_DVSN=01`` + ``ORD_UNPR`` 공란으로 조회해 종목증거금율·상한가가 반영된 수량을
        받고, 지정가는 ``ORD_DVSN=00`` + 호가단위로 반올림한 단가로 조회한다. CMA 평가금액/해외 자산은 포함하지 않는다.
        """
        sym = self._check_symbol(symbol)
        order_type = OrderType(order_type)
        self._require_account()
        assert self._cano and self._acnt_prdt_cd
        if order_type == OrderType.MARKET:
            ord_dvsn, ord_unpr = "01", ""
        elif order_type == OrderType.LIMIT:
            if price is None:
                raise OrderError("지정가 매수가능조회에는 price 가 필요합니다")
            ord_dvsn, ord_unpr = "00", str(int(self.round_price(sym, price)))
        else:
            raise OrderError(f"매수가능조회를 지원하지 않는 주문 유형: {order_type}")
        params = {
            "CANO": self._cano,
            "ACNT_PRDT_CD": self._acnt_prdt_cd,
            "PDNO": sym,
            "ORD_UNPR": ord_unpr,
            "ORD_DVSN": ord_dvsn,
            "CMA_EVLU_AMT_ICLD_YN": "N",
            "OVRS_ICLD_YN": "N",
        }
        res = self._call("GET", PATH_PSBL_ORDER, self._tr("psbl_order"), params=params)
        raw_out = res.body.get("output") or {}
        out = _lower_keys(raw_out) if isinstance(raw_out, dict) else {}
        amount = _to_float(out.get("nrcvb_buy_amt"))
        quantity = _to_float(out.get("nrcvb_buy_qty"))
        if amount is None or quantity is None:
            raise BrokerError(
                f"{sym} 매수가능조회 응답에 nrcvb_buy_amt/nrcvb_buy_qty 가 없습니다", payload=res.body
            )
        cash = _to_float(out.get("ord_psbl_cash"))
        max_amount = _to_float(out.get("max_buy_amt"))
        max_quantity = _to_float(out.get("max_buy_qty"))
        calc_price = _to_float(out.get("psbl_qty_calc_unpr"))
        return KISBuyingPower(
            symbol=sym,
            cash=cash if cash is not None else amount,
            amount=amount,
            quantity=max(int(quantity), 0),
            max_amount=max_amount if max_amount is not None else amount,
            max_quantity=max(int(max_quantity), 0) if max_quantity is not None else max(int(quantity), 0),
            calc_price=calc_price if calc_price is not None and calc_price > 0 else None,
            raw=dict(raw_out) if isinstance(raw_out, dict) else {},
        )

    def _cap_buy_quantity(self, sym: str, qty: int, order_type: OrderType, price: float | None) -> int:
        """매수 수량을 서버가 계산한 매수가능수량(nrcvb_buy_qty) 이하로 맞춘다.

        시장가 매수는 ORD_UNPR=0 으로 나가고 서버는 상한가 × 수량을 주문가능금액에서 잡는다 (공식 order_cash 안내:
        "ORD_UNPR 이 없는 주문은 상한가로 주문금액을 선정"). 현재가로 사이징한 수량은 예수금의 약 77% 를 넘는 순간
        APBK0918(주문가능금액 초과) 로 거부되므로, 공식 안내대로 주문 전에 매수가능조회(ORD_DVSN=01) 로 수량을 맞춘다.
        조회 자체가 실패하면(인증 오류 제외) 경고만 남기고 요청 수량을 그대로 보낸다 — 최종 판정은 서버가 한다.
        """
        try:
            bp = self.get_buying_power(sym, order_type, price)
        except AuthenticationError:
            raise
        except BrokerError as e:
            logger.warning("KIS %s 매수가능조회 실패 → 요청 수량 %d주 그대로 주문합니다: %s", sym, qty, e)
            return qty
        basis = "시장가(상한가 기준)" if order_type == OrderType.MARKET else "지정가"
        calc = f"{bp.calc_price:,.0f}" if bp.calc_price else "-"
        if bp.quantity < 1:
            raise InsufficientFunds(
                f"{sym} 매수가능수량 0주 ({basis}, 계산단가 {calc}, 주문가능현금 {bp.cash:,.0f} KRW, "
                f"미수없는매수금액 {bp.amount:,.0f} KRW)",
                payload=bp.raw,
            )
        if qty > bp.quantity:
            logger.warning(
                "KIS %s 매수 수량 %d주 → 매수가능수량 %d주로 축소 (%s, 계산단가 %s, 미수없는매수금액 %s KRW)",
                sym,
                qty,
                bp.quantity,
                basis,
                calc,
                f"{bp.amount:,.0f}",
            )
            return bp.quantity
        return qty

    # ------------------------------------------------------------------ 주문
    def place_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        order_type: OrderType = OrderType.MARKET,
        price: float | None = None,
    ) -> Order:
        sym = self._check_symbol(symbol, OrderError)
        side = OrderSide(side)
        order_type = OrderType(order_type)
        self._require_account()
        assert self._cano and self._acnt_prdt_cd
        if order_type == OrderType.STOP:
            raise OrderError("KIS 는 STOP 주문을 지원하지 않습니다 (엔진/백테스터가 폴링으로 흉내냅니다)")
        qty = int(self.round_quantity(sym, quantity))
        if qty < 1:
            raise OrderError(f"주문 수량은 1주 이상의 정수여야 합니다 (입력 {quantity!r})")
        px: float | None = None
        if order_type == OrderType.MARKET:
            ord_dvsn, ord_unpr = "01", "0"
        elif order_type == OrderType.LIMIT:
            if price is None:
                raise OrderError("지정가 주문에는 price 가 필요합니다")
            px = self.round_price(sym, price)
            ord_dvsn, ord_unpr = "00", str(int(px))
        else:  # pragma: no cover - enum 확장 대비
            raise OrderError(f"지원하지 않는 주문 유형: {order_type}")
        if side == OrderSide.BUY:
            qty = self._cap_buy_quantity(sym, qty, order_type, px)

        tr_id = self._tr("buy" if side == OrderSide.BUY else "sell")
        body = {
            "CANO": self._cano,
            "ACNT_PRDT_CD": self._acnt_prdt_cd,
            "PDNO": sym,
            "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(qty),
            "ORD_UNPR": ord_unpr,
            "EXCG_ID_DVSN_CD": "KRX",
            "SLL_TYPE": "",
            "CNDT_PRIC": "",
        }
        res = self._call("POST", PATH_ORDER_CASH, tr_id, body=body)
        raw_out = res.body.get("output") or {}
        out = _lower_keys(raw_out) if isinstance(raw_out, dict) else {}
        odno = str(out.get("odno", "")).strip()
        if not odno:
            raise OrderError("주문 응답에 주문번호(ODNO) 가 없습니다", payload=res.body)
        orgno = str(out.get("krx_fwdg_ord_orgno", "")).strip()
        ord_tmd = str(out.get("ord_tmd", "")).strip()
        now = now_utc()
        created = _kst_datetime(now.astimezone(KST).strftime("%Y%m%d"), ord_tmd) or now
        order = Order(
            id=odno,
            symbol=sym,
            side=side,
            type=order_type,
            quantity=float(qty),
            price=px,
            status=OrderStatus.OPEN,
            created_at=created,
            raw={
                "KRX_FWDG_ORD_ORGNO": orgno,
                "ODNO": odno,
                "ORD_TMD": ord_tmd,
                "PDNO": sym,
                "ORD_DVSN": ord_dvsn,
                "ORD_QTY": str(qty),
                "ORD_UNPR": ord_unpr,
                "EXCG_ID_DVSN_CD": "KRX",
                "tr_id": tr_id,
                "msg_cd": res.body.get("msg_cd"),
                "msg1": str(res.body.get("msg1", "")).strip(),
            },
        )
        self._order_cache[_norm_odno(odno)] = {"orgno": orgno, "ord_dvsn": ord_dvsn, "symbol": sym}
        logger.info(
            "KIS 주문 접수 [%s] %s %s %s %d주 @%s → ODNO %s",
            self._env,
            sym,
            side.value,
            order_type.value,
            qty,
            ord_unpr if px is not None else "시장가",
            odno,
        )
        return order

    def _daily_ccld_rows(
        self,
        *,
        start: date,
        end: date,
        odno: str = "",
        pdno: str = "",
        ccld_dvsn: str = "00",
    ) -> list[dict[str, Any]]:
        """주식일별주문체결조회 (최근 3개월 이내) 페이지네이션."""
        self._require_account()
        assert self._cano and self._acnt_prdt_cd
        rows: list[dict[str, Any]] = []
        fk = ""
        nk = ""
        tr_cont = ""
        for _ in range(CCLD_MAX_PAGES):
            params = {
                "CANO": self._cano,
                "ACNT_PRDT_CD": self._acnt_prdt_cd,
                "INQR_STRT_DT": start.strftime("%Y%m%d"),
                "INQR_END_DT": end.strftime("%Y%m%d"),
                "SLL_BUY_DVSN_CD": "00",
                "PDNO": pdno,
                "CCLD_DVSN": ccld_dvsn,
                "INQR_DVSN": "00",  # 역순
                "INQR_DVSN_3": "00",
                "ORD_GNO_BRNO": "",
                "ODNO": odno,
                "INQR_DVSN_1": "",
                "EXCG_ID_DVSN_CD": "KRX",
                "CTX_AREA_FK100": fk,
                "CTX_AREA_NK100": nk,
            }
            res = self._call("GET", PATH_DAILY_CCLD, self._tr("daily_ccld"), params=params, tr_cont=tr_cont)
            out1 = res.body.get("output1") or []
            if isinstance(out1, dict):
                out1 = [out1]
            rows.extend(r for r in out1 if isinstance(r, dict) and str(r.get("odno", "")).strip())
            if not res.has_next:
                break
            fk = str(res.body.get("ctx_area_fk100") or "")
            nk = str(res.body.get("ctx_area_nk100") or "")
            tr_cont = "N"
        return rows

    def _order_from_row(self, row: Mapping[str, Any]) -> Order:
        side = OrderSide.BUY if str(row.get("sll_buy_dvsn_cd", "")).strip() == "02" else OrderSide.SELL
        ord_dvsn_cd = str(row.get("ord_dvsn_cd", "")).strip()
        unpr = _to_float(row.get("ord_unpr")) or 0.0
        if ord_dvsn_cd == "00":
            otype = OrderType.LIMIT
        elif ord_dvsn_cd == "01":
            otype = OrderType.MARKET
        else:
            otype = OrderType.LIMIT if unpr > 0 else OrderType.MARKET
        qty = _to_float(row.get("ord_qty")) or 0.0
        filled = _to_float(row.get("tot_ccld_qty")) or 0.0
        rmn = _to_float(row.get("rmn_qty"))
        rjct = _to_float(row.get("rjct_qty")) or 0.0
        canceled_flag = str(row.get("cncl_yn", "")).strip().upper() == "Y"
        avg = _to_float(row.get("avg_prvs")) if filled > 0 else None
        if avg is not None and avg <= 0:
            avg = None
        created = _kst_datetime(str(row.get("ord_dt", "")), str(row.get("ord_tmd", "")))
        today_kst = now_utc().astimezone(KST).date()
        order_day = created.astimezone(KST).date() if created else today_kst

        if qty > 0 and filled >= qty:
            status = OrderStatus.FILLED
        elif canceled_flag:
            status = OrderStatus.CANCELED
        elif rjct > 0 and filled <= 0:
            status = OrderStatus.REJECTED
        elif rmn is not None and rmn <= 0 and filled < qty:
            status = OrderStatus.CANCELED
        elif order_day < today_kst:
            status = OrderStatus.EXPIRED
        elif filled > 0:
            status = OrderStatus.PARTIALLY_FILLED
        else:
            status = OrderStatus.OPEN

        odno = str(row.get("odno", "")).strip()
        orgno = str(row.get("ord_gno_brno", "")).strip()
        raw = dict(row)
        raw["KRX_FWDG_ORD_ORGNO"] = orgno
        if not status.is_terminal and orgno:
            self._order_cache.setdefault(
                _norm_odno(odno),
                {
                    "orgno": orgno,
                    "ord_dvsn": ord_dvsn_cd or ("00" if otype == OrderType.LIMIT else "01"),
                    "symbol": str(row.get("pdno", "")).strip(),
                },
            )
        return Order(
            id=odno,
            symbol=str(row.get("pdno", "")).strip(),
            side=side,
            type=otype,
            quantity=qty,
            price=unpr if otype == OrderType.LIMIT and unpr > 0 else None,
            status=status,
            filled_quantity=filled,
            average_price=avg,
            fee=0.0,
            created_at=created or now_utc(),
            updated_at=now_utc(),
            raw=raw,
        )

    def get_order(self, order_id: str, symbol: str | None = None) -> Order:
        self._require_account()
        oid = str(order_id).strip()
        if not oid:
            raise OrderError("order_id 가 비었습니다")
        today = now_utc().astimezone(KST).date()
        rows = self._daily_ccld_rows(
            start=today - timedelta(days=ORDER_LOOKUP_DAYS),
            end=today,
            odno=oid,
            pdno=self._check_symbol(symbol, OrderError) if symbol else "",
        )
        target = _norm_odno(oid)
        for row in rows:
            if _norm_odno(row.get("odno")) == target:
                return self._order_from_row(row)
        raise OrderError(f"주문을 찾을 수 없습니다: {oid}")

    def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        self._require_account()
        sym = self._check_symbol(symbol, OrderError) if symbol else None
        today = now_utc().astimezone(KST).date()
        rows = self._daily_ccld_rows(start=today, end=today, pdno=sym or "", ccld_dvsn="02")  # 02: 미체결
        orders = [self._order_from_row(r) for r in rows]
        return [o for o in orders if not o.status.is_terminal and (sym is None or o.symbol == sym)]

    def cancel_order(self, order_id: str, symbol: str | None = None) -> bool:
        """잔량 전부 취소. 이미 종료된 주문이면 False."""
        self._require_account()
        assert self._cano and self._acnt_prdt_cd
        oid = str(order_id).strip()
        info = self._order_cache.get(_norm_odno(oid))
        if info is None:
            order = self.get_order(oid, symbol)
            if order.status.is_terminal:
                logger.info("KIS 주문 %s 는 이미 %s 상태라 취소하지 않습니다", oid, order.status.value)
                return False
            info = {
                "orgno": str(order.raw.get("KRX_FWDG_ORD_ORGNO", "")),
                "ord_dvsn": str(
                    order.raw.get("ord_dvsn_cd") or ("00" if order.type == OrderType.LIMIT else "01")
                ),
                "symbol": order.symbol,
            }
        if not info.get("orgno"):
            raise OrderError(f"취소에 필요한 KRX_FWDG_ORD_ORGNO 를 찾을 수 없습니다: {oid}")
        body = {
            "CANO": self._cano,
            "ACNT_PRDT_CD": self._acnt_prdt_cd,
            "KRX_FWDG_ORD_ORGNO": info["orgno"],
            "ORGN_ODNO": oid,
            "ORD_DVSN": info.get("ord_dvsn") or "00",
            "RVSE_CNCL_DVSN_CD": "02",  # 취소
            "ORD_QTY": "0",  # 잔량 전부
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "Y",
            "EXCG_ID_DVSN_CD": "KRX",
        }
        res = self._call("POST", PATH_ORDER_RVSECNCL, self._tr("cancel"), body=body)
        self._order_cache.pop(_norm_odno(oid), None)
        logger.info(
            "KIS 주문 취소 접수 [%s] ODNO %s: %s", self._env, oid, str(res.body.get("msg1", "")).strip()
        )
        return True


__all__ = ["KISBroker", "KISBuyingPower", "krx_tick_size", "round_to_tick"]
