# tradingbot — 주식 · 코인 자동매매 봇

Upbit(업비트), Binance 등 ccxt 거래소, 한국투자증권(KIS Developers), Alpaca(미국주식) 를 하나의 인터페이스로 묶은
Python 자동매매 봇입니다. **백테스트 → 모의투자(paper) → 실거래(live)** 를 같은 전략 코드로 돌릴 수 있습니다.

- 전략: SMA/EMA 교차, RSI, 볼린저 밴드, MACD, 변동성 돌파 (+ 직접 추가)
- 리스크: 종목당 비중, 최대 종목 수, 손절/익절/추적손절, 일일 손실 한도, 운용 자금 상한
- 백테스터: bar 단위 재생, 다음 캔들 시가 체결, 수수료/슬리피지, STOP 주문 흉내, 18개 성과 지표, JSON/CSV 리포트
- 실시간 엔진: 캔들 폴링, 체결 확인, 상태 파일(원자적 저장) 로 재시작 복원, 텔레그램/슬랙/디스코드 알림
- 모든 시세는 **실제 거래소 데이터** 입니다. 샘플/데모 데이터는 어디에도 없습니다.

> ### ⚠️ 주의 / 면책
> - 이 소프트웨어는 **교육·연구 목적** 으로 제공되며 수익을 보장하지 않습니다. 실거래로 발생하는 모든 손실은 전적으로 사용자 책임입니다.
> - **반드시 백테스트와 모의투자(paper) 로 충분히 검증한 뒤** 소액으로 실거래를 시작하세요. 실거래는 설정의 `mode: live` 와
>   `tradingbot run --live` 를 **둘 다** 줘야만 시작되며, 시작 전 5초 카운트다운이 있습니다.
> - API 키는 **출금 권한 없이** 발급하고 `.env` 에만 보관하세요. 이 프로그램은 키를 로그/알림에 출력하지 않습니다.
> - 거래소 장애, 네트워크 단절, 급변동 시 체결 지연/실패가 날 수 있습니다. 봇이 돌아가는 동안에도 계좌를 직접 확인하세요.

> ### 샘플/데모 데이터 없음
> 이 프로젝트에는 샘플 CSV, 하드코딩된 가격, 가짜 잔고, "데모 모드" 가 **없습니다.** 백테스트 데이터는 항상 거래소 공개 API
> (Upbit/ccxt 는 키 불필요), KIS/Alpaca API, 또는 yfinance 에서 **실제 데이터를 내려받아** 사용합니다. 테스트 역시 Upbit 공개 API 에서
> 받은 실제 캔들을 캐시해 사용하며, 네트워크가 없으면 해당 테스트는 건너뜁니다.

## 지원 거래소

| 브로커 (`broker.name`) | 자산 | 심볼 표기 | 결제 통화 | 시세 조회 키 | 모의 서버 (`sandbox: true`) |
|---|---|---|---|---|---|
| `upbit` | 암호화폐 | `KRW-BTC` | KRW | 불필요 | 없음 → `mode: paper` 로 검증 |
| `binance` / `ccxt` | 암호화폐 (bybit, okx, bithumb … `exchange_id`) | `BTC/USDT` | USDT 등 | 불필요 | Binance 테스트넷 |
| `kis` | 국내주식 (한국투자증권) | `005930` | KRW | **필요** | KIS 모의투자 서버 |
| `alpaca` | 미국주식 | `AAPL` | USD | **필요** | Alpaca paper-api |

세부 사항(키 발급, 사용 API, 제한) 은 [docs/BROKERS.md](docs/BROKERS.md) 를 보세요.

## 설치

Python **3.10 이상** 이 필요합니다.

```bash
git clone <this-repo> automated-trading-bot
cd automated-trading-bot
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e '.[all]'              # ccxt + yfinance + 개발 도구 포함
# 최소 설치: pip install -e .   (Upbit/KIS/Alpaca 만, Binance 는 .[ccxt], Yahoo 데이터는 .[yfinance])
tradingbot --help
```

## 빠른 시작

```bash
# 1. 예시 설정 복사 → config/config.yaml, .env
tradingbot init

# 2. .env 에 사용할 거래소의 키를 넣는다 (Upbit/Binance 의 시세·백테스트·모의투자는 키 없이도 동작)
#    UPBIT_ACCESS_KEY=...  UPBIT_SECRET_KEY=...

# 3. 설정 검증 (브로커/전략/간격/리스크 값, 환경변수 유무)
tradingbot validate-config -c config/config.yaml

# 4. 실제 캔들 내려받기 (data/candles/upbit/KRW-BTC_1h.csv 에 캐시)
tradingbot download -c config/config.yaml --start 2024-01-01

# 5. 백테스트 (데이터가 없으면 자동으로 거래소에서 받는다)
tradingbot backtest -c config/config.yaml --start 2024-01-01 --report
tradingbot backtest -c config/config.yaml --strategy rsi -p period=7 -p oversold=35

# 6. 모의투자 (실제 시세, 가상 계좌, 실제 주문 없음) — Ctrl+C 로 종료, 상태는 data/state.json 에 저장
tradingbot run -c config/config.yaml
tradingbot run -c config/config.yaml --once     # 폴링 1회만 (동작 확인용)
tradingbot status -c config/config.yaml        # 저장된 상태 요약
tradingbot balance -c config/config.yaml       # 모의 계좌 잔고/포지션

# 7. 실거래: config 의 mode 를 live 로 바꾼 뒤 --live 로만 시작된다 (5초 카운트다운, --yes 로 생략)
tradingbot run -c config/config.yaml --live
tradingbot balance -c config/config.yaml --live  # 실제 계좌 조회
```

전략별 예시 설정은 `config/examples/` 에 있습니다:
`upbit_volatility_breakout.yaml`, `binance_sma_cross.yaml`, `kis_rsi.yaml`, `alpaca_macd.yaml`.

## CLI 명령

| 명령 | 설명 |
|---|---|
| `tradingbot init [--force] [--dir DIR]` | `config/config.example.yaml` → `config/config.yaml`, `.env.example` → `.env` 복사 (있으면 건너뜀) |
| `tradingbot validate-config -c CFG` | 설정 검증. 오류면 종료 코드 1, 경고는 출력만 |
| `tradingbot strategies` / `brokers` | 전략 목록(기본 파라미터, 워밍업) / 브로커 목록(환경변수, 지원 간격) |
| `tradingbot download -c CFG [--symbol S ...] [--start D] [--end D] [--interval I] [--source broker\|yfinance]` | 실제 캔들을 `backtest.data_dir` 에 CSV 로 캐시 (기본 365일 전부터) |
| `tradingbot backtest -c CFG [--symbol S ...] [--strategy NAME] [-p k=v ...] [--start D --end D] [--interval I] [--source auto\|csv\|broker\|yfinance] [--cash N] [--fill-on next_open\|close] [--report] [--report-dir DIR] [--trades N]` | 백테스트. `auto` 는 캐시가 구간을 덮으면 csv, 아니면 거래소 다운로드(키 없는 주식 브로커는 yfinance) |
| `tradingbot run -c CFG [--live] [--once] [--yes]` | 매매 엔진. 기본 모의투자. `--live` 는 `mode: live` 일 때만 |
| `tradingbot balance -c CFG [--live]` | paper: 저장된 모의 계좌 / `--live` 또는 `mode: live`: 실제 계좌 |
| `tradingbot status -c CFG` | 상태 파일 요약 (포지션, 돌파 대기, 최근 거래, 당일 손익) |
| 전역 `--debug` | 오류 시 트레이스백 + DEBUG 로그 (기본은 한국어 한 줄 오류 + 종료 코드 1) |

`-p/--param` 값은 `true/false` → bool, 정수 → int, 실수 → float, 그 외 → 문자열로 해석됩니다.
`--strategy` 로 설정과 다른 전략을 고르면 설정 파일의 `params` 는 버리고 `-p` 만 씁니다.

## 설정 파일 (`config/config.yaml`)

모든 키에 한국어 주석이 달린 `config/config.example.yaml` 을 기준으로 설명합니다. 비율은 전부 0~1 소수입니다 (0.02 = 2%).

| 섹션 / 키 | 기본값 | 설명 |
|---|---|---|
| `mode` | `paper` | `backtest` \| `paper` \| `live`. `live` 는 `broker.name` 이 `paper` 면 안 되고 `run --live` 로만 시작 |
| `broker.name` | `upbit` | `upbit` \| `binance` \| `ccxt` \| `kis` \| `alpaca` (시세 출처 겸 실거래 브로커) |
| `broker.exchange_id` | `null` | ccxt 전용 거래소 id (`bybit`, `okx`, `bithumb` …). `binance` 면 자동 |
| `broker.sandbox` | `true` | KIS 모의투자 서버 / Alpaca paper-api / ccxt 테스트넷. paper·backtest·download 에서 ccxt 는 자동으로 실제 시세 사용 |
| `broker.fill_timeout_sec` | `30` | 주문 후 체결 확인 대기(초). 초과하면 취소 시도 |
| `broker.extra` | `{}` | 어댑터별 옵션 (Upbit `jwt_algorithm`, KIS `token_path`/`request_interval`/`holiday_check`, Alpaca `feed`, ccxt `options`, yfinance `yf_market`/`yf_symbols`) |
| `symbols` | `[KRW-BTC]` | 매매 대상 (브로커 네이티브 표기, 중복 불가) |
| `interval` | `1h` | 캔들 간격: `1m 3m 5m 10m 15m 30m 1h 4h 1d 1w` 중 브로커가 지원하는 값 |
| `strategy.name` / `strategy.params` | `sma_cross` / `{}` | 전략과 파라미터 (`tradingbot strategies`) |
| `risk.max_position_pct` | `0.2` | 종목당 최대 투자 비중 (총자산 또는 `capital_limit` 대비) |
| `risk.max_positions` | `5` | 동시 보유 최대 종목 수 |
| `risk.stop_loss_pct` | `0.03` | 평균단가 대비 손절 비율. `null`/0 이면 사용 안 함 |
| `risk.take_profit_pct` | `null` | 익절 비율 |
| `risk.trailing_stop_pct` | `null` | 고점 대비 추적 손절 비율 (고점이 평균단가를 넘은 뒤 활성) |
| `risk.max_daily_loss_pct` | `0.05` | 당일 실현손실 / 당일 시작 자산 이 이 값을 넘으면 당일 신규 진입 중단 (UTC 날짜 기준) |
| `risk.min_order_value` | `0` | 최소 주문 금액(결제 통화). 0 이면 브로커 기본값 (Upbit 5,000 KRW) |
| `risk.capital_limit` | `null` | 총자산 대신 이 금액만 운용 기준으로 삼음 (예: 1000000) |
| `paper.initial_cash` | `10000000` | 모의투자 시작 현금 |
| `paper.quote_currency` | `KRW` | 결제 통화 (시세 브로커가 있으면 그 브로커의 통화를 따름) |
| `paper.fee_pct` / `paper.slippage_pct` | `0.0005` | 모의 체결 수수료(매수/매도 각각) 와 시장가 슬리피지. 실거래 사이징의 수수료 가정치로도 쓰임 |
| `engine.poll_seconds` | `30` | 시세/캔들 확인 주기(초). 변동성 돌파는 10초 정도 |
| `engine.candle_limit` | `300` | 전략에 넘길 최근 캔들 수 (전략 `warmup` 이상, KIS 분봉은 작게) |
| `engine.state_file` | `data/state.json` | 포지션/대기 주문/거래 기록 상태 파일 (설정마다 다르게) |
| `engine.close_positions_at_market_close` | `false` | 장 마감 직전 전량 청산 (주식 데이트레이딩) |
| `engine.sync_positions_on_start` | `true` | 시작 시 거래소 보유 포지션과 상태 파일 동기화 |
| `engine.stale_data_minutes` | `30` | 마지막 완성 캔들이 이보다 오래되면 경고 (일봉은 1500 정도) |
| `notify.telegram/slack/discord.enabled` | `false` | 채널 사용 여부. 토큰/웹훅은 `.env` |
| `notify.notify_on_signal/trade/error` | `false/true/true` | 신호/체결/오류 알림 |
| `notify.daily_summary` | `true` | UTC 날짜가 바뀔 때 일일 요약 |
| `backtest.start` / `backtest.end` | `null` | 기본 백테스트 구간 (`YYYY-MM-DD`, 양끝 포함) |
| `backtest.data_dir` | `data/candles` | 캔들 CSV 캐시 위치 (`{data_dir}/{broker}/{symbol}_{interval}.csv`) |
| `backtest.initial_cash` / `fee_pct` / `slippage_pct` | `10000000` / `0.0005` / `0.0005` | 백테스트 계좌 |
| `backtest.fill_on` | `next_open` | 시장가 체결 시점: `next_open`(다음 캔들 시가) \| `close`(신호 캔들 종가) |
| `backtest.report_dir` | `reports` | `--report` 저장 위치 |
| `logging.level` / `logging.file` | `INFO` / `logs/tradingbot.log` | 로그 레벨, 회전 파일(5MB×5). `null` 이면 콘솔만 |

### 환경변수 (`.env`)

| 변수 | 용도 |
|---|---|
| `UPBIT_ACCESS_KEY`, `UPBIT_SECRET_KEY` | Upbit 잔고/주문 |
| `CCXT_API_KEY`, `CCXT_SECRET`, `CCXT_PASSWORD` (별칭 `BINANCE_API_KEY`, `BINANCE_SECRET_KEY`) | ccxt 거래소 |
| `KIS_APP_KEY`, `KIS_APP_SECRET`, `KIS_ACCOUNT_NO` (`12345678-01`), `KIS_HTS_ID` | 한국투자증권 (시세 포함) |
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` (별칭 `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`) | Alpaca (시세 포함) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `SLACK_WEBHOOK_URL`, `DISCORD_WEBHOOK_URL` | 알림 |

## 전략

| 이름 | 기본 파라미터 | 규칙 |
|---|---|---|
| `sma_cross` | `fast=10, slow=30` | 단기 SMA 가 장기 SMA 를 상향 돌파(골든크로스) 매수, 하향 돌파 매도 |
| `ema_cross` | `fast=12, slow=26` | EMA 버전 |
| `rsi` | `period=14, oversold=30, overbought=70` | RSI 가 과매도선 상향 돌파 매수, 과매수선 하향 돌파 매도 |
| `bollinger` | `period=20, num_std=2.0, mode=reversion` | reversion: 하단밴드 복귀 매수 / 중심선 이상 매도. breakout: 상단 돌파 매수 / 중심선 이탈 매도 |
| `macd` | `fast=12, slow=26, signal=9` | MACD 가 시그널 상향 돌파 매수, 하향 돌파 매도 |
| `volatility_breakout` | `k=0.5, ma_period=0` | 다음 캔들 시가 + k×(전일 고가-저가) 돌파 시 매수, 그 다음 캔들 시가 매도 (래리 윌리엄스) |

규칙·파라미터·주의점은 [docs/STRATEGIES.md](docs/STRATEGIES.md) 에 있습니다. 백테스트와 실거래는 같은 `prepare()/signal_at()`
코드 경로를 타고, 미래 데이터를 참조하지 않도록 테스트로 검증되어 있습니다.

## 리스크 관리

- **포지션 크기** = `min(총자산, capital_limit) × max_position_pct × 신호 강도`, 단 `가용 현금 × (1 - 수수료) × 0.999` 를 넘지 않음.
  최소 주문 금액에 못 미치면 신호를 건너뜁니다.
- **진입 제한**: 보유 종목 ≥ `max_positions` 이거나 당일 실현손실이 `max_daily_loss_pct` 를 넘으면 신규 진입 금지.
- **청산 판정 순서**(매 폴링/매 bar): 손절 → 추적손절 → 익절. 추적손절은 고점이 평균단가를 넘어선 뒤부터 고점 대비 하락률로 판정.
- **최대 보유 기간** (`max_holding_bars`, 변동성 돌파의 "다음 캔들 시가 매도") 은 새 캔들이 완성될 때 시장가 청산.
- 주식은 장 운영 시간에만 주문하며, `close_positions_at_market_close` 로 마감 전 청산을 선택할 수 있습니다.
- 모든 리스크 계산은 백테스터와 실시간 엔진이 **같은 `RiskManager`** 를 사용합니다.

## 브로커 키 발급 요약

| 브로커 | 절차 | 비고 |
|---|---|---|
| **Upbit** | upbit.com → 마이페이지 → Open API 관리 → 자산조회·주문조회·주문하기 권한, **접속 IP 등록** | 모의 서버 없음. `mode: paper` 로 검증. 최소 주문 5,000 KRW |
| **Binance** | API Management → Spot 거래 권한만 (출금 금지) → `CCXT_API_KEY/SECRET` | 테스트넷 키는 testnet.binance.vision. 일부 지역은 API 접속 차단(451) |
| **한국투자증권** | apiportal.koreainvestment.com → API 신청 → **모의투자 별도 신청** → 앱키/시크릿, 계좌번호 `12345678-01` | 시세 조회에도 키 필요. 토큰은 `~/.tradingbot/kis_token_*.json` 에 캐시 (발급 1분당 1회) |
| **Alpaca** | app.alpaca.markets → Paper Trading → API Keys | paper 키와 live 키가 다름 (`sandbox` 와 맞출 것). 시세도 키 필요 (`feed=iex`) |

자세한 내용은 [docs/BROKERS.md](docs/BROKERS.md).

## 백테스트 결과 읽는 법

```
=== 백테스트 결과: volatility_breakout {'k': 0.5, 'ma_period': 5} ===
심볼        : KRW-BTC, KRW-ETH
간격/체결   : 1d / next_open (자산 구분 crypto)
기간        : 2024-01-01T00:00:00+00:00 ~ 2025-06-30T00:00:00+00:00 (547 bars)
초기 자산   : 10,000,000 KRW
최종 자산   : ... KRW (+..%)
거래 N건, 주문 M건, 스킵된 신호 ..건, 무시된 신호 ..건, 거부된 주문 0건
```

- **총 수익률 / CAGR**: 기간 수익률과 연 환산 수익률. 짧은 구간의 CAGR 은 과장되기 쉽습니다.
- **최대 낙폭(MDD) / 지속 bar**: 고점 대비 최대 하락과 회복까지 걸린 bar 수. 실제로 견딜 수 있는 수준인지 먼저 보세요.
- **샤프/소르티노/칼마**: 변동성 대비 수익 (무위험 0, bar 수익률 기준, 연 환산). 소르티노는 하락 변동성만 봅니다.
- **승률 / 손익비(Profit Factor)**: 이익 거래 비율과 총이익/총손실. 손실 거래가 없으면 ∞ 로 표시됩니다.
- **거래 횟수**: 10건 미만이면 통계적 의미가 거의 없습니다.
- **시장 노출 비율**: 포지션을 들고 있던 bar 의 비율.
- **스킵된 신호**: 리스크 한도/예산 부족으로 실행되지 않은 신호. 주식에서 자금이 적으면 수량이 0 이 되어 스킵됩니다 (`--cash` 로 확인).

`--report` 를 주면 `reports/` 에 JSON(전체 결과·자산곡선·거래), `_equity.csv`, `_summary.txt` 가 저장됩니다.
체결 모델과 지표 정의는 [docs/BACKTEST.md](docs/BACKTEST.md) 를 참고하세요.

## 상태 · 로그 · 알림

- **상태 파일** (`engine.state_file`, 기본 `data/state.json`): 포지션(손절/익절/진입 사유 포함), 돌파 대기 주문, 심볼별 마지막 캔들,
  최근 거래 1,000건, 일일 손익, 모의투자 계좌. 임시 파일 → `rename` 으로 원자적으로 저장되고, 손상되면 `.corrupt-<시각>` 으로
  옮긴 뒤 빈 상태로 시작합니다. 재시작하면 복원되며 거래소 포지션과 동기화합니다. `tradingbot status` 로 요약을 봅니다.
- **로그**: 콘솔(rich) + `logging.file` 회전 파일(5MB×5). 비밀값은 마스킹됩니다. `--debug` 로 DEBUG 레벨.
- **알림**: `notify.*.enabled` 와 `.env` 의 토큰/웹훅을 설정하면 체결·오류(같은 내용은 10분에 1회)·일일 요약·시작/종료를 보냅니다.
  텔레그램은 @BotFather 로 봇을 만들고 봇에게 메시지를 보낸 뒤 `getUpdates` 로 `chat_id` 를 확인합니다. 알림 실패는 봇을 멈추지 않습니다.
- **종료**: Ctrl+C(SIGINT) / SIGTERM 을 받으면 상태를 저장하고 `[종료]` 알림 후 끝납니다. 연속 오류 시 지수 백오프(최대 5분).

## 테스트 실행

```bash
pip install -e '.[all]'
pytest -q                       # 전체 (실제 Upbit 캔들을 처음 1회 받아 .pytest_cache 에 캐시, 이후 빠름)
pytest tests/test_cli.py -q     # 모듈별
ruff check .            # CI 와 같은 명령 (B027 예외는 pyproject.toml 의 per-file-ignores 에 있음)
ruff format --check .   # 문서(*.md) 의 코드 블록은 pyproject.toml 에서 제외
```

- 테스트는 `tests/conftest.py` 가 Upbit 공개 API 에서 받은 **실제** KRW-BTC(1d, 1h)/KRW-ETH(1d) 캔들을 사용합니다.
  네트워크가 없으면 해당 테스트는 `skip` 되고 순수 계산/검증 테스트만 돕니다. (`-p no:cacheprovider` 는 쓰지 마세요.)
- 비공개 API(주문/잔고) 테스트는 거래소 공식 문서의 응답 스키마를 그대로 따르는 HTTP 모킹만 사용하며, 모킹 데이터는 테스트 파일 안에만 있습니다.
- CI(`.github/workflows/ci.yml`) 는 Python 3.11/3.12 에서 ruff + pytest 를 실행합니다.

## 프로젝트 구조

```
tradingbot/
  cli.py            typer CLI (init, validate-config, strategies, brokers, download, backtest, run, balance, status)
  config.py         AppConfig(YAML) / Credentials(.env)      models.py  Candle, Signal, Order, Position, Trade ...
  exceptions.py     ConfigError, BrokerError 계열, DataError  logging_setup.py  rich 콘솔 + 회전 파일 로그
  brokers/          base.py(인터페이스) paper.py upbit.py ccxt_broker.py kis.py alpaca.py
  strategies/       base.py indicators.py sma_cross.py rsi.py bollinger.py macd.py volatility_breakout.py
  risk/manager.py   포지션 사이징, 손절/익절/추적손절, 일일 손실 한도
  backtest/         engine.py(Backtester, BacktestResult) metrics.py(성과 지표)
  engine/           trader.py(실시간 루프) state.py(상태 파일, 직렬화)
  notify/           telegram.py slack.py discord.py (비밀값 마스킹)
  data/             store.py(CandleStore CSV 캐시 + 다운로드) yfinance_feed.py
  utils/            http.py(재시도 HTTP) timeutil.py(KST/ET 장 시간)
config/             config.example.yaml, examples/*.yaml      docs/   ARCHITECTURE.md BROKERS.md STRATEGIES.md BACKTEST.md
tests/              pytest (실제 캔들 픽스처는 conftest.py)
```

## 새 전략 추가하기

1. `tradingbot/strategies/my_strategy.py` 에 `BaseStrategy` 를 구현합니다 (`prepare` 는 `shift/rolling/ewm` 만 사용 → 미래 참조 금지).

```python
from __future__ import annotations

from typing import Any

import pandas as pd

from tradingbot.models import Signal, SignalAction
from tradingbot.strategies import indicators as ind
from tradingbot.strategies.base import BaseStrategy


class MomentumStrategy(BaseStrategy):
    name = "momentum"
    description = "N캔들 수익률이 양수로 전환하면 매수, 음수로 전환하면 매도"
    default_params: dict[str, Any] = {"period": 10}

    def __init__(self, **params: Any) -> None:
        super().__init__(**params)
        self.period = ind.as_period(self.params["period"], "period", minimum=1)
        self.params["period"] = self.period

    @property
    def warmup(self) -> int:
        return self.period + 2

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["mom"] = out["close"].pct_change(self.period)
        zero = pd.Series(0.0, index=out.index)
        out["mom_up"] = ind.crossover(out["mom"], zero)
        out["mom_down"] = ind.crossunder(out["mom"], zero)
        return out

    def signal_at(self, symbol: str, df: pd.DataFrame, i: int) -> Signal:
        if i < self.warmup - 1:
            return Signal.hold(symbol, reason=f"워밍업 ({i + 1}/{self.warmup})")
        row = df.iloc[i]
        if pd.isna(row["mom"]):
            return Signal.hold(symbol, reason="지표 미산출 (NaN)")
        meta = {"mom": float(row["mom"]), "period": self.period, "timestamp": row["timestamp"].isoformat()}
        if bool(row["mom_up"]):
            return Signal(action=SignalAction.BUY, symbol=symbol, reason=f"{self.period}봉 모멘텀 양전환", meta=meta)
        if bool(row["mom_down"]):
            return Signal(action=SignalAction.SELL, symbol=symbol, reason=f"{self.period}봉 모멘텀 음전환", meta=meta)
        return Signal.hold(symbol, reason="변화 없음")
```

2. `tradingbot/strategies/__init__.py` 의 `_REGISTRY` 에 `"momentum": ("tradingbot.strategies.my_strategy", "MomentumStrategy")` 를 추가합니다.
3. `tests/test_strategies.py` 를 참고해 실제 캔들 픽스처(`daily_df`) 로 테스트를 추가합니다 — 특히 prefix(미래 참조 없음) 검증.
4. `tradingbot backtest -c config/config.yaml --strategy momentum -p period=10` 으로 확인합니다.

더 자세한 예시는 [docs/STRATEGIES.md](docs/STRATEGIES.md) 의 "새 전략 추가하기" 를 보세요.

## FAQ

**Q. 백테스트 데이터는 어디서 오나요? 샘플 데이터는 없나요?**
없습니다. `backtest.data_dir` 에 캐시된 CSV 가 없으면 설정된 거래소 공개 API(Upbit/ccxt 는 키 불필요) 에서 내려받아 저장합니다.
KIS/Alpaca 는 시세에도 키가 필요하므로 키가 없으면 `--source yfinance` 로 Yahoo 데이터를 씁니다 (`005930` → `005930.KS`).

**Q. 실거래가 시작되지 않아요.**
설정의 `mode: live` 와 `run --live` 플래그가 **둘 다** 있어야 합니다. `broker.name` 이 `paper` 면 안 되고, 해당 브로커의 키가
`.env` 에 있어야 합니다. `validate-config` 로 먼저 확인하세요.

**Q. `sandbox: true` 가 무슨 뜻인가요?**
KIS 는 모의투자 서버, Alpaca 는 paper-api, ccxt 는 테스트넷에 접속합니다. Upbit 는 모의 서버가 없어 무시됩니다.
모의투자(`mode: paper`)/백테스트/다운로드에서는 주문을 내지 않으므로 ccxt 계열은 자동으로 실제 시세를 받습니다.

**Q. `mode: paper` 와 `sandbox: true` 의 차이는?**
`paper` 는 봇 내부의 가상 계좌(PaperBroker) 로 체결을 흉내내며 거래소에 주문이 가지 않습니다. `sandbox` 는 실제로 거래소의
모의 서버에 주문을 보내는 것으로 `mode: live --live` 에서 의미가 있습니다. 순서는 backtest → paper → live(sandbox) → live(실전) 입니다.

**Q. Binance API 가 451/연결 거부를 돌려줍니다.**
해당 지역/클라우드 IP 에서 Binance 접속이 차단된 경우입니다. 다른 `exchange_id`(bybit, okx …) 나 Upbit 를 사용하세요.

**Q. 변동성 돌파의 STOP 주문은 거래소에 들어가나요?**
아니요. 엔진이 "진행 중 캔들 시가 + k×range" 를 기억하고 매 폴링마다 현재가가 넘으면 **시장가** 로 삽니다. 그래서 `poll_seconds` 를
짧게 두세요. 다음 캔들이 시작되면 대기 주문은 만료되고 보유 포지션은 `max_holding_bars=1` 로 시가에 팔립니다.

**Q. 상태 파일을 지우면 어떻게 되나요?**
포지션 기록/일일 손익이 사라집니다. 시작 시 `sync_positions_on_start` 가 거래소 보유 수량을 다시 받아 설정된 심볼은 포지션으로
채택하지만(ccxt 는 평균단가를 모르므로 0), 손절/익절 레벨은 현재 설정 비율로 다시 계산됩니다.

**Q. KIS 모의투자에서 분봉이 부족하다고 나옵니다.**
과거 일자 분봉 API(`FHKST03010230`) 가 모의 서버에서 동작하지 않으면 당일 분봉만 쌓입니다. 일봉 전략을 쓰거나 `candle_limit` 를
줄이고, 실전 서버 키로 시세만 받는 방법을 검토하세요.

**Q. yfinance 분봉이 비어 있어요.**
Yahoo 는 분봉을 최근 60일(1m 은 약 7일) 만 제공합니다. 그 이전 구간은 거래소 API 로 받으세요.

**Q. 텔레그램 chat_id 는 어떻게 알아내나요?**
봇에게 아무 메시지나 보낸 뒤 `https://api.telegram.org/bot<토큰>/getUpdates` 를 열면 `chat.id` 가 보입니다.

**Q. 같은 설정을 여러 개 돌려도 되나요?**
설정마다 `engine.state_file` 과 `logging.file` 을 다르게 두면 됩니다. 단, 같은 거래소 계좌/심볼을 두 봇이 동시에 다루면 포지션이 꼬입니다.

## 라이선스

MIT
