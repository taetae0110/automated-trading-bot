"""config/ 예시 설정과 .env.example, 문서 일관성 검증.

- 모든 예시 YAML 이 tradingbot.config.load_config 로 읽히고, 모드/샌드박스/전략 파라미터가 유효해야 한다.
- 모든 키에 한국어 설명 주석이 있어야 한다.
- .env.example 은 Credentials.from_env 가 읽는 환경변수를 전부 (빈 값으로) 나열해야 한다.
- 저장소에 샘플 시세 파일이 없어야 한다.
- README/docs 의 설명(성과 지표 개수, 일일 요약 시점, KIS_HTS_ID 미사용, conftest 실데이터) 이 코드와 맞아야 한다.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from tradingbot.backtest import METRIC_KEYS
from tradingbot.brokers import available_brokers
from tradingbot.config import Credentials, load_config
from tradingbot.models import INTERVAL_SECONDS
from tradingbot.strategies import available_strategies, create_strategy
from tradingbot.utils.timeutil import parse_date

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
EXAMPLES_DIR = CONFIG_DIR / "examples"
ENV_EXAMPLE = ROOT / ".env.example"
PACKAGE_DIR = ROOT / "tradingbot"
DOCS_DIR = ROOT / "docs"
README = ROOT / "README.md"

#: 파일 → (broker.name, strategy.name)
EXPECTED: dict[str, tuple[str, str]] = {
    "config.example.yaml": ("upbit", "sma_cross"),
    "upbit_volatility_breakout.yaml": ("upbit", "volatility_breakout"),
    "binance_sma_cross.yaml": ("binance", "sma_cross"),
    "kis_rsi.yaml": ("kis", "rsi"),
    "alpaca_macd.yaml": ("alpaca", "macd"),
}
#: 브로커별 네이티브 심볼 표기
SYMBOL_PATTERN: dict[str, str] = {
    "upbit": r"^[A-Z]+-[A-Z0-9]+$",
    "binance": r"^[A-Z0-9]+/[A-Z0-9]+$",
    "kis": r"^\d{6}$",
    "alpaca": r"^[A-Z][A-Z.]*$",
}
#: 브로커별 결제 통화
QUOTE_CURRENCY: dict[str, str] = {"upbit": "KRW", "binance": "USDT", "kis": "KRW", "alpaca": "USD"}
#: Credentials.from_env 가 읽는 환경변수 (대표 이름). 별칭은 주석으로만 둔다.
PRIMARY_ENV_VARS = {
    "UPBIT_ACCESS_KEY",
    "UPBIT_SECRET_KEY",
    "CCXT_API_KEY",
    "CCXT_SECRET",
    "CCXT_PASSWORD",
    "KIS_APP_KEY",
    "KIS_APP_SECRET",
    "KIS_ACCOUNT_NO",
    "KIS_HTS_ID",
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "SLACK_WEBHOOK_URL",
    "DISCORD_WEBHOOK_URL",
}
ALIAS_ENV_VARS = {
    "BINANCE_API_KEY",
    "BINANCE_SECRET_KEY",
    "BINANCE_API_SECRET",
    "APCA_API_KEY_ID",
    "APCA_API_SECRET_KEY",
}
HANGUL = re.compile(r"[가-힣]")
KEY_LINE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*):")

ALL_CONFIGS = [CONFIG_DIR / "config.example.yaml", *sorted(EXAMPLES_DIR.glob("*.yaml"))]


def _config_id(p: Path) -> str:
    return p.name


def test_expected_example_files_exist() -> None:
    assert {p.name for p in ALL_CONFIGS} == set(EXPECTED)
    assert ENV_EXAMPLE.is_file()


@pytest.mark.parametrize("path", ALL_CONFIGS, ids=_config_id)
def test_example_config_loads_and_is_safe(path: Path) -> None:
    cfg = load_config(path)
    broker, strategy = EXPECTED[path.name]
    assert cfg.mode == "paper"
    assert cfg.is_live is False
    assert cfg.broker.sandbox is True
    assert cfg.broker.name == broker
    assert cfg.broker.name in available_brokers()
    assert cfg.strategy.name == strategy
    assert cfg.strategy.name in available_strategies()
    assert cfg.interval in INTERVAL_SECONDS
    assert cfg.paper.quote_currency == QUOTE_CURRENCY[broker]
    for symbol in cfg.symbols:
        assert re.match(SYMBOL_PATTERN[broker], symbol), f"{path.name}: {symbol!r} 는 {broker} 표기가 아님"
    if cfg.backtest.start is not None:
        parse_date(cfg.backtest.start)
    if cfg.backtest.end is not None:
        parse_date(cfg.backtest.end)


@pytest.mark.parametrize("path", ALL_CONFIGS, ids=_config_id)
def test_example_strategy_params_are_valid(path: Path) -> None:
    cfg = load_config(path)
    strat = create_strategy(cfg.strategy.name, cfg.strategy.params)  # 모르는 파라미터면 ConfigError
    assert set(cfg.strategy.params) <= set(strat.default_params)
    assert cfg.engine.candle_limit >= strat.warmup, (
        "candle_limit 이 전략 warmup 보다 작으면 신호가 나오지 않는다"
    )


@pytest.mark.parametrize("path", ALL_CONFIGS, ids=_config_id)
def test_example_risk_settings_are_sane(path: Path) -> None:
    cfg = load_config(path)
    assert 0 < cfg.risk.max_position_pct <= 1
    assert cfg.risk.max_position_pct * cfg.risk.max_positions <= 1.0 + 1e-9, (
        "동시 보유 비중 합이 100% 를 넘는다"
    )
    assert cfg.risk.stop_loss_pct is not None and 0 < cfg.risk.stop_loss_pct < 0.2
    assert cfg.risk.max_daily_loss_pct is not None and 0 < cfg.risk.max_daily_loss_pct < 0.2
    assert cfg.paper.initial_cash > 0
    assert cfg.backtest.initial_cash > 0


@pytest.mark.parametrize("path", ALL_CONFIGS, ids=_config_id)
def test_every_key_has_korean_comment(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert HANGUL.search(lines[0] + lines[1]), "파일 머리에 한국어 설명이 있어야 한다"
    missing: list[str] = []
    prev_comment = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            prev_comment = HANGUL.search(stripped) is not None
            continue
        m = KEY_LINE.match(line)
        if m:
            inline = "#" in line and HANGUL.search(line.split("#", 1)[1]) is not None
            if not (inline or prev_comment):
                missing.append(stripped)
        prev_comment = False
    assert not missing, f"{path.name}: 한국어 주석이 없는 키: {missing}"


def test_env_example_lists_every_credential_variable() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assigned: dict[str, str] = {}
    commented_aliases: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            m = re.match(r"^#\s*([A-Z_]+)=\s*$", line)
            if m:
                commented_aliases.add(m.group(1))
            continue
        key, sep, value = line.partition("=")
        assert sep == "=", f"잘못된 줄: {raw!r}"
        assigned[key.strip()] = value.strip()
    assert set(assigned) == PRIMARY_ENV_VARS
    assert all(v == "" for v in assigned.values()), "예시 파일의 값은 모두 비어 있어야 한다"
    assert commented_aliases <= ALIAS_ENV_VARS
    assert HANGUL.search(text)
    for alias in ALIAS_ENV_VARS:  # 별칭은 최소한 설명에 언급
        assert alias in text


def test_env_example_yields_no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    clean = {k: v for k, v in os.environ.items() if k not in PRIMARY_ENV_VARS | ALIAS_ENV_VARS}
    monkeypatch.setattr(os, "environ", clean)  # load_dotenv 가 실제 환경을 더럽히지 않도록
    creds = Credentials.from_env(dotenv_path=ENV_EXAMPLE)
    assert all(value is None for value in creds.model_dump().values())
    with pytest.raises(Exception, match="UPBIT_ACCESS_KEY"):
        creds.require("upbit_access_key")


@pytest.mark.parametrize("path", ALL_CONFIGS, ids=_config_id)
def test_daily_summary_comment_states_utc_boundary(path: Path) -> None:
    """엔진은 UTC 날짜가 바뀔 때 일일 요약을 보낸다 (= 09:00 KST, 국내주식은 개장 시점). '장 마감 후' 같은 설명을 막는다."""
    for line in path.read_text(encoding="utf-8").splitlines():
        if re.match(r"\s*daily_summary:", line):
            comment = line.split("#", 1)[1] if "#" in line else ""
            assert "UTC" in comment, f"{path.name}: 일일 요약 시점은 UTC 자정이다: {line.strip()}"
            break
    else:
        pytest.fail(f"{path.name}: notify.daily_summary 키가 없다")


def test_kis_hts_id_is_documented_as_unused() -> None:
    """KIS_HTS_ID 는 Credentials 가 읽지만 어떤 어댑터도 쓰지 않는다. 사용처가 생기면 문서의 '미사용' 표기를 바꿔야 한다."""
    users = sorted(
        p.relative_to(ROOT).as_posix()
        for p in PACKAGE_DIR.rglob("*.py")
        if p.name != "config.py" and "hts_id" in p.read_text(encoding="utf-8").lower()
    )
    assert users == [], f"KIS_HTS_ID 사용처가 생겼다 — 문서의 '현재 미사용' 표기를 갱신하라: {users}"
    env_lines = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    assert "미사용" in env_lines[env_lines.index("KIS_HTS_ID=") - 1]
    for doc in (README, DOCS_DIR / "BROKERS.md", EXAMPLES_DIR / "kis_rsi.yaml"):
        text = doc.read_text(encoding="utf-8")
        assert "KIS_HTS_ID" in text and "미사용" in text, doc.name
        assert "일부 조회 API 에서 필요" not in text, doc.name


def test_readme_metric_count_matches_backtester() -> None:
    m = re.search(r"(\d+)개 성과 지표", README.read_text(encoding="utf-8"))
    assert m, "README 에 'N개 성과 지표' 문구가 없다"
    assert int(m.group(1)) == len(METRIC_KEYS)


def test_no_synthetic_candle_generator_is_referenced() -> None:
    """계약 문서(ARCHITECTURE.md) 가 존재하지 않는 합성 캔들 생성기를 고정 파일로 안내하면 안 된다 (가짜 시세 금지)."""
    needle = "make_" + "candles"  # 이 파일 자신이 걸리지 않게 조합
    hits = [
        p.relative_to(ROOT).as_posix()
        for base, pattern in ((DOCS_DIR, "*.md"), (ROOT / "tests", "*.py"), (PACKAGE_DIR, "*.py"))
        for p in base.rglob(pattern)
        if needle in p.read_text(encoding="utf-8")
    ]
    assert hits == [] and needle not in README.read_text(encoding="utf-8")
    arch = (DOCS_DIR / "ARCHITECTURE.md").read_text(encoding="utf-8").splitlines()
    fixed_file_line = next(line for line in arch if line.startswith("- `tests/conftest.py`"))
    assert "실제 캔들" in fixed_file_line and "합성 데이터" not in fixed_file_line


def test_no_sample_market_data_in_repo() -> None:
    """사용자 규칙: 샘플/데모 시세 파일을 저장소에 두지 않는다."""
    data_like = ("*.csv", "*.parquet", "*.feather", "*.pkl", "*.h5")
    offenders: list[Path] = []
    for base in (CONFIG_DIR, ROOT / "tradingbot", ROOT / "tests"):
        for pattern in data_like:
            offenders.extend(base.rglob(pattern))
    assert offenders == []
    assert not (CONFIG_DIR / "data").exists()
