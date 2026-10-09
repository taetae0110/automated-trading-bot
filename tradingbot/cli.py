"""명령줄 인터페이스 (typer) — ARCHITECTURE §9.

명령
    tradingbot init                      예시 설정(config/config.yaml, .env) 생성
    tradingbot validate-config -c CFG    설정 검증
    tradingbot strategies / brokers      전략 / 브로커 목록
    tradingbot download -c CFG ...       실제 거래소 캔들을 CSV 캐시로 내려받기
    tradingbot backtest -c CFG ...       백테스트 (데이터는 항상 실제 거래소/yfinance 에서 받은 것만 사용)
    tradingbot run -c CFG [--live] [--once] [--yes]   모의투자(기본) / 실거래
    tradingbot balance -c CFG            잔고/포지션
    tradingbot status -c CFG             저장된 엔진 상태 요약

원칙
- 데모/샘플 시세는 없다. 백테스트 데이터가 없으면 브로커 공개 API(Upbit/ccxt 는 키 불필요) 또는 yfinance 에서
  내려받는다. 주식 브로커(KIS/Alpaca)는 시세 조회에도 키가 필요하므로 키가 없으면 ``--source yfinance`` 를 안내한다.
- 설정/브로커 오류는 한국어 메시지와 함께 종료 코드 1 로 끝낸다 (``--debug`` 를 주면 트레이스백 출력).
- 비밀값(API 키/토큰)은 출력하지 않는다.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

import pandas as pd
import typer
from pydantic import ValidationError
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from tradingbot import __version__
from tradingbot.backtest import FILL_ON_CHOICES, Backtester, BacktestResult, format_metrics
from tradingbot.brokers import available_brokers, create_broker, get_broker_class
from tradingbot.brokers.base import BaseBroker
from tradingbot.brokers.paper import PaperBroker
from tradingbot.config import AppConfig, Credentials, LoggingConfig, load_config
from tradingbot.data import CandleStore, krx_to_yfinance, load_yfinance, symbol_safe, to_utc_datetime
from tradingbot.engine.state import StateStore
from tradingbot.engine.trader import Trader
from tradingbot.exceptions import ConfigError, DataError, TradingBotError
from tradingbot.logging_setup import setup_logging
from tradingbot.models import AssetClass, interval_to_seconds, utcnow
from tradingbot.notify import create_notifier, mask_secrets
from tradingbot.risk import RiskManager
from tradingbot.strategies import available_strategies, create_strategy, get_strategy_class

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "YFINANCE_DIR",
    "app",
    "main",
    "parse_param_value",
    "parse_params",
    "resolve_config",
    "write_report",
    "yfinance_symbol",
]

#: 기본 설정 파일 경로
DEFAULT_CONFIG_PATH = Path("config/config.yaml")
#: yfinance 로 내려받은 캔들을 저장하는 CandleStore 하위 디렉터리 (브로커 디렉터리와 분리)
YFINANCE_DIR = "yfinance"
#: download / backtest(브로커 다운로드) 의 기본 시작일: 오늘로부터 이 일수 전
DEFAULT_DOWNLOAD_DAYS = 365
#: 실거래 시작 전 카운트다운(초)
LIVE_COUNTDOWN_SEC = 5
#: 패키지 루트 (예시 설정 파일 탐색용)
PACKAGE_ROOT = Path(__file__).resolve().parent.parent

_TEMPLATES: tuple[tuple[str, str], ...] = (
    ("config/config.example.yaml", "config/config.yaml"),
    (".env.example", ".env"),
)

#: 브로커별 안내 정보 (``brokers`` 명령, 오류 메시지). 레지스트리 이름이 키.
BROKER_INFO: dict[str, dict[str, Any]] = {
    "paper": {
        "asset": "모의",
        "env": [],
        "symbol": "(시세 브로커 표기)",
        "data_needs_keys": False,
        "note": "모의 체결 엔진. run(paper) 과 백테스트가 내부적으로 사용. 시세 출처가 없으므로 broker.name 으로는 부적합",
    },
    "upbit": {
        "asset": "암호화폐 (KRW/BTC/USDT 마켓)",
        "env": ["UPBIT_ACCESS_KEY", "UPBIT_SECRET_KEY"],
        "symbol": "KRW-BTC",
        "data_needs_keys": False,
        "note": "공개 시세는 키 없이 조회. 시장가 매수는 금액 기준, 최소 주문 5,000 KRW",
    },
    "binance": {
        "asset": "암호화폐 (ccxt, exchange_id=binance)",
        "env": ["CCXT_API_KEY", "CCXT_SECRET"],
        "symbol": "BTC/USDT",
        "data_needs_keys": False,
        "note": "ccxt 어댑터 별칭. 공개 시세는 키 없이 조회. sandbox=true 면 테스트넷",
    },
    "ccxt": {
        "asset": "암호화폐 (ccxt 범용, broker.exchange_id 로 거래소 선택)",
        "env": ["CCXT_API_KEY", "CCXT_SECRET", "CCXT_PASSWORD(선택)"],
        "symbol": "BTC/USDT",
        "data_needs_keys": False,
        "note": "bybit, okx, bithumb 등 ccxt 지원 거래소. pip install 'tradingbot[ccxt]' 필요",
    },
    "kis": {
        "asset": "국내주식 (한국투자증권)",
        "env": ["KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO"],
        "symbol": "005930",
        "data_needs_keys": True,
        "note": "시세 조회에도 앱키 필요. sandbox=true 면 모의투자 서버. 정규장 09:00~15:30 KST",
    },
    "alpaca": {
        "asset": "미국주식 (Alpaca)",
        "env": ["ALPACA_API_KEY", "ALPACA_SECRET_KEY"],
        "symbol": "AAPL",
        "data_needs_keys": True,
        "note": "시세 조회에도 키 필요. sandbox=true 면 paper-api. 소수점 주식 시장가 가능",
    },
}

_STATE: dict[str, bool] = {"debug": False}
_sleep: Callable[[float], None] = time.sleep
#: 터미널이 아닌 곳(파이프/파일/테스트)으로 출력할 때의 표 너비 (COLUMNS 환경변수가 있으면 그 값)
_FALLBACK_WIDTH = 120


def _console(*, stderr: bool = False) -> Console:
    """rich 콘솔. 터미널이면 실제 너비, 아니면 COLUMNS 또는 120 (표가 80칸에 눌려 깨지지 않게)."""
    stream = sys.stderr if stderr else sys.stdout
    try:
        is_tty = bool(stream.isatty())
    except (AttributeError, ValueError):
        is_tty = False
    width: int | None = None
    if not is_tty:
        try:
            width = int(os.environ.get("COLUMNS", "") or _FALLBACK_WIDTH)
        except ValueError:
            width = _FALLBACK_WIDTH
    # markup=False: 설명 문자열의 "[ccxt]" 같은 대괄호가 rich 태그로 해석되지 않게
    return Console(stderr=stderr, highlight=False, markup=False, width=width)


app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
    # 트레이스백의 지역 변수에는 자격증명 객체가 들어갈 수 있으므로 절대 출력하지 않는다
    pretty_exceptions_show_locals=False,
    help=(
        "주식·코인 자동매매 봇 (Upbit, Binance/ccxt, 한국투자증권 KIS, Alpaca). "
        "백테스트 → 모의투자 → 실거래 순서로 검증하세요. 샘플/데모 시세는 없으며 모든 데이터는 실제 거래소에서 받습니다."
    ),
)


class Source(str, Enum):
    """백테스트 데이터 출처."""

    auto = "auto"
    csv = "csv"
    broker = "broker"
    yfinance = "yfinance"


class DownloadSource(str, Enum):
    broker = "broker"
    yfinance = "yfinance"


# ============================================================================ 공통 헬퍼
def _print(text: str = "") -> None:
    """표준 출력 (rich 의 자동 줄바꿈/마크업 없이 그대로)."""
    print(text)


def _print_err(text: str) -> None:
    print(text, file=sys.stderr)


@contextmanager
def _cli_errors() -> Iterator[None]:
    """봇 예외를 한국어 메시지 + 종료 코드 1 로 바꾼다. ``--debug`` 면 그대로 던져 트레이스백을 보여준다."""
    try:
        yield
    except typer.Exit:
        raise
    except KeyboardInterrupt:
        _print_err("중단되었습니다 (Ctrl+C)")
        raise typer.Exit(code=130) from None
    except TradingBotError as e:
        if _STATE["debug"]:
            raise
        _print_err(f"[오류] {type(e).__name__}: {mask_secrets(str(e))}")
        raise typer.Exit(code=1) from None


def _configure_logging(config: AppConfig, *, with_file: bool) -> None:
    """``run`` 은 설정대로(콘솔 + 파일), 그 외 명령은 콘솔만. ``--debug`` 면 DEBUG 레벨."""
    level = "DEBUG" if _STATE["debug"] else config.logging.level
    setup_logging(LoggingConfig(level=level, file=config.logging.file if with_file else None))


def _load(config_path: Path) -> AppConfig:
    return load_config(config_path)


#: 백테스트 중 bar 마다 INFO 를 찍는 로거 (일자 변경/시작 자산 기록, 모의 주문 접수·체결). 결과 표만 보이도록 잠시 올린다.
BACKTEST_QUIET_LOGGERS: tuple[str, ...] = ("tradingbot.risk.manager", "tradingbot.brokers.paper")


@contextmanager
def _quiet_backtest_logs() -> Iterator[None]:
    """``--debug`` 가 아니면 백테스트 동안 bar 단위 INFO 로그를 WARNING 으로 올린다 (수천 줄 방지)."""
    if _STATE["debug"]:
        yield
        return
    saved = {name: logging.getLogger(name).level for name in BACKTEST_QUIET_LOGGERS}
    for name in BACKTEST_QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        yield
    finally:
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)


def parse_param_value(text: str) -> Any:
    """``--param`` 값 문자열 → bool / int / float / str.

    ``true/false`` (대소문자 무관) → bool, 정수 → int, 실수(지수 표기 포함) → float, 그 외 → 문자열.
    ``nan``/``inf`` 는 파라미터로 의미가 없으므로 문자열로 둔다.
    """
    s = text.strip()
    low = s.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if re.fullmatch(r"[+-]?\d+", s):
        return int(s)
    try:
        f = float(s)
    except ValueError:
        return s
    return f if math.isfinite(f) else s


def parse_params(items: Sequence[str] | None) -> dict[str, Any]:
    """``["fast=5", "slow=20"]`` → ``{"fast": 5, "slow": 20}``. 형식 오류는 ConfigError."""
    out: dict[str, Any] = {}
    for item in items or []:
        if "=" not in item:
            raise ConfigError(f"--param 형식은 key=value 입니다: {item!r}")
        key, _, value = item.partition("=")
        key = key.strip()
        if not key:
            raise ConfigError(f"--param 의 키가 비어 있습니다: {item!r}")
        if key in out:
            logger.warning("--param %s 가 여러 번 지정되어 마지막 값(%s)을 사용합니다", key, value)
        out[key] = parse_param_value(value)
    return out


def resolve_config(
    config: AppConfig,
    *,
    symbols: Sequence[str] | None = None,
    interval: str | None = None,
    strategy: str | None = None,
    params: dict[str, Any] | None = None,
    start: str | None = None,
    end: str | None = None,
    cash: float | None = None,
    fill_on: str | None = None,
) -> AppConfig:
    """CLI 옵션을 설정에 덮어쓴 새 AppConfig (pydantic 재검증).

    ``--strategy`` 가 설정의 전략과 다르면 설정 파일의 params 는 버리고 ``--param`` 만 사용한다
    (다른 전략의 파라미터가 섞이면 '모르는 파라미터' 오류가 나므로).
    """
    data = config.model_dump(mode="python")
    if symbols:
        data["symbols"] = list(symbols)
    if interval is not None:
        data["interval"] = interval
    cli_params = dict(params or {})
    if strategy is not None and strategy.lower() != config.strategy.name.lower():
        data["strategy"] = {"name": strategy, "params": cli_params}
    else:
        data["strategy"] = {
            "name": strategy or config.strategy.name,
            "params": {**config.strategy.params, **cli_params},
        }
    if start is not None:
        data["backtest"]["start"] = start
    if end is not None:
        data["backtest"]["end"] = end
    if cash is not None:
        data["backtest"]["initial_cash"] = cash
    if fill_on is not None:
        data["backtest"]["fill_on"] = fill_on
    try:
        return AppConfig.model_validate(data)
    except ValidationError as e:
        raise ConfigError(f"설정 오류: {e}") from e


def _data_broker_name(config: AppConfig) -> str:
    name = (config.broker.name or "").lower()
    if name == "paper":
        raise ConfigError(
            "broker.name=paper 는 시세 출처가 없습니다. upbit / binance / ccxt / kis / alpaca 중 하나를 지정하세요 "
            "(paper 모드는 `tradingbot run` 의 기본값이며 broker.name 은 시세 브로커여야 합니다)"
        )
    return name


#: 시세 전용으로 쓸 때 sandbox 를 끄는 브로커: ccxt 테스트넷 시세는 희소하고 실제 시장과 다르다
_DATA_SANDBOX_OFF: frozenset[str] = frozenset({"binance", "ccxt"})


def _data_config(config: AppConfig) -> AppConfig:
    """시세 전용(모의투자 data_source / download / backtest) 브로커를 만들 때 쓰는 설정.

    주문을 내지 않는 용도에서는 ccxt 계열의 ``sandbox`` 를 꺼 실제 시장 데이터를 받는다. KIS/Alpaca 의
    sandbox(모의투자 서버)는 실제 시세를 주므로 그대로 둔다. ``--live`` 에는 적용하지 않는다.
    """
    name = (config.broker.name or "").lower()
    if name in _DATA_SANDBOX_OFF and config.broker.sandbox:
        logger.info("%s: 시세 조회 전용이므로 sandbox 를 끄고 실제 시장 데이터를 사용합니다", name)
        return config.model_copy(update={"broker": config.broker.model_copy(update={"sandbox": False})})
    return config


def _asset_class_for(broker_name: str) -> AssetClass:
    """브로커 클래스 속성으로 자산 구분을 얻는다 (인스턴스/네트워크 없이)."""
    return AssetClass(get_broker_class(broker_name).asset_class)


def _quote_currency_for(symbol: str, broker_name: str, config: AppConfig) -> str:
    """심볼 표기로 결제 통화를 추정한다 (브로커 인스턴스/네트워크 없이).

    암호화폐: ``KRW-BTC`` → KRW, ``BTC/USDT[:USDT]`` → USDT. 주식: KIS → KRW, Alpaca → USD.
    그 외는 ``paper.quote_currency``.
    """
    name = broker_name.lower()
    if _asset_class_for(name) == AssetClass.STOCK:
        return {"kis": "KRW", "alpaca": "USD"}.get(name, config.paper.quote_currency)
    if "/" in symbol:
        quote = symbol.split("/", 1)[1].split(":", 1)[0]
        return quote or config.paper.quote_currency
    if "-" in symbol:
        quote = symbol.split("-", 1)[0]
        return quote or config.paper.quote_currency
    return config.paper.quote_currency


def _stock_round_quantity(symbol: str, quantity: float) -> float:
    """백테스트용 주식 수량 반올림: 정수 주 내림 (네트워크 없이 결정적)."""
    if not isinstance(quantity, (int, float)) or not math.isfinite(quantity) or quantity <= 0:
        return 0.0
    return float(math.floor(quantity + 1e-9))


def _broker_has_credentials(broker: BaseBroker) -> bool | None:
    """어댑터가 자격증명을 갖고 있는지. 알 수 없으면 None."""
    for attr in ("has_credentials", "_has_credentials"):
        value = getattr(broker, attr, None)
        if value is None:
            continue
        if callable(value):
            value = value()
        return bool(value)
    return None


def _env_hint(broker_name: str) -> str:
    env = BROKER_INFO.get(broker_name.lower(), {}).get("env") or []
    return ", ".join(env) if env else "(환경변수 없음)"


def _require_data_credentials(broker: BaseBroker, broker_name: str) -> None:
    """시세 조회에도 키가 필요한 브로커(KIS/Alpaca)에 키가 없으면 한국어 오류 + yfinance 안내."""
    info = BROKER_INFO.get(broker_name.lower(), {})
    if not info.get("data_needs_keys"):
        return
    if _broker_has_credentials(broker) is False:
        raise ConfigError(
            f"{broker_name} 는 시세(캔들) 조회에도 API 키가 필요합니다. .env 에 {_env_hint(broker_name)} 를 설정하거나, "
            "키 없이 과거 데이터를 쓰려면 `--source yfinance` 를 사용하세요"
        )


def yfinance_symbol(symbol: str, broker_name: str, config: AppConfig) -> str:
    """브로커 네이티브 심볼 → Yahoo Finance 티커.

    - ``broker.extra.yf_symbols`` 매핑이 있으면 최우선 (예: ``{"BTC/USDT": "BTC-USD"}``)
    - KIS ``005930`` → ``005930.KS`` (``broker.extra.yf_market`` 가 ``KQ`` 면 KOSDAQ)
    - Alpaca ``AAPL`` → 그대로
    - Upbit ``KRW-BTC`` → ``BTC-KRW``, ccxt ``BTC/USDT`` → ``BTC-USDT`` (Yahoo 가 제공하는 쌍인지 확인 필요)
    """
    extra = config.broker.extra or {}
    overrides = extra.get("yf_symbols")
    if isinstance(overrides, dict) and symbol in overrides:
        return str(overrides[symbol])
    name = broker_name.lower()
    if name == "kis":
        return krx_to_yfinance(symbol, str(extra.get("yf_market") or "KS"))
    if name == "alpaca":
        return symbol
    if "/" in symbol:
        base, quote = symbol.split("/", 1)
        return f"{base}-{quote.split(':', 1)[0]}"
    if "-" in symbol and name == "upbit":
        quote, base = symbol.split("-", 1)
        return f"{base}-{quote}"
    return symbol


def _fmt_money(x: float | None) -> str:
    if x is None or not isinstance(x, (int, float)) or not math.isfinite(x):
        return "-"
    return f"{x:,.0f}" if abs(x) >= 1000 else f"{x:,.4g}"


def _fmt_qty(q: float | None) -> str:
    if q is None or not isinstance(q, (int, float)) or not math.isfinite(q):
        return "-"
    return f"{q:.8g}"


def _fmt_ts(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S%z")
    return str(value)


def _fmt_pct(x: float | None) -> str:
    if x is None or not isinstance(x, (int, float)) or not math.isfinite(x):
        return "-"
    return f"{x * 100:+.2f}%"


def _step(interval: str) -> timedelta:
    return timedelta(seconds=interval_to_seconds(interval))


# ============================================================================ 전역 옵션
def _version_callback(value: bool) -> None:
    if value:
        _print(f"tradingbot {__version__}")
        raise typer.Exit()


@app.callback()
def _main_callback(
    debug: bool = typer.Option(False, "--debug", help="오류 시 트레이스백 출력 + DEBUG 로그"),
    version: bool = typer.Option(
        False, "--version", "-V", help="버전 출력", callback=_version_callback, is_eager=True
    ),
) -> None:
    """주식·코인 자동매매 봇."""
    _STATE["debug"] = debug


CONFIG_OPTION = typer.Option(
    DEFAULT_CONFIG_PATH, "--config", "-c", help="설정 YAML 경로 (기본 config/config.yaml)"
)


# ============================================================================ init
def _find_template(rel: str) -> Path:
    for base in (Path.cwd(), PACKAGE_ROOT):
        candidate = base / rel
        if candidate.is_file():
            return candidate
    raise ConfigError(f"예시 파일을 찾을 수 없습니다: {rel} (저장소 루트에서 실행하세요)")


@app.command()
def init(
    force: bool = typer.Option(False, "--force", help="이미 있는 파일도 덮어쓴다"),
    target: Path = typer.Option(Path("."), "--dir", help="생성 위치 (기본 현재 디렉터리)"),
) -> None:
    """예시 설정을 복사해 config/config.yaml 과 .env 를 만든다 (있으면 건너뜀, --force 로 덮어씀)."""
    with _cli_errors():
        created: list[Path] = []
        skipped: list[Path] = []
        for src_rel, dst_rel in _TEMPLATES:
            src = _find_template(src_rel)
            dst = target / dst_rel
            if dst.exists() and not force:
                skipped.append(dst)
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            created.append(dst)
        for p in created:
            _print(f"생성: {p}")
        for p in skipped:
            _print(f"건너뜀 (이미 있음, --force 로 덮어쓰기): {p}")
        _print()
        _print("다음 단계:")
        _print(
            "  1. .env 에 사용할 거래소의 API 키를 입력하세요 (모의투자/백테스트는 키 없이도 Upbit/Binance 시세 사용 가능)"
        )
        _print("  2. config/config.yaml 의 broker / symbols / strategy / risk 를 조정하세요")
        _print("  3. tradingbot validate-config -c config/config.yaml")
        _print(
            "  4. tradingbot download -c config/config.yaml --start 2024-01-01 && tradingbot backtest -c config/config.yaml"
        )
        _print("  5. tradingbot run -c config/config.yaml   (모의투자)")


# ============================================================================ validate-config
_SYMBOL_PATTERNS: dict[str, tuple[str, str]] = {
    "upbit": (r"^[A-Z]{2,10}-[A-Z0-9]{2,20}$", "KRW-BTC"),
    "binance": (r"^[A-Z0-9]{2,20}/[A-Z0-9]{2,20}(:[A-Z0-9]{2,20})?$", "BTC/USDT"),
    "ccxt": (r"^[A-Z0-9]{2,20}/[A-Z0-9]{2,20}(:[A-Z0-9]{2,20})?$", "BTC/USDT"),
    "kis": (r"^[0-9A-Z]{6}$", "005930"),
    "alpaca": (r"^[A-Z][A-Z0-9.\-]{0,9}$", "AAPL"),
}


def _validate(config: AppConfig) -> tuple[list[str], list[str]]:
    """(오류 목록, 경고 목록)."""
    errors: list[str] = []
    warnings: list[str] = []
    name = (config.broker.name or "").lower()

    try:
        broker_cls = get_broker_class(name)
    except ConfigError as e:
        errors.append(str(e))
        broker_cls = None

    try:
        strategy = create_strategy(config.strategy.name, config.strategy.params)
    except ConfigError as e:
        errors.append(str(e))
        strategy = None

    if broker_cls is not None:
        supported = tuple(getattr(broker_cls, "supported_intervals", ()) or ())
        if supported and config.interval not in supported:
            errors.append(
                f"브로커 {name} 는 interval {config.interval!r} 를 지원하지 않습니다 (가능: {', '.join(supported)})"
            )
        pattern = _SYMBOL_PATTERNS.get(name)
        if pattern:
            regex, example = pattern
            for sym in config.symbols:
                if not re.fullmatch(regex, sym):
                    warnings.append(f"심볼 {sym!r} 이 {name} 표기(예: {example})와 다릅니다")
        if name == "paper":
            warnings.append("broker.name=paper 는 시세 출처가 없어 run/backtest/download 에 쓸 수 없습니다")
        if AssetClass(broker_cls.asset_class) == AssetClass.CRYPTO and name != "paper":
            for sym in config.symbols:
                q = _quote_currency_for(sym, name, config)
                if q != config.paper.quote_currency:
                    warnings.append(
                        f"심볼 {sym} 의 결제통화 {q} 와 paper.quote_currency={config.paper.quote_currency} 가 다릅니다 "
                        "(모의투자는 시세 브로커의 결제통화를 따릅니다)"
                    )
                    break

    if strategy is not None and config.engine.candle_limit < strategy.warmup:
        errors.append(
            f"engine.candle_limit({config.engine.candle_limit}) 가 전략 {strategy.name} 의 warmup({strategy.warmup}) "
            "보다 작습니다"
        )

    for label, value in (("backtest.start", config.backtest.start), ("backtest.end", config.backtest.end)):
        if value is not None:
            try:
                to_utc_datetime(value, name=label)
            except DataError as e:
                errors.append(str(e))

    if config.is_live:
        warnings.append("mode=live: 실거래 설정입니다. 반드시 모의투자(paper) 로 먼저 검증하세요")
    if not config.broker.sandbox and name in ("kis", "alpaca", "binance", "ccxt"):
        warnings.append("broker.sandbox=false: 실전 서버를 사용합니다")

    creds = Credentials.from_env()
    env = BROKER_INFO.get(name, {}).get("env") or []
    missing_env = [e for e in env if "선택" not in e and getattr(creds, _cred_field(e), None) in (None, "")]
    if env and missing_env:
        level = (
            "실거래/잔고 조회"
            if not BROKER_INFO[name].get("data_needs_keys")
            else "시세 조회를 포함한 모든 호출"
        )
        warnings.append(f"환경변수 {', '.join(missing_env)} 가 비어 있습니다 ({level}에 필요)")

    notify = config.notify
    if notify.telegram.enabled and not (creds.telegram_bot_token and creds.telegram_chat_id):
        warnings.append("notify.telegram 이 켜져 있지만 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 없습니다")
    if notify.slack.enabled and not creds.slack_webhook_url:
        warnings.append("notify.slack 이 켜져 있지만 SLACK_WEBHOOK_URL 이 없습니다")
    if notify.discord.enabled and not creds.discord_webhook_url:
        warnings.append("notify.discord 가 켜져 있지만 DISCORD_WEBHOOK_URL 이 없습니다")
    return errors, warnings


def _cred_field(env_name: str) -> str:
    """환경변수 이름 → Credentials 필드 이름 (별칭은 대표 이름으로)."""
    key = env_name.split("(", 1)[0].strip().lower()
    return {"ccxt_secret": "ccxt_secret"}.get(key, key)


def _config_table(config: AppConfig, path: Path) -> Table:
    strategy_params = ", ".join(f"{k}={v}" for k, v in config.strategy.params.items()) or "(기본값)"
    risk = config.risk
    table = Table(title=f"설정 요약: {path}", show_header=False, box=None, pad_edge=False)
    table.add_column("항목", style="bold")
    table.add_column("값")
    table.add_row("모드", config.mode)
    table.add_row(
        "브로커",
        f"{config.broker.name} (sandbox={config.broker.sandbox}, exchange_id={config.broker.exchange_id or '-'})",
    )
    table.add_row("심볼", ", ".join(config.symbols))
    table.add_row("간격", config.interval)
    table.add_row("전략", f"{config.strategy.name} ({strategy_params})")
    table.add_row(
        "리스크",
        f"종목당 {risk.max_position_pct * 100:g}%, 최대 {risk.max_positions}종목, 손절 {_opt_pct(risk.stop_loss_pct)}, "
        f"익절 {_opt_pct(risk.take_profit_pct)}, 추적손절 {_opt_pct(risk.trailing_stop_pct)}, "
        f"일일 손실 한도 {_opt_pct(risk.max_daily_loss_pct)}, 최소 주문 {risk.min_order_value:g}",
    )
    table.add_row(
        "모의투자",
        f"초기 현금 {config.paper.initial_cash:,.0f} {config.paper.quote_currency}, "
        f"수수료 {config.paper.fee_pct * 100:g}%, 슬리피지 {config.paper.slippage_pct * 100:g}%",
    )
    table.add_row(
        "엔진",
        f"폴링 {config.engine.poll_seconds:g}초, 캔들 {config.engine.candle_limit}개, 상태 파일 {config.engine.state_file}",
    )
    table.add_row(
        "백테스트",
        f"{config.backtest.start or '(처음부터)'} ~ {config.backtest.end or '(최신)'}, 데이터 {config.backtest.data_dir}, "
        f"초기 자금 {config.backtest.initial_cash:,.0f}, fill_on={config.backtest.fill_on}",
    )
    table.add_row("로깅", f"{config.logging.level}, 파일 {config.logging.file or '(콘솔만)'}")
    return table


def _opt_pct(v: float | None) -> str:
    return "없음" if v is None else f"{v * 100:g}%"


@app.command("validate-config")
def validate_config(config_path: Path = CONFIG_OPTION) -> None:
    """설정 파일을 읽고 브로커/전략/간격/리스크 값을 검증한다."""
    with _cli_errors():
        config = _load(config_path)
        errors, warnings = _validate(config)
        _console().print(_config_table(config, config_path))
        _print()
        for w in warnings:
            _print(f"[경고] {w}")
        for e in errors:
            _print_err(f"[오류] {e}")
        if errors:
            _print_err(f"설정 오류 {len(errors)}건")
            raise typer.Exit(code=1)
        _print(f"설정 OK: {config_path} (경고 {len(warnings)}건)")


# ============================================================================ strategies / brokers
@app.command()
def strategies() -> None:
    """사용 가능한 전략과 기본 파라미터를 표로 보여준다."""
    with _cli_errors():
        table = Table(title="전략 목록")
        table.add_column("이름", style="bold", no_wrap=True)
        table.add_column("설명", ratio=3, min_width=20)
        table.add_column("기본 파라미터", ratio=2, min_width=14)
        table.add_column("워밍업", justify="right", no_wrap=True)
        for name in available_strategies():
            cls = get_strategy_class(name)
            params = ", ".join(f"{k}={v}" for k, v in cls.default_params.items()) or "-"
            try:
                warmup = str(cls().warmup)
            except Exception as e:  # noqa: BLE001 - 목록 표시는 생성 실패해도 계속
                warmup = f"? ({e})"
            table.add_row(name, cls.description or "-", params, warmup)
        _console().print(table)
        _print(
            "설정 예: strategy: {name: sma_cross, params: {fast: 10, slow: 30}}  /  backtest --param fast=5"
        )


@app.command()
def brokers() -> None:
    """사용 가능한 브로커 어댑터와 필요한 환경변수를 표로 보여준다."""
    with _cli_errors():
        table = Table(title="브로커 목록")
        table.add_column("이름", style="bold", no_wrap=True)
        table.add_column("자산", min_width=8)
        table.add_column("심볼 예", min_width=7)
        table.add_column("환경변수 (.env)", min_width=14)
        table.add_column("지원 간격", min_width=8)
        table.add_column("비고", ratio=2, min_width=16)
        for name in available_brokers():
            info = BROKER_INFO.get(name, {})
            try:
                cls = get_broker_class(name)
                supported = tuple(getattr(cls, "supported_intervals", ()) or ())
                intervals = ", ".join(supported) if supported else "(전체)"
            except Exception as e:  # noqa: BLE001
                intervals = f"? ({e})"
            table.add_row(
                name,
                str(info.get("asset", "-")),
                str(info.get("symbol", "-")),
                ", ".join(info.get("env") or []) or "-",
                intervals,
                str(info.get("note", "")),
            )
        _console().print(table)
        _print(
            "키는 .env 에만 두세요 (tradingbot init 으로 .env.example 복사). 공개 시세는 Upbit/ccxt 에서 키 없이 조회됩니다."
        )


# ============================================================================ 데이터 로딩 (download / backtest 공용)
class _LazyBroker:
    """필요할 때 한 번만 시세 브로커를 만든다 (CSV 만 쓰는 백테스트는 네트워크 어댑터를 만들지 않음)."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.name = _data_broker_name(config)
        self._broker: BaseBroker | None = None

    def get(self) -> BaseBroker:
        if self._broker is None:
            self._broker = create_broker(self.name, _data_config(self.config))
        return self._broker

    def needs_yfinance(self) -> bool:
        """시세 조회에 키가 필요한 브로커인데 키가 없으면 True."""
        if not BROKER_INFO.get(self.name, {}).get("data_needs_keys"):
            return False
        return _broker_has_credentials(self.get()) is False

    def close(self) -> None:
        if self._broker is not None:
            try:
                self._broker.close()
            except Exception as e:  # noqa: BLE001
                logger.debug("브로커 종료 중 오류 (무시): %s", mask_secrets(str(e)))
            self._broker = None


def _default_start(interval: str) -> datetime:
    return utcnow() - timedelta(days=DEFAULT_DOWNLOAD_DAYS) - _step(interval)


def _csv_dirs(broker_name: str) -> tuple[str, ...]:
    return (broker_name, YFINANCE_DIR)


def _csv_coverage(
    store: CandleStore,
    broker_name: str,
    symbol: str,
    interval: str,
    start: datetime | None,
    end: datetime | None,
) -> str | None:
    """요청 구간을 덮는 CSV 가 있으면 그 디렉터리 이름(브로커 또는 yfinance), 없으면 None."""
    step = _step(interval)
    end_ref = end or utcnow()
    for d in _csv_dirs(broker_name):
        if not store.exists(d, symbol, interval):
            continue
        rng = store.date_range(d, symbol, interval)
        if rng is None:
            continue
        first, last = rng
        start_ok = start is None or first <= start + step
        end_ok = last >= end_ref - 2 * step
        if start_ok and end_ok:
            return d
    return None


def _load_csv(
    store: CandleStore,
    broker_name: str,
    symbol: str,
    interval: str,
    start: datetime | None,
    end: datetime | None,
) -> tuple[pd.DataFrame, str]:
    for d in _csv_dirs(broker_name):
        if not store.exists(d, symbol, interval):
            continue
        df = store.load(d, symbol, interval, start, end)
        if df.empty:
            rng = store.date_range(d, symbol, interval)
            span = f"{_fmt_ts(rng[0])} ~ {_fmt_ts(rng[1])}" if rng else "(비어 있음)"
            raise DataError(
                f"{symbol} {interval}: 저장된 데이터({store.path(d, symbol, interval)}, {span}) 에 요청 구간 "
                f"{_fmt_ts(start) if start else '처음'} ~ {_fmt_ts(end) if end else '최신'} 의 캔들이 없습니다. "
                "`tradingbot download` 로 구간을 내려받거나 --source broker 를 사용하세요"
            )
        step = _step(interval)
        first = df["timestamp"].iloc[0].to_pydatetime()
        last = df["timestamp"].iloc[-1].to_pydatetime()
        if start is not None and first > start + step:
            _print_err(
                f"[경고] {symbol} {interval}: 저장 데이터가 {_fmt_ts(first)} 부터 시작합니다 (요청 {_fmt_ts(start)})"
            )
        if end is not None and last < end - 2 * step:
            _print_err(
                f"[경고] {symbol} {interval}: 저장 데이터가 {_fmt_ts(last)} 에서 끝납니다 (요청 {_fmt_ts(end)})"
            )
        return df, d
    raise DataError(
        f"{symbol} {interval}: 저장된 데이터가 없습니다 ({store.path(broker_name, symbol, interval)}). "
        f"`tradingbot download -c <설정> --symbol {symbol}` 로 먼저 내려받거나 --source broker 를 사용하세요"
    )


def _download_with_progress(
    store: CandleStore,
    broker: BaseBroker,
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime | None,
    *,
    batch: int = 200,
    sleep: float = 0.1,
) -> pd.DataFrame:
    label = f"{broker.name} {symbol} {interval} 다운로드"
    with Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        TimeElapsedColumn(),
        console=_console(stderr=True),
        transient=True,
    ) as progress:
        task = progress.add_task(label, total=None)

        def on_page(count: int, oldest: datetime) -> None:
            progress.update(task, description=f"{label}: {count:,}개 (가장 오래된 {oldest:%Y-%m-%d %H:%M}Z)")

        df = store.download(broker, symbol, interval, start, end, batch=batch, sleep=sleep, progress=on_page)
    if df.empty:
        raise DataError(
            f"{broker.name} 에서 {symbol} {interval} {_fmt_ts(start)} ~ {_fmt_ts(end) if end else '최신'} 구간의 "
            "캔들을 받지 못했습니다 (심볼/간격/기간을 확인하세요)"
        )
    return df


def _load_yfinance_to_store(
    store: CandleStore,
    config: AppConfig,
    broker_name: str,
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime | None,
) -> tuple[pd.DataFrame, str]:
    yf_sym = yfinance_symbol(symbol, broker_name, config)
    _print_err(f"yfinance 에서 {symbol} → {yf_sym} ({interval}) 내려받는 중 ...")
    df = load_yfinance(yf_sym, interval, start, end)
    store.save(YFINANCE_DIR, symbol, interval, df)
    return df, yf_sym


def _load_backtest_data(
    config: AppConfig,
    store: CandleStore,
    source: Source,
    start: datetime | None,
    end: datetime | None,
) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """심볼별 캔들 DataFrame 과 출처 설명. 데이터가 없으면 실제 거래소/yfinance 에서 받아 캐시한다."""
    lazy = _LazyBroker(config)
    broker_name = lazy.name
    interval = config.interval
    data: dict[str, pd.DataFrame] = {}
    used: dict[str, str] = {}
    try:
        for sym in config.symbols:
            chosen = source
            if source == Source.auto:
                covered = _csv_coverage(store, broker_name, sym, interval, start, end)
                if covered is not None:
                    chosen = Source.csv
                elif lazy.needs_yfinance():
                    _print_err(
                        f"[안내] {broker_name} 시세 조회에 필요한 API 키가 없어 {sym} 은 yfinance 에서 내려받습니다"
                    )
                    chosen = Source.yfinance
                else:
                    chosen = Source.broker
            if chosen == Source.csv:
                df, where = _load_csv(store, broker_name, sym, interval, start, end)
                used[sym] = f"csv ({store.path(where, sym, interval)})"
            elif chosen == Source.broker:
                broker = lazy.get()
                _require_data_credentials(broker, broker_name)
                dl_start = start or _default_start(interval)
                df = _download_with_progress(store, broker, sym, interval, dl_start, end)
                used[sym] = f"{broker_name} API → {store.path(broker_name, sym, interval)}"
            else:
                dl_start = start or _default_start(interval)
                df, yf_sym = _load_yfinance_to_store(store, config, broker_name, sym, interval, dl_start, end)
                used[sym] = f"yfinance {yf_sym} → {store.path(YFINANCE_DIR, sym, interval)}"
            data[sym] = df
    finally:
        lazy.close()
    return data, used


# ============================================================================ download
@app.command()
def download(
    config_path: Path = CONFIG_OPTION,
    symbols: list[str] | None = typer.Option(
        None, "--symbol", "-s", help="심볼 (여러 번 지정 가능, 기본 설정값)"
    ),
    start: str | None = typer.Option(None, "--start", help="시작일 YYYY-MM-DD (기본 365일 전)"),
    end: str | None = typer.Option(None, "--end", help="종료일 YYYY-MM-DD (기본 최신)"),
    interval: str | None = typer.Option(None, "--interval", help="캔들 간격 (기본 설정값)"),
    source: DownloadSource = typer.Option(DownloadSource.broker, "--source", help="broker | yfinance"),
    batch: int = typer.Option(200, "--batch", min=1, help="브로커 요청당 캔들 수"),
    sleep: float = typer.Option(0.1, "--sleep", min=0.0, help="요청 사이 대기(초)"),
) -> None:
    """실제 거래소(또는 yfinance) 캔들을 내려받아 backtest.data_dir 에 CSV 로 저장한다."""
    with _cli_errors():
        config = resolve_config(_load(config_path), symbols=symbols, interval=interval)
        _configure_logging(config, with_file=False)
        broker_name = _data_broker_name(config)
        start_dt = to_utc_datetime(start, name="--start") or _default_start(config.interval)
        end_dt = to_utc_datetime(end, name="--end")
        if end_dt is not None and start_dt > end_dt:
            raise DataError(f"--start({_fmt_ts(start_dt)}) 가 --end({_fmt_ts(end_dt)}) 보다 늦습니다")
        store = CandleStore(config.backtest.data_dir)
        rows: list[tuple[str, str, int, str, str, str]] = []
        if source == DownloadSource.broker:
            broker = create_broker(broker_name, _data_config(config))
            try:
                _require_data_credentials(broker, broker_name)
                for sym in config.symbols:
                    df = _download_with_progress(
                        store, broker, sym, config.interval, start_dt, end_dt, batch=batch, sleep=sleep
                    )
                    rows.append(_download_row(store, broker_name, sym, config.interval, df))
            finally:
                broker.close()
        else:
            for sym in config.symbols:
                df, yf_sym = _load_yfinance_to_store(
                    store, config, broker_name, sym, config.interval, start_dt, end_dt
                )
                rows.append(
                    _download_row(store, YFINANCE_DIR, sym, config.interval, df, label=f"yfinance {yf_sym}")
                )
        table = Table(title="다운로드 결과")
        for col in ("심볼", "출처", "캔들 수", "처음", "마지막", "파일"):
            table.add_column(col, no_wrap=col != "파일", justify="right" if col == "캔들 수" else "left")
        for row in rows:
            table.add_row(row[0], row[1], f"{row[2]:,}", row[3], row[4], row[5])
        _console().print(table)
        _print(f"완료: {len(rows)}개 심볼, 저장 위치 {store.data_dir}")


def _download_row(
    store: CandleStore, where: str, symbol: str, interval: str, df: pd.DataFrame, *, label: str | None = None
) -> tuple[str, str, int, str, str, str]:
    first = df["timestamp"].iloc[0].to_pydatetime()
    last = df["timestamp"].iloc[-1].to_pydatetime()
    return (
        symbol,
        label or where,
        len(df),
        _fmt_ts(first),
        _fmt_ts(last),
        str(store.path(where, symbol, interval)),
    )


# ============================================================================ backtest
def write_report(result: BacktestResult, report_dir: Path) -> dict[str, Path]:
    """결과를 ``report_dir`` 에 저장: JSON(to_dict), 자산곡선 CSV, 요약 텍스트. 경로 dict 반환."""
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    symbols = "+".join(symbol_safe(s) for s in result.symbols)
    base = f"{result.strategy}_{symbols}_{result.interval}_{stamp}"
    json_path = report_dir / f"{base}.json"
    equity_path = report_dir / f"{base}_equity.csv"
    summary_path = report_dir / f"{base}_summary.txt"

    json_path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    index = pd.DatetimeIndex(result.equity_curve.index)
    if index.tz is None:
        index = index.tz_localize("UTC")
    pd.DataFrame(
        {
            "timestamp": index.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "equity": result.equity_curve.to_numpy(dtype=float),
        }
    ).to_csv(equity_path, index=False, lineterminator="\n")
    summary_path.write_text(result.summary() + "\n", encoding="utf-8")
    return {"json": json_path, "equity_csv": equity_path, "summary": summary_path}


def _metrics_table(metrics: dict[str, Any]) -> Table:
    table = Table(title="성과 지표", show_header=False, box=None, pad_edge=False)
    table.add_column("지표", style="bold", no_wrap=True)
    table.add_column("값", justify="right", no_wrap=True)
    for line in format_metrics(metrics).splitlines():
        label, sep, value = line.partition(" : ")
        if sep:
            table.add_row(label.rstrip(), value.strip())
        else:
            table.add_row(line, "")
    return table


def _trades_table(result: BacktestResult, limit: int) -> Table:
    trades = result.trades[-limit:] if limit > 0 else []
    title = f"거래 내역 (최근 {len(trades)}건 / 총 {len(result.trades)}건)"
    table = Table(title=title)
    for col, justify in (
        ("심볼", "left"),
        ("진입", "left"),
        ("청산", "left"),
        ("수량", "right"),
        ("진입가", "right"),
        ("청산가", "right"),
        ("손익", "right"),
        ("손익률", "right"),
        ("사유", "left"),
    ):
        table.add_column(col, justify=justify, no_wrap=col != "사유")
    for t in trades:
        table.add_row(
            t.symbol,
            t.entry_time.strftime("%Y-%m-%d %H:%M"),
            t.exit_time.strftime("%Y-%m-%d %H:%M"),
            _fmt_qty(t.quantity),
            _fmt_money(t.entry_price),
            _fmt_money(t.exit_price),
            f"{t.pnl:+,.0f}" if abs(t.pnl) >= 1000 else f"{t.pnl:+,.2f}",
            _fmt_pct(t.pnl_pct),
            t.reason or "-",
        )
    return table


def _print_backtest(result: BacktestResult, sources: dict[str, str], trades_shown: int) -> None:
    summary = result.summary()
    head, _, _ = summary.partition("--- 성과 지표 ---")
    _print(head.rstrip())
    for sym, src in sources.items():
        _print(f"데이터 출처 : {sym} ← {src}")
    _print()
    _console().print(_metrics_table(result.metrics))
    _print()
    if result.trades:
        _console().print(_trades_table(result, trades_shown))
    else:
        _print("거래 내역: 없음 (신호가 없었거나 모두 스킵됨)")


@app.command()
def backtest(
    config_path: Path = CONFIG_OPTION,
    symbols: list[str] | None = typer.Option(
        None, "--symbol", "-s", help="심볼 (여러 번 지정 가능, 기본 설정값)"
    ),
    strategy: str | None = typer.Option(None, "--strategy", help="전략 이름 (기본 설정값)"),
    params: list[str] | None = typer.Option(
        None, "--param", "-p", help="전략 파라미터 key=value (여러 번 지정 가능, 예: -p fast=5 -p slow=20)"
    ),
    start: str | None = typer.Option(None, "--start", help="시작일 YYYY-MM-DD (기본 backtest.start)"),
    end: str | None = typer.Option(None, "--end", help="종료일 YYYY-MM-DD (기본 backtest.end)"),
    interval: str | None = typer.Option(None, "--interval", help="캔들 간격 (기본 설정값)"),
    source: Source = typer.Option(
        Source.auto,
        "--source",
        help="데이터 출처: auto(저장된 CSV 가 구간을 덮으면 csv, 아니면 broker; 키 없는 주식 브로커는 yfinance) | csv | broker | yfinance",
    ),
    cash: float | None = typer.Option(None, "--cash", min=0.0, help="초기 자금 (기본 backtest.initial_cash)"),
    fill_on: str | None = typer.Option(None, "--fill-on", help="시장가 체결 시점: next_open | close"),
    report: bool = typer.Option(False, "--report", help="JSON + 자산곡선 CSV + 요약을 report_dir 에 저장"),
    report_dir: Path | None = typer.Option(
        None, "--report-dir", help="리포트 저장 위치 (기본 backtest.report_dir)"
    ),
    trades_shown: int = typer.Option(20, "--trades", min=0, help="표시할 최근 거래 수"),
) -> None:
    """실제 거래소 캔들로 백테스트를 실행한다. 데이터가 없으면 브로커 공개 API / yfinance 에서 내려받아 캐시한다."""
    with _cli_errors():
        if fill_on is not None and fill_on not in FILL_ON_CHOICES:
            raise ConfigError(f"--fill-on 은 {', '.join(FILL_ON_CHOICES)} 중 하나여야 합니다: {fill_on!r}")
        config = resolve_config(
            _load(config_path),
            symbols=symbols,
            interval=interval,
            strategy=strategy,
            params=parse_params(params),
            start=start,
            end=end,
            cash=cash,
            fill_on=fill_on,
        )
        _configure_logging(config, with_file=False)
        strat = create_strategy(config.strategy.name, config.strategy.params)
        broker_name = _data_broker_name(config)
        start_dt = to_utc_datetime(config.backtest.start, name="--start")
        end_dt = to_utc_datetime(config.backtest.end, name="--end")
        if start_dt is not None and end_dt is not None and start_dt > end_dt:
            raise DataError(f"--start({_fmt_ts(start_dt)}) 가 --end({_fmt_ts(end_dt)}) 보다 늦습니다")

        store = CandleStore(config.backtest.data_dir)
        data, sources = _load_backtest_data(config, store, source, start_dt, end_dt)

        asset_class = _asset_class_for(broker_name)
        quote = _quote_currency_for(config.symbols[0], broker_name, config)
        bt = Backtester(
            strat,
            RiskManager(config.risk),
            initial_cash=config.backtest.initial_cash,
            fee_pct=config.backtest.fee_pct,
            slippage_pct=config.backtest.slippage_pct,
            fill_on=config.backtest.fill_on,
            quote_currency=quote,
            interval=config.interval,
            asset_class=asset_class,
            min_order_value=config.risk.min_order_value,
            round_quantity=_stock_round_quantity if asset_class == AssetClass.STOCK else None,
        )
        with _quiet_backtest_logs():
            result = bt.run(data)
        _print_backtest(result, sources, trades_shown)
        if report:
            paths = write_report(result, report_dir or Path(config.backtest.report_dir))
            _print()
            _print(f"리포트 저장: JSON {paths['json']}")
            _print(f"             자산곡선 CSV {paths['equity_csv']}")
            _print(f"             요약 {paths['summary']}")


# ============================================================================ run
def _live_banner(config: AppConfig, broker: BaseBroker) -> None:
    risk = config.risk
    body = "\n".join(
        [
            "실제 자금으로 주문이 전송됩니다. 손실은 전적으로 사용자 책임입니다.",
            "",
            f"브로커   : {broker.name} (sandbox={config.broker.sandbox})",
            f"심볼     : {', '.join(config.symbols)}",
            f"간격     : {config.interval} (폴링 {config.engine.poll_seconds:g}초)",
            f"전략     : {config.strategy.name} {config.strategy.params}",
            f"리스크   : 종목당 {risk.max_position_pct * 100:g}%, 최대 {risk.max_positions}종목, "
            f"손절 {_opt_pct(risk.stop_loss_pct)}, 익절 {_opt_pct(risk.take_profit_pct)}, "
            f"추적손절 {_opt_pct(risk.trailing_stop_pct)}, 일일 손실 한도 {_opt_pct(risk.max_daily_loss_pct)}",
            f"운용 한도: {risk.capital_limit:,.0f}" if risk.capital_limit else "운용 한도: 총자산 전체",
        ]
    )
    _console(stderr=True).print(
        Panel(body, title="!!! 실거래 (LIVE) 모드 !!!", border_style="red", expand=False)
    )


def _countdown(seconds: int) -> None:
    """Ctrl+C 로 취소할 수 있는 카운트다운."""
    _print_err(f"{seconds}초 후 실거래를 시작합니다. 취소하려면 Ctrl+C 를 누르세요 (--yes 로 생략 가능)")
    for remaining in range(seconds, 0, -1):
        _print_err(f"  {remaining} ...")
        _sleep(1.0)
    _print_err("실거래 시작")


def _print_run_status(status: dict[str, Any]) -> None:
    quote = status.get("quote_currency") or ""
    _print("=== 사이클 결과 ===")
    _print(
        f"모드/브로커 : {status.get('mode')} / {status.get('broker')} (시세: {status.get('data_source') or '-'})"
    )
    _print(f"전략        : {status.get('strategy')} {status.get('params')}")
    _print(f"사이클      : {status.get('cycles')}회, 연속 실패 {status.get('consecutive_failures')}회")
    _print(f"자산 / 현금 : {_fmt_money(status.get('equity'))} / {_fmt_money(status.get('cash'))} {quote}")
    _print(
        f"당일 손익   : {_fmt_money(status.get('daily_pnl'))} {quote} ({status.get('daily_trades')}건, "
        f"시작 자산 {_fmt_money(status.get('day_start_equity'))})"
    )
    last_ts = status.get("last_candle_ts") or {}
    prices = status.get("last_prices") or {}
    for sym in status.get("symbols") or []:
        _print(f"{sym:<12}: 마지막 캔들 {last_ts.get(sym) or '-'}, 현재가 {_fmt_money(prices.get(sym))}")
    positions = status.get("positions") or {}
    if positions:
        table = Table(title="보유 포지션")
        for col in ("심볼", "수량", "평균단가", "현재가", "평가손익", "손절", "익절", "진입 사유"):
            table.add_column(
                col,
                justify="right" if col not in ("심볼", "진입 사유") else "left",
                no_wrap=col != "진입 사유",
            )
        for sym, p in positions.items():
            table.add_row(
                sym,
                _fmt_qty(p.get("quantity")),
                _fmt_money(p.get("average_price")),
                _fmt_money(p.get("last_price")),
                f"{_fmt_money(p.get('unrealized_pnl'))} ({_fmt_pct(p.get('unrealized_pnl_pct'))})",
                _fmt_money(p.get("stop_loss")),
                _fmt_money(p.get("take_profit")),
                str(p.get("entry_reason") or "-"),
            )
        _console().print(table)
    else:
        _print("보유 포지션 : 없음")
    pending = status.get("pending_breakouts") or {}
    for sym, pb in pending.items():
        _print(f"돌파 대기   : {sym} 트리거 {_fmt_money(pb.get('trigger'))} (만료 {pb.get('expires')})")
    _print(f"누적 거래   : {status.get('trades')}건")


@app.command()
def run(
    config_path: Path = CONFIG_OPTION,
    live: bool = typer.Option(False, "--live", help="실거래 (설정 mode 가 live 여야 함)"),
    once: bool = typer.Option(False, "--once", help="폴링 사이클 1회만 실행하고 종료"),
    yes: bool = typer.Option(False, "--yes", "-y", help="실거래 시작 전 카운트다운 생략"),
) -> None:
    """매매 엔진을 실행한다. 기본은 모의투자(paper); 실거래는 --live 와 mode: live 둘 다 필요하다."""
    with _cli_errors():
        config = _load(config_path)
        _configure_logging(config, with_file=True)
        if live and not config.is_live:
            raise ConfigError(
                f"--live 는 설정 파일의 mode 가 live 일 때만 허용됩니다 (현재 mode={config.mode}). "
                "실거래를 원하면 config 의 mode: live 로 바꾸고, 모의투자는 --live 없이 실행하세요"
            )
        if config.is_live and not live:
            raise ConfigError(
                "설정 mode=live 인데 --live 플래그가 없습니다. 실거래는 `tradingbot run --live` 로만 시작되며, "
                "모의투자로 실행하려면 설정의 mode 를 paper 로 바꾸세요"
            )
        strategy = create_strategy(config.strategy.name, config.strategy.params)
        risk = RiskManager(config.risk)
        notifier = create_notifier(config.notify, Credentials.from_env())
        state = StateStore(config.engine.state_file)

        data_broker: BaseBroker | None = None
        if config.is_live:
            broker: BaseBroker = create_broker(config.broker.name, config)
            _live_banner(config, broker)
            if not yes:
                _countdown(LIVE_COUNTDOWN_SEC)
        else:
            name = _data_broker_name(config)
            data_broker = create_broker(name, _data_config(config))
            broker = PaperBroker.from_config(config, data_source=data_broker)
            _print_err(
                f"모의투자(paper): {name} 실시간 시세로 모의 체결합니다. 실제 주문은 나가지 않습니다 "
                f"(초기 현금 {config.paper.initial_cash:,.0f} {broker.quote_currency(config.symbols[0])})"
            )

        trader = Trader(config, broker, strategy, risk, notifier, state)
        try:
            if once:
                trader.run_once()
                _print_run_status(trader.status())
                _print(f"상태 파일: {state.path}")
            else:
                _print_err(
                    f"엔진 시작: {config.interval} 캔들, {config.engine.poll_seconds:g}초 폴링. 종료하려면 Ctrl+C"
                )
                trader.run_forever()
                _print_err("엔진 종료")
        finally:
            for b in (trader.broker, data_broker):
                if b is not None:
                    try:
                        b.close()
                    except Exception as e:  # noqa: BLE001
                        logger.debug("브로커 종료 중 오류 (무시): %s", mask_secrets(str(e)))


# ============================================================================ balance
def _print_account(broker: BaseBroker, symbols: list[str], *, title: str) -> None:
    balances = broker.get_balances()
    positions = broker.get_positions()
    _print(f"=== {title} ===")
    if balances:
        table = Table(title="잔고")
        for col in ("통화", "총액", "사용 가능", "잠김"):
            table.add_column(col, justify="right" if col != "통화" else "left", no_wrap=True)
        for cur, b in sorted(balances.items()):
            table.add_row(cur, _fmt_money(b.total), _fmt_money(b.available), _fmt_money(b.locked))
        _console().print(table)
    else:
        _print("잔고: 없음")

    if positions:
        table = Table(title="보유 포지션")
        for col in ("심볼", "수량", "평균단가", "현재가", "평가손익", "손절", "익절"):
            table.add_column(col, justify="right" if col != "심볼" else "left", no_wrap=True)
        for sym, p in sorted(positions.items()):
            price: float | None
            try:
                price = float(broker.get_ticker(sym))
            except Exception as e:  # noqa: BLE001 - 현재가 실패는 표시만 생략
                logger.debug("%s 현재가 조회 실패: %s", sym, mask_secrets(str(e)))
                price = None
            pnl = (
                f"{_fmt_money(p.unrealized_pnl(price))} ({_fmt_pct(p.unrealized_pnl_pct(price))})"
                if price
                else "-"
            )
            table.add_row(
                sym,
                _fmt_qty(p.quantity),
                _fmt_money(p.average_price),
                _fmt_money(price),
                pnl,
                _fmt_money(p.stop_loss),
                _fmt_money(p.take_profit),
            )
        _console().print(table)
    else:
        _print("보유 포지션: 없음")

    try:
        equity = broker.get_equity(symbols)
        quote = broker.quote_currency(symbols[0]) if symbols else ""
        _print(f"총 자산 평가액: {_fmt_money(equity)} {quote}")
    except Exception as e:  # noqa: BLE001
        _print(f"총 자산 평가액: 계산 실패 ({mask_secrets(str(e))})")


@app.command()
def balance(
    config_path: Path = CONFIG_OPTION,
    live: bool = typer.Option(False, "--live", help="설정 mode 와 무관하게 실제 브로커 계좌를 조회"),
) -> None:
    """잔고와 포지션을 보여준다. paper 모드는 저장된 모의투자 상태, live 는 브로커 계좌."""
    with _cli_errors():
        config = _load(config_path)
        _configure_logging(config, with_file=False)
        if live or config.is_live:
            name = _data_broker_name(config)
            broker = create_broker(name, config)
            try:
                _print_account(broker, config.symbols, title=f"{name} 계좌 (sandbox={config.broker.sandbox})")
            finally:
                broker.close()
            return

        store = StateStore(config.engine.state_file)
        data = store.load()
        saved = data.get("paper_broker") if isinstance(data, dict) else None
        if not isinstance(saved, dict):
            _print(
                f"저장된 모의투자 상태가 없습니다 ({store.path}). `tradingbot run -c {config_path}` 를 먼저 실행하세요. "
                f"초기 현금 {config.paper.initial_cash:,.0f} {config.paper.quote_currency}"
            )
            return
        paper = PaperBroker.from_dict(saved)
        note = ""
        if paper.get_positions():
            try:
                data_broker = create_broker(_data_broker_name(config), _data_config(config))
                try:
                    for sym in paper.get_positions():
                        paper.mark_price(sym, float(data_broker.get_ticker(sym)))
                finally:
                    data_broker.close()
            except TradingBotError as e:
                note = f"현재가 조회 실패로 평균단가 기준 평가: {mask_secrets(str(e))}"
        _print_account(
            paper,
            config.symbols,
            title=f"모의투자 계좌 (상태 파일 {store.path}, 갱신 {data.get('updated_at') or '-'})",
        )
        _print(
            f"초기 현금 {_fmt_money(paper.initial_cash)} → 현금 {_fmt_money(paper.cash)}, 청산 거래 {len(paper.trades)}건"
        )
        if note:
            _print(f"[참고] {note}")


# ============================================================================ status
@app.command()
def status(config_path: Path = CONFIG_OPTION) -> None:
    """저장된 엔진 상태 파일(engine.state_file)을 요약해 보여준다."""
    with _cli_errors():
        config = _load(config_path)
        store = StateStore(config.engine.state_file)
        data = store.load()
        if not data:
            _print(f"상태 파일이 없습니다: {store.path} (`tradingbot run` 이 사이클을 돌면 생성됩니다)")
            return

        table = Table(title=f"엔진 상태: {store.path}", show_header=False, box=None, pad_edge=False)
        table.add_column("항목", style="bold")
        table.add_column("값")
        table.add_row("버전", str(data.get("version", "-")))
        table.add_row(
            "모드 / 브로커",
            f"{data.get('mode', '-')} / {data.get('broker', '-')} (시세: {data.get('data_source') or '-'})",
        )
        table.add_row("전략", f"{data.get('strategy', '-')} {data.get('strategy_params', {})}")
        table.add_row("심볼 / 간격", f"{', '.join(data.get('symbols') or [])} / {data.get('interval', '-')}")
        table.add_row("시작 / 갱신", f"{data.get('started_at') or '-'} / {data.get('updated_at') or '-'}")
        table.add_row("사이클 / 일자", f"{data.get('cycles', '-')} / {data.get('day') or '-'}")
        risk = data.get("risk") or {}
        if isinstance(risk, dict):
            table.add_row(
                "리스크",
                f"당일 손익 {_fmt_money(_num(risk.get('daily_pnl')))} ({risk.get('daily_trades', 0)}건), "
                f"당일 시작 자산 {_fmt_money(_num(risk.get('day_start_equity')))}",
            )
        paper = data.get("paper_broker")
        if isinstance(paper, dict):
            table.add_row(
                "모의투자",
                f"현금 {_fmt_money(_num(paper.get('cash')))} / 초기 {_fmt_money(_num(paper.get('initial_cash')))} "
                f"{paper.get('quote_currency', '')}, 주문 {len(paper.get('orders') or [])}건, "
                f"청산 거래 {len(paper.get('trades') or [])}건",
            )
        _console().print(table)

        positions = data.get("positions") or {}
        if isinstance(positions, dict) and positions:
            ptable = Table(title="보유 포지션")
            for col in ("심볼", "수량", "평균단가", "손절", "익절", "최고가", "진입", "진입 사유"):
                ptable.add_column(
                    col,
                    justify="right" if col in ("수량", "평균단가", "손절", "익절", "최고가") else "left",
                    no_wrap=col != "진입 사유",
                )
            for sym, p in positions.items():
                if not isinstance(p, dict):
                    continue
                meta = p.get("meta") or {}
                ptable.add_row(
                    str(sym),
                    _fmt_qty(_num(p.get("quantity"))),
                    _fmt_money(_num(p.get("average_price"))),
                    _fmt_money(_num(p.get("stop_loss"))),
                    _fmt_money(_num(p.get("take_profit"))),
                    _fmt_money(_num(p.get("highest_price"))),
                    str(p.get("opened_at") or "-"),
                    str(meta.get("entry_reason") or "-") if isinstance(meta, dict) else "-",
                )
            _console().print(ptable)
        else:
            _print("보유 포지션: 없음")

        pending = data.get("pending_breakouts") or {}
        if isinstance(pending, dict) and pending:
            for sym, pb in pending.items():
                if isinstance(pb, dict):
                    _print(
                        f"돌파 대기: {sym} 트리거 {_fmt_money(_num(pb.get('trigger')))} (만료 {pb.get('expires') or '-'})"
                    )
        last_ts = data.get("last_candle_ts") or {}
        if isinstance(last_ts, dict) and last_ts:
            _print("마지막 캔들: " + ", ".join(f"{s}={t}" for s, t in last_ts.items()))

        trades = data.get("trades") or []
        if isinstance(trades, list) and trades:
            recent = [t for t in trades[-5:] if isinstance(t, dict)]
            ttable = Table(title=f"최근 거래 (총 {len(trades)}건 중 {len(recent)}건)")
            for col in ("심볼", "수량", "진입가", "청산가", "청산 시각", "사유"):
                ttable.add_column(
                    col,
                    justify="right" if col in ("수량", "진입가", "청산가") else "left",
                    no_wrap=col != "사유",
                )
            for t in recent:
                ttable.add_row(
                    str(t.get("symbol", "-")),
                    _fmt_qty(_num(t.get("quantity"))),
                    _fmt_money(_num(t.get("entry_price"))),
                    _fmt_money(_num(t.get("exit_price"))),
                    str(t.get("exit_time") or "-"),
                    str(t.get("reason") or "-"),
                )
            _console().print(ttable)
        else:
            _print("거래 내역: 없음")


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ============================================================================ 진입점
def main() -> None:
    """콘솔 스크립트 / ``python -m tradingbot`` 진입점."""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
