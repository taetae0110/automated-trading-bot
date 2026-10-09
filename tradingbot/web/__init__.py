"""웹 대시보드 (FastAPI) — 모의투자 엔진 제어 / 상태 조회 / 백테스트 / 거래내역 / 로그.

구성
- ``app.py``     : ``create_app(config, *, config_path=None, data_dir=None) -> FastAPI`` (HTTP 라우트, JSON 계약)
- ``service.py`` : ``DashboardService`` — 엔진 스레드, 상태 파일 요약, 시세 캐시, 자산 이력, 로그 tail
- ``jobs.py``    : ``BacktestJobRunner`` — 백그라운드 백테스트 작업 (최근 20개 메모리 보관)
- ``cli.py``     : ``web`` typer 명령 (``tradingbot web`` 으로 등록), ``__main__`` : ``python -m tradingbot.web``
- ``static/``    : 프런트엔드 (index.html, app.js, style.css)

원칙
- 데모/샘플 시세는 없다. 모든 가격/캔들은 설정된 시세 브로커(Upbit 등) 또는 실제 상태 파일에서 온다.
- 비밀값(API 키/토큰/웹훅)은 응답과 로그에 나오지 않는다 (설정 응답은 AppConfig 만, 로그 tail 은 마스킹).
- 실거래(mode: live) 는 웹에서 시작할 수 없다 — 터미널 ``tradingbot run --live`` 만 허용.

이 패키지의 ``__init__`` 은 의도적으로 가볍다: ``tradingbot.cli`` 가 ``tradingbot.web.cli.web`` 을 등록할 때
순환 import 가 생기지 않도록 FastAPI 앱/서비스는 여기서 import 하지 않는다.
"""

from __future__ import annotations

__all__: list[str] = []
