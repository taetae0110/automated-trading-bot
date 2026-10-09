"""FastAPI 앱 — JSON 계약 (ARCHITECTURE 와 웹 대시보드 스펙).

모든 응답은 JSON, 시각은 ISO-8601 UTC 문자열, 금액은 float, 오류는 ``{"error": "<한국어 메시지>"}`` + 4xx/5xx.
정적 파일(``tradingbot/web/static``)은 패키지 디렉터리에서 그대로 서빙한다 (빌드 단계 없음).

보안 (인증 없는 로컬 대시보드를 브라우저 공격면에서 지키는 최소 장치)
- Host 검사: 바인드 주소에 맞는 Host 헤더만 허용한다 (DNS 리바인딩 차단). 루프백 바인드면 localhost/127.0.0.1/[::1] 만.
- 상태 변경(POST) 요청은 ``X-Dashboard-Token`` (프로세스마다 새로 만드는 난수, index.html 의 meta 태그로 전달) 이 있어야
  하고, ``Sec-Fetch-Site`` 가 cross-site 이거나 ``Origin`` 호스트가 다르면 거부한다 (CSRF 차단). 본문이 있으면
  ``application/json`` 이어야 한다.
- 라우트는 동기 ``def`` 로 선언해 Starlette 스레드풀에서 돌린다 — 브로커 HTTP/파일 I/O 가 이벤트 루프를 막지 않는다.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import secrets
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from tradingbot import __version__
from tradingbot.config import AppConfig
from tradingbot.exceptions import TradingBotError
from tradingbot.notify import mask_secrets
from tradingbot.web.jobs import JobQueueFullError, JobRequestError
from tradingbot.web.service import DashboardService, WebError, error_status, is_loopback_host

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_BODY_BYTES",
    "STATIC_DIR",
    "TOKEN_HEADER",
    "TOKEN_PLACEHOLDER",
    "create_app",
    "host_allowed",
]

#: 프런트엔드 정적 파일 위치 (패키지 안, 빌드 없음)
STATIC_DIR = Path(__file__).resolve().parent / "static"
#: 상태 변경 요청에 필요한 토큰 헤더
TOKEN_HEADER = "X-Dashboard-Token"
#: index.html 에서 토큰으로 치환되는 자리표시자
TOKEN_PLACEHOLDER = "__DASHBOARD_TOKEN__"
#: POST 본문 최대 크기 (백테스트 요청)
MAX_BODY_BYTES = 64 * 1024

_NO_CACHE = {"Cache-Control": "no-store"}
_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})
_WILDCARD_BINDS = frozenset({"", "0.0.0.0", "::", "*"})


def _error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code, headers=_NO_CACHE)


def _json(payload: Any) -> JSONResponse:
    return JSONResponse(payload, headers=_NO_CACHE)


# ============================================================================ Host 검사
def _host_only(host_header: str) -> str:
    """``Host`` 헤더에서 포트를 뗀 호스트 (IPv6 는 대괄호 제거, 소문자)."""
    h = host_header.strip().lower()
    if h.startswith("["):
        end = h.find("]")
        return h[1:end] if end > 0 else h
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def host_allowed(host_header: str | None, bind_host: str | None, extra: Sequence[str] = ()) -> bool:
    """요청의 Host 헤더를 허용할지.

    - 루프백 이름(localhost / 127.0.0.1 / ::1 / 127.x) 과 ``extra`` 는 항상 허용.
    - 루프백에 바인드했으면 그 외는 전부 거부 (DNS 리바인딩: 공격자 도메인이 127.0.0.1 을 가리켜도 Host 가 다르다).
    - 비루프백 바인드(LAN 노출) 면 바인드 주소와 **IP 리터럴** Host 를 허용한다. 호스트명은 리바인딩에 쓰일 수 있어
      바인드 주소로 지정한 이름만 허용한다.
    """
    if not host_header:
        return False
    host = _host_only(host_header)
    if not host:
        return False
    if host in _LOOPBACK_NAMES or (_is_ip_literal(host) and ipaddress.ip_address(host).is_loopback):
        return True
    if host in {e.strip().lower() for e in extra if e}:
        return True
    bind = (bind_host or "").strip().lower()
    if bind.startswith("[") and bind.endswith("]"):
        bind = bind[1:-1]
    if not bind or is_loopback_host(bind):
        return False
    if host == bind:
        return True
    return _is_ip_literal(host)


class _HostGuard:
    """순수 ASGI 미들웨어: 허용되지 않는 Host 헤더는 400 + 한국어 JSON (TrustedHostMiddleware 는 영문 평문)."""

    def __init__(self, app: ASGIApp, *, bind_host: str | None, extra: Sequence[str]) -> None:
        self.app = app
        self.bind_host = bind_host
        self.extra = tuple(extra)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        host = Headers(scope=scope).get("host")
        if not host_allowed(host, self.bind_host, self.extra):
            response = _error(
                400,
                "허용되지 않는 Host 헤더입니다. 대시보드는 localhost/127.0.0.1 (또는 --host 로 지정한 주소/IP) 로만 접속할 수 있습니다",
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


# ============================================================================ 앱
def create_app(
    config: AppConfig,
    *,
    config_path: Path | None = None,
    data_dir: Path | None = None,
    bind_host: str | None = None,
    allowed_hosts: Sequence[str] = (),
    engine_log_file: bool = False,
) -> FastAPI:
    """대시보드 앱.

    - ``app.state.service`` 로 ``DashboardService`` 에 접근할 수 있다 (테스트/통합용).
    - ``app.state.dashboard_token`` 은 이 프로세스의 상태 변경 토큰 (POST 요청의 ``X-Dashboard-Token``).
    - ``bind_host`` 는 uvicorn 바인드 주소 (Host 검사에 사용, 기본 루프백만 허용). ``allowed_hosts`` 는 추가 허용 이름.
    - ``engine_log_file`` 이 True 면 이 프로세스가 엔진을 실제로 돌리는 동안만 ``logging.file`` 에 파일 로그를 붙인다.
    """
    service = DashboardService(
        config, config_path=config_path, data_dir=data_dir, engine_log_file=engine_log_file
    )
    token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await run_in_threadpool(service.close)

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
    app.state.dashboard_token = token
    app.add_middleware(_HostGuard, bind_host=bind_host, extra=tuple(allowed_hosts))

    # ------------------------------------------------------------------ 오류 → {"error": ...}
    @app.exception_handler(WebError)
    async def _web_error(_request: Request, exc: WebError) -> JSONResponse:
        return _error(exc.status_code, exc.message)

    @app.exception_handler(JobQueueFullError)
    async def _job_queue_full(_request: Request, exc: JobQueueFullError) -> JSONResponse:
        return _error(429, str(exc))

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
        # 내부 예외 문자열(파일 경로/라이브러리 내부)은 서버 로그에만 남기고 응답은 일반 메시지로
        logger.exception("처리되지 않은 오류: %s: %s", type(exc).__name__, mask_secrets(str(exc)))
        return _error(500, "서버 내부 오류가 발생했습니다. 자세한 내용은 서버 로그를 확인하세요")

    # ------------------------------------------------------------------ 상태 변경 요청 보호 (CSRF)
    def _mutation_guard(request: Request) -> None:
        site = (request.headers.get("sec-fetch-site") or "").strip().lower()
        if site and site not in ("same-origin", "none"):
            raise WebError(403, "다른 출처(cross-site)에서 보낸 요청은 거부합니다")
        origin = (request.headers.get("origin") or "").strip()
        if origin:
            origin_host = urlsplit(origin).netloc.lower() if origin.lower() != "null" else ""
            if not origin_host or origin_host != (request.headers.get("host") or "").strip().lower():
                raise WebError(403, "요청 Origin 이 대시보드 주소와 다릅니다 (cross-site 요청 거부)")
        supplied = request.headers.get(TOKEN_HEADER) or ""
        if not supplied or not hmac.compare_digest(supplied.encode(), token.encode()):
            raise WebError(
                403,
                f"대시보드 토큰({TOKEN_HEADER})이 없거나 유효하지 않습니다. 페이지를 새로고침한 뒤 다시 시도하세요",
            )
        has_body = request.headers.get("content-length", "0") not in ("", "0") or bool(
            request.headers.get("transfer-encoding")
        )
        if has_body and not (request.headers.get("content-type") or "").lower().startswith(
            "application/json"
        ):
            raise WebError(415, "요청 본문은 application/json 이어야 합니다")

    mutation = [Depends(_mutation_guard)]

    # ------------------------------------------------------------------ 정적 파일
    def _index_html() -> str | None:
        page = STATIC_DIR / "index.html"
        try:
            text = page.read_text(encoding="utf-8")
        except OSError:
            return None
        if TOKEN_PLACEHOLDER in text:
            return text.replace(TOKEN_PLACEHOLDER, token)
        # 자리표시자가 없는 index.html 이면 <head> 바로 뒤에 meta 태그를 넣는다
        meta = f'<meta name="dashboard-token" content="{token}">'
        head_at = text.lower().find("<head>")
        if head_at >= 0:
            insert_at = head_at + len("<head>")
            return text[:insert_at] + meta + text[insert_at:]
        return meta + text

    @app.get("/", include_in_schema=False)
    def index() -> Any:
        html = _index_html()
        if html is None:
            return _error(503, "대시보드 정적 파일(index.html)이 없습니다")
        return HTMLResponse(html, headers=_NO_CACHE)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR), check_dir=False), name="static")

    # ------------------------------------------------------------------ API (동기 def → 스레드풀)
    @app.get("/api/health")
    def health() -> Any:
        return _json(service.health())

    @app.get("/api/config")
    def get_config() -> Any:
        return _json(service.config_payload())

    @app.get("/api/strategies")
    def strategies() -> Any:
        return _json(service.strategies())

    @app.get("/api/status")
    def status() -> Any:
        return _json(service.status())

    @app.get("/api/positions")
    def positions() -> Any:
        return _json(service.positions())

    @app.get("/api/trades")
    def trades(limit: int = Query(100, ge=1, le=1000)) -> Any:
        return _json(service.trades(limit))

    @app.get("/api/prices")
    def prices() -> Any:
        return _json(service.prices())

    @app.get("/api/candles")
    def candles(
        symbol: str | None = Query(None, min_length=1, max_length=40),
        interval: str | None = Query(None, min_length=1, max_length=8),
        limit: int = Query(200, ge=1, le=1000),
    ) -> Any:
        sym = symbol if symbol is not None else config.symbols[0]
        itv = interval if interval is not None else config.interval
        return _json(service.candles(sym, itv, limit))

    @app.get("/api/equity")
    def equity(limit: int = Query(500, ge=1, le=5000)) -> Any:
        return _json(service.equity_history(limit))

    @app.get("/api/logs")
    def logs(lines: int = Query(200, ge=1, le=2000)) -> Any:
        return _json(service.logs(lines))

    @app.post("/api/engine/start", dependencies=mutation)
    def engine_start() -> Any:
        return _json(service.start_engine())

    @app.post("/api/engine/stop", dependencies=mutation)
    def engine_stop() -> Any:
        return _json(service.stop_engine())

    @app.post("/api/backtest", dependencies=mutation)
    async def backtest_submit(request: Request) -> Any:
        declared = request.headers.get("content-length") or ""
        if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            return _error(413, f"요청 본문이 너무 큽니다 (최대 {MAX_BODY_BYTES // 1024} KiB)")
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                return _error(413, f"요청 본문이 너무 큽니다 (최대 {MAX_BODY_BYTES // 1024} KiB)")
            chunks.append(chunk)
        raw = b"".join(chunks)
        body: Any = {}
        if raw.strip():
            try:
                body = json.loads(raw)
            except ValueError as e:
                return _error(400, f"요청 본문이 올바른 JSON 이 아닙니다: {e}")
        job = await run_in_threadpool(service.jobs.submit, body)
        return JSONResponse({"job_id": job.job_id, "status": job.status}, status_code=202, headers=_NO_CACHE)

    @app.get("/api/backtest")
    def backtest_list() -> Any:
        return _json({"jobs": service.jobs.list()})

    @app.get("/api/backtest/{job_id}")
    def backtest_get(job_id: str) -> Any:
        job = service.jobs.get(job_id)
        if job is None:
            return _error(404, f"백테스트 작업을 찾을 수 없습니다: {job_id}")
        return _json(job.to_dict())

    return app
