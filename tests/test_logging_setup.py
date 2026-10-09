"""logging_setup.setup_logging 테스트 (루트 로거 상태는 각 테스트 후 복원)."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest
from rich.logging import RichHandler

from tradingbot.config import LoggingConfig
from tradingbot.exceptions import ConfigError
from tradingbot.logging_setup import (
    CONSOLE_HANDLER_NAME,
    FILE_BACKUP_COUNT,
    FILE_HANDLER_NAME,
    FILE_MAX_BYTES,
    NOISY_LOGGERS,
    parse_level,
    setup_logging,
    teardown_logging,
)

TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[+-]\d{4} (DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+"
)


@pytest.fixture(autouse=True)
def _restore_root_logger():
    root = logging.getLogger()
    saved_level = root.level
    saved_handlers = list(root.handlers)
    saved_noisy = {name: logging.getLogger(name).level for name in NOISY_LOGGERS}
    yield
    teardown_logging()
    for h in list(root.handlers):
        if h not in saved_handlers:
            root.removeHandler(h)
    for h in saved_handlers:
        if h not in root.handlers:
            root.addHandler(h)
    root.setLevel(saved_level)
    for name, lvl in saved_noisy.items():
        logging.getLogger(name).setLevel(lvl)


def managed_handlers() -> list[logging.Handler]:
    return [
        h for h in logging.getLogger().handlers if h.get_name() in (CONSOLE_HANDLER_NAME, FILE_HANDLER_NAME)
    ]


def console_handler() -> RichHandler | None:
    for h in logging.getLogger().handlers:
        if h.get_name() == CONSOLE_HANDLER_NAME:
            assert isinstance(h, RichHandler)
            return h
    return None


def file_handler() -> RotatingFileHandler | None:
    for h in logging.getLogger().handlers:
        if h.get_name() == FILE_HANDLER_NAME:
            assert isinstance(h, RotatingFileHandler)
            return h
    return None


class TestParseLevel:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("DEBUG", logging.DEBUG),
            ("info", logging.INFO),
            (" Warning ", logging.WARNING),
            ("ERROR", logging.ERROR),
            ("critical", logging.CRITICAL),
            ("WARN", logging.WARNING),
            ("30", 30),
            (10, 10),
        ],
    )
    def test_valid(self, value, expected) -> None:
        assert parse_level(value) == expected

    @pytest.mark.parametrize("bad", ["VERBOSE", "", "loud", True])
    def test_invalid(self, bad) -> None:
        with pytest.raises(ConfigError):
            parse_level(bad)


class TestSetupLogging:
    def test_console_and_file_handlers(self, tmp_path: Path) -> None:
        log_file = tmp_path / "logs" / "nested" / "bot.log"
        setup_logging(LoggingConfig(level="INFO", file=str(log_file)))
        root = logging.getLogger()
        assert root.level == logging.INFO
        ch = console_handler()
        assert ch is not None
        assert ch.markup is False
        fh = file_handler()
        assert fh is not None
        assert fh.maxBytes == FILE_MAX_BYTES == 5 * 1024 * 1024
        assert fh.backupCount == FILE_BACKUP_COUNT == 5
        assert fh.encoding == "utf-8"
        assert Path(fh.baseFilename) == log_file.resolve()
        assert log_file.parent.is_dir()

    def test_idempotent(self, tmp_path: Path) -> None:
        cfg = LoggingConfig(level="DEBUG", file=str(tmp_path / "bot.log"))
        setup_logging(cfg)
        first = managed_handlers()
        assert len(first) == 2
        setup_logging(cfg)
        setup_logging(cfg)
        second = managed_handlers()
        assert len(second) == 2
        assert len(logging.getLogger().handlers) == len(set(map(id, logging.getLogger().handlers)))
        # 이전 핸들러는 닫히고 교체된다
        assert all(h not in second for h in first)
        assert all(
            fh.stream is None or fh.stream.closed for fh in first if isinstance(fh, RotatingFileHandler)
        )

    def test_file_format_has_timestamp_and_korean(self, tmp_path: Path) -> None:
        log_file = tmp_path / "bot.log"
        setup_logging(LoggingConfig(level="INFO", file=str(log_file)), console=False)
        logging.getLogger("tradingbot.test").info("체결 완료 %s", "KRW-BTC")
        logging.getLogger("tradingbot.test").debug("보이면 안 됨")
        for h in managed_handlers():
            h.flush()
        lines = log_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert TIMESTAMP_RE.match(lines[0]), lines[0]
        assert "tradingbot.test: 체결 완료 KRW-BTC" in lines[0]

    def test_console_false_and_no_file(self) -> None:
        setup_logging(LoggingConfig(level="WARNING", file=None), console=False)
        assert managed_handlers() == []
        assert logging.getLogger().level == logging.WARNING

    def test_switching_file_off_removes_file_handler(self, tmp_path: Path) -> None:
        setup_logging(LoggingConfig(level="INFO", file=str(tmp_path / "a.log")))
        assert file_handler() is not None
        setup_logging(LoggingConfig(level="INFO", file=None))
        assert file_handler() is None
        assert console_handler() is not None

    def test_level_applies_to_handlers(self, tmp_path: Path) -> None:
        setup_logging(LoggingConfig(level="debug", file=str(tmp_path / "a.log")))
        assert logging.getLogger().level == logging.DEBUG
        assert console_handler().level == logging.DEBUG
        assert file_handler().level == logging.DEBUG

    def test_invalid_level_raises_before_touching_handlers(self) -> None:
        with pytest.raises(ConfigError):
            setup_logging(LoggingConfig(level="LOUD"))
        assert managed_handlers() == []

    def test_noisy_libraries_quieted(self) -> None:
        logging.getLogger("urllib3").setLevel(logging.DEBUG)
        logging.getLogger("ccxt").setLevel(logging.DEBUG)
        setup_logging(LoggingConfig(level="DEBUG", file=None), console=False)
        assert logging.getLogger("urllib3").level == logging.WARNING
        assert logging.getLogger("ccxt").level == logging.WARNING
        assert logging.getLogger("urllib3.connectionpool").getEffectiveLevel() == logging.WARNING
        assert logging.getLogger("tradingbot.engine").getEffectiveLevel() == logging.DEBUG

    def test_file_dir_creation_failure_raises_configerror(self, tmp_path: Path) -> None:
        blocker = tmp_path / "file"
        blocker.write_text("x")
        with pytest.raises(ConfigError):
            setup_logging(LoggingConfig(level="INFO", file=str(blocker / "sub" / "bot.log")), console=False)

    def test_rotation_keeps_backups(self, tmp_path: Path) -> None:
        log_file = tmp_path / "rot.log"
        setup_logging(LoggingConfig(level="INFO", file=str(log_file)), console=False)
        fh = file_handler()
        assert fh is not None
        fh.maxBytes = 2_000  # 테스트용으로 작게 — 설정값 자체는 위에서 검증
        lg = logging.getLogger("tradingbot.rotation")
        for i in range(400):
            lg.info("라인 %04d " + "x" * 50, i)
        fh.flush()
        rotated = sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("rot.log"))
        assert "rot.log" in rotated
        assert f"rot.log.{FILE_BACKUP_COUNT}" in rotated
        assert f"rot.log.{FILE_BACKUP_COUNT + 1}" not in rotated

    def test_teardown(self, tmp_path: Path) -> None:
        setup_logging(LoggingConfig(level="INFO", file=str(tmp_path / "t.log")))
        assert managed_handlers()
        teardown_logging()
        assert managed_handlers() == []
