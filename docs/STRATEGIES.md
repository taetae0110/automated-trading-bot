# 전략 (STRATEGIES)

이 문서는 내장 전략의 **규칙 · 파라미터 · 주의점** 을 설명한다. 전략 코드는 `tradingbot/strategies/` 에 있고,
목록과 기본 파라미터는 `tradingbot strategies` 로 확인할 수 있다.

```
tradingbot strategies
tradingbot backtest -c config/config.yaml --strategy rsi -p period=7 -p oversold=35
```

## 공통 규약

- 모든 전략은 `BaseStrategy` 를 상속하고 두 단계로 동작한다.
  1. `prepare(df)` : 캔들 DataFrame(`timestamp, open, high, low, close, volume`) 에 지표 컬럼을 **벡터 연산** 으로
     추가한다. `shift/rolling/ewm` 만 사용하므로 미래 데이터를 참조하지 않는다 (prefix 불변성 테스트로 검증).
  2. `signal_at(symbol, df, i)` : 행 `i` 까지만 보고 `Signal` 을 만든다.
- 백테스터는 `prepare` 1회 후 `i` 를 늘리며 `signal_at` 을 호출하고, 실시간 엔진은 최근 N 개 캔들로
  `generate_signal` (= `prepare` + 마지막 행 `signal_at`) 을 호출한다. **백테스트와 실거래가 같은 코드 경로** 를 탄다.
- `warmup` : 신호를 내는 데 필요한 최소 캔들 수. `i < warmup-1` 이거나 지표가 NaN 이면 HOLD
  (`reason` 이 `워밍업 (k/warmup)` 또는 `지표 미산출 (NaN)`). `engine.candle_limit` 는 `warmup` 이상이어야 한다
  (`validate-config` 가 검사).
- `Signal.action` 은 BUY / SELL / HOLD, `strength` 는 BUY/SELL 1.0, HOLD 0.0.
  `reason` 은 짧은 한국어 사유, `meta` 는 지표 값 + 파라미터 + `timestamp`(신호 캔들 ISO) 로 JSON 직렬화 가능하다.
- 전략은 **포지션 보유 여부를 모른다.** "이미 보유 중인데 BUY", "포지션 없는데 SELL" 은 엔진/백테스터가 무시한다.
- 파라미터는 `__init__` 에서 검증한다. 잘못된 값은 `ValueError` 이며 레지스트리(`create_strategy`) 를 거치면
  `ConfigError` 가 된다. 정수 파라미터는 `5`, `5.0`, `"5"` 를 모두 받아 정수로 정규화한다 (bool 은 거부).
- 지표 구현 (`strategies/indicators.py`, pandas 전용, TA-Lib 없음)
  - `sma` : `rolling(period).mean()`
  - `ema` : `ewm(span=period, adjust=False)`, 처음 `period-1` 행은 NaN 으로 가림 (재귀는 0행부터 시작)
  - `rsi` : Wilder 방식 (`ewm(alpha=1/period, adjust=False)`), 첫 변화량을 시드로 사용. 변동이 전혀 없는 구간은 NaN
  - `macd` : `ema(fast) - ema(slow)`, 시그널은 MACD 가 유효해지는 행부터의 EMA(signal), 히스토그램 = MACD - 시그널
  - `bollinger` : 중심선 SMA(period), 상/하단 = 중심 ± num_std × 모집단 표준편차(ddof=0)
  - `true_range` / `atr` : Wilder ATR (첫 행 TR 은 NaN)
  - `crossover(a, b)` / `crossunder(a, b)` : 직전 행에서 `a <= b` 였다가 현재 행에서 `a > b` (반대) 인 행. 어느 한쪽이
    NaN 인 행은 False

## 전략 표

| 이름 | 클래스 | 기본 파라미터 | warmup | 매수 | 매도 |
|---|---|---|---|---|---|
| `sma_cross` | `SMACrossStrategy` | `fast=10, slow=30` | `slow+1` (31) | 단기 SMA 가 장기 SMA 상향 돌파 (골든크로스) | 하향 돌파 (데드크로스) |
| `ema_cross` | `EMACrossStrategy` | `fast=12, slow=26` | `slow+1` (27) | 단기 EMA 가 장기 EMA 상향 돌파 | 하향 돌파 |
| `rsi` | `RSIStrategy` | `period=14, oversold=30, overbought=70` | `period+2` (16) | RSI 가 oversold 상향 돌파 | RSI 가 overbought 하향 돌파 |
| `bollinger` | `BollingerStrategy` | `period=20, num_std=2.0, mode="reversion"` | `period+1` (21) | reversion: 하단밴드 아래→위 복귀 / breakout: 상단 상향 돌파 | reversion: 종가 ≥ 중심선 / breakout: 중심선 하향 이탈 |
| `macd` | `MACDStrategy` | `fast=12, slow=26, signal=9` | `slow+signal` (35) | MACD 가 시그널 상향 돌파 | 하향 돌파 |
| `volatility_breakout` | `VolatilityBreakoutStrategy` | `k=0.5, ma_period=0` | `ma_period+1` (필터 없으면 1) | 다음 캔들 시가 + k×(고가-저가) 상향 돌파 (STOP) | 없음 (`max_holding_bars=1` 로 다음 캔들 시가 청산) |

---

## sma_cross / ema_cross — 이동평균 교차

**규칙**
- 단기선이 장기선을 **상향 돌파** 한 행 → BUY (`SMA10 > SMA30 골든크로스`)
- 단기선이 장기선을 **하향 돌파** 한 행 → SELL (`SMA10 < SMA30 데드크로스`)
- 그 외 → HOLD

**파라미터**

| 이름 | 기본 | 제약 | 설명 |
|---|---|---|---|
| `fast` | 10 / 12 | 1 이상 정수, `fast < slow` | 단기 이동평균 기간 |
| `slow` | 30 / 26 | 2 이상 정수 | 장기 이동평균 기간 |

**추가 컬럼** `sma_fast, sma_slow, cross_up, cross_down` (EMA 는 `ema_fast, ema_slow, ...`).

**주의**
- 횡보장에서 잦은 교차(whipsaw) 로 수수료가 누적된다. 손절/추적손절과 함께 쓰거나 기간을 늘려 완화한다.
- EMA 의 초기 구간은 시드 효과가 있으므로 백테스트 구간 앞쪽에 충분한 여유 캔들을 두는 편이 좋다
  (엔진은 `candle_limit` 만큼의 캔들을 매번 넘기므로 자연히 해결된다).
- 1시간봉 이하에서는 `slow` 를 너무 작게 잡으면 노이즈에 반응한다. 예시 설정은 1h 기준 `10/30` 이다.

---

## rsi — RSI 과매도/과매수

**규칙**
- RSI 가 `oversold` 선을 **상향 돌파** (과매도 탈출) → BUY (`RSI(14) 30 상향 돌파 (RSI 32.1)`)
- RSI 가 `overbought` 선을 **하향 돌파** (과매수 이탈) → SELL
- 그 외 → HOLD

**파라미터**

| 이름 | 기본 | 제약 | 설명 |
|---|---|---|---|
| `period` | 14 | 2 이상 정수 | Wilder RSI 기간 |
| `oversold` | 30 | 0 < oversold < overbought | 과매도 기준선 |
| `overbought` | 70 | oversold < overbought < 100 | 과매수 기준선 |

**추가 컬럼** `rsi, rsi_cross_up, rsi_cross_down`.

**주의**
- 이 전략은 "선을 넘는 순간" 에만 신호를 낸다. RSI 가 30 아래에 오래 머물러도 다시 30 을 넘기 전까지는 매수하지 않는다.
- RSI 는 ewm 시드 방식이므로 TA-Lib 의 SMA 시드 RSI 와 초기 수십 개 값이 약간 다르다 (충분한 워밍업 뒤에는 수렴).
- 강한 추세장에서는 과매수 하향 돌파 매도가 너무 이르거나, 과매도 상향 돌파 매수가 하락 추세의 되돌림에 걸릴 수 있다.
  `ma` 필터가 없으므로 리스크 설정(손절, 일일 손실 한도) 에 의존한다.

---

## bollinger — 볼린저 밴드

**규칙 (`mode="reversion"`, 기본, 평균 회귀)**
- 종가가 하단밴드 아래에 있다가 **위로 복귀** → BUY (`BB(20,2) 종가 하단밴드 95,000,000 상향 복귀 (매수)`)
- 종가가 **중심선 이상** → SELL (목표 도달, `BB(20,2) 종가 ... >= 중심선 ... (청산)`)
- 두 조건이 같은 행에서 동시에 참이면 (큰 갭 상승) SELL 을 우선한다 — 이미 목표가에 도달한 진입은 기대 수익이 없기 때문.

**규칙 (`mode="breakout"`, 추세 추종)**
- 종가가 상단밴드를 **상향 돌파** → BUY
- 종가가 중심선을 **하향 돌파** → SELL

**파라미터**

| 이름 | 기본 | 제약 | 설명 |
|---|---|---|---|
| `period` | 20 | 2 이상 정수 | 중심선(SMA)/표준편차 기간 |
| `num_std` | 2.0 | 0 보다 큰 실수 | 밴드 폭 (표준편차 배수) |
| `mode` | `reversion` | `reversion` / `breakout` | 매매 규칙 선택 |

**추가 컬럼** `bb_mid, bb_upper, bb_lower, bb_buy, bb_sell`.

**주의**
- reversion 모드는 "밴드 안으로 돌아올 때" 사므로 급락 중에는 진입하지 않지만, 바닥을 찍고 반등하는 첫 캔들을 놓칠 수 있다.
- reversion 의 SELL 은 중심선 **이상이면 매 캔들** 나온다. 포지션이 없으면 엔진/백테스터가 무시하므로 문제는 없지만
  `notify_on_signal: true` 로 두면 알림이 잦다.
- breakout 모드는 추세 초입에 늦게 들어가고 중심선 이탈 때 늦게 나온다. 추적손절(`trailing_stop_pct`) 과 궁합이 좋다.

---

## macd — MACD 교차

**규칙**
- MACD 선이 시그널 선을 **상향 돌파** → BUY (`MACD(12,26,9) 시그널 상향 돌파 (hist +1.2e+05)`)
- MACD 선이 시그널 선을 **하향 돌파** → SELL
- 그 외 → HOLD

**파라미터**

| 이름 | 기본 | 제약 | 설명 |
|---|---|---|---|
| `fast` | 12 | 1 이상 정수, `fast < slow` | 단기 EMA |
| `slow` | 26 | 2 이상 정수 | 장기 EMA |
| `signal` | 9 | 1 이상 정수 | 시그널 EMA |

**추가 컬럼** `macd, macd_signal, macd_hist, cross_up, cross_down`.

**주의**
- 시그널 EMA 는 MACD 가 유효해지는 행(`slow-1`) 에서 시작하므로 `ewm` 을 0행부터 돌리는 한 줄짜리 구현과 초기 값이
  다르다 (워밍업 이후 수렴). warmup 이 35 로 가장 길다.
- 히스토그램 크기(`meta["hist"]`) 는 가격 단위이므로 종목 간 비교에는 쓰지 말 것.

---

## volatility_breakout — 변동성 돌파 (래리 윌리엄스, 일봉 권장)

**규칙** (행 `i` = 가장 최근 **완성** 캔들)
1. `range = high_i - low_i`. `range <= 0` 이면 HOLD.
2. `ma_period > 0` 이고 `close_i < SMA(ma_period)` 이면 HOLD (추세 필터).
3. 그 외에는 `Signal(action=BUY, order_type=STOP, price=None, stop_offset=k × range, max_holding_bars=1)` 을 낸다.
   의미: **다음 캔들의 시가 + k×range 를 상향 돌파하면 매수하고, 그 다음 캔들 시가에 매도** 한다.
4. SELL 신호는 내지 않는다. 청산은 `max_holding_bars=1` (다음 캔들 시가) 또는 리스크 매니저(손절 등) 가 담당한다.

**파라미터**

| 이름 | 기본 | 제약 | 설명 |
|---|---|---|---|
| `k` | 0.5 | 0 보다 큰 실수 | 돌파 계수. 클수록 진입이 보수적 (0.3~0.7 권장) |
| `ma_period` | 0 | 0 이상 정수 | 0 이면 필터 없음, 양수면 종가가 그 기간 SMA 아래일 때 진입 금지 |

**추가 컬럼** `vb_range, vb_offset, vb_ma` (필터 없으면 `vb_ma` 는 전부 NaN, 스키마 고정용).

**엔진/백테스터에서의 처리**
- 트리거 가격은 "다음 캔들 시가" 를 알게 된 시점에 계산한다.
  - 백테스터: 다음 bar 시작에 `trigger = open + stop_offset`. `open >= trigger` 면 시가(갭), 아니면 `high >= trigger` 일 때
    트리거 가격에 체결 (슬리피지 적용). 그 bar 안에 돌파가 없으면 주문은 취소된다 (1 캔들 유효).
  - 실시간 엔진: 새 캔들 이벤트에서 "진행 중 캔들의 시가"(`get_candles(include_partial=True)` 마지막 캔들, 실패 시 현재가)
    + `stop_offset` 을 `pending_breakouts` 에 등록하고, 매 폴링마다 현재가가 트리거 이상이면 **시장가** 로 매수한다.
    거래소에 STOP 주문을 보내지 않는다. 다음 캔들이 시작되면 대기 주문은 만료된다.
- 보유 중에는 BUY 를 무시하므로 하루 한 번만 진입한다. 다음 캔들 시가에 만료 청산된 뒤 같은 bar 에서 새 돌파로 재진입할 수 있다.
- 손절(`stop_loss_pct`) 은 그대로 적용된다. 익절은 보통 쓰지 않는다 (예시 설정 참고).

**주의**
- 일봉 기준 전략이다. Upbit 일봉은 UTC 00:00 (한국 09:00) 에 바뀐다. 돌파 감시를 위해 `poll_seconds` 를 짧게(10초 정도) 둔다.
- 24시간 거래되는 암호화폐에서 "다음 캔들 시가 매도" 는 UTC 00:00 직후에 실행된다. 주식에서는 장 시작 직후다.
- 트리거 가격은 bar 중간에 계산되므로 캔들 지연(`stale_data_minutes`) 이 크면 하루를 건너뛸 수 있다.

---

## 새 전략 추가하기

1. `tradingbot/strategies/my_strategy.py` 에 `BaseStrategy` 를 구현한다.

```python
from __future__ import annotations

from typing import Any

import pandas as pd

from tradingbot.exceptions import DataError
from tradingbot.models import Signal, SignalAction
from tradingbot.strategies import indicators as ind
from tradingbot.strategies.base import BaseStrategy


class DonchianStrategy(BaseStrategy):
    name = "donchian"
    description = "돈치안 채널: N일 최고가 돌파 매수, N일 최저가 이탈 매도"
    default_params: dict[str, Any] = {"period": 20}

    def __init__(self, **params: Any) -> None:
        super().__init__(**params)
        self.period = ind.as_period(self.params["period"], "period", minimum=2)
        self.params["period"] = self.period

    @property
    def warmup(self) -> int:
        return self.period + 1

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        if "close" not in df.columns:
            raise DataError(f"{self.name}: close 컬럼이 없습니다")
        out = df.copy()
        # 직전 N개 캔들의 최고/최저 (현재 행은 제외 → shift(1), 미래 참조 없음)
        out["dc_high"] = out["high"].rolling(self.period).max().shift(1)
        out["dc_low"] = out["low"].rolling(self.period).min().shift(1)
        return out

    def signal_at(self, symbol: str, df: pd.DataFrame, i: int) -> Signal:
        if i < self.warmup - 1:
            return Signal.hold(symbol, reason=f"워밍업 ({i + 1}/{self.warmup})")
        row = df.iloc[i]
        if pd.isna(row["dc_high"]) or pd.isna(row["dc_low"]):
            return Signal.hold(symbol, reason="지표 미산출 (NaN)")
        meta = {"close": float(row["close"]), "dc_high": float(row["dc_high"]), "dc_low": float(row["dc_low"]),
                "period": self.period, "timestamp": row["timestamp"].isoformat()}
        if row["close"] > row["dc_high"]:
            return Signal(action=SignalAction.BUY, symbol=symbol, reason=f"{self.period}일 최고가 돌파", meta=meta)
        if row["close"] < row["dc_low"]:
            return Signal(action=SignalAction.SELL, symbol=symbol, reason=f"{self.period}일 최저가 이탈", meta=meta)
        return Signal.hold(symbol, reason="채널 내부")
```

2. `tradingbot/strategies/__init__.py` 의 `_REGISTRY` 에 등록한다.

```python
_REGISTRY["donchian"] = ("tradingbot.strategies.my_strategy", "DonchianStrategy")
```

3. 테스트를 추가한다 (`tests/test_strategies.py` 참고). 특히 **미래 참조 없음** 을 확인하는 prefix 테스트를 꼭 넣는다:
   `strategy.prepare(df.iloc[:k])` 의 마지막 행 신호와 전체 `df` 를 준비한 뒤 `signal_at(k-1)` 이 같아야 한다.
   실제 캔들은 `tests/conftest.py` 의 `daily_df` / `candles_df` 픽스처를 쓴다 (가짜 시세 금지).

4. 설정에서 사용한다.

```yaml
strategy:
  name: donchian
  params:
    period: 20
```

## 파라미터 탐색 팁

- `tradingbot backtest -c ... -p period=10`, `-p period=20` 처럼 바꿔 가며 돌리고 `--report` 로 JSON 을 남겨 비교한다.
- 과최적화를 피하려면 (1) 기간을 두 구간으로 나눠 앞 구간에서 고른 파라미터를 뒤 구간에서 검증하고, (2) 수수료·슬리피지를
  실제보다 보수적으로 두며, (3) 거래 수가 너무 적은 결과(예: 5건 미만) 는 통계적 의미가 없다고 본다.
- 간격을 바꾸면(1h → 1d) 같은 파라미터라도 의미가 달라진다. 예시 설정의 기간은 각 간격에 맞춰 둔 값이다.
