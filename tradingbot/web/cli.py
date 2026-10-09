"""``tradingbot web`` 명령 (typer).

``tradingbot/cli.py`` 는 ``from tradingbot.web.cli import web`` 후 ``app.command("web")(web)`` 으로 등록한다.
이 모듈은 ``tradingbot.cli`` 를 import 하지 않는다 (순환 import 방지) — FastAPI 앱도 명령 실행 시점에 import 한다.
"""

from __future__ import annotations

import logging
import sys
import threading
import webbrowser
from pathlib import Path

import typer

from tradingbot.config import load_config
from tradingbot.exceptions import TradingBotError
from tradingbot.logging_setup import setup_logging
from tradingbot.notify import mask_secrets

logger = logging.getLogger(__name__)

__all__ = ["CONFIG_OPTION", "DEFAULT_HOST", "DEFAULT_PORT", "web"]

DEFAULT_CONFIG_PATH = Path("config/config.yaml")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080

#: ``tradingbot/cli.py`` 의 CONFIG_OPTION 과 같은 형태
CONFIG_OPTION = typer.Option(
    DEFAULT_CONFIG_PATH, "--config", "-c", help="설정 YAML 경로 (기본 config/config.yaml)"
)


def _browser_url(host: str, port: int) -> str:
    h = host.strip()
    if h in ("0.0.0.0", "::", ""):
        h = "127.0.0.1"
    elif ":" in h and not h.startswith("["):
        h = f"[{h}]"
    return f"http://{h}:{port}/"


def web(
    config_path: Path = CONFIG_OPTION,
    host: str = typer.Option(DEFAULT_HOST, "--host", help="바인드 주소 (기본 127.0.0.1, 로컬 전용)"),
    port: int = typer.Option(DEFAULT_PORT, "--port", "-p", min=1, max=65535, help="포트 (기본 8080)"),
    no_open: bool = typer.Option(False, "--no-open", help="시작 시 브라우저를 열지 않는다"),
) -> None:
    """웹 대시보드를 띄운다: 모의투자 엔진 시작/정지, 포지션·거래·자산 곡선, 백테스트, 로그, 설정 보기."""
    from tradingbot.web.service import is_loopback_host

    try:
        config = load_config(config_path)
    except TradingBotError as e:
        print(f"[오류] {type(e).__name__}: {mask_secrets(str(e))}", file=sys.stderr)
        raise typer.Exit(code=1) from None
    # 웹에서 시작한 엔진의 로그가 파일에도 남아야 로그 탭에서 보인다 (run 과 동일하게 콘솔 + 파일)
    setup_logging(config.logging)
    if not is_loopback_host(host):
        logger.warning(
            "대시보드에는 인증이 없습니다. --host %s 로 외부에 노출하면 누구나 모의투자 엔진을 시작/정지하고 "
            "로그/설정을 볼 수 있습니다. 신뢰할 수 있는 네트워크에서만 사용하세요",
            host,
        )
    if config.is_live:
        logger.warning(
            "mode=live 설정입니다. 웹에서는 실거래 엔진을 시작할 수 없으며(조회만), 실거래는 tradingbot run --live 로만 시작됩니다"
        )

    from tradingbot.web.app import create_app

    try:
        import uvicorn
    except ImportError:  # pragma: no cover - 선택 의존성
        print("[오류] uvicorn 이 설치되어 있지 않습니다: pip install 'tradingbot[web]'", file=sys.stderr)
        raise typer.Exit(code=1) from None

    app = create_app(config, config_path=config_path)
    url = _browser_url(host, port)
    print(f"대시보드: {url}  (종료: Ctrl+C)", file=sys.stderr)
    if not no_open:
        timer = threading.Timer(1.0, lambda: webbrowser.open(url))
        timer.daemon = True
        timer.start()
    # log_config=None: uvicorn 이 루트 로거 설정을 덮어쓰지 않게 (봇의 콘솔/파일 핸들러 유지)
    uvicorn.run(app, host=host, port=port, log_config=None, access_log=False)
