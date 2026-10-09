"""대시보드 서비스 — HTTP 와 무관한 비즈니스 로직 (``app.py`` 가 라우트에서 호출).

담당
- 엔진: 모의투자 ``Trader`` 를 데몬 스레드에서 시작/정지 (``cli.run`` 의 paper 분기와 같은 배선).
  ``Trader.run_forever`` 는 메인 스레드가 아니면 시그널 핸들러를 설치하지 않으므로 그대로 호출한다.
- 상태: 프로세스 내 엔진이 있으면 ``Trader.status()``, 없으면 상태 파일(data/state.json) 로 같은 모양의 요약을 만든다.
  ``external_running`` = 프로세스 내 엔진이 없고 상태 파일이 ``max(3*poll_seconds, 120초)`` 안에 갱신됐을 때
  (단, 이 프로세스의 엔진이 마지막으로 저장한 파일 그대로면 외부 실행으로 보지 않는다).
- 시세: 설정된 시세 브로커(``cli._data_config`` 와 같은 sandbox 규칙) 의 ``get_ticker`` / ``get_candles`` (5초 캐시).
- 자산 이력: 상태를 서빙할 때마다(또는 엔진 실행 중 60초마다) ``equity_history.jsonl`` 에 한 줄 추가
  (60초에 최대 1점, 최근 10,000줄 유지).
- 로그: ``config.logging.file`` 의 꼬리를 비밀값 마스킹 후 반환.

비밀값은 어떤 응답에도 넣지 않는다. 가짜/데모 시세는 없다.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tradingbot.brokers import create_broker
from tradingbot.brokers.base import BaseBroker
from tradingbot.brokers.paper import PaperBroker
from tradingbot.cli import _data_broker_name, _data_config
from tradingbot.config import AppConfig, Credentials
from tradingbot.engine.state import (
    StateStore,
    deserialize_trade,
    dt_from_iso,
    dt_to_iso,
    serialize_trade,
    to_jsonable,
)
from tradingbot.engine.trader import Trader
from tradingbot.exceptions import (
    AuthenticationError,
    BrokerError,
    ConfigError,
    DataError,
    TradingBotError,
)
from tradingbot.models import INTERVAL_SECONDS, ensure_utc, utcnow
from tradingbot.notify import create_notifier, mask_secrets
from tradingbot.risk import RiskManager
from tradingbot.strategies import available_strategies, create_strategy, get_strategy_class
from tradingbot.web.jobs import BacktestJobRunner

logger = logging.getLogger(__name__)

__all__ = [
    "EQUITY_HISTORY_FILE",
    "EQUITY_MAX_LINES",
    "EQUITY_SAMPLE_SEC",
    "PRICE_CACHE_SEC",
    "DashboardService",
    "WebError",
    "error_status",
    "is_loopback_host",
    "make_data_broker",
    "mask_log_line",
    "tail_lines",
]

#: 현재가 캐시 유지 시간 (초)
PRICE_CACHE_SEC = 5.0
#: 자산 이력 샘플 최소 간격 (초)
EQUITY_SAMPLE_SEC = 60.0
#: 자산 이력 파일에 유지하는 최대 줄 수 (초과 시 뒤에서부터 이 수만큼 남기고 다시 쓴다)
EQUITY_MAX_LINES = 10_000
#: 이 줄 수를 넘기면 정리(다시 쓰기) 한다 — 매번 다시 쓰지 않기 위한 여유분
EQUITY_TRIM_AT = EQUITY_MAX_LINES + 500
#: 자산 이력 파일 이름 (상태 파일과 같은 디렉터리)
EQUITY_HISTORY_FILE = "equity_history.jsonl"
#: 외부 엔진 실행 판정의 최소 신선도 (초)
EXTERNAL_MIN_FRESH_SEC = 120.0
#: 엔진 정지 시 스레드 join 대기 (초)
ENGINE_STOP_TIMEOUT_SEC = 30.0
#: 캔들/거래/자산 조회 상한
MAX_CANDLES = 1000
MAX_TRADES = 1000
MAX_EQUITY_POINTS = 5000
MAX_LOG_LINES = 2000

#: 심볼 표기 검사 (Upbit KRW-BTC, ccxt BTC/USDT[:USDT], KIS 005930, Alpaca AAPL/BRK.B)
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:\-]{1,39}$")

LIVE_START_MESSAGE = "실거래는 터미널에서 tradingbot run --live 로만 시작할 수 있습니다"


class WebError(TradingBotError):
    """HTTP 상태 코드를 가진 오류 (한국어 메시지). ``app.py`` 가 ``{"error": message}`` 로 변환한다."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.message = message


def error_status(exc: BaseException) -> int:
    """봇 예외 → HTTP 상태 코드. 설정/입력 오류 400, 인증 401, 브로커(네트워크) 502, 그 외 500."""
    if isinstance(exc, WebError):
        return exc.status_code
    if isinstance(exc, AuthenticationError):
        return 401
    if isinstance(exc, (ConfigError, DataError)):
        return 400
    if isinstance(exc, BrokerError):
        return 502
    return 500


def is_loopback_host(host: str) -> bool:
    """``127.0.0.1`` / ``localhost`` / ``::1`` 처럼 로컬에서만 접근 가능한 바인드 주소인지."""
    h = (host or "").strip().lower()
    if h in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def make_data_broker(config: AppConfig) -> BaseBroker:
    """시세 전용 브로커 (``cli.run`` paper 분기와 동일: ccxt 계열은 sandbox 해제). ``paper`` 면 ConfigError."""
    name = _data_broker_name(config)
    return create_broker(name, _data_config(config))


# ============================================================================ 파일 유틸
def tail_lines(path: Path, n: int, *, block_size: int = 8192) -> list[str]:
    """파일 끝에서 ``n`` 줄 (UTF-8, 디코딩 오류는 대체 문자). 파일이 없으면 ``[]``."""
    if n <= 0 or not path.is_file():
        return []
    chunks: list[bytes] = []
    newlines = 0
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        while pos > 0 and newlines <= n:
            step = min(block_size, pos)
            pos -= step
            f.seek(pos)
            chunk = f.read(step)
            chunks.append(chunk)
            newlines += chunk.count(b"\n")
    data = b"".join(reversed(chunks))
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    return lines[-n:]


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ============================================================================ 로그 마스킹
_MASK = "***"
#: key=value / key: value 형태의 비밀값 (키 이름에 secret/token/…/key 가 들어가면 값을 가린다. UPBIT_SECRET_KEY 처럼
#: 밑줄로 이어진 이름도 잡기 위해 \b 대신 접두/접미 단어 문자를 허용한다)
_KV_SECRET_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])([A-Za-z0-9_\-]*?(?:secret|token|password|passwd|pwd|webhook|authorization|"
    r"(?:access|secret|app|api|private)[_-]?key|\bkey)[A-Za-z0-9_\-]*?)"
    r"(\s*[:=]\s*)(['\"]?)(?!\*\*\*)(?!bearer\b)([^\s'\",;]{4,})"
)
#: Bearer 토큰
_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._\-~+/=]{8,})")
#: 텔레그램 봇 URL (bot<token>), 슬랙/디스코드 웹훅 URL
_TELEGRAM_RE = re.compile(r"(api\.telegram\.org/bot)([^/\s]+)")
_SLACK_RE = re.compile(r"(hooks\.slack\.com/services/)([^\s'\"]+)")
_DISCORD_RE = re.compile(r"((?:discord(?:app)?\.com)/api/webhooks/)([^\s'\"]+)")
#: JWT (header.payload.signature)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")
#: 긴 토큰처럼 생긴 문자열 (32자 이상 영숫자, 문자와 숫자가 모두 있어야 함 — 긴 단어/숫자열은 제외)
_LONG_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_\-])(?=[A-Za-z0-9_\-]*[A-Za-z])(?=[A-Za-z0-9_\-]*\d)[A-Za-z0-9_\-]{32,}"
)


def mask_log_line(line: str) -> str:
    """등록된 비밀값(``mask_secrets``) + 토큰처럼 보이는 문자열을 ``***`` 로 가린다."""
    text = mask_secrets(line)
    text = _BEARER_RE.sub(lambda m: f"{m.group(1)}{_MASK}", text)
    text = _TELEGRAM_RE.sub(lambda m: f"{m.group(1)}{_MASK}", text)
    text = _SLACK_RE.sub(lambda m: f"{m.group(1)}{_MASK}", text)
    text = _DISCORD_RE.sub(lambda m: f"{m.group(1)}{_MASK}", text)
    text = _JWT_RE.sub(_MASK, text)
    text = _LONG_TOKEN_RE.sub(_MASK, text)
    text = _KV_SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{_MASK}", text)
    return text


# ============================================================================ 숫자 유틸
def _num(value: Any) -> float | None:
    """JSON 안전한 float (None / bool / NaN / inf / 변환 불가 → None)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _positive(value: Any) -> float | None:
    f = _num(value)
    return f if f is not None and f > 0 else None


def _err(e: BaseException) -> str:
    return mask_secrets(str(e)) or type(e).__name__


# ============================================================================ 서비스
class DashboardService:
    """대시보드 비즈니스 로직. 모든 공개 메서드는 JSON 직렬화 가능한 dict 를 돌려주거나 ``WebError`` 를 던진다.

    ``broker_factory(config) -> BaseBroker`` 는 시세 브로커 생성기 (기본 ``make_data_broker``; 테스트는
    ``tradingbot.web.service.create_broker`` 를 monkeypatch 하거나 이 인자로 가짜 피드를 넣는다).
    """

    def __init__(
        self,
        config: AppConfig,
        *,
        config_path: Path | None = None,
        data_dir: Path | None = None,
        broker_factory: Callable[[AppConfig], BaseBroker] | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.config = config
        self.config_path: Path | None = Path(config_path) if config_path is not None else None
        self.state_store = StateStore(config.engine.state_file)
        self.data_dir: Path = Path(data_dir) if data_dir is not None else self.state_store.path.parent
        self.equity_path: Path = self.data_dir / EQUITY_HISTORY_FILE
        self._broker_factory: Callable[[AppConfig], BaseBroker] = broker_factory or make_data_broker
        self._clock = clock

        self._lock = threading.RLock()
        self._data_broker: BaseBroker | None = None
        self._price_cache: dict[str, tuple[float, float]] = {}  # symbol -> (price, monotonic)
        self._prices_at: datetime | None = None

        # 프로세스 내 엔진
        self._trader: Trader | None = None
        self._engine_thread: threading.Thread | None = None
        self._engine_broker: BaseBroker | None = None
        self._engine_error: str | None = None
        self._engine_stopped_at: datetime | None = None
        #: 이 프로세스의 엔진이 마지막으로 저장한 상태 파일 서명 (mtime_ns, updated_at) — 외부 실행 오판 방지
        self._own_state_sig: tuple[int, str | None] | None = None

        # 자산 이력
        self._equity_lock = threading.Lock()
        self._equity_lines = 0
        self._last_sample_at: datetime | None = None
        self._load_equity_meta()

        self.jobs = BacktestJobRunner(config, broker_factory=self._broker_factory)

    # ------------------------------------------------------------------ 공통
    def now(self) -> datetime:
        return ensure_utc(self._clock())

    def close(self) -> None:
        """서버 종료: 프로세스 내 엔진이 있으면 정지하고 브로커 세션을 닫는다."""
        try:
            if self.engine_alive():
                self.stop_engine()
        except Exception as e:  # noqa: BLE001 - 종료 중 오류는 로그만
            logger.warning("엔진 정지 중 오류 (무시): %s", _err(e))
        self.jobs.close()
        with self._lock:
            broker, self._data_broker = self._data_broker, None
        self._close_broker(broker)

    @staticmethod
    def _close_broker(broker: BaseBroker | None) -> None:
        if broker is None:
            return
        try:
            broker.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("브로커 종료 중 오류 (무시): %s", _err(e))

    def _get_data_broker(self) -> BaseBroker:
        """시세 조회용 브로커 (한 번 만들어 재사용). ``broker.name=paper`` 등 설정 오류는 WebError(400)."""
        with self._lock:
            if self._data_broker is None:
                try:
                    self._data_broker = self._broker_factory(self.config)
                except TradingBotError as e:
                    raise WebError(error_status(e), f"시세 브로커를 만들 수 없습니다: {_err(e)}") from e
            return self._data_broker

    # ------------------------------------------------------------------ 기본 정보
    def health(self) -> dict[str, Any]:
        from tradingbot import __version__

        return {"ok": True, "version": __version__}

    def config_payload(self) -> dict[str, Any]:
        """설정 (AppConfig 에는 비밀값이 없다 — 키는 .env 에만 있다)."""
        return {
            "config_path": str(self.config_path) if self.config_path is not None else None,
            "config": self.config.model_dump(mode="json"),
        }

    def strategies(self) -> dict[str, Any]:
        out: list[dict[str, Any]] = []
        for name in available_strategies():
            cls = get_strategy_class(name)
            warmup: int | None
            try:
                warmup = int(cls().warmup)
            except Exception:  # noqa: BLE001 - 목록 표시는 생성 실패해도 계속
                warmup = None
            out.append(
                {
                    "name": name,
                    "description": cls.description or "",
                    "default_params": to_jsonable(dict(cls.default_params)),
                    "warmup": warmup,
                }
            )
        return {"strategies": out}

    # ------------------------------------------------------------------ 시세
    def _fetch_prices(self, symbols: Iterable[str]) -> tuple[dict[str, float], dict[str, str]]:
        """심볼별 현재가 (5초 캐시). 실패한 심볼은 ``errors`` 에 메시지."""
        prices: dict[str, float] = {}
        errors: dict[str, str] = {}
        broker: BaseBroker | None = None
        for sym in symbols:
            mono = time.monotonic()
            with self._lock:
                cached = self._price_cache.get(sym)
            if cached is not None and mono - cached[1] < PRICE_CACHE_SEC:
                prices[sym] = cached[0]
                continue
            try:
                if broker is None:
                    broker = self._get_data_broker()
                px = float(broker.get_ticker(sym))
                if not math.isfinite(px) or px <= 0:
                    raise BrokerError(f"{sym} 현재가가 유효하지 않습니다: {px!r}")
            except WebError:
                raise
            except Exception as e:  # noqa: BLE001 - 심볼 하나의 실패가 나머지를 막지 않게
                errors[sym] = _err(e)
                logger.warning("%s 현재가 조회 실패: %s", sym, _err(e))
                continue
            with self._lock:
                self._price_cache[sym] = (px, time.monotonic())
            prices[sym] = px
        return prices, errors

    def prices(self, symbols: list[str] | None = None) -> dict[str, Any]:
        """``GET /api/prices``. 설정 심볼의 현재가 (실제 시세 브로커, 5초 캐시)."""
        syms = [s for s in (symbols or self.config.symbols) if s]
        prices, errors = self._fetch_prices(syms)
        now = self.now()
        payload: dict[str, Any] = {"prices": prices, "at": dt_to_iso(now)}
        if errors:
            payload["errors"] = errors
        return payload

    def candles(self, symbol: str, interval: str, limit: int) -> dict[str, Any]:
        """``GET /api/candles``. 시세 브로커의 완성 캔들 (오래된→최신)."""
        if (self.config.broker.name or "").lower() == "paper":
            raise WebError(400, "broker.name=paper 는 시세 출처가 없어 캔들을 조회할 수 없습니다")
        symbol = (symbol or "").strip()
        if not symbol or not _SYMBOL_RE.fullmatch(symbol):
            raise WebError(400, f"심볼 형식이 잘못되었습니다: {symbol!r}")
        if interval not in INTERVAL_SECONDS:
            raise WebError(400, f"지원하지 않는 interval {interval!r} (가능: {', '.join(INTERVAL_SECONDS)})")
        try:
            limit = int(limit)
        except (TypeError, ValueError) as e:
            raise WebError(400, f"limit 은 정수여야 합니다: {limit!r}") from e
        limit = max(1, min(limit, MAX_CANDLES))
        broker = self._get_data_broker()
        supported = tuple(getattr(broker, "supported_intervals", ()) or ())
        if supported and interval not in supported:
            raise WebError(
                400,
                f"브로커 {broker.name} 는 interval {interval!r} 를 지원하지 않습니다 (가능: {', '.join(supported)})",
            )
        try:
            candles = broker.get_candles(symbol, interval, limit=limit)
        except TradingBotError as e:
            status = 400 if symbol not in self.config.symbols else error_status(e)
            raise WebError(status, f"{symbol} {interval} 캔들 조회 실패: {_err(e)}") from e
        return {
            "symbol": symbol,
            "interval": interval,
            "candles": [
                {
                    "t": dt_to_iso(c.timestamp),
                    "o": float(c.open),
                    "h": float(c.high),
                    "l": float(c.low),
                    "c": float(c.close),
                    "v": float(c.volume),
                }
                for c in candles
            ],
        }

    # ------------------------------------------------------------------ 상태
    def engine_alive(self) -> bool:
        with self._lock:
            return (
                self._trader is not None
                and self._engine_thread is not None
                and self._engine_thread.is_alive()
            )

    def _fresh_window_sec(self) -> float:
        return max(3.0 * float(self.config.engine.poll_seconds), EXTERNAL_MIN_FRESH_SEC)

    def _state_signature(self) -> tuple[int, str | None] | None:
        path = self.state_store.path
        try:
            st = path.stat()
        except OSError:
            return None
        updated: str | None = None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                updated = data.get("updated_at")
        except (OSError, ValueError):
            updated = None
        return (st.st_mtime_ns, updated)

    def _remember_own_state(self) -> None:
        self._own_state_sig = self._state_signature()

    def _state_updated_at(self, data: Mapping[str, Any]) -> datetime | None:
        try:
            parsed = dt_from_iso(data.get("updated_at"))
        except ValueError:
            parsed = None
        if parsed is not None:
            return parsed
        try:
            return datetime.fromtimestamp(self.state_store.path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            return None

    def _is_external_running(self, data: Mapping[str, Any], updated_at: datetime | None) -> bool:
        if not data or updated_at is None or self.engine_alive():
            return False
        age = (self.now() - updated_at).total_seconds()
        if age > self._fresh_window_sec():
            return False
        # 이 프로세스의 엔진이 저장한 파일 그대로면 외부 프로세스가 아니다
        return self._own_state_sig is None or self._state_signature() != self._own_state_sig

    def status(self) -> dict[str, Any]:
        """``GET /api/status``. 자산을 알 수 있으면 자산 이력에 샘플을 남긴다."""
        with self._lock:
            trader = self._trader if self.engine_alive() else None
            engine_error = self._engine_error
        now = self.now()
        if trader is not None:
            try:
                st = trader.status()
            except Exception as e:  # noqa: BLE001 - 브로커 조회 실패는 상태 조회를 막지 않는다
                raise WebError(502, f"엔진 상태 조회 실패: {_err(e)}") from e
            self.sample_equity(st.get("equity"), now)
            return {
                "source": "engine",
                "external_running": False,
                "state_updated_at": st.get("last_cycle_at") or st.get("started_at"),
                "status": st,
                "engine_error": engine_error,
                "now": dt_to_iso(now),
            }

        data = self._load_state()
        if not data:
            return {
                "source": "none",
                "external_running": False,
                "state_updated_at": None,
                "status": None,
                "engine_error": engine_error,
                "now": dt_to_iso(now),
            }
        updated_at = self._state_updated_at(data)
        external = self._is_external_running(data, updated_at)
        st = self._status_from_state(data, running=external)
        self.sample_equity(st.get("equity"), now)
        return {
            "source": "state_file",
            "external_running": external,
            "state_updated_at": dt_to_iso(updated_at),
            "status": st,
            "engine_error": engine_error,
            "now": dt_to_iso(now),
        }

    def _load_state(self) -> dict[str, Any]:
        if not self.state_store.exists:
            return {}
        try:
            return self.state_store.load()
        except DataError as e:
            raise WebError(500, f"상태 파일을 읽을 수 없습니다: {_err(e)}") from e

    def _status_from_state(self, data: Mapping[str, Any], *, running: bool) -> dict[str, Any]:
        """상태 파일 → ``Trader.status()`` 와 같은 모양의 요약 (현재가는 시세 브로커에서)."""
        raw_positions = data.get("positions") or {}
        if not isinstance(raw_positions, Mapping):
            raw_positions = {}
        paper = data.get("paper_broker") if isinstance(data.get("paper_broker"), Mapping) else None
        paper_prices: Mapping[str, Any] = paper.get("prices") or {} if paper else {}
        symbols = [str(s) for s in (data.get("symbols") or self.config.symbols)]

        prices, price_errors = self._fetch_prices(list(raw_positions)) if raw_positions else ({}, {})

        positions: dict[str, Any] = {}
        position_value = 0.0
        for sym, p in raw_positions.items():
            if not isinstance(p, Mapping):
                continue
            qty = _num(p.get("quantity")) or 0.0
            avg = _num(p.get("average_price")) or 0.0
            px = prices.get(sym)
            if px is None:
                px = _positive(paper_prices.get(sym))
            meta = p.get("meta") if isinstance(p.get("meta"), Mapping) else {}
            mark = px if px is not None else avg
            position_value += qty * mark
            positions[str(sym)] = {
                "quantity": qty,
                "average_price": avg,
                "opened_at": p.get("opened_at"),
                "stop_loss": _num(p.get("stop_loss")),
                "take_profit": _num(p.get("take_profit")),
                "highest_price": _num(p.get("highest_price")),
                "last_price": px,
                "unrealized_pnl": (px - avg) * qty if px is not None else None,
                "unrealized_pnl_pct": ((px - avg) / avg if avg else 0.0) if px is not None else None,
                "max_holding_bars": meta.get("max_holding_bars"),
                "entry_reason": meta.get("entry_reason"),
                "entry_bar_ts": meta.get("entry_bar_ts"),
                "price_error": price_errors.get(sym),
            }

        pending: dict[str, Any] = {}
        raw_pending = data.get("pending_breakouts") or {}
        if isinstance(raw_pending, Mapping):
            for sym, pb in raw_pending.items():
                if not isinstance(pb, Mapping):
                    continue
                sig = pb.get("signal") if isinstance(pb.get("signal"), Mapping) else {}
                pending[str(sym)] = {
                    "trigger": _num(pb.get("trigger")),
                    "expires": pb.get("expires"),
                    "candle_ts": pb.get("candle_ts"),
                    "reason": pb.get("reason") or sig.get("reason"),
                }

        risk = data.get("risk") if isinstance(data.get("risk"), Mapping) else {}
        cash = _num(paper.get("cash")) if paper else None
        equity = cash + position_value if cash is not None else None
        quote = (paper.get("quote_currency") if paper else None) or self.config.paper.quote_currency

        trades = data.get("trades") or []
        if not isinstance(trades, list):
            trades = []
        last_trade = self._trade_dict(trades[-1]) if trades else None

        params = data.get("strategy_params")
        if not isinstance(params, Mapping):
            params = {}
        mode = str(data.get("mode") or self.config.mode)
        return {
            "mode": mode,
            "live": mode == "live",
            "broker": data.get("broker"),
            "data_source": data.get("data_source"),
            "strategy": data.get("strategy"),
            "params": dict(params),
            "strategy_params": dict(params),
            "symbols": symbols,
            "interval": data.get("interval"),
            "quote_currency": quote,
            "started": True,
            "running": running,
            "started_at": data.get("started_at"),
            "updated_at": data.get("updated_at"),
            "last_cycle_at": data.get("updated_at"),
            "cycles": data.get("cycles"),
            "day": data.get("day"),
            "consecutive_failures": None,
            "equity": equity,
            "cash": cash,
            "daily_pnl": _num(risk.get("daily_pnl")),
            "daily_trades": risk.get("daily_trades"),
            "day_start_equity": _num(risk.get("day_start_equity")),
            "positions": positions,
            "pending_breakouts": pending,
            "last_candle_ts": dict(data.get("last_candle_ts") or {})
            if isinstance(data.get("last_candle_ts"), Mapping)
            else {},
            "last_prices": prices,
            "trades": len(trades),
            "last_trade": last_trade,
            "version": data.get("version"),
        }

    # ------------------------------------------------------------------ 포지션 / 거래
    def positions(self) -> dict[str, Any]:
        """``GET /api/positions``."""
        st = self.status()
        status = st.get("status") or {}
        raw = status.get("positions") or {}
        out: list[dict[str, Any]] = []
        for sym, p in raw.items():
            out.append(
                {
                    "symbol": sym,
                    "quantity": _num(p.get("quantity")),
                    "average_price": _num(p.get("average_price")),
                    "last_price": _num(p.get("last_price")),
                    "unrealized_pnl": _num(p.get("unrealized_pnl")),
                    "unrealized_pnl_pct": _num(p.get("unrealized_pnl_pct")),
                    "stop_loss": _num(p.get("stop_loss")),
                    "take_profit": _num(p.get("take_profit")),
                    "opened_at": p.get("opened_at"),
                    "entry_reason": p.get("entry_reason"),
                    "highest_price": _num(p.get("highest_price")),
                    "max_holding_bars": p.get("max_holding_bars"),
                }
            )
        return {"positions": out, "source": st.get("source")}

    @staticmethod
    def _trade_dict(raw: Any) -> dict[str, Any] | None:
        """상태 파일의 거래 dict → 응답 모양 (pnl/pnl_pct 는 재계산)."""
        if not isinstance(raw, Mapping):
            return None
        try:
            return serialize_trade(deserialize_trade(raw))
        except DataError:
            return None

    def trades(self, limit: int = 100) -> dict[str, Any]:
        """``GET /api/trades``. 최신 거래가 먼저."""
        limit = max(1, min(int(limit), MAX_TRADES))
        with self._lock:
            trader = self._trader if self.engine_alive() else None
        rows: list[dict[str, Any]]
        if trader is not None:
            rows = [serialize_trade(t) for t in trader.trades]
            source = "engine"
        else:
            data = self._load_state()
            raw = data.get("trades") or [] if data else []
            rows = (
                [d for d in (self._trade_dict(t) for t in raw) if d is not None]
                if isinstance(raw, list)
                else []
            )
            source = "state_file" if data else "none"
        rows.reverse()
        return {"trades": rows[:limit], "total": len(rows), "source": source}

    # ------------------------------------------------------------------ 자산 이력
    def _load_equity_meta(self) -> None:
        """시작 시 파일의 줄 수와 마지막 샘플 시각을 읽는다."""
        self._equity_lines = 0
        self._last_sample_at = None
        if not self.equity_path.is_file():
            return
        try:
            with open(self.equity_path, "rb") as f:
                self._equity_lines = sum(1 for _ in f)
            for line in reversed(tail_lines(self.equity_path, 5)):
                try:
                    obj = json.loads(line)
                    ts = dt_from_iso(obj.get("t")) if isinstance(obj, dict) else None
                except (ValueError, TypeError):
                    continue
                if ts is not None:
                    self._last_sample_at = ts
                    break
        except OSError as e:
            logger.warning("자산 이력 파일을 읽을 수 없습니다 (%s): %s", self.equity_path, e)

    def sample_equity(self, equity: Any, now: datetime | None = None) -> bool:
        """자산 샘플 1점 추가 (60초에 최대 1점, 값이 없으면 건너뜀). 추가했으면 True."""
        value = _num(equity)
        if value is None:
            return False
        now = ensure_utc(now) if now is not None else self.now()
        with self._equity_lock:
            last = self._last_sample_at
            if last is not None and (now - last).total_seconds() < EQUITY_SAMPLE_SEC:
                return False
            line = json.dumps({"t": dt_to_iso(now), "equity": value}, ensure_ascii=False)
            try:
                self.equity_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.equity_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError as e:
                logger.warning("자산 이력 저장 실패 (%s): %s", self.equity_path, e)
                return False
            self._last_sample_at = now
            self._equity_lines += 1
            if self._equity_lines > EQUITY_TRIM_AT:
                self._trim_equity_file()
        return True

    def _trim_equity_file(self) -> None:
        """최근 ``EQUITY_MAX_LINES`` 줄만 남기고 원자적으로 다시 쓴다 (``_equity_lock`` 보유 상태에서 호출)."""
        try:
            lines = tail_lines(self.equity_path, EQUITY_MAX_LINES)
            _atomic_write_text(self.equity_path, "".join(f"{ln}\n" for ln in lines))
            self._equity_lines = len(lines)
        except OSError as e:
            logger.warning("자산 이력 정리 실패 (%s): %s", self.equity_path, e)

    def equity_history(self, limit: int = 500) -> dict[str, Any]:
        """``GET /api/equity``. 최근 ``limit`` 점 (오래된→최신)."""
        limit = max(1, min(int(limit), MAX_EQUITY_POINTS))
        points: list[dict[str, Any]] = []
        with self._equity_lock:
            lines = tail_lines(self.equity_path, limit)
        for line in lines:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            value = _num(obj.get("equity"))
            t = obj.get("t")
            if value is None or not isinstance(t, str):
                continue
            points.append({"t": t, "equity": value})
        return {"points": points, "file": str(self.equity_path)}

    # ------------------------------------------------------------------ 로그
    def logs(self, lines: int = 200) -> dict[str, Any]:
        """``GET /api/logs``. 로그 파일 꼬리 (비밀값 마스킹)."""
        n = max(1, min(int(lines), MAX_LOG_LINES))
        file = self.config.logging.file
        if not file:
            return {"file": None, "lines": [], "note": "logging.file 이 설정되지 않아 파일 로그가 없습니다"}
        path = Path(file)
        if not path.is_file():
            return {"file": str(path), "lines": [], "note": "로그 파일이 아직 없습니다"}
        try:
            tail = tail_lines(path, n)
        except OSError as e:
            raise WebError(500, f"로그 파일을 읽을 수 없습니다: {e}") from e
        return {"file": str(path), "lines": [mask_log_line(ln) for ln in tail]}

    # ------------------------------------------------------------------ 엔진
    def start_engine(self) -> dict[str, Any]:
        """``POST /api/engine/start``. 모의투자 Trader 를 데몬 스레드에서 시작한다."""
        with self._lock:
            if self.config.is_live:
                raise WebError(409, LIVE_START_MESSAGE)
            if self.engine_alive():
                raise WebError(409, "엔진이 이미 이 프로세스에서 실행 중입니다")
            data = self._load_state()
            if data and self._is_external_running(data, self._state_updated_at(data)):
                raise WebError(
                    409,
                    "다른 프로세스가 상태 파일을 갱신하고 있습니다 (외부 엔진 실행 중). "
                    "그 프로세스를 먼저 종료한 뒤 다시 시도하세요",
                )
            try:
                data_broker = self._broker_factory(self.config)
            except TradingBotError as e:
                raise WebError(error_status(e), f"시세 브로커를 만들 수 없습니다: {_err(e)}") from e
            try:
                broker = PaperBroker.from_config(self.config, data_source=data_broker)
                strategy = create_strategy(self.config.strategy.name, self.config.strategy.params)
                risk = RiskManager(self.config.risk)
                notifier = create_notifier(self.config.notify, Credentials.from_env())
                state = StateStore(self.config.engine.state_file)
                trader = Trader(self.config, broker, strategy, risk, notifier, state)
                # 상태 복원/검증은 요청 스레드에서 동기적으로 — 설정 오류를 바로 돌려준다
                trader.start()
            except TradingBotError as e:
                self._close_broker(data_broker)
                raise WebError(error_status(e), f"엔진을 시작할 수 없습니다: {_err(e)}") from e
            except Exception as e:  # noqa: BLE001
                self._close_broker(data_broker)
                raise WebError(500, f"엔진을 시작할 수 없습니다: {_err(e)}") from e

            self._engine_error = None
            self._engine_stopped_at = None
            self._trader = trader
            self._engine_broker = data_broker
            thread = threading.Thread(
                target=self._run_engine, args=(trader, data_broker), name="tradingbot-web-engine", daemon=True
            )
            self._engine_thread = thread
            thread.start()
            sampler = threading.Thread(
                target=self._sample_loop, args=(trader, thread), name="tradingbot-web-equity", daemon=True
            )
            sampler.start()
        logger.info(
            "웹 대시보드에서 모의투자 엔진 시작 (%s, %s)",
            self.config.strategy.name,
            ", ".join(self.config.symbols),
        )
        return self.status()

    def _run_engine(self, trader: Trader, data_broker: BaseBroker) -> None:
        try:
            trader.run_forever()
        except Exception as e:  # noqa: BLE001 - 스레드가 조용히 죽지 않게
            logger.exception("엔진 스레드 비정상 종료: %s", _err(e))
            with self._lock:
                self._engine_error = _err(e)
        finally:
            with self._lock:
                self._remember_own_state()
                self._engine_stopped_at = self.now()
                if self._trader is trader:
                    self._trader = None
                    self._engine_thread = None
                    self._engine_broker = None
            self._close_broker(trader.broker)
            self._close_broker(data_broker)
            logger.info("웹 대시보드 엔진 스레드 종료")

    def _sample_loop(self, trader: Trader, thread: threading.Thread) -> None:
        """엔진 실행 중 60초마다 자산 샘플 (상태 조회가 없어도 자산 곡선이 이어지게)."""
        while thread.is_alive():
            thread.join(EQUITY_SAMPLE_SEC)
            if not thread.is_alive() or trader.stopped:
                break
            try:
                equity = trader.broker.get_equity(trader.symbols)
            except Exception as e:  # noqa: BLE001
                logger.debug("자산 샘플 실패: %s", _err(e))
                continue
            self.sample_equity(equity)

    def stop_engine(self) -> dict[str, Any]:
        """``POST /api/engine/stop``. ``Trader.stop()`` 후 최대 30초 join."""
        with self._lock:
            if not self.engine_alive():
                raise WebError(409, "이 프로세스에서 실행 중인 엔진이 없습니다")
            trader = self._trader
            thread = self._engine_thread
            assert trader is not None and thread is not None
            trader.stop()
        thread.join(ENGINE_STOP_TIMEOUT_SEC)
        if thread.is_alive():
            logger.warning(
                "엔진 스레드가 %.0f초 안에 끝나지 않았습니다 (종료 요청은 전달됨)", ENGINE_STOP_TIMEOUT_SEC
            )
            payload = self.status()
            payload["stop_pending"] = True
            return payload
        return self.status()
