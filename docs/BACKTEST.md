# 백테스트 (BACKTEST)

`tradingbot backtest` 는 **실제 거래소에서 내려받은 캔들** 위에서 전략 → 리스크 → 모의 체결(PaperBroker) 을 bar 단위로
재생하고 성과 지표를 계산한다. 이 문서는 데이터 처리, 체결 모델, 지표 정의, 결과 해석 방법을 설명한다.
구현: `tradingbot/backtest/engine.py`, `tradingbot/backtest/metrics.py`, `tradingbot/brokers/paper.py`, `tradingbot/risk/manager.py`.

```
tradingbot download -c config/config.yaml --start 2024-01-01          # 캔들 캐시
tradingbot backtest -c config/config.yaml --start 2024-01-01 --report  # 백테스트 + 리포트 저장
tradingbot backtest -c config/config.yaml --strategy rsi -p period=7 --fill-on close
```

## 1. 데이터

### 샘플 데이터는 없다
이 저장소에는 샘플/데모 시세가 **전혀 없다.** 백테스트 데이터는 항상 다음 중 하나에서 온다.

| `--source` | 출처 | 비고 |
|---|---|---|
| `csv` | `backtest.data_dir` 의 캐시 (`{data_dir}/{broker}/{symbol}_{interval}.csv`) | 이전에 `download`/`backtest` 가 내려받은 실제 데이터. 없으면 오류 |
| `broker` | 설정된 브로커의 공개 API (`broker.get_candles` 페이지네이션) | Upbit/ccxt 는 키 불필요. KIS/Alpaca 는 시세 조회에도 키 필요 |
| `yfinance` | Yahoo Finance (`pip install yfinance`) | 키 불필요. `{data_dir}/yfinance/{symbol}_{interval}.csv` 에 저장 |
| `auto` (기본) | 캐시가 요청 구간을 덮으면 `csv`, 아니면 `broker`; 키 없는 주식 브로커는 `yfinance` | 받은 데이터는 캐시에 병합되므로 다음 실행은 `csv` |

- "구간을 덮는다" 의 판정: 캐시의 첫 캔들 ≤ `start + interval` 이고 마지막 캔들 ≥ `end(또는 지금) - 2×interval`.
- `--start`/`--end` 는 `YYYY-MM-DD` 또는 ISO 8601, **양끝 포함**, UTC. 생략하면 설정의 `backtest.start/end`;
  그것도 없으면 csv 는 저장된 전체, 다운로드는 365일 전부터.
- `csv` 모드에서 저장 데이터가 요청 구간보다 짧으면 경고만 내고 있는 만큼 돌린다.
- **미완성 캔들은 제외** 된다 (`get_candles(include_partial=False)`). 일봉이면 오늘 캔들은 들어가지 않는다.
- ccxt 계열(binance/ccxt) 은 시세 전용으로 쓸 때 `sandbox` 를 자동으로 꺼 실제 시장 데이터를 받는다 (테스트넷 시세는
  희소하고 비현실적). `--live` 에는 적용되지 않는다.

### yfinance 사용 시
- 심볼 매핑: KIS `005930` → `005930.KS` (`broker.extra.yf_market: KQ` 면 `.KQ`), Alpaca `AAPL` → 그대로,
  Upbit `KRW-BTC` → `BTC-KRW`, ccxt `BTC/USDT` → `BTC-USDT`. 다른 티커가 필요하면
  `broker.extra.yf_symbols: {"BTC/USDT": "BTC-USD"}` 로 지정한다.
- 일봉/주봉은 수정주가(`auto_adjust=True`) 이며 Yahoo 의 "거래일" 을 그 날짜 00:00 UTC 로 표기한다 (Upbit 일봉과 같은 규약).
- 분봉은 **최근 60일** 만 제공된다 (1m 은 약 7일). `3m/10m/4h` 는 지원하지 않는다.

### DataFrame 규약
컬럼 `timestamp(UTC), open, high, low, close, volume`, 오래된 → 최신, timestamp 중복 없음. OHLC 가 NaN/0 이하이거나
`high < max(open, close)` 같은 행이 있으면 `DataError` 로 중단한다 (캐시 CSV 는 저장 시 정규화되므로 보통 통과).

## 2. 실행 흐름 (bar 알고리즘)

여러 심볼을 돌리면 **timestamp 합집합** 을 시간순으로 순회하며 심볼별로 아래를 수행한다. 현금은 하나의 계좌를 공유한다.

```
준비: 심볼별 df 를 strategy.prepare() 로 1회 지표 계산

각 bar (timestamp ts):
  1. 시가 mark      broker.mark_price(sym, open, timestamp=ts)
     a. 새 UTC 날짜면 risk.start_day(equity)           ← 일일 손실 한도의 기준 자산
     b. max_holding_bars 가 만료된 포지션 → 시가에 시장가 청산 (변동성 돌파의 "다음날 시가 매도")
     c. 이전 bar 종가에서 만든 대기 주문 실행
        - MARKET BUY  : 시가×(1+slippage) 로 즉시 체결 (수량은 이 가격 기준으로 다시 산정)
        - STOP/LIMIT  : 이 시점에 브로커에 접수 → process_candle(OHLC) 로 체결 판정, 이 bar 에 안 되면 취소
        - MARKET SELL : 시가×(1-slippage) 로 전량 청산
     d. 리스크 청산 (bar 중)  시가 → 저가 → 고가(최고가 갱신) → 저가(갱신된 고점 기준 추적손절) 순으로 risk.check_exit
        - 갭이면 시가, 아니면 손절/추적손절/익절 레벨에 체결 (손절 우선)
        - 이 bar 중간(STOP/LIMIT 트리거) 에 진입한 포지션은 체결 이후 경로를 알 수 없으므로 종가에서만 판정
  2. 종가 mark      broker.mark_price(sym, close, timestamp=ts)
     e. (bar 중 진입분) 종가 리스크 판정
     f. i >= warmup-1 이면 sig = strategy.signal_at(sym, df, i)
        - BUY  & 포지션 없음 & risk.can_open & 예산 > 0 →  fill_on=close 면 즉시 시장가 체결, 아니면 다음 bar 대기 주문
        - STOP/LIMIT BUY 는 항상 다음 bar
        - SELL & 포지션 있음 → 같은 규칙 (fill_on=close 면 종가, 아니면 다음 bar 시가)
        - 보유 중 BUY / 미보유 SELL 은 무시 (ignored_signals). 단, 다음 bar 시가에 만료 청산될 포지션은
          "없음" 으로 보고 재진입 신호를 받는다 (엔진과 동일한 a→b→d 순서)
  3. 자산 기록      equity_curve[ts] = cash + Σ 수량 × 종가
마지막 bar 의 잔여 포지션은 종가로 평가만 한다 (청산하지 않음).
```

### 체결 시점 (`backtest.fill_on`)

| 값 | 의미 | 용도 |
|---|---|---|
| `next_open` (기본) | 신호 캔들 **다음 캔들 시가** 에 시장가 체결 | 현실적. 실시간 엔진도 캔들이 완성된 뒤 신호를 보므로 체결은 다음 캔들 초반이다 |
| `close` | 신호 캔들 **종가** 에 즉시 체결 | 낙관적. 종가를 보고 종가에 산다는 가정이므로 결과가 좋게 나온다 |

STOP(변동성 돌파) 은 `fill_on` 과 무관하게 항상 다음 캔들 안에서 돌파 시점에 체결된다.

### STOP 주문 흉내 (변동성 돌파)
- 신호: `order_type=STOP, price=None, stop_offset=k×(high-low), max_holding_bars=1`.
- 다음 bar 시작에서 `trigger = open + stop_offset`. `open >= trigger` 면 시가(갭 상승), 아니면 `high >= trigger` 일 때
  트리거 가격에 체결하고 슬리피지(`×(1+slippage)`) 를 더한다. 수량은 **트리거 가격 기준** 으로 산정하므로 갭 상승으로
  현금이 모자라 거부되는 일이 없다. 그 bar 에 돌파가 없으면 주문은 취소된다.
- `price` 가 있는 STOP 은 `max(open, price)` 기준, `stop_offset` 과 `price` 가 둘 다 있으면 `price` 를 쓴다.
- STOP SELL: `open <= trigger` 면 시가, `low <= trigger` 면 트리거.
- 진입한 포지션은 그 다음 bar 시가에 `max_holding_bars=1` 로 청산된다 (`Trade.reason` = `최대 보유 기간 만료 ...`).

### LIMIT 주문
LIMIT BUY 는 `low <= price` 일 때 `price` 에, LIMIT SELL 은 `high >= price` 일 때 `price` 에 체결한다 (슬리피지 없음).
한 bar 안에 체결되지 않으면 취소된다. 수량은 지정가 기준으로 산정한다.

## 3. 포지션 사이징과 리스크

`RiskManager.position_size` (실시간 엔진과 같은 함수):

```
budget = min(equity, capital_limit) × max_position_pct × signal.strength
budget = min(budget, cash × (1 - fee_pct) × 0.999)
qty    = budget / price
budget < max(min_order_value, risk.min_order_value) → qty = 0 (신호 스킵, skipped_signals 에 집계)
```

- `equity` 는 bar 시작 시점의 현금 + 포지션 평가액, `cash` 는 가용 현금.
- `risk.can_open` : 보유 종목 수 ≥ `max_positions` 이거나 (당일 실현손익 / 당일 시작 자산) ≤ -`max_daily_loss_pct` 면
  신규 진입을 거부한다. 일일 손익은 UTC 날짜 기준으로 리셋된다.
- 진입 시 `stop_loss = fill × (1 - stop_loss_pct)`, `take_profit = fill × (1 + take_profit_pct)` (신호가 절대 가격을 주면 그 값),
  `highest_price = fill`. 매 bar 위 1-d 순서로 판정한다. 추적손절은 `highest_price > average_price` 일 때만 활성화되며
  `price <= highest × (1 - trailing_stop_pct)` 에서 청산한다.
- 수량 반올림: 암호화폐는 소수 8자리 내림, 주식(KIS/Alpaca 설정) 은 **정수 주** 내림 (네트워크 없이 결정적으로 돌리기 위해
  거래소의 정밀도 API 는 쓰지 않는다). 주가 대비 자금이 적으면 수량이 0 이 되어 신호가 스킵된다 — `--cash` 를 키워 확인하라.

## 4. 수수료·슬리피지

- `backtest.fee_pct` 는 **매수와 매도에 각각** 적용된다 (Upbit KRW 0.05% → `0.0005`, Binance 0.1% → `0.001`,
  국내주식은 수수료+거래세를 합쳐 `0.001` 안팎, Alpaca 는 0).
- 시장가/STOP 체결에는 `slippage_pct` 만큼 불리한 가격이 적용된다 (매수 `×(1+s)`, 매도 `×(1-s)`). LIMIT 은 없음.
- 수수료는 현금에서 바로 차감되고 `Position.average_price` 에는 포함되지 않는다. 매도 체결 시 `Trade.fee` =
  (해당 수량에 비례 배분한 매수 수수료) + 매도 수수료, `Trade.pnl` = (청산가 - 진입가) × 수량 - fee.
- 자산 곡선은 수수료를 반영한 현금 + 포지션 평가액이므로 `total_return` 과 Σ `Trade.pnl` 은 포지션이 없는 시점에서 일치한다.

## 5. 성과 지표 정의 (`metrics.compute_metrics`)

bar 수익률 `r_t = equity_t / equity_{t-1} - 1`, 무위험 수익률 0, 표준편차 `ddof=1`.
연 환산 bar 수 `periods_per_year` (crypto: 365일 24시간 → 1d=365, 1h=8760, 1w=52.14; stock: 1d=252, 1w=52,
분봉/시간봉은 252일 × 6.5시간 비례, 예 1h=1638).

| 키 | 정의 |
|---|---|
| `total_return` | `final_equity / initial_cash - 1` |
| `cagr` | `(1 + total_return)^(1/years) - 1`, `years` 는 첫/마지막 timestamp 경과 시간(365.25일 기준). 전액 손실이면 -1 |
| `max_drawdown` | 누적 최고 자산 대비 하락폭의 최댓값 (양수 소수) |
| `max_drawdown_duration_bars` | 이전 최고 자산을 회복하지 못한 가장 긴 연속 bar 수 |
| `volatility` | `std(r) × sqrt(periods_per_year)` |
| `sharpe` | `mean(r) / std(r) × sqrt(periods_per_year)` (std 0 이면 0) |
| `sortino` | `mean(r) / downside × sqrt(ppy)`, `downside = sqrt(Σ min(r,0)² / (n-1))` |
| `calmar` | `cagr / max_drawdown` (MDD 0 이면 0) |
| `win_rate` | 이익 거래 수 / 전체 거래 수 (pnl > 0 기준) |
| `profit_factor` | Σ 이익 / Σ 손실(절댓값). 손실 거래가 없으면 ∞ (JSON 에서는 null) |
| `num_trades` | 청산 완료 거래 수 |
| `avg_pnl`, `avg_pnl_pct` | 거래당 평균 손익(통화) / 평균 손익률(진입 금액 대비) |
| `avg_win`, `avg_loss` | 이익 거래 평균 / 손실 거래 평균 (통화) |
| `best_trade_pct`, `worst_trade_pct` | 거래 손익률 최댓값/최솟값 |
| `avg_holding_hours` | 평균 보유 시간 (시간) |
| `exposure` | 포지션을 하나라도 들고 있던 bar 의 비율 |

비율은 모두 소수(0.1 = 10%) 이며 CLI 는 % 로 표시한다.

## 6. 결과 출력과 리포트

CLI 출력 순서: 요약(전략/파라미터/심볼/기간/초기·최종 자산/거래·스킵·무시 건수/미청산 포지션) → 데이터 출처 → 성과 지표 표
→ 최근 거래 표(`--trades N`, 기본 20).

- `스킵된 신호` : `risk.can_open` 거부, 예산 부족(수량 0), 신호 형식 오류, 주문 접수 실패, 대기 주문 실행 시 이미 보유.
  사유별 건수가 `스킵 사유` 줄에 나온다.
- `무시된 신호` : 보유 중 BUY / 미보유 SELL — 정상 동작이다.
- `거부된 주문` : 체결 시점에 현금 부족 등으로 브로커가 거부한 주문. 보통 0 이다.

`--report` 를 주면 `backtest.report_dir` (또는 `--report-dir`) 에 세 파일을 만든다.

| 파일 | 내용 |
|---|---|
| `{전략}_{심볼}_{간격}_{UTC시각}.json` | `BacktestResult.to_dict()` — 설정, 지표, `equity_curve: [[iso, equity], ...]`, 모든 거래/주문/미청산 포지션 |
| `..._equity.csv` | `timestamp,equity` (bar 종가 기준 자산 곡선) |
| `..._summary.txt` | 콘솔 요약과 같은 plain text |

JSON 의 거래 항목에는 `entry_price, exit_price, entry_time, exit_time, fee, reason, pnl, pnl_pct, holding_hours` 가 있어
스프레드시트로 바로 분석할 수 있다.

## 7. 결과 해석 가이드

- **거래 수가 적으면 믿지 말 것.** 200 개 일봉에서 거래 5건이면 운이다. 기간을 늘리거나 간격을 줄여 표본을 확보한다.
- **MDD 와 지속 bar** 를 먼저 보라. 실제로 그 낙폭을 견딜 수 있는지가 전략의 생존 조건이다.
- `fill_on=close` 결과가 `next_open` 보다 눈에 띄게 좋다면 전략이 "종가를 보고 종가에 사는" 비현실적 가정에 의존하는 것이다.
- 슬리피지/수수료를 실제보다 보수적으로 넣어 보고도 수익이 남는지 확인한다 (예: 암호화폐 `slippage_pct: 0.001`).
- `exposure` 가 낮은데 수익이 큰 전략은 소수 거래에 의존할 가능성이 크다. `best_trade_pct` 를 뺀 결과도 상상해 보라.
- 같은 파라미터로 다른 심볼/기간에서도 비슷한 성과가 나는지 확인한다 (`--symbol`, `--start/--end`).
- 백테스트는 미래 참조를 막아 두었지만(prefix 불변 테스트), 상장 폐지 종목이 빠진 생존 편향, 급변동 시 체결 불가, 거래소 장애,
  호가 단위, 부분 체결 같은 현실 요소는 반영하지 않는다. 반드시 **모의투자(paper) 로 일정 기간 돌려본 뒤** 실거래로 넘어간다.

## 8. 프로그램에서 직접 쓰기

```python
from tradingbot.backtest import Backtester
from tradingbot.config import load_config
from tradingbot.data import CandleStore
from tradingbot.risk import RiskManager
from tradingbot.strategies import create_strategy

cfg = load_config("config/config.yaml")
store = CandleStore(cfg.backtest.data_dir)
data = {sym: store.load("upbit", sym, "1d", start="2024-01-01") for sym in cfg.symbols}

bt = Backtester(
    create_strategy("volatility_breakout", {"k": 0.5, "ma_period": 5}),
    RiskManager(cfg.risk),
    initial_cash=cfg.backtest.initial_cash,
    fee_pct=cfg.backtest.fee_pct,
    slippage_pct=cfg.backtest.slippage_pct,
    fill_on=cfg.backtest.fill_on,
    quote_currency="KRW",
    interval="1d",
    min_order_value=cfg.risk.min_order_value,
)
result = bt.run(data)
print(result.summary())
result.equity_curve.to_csv("equity.csv")
```
