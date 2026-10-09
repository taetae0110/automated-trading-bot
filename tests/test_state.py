"""StateStore 와 직렬화 헬퍼 테스트.

가격이 필요한 곳은 conftest 의 실제 KRW-BTC 캔들을 쓴다 (네트워크 없으면 skip). 가짜 시세는 만들지 않는다.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from tradingbot.engine import state as state_mod
from tradingbot.engine.state import (
    StateStore,
    deserialize_order,
    deserialize_position,
    deserialize_signal,
    deserialize_trade,
    dt_from_iso,
    dt_to_iso,
    json_default,
    serialize_order,
    serialize_position,
    serialize_signal,
    serialize_trade,
    to_jsonable,
)
from tradingbot.exceptions import DataError
from tradingbot.models import (
    Candle,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Signal,
    SignalAction,
    Trade,
)

BTC = "KRW-BTC"
KST = ZoneInfo("Asia/Seoul")


def utc(y: int, m: int, d: int, hh: int = 0, mm: int = 0, ss: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, ss, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "data" / "state.json")


def leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name != "state.json")


# ----------------------------------------------------------------------------- StateStore
class TestStateStore:
    def test_load_missing_returns_empty(self, store: StateStore) -> None:
        assert store.exists is False
        assert store.load() == {}
        assert not store.path.exists()  # load 는 파일을 만들지 않는다

    def test_save_creates_parent_dirs_and_round_trips(self, store: StateStore) -> None:
        data = {"a": 1, "b": {"c": [1, 2, 3]}, "한글": "값"}
        store.save(data)
        assert store.exists
        assert store.load() == data
        raw = store.path.read_text(encoding="utf-8")
        assert "한글" in raw  # ensure_ascii=False
        assert raw.endswith("\n")

    def test_accepts_str_path(self, tmp_path: Path) -> None:
        s = StateStore(str(tmp_path / "s.json"))
        assert isinstance(s.path, Path)
        s.save({"x": 1})
        assert s.load() == {"x": 1}

    def test_json_default_types(self, store: StateStore) -> None:
        data = {
            "dt": utc(2026, 1, 2, 3, 4, 5),
            "naive": datetime(2026, 1, 2, 3, 4, 5),
            "kst": datetime(2026, 1, 2, 12, 0, tzinfo=KST),
            "dec": Decimal("1234.5678"),
            "enum": OrderSide.BUY,
            "status": OrderStatus.FILLED,
            "set": {1},
            "tuple": (1, 2),
            "np_f": np.float64(1.5),
            "np_i": np.int64(7),
            "path": Path("/tmp/x"),
        }
        store.save(data)
        loaded = store.load()
        assert loaded["dt"] == "2026-01-02T03:04:05+00:00"
        assert loaded["naive"] == "2026-01-02T03:04:05+00:00"
        assert loaded["kst"] == "2026-01-02T03:00:00+00:00"
        assert loaded["dec"] == "1234.5678"
        assert loaded["enum"] == "buy"
        assert loaded["status"] == "filled"
        assert loaded["set"] == [1]
        assert loaded["tuple"] == [1, 2]
        assert loaded["np_f"] == 1.5
        assert loaded["np_i"] == 7
        assert loaded["path"] == "/tmp/x"

    def test_save_overwrites_atomically_without_leftovers(self, store: StateStore, monkeypatch) -> None:
        store.save({"v": 1})
        calls: list[tuple[str, str]] = []
        real_replace = os.replace

        def spy(src, dst, *a, **k):
            calls.append((str(src), str(dst)))
            return real_replace(src, dst, *a, **k)

        monkeypatch.setattr(state_mod.os, "replace", spy)
        store.save({"v": 2})
        assert store.load() == {"v": 2}
        assert len(calls) == 1
        src, dst = calls[0]
        assert dst == str(store.path)
        assert Path(src).parent == store.path.parent  # 같은 디렉터리의 임시 파일
        assert Path(src).name.startswith(".state.json.")
        assert leftovers(store.path.parent) == []

    def test_failed_write_keeps_previous_file(self, store: StateStore, monkeypatch) -> None:
        store.save({"v": "original"})

        def boom(*a, **k):
            raise ValueError("직렬화 실패 시뮬레이션")

        monkeypatch.setattr(state_mod.json, "dump", boom)
        with pytest.raises(DataError, match="상태 저장 실패"):
            store.save({"v": "new"})
        monkeypatch.undo()
        assert store.load() == {"v": "original"}
        assert leftovers(store.path.parent) == []

    def test_failed_replace_cleans_temp(self, store: StateStore, monkeypatch) -> None:
        def boom(*a, **k):
            raise OSError("replace 실패 시뮬레이션")

        monkeypatch.setattr(state_mod.os, "replace", boom)
        with pytest.raises(DataError):
            store.save({"v": 1})
        monkeypatch.undo()
        assert not store.path.exists()
        assert leftovers(store.path.parent) == []

    def test_save_non_dict_raises(self, store: StateStore) -> None:
        with pytest.raises(DataError):
            store.save(["not", "a", "dict"])  # type: ignore[arg-type]
        assert not store.path.exists()

    def test_corrupt_json_is_backed_up(self, store: StateStore, caplog: pytest.LogCaptureFixture) -> None:
        store.path.parent.mkdir(parents=True)
        garbage = '{"unterminated": [1, 2'
        store.path.write_text(garbage, encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="tradingbot.engine.state"):
            assert store.load() == {}
        assert not store.path.exists()
        backups = [p for p in store.path.parent.iterdir() if p.name.startswith("state.json.corrupt-")]
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == garbage
        assert any("손상된 상태 파일" in r.getMessage() for r in caplog.records)
        # 백업 뒤 저장/로드는 정상
        store.save({"fresh": True})
        assert store.load() == {"fresh": True}

    def test_non_object_json_is_corrupt(self, store: StateStore) -> None:
        store.path.parent.mkdir(parents=True)
        store.path.write_text("[1, 2, 3]", encoding="utf-8")
        assert store.load() == {}
        assert not store.path.exists()
        assert any(p.name.startswith("state.json.corrupt-") for p in store.path.parent.iterdir())

    def test_empty_file_is_empty_state(self, store: StateStore) -> None:
        store.path.parent.mkdir(parents=True)
        store.path.write_text("   \n", encoding="utf-8")
        assert store.load() == {}
        assert store.path.exists()  # 빈 파일은 손상으로 보지 않는다

    def test_backup_failure_is_logged_not_raised(self, store: StateStore, monkeypatch, caplog) -> None:
        store.path.parent.mkdir(parents=True)
        store.path.write_text("{bad", encoding="utf-8")

        def boom(*a, **k):
            raise OSError("rename 불가")

        monkeypatch.setattr(state_mod.os, "replace", boom)
        with caplog.at_level(logging.WARNING, logger="tradingbot.engine.state"):
            assert store.load() == {}
        assert any("백업 실패" in r.getMessage() for r in caplog.records)

    def test_unreadable_file_raises_dataerror(self, store: StateStore) -> None:
        if os.geteuid() == 0:
            pytest.skip("root 는 권한 검사를 우회한다")
        store.save({"v": 1})
        store.path.chmod(0)
        try:
            with pytest.raises(DataError):
                store.load()
        finally:
            store.path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def test_path_is_directory_raises(self, tmp_path: Path) -> None:
        s = StateStore(tmp_path)
        with pytest.raises(DataError):
            s.save({"v": 1})

    def test_repr(self, store: StateStore) -> None:
        assert "state.json" in repr(store)


# ----------------------------------------------------------------------------- 기본 변환
class TestConversions:
    def test_dt_round_trip(self) -> None:
        dt = utc(2026, 3, 4, 5, 6, 7)
        assert dt_to_iso(dt) == "2026-03-04T05:06:07+00:00"
        assert dt_from_iso(dt_to_iso(dt)) == dt
        assert dt_to_iso(None) is None
        assert dt_from_iso(None) is None
        assert dt_from_iso("") is None
        assert dt_from_iso("2026-03-04T05:06:07Z") == dt
        assert dt_from_iso("2026-03-04T14:06:07+09:00") == dt
        assert dt_from_iso(datetime(2026, 3, 4, 5, 6, 7)) == dt  # naive → UTC
        assert dt_from_iso(dt).tzinfo == timezone.utc
        with pytest.raises(ValueError):
            dt_from_iso("yesterday")

    def test_to_jsonable_nested(self) -> None:
        value = {
            "a": [utc(2026, 1, 1), {OrderSide.BUY: Decimal("1")}],
            "b": (np.float32(2.5), frozenset({3})),
        }
        out = to_jsonable(value)
        assert out == {"a": ["2026-01-01T00:00:00+00:00", {"buy": "1"}], "b": [2.5, [3]]}
        json.dumps(out)

    def test_json_default_fallback_str(self) -> None:
        class Weird:
            def __str__(self) -> str:
                return "weird"

        assert json_default(Weird()) == "weird"
        assert json_default(b"bytes") == "bytes"


# ----------------------------------------------------------------------------- 직렬화 헬퍼
@pytest.fixture
def last(daily_candles: list[Candle]) -> Candle:
    return daily_candles[-1]


class TestPositionSerialization:
    def test_round_trip(self, last: Candle) -> None:
        pos = Position(
            symbol=BTC,
            quantity=0.0123,
            average_price=last.close,
            opened_at=last.timestamp,
            highest_price=last.high,
            stop_loss=last.close * 0.97,
            take_profit=None,
            meta={
                "entry_bar_ts": last.timestamp,
                "max_holding_bars": 1,
                "side": OrderSide.BUY,
                "n": np.int64(3),
            },
        )
        d = serialize_position(pos)
        json.dumps(d)  # default 없이 JSON 직렬화 가능
        assert d["opened_at"] == dt_to_iso(last.timestamp)
        assert d["meta"] == {
            "entry_bar_ts": dt_to_iso(last.timestamp),
            "max_holding_bars": 1,
            "side": "buy",
            "n": 3,
        }
        back = deserialize_position(d)
        assert back.symbol == BTC
        assert back.quantity == pos.quantity
        assert back.average_price == pos.average_price
        assert back.opened_at == last.timestamp
        assert back.highest_price == last.high
        assert back.stop_loss == pos.stop_loss
        assert back.take_profit is None
        assert back.meta == d["meta"]
        assert back.meta is not d["meta"]  # 복사본

    def test_minimal(self, last: Candle) -> None:
        pos = deserialize_position({"symbol": BTC, "quantity": "0.5", "average_price": last.close})
        assert pos.quantity == 0.5
        assert pos.opened_at is None and pos.highest_price is None and pos.meta == {}

    @pytest.mark.parametrize(
        "bad",
        [
            {"quantity": 1.0, "average_price": 1.0},  # symbol 없음
            {"symbol": BTC, "average_price": 1.0},
            {"symbol": BTC, "quantity": "abc", "average_price": 1.0},
            {"symbol": BTC, "quantity": 1.0, "average_price": 1.0, "opened_at": "not-a-date"},
            {"symbol": BTC, "quantity": 1.0, "average_price": 1.0, "meta": ["list"]},
            "not a dict",
        ],
    )
    def test_invalid(self, bad) -> None:
        with pytest.raises(DataError):
            deserialize_position(bad)


class TestTradeSerialization:
    def test_round_trip(self, daily_candles: list[Candle]) -> None:
        a, b = daily_candles[-10], daily_candles[-1]
        trade = Trade(
            symbol=BTC,
            side=OrderSide.BUY,
            quantity=0.01,
            entry_price=a.close,
            exit_price=b.close,
            entry_time=a.timestamp,
            exit_time=b.timestamp,
            fee=12.5,
            reason="익절",
        )
        d = serialize_trade(trade)
        json.dumps(d)
        assert d["side"] == "buy"
        assert d["entry_time"] == dt_to_iso(a.timestamp)
        assert d["pnl"] == trade.pnl and d["pnl_pct"] == trade.pnl_pct
        back = deserialize_trade(d)
        assert back == trade

    def test_naive_times_become_utc(self, last: Candle) -> None:
        d = {
            "symbol": BTC,
            "quantity": 1,
            "entry_price": last.open,
            "exit_price": last.close,
            "entry_time": "2026-01-01T00:00:00",
            "exit_time": "2026-01-01T09:00:00+09:00",
        }
        t = deserialize_trade(d)
        assert t.entry_time == utc(2026, 1, 1)
        assert t.exit_time == utc(2026, 1, 1)
        assert t.side == OrderSide.BUY  # 기본값
        assert t.fee == 0.0 and t.reason == ""

    @pytest.mark.parametrize(
        "bad",
        [
            {
                "symbol": BTC,
                "quantity": 1,
                "entry_price": 1,
                "exit_price": 1,
                "entry_time": None,
                "exit_time": "2026-01-01T00:00:00+00:00",
            },
            {
                "symbol": BTC,
                "quantity": 1,
                "entry_price": 1,
                "exit_price": 1,
                "entry_time": "2026-01-01T00:00:00+00:00",
            },
            {
                "symbol": BTC,
                "side": "short",
                "quantity": 1,
                "entry_price": 1,
                "exit_price": 1,
                "entry_time": "2026-01-01T00:00:00+00:00",
                "exit_time": "2026-01-01T00:00:00+00:00",
            },
            [],
        ],
    )
    def test_invalid(self, bad) -> None:
        with pytest.raises(DataError):
            deserialize_trade(bad)


class TestOrderSerialization:
    def test_round_trip(self, last: Candle) -> None:
        order = Order(
            id="abc-123",
            symbol=BTC,
            side=OrderSide.SELL,
            type=OrderType.LIMIT,
            quantity=0.02,
            price=last.high,
            status=OrderStatus.PARTIALLY_FILLED,
            filled_quantity=0.01,
            average_price=last.high,
            fee=3.3,
            created_at=last.timestamp,
            updated_at=last.timestamp + timedelta(minutes=5),
            raw={"uuid": "abc-123", "ts": last.timestamp, "state": OrderStatus.OPEN},
        )
        d = serialize_order(order)
        json.dumps(d)
        assert d["side"] == "sell" and d["type"] == "limit" and d["status"] == "partially_filled"
        assert d["raw"] == {"uuid": "abc-123", "ts": dt_to_iso(last.timestamp), "state": "open"}
        back = deserialize_order(d)
        assert back.id == order.id
        assert back.side == OrderSide.SELL and back.type == OrderType.LIMIT
        assert back.status == OrderStatus.PARTIALLY_FILLED
        assert back.price == last.high and back.average_price == last.high
        assert back.filled_quantity == 0.01 and back.fee == 3.3
        assert back.created_at == last.timestamp
        assert back.updated_at == order.updated_at
        assert back.raw == d["raw"]
        assert back.remaining_quantity == pytest.approx(0.01)

    def test_defaults(self) -> None:
        before = datetime.now(timezone.utc)
        o = deserialize_order({"id": 1, "symbol": BTC, "side": "buy", "type": "market", "quantity": 1})
        assert o.id == "1"
        assert o.status == OrderStatus.PENDING
        assert o.price is None and o.average_price is None and o.updated_at is None
        assert o.created_at >= before - timedelta(seconds=1)
        assert o.created_at.tzinfo is not None
        assert o.raw == {}

    @pytest.mark.parametrize(
        "bad",
        [
            {"symbol": BTC, "side": "buy", "type": "market", "quantity": 1},  # id 없음
            {"id": "1", "symbol": BTC, "side": "buy", "type": "trailing", "quantity": 1},
            {"id": "1", "symbol": BTC, "side": "buy", "type": "market", "quantity": 1, "status": "done"},
            {"id": "1", "symbol": BTC, "side": "buy", "type": "market", "quantity": 1, "raw": "x"},
            None,
        ],
    )
    def test_invalid(self, bad) -> None:
        with pytest.raises(DataError):
            deserialize_order(bad)


class TestSignalSerialization:
    def test_round_trip_breakout(self, last: Candle) -> None:
        sig = Signal(
            action=SignalAction.BUY,
            symbol=BTC,
            strength=0.8,
            reason="변동성 돌파 대기",
            order_type=OrderType.STOP,
            price=None,
            stop_offset=(last.high - last.low) * 0.5,
            max_holding_bars=1,
            meta={"timestamp": dt_to_iso(last.timestamp), "range": last.high - last.low},
        )
        d = serialize_signal(sig)
        json.dumps(d)
        assert d["action"] == "buy" and d["order_type"] == "stop"
        back = deserialize_signal(d)
        assert back == sig

    def test_defaults_and_hold(self) -> None:
        hold = Signal.hold(BTC, reason="대기")
        back = deserialize_signal(serialize_signal(hold))
        assert back.is_hold and back.strength == 0.0 and back.reason == "대기"
        minimal = deserialize_signal({"action": "sell", "symbol": BTC})
        assert minimal.order_type == OrderType.MARKET and minimal.strength == 1.0
        assert minimal.max_holding_bars is None and minimal.meta == {}

    @pytest.mark.parametrize(
        "bad",
        [{"symbol": BTC}, {"action": "short", "symbol": BTC}, {"action": "buy", "symbol": BTC, "meta": 1}, 5],
    )
    def test_invalid(self, bad) -> None:
        with pytest.raises(DataError):
            deserialize_signal(bad)


class TestEngineStateShape:
    """엔진이 저장할 법한 전체 상태를 StateStore 로 왕복시킨다."""

    def test_full_round_trip(self, store: StateStore, daily_candles: list[Candle]) -> None:
        a, b = daily_candles[-5], daily_candles[-1]
        pos = Position(
            symbol=BTC, quantity=0.01, average_price=a.close, opened_at=a.timestamp, highest_price=b.high
        )
        trade = Trade(BTC, OrderSide.BUY, 0.01, a.close, b.close, a.timestamp, b.timestamp, fee=1.0)
        order = Order(
            "o1",
            BTC,
            OrderSide.BUY,
            OrderType.MARKET,
            0.01,
            status=OrderStatus.FILLED,
            created_at=a.timestamp,
        )
        sig = Signal(SignalAction.BUY, BTC, order_type=OrderType.STOP, stop_offset=100.0, max_holding_bars=1)
        state = {
            "positions": {BTC: serialize_position(pos)},
            "pending_breakouts": {
                BTC: {"trigger": b.open + 100.0, "signal": serialize_signal(sig), "expires": b.timestamp}
            },
            "last_candle_ts": {BTC: b.timestamp},
            "trades": [serialize_trade(trade)],
            "orders": [serialize_order(order)],
            "risk": {
                "version": 1,
                "day": "2026-01-01",
                "daily_pnl": -1.5,
                "day_start_equity": 1e7,
                "daily_trades": 1,
            },
        }
        store.save(state)
        loaded = store.load()
        assert deserialize_position(loaded["positions"][BTC]).average_price == a.close
        assert deserialize_signal(loaded["pending_breakouts"][BTC]["signal"]) == sig
        assert dt_from_iso(loaded["pending_breakouts"][BTC]["expires"]) == b.timestamp
        assert dt_from_iso(loaded["last_candle_ts"][BTC]) == b.timestamp
        assert deserialize_trade(loaded["trades"][0]) == trade
        assert deserialize_order(loaded["orders"][0]).status == OrderStatus.FILLED
        assert loaded["risk"]["daily_pnl"] == -1.5
