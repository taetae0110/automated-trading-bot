"""설정 스키마 (YAML + 환경변수).

- 전략/리스크/엔진 설정은 YAML 파일 (config/config.yaml) 에서 읽는다.
- API 키/시크릿 등 비밀은 **환경변수(.env)** 에서만 읽는다. YAML 에 넣지 말 것.

사용:
    cfg = load_config("config/config.yaml")
    creds = Credentials.from_env()
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator

from tradingbot.exceptions import ConfigError
from tradingbot.models import INTERVAL_SECONDS

Mode = Literal["backtest", "paper", "live"]


class BrokerConfig(BaseModel):
    """실제 시세/주문을 담당할 브로커."""

    name: str = "upbit"  # paper | upbit | binance | ccxt | kis | alpaca
    # ccxt 전용: 거래소 id (binance, bybit, okx, bithumb ...). name 이 "binance" 면 자동으로 binance.
    exchange_id: str | None = None
    # KIS/Alpaca: 모의투자 서버 사용 여부. 실거래 전에 반드시 모의투자로 검증할 것.
    sandbox: bool = True
    # 주문 후 체결 확인 대기 (초)
    fill_timeout_sec: float = 30.0
    extra: dict[str, Any] = Field(default_factory=dict)


class StrategyConfig(BaseModel):
    name: str = "sma_cross"
    params: dict[str, Any] = Field(default_factory=dict)


class RiskConfig(BaseModel):
    """리스크 한도. 비율은 모두 0~1 소수 (0.02 = 2%)."""

    max_position_pct: float = Field(0.2, gt=0, le=1, description="종목당 최대 투자 비중 (총자산 대비)")
    max_positions: int = Field(5, ge=1, description="동시 보유 가능한 최대 종목 수")
    stop_loss_pct: float | None = Field(0.03, ge=0, description="손절 비율. None 이면 사용 안 함")
    take_profit_pct: float | None = Field(None, ge=0, description="익절 비율. None 이면 사용 안 함")
    trailing_stop_pct: float | None = Field(None, ge=0, description="고점 대비 추적 손절 비율")
    max_daily_loss_pct: float | None = Field(
        0.05, ge=0, description="일일 누적 손실 한도. 초과 시 당일 신규 진입 중단"
    )
    min_order_value: float = Field(0.0, ge=0, description="최소 주문 금액(quote). 0 이면 브로커 기본값")
    # 총자산 대신 고정 금액을 기준으로 포지션 크기를 정할 때 (예: 100만원만 운용)
    capital_limit: float | None = Field(None, gt=0)

    @field_validator("stop_loss_pct", "take_profit_pct", "trailing_stop_pct", "max_daily_loss_pct")
    @classmethod
    def _zero_is_none(cls, v: float | None) -> float | None:
        return None if v == 0 else v


class PaperConfig(BaseModel):
    """모의투자(PaperBroker) 설정."""

    initial_cash: float = Field(10_000_000, gt=0)
    quote_currency: str = "KRW"
    fee_pct: float = Field(0.0005, ge=0, description="매수/매도 각각 적용되는 수수료율")
    slippage_pct: float = Field(0.0005, ge=0, description="시장가 체결 슬리피지")


class EngineConfig(BaseModel):
    poll_seconds: float = Field(30, gt=0, description="시세 확인 주기")
    candle_limit: int = Field(300, ge=10, description="전략에 넘길 최근 캔들 개수")
    state_file: str = "data/state.json"
    # 장 마감 시 포지션 전량 청산 (주식 데이트레이딩용)
    close_positions_at_market_close: bool = False
    # 시작 시 거래소 포지션을 상태 파일과 동기화
    sync_positions_on_start: bool = True
    # 이 시간 동안 캔들이 갱신되지 않으면 경고
    stale_data_minutes: int = 30


class TelegramConfig(BaseModel):
    enabled: bool = False
    # 토큰/채팅ID 는 환경변수 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID


class SlackConfig(BaseModel):
    enabled: bool = False
    # 환경변수 SLACK_WEBHOOK_URL


class DiscordConfig(BaseModel):
    enabled: bool = False
    # 환경변수 DISCORD_WEBHOOK_URL


class NotifyConfig(BaseModel):
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    slack: SlackConfig = Field(default_factory=SlackConfig)
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    notify_on_signal: bool = False
    notify_on_trade: bool = True
    notify_on_error: bool = True
    daily_summary: bool = True


class BacktestConfig(BaseModel):
    start: str | None = None  # "YYYY-MM-DD"
    end: str | None = None
    data_dir: str = "data/candles"
    initial_cash: float = Field(10_000_000, gt=0)
    fee_pct: float = Field(0.0005, ge=0)
    slippage_pct: float = Field(0.0005, ge=0)
    # 시장가 체결 시점: next_open(다음 캔들 시가, 현실적) | close(신호 캔들 종가)
    fill_on: Literal["next_open", "close"] = "next_open"
    # 결과 리포트 저장 경로
    report_dir: str = "reports"


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str | None = "logs/tradingbot.log"


class AppConfig(BaseModel):
    mode: Mode = "paper"
    broker: BrokerConfig = Field(default_factory=BrokerConfig)
    symbols: list[str] = Field(default_factory=lambda: ["KRW-BTC"])
    interval: str = "1h"
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    paper: PaperConfig = Field(default_factory=PaperConfig)
    engine: EngineConfig = Field(default_factory=EngineConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @field_validator("interval")
    @classmethod
    def _check_interval(cls, v: str) -> str:
        if v not in INTERVAL_SECONDS:
            raise ValueError(f"지원하지 않는 interval {v!r}. 가능: {', '.join(INTERVAL_SECONDS)}")
        return v

    @field_validator("symbols")
    @classmethod
    def _check_symbols(cls, v: list[str]) -> list[str]:
        cleaned = [s.strip() for s in v if s and s.strip()]
        if not cleaned:
            raise ValueError("symbols 는 최소 1개 이상이어야 합니다")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("symbols 에 중복이 있습니다")
        return cleaned

    @model_validator(mode="after")
    def _check_mode(self) -> AppConfig:
        if self.mode == "live" and self.broker.name == "paper":
            raise ValueError("mode=live 인데 broker.name=paper 입니다. 실거래 브로커를 지정하세요")
        return self

    @property
    def is_live(self) -> bool:
        return self.mode == "live"


class Credentials(BaseModel):
    """환경변수에서 읽는 API 자격 증명. 값이 없으면 None."""

    upbit_access_key: str | None = None
    upbit_secret_key: str | None = None

    ccxt_api_key: str | None = None
    ccxt_secret: str | None = None
    ccxt_password: str | None = None  # OKX 등 passphrase

    kis_app_key: str | None = None
    kis_app_secret: str | None = None
    kis_account_no: str | None = None  # "12345678-01" (계좌번호-상품코드) 또는 "1234567801"
    kis_hts_id: str | None = None

    alpaca_api_key: str | None = None
    alpaca_secret_key: str | None = None

    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    slack_webhook_url: str | None = None
    discord_webhook_url: str | None = None

    @classmethod
    def from_env(cls, dotenv_path: str | os.PathLike[str] | None = None) -> Credentials:
        load_dotenv(dotenv_path=dotenv_path, override=False)
        env = os.environ

        def g(*names: str) -> str | None:
            for n in names:
                v = env.get(n)
                if v is not None and v.strip() != "":
                    return v.strip()
            return None

        return cls(
            upbit_access_key=g("UPBIT_ACCESS_KEY"),
            upbit_secret_key=g("UPBIT_SECRET_KEY"),
            ccxt_api_key=g("CCXT_API_KEY", "BINANCE_API_KEY"),
            ccxt_secret=g("CCXT_SECRET", "BINANCE_SECRET_KEY", "BINANCE_API_SECRET"),
            ccxt_password=g("CCXT_PASSWORD"),
            kis_app_key=g("KIS_APP_KEY"),
            kis_app_secret=g("KIS_APP_SECRET"),
            kis_account_no=g("KIS_ACCOUNT_NO"),
            kis_hts_id=g("KIS_HTS_ID"),
            alpaca_api_key=g("ALPACA_API_KEY", "APCA_API_KEY_ID"),
            alpaca_secret_key=g("ALPACA_SECRET_KEY", "APCA_API_SECRET_KEY"),
            telegram_bot_token=g("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=g("TELEGRAM_CHAT_ID"),
            slack_webhook_url=g("SLACK_WEBHOOK_URL"),
            discord_webhook_url=g("DISCORD_WEBHOOK_URL"),
        )

    def require(self, *fields: str) -> None:
        missing = [f for f in fields if getattr(self, f) in (None, "")]
        if missing:
            env_names = ", ".join(f.upper() for f in missing)
            raise ConfigError(f"환경변수가 필요합니다: {env_names} (.env 파일 또는 export 로 설정)")


def load_config(
    path: str | os.PathLike[str] | None = None, overrides: dict[str, Any] | None = None
) -> AppConfig:
    """YAML 설정을 읽어 AppConfig 를 만든다. path 가 None 이면 기본값."""
    data: dict[str, Any] = {}
    if path is not None:
        p = Path(path)
        if not p.exists():
            raise ConfigError(f"설정 파일을 찾을 수 없습니다: {p}")
        try:
            loaded = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"YAML 파싱 오류 ({p}): {e}") from e
        if not isinstance(loaded, dict):
            raise ConfigError(f"설정 파일 최상위는 매핑이어야 합니다: {p}")
        data = loaded
    if overrides:
        data = _deep_merge(data, overrides)
    try:
        return AppConfig.model_validate(data)
    except Exception as e:  # pydantic.ValidationError 포함
        raise ConfigError(f"설정 오류: {e}") from e


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if v is None:
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out
