# 브로커 (BROKERS)

지원 어댑터, 인증/키 발급, 사용하는 API, 심볼 표기, 제한 사항을 정리한다. 모든 어댑터는 `tradingbot/brokers/base.py` 의
`BaseBroker` 인터페이스를 구현하며 설정의 `broker.name` 으로 선택한다 (`tradingbot brokers` 로 목록 확인).

| name | 자산 | 심볼 표기 | 결제 통화 | 시세 조회에 키 필요 | sandbox 의미 | 네이티브 STOP |
|---|---|---|---|---|---|---|
| `upbit` | 암호화폐 | `KRW-BTC` | KRW (BTC/USDT 마켓도 가능) | 아니오 | 없음 (무시) | 없음 → 엔진 흉내 |
| `binance` / `ccxt` | 암호화폐 | `BTC/USDT` | USDT 등 | 아니오 | ccxt `set_sandbox_mode` (테스트넷) | 없음 → 엔진 흉내 |
| `kis` | 국내주식 | `005930` | KRW | **예** | 모의투자 서버 (앱키 별도) | 없음 → 엔진 흉내 |
| `alpaca` | 미국주식 | `AAPL` | USD | **예** | paper-api (키 별도) | 없음 → 엔진 흉내 |
| `paper` | 모의 체결 엔진 | (시세 브로커 표기) | 설정값 | — | — | 캔들/현재가로 체결 판정 |

## 공통 규약

- **키는 `.env` 에만** 둔다 (`tradingbot init` 이 `.env.example` 을 권한 600 으로 복사). YAML/코드/로그에 넣지 않는다. 어댑터는
  `Credentials.from_env()` 로 읽고, 키가 없어도 생성은 되며 비공개 API 호출 시에만 `AuthenticationError("... 환경변수 필요")`
  가 난다. CLI 는 명령 시작 시 현재 디렉터리(상위 포함) → 설정 파일 디렉터리 → 그 상위 순으로 `.env` 를 찾아 먼저 읽는다
  (`Credentials.from_env()` 의 기본 탐색은 python-dotenv 규칙대로 패키지 디렉터리 기준이라 현재 디렉터리를 보지 않는다).
- 모든 시각은 UTC aware. 거래소의 KST/ET 응답은 즉시 UTC 로 변환한다. `get_candles` 는 오래된 → 최신 순이고
  미완성 캔들은 제외하며 (`include_partial=False`), `end` 는 **미포함(exclusive)** 이다.
- 네트워크/API 오류는 `BrokerError` 계열로 변환된다: `AuthenticationError`(401/403/키 오류), `RateLimitError`(429),
  `InsufficientFunds`, `OrderError`(주문 거부/형식), 그 외 `BrokerError`. 엔진은 이를 잡아 로그+알림 후 계속 돈다.
- `HttpClient` 는 GET 만 429/5xx/네트워크 오류를 지수 백오프로 재시도한다. **주문(POST/DELETE) 은 재시도하지 않는다**
  (중복 체결 방지). 주문 후 상태 확인은 엔진이 `get_order` 폴링으로 한다 (`broker.fill_timeout_sec`).
- 수량/가격 반올림은 어댑터의 `round_quantity` / `round_price` 가 담당한다. 시장가 매수 금액 환산에는 수수료 여유를 둔다.
- STOP 주문은 어떤 거래소에도 보내지 않는다. 변동성 돌파의 STOP 은 엔진이 현재가 폴링으로 흉내내고 **시장가** 로 매수한다.
- 롱(매수 → 매도) 만 지원한다. 공매도/선물/마진 없음.
- 모의투자(paper) 와 백테스트, `download` 는 시세만 쓰므로 ccxt 계열은 `sandbox` 를 자동으로 끈다. `--live` 는 설정대로.

---

## Upbit (업비트) — `upbit`

구현 `tradingbot/brokers/upbit.py`. 공식 문서 https://docs.upbit.com/kr/reference/ 기준.

### 키 발급
1. upbit.com 로그인 → **마이페이지 → Open API 관리**.
2. 권한: **자산조회 + 주문조회 + 주문하기** 를 켠다 (출금 권한은 주지 말 것).
3. **접속 IP 등록** 필수. 봇을 돌리는 서버의 공인 IP 를 넣지 않으면 `no_authorization_ip` 오류가 난다.
4. 발급된 Access Key / Secret Key 를 `.env` 의 `UPBIT_ACCESS_KEY` / `UPBIT_SECRET_KEY` 에 넣는다.
5. 모의 서버는 없다. `broker.sandbox` 는 무시된다 → **반드시 `mode: paper` 로 충분히 검증** 한 뒤 소액으로 시작한다.

### 사용 API
| 용도 | 엔드포인트 | 비고 |
|---|---|---|
| 현재가 | `GET /v1/ticker?markets=` | 여러 심볼 한 번에 (`get_tickers`) |
| 캔들 | `GET /v1/candles/minutes/{1,3,5,10,15,30,60,240}`, `/v1/candles/days`, `/v1/candles/weeks` | `count` ≤ 200, `to` 는 exclusive, 최신→과거 응답을 뒤집음. limit > 200 이면 `to` 를 가장 오래된 캔들로 옮기며 자동 페이지네이션 |
| 잔고 | `GET /v1/accounts` | `balance + locked` 가 총액 |
| 주문 가능 정보 | `GET /v1/orders/chance?market=` | 수수료율(bid_fee/ask_fee), 최소 주문 금액. 1시간 캐시 |
| 주문 | `POST /v1/orders` | 시장가 매수 `ord_type=price` + KRW 금액, 시장가 매도 `ord_type=market` + 수량, 지정가 `limit` |
| 주문 조회/취소 | `GET /v1/order?uuid=`, `DELETE /v1/order?uuid=` | 취소 시 `order_not_found` 는 False 반환 |
| 미체결 | `GET /v1/orders/open` (`states[]=wait,watch`, 100개씩 페이지) | |

### 인증
`Authorization: Bearer <JWT>`. 페이로드 `{access_key, nonce(uuid4), query_hash, query_hash_alg: SHA512}`.
`query_hash` 는 **URL 인코딩하지 않은** 쿼리 문자열(`unquote(urlencode(params, doseq=True))`) 의 SHA512 이고, POST 는 JSON 본문의
key=value 쌍을 같은 방식으로 해시한다. 서명은 **HS512**(공식 권장) 기본, `broker.extra.jwt_algorithm: HS256` 으로 바꿀 수 있다.

### 제한/주의
- 레이트리밋(IP/계정): 시세 그룹 10회/초, 주문 그룹 12회/초(어댑터는 8회/초로 여유), 기타 30회/초. `Remaining-Req` 헤더를 읽어
  `sec=0` 이면 다음 초까지 기다린다. 429 는 재시도(GET), 418 은 반복 위반으로 일시 차단이므로 봇을 멈추고 원인을 확인한다.
- 최소 주문 금액: KRW 마켓 **5,000 KRW** (BTC 마켓 0.00005 BTC, USDT 마켓 0.5 USDT). `risk.min_order_value: 5000` 권장.
- 호가 단위(원화, 2025-07-31 개정): 2,000,000 이상 1,000원 … 1,000~5,000 1원, 100~1,000 1원, 10~100 0.1원, 1~10 0.01원 …
  `round_price` 가 ROUND_HALF_UP 으로 맞춘다. 수량은 소수 8자리 내림.
- 시장가 매수는 `floor(수량 × 현재가 / (1 + bid_fee))` KRW 로 환산해 보낸다. 체결 수량은 주문 조회의 `executed_volume`,
  평균가는 체결 내역(`trades`) 으로 계산한다.
- `get_positions` 는 `KRW-{currency}` 키로 돌려주며 `balance + locked` 를 수량으로 본다. 평균매수가 0 인 항목(에어드랍) 은 제외.
- 지원 간격: `1m 3m 5m 10m 15m 30m 1h 4h 1d 1w`. 일봉은 UTC 00:00(한국 09:00) 기준.
- 어댑터 옵션 `broker.extra`: `base_url`, `timeout`, `jwt_algorithm`.

---

## Binance 등 ccxt 거래소 — `binance`, `ccxt`

구현 `tradingbot/brokers/ccxt_broker.py`. ccxt(https://docs.ccxt.com) 통합 API 만 사용. `pip install "tradingbot[ccxt]"` 필요.

### 키 발급 (Binance)
1. binance.com → 프로필 → **API Management** → Create API.
2. 권한은 **Enable Spot & Margin Trading** 만, **출금(Withdrawals) 은 절대 켜지 말 것**. IP 제한을 거는 것을 권장.
3. `.env` 의 `CCXT_API_KEY` / `CCXT_SECRET` (또는 `BINANCE_API_KEY` / `BINANCE_SECRET_KEY`). OKX 등 passphrase 가 있는
   거래소는 `CCXT_PASSWORD`.
4. 테스트넷: https://testnet.binance.vision 에서 별도 키를 만들고 `broker.sandbox: true` 로 둔다 (`set_sandbox_mode(True)`).
   테스트넷은 유동성이 적고 시세가 실제와 다르므로 전략 검증에는 **`mode: paper` + 실제 시세** 를 권장한다.

### 설정
```yaml
broker:
  name: binance          # 또는 ccxt
  exchange_id: binance   # bybit, okx, bithumb ... (name 이 binance/ccxt 면 기본 binance)
  sandbox: true          # 실거래(--live) 에서 테스트넷 사용. paper/backtest 는 자동으로 실제 시세
  extra:
    options: {defaultType: spot}   # ccxt 생성자 options (선택)
```

### 동작
- 캔들 `fetch_ohlcv(symbol, timeframe, since, limit)` 를 `since = end - limit×interval` 부터 앞으로 페이지네이션(최대 1000/회).
  지원 간격 `1m 3m 5m 15m 30m 1h 4h 1d 1w` (**`10m` 없음**).
- 현재가 `fetch_ticker().last`, 잔고 `fetch_balance()`.
- 포지션: 현물에는 평균 매수가가 없다. `get_positions` 는 잔고에서 `{코인}/{USDT|USDC|KRW|USD}` 마켓이 있는 코인만
  `average_price=0.0` 으로 돌려주고, **실제 평균단가는 엔진 상태 파일이 보관** 한다 (재시작 시 동기화에서 유지).
  최소 수량(`limits.amount.min`) 미만의 잔량(dust) 은 무시.
- 주문 `create_order(symbol, market|limit, side, amount, price)`. 수량은 `amount_to_precision`(내림), 가격은 `price_to_precision`.
  시장가 매수는 기본적으로 기초자산 수량으로 보내고, 거래소 옵션 `createMarketBuyOrderRequiresPrice` 가 켜진 경우에만
  Binance 는 `quoteOrderQty`, 그 외는 `price=현재가` 로 ccxt 가 총액을 계산한다.
- 최소 주문 금액은 `limits.cost.min` (Binance `minNotional`). 예시 설정은 `risk.min_order_value: 10` USDT.
- 예외 매핑: `AuthenticationError/PermissionDenied/AccountSuspended` → AuthenticationError, `InsufficientFunds` → InsufficientFunds,
  `RateLimitExceeded/DDoSProtection` → RateLimitError, `InvalidOrder/OrderNotFound` → OrderError, 나머지 → BrokerError.
- `cancel_order` / `get_order` 는 Binance 에서 `symbol` 이 필요하다 (엔진은 항상 넘긴다).
- 지역 차단: 일부 국가/클라우드 IP 에서는 api.binance.com 이 451 을 돌려준다. 그 경우 다른 `exchange_id` 나 Upbit 를 쓴다.

---

## 한국투자증권 KIS Developers — `kis`

구현 `tradingbot/brokers/kis.py`. 공식 샘플(koreainvestment/open-trading-api) 과 KIS Developers 문서 기준. **국내주식 현금 주문만**.

### 키 발급
1. https://apiportal.koreainvestment.com 접속 → 로그인(한국투자증권 계좌 필요) → **API 신청**.
2. **실전투자** 와 **모의투자** 는 별도로 신청하며 앱키/앱시크릿이 다르다. 먼저 모의투자를 신청해
   `broker.sandbox: true` 로 검증한다 (모의투자 서버 `openapivts.koreainvestment.com:29443`, 실전 `openapi.koreainvestment.com:9443`).
3. `.env`:
   - `KIS_APP_KEY`, `KIS_APP_SECRET`
   - `KIS_ACCOUNT_NO` : `12345678-01` (종합계좌 8자리 - 상품코드 2자리) 또는 `1234567801`
   - `KIS_HTS_ID` : **현재 미사용 (예약)**. 이 어댑터가 쓰는 엔드포인트(시세/일봉/분봉/주문/취소/잔고/체결조회/휴장일) 는
     HTS ID 가 필요 없다. 조건검색(psearch)·관심종목·체결통보(WebSocket) 를 붙일 때 쓰도록 필드만 남겨 둔다
4. 접근 토큰(`POST /oauth2/tokenP`) 은 약 1일 유효하고 **발급이 1분당 1회로 제한** 된다. 어댑터가
   `~/.tradingbot/kis_token_<paper|real>.json` (권한 600) 에 캐시해 재사용하며 만료/401/`EGW00123` 시 1회 재발급한다.
   경로는 `broker.extra.token_path` 로 바꿀 수 있다. 토큰 파일은 `.gitignore` 에 포함되어 있다.

### 사용 API
| 용도 | 경로 / tr_id |
|---|---|
| 현재가 | `inquire-price` (FHKST01010100) |
| 일봉/주봉 | `inquire-daily-itemchartprice` (FHKST03010100, 100행/회, 수정주가) |
| 분봉 (당일) | `inquire-time-itemchartprice` (FHKST03010200, 30행/회) |
| 분봉 (과거) | `inquire-time-dailychartprice` (FHKST03010230, 120행/회) — 모의투자 서버 지원 여부 미확인, 실패 시 당일 분봉만 사용 |
| 주문 | `order-cash` (실전 매수 TTTC0012U / 매도 TTTC0011U, 모의 VTTC0012U / VTTC0011U). 시장가 `ORD_DVSN=01`, 지정가 `00` |
| 취소 | `order-rvsecncl` (TTTC0013U / VTTC0013U), 잔량 전부 취소 |
| 잔고/포지션 | `inquire-balance` (TTTC8434R / VTTC8434R) |
| 주문 조회 | `inquire-daily-ccld` (TTTC0081R / VTTC0081R, 최근 7일에서 주문번호로 검색) |
| 휴장일 | `chk-holiday` (CTCA0903R, 일자별 캐시) |

### 동작/주의
- **시세 조회에도 토큰이 필요** 하다. 키가 없으면 백테스트 데이터는 `--source yfinance` (`005930.KS`) 로 받는다.
- 정규장 09:00~15:30 KST 에만 주문이 나간다 (`is_market_open` = 요일/시간 + 휴장일 API). 장외에는 엔진이 심볼을 건너뛴다.
  `broker.extra.holiday_check: false` 면 휴장일 API 를 쓰지 않는다.
- 분봉은 1분봉을 받아 `3m/5m/10m/15m/30m/1h` 로 직접 합성한다. 15:30 동시호가 체결은 마지막 봉에 합치고 장외 체결은 버린다.
  처음 `1h` 300개를 받으려면 약 170회 호출(모의 서버 0.25초 간격이면 45초) 이 든다 → `engine.candle_limit` 을 작게.
- 일봉 timestamp 는 거래일 00:00 UTC (= 09:00 KST), 주봉은 월요일. 일봉은 15:30 KST + 5분 뒤부터 완성으로 본다.
- 수량은 정수 주 내림, 지정가는 KRX 호가단위 반올림. STOP → `OrderError` (엔진이 흉내냄).
- 잔고 `available` 은 D+2 가수도 정산금(`prvs_rcdl_excc_amt`) 기준이라 당일 매도 직후에는 총액보다 클 수 있다.
- 주문 수수료는 API 가 알려주지 않아 `Order.fee` 는 0 이다. 사이징에는 `paper.fee_pct` 를 가정치로 쓴다.
- 요청 간격 `broker.extra.request_interval` (기본 모의 0.25초, 실전 0.05초), `timeout`, `user_agent`.

---

## Alpaca (미국주식) — `alpaca`

구현 `tradingbot/brokers/alpaca.py`. 공식 문서 https://docs.alpaca.markets 기준.

### 키 발급
1. https://app.alpaca.markets 가입 → 오른쪽 상단 **Paper Trading** 전환 → **API Keys → Generate**.
   paper 키와 live 키가 다르므로 `broker.sandbox` (true = `paper-api.alpaca.markets`, false = `api.alpaca.markets`) 와 맞춘다.
2. `.env` 의 `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` (또는 `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY`).
3. 시세(`data.alpaca.markets`) 도 같은 키가 필요하다. 무료 플랜은 `feed=iex` (기본). SIP 구독이 있으면 `broker.extra.feed: sip`.

### 사용 API
| 용도 | 엔드포인트 |
|---|---|
| 캔들 | `GET /v2/stocks/{symbol}/bars?timeframe=1Min..1Week&start&end&limit&adjustment=all&feed&sort=desc` (page_token 페이지네이션) |
| 현재가 | `GET /v2/stocks/{symbol}/trades/latest` → 없으면 `quotes/latest` 중간값 |
| 계좌/포지션 | `GET /v2/account`, `GET /v2/positions` |
| 주문 | `POST /v2/orders` (market: 소수 수량 + `time_in_force=day`, limit: 정수 주 + `limit_price`), `GET/DELETE /v2/orders/{id}`, `GET /v2/orders?status=open` |
| 장 운영 | `GET /v2/clock` (30초 캐시, 실패 시 09:30~16:00 ET 시계) |

### 동작/주의
- 정규장 중 시장가는 **소수점 주식**(소수 9자리 내림) 으로, 장외/지정가는 정수 주로 보낸다. 최소 주문 1 USD. 수수료 0.
- 1Day 캔들은 자정(ET) 이 지나야 완성으로 본다 → 일봉 전략은 다음 세션에 신호가 나온다.
- 레이트리밋 200회/분 (429 → RateLimitError, GET 재시도). 403 + "insufficient/buying power" → InsufficientFunds.
- `engine.close_positions_at_market_close: true` 면 `next_close` 직전 폴링에서 전량 청산한다 (예시 `alpaca_macd.yaml`).
- STOP → `OrderError` (엔진이 흉내냄). 지원 간격은 전부 (`[1-59]Min`, `[1-23]Hour`, `1Day`, `1Week`).

---

## PaperBroker (모의 체결) — `paper`

구현 `tradingbot/brokers/paper.py`. 백테스트와 모의투자가 공용으로 쓰는 시뮬레이션 계좌.

- `tradingbot run` (paper 모드) 은 `broker.name` 의 어댑터를 **시세 전용 data_source** 로 붙인 PaperBroker 를 만든다.
  결제 통화와 자산 구분은 data_source 를 따른다 (`paper.quote_currency` 는 data_source 가 없을 때의 기본값).
- MARKET 은 즉시 `현재가 × (1 ± slippage_pct)` 로 체결하고 수수료(`fee_pct`) 를 현금에서 뺀다. LIMIT/STOP 은 OPEN 으로
  보관했다가 백테스트는 `process_candle(OHLC)`, 모의투자는 `check_pending(현재가)` 로 체결 판정한다.
- 잔고 부족은 `InsufficientFunds`, 최소 주문 금액(`risk.min_order_value` 와 data_source 의 값 중 큰 쪽) 미만은 `OrderError`.
- 상태(`to_dict`) 는 엔진 상태 파일의 `paper_broker` 키에 저장되고 재시작 시 복원된다 (수수료/슬리피지는 현재 설정값 사용).
- `broker.name: paper` 로 두면 시세 출처가 없어 `run/backtest/download` 가 거부한다. 항상 시세 브로커 이름을 쓴다.

## 새 브로커 추가하기

1. `tradingbot/brokers/<name>.py` 에 `BaseBroker` 를 상속해 `get_candles / get_ticker / get_balances / get_positions /
   place_order / cancel_order / get_order / get_open_orders / quote_currency` 와 `@classmethod from_config(cls, config)` 를 구현한다.
   생성자는 키 없이도 동작해야 하고 비공개 API 호출 시에만 `AuthenticationError` 를 던진다.
2. `tradingbot/brokers/__init__.py` 의 `_REGISTRY` 에 `(모듈, 클래스)` 를 추가한다.
3. 테스트는 공식 문서의 응답 스키마를 그대로 따르는 HTTP 모킹(`responses`) 만 쓰고, 캔들 값은 `tests/conftest.py` 의
   실제 Upbit 캔들을 재사용한다 (가짜 시세 금지).
