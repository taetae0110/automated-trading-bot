"""백테스트 성과 지표 (ARCHITECTURE §5 ``metrics.compute_metrics`` / ``metrics.periods_per_year``).

입력은 백테스터가 만든 **bar 종가 기준 자산 곡선**(``pd.Series``, index=UTC timestamp) 과 청산 완료
``Trade`` 목록이다. 네트워크/브로커 의존이 없는 순수 계산 모듈이다.

규약
- 비율은 모두 소수 (0.1 = 10%). 무위험 수익률은 0 으로 둔다.
- 수익률은 bar 단위 단순 수익률 ``equity.pct_change()`` 이며, 표준편차는 ``ddof=1`` 이다.
- ``sharpe = mean / std * sqrt(periods_per_year)``; std 가 0 이거나 수익률이 2개 미만이면 0.
- ``sortino`` 의 분모는 하방 편차 ``sqrt(Σ min(r, 0)² / (n - 1))`` (목표 수익률 0, ddof=1) 이다.
- ``max_drawdown`` 은 누적 최고 자산 대비 하락폭의 최댓값(양수), ``max_drawdown_duration_bars`` 는
  이전 최고 자산 아래에 머문 가장 긴 연속 bar 수 (회복하지 못하면 마지막 bar 까지).
- ``cagr`` 는 첫/마지막 timestamp 사이의 경과 시간을 연 단위로 환산해 계산한다
  (index 가 datetime 이 아니면 ``bar 수 / periods_per_year`` 로 대체).
- ``profit_factor`` 는 손실 거래가 없고 이익 거래가 있으면 ``inf`` 다 (JSON 저장 시 호출자가 처리).
- ``initial_equity`` (백테스터는 초기 현금을 넘긴다) 를 주면 총수익률·낙폭·bar 수익률의 기준점이 첫 bar
  종가가 아니라 그 값이 된다: ``total_return = last / initial_equity - 1``, 누적 최고 자산은
  ``initial_equity`` 에서 시작하고 bar 0 의 수익률(``equity[0] / initial_equity - 1``) 이 수익률 열에
  들어간다. 그래야 bar 0 체결의 수수료/슬리피지/가격 변동이 지표에서 빠지지 않고
  ``BacktestResult.total_return`` 과 일치한다. 주지 않으면 첫 bar 종가(``equity[0]``) 기준이다.
"""

from __future__ import annotations

import logging
import math
import numbers
import unicodedata
from collections.abc import Sequence
from typing import Any

import numpy as np
import pandas as pd

from tradingbot.exceptions import DataError
from tradingbot.models import AssetClass, Trade, interval_to_seconds

logger = logging.getLogger(__name__)

__all__ = [
    "METRIC_KEYS",
    "compute_metrics",
    "drawdown_series",
    "format_metrics",
    "periods_per_year",
]

#: ``compute_metrics`` 가 반환하는 키 (계약 순서).
METRIC_KEYS: tuple[str, ...] = (
    "total_return",
    "cagr",
    "max_drawdown",
    "max_drawdown_duration_bars",
    "volatility",
    "sharpe",
    "sortino",
    "calmar",
    "win_rate",
    "profit_factor",
    "num_trades",
    "avg_pnl",
    "avg_pnl_pct",
    "avg_win",
    "avg_loss",
    "best_trade_pct",
    "worst_trade_pct",
    "avg_holding_hours",
    "exposure",
)

#: 연 환산 기준 (암호화폐: 365일 24시간, 주식: 연 252 거래일 × 6.5 시간 정규장, 1주 = 52주)
_CRYPTO_SECONDS_PER_YEAR = 365.0 * 86400.0
_STOCK_TRADING_DAYS = 252.0
_STOCK_SESSION_HOURS = 6.5
_STOCK_WEEKS_PER_YEAR = 52.0
_SECONDS_PER_YEAR = 365.25 * 86400.0


# ---------------------------------------------------------------------------- 연 환산 주기
def periods_per_year(interval: str, asset_class: AssetClass | str) -> float:
    """1년에 들어가는 bar 수.

    - crypto: 24시간 365일 거래 → ``365*86400 / interval_seconds`` (1d=365, 1h=8760, 1w=365/7).
    - stock : 연 252 거래일, 하루 6.5 시간 정규장 → 1d=252, 1h=252*6.5, 분봉은 그 비례, 1w=52.
    알 수 없는 interval 은 ``ValueError`` (``interval_to_seconds`` 와 동일).
    """
    secs = interval_to_seconds(interval)
    try:
        ac = AssetClass(asset_class)
    except ValueError as e:
        raise ValueError(f"알 수 없는 자산 구분: {asset_class!r} (가능: crypto, stock)") from e

    if ac == AssetClass.CRYPTO:
        return _CRYPTO_SECONDS_PER_YEAR / secs

    # 주식: 정규장 시간만 bar 가 생긴다.
    if interval == "1w":
        return _STOCK_WEEKS_PER_YEAR
    if interval == "1d":
        return _STOCK_TRADING_DAYS
    return _STOCK_TRADING_DAYS * _STOCK_SESSION_HOURS * 3600.0 / secs


# ---------------------------------------------------------------------------- 내부 헬퍼
def _as_equity_series(equity: pd.Series | Sequence[float] | np.ndarray) -> pd.Series:
    if isinstance(equity, pd.Series):
        s = equity
    else:
        s = pd.Series(list(equity) if not isinstance(equity, np.ndarray) else equity)
    if len(s) == 0:
        raise DataError("자산 곡선(equity) 이 비어 있어 지표를 계산할 수 없습니다")
    try:
        values = s.astype(float)
    except (TypeError, ValueError) as e:
        raise DataError(f"자산 곡선 값이 숫자가 아닙니다: {e}") from e
    if values.isna().any() or not np.isfinite(values.to_numpy()).all():
        raise DataError("자산 곡선에 NaN/inf 값이 있습니다")
    return values


def _bar_returns(equity: pd.Series, initial_equity: float | None = None) -> pd.Series:
    """bar 단위 단순 수익률. ``initial_equity`` 가 있으면 bar 0 의 수익률(초기 자산 대비) 도 포함한다.

    직전 자산이 0 이면 그 구간은 제외한다.
    """
    cur = equity.to_numpy(dtype=float)
    if initial_equity is None:
        prev, cur = cur[:-1], cur[1:]
    else:
        prev = np.concatenate(([float(initial_equity)], cur[:-1]))
    with np.errstate(divide="ignore", invalid="ignore"):
        rets = pd.Series((cur - prev) / prev, dtype=float)
    return rets.replace([np.inf, -np.inf], np.nan).dropna()


def drawdown_series(equity: pd.Series, initial_equity: float | None = None) -> pd.Series:
    """누적 최고 자산 대비 하락폭 (0 이상의 소수, 같은 인덱스).

    ``initial_equity`` 를 주면 그것을 첫 bar 이전의 최고 자산으로 삼아 bar 0 부터 낙폭을 잰다.
    """
    values = _as_equity_series(equity)
    running_max = values.cummax()
    if initial_equity is not None:
        running_max = running_max.clip(lower=float(initial_equity))
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = 1.0 - values / running_max
    return dd.replace([np.inf, -np.inf], np.nan).fillna(0.0).clip(lower=0.0)


def _max_drawdown_duration(dd: pd.Series) -> int:
    """하락 상태(dd > 0) 가 연속된 가장 긴 bar 수."""
    longest = 0
    current = 0
    for value in dd.to_numpy():
        if value > 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _elapsed_years(index: pd.Index, n_bars: int, ppy: float) -> float:
    """첫/마지막 timestamp 사이 경과 연수. datetime index 가 아니면 bar 수 기반."""
    if isinstance(index, pd.DatetimeIndex) and len(index) >= 2:
        first = index[0]
        last = index[-1]
        if pd.isna(first) or pd.isna(last):
            return 0.0
        seconds = (last - first).total_seconds()
        return max(seconds, 0.0) / _SECONDS_PER_YEAR
    if ppy > 0 and n_bars >= 2:
        return (n_bars - 1) / ppy
    return 0.0


def _cagr(total_return: float, years: float) -> float:
    if years <= 0.0:
        return 0.0
    growth = 1.0 + total_return
    if growth <= 0.0:
        return -1.0
    try:
        return growth ** (1.0 / years) - 1.0
    except OverflowError:
        logger.warning("CAGR 계산 오버플로 (총수익률 %.4f, %.6f 년) → inf", total_return, years)
        return math.inf


def _exposure_from_trades(index: pd.Index, trades: Sequence[Trade]) -> float:
    """Trade 의 진입~청산 구간으로 '포지션 보유 bar 비율' 을 근사한다 (datetime index 일 때만)."""
    if not isinstance(index, pd.DatetimeIndex) or len(index) == 0 or not trades:
        return 0.0
    ts = index.tz_convert("UTC") if index.tz is not None else index.tz_localize("UTC")
    held = np.zeros(len(ts), dtype=bool)
    for t in trades:
        entry = pd.Timestamp(t.entry_time)
        exit_ = pd.Timestamp(t.exit_time)
        if entry.tzinfo is None:
            entry = entry.tz_localize("UTC")
        if exit_.tzinfo is None:
            exit_ = exit_.tz_localize("UTC")
        held |= (ts >= entry) & (ts < exit_)
    return float(held.mean())


def _trade_stats(trades: Sequence[Trade]) -> dict[str, float]:
    n = len(trades)
    if n == 0:
        return {
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "num_trades": 0,
            "avg_pnl": 0.0,
            "avg_pnl_pct": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "best_trade_pct": 0.0,
            "worst_trade_pct": 0.0,
            "avg_holding_hours": 0.0,
        }
    pnls = np.array([float(t.pnl) for t in trades], dtype=float)
    pcts = np.array([float(t.pnl_pct) for t in trades], dtype=float)
    hours = np.array([float(t.holding_seconds) / 3600.0 for t in trades], dtype=float)
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    gross_profit = float(wins.sum())
    gross_loss = float(-losses.sum())
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = math.inf
    else:
        profit_factor = 0.0
    return {
        "win_rate": float(len(wins)) / n,
        "profit_factor": profit_factor,
        "num_trades": n,
        "avg_pnl": float(pnls.mean()),
        "avg_pnl_pct": float(pcts.mean()),
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "best_trade_pct": float(pcts.max()),
        "worst_trade_pct": float(pcts.min()),
        "avg_holding_hours": float(hours.mean()),
    }


# ---------------------------------------------------------------------------- 지표 계산
def compute_metrics(
    equity: pd.Series | Sequence[float] | np.ndarray,
    trades: Sequence[Trade],
    periods_per_year: float,
    *,
    exposure: float | None = None,
    initial_equity: float | None = None,
) -> dict[str, float]:
    """자산 곡선과 거래 목록으로 성과 지표 dict 를 만든다 (키는 ``METRIC_KEYS``).

    Args:
        equity: bar 종가 기준 자산 곡선. index 가 UTC datetime 이면 CAGR 은 경과 시간으로 계산한다.
        trades: 청산 완료 Trade 목록.
        periods_per_year: 연 환산 bar 수 (``periods_per_year()``).
        exposure: 포지션 보유 bar 비율. 백테스터가 직접 계산해 넘긴다. None 이면 Trade 구간으로 근사.
        initial_equity: 첫 bar 이전의 자산(백테스터의 초기 현금). 주면 총수익률/낙폭/bar 수익률의
            기준점이 되어 bar 0 의 손익도 지표에 들어간다. None 이면 첫 bar 종가 기준.
    """
    if not isinstance(periods_per_year, (int, float)) or isinstance(periods_per_year, bool):
        raise ValueError(f"periods_per_year 는 숫자여야 합니다: {periods_per_year!r}")
    ppy = float(periods_per_year)
    if not math.isfinite(ppy) or ppy <= 0:
        raise ValueError(f"periods_per_year 는 0 보다 큰 유한한 값이어야 합니다: {periods_per_year!r}")
    initial: float | None = None
    if initial_equity is not None:
        if not isinstance(initial_equity, numbers.Real) or isinstance(initial_equity, bool):
            raise ValueError(f"initial_equity 는 숫자여야 합니다: {initial_equity!r}")
        initial = float(initial_equity)
        if not math.isfinite(initial) or initial <= 0:
            raise ValueError(f"initial_equity 는 0 보다 큰 유한한 값이어야 합니다: {initial_equity!r}")

    values = _as_equity_series(equity)
    n_bars = len(values)
    first = initial if initial is not None else float(values.iloc[0])
    last = float(values.iloc[-1])
    total_return = (last / first - 1.0) if first != 0 else 0.0

    rets = _bar_returns(values, initial)
    n_rets = len(rets)
    sqrt_ppy = math.sqrt(ppy)
    if n_rets >= 2:
        mean = float(rets.mean())
        std = float(rets.std(ddof=1))
        downside = rets.clip(upper=0.0)
        downside_dev = math.sqrt(float((downside**2).sum()) / (n_rets - 1))
    else:
        mean = std = downside_dev = 0.0
    volatility = std * sqrt_ppy if std > 0 else 0.0
    sharpe = mean / std * sqrt_ppy if std > 0 else 0.0
    sortino = mean / downside_dev * sqrt_ppy if downside_dev > 0 else 0.0

    dd = drawdown_series(values, initial)
    max_dd = float(dd.max()) if n_bars else 0.0
    max_dd_bars = _max_drawdown_duration(dd)

    years = _elapsed_years(values.index, n_bars, ppy)
    cagr = _cagr(total_return, years)
    calmar = cagr / max_dd if max_dd > 0 and math.isfinite(cagr) else 0.0

    if exposure is None:
        exposure_value = _exposure_from_trades(values.index, trades)
    else:
        exposure_value = float(exposure)
        if not math.isfinite(exposure_value):
            raise ValueError(f"exposure 가 유효하지 않습니다: {exposure!r}")
        exposure_value = min(max(exposure_value, 0.0), 1.0)

    out: dict[str, float] = {
        "total_return": float(total_return),
        "cagr": float(cagr),
        "max_drawdown": max_dd,
        "max_drawdown_duration_bars": int(max_dd_bars),
        "volatility": float(volatility),
        "sharpe": float(sharpe),
        "sortino": float(sortino),
        "calmar": float(calmar),
    }
    out.update(_trade_stats(trades))
    out["exposure"] = exposure_value
    # 키 순서를 계약 순서로 고정
    return {k: out[k] for k in METRIC_KEYS}


# ---------------------------------------------------------------------------- 출력
_LABELS: tuple[tuple[str, str, str], ...] = (
    # (키, 한국어 라벨, 포맷 종류: ret=부호 있는 %, pct=부호 없는 %, money=부호 있는 금액)
    ("total_return", "총 수익률", "ret"),
    ("cagr", "연환산 수익률(CAGR)", "ret"),
    ("max_drawdown", "최대 낙폭(MDD)", "pct"),
    ("max_drawdown_duration_bars", "최대 낙폭 지속(bar)", "int"),
    ("volatility", "연환산 변동성", "pct"),
    ("sharpe", "샤프 비율", "ratio"),
    ("sortino", "소르티노 비율", "ratio"),
    ("calmar", "칼마 비율", "ratio"),
    ("win_rate", "승률", "pct"),
    ("profit_factor", "손익비(Profit Factor)", "ratio"),
    ("num_trades", "거래 횟수", "int"),
    ("avg_pnl", "평균 손익", "money"),
    ("avg_pnl_pct", "평균 손익률", "ret"),
    ("avg_win", "평균 이익(이익 거래)", "money"),
    ("avg_loss", "평균 손실(손실 거래)", "money"),
    ("best_trade_pct", "최고 거래 수익률", "ret"),
    ("worst_trade_pct", "최악 거래 수익률", "ret"),
    ("avg_holding_hours", "평균 보유 시간(시간)", "hours"),
    ("exposure", "시장 노출 비율", "pct"),
)


def _fmt_value(value: Any, kind: str) -> str:
    if value is None:
        return "-"
    try:
        x = float(value)
    except (TypeError, ValueError):
        return str(value)
    if math.isnan(x):
        return "-"
    if math.isinf(x):
        return "∞" if x > 0 else "-∞"
    if kind == "ret":
        return f"{x * 100:+.2f}%"
    if kind == "pct":
        return f"{x * 100:.2f}%"
    if kind == "int":
        return f"{int(round(x)):,d}"
    if kind == "money":
        return f"{x:+,.2f}" if abs(x) < 1000 else f"{x:+,.0f}"
    if kind == "hours":
        return f"{x:,.1f}"
    return f"{x:.2f}"


def _display_width(text: str) -> int:
    """터미널 표시 폭 (한글 등 전각 문자는 2칸)."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def format_metrics(metrics: dict[str, Any]) -> str:
    """지표 dict 를 사람이 읽는 한국어 표 형태(plain text)로 만든다. 알 수 없는 키도 뒤에 덧붙인다."""
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for key, label, kind in _LABELS:
        if key in metrics:
            rows.append((label, _fmt_value(metrics[key], kind)))
            seen.add(key)
    for key, value in metrics.items():
        if key not in seen:
            rows.append((key, _fmt_value(value, "ratio")))
    if not rows:
        return "(지표 없음)"
    width = max(_display_width(label) for label, _ in rows)
    lines = [f"{label}{' ' * (width - _display_width(label))} : {value}" for label, value in rows]
    return "\n".join(lines)
