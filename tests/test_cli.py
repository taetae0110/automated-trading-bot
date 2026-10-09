"""CLI(typer) 테스트.

- 시세는 전부 tests/conftest.py 가 Upbit 공개 API 에서 받아 캐시한 **실제** KRW-BTC / KRW-ETH 캔들이다
  (네트워크가 없으면 해당 테스트는 skip). 가짜/데모 가격은 없다.
- 네트워크 브로커 대신 ``RealFeedBroker`` (실제 캔들을 서빙하는 시세 전용 브로커) 를 ``tradingbot.cli.create_broker``
  자리에 monkeypatch 한다. yfinance 는 실제 캔들 DataFrame 을 돌려주는 함수로 대체한다.
- 계좌 설정값(초기 현금, 수수료, 리스크 비율) 만 상수다.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import yaml
from dotenv import load_dotenv as real_load_dotenv
from typer.testing import CliRunner

from tradingbot import cli
from tradingbot.backtest import Backtester
from tradingbot.brokers import available_brokers
from tradingbot.brokers.base import BaseBroker
from tradingbot.brokers.paper import PaperBroker
from tradingbot.cli import (
    YFINANCE_DIR,
    app,
    parse_param_value,
    parse_params,
    resolve_config,
    write_report,
    yfinance_symbol,
)
from tradingbot.config import AppConfig, load_config
from tradingbot.data import CandleStore, filter_candles_df
from tradingbot.exceptions import AuthenticationError, BrokerError, ConfigError
from tradingbot.logging_setup import teardown_logging
from tradingbot.models import (
    AssetClass,
    Candle,
    OrderSide,
    OrderType,
    SignalAction,
    ensure_utc,
    interval_to_seconds,
    utcnow,
)
from tradingbot.risk import RiskManager
from tradingbot.strategies import available_strategies, candles_to_df, create_strategy

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIGS = sorted((ROOT / "config" / "examples").glob("*.yaml")) + [
    ROOT / "config" / "config.example.yaml"
]
BTC = "KRW-BTC"
ETH = "KRW-ETH"
SAMSUNG = "005930"

# 계좌 설정 (시세가 아님)
INITIAL_CASH = 10_000_000.0
FEE = 0.0005
SLIP = 0.0005

#: 자격증명 관련 환경변수 — 테스트는 항상 "키 없음" 상태에서 돈다
_CRED_ENV = (
    "UPBIT_ACCESS_KEY",
    "UPBIT_SECRET_KEY",
    "CCXT_API_KEY",
    "CCXT_SECRET",
    "CCXT_PASSWORD",
    "BINANCE_API_KEY",
    "BINANCE_SECRET_KEY",
    "BINANCE_API_SECRET",
    "KIS_APP_KEY",
    "KIS_APP_SECRET",
    "KIS_ACCOUNT_NO",
    "KIS_HTS_ID",
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
    "APCA_API_KEY_ID",
    "APCA_API_SECRET_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "SLACK_WEBHOOK_URL",
    "DISCORD_WEBHOOK_URL",
)


# ============================================================================ 테스트 인프라
class RealFeedBroker(BaseBroker):
    """conftest 의 실제 캔들을 서빙하는 시세 전용 브로커 (``create_broker`` 대체용).

    - ``get_candles``: 완성된 캔들(시작 + 간격 <= 지금) 만, ``end`` 미만(exclusive), 마지막 ``limit`` 개
    - ``get_ticker``: 해당 심볼의 가장 최근 실제 캔들 종가
    - 주문/잔고는 키 없는 거래소처럼 AuthenticationError
    """

    asset_class = AssetClass.CRYPTO
    supported_intervals = ("1h", "1d")

    def __init__(
        self,
        candles: dict[tuple[str, str], list[Candle]],
        *,
        name: str = "upbit",
        has_credentials: bool = False,
    ) -> None:
        self.name = name
        self.has_credentials = has_credentials
        self._candles = {k: sorted(v, key=lambda c: c.timestamp) for k, v in candles.items()}
        self.calls: Counter[str] = Counter()
        self.closed = False

    def _series(self, symbol: str, interval: str) -> list[Candle]:
        try:
            return self._candles[(symbol, interval)]
        except KeyError as e:
            raise BrokerError(f"feed 에 없는 심볼/간격: {symbol} {interval}") from e

    def get_candles(self, symbol, interval, limit=200, end=None, include_partial=False):
        self.calls["get_candles"] += 1
        now = utcnow()
        step = timedelta(seconds=interval_to_seconds(interval))
        out = self._series(symbol, interval)
        if not include_partial:
            out = [c for c in out if c.timestamp + step <= now]
        if end is not None:
            out = [c for c in out if c.timestamp < ensure_utc(end)]
        return out[-limit:]

    def get_ticker(self, symbol):
        self.calls["get_ticker"] += 1
        newest: Candle | None = None
        for (sym, _interval), series in self._candles.items():
            if sym == symbol and series:
                if newest is None or series[-1].timestamp > newest.timestamp:
                    newest = series[-1]
        if newest is None:
            raise BrokerError(f"feed 에 없는 심볼: {symbol}")
        return float(newest.close)

    def get_balances(self):
        raise AuthenticationError("잔고 조회에는 UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY 환경변수 필요")

    def get_positions(self):
        raise AuthenticationError("포지션 조회에는 UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY 환경변수 필요")

    def place_order(self, symbol, side, quantity, order_type=OrderType.MARKET, price=None):
        raise AuthenticationError("주문에는 API 키 필요")

    def cancel_order(self, order_id, symbol=None):
        raise AuthenticationError("주문 취소에는 API 키 필요")

    def get_order(self, order_id, symbol=None):
        raise AuthenticationError("주문 조회에는 API 키 필요")

    def get_open_orders(self, symbol=None):
        return []

    def quote_currency(self, symbol):
        if "/" in symbol:  # ccxt 표기 BTC/USDT[:USDT]
            return symbol.split("/", 1)[1].split(":", 1)[0]
        return symbol.split("-", 1)[0]

    def base_currency(self, symbol):
        if "/" in symbol:
            return symbol.split("/", 1)[0]
        return symbol.split("-", 1)[1]

    def min_order_value(self, symbol):
        return 5000.0

    def close(self):
        self.closed = True


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def base_config(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "mode": "paper",
        "broker": {"name": "upbit", "sandbox": True, "fill_timeout_sec": 5},
        "symbols": [BTC],
        "interval": "1d",
        "strategy": {"name": "sma_cross", "params": {"fast": 5, "slow": 20}},
        "risk": {
            "max_position_pct": 0.2,
            "max_positions": 2,
            "stop_loss_pct": 0.03,
            "take_profit_pct": None,
            "max_daily_loss_pct": 0.05,
            "min_order_value": 5000,
        },
        "paper": {
            "initial_cash": INITIAL_CASH,
            "quote_currency": "KRW",
            "fee_pct": FEE,
            "slippage_pct": SLIP,
        },
        "engine": {
            "poll_seconds": 1,
            "candle_limit": 60,
            "state_file": str(tmp_path / "state.json"),
            "stale_data_minutes": 30,
        },
        "notify": {"notify_on_trade": True, "notify_on_error": True},
        "backtest": {
            "start": None,
            "end": None,
            "data_dir": str(tmp_path / "data"),
            "initial_cash": INITIAL_CASH,
            "fee_pct": FEE,
            "slippage_pct": SLIP,
            "fill_on": "next_open",
            "report_dir": str(tmp_path / "reports"),
        },
        "logging": {"level": "WARNING", "file": None},
    }
    if "strategy" in overrides:  # 전략 블록은 통째로 교체 (다른 전략의 파라미터가 섞이지 않게)
        cfg["strategy"] = overrides.pop("strategy")
    return _merge(cfg, overrides)


def write_config(tmp_path: Path, name: str = "config.yaml", **overrides: Any) -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(base_config(tmp_path, **overrides), allow_unicode=True), encoding="utf-8")
    return path


def kis_overrides(tmp_path: Path) -> dict[str, Any]:
    return {
        "broker": {"name": "kis", "sandbox": True},
        "symbols": [SAMSUNG],
        "strategy": {"name": "sma_cross", "params": {"fast": 5, "slow": 20}},
        "backtest": {"initial_cash": 10_000_000_000.0},
        "paper": {"initial_cash": 10_000_000_000.0},
    }


def _complete(candles: list[Candle], interval: str) -> list[Candle]:
    step = timedelta(seconds=interval_to_seconds(interval))
    now = utcnow()
    return [c for c in candles if c.timestamp + step <= now]


def _date(dt: datetime) -> str:
    return ensure_utc(dt).strftime("%Y-%m-%d")


# ============================================================================ 픽스처
@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch):
    """자격증명 없음 + 개발자의 .env 무시(config.py 기본 탐색과 CLI 의 프로젝트 .env 로더 모두) + 로깅 핸들러 정리."""
    for name in _CRED_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("tradingbot.config.load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr(cli, "load_dotenv", lambda *a, **k: False)
    cli._STATE["debug"] = False
    yield
    teardown_logging()
    cli._STATE["debug"] = False


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def invoke(runner: CliRunner) -> Callable[..., Any]:
    """표가 줄바꿈되지 않도록 넓은 COLUMNS 로 호출한다."""

    def _invoke(*args: str, **kw: Any) -> Any:
        env = {"COLUMNS": "220", **kw.pop("env", {})}
        return runner.invoke(app, list(args), env=env, **kw)

    return _invoke


@pytest.fixture
def feed(daily_candles: list[Candle], candles: list[Candle]) -> RealFeedBroker:
    return RealFeedBroker({(BTC, "1d"): daily_candles, (BTC, "1h"): candles})


@pytest.fixture
def patch_broker(monkeypatch: pytest.MonkeyPatch) -> Callable[[BaseBroker], list[str]]:
    """``cli.create_broker`` 를 주어진 브로커를 돌려주는 함수로 바꾸고, 요청된 이름 목록을 돌려준다."""

    def _patch(broker: BaseBroker) -> list[str]:
        names: list[str] = []

        def fake_create(name: str, config: AppConfig) -> BaseBroker:
            names.append(name)
            return broker

        monkeypatch.setattr(cli, "create_broker", fake_create)
        return names

    return _patch


@pytest.fixture
def saved_store(tmp_path: Path, daily_df: pd.DataFrame) -> CandleStore:
    store = CandleStore(tmp_path / "data")
    store.save("upbit", BTC, "1d", daily_df)
    return store


# ============================================================================ --param / 설정 오버라이드
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("5", 5),
        ("-3", -3),
        ("+7", 7),
        (" 12 ", 12),
        ("0.5", 0.5),
        ("1e-3", 0.001),
        ("-2.5", -2.5),
        ("true", True),
        ("True", True),
        ("yes", True),
        ("on", True),
        ("false", False),
        ("no", False),
        ("off", False),
        ("reversion", "reversion"),
        ("nan", "nan"),
        ("inf", "inf"),
        ("", ""),
        ("KRW-BTC", "KRW-BTC"),
    ],
)
def test_parse_param_value(text: str, expected: Any) -> None:
    value = parse_param_value(text)
    assert value == expected
    assert type(value) is type(expected)


def test_parse_params_builds_dict_last_wins() -> None:
    assert parse_params(None) == {}
    assert parse_params([]) == {}
    out = parse_params(["fast=5", "slow=20", "mode=breakout", "k=0.5", "flag=true", "fast=7"])
    assert out == {"fast": 7, "slow": 20, "mode": "breakout", "k": 0.5, "flag": True}
    assert parse_params(["url=a=b"]) == {"url": "a=b"}  # 첫 '=' 기준으로 분리


@pytest.mark.parametrize("bad", ["novalue", "=5", "   =5", " = "])
def test_parse_params_rejects_malformed(bad: str) -> None:
    with pytest.raises(ConfigError, match="key=value|비어"):
        parse_params([bad])


def test_resolve_config_merges_same_strategy_and_replaces_other(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    same = resolve_config(config, params={"fast": 7})
    assert same.strategy.name == "sma_cross"
    assert same.strategy.params == {"fast": 7, "slow": 20}
    explicit_same = resolve_config(config, strategy="SMA_CROSS", params={"slow": 50})
    assert explicit_same.strategy.params == {"fast": 5, "slow": 50}
    other = resolve_config(config, strategy="rsi", params={"period": 7})
    assert other.strategy.name == "rsi"
    assert other.strategy.params == {"period": 7}  # 설정 파일의 fast/slow 는 버린다
    assert create_strategy(other.strategy.name, other.strategy.params).params["period"] == 7
    # 원본은 그대로
    assert config.strategy.params == {"fast": 5, "slow": 20}


def test_resolve_config_overrides_and_validates(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    new = resolve_config(
        config,
        symbols=[ETH, BTC],
        interval="1h",
        start="2025-01-01",
        end="2025-03-01",
        cash=123_456.0,
        fill_on="close",
    )
    assert new.symbols == [ETH, BTC]
    assert new.interval == "1h"
    assert new.backtest.start == "2025-01-01"
    assert new.backtest.end == "2025-03-01"
    assert new.backtest.initial_cash == 123_456.0
    assert new.backtest.fill_on == "close"
    assert resolve_config(config, symbols=[]).symbols == [BTC]
    with pytest.raises(ConfigError, match="설정 오류"):
        resolve_config(config, interval="2h")
    with pytest.raises(ConfigError, match="설정 오류"):
        resolve_config(config, fill_on="sometime")
    with pytest.raises(ConfigError, match="설정 오류"):
        resolve_config(config, symbols=[BTC, BTC])


def test_yfinance_symbol_mapping(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    assert yfinance_symbol(SAMSUNG, "kis", config) == "005930.KS"
    assert yfinance_symbol("AAPL", "alpaca", config) == "AAPL"
    assert yfinance_symbol(BTC, "upbit", config) == "BTC-KRW"
    assert yfinance_symbol("BTC/USDT", "binance", config) == "BTC-USDT"
    assert yfinance_symbol("BTC/USDT:USDT", "ccxt", config) == "BTC-USDT"
    kq = load_config(
        write_config(tmp_path, name="kq.yaml", broker={"name": "kis", "extra": {"yf_market": "KQ"}})
    )
    assert yfinance_symbol("035720", "kis", kq) == "035720.KQ"
    override = load_config(
        write_config(
            tmp_path,
            name="ov.yaml",
            broker={"name": "binance", "extra": {"yf_symbols": {"BTC/USDT": "BTC-USD"}}},
        )
    )
    assert yfinance_symbol("BTC/USDT", "binance", override) == "BTC-USD"
    assert yfinance_symbol("ETH/USDT", "binance", override) == "ETH-USDT"
    with pytest.raises(ConfigError):
        yfinance_symbol("12", "kis", config)


# ============================================================================ 목록 / 도움말
def test_help_version_and_no_args(invoke: Callable[..., Any]) -> None:
    result = invoke("--help")
    assert result.exit_code == 0
    for cmd in (
        "init",
        "validate-config",
        "strategies",
        "brokers",
        "backtest",
        "download",
        "run",
        "balance",
        "status",
    ):
        assert cmd in result.output
    result = invoke("--version")
    assert result.exit_code == 0
    assert "tradingbot 0." in result.output
    result = invoke()
    assert "Usage" in result.output
    assert app.pretty_exceptions_show_locals is False  # 트레이스백에 자격증명 지역변수가 찍히지 않게


def test_strategies_lists_every_registered_strategy(invoke: Callable[..., Any]) -> None:
    result = invoke("strategies")
    assert result.exit_code == 0, result.output
    for name in available_strategies():
        assert name in result.output
    assert "fast=10, slow=30" in result.output
    assert "k=0.5, ma_period=0" in result.output
    assert re.search(r"sma_cross.*\b31\b", result.output)  # 워밍업


def test_brokers_lists_every_registered_broker(invoke: Callable[..., Any]) -> None:
    result = invoke("brokers")
    assert result.exit_code == 0, result.output
    for name in available_brokers():
        assert re.search(rf"\b{name}\b", result.output)
    assert "UPBIT_ACCESS_KEY" in result.output
    assert "KIS_APP_KEY" in result.output
    assert "tradingbot[ccxt]" in result.output  # 대괄호가 rich 마크업으로 사라지지 않는다


# ============================================================================ validate-config
@pytest.mark.parametrize("path", EXAMPLE_CONFIGS, ids=lambda p: p.name)
def test_validate_config_accepts_example_configs(invoke: Callable[..., Any], path: Path) -> None:
    result = invoke("validate-config", "-c", str(path))
    assert result.exit_code == 0, result.output
    assert "설정 OK" in result.output
    assert "[오류]" not in result.output


def test_validate_config_missing_file(invoke: Callable[..., Any], tmp_path: Path) -> None:
    result = invoke("validate-config", "-c", str(tmp_path / "missing.yaml"))
    assert result.exit_code == 1
    assert "[오류] ConfigError" in result.output
    assert "찾을 수 없습니다" in result.output
    assert "Traceback" not in result.output


def test_validate_config_debug_reraises(invoke: Callable[..., Any], tmp_path: Path) -> None:
    result = invoke("--debug", "validate-config", "-c", str(tmp_path / "missing.yaml"))
    assert result.exit_code == 1
    assert isinstance(result.exception, ConfigError)


def test_validate_config_reports_strategy_param_error(invoke: Callable[..., Any], tmp_path: Path) -> None:
    cfg = write_config(tmp_path, strategy={"name": "sma_cross", "params": {"fast": 50, "slow": 20}})
    result = invoke("validate-config", "-c", str(cfg))
    assert result.exit_code == 1
    assert "[오류]" in result.output and "sma_cross" in result.output


def test_validate_config_reports_candle_limit_and_interval(
    invoke: Callable[..., Any], tmp_path: Path
) -> None:
    cfg = write_config(tmp_path, engine={"candle_limit": 10})
    result = invoke("validate-config", "-c", str(cfg))
    assert result.exit_code == 1
    assert "candle_limit" in result.output
    cfg2 = write_config(
        tmp_path, name="c2.yaml", broker={"name": "binance"}, symbols=["BTC/USDT"], interval="10m"
    )
    result = invoke("validate-config", "-c", str(cfg2))
    assert result.exit_code == 1
    assert "지원하지 않습니다" in result.output


def test_validate_config_warnings_do_not_fail(invoke: Callable[..., Any], tmp_path: Path) -> None:
    cfg = write_config(tmp_path, symbols=["BTC/USDT"], notify={"telegram": {"enabled": True}})
    result = invoke("validate-config", "-c", str(cfg))
    assert result.exit_code == 0, result.output
    assert "[경고]" in result.output
    assert "TELEGRAM_BOT_TOKEN" in result.output
    assert "표기" in result.output  # 심볼 표기 경고
    assert "설정 OK" in result.output


def test_validate_config_invalid_yaml_and_bad_date(invoke: Callable[..., Any], tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("mode: [unclosed", encoding="utf-8")
    result = invoke("validate-config", "-c", str(bad))
    assert result.exit_code == 1 and "[오류]" in result.output
    cfg = write_config(tmp_path, backtest={"start": "2025-13-45"})
    result = invoke("validate-config", "-c", str(cfg))
    assert result.exit_code == 1 and "backtest.start" in result.output


def test_validate_config_rejects_unknown_keys(invoke: Callable[..., Any], tmp_path: Path) -> None:
    """오타 난 키는 기본값으로 조용히 대체되지 않고 오류다 (config.py 모델은 extra='ignore' 라 CLI 가 거부한다).

    risk.stop_loss_pc 같은 오타가 손절을 지우고, 그대로 '설정 OK' 와 함께 run(--live) 이 돌면 안 된다.
    """
    cfg = write_config(
        tmp_path,
        symbol=[ETH],  # symbols 오타 (최상위)
        risk={"stop_loss_pc": 0.1, "trailing_stop": 0.05},  # risk.* 오타
        risks={"max_positions": 1},  # 섹션 이름 오타
        engine={"pollseconds": 5},
        notify={"telegram": {"enable": True}},  # 중첩 모델 안의 오타
    )
    result = invoke("validate-config", "-c", str(cfg))
    assert result.exit_code == 1, result.output
    assert "[오류] ConfigError" in result.output and "알 수 없는 키가 6개" in result.output
    assert "설정 OK" not in result.output and "Traceback" not in result.output
    for unknown, suggestion in (
        ("symbol", "symbols"),
        ("risk.stop_loss_pc", "risk.stop_loss_pct"),
        ("risk.trailing_stop", "risk.trailing_stop_pct"),
        ("risks", "risk"),
        ("engine.pollseconds", "engine.poll_seconds"),
        ("notify.telegram.enable", "notify.telegram.enabled"),
    ):
        assert f"'{unknown}' (혹시 '{suggestion}'?)" in result.output

    # 설정을 읽는 다른 명령도 같은 이유로 시작하지 않는다 (엔진이 기본 리스크 값으로 돌지 않음)
    for args in (
        ("run", "--once"),
        ("backtest", "--source", "csv"),
        ("download",),
        ("status",),
        ("balance",),
    ):
        result = invoke(args[0], "-c", str(cfg), *args[1:])
        assert result.exit_code == 1 and "알 수 없는 키" in result.output, (args, result.output)
    assert not (tmp_path / "state.json").exists()


def test_unknown_key_check_skips_free_form_sections(invoke: Callable[..., Any], tmp_path: Path) -> None:
    """broker.extra / strategy.params 는 자유 형식이라 검사하지 않는다 (전략 파라미터 오타는 전략이 거부)."""
    cfg = write_config(
        tmp_path,
        broker={"extra": {"jwt_algorithm": "HS256", "yf_symbols": {BTC: "BTC-USD"}}},
        strategy={"name": "rsi", "params": {"period": 7, "oversold": 35, "overbought": 65}},
    )
    result = invoke("validate-config", "-c", str(cfg))
    assert result.exit_code == 0, result.output
    assert "알 수 없는 키" not in result.output
    assert (
        cli.find_unknown_keys({"broker": {"extra": {"anything": 1}}, "strategy": {"params": {"zzz": 1}}})
        == []
    )
    assert cli.find_unknown_keys({"notify": {"slack": {"enabled": True, "url": "x"}}}) == [
        "'notify.slack.url'"
    ]
    assert cli.find_unknown_keys({"risk": None, "logging": {"levl": "INFO"}}) == [
        "'logging.levl' (혹시 'logging.level'?)"
    ]
    cfg2 = write_config(
        tmp_path, name="p.yaml", strategy={"name": "sma_cross", "params": {"fasst": 5, "slow": 20}}
    )
    result = invoke("validate-config", "-c", str(cfg2))
    assert result.exit_code == 1 and "fasst" in result.output


def test_validate_config_warns_about_mode_backtest(invoke: Callable[..., Any], tmp_path: Path) -> None:
    cfg = write_config(tmp_path, mode="backtest")
    result = invoke("validate-config", "-c", str(cfg))
    assert result.exit_code == 0, result.output
    assert "[경고] mode=backtest" in result.output and "tradingbot backtest" in result.output


def test_project_dotenv_is_loaded_from_cwd_or_config_dir(
    invoke: Callable[..., Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI 가 프로젝트 .env 를 읽는다: `init --dir DIR` 레이아웃(설정 파일 디렉터리의 상위) 과 현재 디렉터리.

    config.py 의 load_dotenv() 기본 탐색은 패키지 디렉터리 기준이라 (픽스처가 꺼 둔 상태 그대로) 이 파일들을 보지 못한다.
    """
    monkeypatch.setattr(cli, "load_dotenv", real_load_dotenv)  # 픽스처가 꺼 둔 CLI 로더만 복원
    keys = ("UPBIT_ACCESS_KEY", "UPBIT_SECRET_KEY")

    def clear() -> None:
        for k in keys:
            os.environ.pop(k, None)

    try:
        # 1) init --dir 레이아웃: proj/config/config.yaml + proj/.env, 실행은 다른 디렉터리에서
        proj = tmp_path / "proj"
        (proj / "config").mkdir(parents=True)
        cfg = write_config(tmp_path, name="proj/config/config.yaml")
        (proj / ".env").write_text(
            "UPBIT_ACCESS_KEY=dotenv-test-access\nUPBIT_SECRET_KEY=dotenv-test-secret\n", encoding="utf-8"
        )
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        assert cli.find_project_dotenv(cfg) == proj / ".env"
        result = invoke("validate-config", "-c", str(cfg))
        assert result.exit_code == 0, result.output
        assert (
            "비어 있습니다" not in result.output
        )  # 키를 읽었으므로 '환경변수 ... 가 비어 있습니다' 경고가 없다
        assert "환경변수 파일" in result.output and "proj" in result.output
        assert (
            "dotenv-test-access" not in result.output and "dotenv-test-secret" not in result.output
        )  # 값은 미출력
        assert os.environ.get("UPBIT_ACCESS_KEY") == "dotenv-test-access"
        clear()

        # 2) 현재 디렉터리의 .env (설정 파일은 다른 곳)
        work = tmp_path / "work"
        work.mkdir()
        (work / ".env").write_text("UPBIT_ACCESS_KEY=cwd-test-access\n", encoding="utf-8")
        (tmp_path / "cfgdir").mkdir()
        cfg2 = write_config(tmp_path, name="cfgdir/c2.yaml")
        monkeypatch.chdir(work)
        assert cli.find_project_dotenv(cfg2) == work / ".env"
        result = invoke("validate-config", "-c", str(cfg2))
        assert result.exit_code == 0, result.output
        assert os.environ.get("UPBIT_ACCESS_KEY") == "cwd-test-access"
        assert (
            "환경변수 UPBIT_SECRET_KEY 가 비어 있습니다" in result.output
        )  # 읽힌 ACCESS_KEY 는 경고에서 빠진다
        clear()

        # 3) 이미 export 된 값이 우선한다 (override=False)
        monkeypatch.setenv("UPBIT_ACCESS_KEY", "exported-wins")
        result = invoke("validate-config", "-c", str(cfg2))
        assert result.exit_code == 0, result.output
        assert os.environ.get("UPBIT_ACCESS_KEY") == "exported-wins"
        clear()

        # 4) 아무 데도 없으면 경고 + 표에 '찾지 못함'
        monkeypatch.chdir(elsewhere)
        assert cli.find_project_dotenv(cfg2) is None
        result = invoke("validate-config", "-c", str(cfg2))
        assert result.exit_code == 0, result.output
        assert "UPBIT_ACCESS_KEY, UPBIT_SECRET_KEY 가 비어 있습니다" in result.output
        assert ".env 를 찾지 못함" in result.output
    finally:
        clear()


# ============================================================================ init
def test_init_creates_config_and_env(
    invoke: Callable[..., Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = invoke("init")
    assert result.exit_code == 0, result.output
    cfg = tmp_path / "config" / "config.yaml"
    env = tmp_path / ".env"
    assert cfg.is_file() and env.is_file()
    assert cfg.read_text(encoding="utf-8") == (ROOT / "config" / "config.example.yaml").read_text(
        encoding="utf-8"
    )
    assert env.read_text(encoding="utf-8") == (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "생성" in result.output
    # 생성된 설정은 바로 검증을 통과한다
    assert invoke("validate-config", "-c", str(cfg)).exit_code == 0

    cfg.write_text("mode: paper\n", encoding="utf-8")
    result = invoke("init")
    assert result.exit_code == 0
    assert "건너뜀" in result.output
    assert cfg.read_text(encoding="utf-8") == "mode: paper\n"  # 덮어쓰지 않음

    result = invoke("init", "--force")
    assert result.exit_code == 0
    assert cfg.read_text(encoding="utf-8") == (ROOT / "config" / "config.example.yaml").read_text(
        encoding="utf-8"
    )


def test_init_target_dir_and_missing_templates(
    invoke: Callable[..., Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "proj"
    monkeypatch.chdir(tmp_path)
    result = invoke("init", "--dir", str(target))
    assert result.exit_code == 0, result.output
    assert (target / "config" / "config.yaml").is_file()
    assert (target / ".env").is_file()

    monkeypatch.setattr(cli, "PACKAGE_ROOT", tmp_path / "nowhere")
    result = invoke("init", "--dir", str(tmp_path / "other"))
    assert result.exit_code == 1
    assert "예시 파일" in result.output


@pytest.mark.skipif(os.name != "posix", reason="POSIX 파일 권한")
def test_init_creates_env_file_owner_only(
    invoke: Callable[..., Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """.env 에는 API 키가 들어가므로 umask 와 무관하게 0600 으로 만든다 (KIS 토큰 캐시와 같은 기준). --force 뒤에도 0600."""
    monkeypatch.chdir(tmp_path)
    old_umask = os.umask(0o022)
    try:
        result = invoke("init")
        assert result.exit_code == 0, result.output
        env = tmp_path / ".env"
        assert stat.S_IMODE(env.stat().st_mode) == 0o600
        assert env.read_text(encoding="utf-8") == (ROOT / ".env.example").read_text(encoding="utf-8")
        assert "권한 600" in result.output
        # 비밀이 없는 설정 파일은 umask 기본 권한
        assert stat.S_IMODE((tmp_path / "config" / "config.yaml").stat().st_mode) == 0o644

        env.chmod(0o644)
        env.write_text("UPBIT_ACCESS_KEY=old\n", encoding="utf-8")
        result = invoke("init", "--force")
        assert result.exit_code == 0, result.output
        assert stat.S_IMODE(env.stat().st_mode) == 0o600
        assert env.read_text(encoding="utf-8") == (ROOT / ".env.example").read_text(encoding="utf-8")
    finally:
        os.umask(old_umask)


# ============================================================================ backtest
def test_backtest_csv_end_to_end_with_report(
    invoke: Callable[..., Any], tmp_path: Path, saved_store: CandleStore, daily_df: pd.DataFrame
) -> None:
    cfg = write_config(tmp_path)
    reports = tmp_path / "out"
    result = invoke(
        "backtest",
        "-c",
        str(cfg),
        "--source",
        "csv",
        "--strategy",
        "sma_cross",
        "-p",
        "fast=5",
        "-p",
        "slow=20",
        "--report",
        "--report-dir",
        str(reports),
    )
    assert result.exit_code == 0, result.output
    assert "=== 백테스트 결과: sma_cross {'fast': 5, 'slow': 20} ===" in result.output
    assert f"데이터 출처 : {BTC} ← csv" in result.output
    assert "총 수익률" in result.output and "최대 낙폭" in result.output
    assert f"심볼        : {BTC}" in result.output
    assert "리포트 저장" in result.output

    json_files = list(reports.glob("*.json"))
    equity_files = list(reports.glob("*_equity.csv"))
    summary_files = list(reports.glob("*_summary.txt"))
    assert len(json_files) == len(equity_files) == len(summary_files) == 1
    report = json.loads(json_files[0].read_text(encoding="utf-8"))
    assert report["strategy"] == "sma_cross"
    assert report["params"] == {"fast": 5, "slow": 20}
    assert report["symbols"] == [BTC]
    assert report["interval"] == "1d"
    assert report["bars"] == len(daily_df) == len(report["equity_curve"])
    assert report["initial_cash"] == INITIAL_CASH
    assert report["quote_currency"] == "KRW"
    assert report["fill_on"] == "next_open"
    equity = pd.read_csv(equity_files[0])
    assert list(equity.columns) == ["timestamp", "equity"]
    assert len(equity) == report["bars"]
    assert equity["equity"].iloc[0] == pytest.approx(INITIAL_CASH)
    assert equity["equity"].iloc[-1] == pytest.approx(report["final_equity"])
    assert equity["timestamp"].iloc[0] == daily_df["timestamp"].iloc[0].strftime("%Y-%m-%dT%H:%M:%SZ")
    assert "백테스트 결과" in summary_files[0].read_text(encoding="utf-8")

    # CLI 배선(수수료/슬리피지/fill_on/통화) 이 백테스터 직접 호출과 같은 결과를 낸다
    config = load_config(cfg)
    direct = Backtester(
        create_strategy("sma_cross", {"fast": 5, "slow": 20}),
        RiskManager(config.risk),
        initial_cash=INITIAL_CASH,
        fee_pct=FEE,
        slippage_pct=SLIP,
        fill_on="next_open",
        quote_currency="KRW",
        interval="1d",
        min_order_value=config.risk.min_order_value,
    ).run({BTC: daily_df})
    assert report["final_equity"] == pytest.approx(direct.final_equity)
    assert len(report["trades"]) == len(direct.trades)
    assert report["metrics"]["num_trades"] == len(direct.trades)
    if direct.trades:
        assert "거래 내역" in result.output
        assert direct.trades[-1].reason in result.output
    else:
        assert "거래 내역: 없음" in result.output


def test_backtest_quiets_per_bar_logs_unless_debug(
    invoke: Callable[..., Any],
    tmp_path: Path,
    saved_store: CandleStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """bar 마다 나오는 risk/paper INFO 로그는 백테스트 동안 막고(--debug 면 그대로), 로거 레벨은 복원한다."""
    cfg = write_config(tmp_path)
    args = (
        "backtest",
        "-c",
        str(cfg),
        "--source",
        "csv",
        "--strategy",
        "volatility_breakout",
        "-p",
        "k=0.5",
        "--trades",
        "1",
    )
    with caplog.at_level(logging.DEBUG):
        result = invoke(*args)
    assert result.exit_code == 0, result.output
    assert "백테스트 결과" in result.output
    chatty = {r.name for r in caplog.records if r.levelno < logging.WARNING}
    assert not chatty & set(cli.BACKTEST_QUIET_LOGGERS)
    for name in cli.BACKTEST_QUIET_LOGGERS:
        assert logging.getLogger(name).level == logging.NOTSET  # 실행 후 원래 레벨로 복원

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        result = invoke("--debug", *args)
    assert result.exit_code == 0, result.output
    chatty = {r.name for r in caplog.records if r.levelno < logging.WARNING}
    assert set(cli.BACKTEST_QUIET_LOGGERS) <= chatty
    assert "tradingbot.backtest.engine" in chatty
    assert any("[paper] 주문 접수" in r.getMessage() for r in caplog.records)
    assert any("UTC 날짜 변경" in r.getMessage() for r in caplog.records)


def test_backtest_uses_config_strategy_params_when_not_overridden(
    invoke: Callable[..., Any], tmp_path: Path, saved_store: CandleStore
) -> None:
    cfg = write_config(
        tmp_path, strategy={"name": "rsi", "params": {"period": 7, "oversold": 35, "overbought": 65}}
    )
    result = invoke("backtest", "-c", str(cfg), "--source", "csv", "-p", "oversold=40")
    assert result.exit_code == 0, result.output
    assert (
        "rsi {'period': 7, 'oversold': 40.0, 'overbought': 65.0}" in result.output
    )  # 전략이 float 로 정규화


def test_backtest_csv_missing_data_exits_1(invoke: Callable[..., Any], tmp_path: Path) -> None:
    cfg = write_config(tmp_path)
    result = invoke("backtest", "-c", str(cfg), "--source", "csv")
    assert result.exit_code == 1
    assert "[오류] DataError" in result.output
    assert "download" in result.output


def test_backtest_csv_range_without_candles_exits_1(
    invoke: Callable[..., Any], tmp_path: Path, saved_store: CandleStore, daily_df: pd.DataFrame
) -> None:
    last = daily_df["timestamp"].iloc[-1].to_pydatetime()
    cfg = write_config(tmp_path)
    result = invoke("backtest", "-c", str(cfg), "--source", "csv", "--start", _date(last + timedelta(days=5)))
    assert result.exit_code == 1
    assert "구간" in result.output
    result = invoke("backtest", "-c", str(cfg), "--start", "2025-02-01", "--end", "2025-01-01")
    assert result.exit_code == 1
    assert "보다 늦습니다" in result.output


def test_backtest_auto_prefers_covered_csv(
    invoke: Callable[..., Any],
    tmp_path: Path,
    saved_store: CandleStore,
    daily_df: pd.DataFrame,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = daily_df["timestamp"].iloc[0].to_pydatetime()
    last = daily_df["timestamp"].iloc[-1].to_pydatetime()

    def must_not_download(name: str, config: AppConfig) -> BaseBroker:
        raise AssertionError("CSV 가 구간을 덮으면 브로커를 만들지 않아야 한다")

    monkeypatch.setattr(cli, "create_broker", must_not_download)
    cfg = write_config(tmp_path)
    result = invoke("backtest", "-c", str(cfg), "--start", _date(first), "--end", _date(last))
    assert result.exit_code == 0, result.output
    assert f"데이터 출처 : {BTC} ← csv" in result.output
    assert f"({len(daily_df)} bars)" in result.output


def test_backtest_auto_downloads_from_broker_when_missing(
    invoke: Callable[..., Any],
    tmp_path: Path,
    feed: RealFeedBroker,
    patch_broker: Callable[[BaseBroker], list[str]],
    daily_candles: list[Candle],
) -> None:
    names = patch_broker(feed)
    cfg = write_config(tmp_path)
    result = invoke("backtest", "-c", str(cfg))
    assert result.exit_code == 0, result.output
    assert names == ["upbit"]
    assert f"데이터 출처 : {BTC} ← upbit API" in result.output
    saved = tmp_path / "data" / "upbit" / "KRW-BTC_1d.csv"
    assert saved.is_file()
    store = CandleStore(tmp_path / "data")
    df = store.load("upbit", BTC, "1d")
    assert len(df) == len(_complete(daily_candles, "1d"))
    assert feed.calls["get_candles"] >= 1
    assert feed.closed

    # 두 번째 실행은 저장된 CSV 를 쓴다 (다운로드 안 함). 픽스처는 고정된 과거 구간이라 종료일을 주지 않으면
    # "최신 구간을 덮는가" 검사가 현재 시각 기준이 되어 다시 내려받으므로 저장된 구간을 그대로 요청한다
    before = feed.calls["get_candles"]
    first, last = df["timestamp"].iloc[0], df["timestamp"].iloc[-1]
    result = invoke("backtest", "-c", str(cfg), "--start", _date(first), "--end", _date(last))
    assert result.exit_code == 0, result.output
    assert f"데이터 출처 : {BTC} ← csv" in result.output
    assert feed.calls["get_candles"] == before


def test_backtest_source_broker_explicit(
    invoke: Callable[..., Any],
    tmp_path: Path,
    feed: RealFeedBroker,
    patch_broker: Callable[[BaseBroker], list[str]],
    daily_candles: list[Candle],
) -> None:
    patch_broker(feed)
    complete = _complete(daily_candles, "1d")
    start = complete[len(complete) // 2].timestamp
    cfg = write_config(tmp_path)
    result = invoke("backtest", "-c", str(cfg), "--source", "broker", "--start", _date(start))
    assert result.exit_code == 0, result.output
    expected_bars = len([c for c in complete if c.timestamp >= start])
    assert f"({expected_bars} bars)" in result.output


def test_backtest_stock_broker_without_keys_suggests_yfinance(
    invoke: Callable[..., Any], tmp_path: Path
) -> None:
    cfg = write_config(tmp_path, **kis_overrides(tmp_path))
    result = invoke("backtest", "-c", str(cfg), "--source", "broker")
    assert result.exit_code == 1
    assert "--source yfinance" in result.output
    assert "KIS_APP_KEY" in result.output
    assert "Traceback" not in result.output


def test_backtest_yfinance_source_for_stock(
    invoke: Callable[..., Any], tmp_path: Path, daily_df: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Any, ...]] = []

    def fake_load_yfinance(
        symbol: str, interval: str, start: Any, end: Any = None, **kw: Any
    ) -> pd.DataFrame:
        calls.append((symbol, interval, start, end))
        return filter_candles_df(daily_df, start, end)

    monkeypatch.setattr(cli, "load_yfinance", fake_load_yfinance)
    first = daily_df["timestamp"].iloc[0].to_pydatetime()
    last = daily_df["timestamp"].iloc[-1].to_pydatetime()
    cfg = write_config(tmp_path, **kis_overrides(tmp_path))
    result = invoke(
        "backtest", "-c", str(cfg), "--source", "yfinance", "--start", _date(first), "--end", _date(last)
    )
    assert result.exit_code == 0, result.output
    assert calls and calls[0][0] == "005930.KS" and calls[0][1] == "1d"
    assert "yfinance 005930.KS" in result.output
    assert "(자산 구분 stock)" in result.output
    assert "KRW" in result.output
    assert (tmp_path / "data" / YFINANCE_DIR / "005930_1d.csv").is_file()

    # auto 모드: 키 없는 주식 브로커는 yfinance 로 넘어간다
    result = invoke("backtest", "-c", str(cfg), "--start", _date(first), "--end", _date(last))
    assert result.exit_code == 0, result.output
    assert f"데이터 출처 : {SAMSUNG} ← csv" in result.output  # 방금 저장한 yfinance CSV 재사용

    (tmp_path / "data" / YFINANCE_DIR / "005930_1d.csv").unlink()
    result = invoke("backtest", "-c", str(cfg), "--start", _date(first), "--end", _date(last))
    assert result.exit_code == 0, result.output
    assert "[안내]" in result.output and "yfinance" in result.output
    assert len(calls) == 2


def test_backtest_stock_quantities_are_whole_shares(
    invoke: Callable[..., Any], tmp_path: Path, daily_df: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "load_yfinance", lambda symbol, interval, start, end=None, **kw: daily_df)
    cfg = write_config(tmp_path, **kis_overrides(tmp_path))
    reports = tmp_path / "rep"
    result = invoke(
        "backtest", "-c", str(cfg), "--source", "yfinance", "--report", "--report-dir", str(reports)
    )
    assert result.exit_code == 0, result.output
    report = json.loads(next(reports.glob("*.json")).read_text(encoding="utf-8"))
    assert report["asset_class"] == "stock"
    for order in report["orders"]:
        assert float(order["quantity"]).is_integer()


def test_backtest_param_and_option_errors(
    invoke: Callable[..., Any], tmp_path: Path, saved_store: CandleStore
) -> None:
    cfg = write_config(tmp_path)
    result = invoke("backtest", "-c", str(cfg), "--source", "csv", "-p", "fast=abc")
    assert result.exit_code == 1 and "파라미터 오류" in result.output
    result = invoke("backtest", "-c", str(cfg), "--source", "csv", "-p", "novalue")
    assert result.exit_code == 1 and "key=value" in result.output
    result = invoke("backtest", "-c", str(cfg), "--source", "csv", "--fill-on", "bogus")
    assert result.exit_code == 1 and "--fill-on" in result.output
    result = invoke("backtest", "-c", str(cfg), "--source", "bogus")
    assert result.exit_code == 2
    result = invoke("backtest", "-c", str(cfg), "--source", "csv", "--strategy", "nope")
    assert result.exit_code == 1 and "알 수 없는 전략" in result.output
    result = invoke("backtest", "-c", str(cfg), "--source", "csv", "--interval", "2h")
    assert result.exit_code == 1 and "설정 오류" in result.output
    result = invoke("backtest", "-c", str(cfg), "--source", "csv", "--start", "not-a-date")
    assert result.exit_code == 1 and "형식" in result.output


def test_backtest_rejects_paper_broker_name(invoke: Callable[..., Any], tmp_path: Path) -> None:
    cfg = write_config(tmp_path, broker={"name": "paper"})
    result = invoke("backtest", "-c", str(cfg))
    assert result.exit_code == 1
    assert "시세 출처" in result.output


def test_backtest_multi_symbol_cash_and_fill_on(
    invoke: Callable[..., Any],
    tmp_path: Path,
    saved_store: CandleStore,
    eth_daily_df: pd.DataFrame,
) -> None:
    saved_store.save("upbit", ETH, "1d", eth_daily_df)
    cfg = write_config(tmp_path)
    reports = tmp_path / "rep"
    result = invoke(
        "backtest",
        "-c",
        str(cfg),
        "--source",
        "csv",
        "--symbol",
        BTC,
        "--symbol",
        ETH,
        "--cash",
        "5000000",
        "--fill-on",
        "close",
        "--trades",
        "3",
        "--report",
        "--report-dir",
        str(reports),
    )
    assert result.exit_code == 0, result.output
    assert f"심볼        : {BTC}, {ETH}" in result.output
    assert f"데이터 출처 : {ETH} ← csv" in result.output
    report = json.loads(next(reports.glob("*.json")).read_text(encoding="utf-8"))
    assert report["symbols"] == [BTC, ETH]
    assert report["initial_cash"] == 5_000_000
    assert report["fill_on"] == "close"
    if len(report["trades"]) > 3:
        assert "최근 3건" in result.output


def test_write_report_files(tmp_path: Path, daily_df: pd.DataFrame) -> None:
    config = load_config(write_config(tmp_path))
    result = Backtester(
        create_strategy("sma_cross", {"fast": 5, "slow": 20}),
        RiskManager(config.risk),
        initial_cash=INITIAL_CASH,
        interval="1d",
    ).run({BTC: daily_df})
    paths = write_report(result, tmp_path / "r1")
    assert set(paths) == {"json", "equity_csv", "summary"}
    for p in paths.values():
        assert p.is_file() and p.parent == tmp_path / "r1"
    assert paths["json"].name.startswith("sma_cross_KRW-BTC_1d_")
    data = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert data == result.to_dict()
    equity = pd.read_csv(paths["equity_csv"])
    assert len(equity) == len(result.equity_curve)
    assert equity["equity"].to_list() == pytest.approx(result.equity_curve.to_list())


# ============================================================================ download
def test_download_from_broker(
    invoke: Callable[..., Any],
    tmp_path: Path,
    feed: RealFeedBroker,
    patch_broker: Callable[[BaseBroker], list[str]],
    daily_candles: list[Candle],
) -> None:
    patch_broker(feed)
    complete = _complete(daily_candles, "1d")
    start = complete[-100].timestamp
    cfg = write_config(tmp_path)
    result = invoke("download", "-c", str(cfg), "--start", _date(start))
    assert result.exit_code == 0, result.output
    assert "다운로드 결과" in result.output
    path = tmp_path / "data" / "upbit" / "KRW-BTC_1d.csv"
    assert path.is_file()
    assert str(path) in result.output
    stored = CandleStore(tmp_path / "data").load("upbit", BTC, "1d")
    assert len(stored) == len(complete)  # 받은 캔들은 전부 캐시
    in_range = len([c for c in complete if c.timestamp >= start])
    assert re.search(rf"\b{in_range}\b", result.output)
    assert feed.closed


def test_download_options_and_errors(
    invoke: Callable[..., Any],
    tmp_path: Path,
    feed: RealFeedBroker,
    patch_broker: Callable[[BaseBroker], list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_broker(feed)
    cfg = write_config(tmp_path)
    result = invoke("download", "-c", str(cfg), "--start", "2025-01-02", "--end", "2025-01-01")
    assert result.exit_code == 1 and "보다 늦습니다" in result.output
    result = invoke("download", "-c", str(cfg), "--start", "nope")
    assert result.exit_code == 1 and "형식" in result.output
    result = invoke("download", "-c", str(cfg), "--symbol", "KRW-XYZ", "--start", "2025-01-01")
    assert result.exit_code == 1 and "[오류] BrokerError" in result.output
    result = invoke("download", "-c", str(cfg), "--source", "nope")
    assert result.exit_code == 2
    result = invoke("download", "-c", str(cfg), "--interval", "1w", "--start", "2025-01-01")
    assert result.exit_code == 1 and "[오류] BrokerError" in result.output  # feed 에 없는 간격


def test_download_stock_without_keys(invoke: Callable[..., Any], tmp_path: Path) -> None:
    cfg = write_config(tmp_path, **kis_overrides(tmp_path))
    result = invoke("download", "-c", str(cfg), "--start", "2025-01-01")
    assert result.exit_code == 1
    assert "--source yfinance" in result.output


def test_download_from_yfinance(
    invoke: Callable[..., Any], tmp_path: Path, daily_df: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Any, ...]] = []

    def fake_load_yfinance(
        symbol: str, interval: str, start: Any, end: Any = None, **kw: Any
    ) -> pd.DataFrame:
        calls.append((symbol, interval, start, end))
        return filter_candles_df(daily_df, start, end)

    monkeypatch.setattr(cli, "load_yfinance", fake_load_yfinance)
    first = daily_df["timestamp"].iloc[0].to_pydatetime()
    cfg = write_config(tmp_path)
    result = invoke(
        "download", "-c", str(cfg), "--source", "yfinance", "--start", _date(first), "--symbol", BTC
    )
    assert result.exit_code == 0, result.output
    assert calls[0][0] == "BTC-KRW"
    path = tmp_path / "data" / YFINANCE_DIR / "KRW-BTC_1d.csv"
    assert path.is_file()
    assert "yfinance BTC-KRW" in result.output
    stored = CandleStore(tmp_path / "data").load(YFINANCE_DIR, BTC, "1d")
    pd.testing.assert_frame_equal(stored, daily_df)


# ============================================================================ run
def test_run_live_flag_requires_mode_live(invoke: Callable[..., Any], tmp_path: Path) -> None:
    cfg = write_config(tmp_path)
    result = invoke("run", "-c", str(cfg), "--live")
    assert result.exit_code == 1
    assert "[오류] ConfigError" in result.output
    assert "mode" in result.output
    assert not (tmp_path / "state.json").exists()


def test_run_mode_live_requires_live_flag(invoke: Callable[..., Any], tmp_path: Path) -> None:
    cfg = write_config(tmp_path, mode="live")
    result = invoke("run", "-c", str(cfg), "--once")
    assert result.exit_code == 1
    assert "--live" in result.output
    assert not (tmp_path / "state.json").exists()


def test_run_rejects_paper_broker_name(invoke: Callable[..., Any], tmp_path: Path) -> None:
    cfg = write_config(tmp_path, broker={"name": "paper"})
    result = invoke("run", "-c", str(cfg), "--once")
    assert result.exit_code == 1
    assert "시세 출처" in result.output


def test_run_refuses_mode_backtest(
    invoke: Callable[..., Any],
    tmp_path: Path,
    feed: RealFeedBroker,
    patch_broker: Callable[[BaseBroker], list[str]],
) -> None:
    """mode: backtest 는 엔진에 의미가 없다 (is_live 만 봄) — 조용히 실시간 paper 루프로 내려가지 않고 거부한다."""
    names = patch_broker(feed)
    cfg = write_config(tmp_path, mode="backtest")
    result = invoke("run", "-c", str(cfg), "--once")
    assert result.exit_code == 1, result.output
    assert "[오류] ConfigError" in result.output and "tradingbot backtest" in result.output
    assert "모의투자(paper)" not in result.output
    assert names == [] and feed.calls["get_candles"] == 0  # 브로커/엔진을 만들지 않았다
    assert not (tmp_path / "state.json").exists()


def test_run_once_paper_mode_writes_state_then_status_and_balance(
    invoke: Callable[..., Any],
    tmp_path: Path,
    feed: RealFeedBroker,
    patch_broker: Callable[[BaseBroker], list[str]],
    daily_candles: list[Candle],
) -> None:
    names = patch_broker(feed)
    cfg = write_config(tmp_path)
    state_file = tmp_path / "state.json"

    result = invoke("status", "-c", str(cfg))
    assert result.exit_code == 0 and "상태 파일이 없습니다" in result.output
    result = invoke("balance", "-c", str(cfg))
    assert result.exit_code == 0 and "저장된 모의투자 상태가 없습니다" in result.output

    result = invoke("run", "-c", str(cfg), "--once")
    assert result.exit_code == 0, result.output
    assert names == ["upbit"]
    assert "모의투자(paper)" in result.output
    assert "=== 사이클 결과 ===" in result.output
    assert "모드/브로커 : paper / paper (시세: upbit)" in result.output
    assert "사이클      : 1회" in result.output
    assert feed.calls["get_candles"] >= 1 and feed.calls["get_ticker"] >= 1
    assert feed.closed

    assert state_file.is_file()
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["version"] == 1
    assert state["mode"] == "paper"
    assert state["broker"] == "paper"
    assert state["data_source"] == "upbit"
    assert state["strategy"] == "sma_cross"
    assert state["symbols"] == [BTC]
    assert state["cycles"] == 1
    last_complete = _complete(daily_candles, "1d")[-1].timestamp
    assert state["last_candle_ts"][BTC] == last_complete.isoformat()
    paper = state["paper_broker"]
    assert paper["initial_cash"] == INITIAL_CASH
    assert paper["quote_currency"] == "KRW"
    # 전략이 마지막 완성 캔들에서 낸 신호로 포지션 유무를 결정적으로 검증한다 (캔들 구간이 고정된 실데이터).
    strategy = create_strategy("sma_cross", {"fast": 5, "slow": 20})
    expected_signal = strategy.generate_signal(BTC, candles_to_df(_complete(daily_candles, "1d")[-60:]))
    if expected_signal.action == SignalAction.BUY:
        assert BTC in state["positions"], "매수 신호인데 포지션이 없다"
        assert paper["cash"] < INITIAL_CASH
        assert BTC in result.output
    else:
        assert state["positions"] == {}, "매수 신호가 아닌데 포지션이 생겼다"
        assert paper["cash"] == INITIAL_CASH

    result = invoke("status", "-c", str(cfg))
    assert result.exit_code == 0, result.output
    assert "엔진 상태" in result.output
    assert BTC in result.output
    assert "paper / paper (시세: upbit)" in result.output
    assert re.search(r"사이클 / 일자\s+1 /", result.output)

    result = invoke("balance", "-c", str(cfg))
    assert result.exit_code == 0, result.output
    assert "모의투자 계좌" in result.output
    assert "KRW" in result.output
    assert "총 자산 평가액" in result.output
    if state["positions"]:
        assert BTC in result.output

    # 두 번째 사이클: 상태 복원 후 같은 캔들은 다시 평가하지 않는다
    result = invoke("run", "-c", str(cfg), "--once")
    assert result.exit_code == 0, result.output
    state2 = json.loads(state_file.read_text(encoding="utf-8"))
    assert state2["cycles"] == 1  # 사이클 수는 프로세스 기준 (복원 대상 아님)
    assert state2["last_candle_ts"] == state["last_candle_ts"]
    assert set(state2["positions"]) == set(state["positions"])
    assert state2["paper_broker"]["cash"] == pytest.approx(paper["cash"])
    assert "새 캔들" not in result.output or state["last_candle_ts"][BTC] not in result.output


def test_run_live_once_with_yes_skips_countdown(
    invoke: Callable[..., Any],
    tmp_path: Path,
    feed: RealFeedBroker,
    patch_broker: Callable[[BaseBroker], list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # "실거래 어댑터" 자리에 실제 캔들을 쓰는 PaperBroker 를 넣어 배선만 검증한다 (실제 주문 없음)
    live_broker = PaperBroker(initial_cash=INITIAL_CASH, quote_currency="KRW", data_source=feed)
    names = patch_broker(live_broker)
    sleeps: list[float] = []
    monkeypatch.setattr(cli, "_sleep", lambda s: sleeps.append(s))
    cfg = write_config(tmp_path, mode="live")
    result = invoke("run", "-c", str(cfg), "--live", "--once", "--yes")
    assert result.exit_code == 0, result.output
    assert names == ["upbit"]
    assert "실거래 (LIVE)" in result.output
    assert "초 후 실거래를 시작합니다" not in result.output
    assert sleeps == []
    assert "=== 사이클 결과 ===" in result.output
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["mode"] == "live" and state["cycles"] == 1


def test_run_live_countdown_and_interrupt(
    invoke: Callable[..., Any],
    tmp_path: Path,
    feed: RealFeedBroker,
    patch_broker: Callable[[BaseBroker], list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_broker(PaperBroker(initial_cash=INITIAL_CASH, quote_currency="KRW", data_source=feed))
    sleeps: list[float] = []
    monkeypatch.setattr(cli, "_sleep", lambda s: sleeps.append(s))
    cfg = write_config(tmp_path, mode="live")
    result = invoke("run", "-c", str(cfg), "--live", "--once")
    assert result.exit_code == 0, result.output
    assert sleeps == [1.0] * cli.LIVE_COUNTDOWN_SEC
    assert f"{cli.LIVE_COUNTDOWN_SEC}초 후 실거래를 시작합니다" in result.output
    assert "실거래 시작" in result.output

    def interrupt(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_sleep", interrupt)
    (tmp_path / "state.json").unlink()
    result = invoke("run", "-c", str(cfg), "--live", "--once")
    assert result.exit_code == 130
    assert "중단" in result.output
    assert not (tmp_path / "state.json").exists()  # 카운트다운 중 취소 → 엔진 시작 안 함


# ============================================================================ balance / status
def test_balance_live_queries_broker_account(
    invoke: Callable[..., Any],
    tmp_path: Path,
    feed: RealFeedBroker,
    patch_broker: Callable[[BaseBroker], list[str]],
    daily_candles: list[Candle],
) -> None:
    account = PaperBroker(initial_cash=INITIAL_CASH, quote_currency="KRW", fee_pct=FEE, data_source=feed)
    qty = 0.01
    account.place_order(BTC, OrderSide.BUY, qty)
    patch_broker(account)
    cfg = write_config(tmp_path)
    result = invoke("balance", "-c", str(cfg), "--live")
    assert result.exit_code == 0, result.output
    assert "upbit 계좌" in result.output
    assert "KRW" in result.output and "BTC" in result.output
    assert BTC in result.output
    assert f"{account.get_equity([BTC]):,.0f}" in result.output
    assert "총 자산 평가액" in result.output


def test_balance_live_without_keys_fails_cleanly(
    invoke: Callable[..., Any],
    tmp_path: Path,
    feed: RealFeedBroker,
    patch_broker: Callable[[BaseBroker], list[str]],
) -> None:
    patch_broker(feed)
    cfg = write_config(tmp_path)
    result = invoke("balance", "-c", str(cfg), "--live")
    assert result.exit_code == 1
    assert "[오류] AuthenticationError" in result.output
    assert "UPBIT_ACCESS_KEY" in result.output
    assert feed.closed


def test_balance_paper_falls_back_to_cost_when_ticker_fails(
    invoke: Callable[..., Any],
    tmp_path: Path,
    feed: RealFeedBroker,
    patch_broker: Callable[[BaseBroker], list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paper = PaperBroker(initial_cash=INITIAL_CASH, quote_currency="KRW", data_source=feed)
    paper.place_order(BTC, OrderSide.BUY, 0.02)
    cfg = write_config(tmp_path)
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"version": 1, "paper_broker": paper.to_dict()}), encoding="utf-8")

    def broken(name: str, config: AppConfig) -> BaseBroker:
        raise BrokerError("시세 서버 접속 실패")

    monkeypatch.setattr(cli, "create_broker", broken)
    result = invoke("balance", "-c", str(cfg))
    assert result.exit_code == 0, result.output
    assert "[참고]" in result.output and "평균단가 기준" in result.output
    assert BTC in result.output


def test_status_shows_positions_pending_and_trades(invoke: Callable[..., Any], tmp_path: Path) -> None:
    cfg = write_config(tmp_path)
    state = {
        "version": 1,
        "mode": "paper",
        "broker": "paper",
        "data_source": "upbit",
        "strategy": "volatility_breakout",
        "strategy_params": {"k": 0.5},
        "interval": "1d",
        "symbols": [BTC, ETH],
        "started_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-02T00:00:00+00:00",
        "cycles": 42,
        "day": "2026-01-02",
        "positions": {
            BTC: {
                "symbol": BTC,
                "quantity": 0.01,
                "average_price": 100_000_000.0,
                "opened_at": "2026-01-01T00:00:00+00:00",
                "stop_loss": 97_000_000.0,
                "take_profit": None,
                "highest_price": 101_000_000.0,
                "meta": {"entry_reason": "테스트 진입"},
            }
        },
        "pending_breakouts": {ETH: {"trigger": 5_000_000.0, "expires": "2026-01-03T00:00:00+00:00"}},
        "last_candle_ts": {BTC: "2026-01-01T00:00:00+00:00"},
        "trades": [
            {
                "symbol": BTC,
                "side": "buy",
                "quantity": 0.01,
                "entry_price": 90_000_000.0,
                "exit_price": 95_000_000.0,
                "entry_time": "2025-12-30T00:00:00+00:00",
                "exit_time": "2025-12-31T00:00:00+00:00",
                "fee": 10.0,
                "reason": "최대 보유 기간 만료",
            }
        ],
        "risk": {"version": 1, "daily_pnl": 12345.0, "day_start_equity": 10_000_000.0, "daily_trades": 1},
        "paper_broker": {
            "cash": 9_000_000.0,
            "initial_cash": 10_000_000.0,
            "quote_currency": "KRW",
            "orders": [],
            "trades": [],
        },
    }
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    result = invoke("status", "-c", str(cfg))
    assert result.exit_code == 0, result.output
    assert "volatility_breakout" in result.output
    assert "42 / 2026-01-02" in result.output
    assert "테스트 진입" in result.output
    assert "돌파 대기: KRW-ETH 트리거 5,000,000" in result.output
    assert "마지막 캔들: KRW-BTC=2026-01-01T00:00:00+00:00" in result.output
    assert "최대 보유 기간 만료" in result.output
    assert "12,345" in result.output
    assert "9,000,000" in result.output


def test_status_tolerates_corrupt_state(invoke: Callable[..., Any], tmp_path: Path) -> None:
    cfg = write_config(tmp_path)
    (tmp_path / "state.json").write_text("{not json", encoding="utf-8")
    result = invoke("status", "-c", str(cfg))
    assert result.exit_code == 0, result.output
    assert "상태 파일이 없습니다" in result.output  # 손상 파일은 StateStore 가 격리하고 빈 상태
    assert list(tmp_path.glob("state.json.corrupt-*"))


# ============================================================================ ccxt sandbox 와 시세 전용 사용
def test_market_data_only_disables_ccxt_sandbox_but_live_keeps_it(
    invoke: Callable[..., Any],
    tmp_path: Path,
    daily_candles: list[Candle],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """paper/backtest/download 는 주문을 내지 않으므로 binance/ccxt 는 sandbox 를 끄고 실제 시세를 받는다."""
    pair = "BTC/USDT"
    feed = RealFeedBroker({(pair, "1d"): daily_candles}, name="binance")
    seen: list[AppConfig] = []

    def fake_create(name: str, config: AppConfig) -> BaseBroker:
        seen.append(config)
        return feed

    monkeypatch.setattr(cli, "create_broker", fake_create)
    common: dict[str, Any] = {
        "broker": {"name": "binance", "sandbox": True},
        "symbols": [pair],
        "paper": {"quote_currency": "USDT"},
        "risk": {"min_order_value": 10},
    }
    cfg = write_config(tmp_path, **common)

    result = invoke("run", "-c", str(cfg), "--once")
    assert result.exit_code == 0, result.output
    assert seen[-1].broker.sandbox is False
    assert seen[-1].broker.name == "binance"
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["paper_broker"]["quote_currency"] == "USDT"

    result = invoke("backtest", "-c", str(cfg))  # auto → 브로커 다운로드
    assert result.exit_code == 0, result.output
    assert seen[-1].broker.sandbox is False
    assert "USDT" in result.output
    (tmp_path / "data" / "binance" / "BTC_USDT_1d.csv").unlink()

    result = invoke("download", "-c", str(cfg), "--start", _date(daily_candles[0].timestamp))
    assert result.exit_code == 0, result.output
    assert seen[-1].broker.sandbox is False

    # --live: 설정의 sandbox 를 그대로 쓴다 (테스트넷 주문)
    live_broker = PaperBroker(initial_cash=10_000.0, quote_currency="USDT", data_source=feed)

    def fake_create_live(name: str, config: AppConfig) -> BaseBroker:
        seen.append(config)
        return live_broker

    monkeypatch.setattr(cli, "create_broker", fake_create_live)
    cfg_live = write_config(
        tmp_path,
        name="live.yaml",
        mode="live",
        engine={"state_file": str(tmp_path / "live_state.json")},
        **common,
    )
    result = invoke("run", "-c", str(cfg_live), "--live", "--yes", "--once")
    assert result.exit_code == 0, result.output
    assert seen[-1].broker.sandbox is True
    assert "실거래 (LIVE)" in result.output
