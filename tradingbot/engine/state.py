"""엔진 상태 파일 (JSON) 과 도메인 모델 직렬화 헬퍼.

- ``StateStore``: 원자적 저장 (같은 디렉터리에 임시 파일 작성 → ``os.replace``), 손상 파일은 백업 후 빈 상태로 시작.
- ``serialize_* / deserialize_*``: Position / Trade / Order / Signal ↔ JSON 호환 dict
  (datetime 은 UTC ISO 문자열, Enum 은 value). 엔진(trader.py)이 사용한다.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from tradingbot.exceptions import DataError
from tradingbot.models import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Signal,
    SignalAction,
    Trade,
    ensure_utc,
    utcnow,
)

logger = logging.getLogger(__name__)

__all__ = [
    "StateStore",
    "deserialize_order",
    "deserialize_position",
    "deserialize_signal",
    "deserialize_trade",
    "dt_from_iso",
    "dt_to_iso",
    "json_default",
    "serialize_order",
    "serialize_position",
    "serialize_signal",
    "serialize_trade",
    "to_jsonable",
]


# ---------------------------------------------------------------------------- 기본 변환
def dt_to_iso(dt: datetime | None) -> str | None:
    """aware/naive datetime → UTC ISO 8601 문자열 (None 은 그대로)."""
    if dt is None:
        return None
    return ensure_utc(dt).isoformat()


def dt_from_iso(s: Any) -> datetime | None:
    """ISO 문자열 (또는 datetime) → UTC aware datetime. None/빈 문자열은 None, 해석 불가는 ValueError."""
    if s is None:
        return None
    if isinstance(s, datetime):
        return ensure_utc(s)
    text = str(s).strip()
    if not text:
        return None
    return ensure_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))


def json_default(obj: Any) -> Any:
    """``json.dump(default=...)`` 용. datetime → ISO, Enum → value, Decimal/기타 → str."""
    if isinstance(obj, datetime):
        return dt_to_iso(obj)
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (set, frozenset, tuple)):
        return list(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    item = getattr(obj, "item", None)  # numpy 스칼라
    if callable(item):
        try:
            return item()
        except (TypeError, ValueError):
            pass
    return str(obj)


def to_jsonable(value: Any) -> Any:
    """중첩 구조를 재귀적으로 JSON 호환 값으로 바꾼다 (meta/raw 저장용)."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return dt_to_iso(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {_key(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in value]
    return json_default(value)


def _key(k: Any) -> str:
    """dict 키: Enum 은 value, 그 외는 str."""
    if isinstance(k, Enum):
        return str(k.value)
    return str(k)


def _opt_float(v: Any) -> float | None:
    return None if v is None else float(v)


def _require(d: Mapping[str, Any], key: str, what: str) -> Any:
    if key not in d:
        raise DataError(f"{what} 직렬화 데이터에 '{key}' 가 없습니다")
    return d[key]


def _check_mapping(d: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(d, Mapping):
        raise DataError(f"{what} 직렬화 데이터는 dict 여야 합니다: {type(d).__name__}")
    return d


# ---------------------------------------------------------------------------- Position
def serialize_position(position: Position) -> dict[str, Any]:
    return {
        "symbol": position.symbol,
        "quantity": float(position.quantity),
        "average_price": float(position.average_price),
        "opened_at": dt_to_iso(position.opened_at),
        "highest_price": _opt_float(position.highest_price),
        "stop_loss": _opt_float(position.stop_loss),
        "take_profit": _opt_float(position.take_profit),
        "meta": to_jsonable(position.meta),
    }


def deserialize_position(d: Mapping[str, Any]) -> Position:
    d = _check_mapping(d, "Position")
    try:
        meta = d.get("meta") or {}
        if not isinstance(meta, Mapping):
            raise DataError(f"Position.meta 는 dict 여야 합니다: {type(meta).__name__}")
        return Position(
            symbol=str(_require(d, "symbol", "Position")),
            quantity=float(_require(d, "quantity", "Position")),
            average_price=float(_require(d, "average_price", "Position")),
            opened_at=dt_from_iso(d.get("opened_at")),
            highest_price=_opt_float(d.get("highest_price")),
            stop_loss=_opt_float(d.get("stop_loss")),
            take_profit=_opt_float(d.get("take_profit")),
            meta=dict(meta),
        )
    except (TypeError, ValueError) as e:
        raise DataError(f"Position 복원 실패: {e}") from e


# ---------------------------------------------------------------------------- Trade
def serialize_trade(trade: Trade) -> dict[str, Any]:
    return {
        "symbol": trade.symbol,
        "side": OrderSide(trade.side).value,
        "quantity": float(trade.quantity),
        "entry_price": float(trade.entry_price),
        "exit_price": float(trade.exit_price),
        "entry_time": dt_to_iso(trade.entry_time),
        "exit_time": dt_to_iso(trade.exit_time),
        "fee": float(trade.fee),
        "reason": trade.reason,
        # 파생값(참고용, 복원 시 사용하지 않음)
        "pnl": float(trade.pnl),
        "pnl_pct": float(trade.pnl_pct),
    }


def deserialize_trade(d: Mapping[str, Any]) -> Trade:
    d = _check_mapping(d, "Trade")
    try:
        entry_time = dt_from_iso(_require(d, "entry_time", "Trade"))
        exit_time = dt_from_iso(_require(d, "exit_time", "Trade"))
        if entry_time is None or exit_time is None:
            raise DataError("Trade 의 entry_time / exit_time 이 비어 있습니다")
        return Trade(
            symbol=str(_require(d, "symbol", "Trade")),
            side=OrderSide(d.get("side", OrderSide.BUY.value)),
            quantity=float(_require(d, "quantity", "Trade")),
            entry_price=float(_require(d, "entry_price", "Trade")),
            exit_price=float(_require(d, "exit_price", "Trade")),
            entry_time=entry_time,
            exit_time=exit_time,
            fee=float(d.get("fee", 0.0) or 0.0),
            reason=str(d.get("reason", "") or ""),
        )
    except (TypeError, ValueError) as e:
        raise DataError(f"Trade 복원 실패: {e}") from e


# ---------------------------------------------------------------------------- Order
def serialize_order(order: Order) -> dict[str, Any]:
    return {
        "id": order.id,
        "symbol": order.symbol,
        "side": OrderSide(order.side).value,
        "type": OrderType(order.type).value,
        "quantity": float(order.quantity),
        "price": _opt_float(order.price),
        "status": OrderStatus(order.status).value,
        "filled_quantity": float(order.filled_quantity),
        "average_price": _opt_float(order.average_price),
        "fee": float(order.fee),
        "created_at": dt_to_iso(order.created_at),
        "updated_at": dt_to_iso(order.updated_at),
        "raw": to_jsonable(order.raw),
    }


def deserialize_order(d: Mapping[str, Any]) -> Order:
    d = _check_mapping(d, "Order")
    try:
        raw = d.get("raw") or {}
        if not isinstance(raw, Mapping):
            raise DataError(f"Order.raw 는 dict 여야 합니다: {type(raw).__name__}")
        created = dt_from_iso(d.get("created_at"))
        return Order(
            id=str(_require(d, "id", "Order")),
            symbol=str(_require(d, "symbol", "Order")),
            side=OrderSide(_require(d, "side", "Order")),
            type=OrderType(_require(d, "type", "Order")),
            quantity=float(_require(d, "quantity", "Order")),
            price=_opt_float(d.get("price")),
            status=OrderStatus(d.get("status", OrderStatus.PENDING.value)),
            filled_quantity=float(d.get("filled_quantity", 0.0) or 0.0),
            average_price=_opt_float(d.get("average_price")),
            fee=float(d.get("fee", 0.0) or 0.0),
            created_at=created if created is not None else utcnow(),
            updated_at=dt_from_iso(d.get("updated_at")),
            raw=dict(raw),
        )
    except (TypeError, ValueError) as e:
        raise DataError(f"Order 복원 실패: {e}") from e


# ---------------------------------------------------------------------------- Signal
def serialize_signal(signal: Signal) -> dict[str, Any]:
    """대기 중인 돌파 신호(pending_breakouts) 저장용."""
    return {
        "action": SignalAction(signal.action).value,
        "symbol": signal.symbol,
        "strength": float(signal.strength),
        "reason": signal.reason,
        "order_type": OrderType(signal.order_type).value,
        "price": _opt_float(signal.price),
        "stop_offset": _opt_float(signal.stop_offset),
        "stop_loss": _opt_float(signal.stop_loss),
        "take_profit": _opt_float(signal.take_profit),
        "max_holding_bars": None if signal.max_holding_bars is None else int(signal.max_holding_bars),
        "meta": to_jsonable(signal.meta),
    }


def deserialize_signal(d: Mapping[str, Any]) -> Signal:
    d = _check_mapping(d, "Signal")
    try:
        meta = d.get("meta") or {}
        if not isinstance(meta, Mapping):
            raise DataError(f"Signal.meta 는 dict 여야 합니다: {type(meta).__name__}")
        mhb = d.get("max_holding_bars")
        return Signal(
            action=SignalAction(_require(d, "action", "Signal")),
            symbol=str(_require(d, "symbol", "Signal")),
            strength=float(d.get("strength", 1.0)),
            reason=str(d.get("reason", "") or ""),
            order_type=OrderType(d.get("order_type", OrderType.MARKET.value)),
            price=_opt_float(d.get("price")),
            stop_offset=_opt_float(d.get("stop_offset")),
            stop_loss=_opt_float(d.get("stop_loss")),
            take_profit=_opt_float(d.get("take_profit")),
            max_holding_bars=None if mhb is None else int(mhb),
            meta=dict(meta),
        )
    except (TypeError, ValueError) as e:
        raise DataError(f"Signal 복원 실패: {e}") from e


# ---------------------------------------------------------------------------- StateStore
class StateStore:
    """JSON 상태 파일. 저장은 원자적(임시 파일 → ``os.replace``), 손상 파일은 백업 후 ``{}``."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> dict[str, Any]:
        """상태를 읽는다. 파일이 없으면 ``{}``. 손상(JSON 오류/최상위가 dict 아님)이면 백업 후 ``{}``."""
        if not self.path.exists():
            logger.debug("상태 파일 없음: %s (빈 상태로 시작)", self.path)
            return {}
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as e:
            raise DataError(f"상태 파일을 읽을 수 없습니다 ({self.path}): {e}") from e
        try:
            data = json.loads(text) if text.strip() else {}
        except ValueError as e:
            self._quarantine(f"JSON 파싱 오류: {e}")
            return {}
        if not isinstance(data, dict):
            self._quarantine(f"최상위가 객체가 아님: {type(data).__name__}")
            return {}
        return data

    def save(self, data: Mapping[str, Any]) -> None:
        """원자적 저장: 같은 디렉터리에 임시 파일을 쓰고 fsync 후 ``os.replace`` 로 교체."""
        if not isinstance(data, Mapping):
            raise DataError(f"상태는 dict 여야 합니다: {type(data).__name__}")
        directory = self.path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise DataError(f"상태 디렉터리를 만들 수 없습니다 ({directory}): {e}") from e

        tmp_path: str | None = None
        try:
            fd, tmp_path = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=str(directory))
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=json_default)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.path)
            tmp_path = None
        except (OSError, TypeError, ValueError) as e:
            raise DataError(f"상태 저장 실패 ({self.path}): {e}") from e
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        logger.debug("상태 저장: %s", self.path)

    def archive(self, label: str) -> Path | None:
        """현재 상태 파일을 ``{name}.{label}-{UTC 시각}`` 으로 옮겨 보관한다 (파일이 없거나 실패하면 None).

        모드/브로커가 바뀐 상태 파일처럼 복원하지는 않지만 지우고 싶지도 않은 경우에 쓴다.
        """
        if not self.path.exists():
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in label) or "backup"
        backup = self.path.with_name(f"{self.path.name}.{safe}-{stamp}")
        try:
            os.replace(self.path, backup)
        except OSError as e:
            logger.warning("상태 파일 %s 보관 실패 (%s): %s", self.path, backup.name, e)
            return None
        logger.info("상태 파일을 %s 로 보관했습니다", backup.name)
        return backup

    # ------------------------------------------------------------------
    def _quarantine(self, why: str) -> None:
        backup = self.archive("corrupt")
        if backup is None:
            logger.warning("손상된 상태 파일 %s 백업 실패 (%s) — 빈 상태로 시작", self.path, why)
            return
        logger.warning("손상된 상태 파일을 %s 로 옮기고 빈 상태로 시작합니다 (%s)", backup.name, why)

    def __repr__(self) -> str:
        return f"<StateStore {self.path}>"
