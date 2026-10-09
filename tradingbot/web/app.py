"""FastAPI 앱 — JSON 계약 (ARCHITECTURE 와 웹 대시보드 스펙).

모든 응답은 JSON, 시각은 ISO-8601 UTC 문자열, 금액은 float, 오류는 ``{"error": "<한국어 메시지>"}`` + 4xx/5xx.
정적 파일(``tradingbot/web/static``)은 패키지 디렉터리에서 그대로 서빙한다 (빌드 단계 없음).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from tradingbot import __version__
from tradingbot.config import AppConfig
from tradingbot.exceptions import TradingBotError
from tradingbot.notify import mask_secrets
from tradingbot.web.jobs import JobRequestError
from tradingbot.web.service import DashboardService, WebError, error_status

logger = logging.getLogger(__name__)

__all__ = ["STATIC_DIR", "create_app"]

#: 프런트엔드 정적 파일 위치 (패키지 안, 빌드 없음)
STATIC_DIR = Path(__file__).resolve().parent / "static"

_NO_CACHE = {"Cache-Control": "no-store"}


def _error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code, headers=_NO_CACHE)


def _json(payload: Any) -> JSONResponse:
    return JSONResponse(payload, headers=_NO_CACHE)


def create_app(
    config: AppConfig,
    *,
    config_path: Path | None = None,
    data_dir: Path | None = None,
) -> FastAPI:
    """대시보드 앱. ``app.state.service`` 로 ``DashboardService`` 에 접근할 수 있다 (테스트/통합용)."""
    service = DashboardService(config, config_path=config_path, data_dir=data_dir)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            service.close()

    app = FastAPI(
        title="tradingbot 대시보드",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.service = service
    app.state.config = config

    # ------------------------------------------------------------------ 오류 → {"error": ...}
    @app.exception_handler(WebError)
    async def _web_error(_request: Request, exc: WebError) -> JSONResponse:
        return _error(exc.status_code, exc.message)

    @app.exception_handler(JobRequestError)
    async def _job_request_error(_request: Request, exc: JobRequestError) -> JSONResponse:
        return _error(400, str(exc))

    @app.exception_handler(TradingBotError)
    async def _bot_error(_request: Request, exc: TradingBotError) -> JSONResponse:
        status = error_status(exc)
        if status >= 500:
            logger.error("요청 처리 오류: %s", mask_secrets(str(exc)))
        return _error(status, mask_secrets(str(exc)) or type(exc).__name__)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        parts: list[str] = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", ()) if p not in ("query", "body", "path"))
            parts.append(f"{loc}: {err.get('msg', '')}".strip(": "))
        return _error(400, "잘못된 요청: " + ("; ".join(parts) if parts else "입력값을 확인하세요"))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) and exc.detail else None
        if detail in (None, "Not Found"):
            detail = "요청한 경로가 없습니다" if exc.status_code == 404 else f"HTTP 오류 {exc.status_code}"
        elif detail == "Method Not Allowed":
            detail = "허용되지 않는 메서드입니다"
        return _error(exc.status_code, detail)

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("처리되지 않은 오류: %s", mask_secrets(str(exc)))
        return _error(500, f"서버 내부 오류: {type(exc).__name__}: {mask_secrets(str(exc))}")

    # ------------------------------------------------------------------ 정적 파일
    @app.get("/", include_in_schema=False)
    async def index() -> Any:
        page = STATIC_DIR / "index.html"
        if not page.is_file():
            return _error(503, f"대시보드 정적 파일이 없습니다: {page}")
        return FileResponse(page, media_type="text/html; charset=utf-8", headers=_NO_CACHE)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR), check_dir=False), name="static")

    # ------------------------------------------------------------------ API
    @app.get("/api/health")
    async def health() -> Any:
        return _json(service.health())

    @app.get("/api/config")
    async def get_config() -> Any:
        return _json(service.config_payload())

    @app.get("/api/strategies")
    async def strategies() -> Any:
        return _json(service.strategies())

    @app.get("/api/status")
    async def status() -> Any:
        return _json(service.status())

    @app.get("/api/positions")
    async def positions() -> Any:
        return _json(service.positions())

    @app.get("/api/trades")
    async def trades(limit: int = Query(100, ge=1, le=1000)) -> Any:
        return _json(service.trades(limit))

    @app.get("/api/prices")
    async def prices() -> Any:
        return _json(service.prices())

    @app.get("/api/candles")
    async def candles(
        symbol: str | None = Query(None, min_length=1, max_length=40),
        interval: str | None = Query(None, min_length=1, max_length=8),
        limit: int = Query(200, ge=1, le=1000),
    ) -> Any:
        sym = symbol if symbol is not None else config.symbols[0]
        itv = interval if interval is not None else config.interval
        return _json(service.candles(sym, itv, limit))

    @app.get("/api/equity")
    async def equity(limit: int = Query(500, ge=1, le=5000)) -> Any:
        return _json(service.equity_history(limit))

    @app.get("/api/logs")
    async def logs(lines: int = Query(200, ge=1, le=2000)) -> Any:
        return _json(service.logs(lines))

    @app.post("/api/engine/start")
    async def engine_start() -> Any:
        return _json(service.start_engine())

    @app.post("/api/engine/stop")
    async def engine_stop() -> Any:
        return _json(service.stop_engine())

    @app.post("/api/backtest")
    async def backtest_submit(request: Request) -> Any:
        raw = await request.body()
        body: Any = {}
        if raw.strip():
            try:
                import json

                body = json.loads(raw)
            except ValueError as e:
                return _error(400, f"요청 본문이 올바른 JSON 이 아닙니다: {e}")
        job = service.jobs.submit(body)
        return JSONResponse({"job_id": job.job_id, "status": job.status}, status_code=202, headers=_NO_CACHE)

    @app.get("/api/backtest")
    async def backtest_list() -> Any:
        return _json({"jobs": service.jobs.list()})

    @app.get("/api/backtest/{job_id}")
    async def backtest_get(job_id: str) -> Any:
        job = service.jobs.get(job_id)
        if job is None:
            return _error(404, f"백테스트 작업을 찾을 수 없습니다: {job_id}")
        return _json(job.to_dict())

    return app
