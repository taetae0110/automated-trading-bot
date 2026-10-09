"""``python -m tradingbot.web -c config/config.yaml [--host 127.0.0.1] [--port 8080] [--no-open]``."""

from __future__ import annotations

import typer

from tradingbot.web.cli import web

app = typer.Typer(
    add_completion=False,
    rich_markup_mode=None,
    pretty_exceptions_show_locals=False,
    help="tradingbot 웹 대시보드 (모의투자 엔진 제어 / 백테스트 / 거래내역 / 로그 / 설정)",
)
app.command()(web)


def main() -> None:
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
