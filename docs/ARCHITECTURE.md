# 아키텍처 / 모듈 계약 (ARCHITECTURE)

이 문서는 모든 모듈이 따라야 하는 **계약(contract)** 이다. 병렬로 구현되는 모듈들이 서로 맞물리도록
공개 API 를 여기서 고정한다. 계약 파일(아래 "고정 파일")은 구현자가 수정하지 않는다.
변경이 필요하면 구현 보고서에 "계약 변경 요청" 으로 적는다.

## 고정 파일 (수정 금지)
- `tradingbot/models.py` — Candle, Signal, Order, Position, Balance, Trade, enum, INTERVAL_SECONDS
- `tradingbot/exceptions.py`
- `tradingbot/config.py` — AppConfig(YAML), Credentials(env)
- `tradingbot/brokers/base.py`, `tradingbot/brokers/__init__.py` (레지스트리)
- `tradingbot/strategies/base.py`, `tradingbot/strategies/__init__.py` (레지스트리)
- `tradingbot/utils/http.py`, `tradingbot/utils/timeutil.py`
- `tests/conftest.py` — `make_candles()` 합성 데이터

## 디렉터리
```
tradingbot/
  models.py  config.py  exceptions.py  cli.py  logging_setup.py
  brokers/   base.py  paper.py  upbit.py  ccxt_broker.py  kis.py  alpaca.py
  strategies/ base.py indicators.py sma_cross.py rsi.py bollinger.py macd.py volatility_breakout.py
  risk/      manager.py
  backtest/  engine.py  metrics.py
  engine/    trader.py  state.py
  notify/    base.py  telegram.py  slack.py  discord.py
  data/      store.py  yfinance_feed.py
  utils/     http.py  timeutil.py
tests/        pytest (네트워크 호출은 `responses` 또는 monkeypatch 로 모킹)
config/       config.example.yaml, examples/*.yaml
docs/         ARCHITECTURE.md, BROKERS.md(브로커별 키 발급/주의사항), STRATEGIES.md
```

## 전역 규약
- **데모/샘플/가짜 시세 데이터 절대 금지.** 저장소에 샘플 CSV 를 넣지 않고, 코드에 하드코딩된 가격·가짜 잔고·"데모 모드" 를
  만들지 않는다. 백테스트 데이터는 항상 실제 거래소(Upbit 공개 API, ccxt, KIS, Alpaca, yfinance)에서 내려받는다.
  테스트도 `tests/conftest.py` 가 Upbit 공개 API 에서 받아 캐시한 **실제 캔들**(`daily_df`, `candles_df`, `eth_daily_df`)을
  쓴다. 지표 단위 테스트의 아주 짧은 손계산 검증 벡터(예: [1,2,3,4,5] 의 SMA) 는 허용. 비공개 API(주문/잔고) 테스트는
  거래소 공식 문서의 응답 스키마를 그대로 따르는 HTTP 모킹만 허용하며, 모킹 데이터는 테스트 파일 안에만 둔다.
- Python 3.10+, 타입 힌트 필수, `from __future__ import annotations`.
- 모든 datetime 은 **UTC aware**. 거래소 응답의 KST/ET 는 즉시 UTC 로 변환.
- 심볼은 브로커 네이티브 표기 (Upbit `KRW-BTC`, ccxt `BTC/USDT`, KIS `005930`, Alpaca `AAPL`).
- 로깅: `logging.getLogger(__name__)`. 한국어 메시지 OK. 비밀값(키, 토큰) 절대 로그 금지.
- 네트워크 오류 → `BrokerError` 계열로 변환. 브로커 생성자는 **자격증명 없이도 생성 가능**해야 하며
  (공개 시세 조회용), 비공개 API 호출 시에만 `AuthenticationError("... 환경변수 필요")` 를 던진다.
- 각 브로커 어댑터는 `@classmethod from_config(cls, config: AppConfig) -> Self` 를 제공한다.
  내부에서 `Credentials.from_env()` 를 호출해 키를 읽는다.
- 테스트는 실제 네트워크를 호출하지 않는다 (`responses` 라이브러리 또는 세션 monkeypatch).
  단, Upbit 공개 API 는 네트워크가 되는 환경에서 수동 검증은 해도 된다.
- 수량/가격 반올림은 어댑터의 `round_quantity` / `round_price` 가 담당. 시장가 매수 금액 환산 시 수수료
  여유를 두어 잔고 초과가 나지 않게 한다.
- 롱(매수→매도)만 지원. 공매도/선물 없음.

## 캔들 DataFrame 규약
`tradingbot.strategies.base.candles_to_df(list[Candle]) -> DataFrame`
컬럼 `timestamp(UTC datetime64), open, high, low, close, volume` (float), RangeIndex, 오래된→최신.
전략 `prepare()` 가 추가하는 지표 컬럼 이름은 자유이나 소문자_스네이크.

## 1. 지표 (`strategies/indicators.py`)
pandas 전용, TA-Lib 없이 구현. 입력 `pd.Series`(close 등) 또는 DataFrame, 같은 인덱스의 Series 반환, 워밍업 구간은 NaN.
```python
def sma(s: pd.Series, period: int) -> pd.Series
def ema(s: pd.Series, period: int) -> pd.Series            # adjust=False
def rsi(s: pd.Series, period: int = 14) -> pd.Series        # Wilder 방식 (ewm alpha=1/period)
def macd(s, fast=12, slow=26, signal=9) -> tuple[pd.Series, pd.Series, pd.Series]  # (macd, signal, hist)
def bollinger(s, period=20, num_std=2.0) -> tuple[pd.Series, pd.Series, pd.Series]  # (mid, upper, lower), ddof=0
def true_range(df: pd.DataFrame) -> pd.Series
def atr(df: pd.DataFrame, period: int = 14) -> pd.Series   # Wilder
def crossover(a: pd.Series, b: pd.Series) -> pd.Series      # bool: a 가 b 를 상향 돌파한 행
def crossunder(a: pd.Series, b: pd.Series) -> pd.Series
```

## 2. 전략 (`strategies/*.py`) — `BaseStrategy` 구현
| name | class | default_params | 규칙 |
|---|---|---|---|
| `sma_cross` | `SMACrossStrategy` | `fast=10, slow=30` | 골든크로스 BUY, 데드크로스 SELL |
| `ema_cross` | `EMACrossStrategy` | `fast=12, slow=26` | EMA 버전 (sma_cross.py 안에 함께) |
| `rsi` | `RSIStrategy` | `period=14, oversold=30, overbought=70` | RSI 가 oversold 를 상향 돌파하면 BUY, overbought 를 하향 돌파하면 SELL |
| `bollinger` | `BollingerStrategy` | `period=20, num_std=2.0, mode="reversion"` | reversion: 종가가 하단밴드 아래에서 위로 복귀 시 BUY, 중심선 이상이면 SELL. breakout: 상단 돌파 BUY, 중심선 하향 이탈 SELL |
| `macd` | `MACDStrategy` | `fast=12, slow=26, signal=9` | MACD 가 시그널 상향 돌파 BUY, 하향 돌파 SELL |
| `volatility_breakout` | `VolatilityBreakoutStrategy` | `k=0.5, ma_period=0` | 아래 참고 |

- `warmup` = 가장 긴 기간 + 1 이상. `signal_at(i)` 는 `i < warmup-1` 또는 지표 NaN 이면 HOLD.
- `Signal.reason` 에 짧은 한국어 사유 (예: "SMA10 > SMA30 골든크로스"), `Signal.meta` 에 지표값.
- `Signal.strength` 는 기본 1.0.
- **변동성 돌파** (래리 윌리엄스, 일봉 기준):
  행 i(완성 캔들) 에서 `range = high_i - low_i`. `ma_period>0` 이고 `close_i < SMA(ma_period)` 면 HOLD.
  아니면 `Signal(action=BUY, order_type=OrderType.STOP, price=None, stop_offset=k*range, max_holding_bars=1, reason=...)`.
  즉 "다음 캔들 시가 + k*range 를 돌파하면 매수, 그 다음 캔들 시가에 매도". SELL 신호는 내지 않는다
  (청산은 max_holding_bars 로 처리). 포지션 보유 중 BUY 는 엔진/백테스터가 무시한다.

## 3. PaperBroker (`brokers/paper.py`) — 모의 체결 엔진 (백테스트 + 모의투자 공용)
```python
class PaperBroker(BaseBroker):
    name = "paper"
    def __init__(self, *, initial_cash: float, quote_currency: str = "KRW", fee_pct: float = 0.0005,
                 slippage_pct: float = 0.0005, data_source: BaseBroker | None = None,
                 asset_class: AssetClass = AssetClass.CRYPTO, min_order_value: float = 0.0) -> None
    @classmethod
    def from_config(cls, config: AppConfig, data_source: BaseBroker | None = None) -> "PaperBroker"
        # config.paper.* 사용. data_source 가 있으면 asset_class/quote 는 data_source 를 따른다.
    def mark_price(self, symbol: str, price: float) -> None    # 현재가 설정 (백테스터가 bar 마다 호출)
    def get_ticker(self, symbol) -> float   # mark 된 가격 > data_source.get_ticker > BrokerError
    def get_candles(...)                     # data_source 에 위임, 없으면 DataError
    def process_candle(self, symbol: str, candle: Candle) -> list[Order]
        # 미체결 LIMIT/STOP 주문을 캔들로 체결 판정 (백테스트). 체결된 주문 목록 반환.
        # STOP BUY: trigger = candle.open + stop_offset (price None 일 때) 또는 price.
        #   open >= trigger → open 에 체결, elif high >= trigger → trigger 에 체결. 슬리피지 적용.
        # LIMIT BUY: low <= price → price 체결. LIMIT SELL: high >= price → price 체결.
        # STOP SELL: open <= trigger → open, elif low <= trigger → trigger.
    def place_order(symbol, side, quantity, order_type=MARKET, price=None, *, stop_offset=None) -> Order
        # MARKET: 즉시 get_ticker()*(1±slippage) 로 체결, 수수료 차감. 잔고 부족 → InsufficientFunds.
        # LIMIT/STOP: OPEN 상태로 보관 (process_candle 또는 check_pending(price) 에서 체결).
        # stop_offset 은 BaseBroker 시그니처에 없는 키워드 전용 확장 (엔진은 STOP 을 직접 흉내내므로 백테스터만 사용).
    def check_pending(self, symbol: str, price: float) -> list[Order]  # 현재가 기준 LIMIT/STOP 체결 판정 (모의투자 폴링용)
    def get_balances / get_positions / cancel_order / get_order / get_open_orders / quote_currency / base_currency
    def get_equity(self, symbols=None) -> float   # cash + Σ position * 현재가
    def to_dict() -> dict / @classmethod from_dict(d, data_source=None)   # 상태 저장/복원
    @property cash -> float ; @property trades -> list[Trade]   # 청산 완료 Trade 자동 생성 (평균단가 기준, 수수료 포함)
```
체결 시 `Position.average_price` 는 수수료 제외 평균단가, 수수료는 cash 에서 차감. 매도 체결 시 `Trade` 를 생성해 `trades` 에 추가 (fee = 매수+매도 수수료 비례 배분).

## 4. RiskManager (`risk/manager.py`)
```python
class RiskManager:
    def __init__(self, config: RiskConfig) -> None
    def can_open(self, open_positions: int, now: datetime) -> tuple[bool, str]
        # max_positions, 일일 손실 한도(당일 realized pnl / 당일 시작 자산 <= -max_daily_loss_pct → 거부) 검사
    def position_size(self, *, equity: float, cash: float, price: float, signal: Signal,
                      open_positions: int, min_order_value: float = 0.0, fee_pct: float = 0.0) -> float
        # budget = min(equity, capital_limit) * max_position_pct * signal.strength ; budget = min(budget, cash*(1-fee_pct)*0.999)
        # qty = budget / price ; budget < max(min_order_value, config.min_order_value) → 0.0
    def apply_entry(self, position: Position, signal: Signal, fill_price: float) -> None
        # position.stop_loss / take_profit 를 signal 값 또는 % 로 설정, highest_price = fill_price,
        # position.meta["max_holding_bars"], ["entry_reason"], ["entry_bar_ts"] 기록
    def check_exit(self, position: Position, price: float) -> Signal | None
        # highest_price 갱신 → 순서: stop_loss → trailing_stop → take_profit. 해당 시 SELL MARKET Signal(reason 명시, meta["exit_type"] in {"stop_loss","trailing_stop","take_profit"})
    def record_trade(self, trade: Trade) -> None        # 당일 realized pnl 누적 (UTC 날짜가 바뀌면 리셋)
    def start_day(self, equity: float, now: datetime) -> None   # 당일 시작 자산 기록
    @property daily_pnl -> float ; def to_dict() / from_dict()
```

## 5. 백테스터 (`backtest/engine.py`, `backtest/metrics.py`)
```python
@dataclass
class BacktestResult:
    strategy: str; params: dict; symbols: list[str]; interval: str
    start: datetime; end: datetime; initial_cash: float; final_equity: float
    equity_curve: pd.Series        # index=timestamp(UTC), value=equity (각 bar 종가 기준)
    trades: list[Trade]; orders: list[Order]
    metrics: dict[str, float]      # metrics.compute_metrics 결과
    def summary(self) -> str       # 사람이 읽는 한국어 요약 (rich 없이 plain text)
    def to_dict(self) -> dict      # JSON 저장용 (equity_curve 는 [[iso, value], ...])

class Backtester:
    def __init__(self, strategy: BaseStrategy, risk: RiskManager, *, initial_cash: float = 10_000_000,
                 fee_pct: float = 0.0005, slippage_pct: float = 0.0005, fill_on: str = "next_open",
                 quote_currency: str = "KRW", interval: str = "1d") -> None
    def run(self, data: dict[str, pd.DataFrame]) -> BacktestResult   # {symbol: 캔들 df}
```
알고리즘 (bar 단위, 타임스탬프 합집합을 시간순으로):
1. 각 심볼 df 를 `strategy.prepare()` 로 1회 준비.
2. bar 시작: `broker.mark_price(sym, open)`; 이전 bar 에서 생성된 대기 주문(MARKET→open 체결, STOP/LIMIT→`process_candle`) 처리;
   `max_holding_bars` 만료 포지션은 open 에 시장가 청산; 리스크 청산: `low <= stop_loss` → stop_loss 가격(갭이면 open) 체결,
   `high >= take_profit` → take_profit 체결 (손절 우선). 추적손절은 bar 의 high 로 `highest_price` 갱신 후 판정.
3. bar 종료: `mark_price(sym, close)`; `sig = strategy.signal_at(sym, df, i)`;
   BUY & 포지션 없음 & `risk.can_open` → `risk.position_size` → fill_on == "close" 면 즉시 체결, 아니면 다음 bar 대기 주문;
   STOP BUY 는 항상 다음 bar `process_candle` 로; SELL & 포지션 있음 → 같은 규칙.
4. bar 종료 자산(equity) 기록. 마지막 bar 에서 남은 포지션은 종가로 평가(청산하지 않음, metrics 는 equity 기준).
`metrics.compute_metrics(equity: pd.Series, trades: list[Trade], periods_per_year: float) -> dict` 키:
`total_return, cagr, max_drawdown, max_drawdown_duration_bars, volatility, sharpe, sortino, calmar, win_rate,
profit_factor, num_trades, avg_pnl, avg_pnl_pct, avg_win, avg_loss, best_trade_pct, worst_trade_pct, avg_holding_hours, exposure`
(비율은 소수, 무위험 0, sharpe = mean/std * sqrt(periods_per_year) of bar 수익률).
`metrics.periods_per_year(interval: str, asset_class: AssetClass) -> float` (crypto 1d=365, 1h=8760; stock 1d=252, 1h=252*6.5).

## 6. 엔진 (`engine/trader.py`, `engine/state.py`)
```python
class StateStore:   # JSON 파일, 원자적 저장(temp → rename)
    def __init__(self, path: str | Path) ; def load() -> dict ; def save(data: dict) -> None
class Trader:
    def __init__(self, config: AppConfig, broker: BaseBroker, strategy: BaseStrategy, risk: RiskManager,
                 notifier: "Notifier | None" = None, state: StateStore | None = None,
                 clock: Callable[[], datetime] = utcnow) -> None
    def run_once(self) -> None     # 한 번의 폴링 사이클 (테스트 가능해야 함)
    def run_forever(self) -> None  # poll_seconds 간격 루프, SIGINT/SIGTERM 시 상태 저장 후 종료
    def stop(self) -> None
```
`run_once` 흐름 (심볼별):
1. `broker.is_market_open()` 거짓이면 (주식) 스킵. `close_positions_at_market_close` 처리.
2. `candles = broker.get_candles(sym, interval, limit=candle_limit)`; 마지막 완성 캔들 timestamp 가 상태의
   `last_candle_ts[sym]` 보다 새로우면 "새 캔들" 이벤트:
   a. `max_holding_bars` 만료 포지션 시장가 청산.
   b. `sig = strategy.generate_signal(sym, candles_to_df(candles))`.
   c. BUY MARKET & 포지션 없음 → `risk.can_open` → `position_size` → `broker.place_order` → 체결 확인(`fill_timeout_sec` 동안 get_order 폴링) → `risk.apply_entry`.
   d. BUY STOP → `pending_breakouts[sym] = {"trigger": 현재 진행중 캔들 open + stop_offset, "signal": sig, "expires": 다음 캔들 시작}`
      (진행중 캔들 open 은 `get_candles(include_partial=True)` 마지막 캔들의 open, 실패 시 현재가).
   e. SELL & 포지션 있음 → 전량 시장가 매도 → Trade 기록 → `risk.record_trade`.
3. 매 폴링: `price = broker.get_ticker(sym)`; pending_breakout 트리거 충족 시 매수; `risk.check_exit(position, price)` → 매도.
4. 상태 저장: positions(meta 포함), pending_breakouts, last_candle_ts, trades(최근 1000), risk 상태, paper broker `to_dict()`.
- 알림: 체결/오류/일일 요약을 notifier 로. 예외는 잡아서 로그+알림 후 루프 계속 (연속 N회 실패 시 백오프).
- 실거래(`config.is_live`) 시작 시 WARNING 배너 로그.

## 7. 알림 (`notify/`)
```python
class Notifier(ABC): def send(self, text: str) -> None   # 절대 예외를 밖으로 던지지 않는다 (로그만)
class MultiNotifier(Notifier)  # 여러 개 묶음
class TelegramNotifier(token, chat_id) ; SlackNotifier(webhook_url) ; DiscordNotifier(webhook_url)
class NullNotifier
def create_notifier(config: NotifyConfig, creds: Credentials) -> Notifier   # enabled 된 것만, 키 없으면 경고 후 제외
```

## 8. 데이터 저장소 (`data/store.py`)
```python
class CandleStore:
    def __init__(self, data_dir: str | Path) ; path(broker, symbol, interval) -> Path   # {data_dir}/{broker}/{symbol_safe}_{interval}.csv  (symbol_safe: '/'→'_')
    def load(self, broker: str, symbol: str, interval: str, start=None, end=None) -> pd.DataFrame
    def save(self, broker, symbol, interval, df) -> Path     # 기존과 병합, timestamp 중복 제거, 정렬
    def download(self, broker: BaseBroker, symbol, interval, start: datetime, end: datetime | None = None,
                 batch: int = 200, sleep: float = 0.1, progress: Callable|None = None) -> pd.DataFrame
        # broker.get_candles(end=...) 를 과거 방향으로 페이지네이션하여 start 까지 수집 후 save
def load_yfinance(symbol: str, interval: str, start, end) -> pd.DataFrame   # data/yfinance_feed.py, 선택 의존성
```

## 9. CLI (`cli.py`, typer)
```
tradingbot init                      # config/config.yaml, .env 생성 (예시 복사)
tradingbot validate-config -c CFG
tradingbot strategies                # 전략 목록 + 파라미터
tradingbot brokers
tradingbot backtest -c CFG [--symbol S ...] [--strategy NAME] [--param k=v ...] [--start D --end D] [--source csv|broker|yfinance] [--report]
tradingbot download -c CFG [--symbol S ...] [--start D] [--end D] [--source broker|yfinance]
tradingbot run -c CFG [--live] [--once]   # 기본 paper. --live 는 config.mode == "live" 일 때만 허용, 시작 전 5초 카운트다운(--yes 로 생략)
tradingbot balance -c CFG
tradingbot optimize ...  (선택: 파라미터 그리드 탐색)
```
- `run` (paper 모드): `data_broker = create_broker(config.broker.name)` (name 이 paper 가 아니면) → `PaperBroker.from_config(config, data_source=data_broker)`.
- `run --live`: `create_broker(config.broker.name, config)` 를 그대로 사용. `config.broker.sandbox=True` 면 KIS 모의투자/Alpaca paper 서버.
- 로깅: `logging_setup.setup_logging(config.logging)` (콘솔 rich + 파일 회전).

## 10. 브로커 어댑터 요약
| name | 자산 | 인증 | 캔들 | 비고 |
|---|---|---|---|---|
| upbit | crypto | JWT(HS256) query_hash SHA512 | /v1/candles/{minutes/N,days,weeks} 최대 200, 최신→과거 응답 | 시장가 매수는 금액(`ord_type=price`), 매도는 수량(`ord_type=market`); 최소 주문 5,000 KRW; 호가단위 반올림 |
| binance/ccxt | crypto | ccxt | fetch_ohlcv | `exchange_id` 로 거래소 선택, `sandbox` 면 `set_sandbox_mode(True)`; amount/price precision 은 ccxt 사용 |
| kis | stock(KR) | appkey/appsecret → access token(24h, 파일 캐시) | 일봉 inquire-daily-itemchartprice, 분봉 inquire-time-itemchartprice | 실전/모의 tr_id 분리, 계좌번호 8+2, 시장가 ORD_DVSN=01, 장중 09:00~15:30 KST |
| alpaca | stock(US) | APCA 헤더 | data.alpaca.markets /v2/stocks/bars (feed=iex) | paper-api vs api 베이스, 소수점 주식 가능(시장가, tif=day), /v2/clock |

각 어댑터의 공개 메서드 동작은 `BaseBroker` docstring 을 따른다. `get_candles` 는 **오래된→최신**, 미완성 캔들 제외(include_partial=False).
