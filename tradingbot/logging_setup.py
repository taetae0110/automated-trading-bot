"""로깅 설정: rich 콘솔 핸들러 + 회전 파일 핸들러.

- ``setup_logging(config.logging)`` 을 여러 번 호출해도 핸들러가 중복되지 않는다 (이름으로 식별해 교체).
- 파일 핸들러는 5MB × 5개 회전, 디렉터리는 자동 생성.
- urllib3/ccxt 등 시끄러운 라이브러리는 WARNING 으로 낮춘다.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

from tradingbot.config import LoggingConfig
from tradingbot.exceptions import ConfigError

CONSOLE_HANDLER_NAME = "tradingbot.console"
FILE_HANDLER_NAME = "tradingbot.file"

FILE_MAX_BYTES = 5 * 1024 * 1024
FILE_BACKUP_COUNT = 5

FILE_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
FILE_DATEFMT = "%Y-%m-%d %H:%M:%S%z"
CONSOLE_FORMAT = "%(name)s: %(message)s"
CONSOLE_TIME_FORMAT = "[%Y-%m-%d %H:%M:%S]"

#: WARNING 으로 낮출 외부 라이브러리 로거
NOISY_LOGGERS: tuple[str, ...] = (
    "urllib3",
    "ccxt",
    "requests",
    "charset_normalizer",
    "yfinance",
    "peewee",
    "asyncio",
)


def parse_level(level: str | int) -> int:
    """ "INFO" / "debug" / 20 같은 값을 logging 레벨 정수로. 모르는 값은 ConfigError."""
    if isinstance(level, bool):
        raise ConfigError(f"알 수 없는 로그 레벨: {level!r}")
    if isinstance(level, int):
        return level
    text = str(level).strip().upper()
    if text.isdigit():
        return int(text)
    value = logging.getLevelName(text)
    if isinstance(value, int):
        return value
    raise ConfigError(f"알 수 없는 로그 레벨: {level!r} (가능: DEBUG, INFO, WARNING, ERROR, CRITICAL)")


def _remove_managed_handlers(root: logging.Logger) -> None:
    for h in list(root.handlers):
        if h.get_name() in (CONSOLE_HANDLER_NAME, FILE_HANDLER_NAME):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:  # noqa: BLE001 - 종료 중 핸들러 정리 실패는 무시
                pass


def setup_logging(config: LoggingConfig, *, console: bool = True) -> None:
    """루트 로거를 설정한다. 여러 번 호출해도 안전하다 (기존 봇 핸들러를 교체)."""
    level = parse_level(config.level)
    root = logging.getLogger()
    root.setLevel(level)
    _remove_managed_handlers(root)

    if console:
        handler = RichHandler(
            level=level,
            console=Console(stderr=True),
            markup=False,
            rich_tracebacks=False,
            show_path=False,
            omit_repeated_times=False,
            log_time_format=CONSOLE_TIME_FORMAT,
        )
        handler.setFormatter(logging.Formatter(CONSOLE_FORMAT))
        handler.set_name(CONSOLE_HANDLER_NAME)
        root.addHandler(handler)

    if config.file:
        path = Path(config.file)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                path, maxBytes=FILE_MAX_BYTES, backupCount=FILE_BACKUP_COUNT, encoding="utf-8"
            )
        except OSError as e:
            raise ConfigError(f"로그 파일을 열 수 없습니다 ({path}): {e}") from e
        file_handler.setLevel(level)
        file_handler.setFormatter(logging.Formatter(FILE_FORMAT, datefmt=FILE_DATEFMT))
        file_handler.set_name(FILE_HANDLER_NAME)
        root.addHandler(file_handler)

    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    logging.getLogger(__name__).debug(
        "로깅 설정: level=%s console=%s file=%s (python %s)",
        logging.getLevelName(level),
        console,
        config.file or "-",
        sys.version.split()[0],
    )


def teardown_logging() -> None:
    """봇이 설치한 핸들러를 제거한다 (테스트/종료 시)."""
    _remove_managed_handlers(logging.getLogger())


__all__ = [
    "CONSOLE_HANDLER_NAME",
    "FILE_BACKUP_COUNT",
    "FILE_HANDLER_NAME",
    "FILE_MAX_BYTES",
    "NOISY_LOGGERS",
    "parse_level",
    "setup_logging",
    "teardown_logging",
]
