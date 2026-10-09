"""알림(Notifier) 테스트. 네트워크는 전부 `responses` 로 모킹하며, 모킹 응답은 각 서비스 공식 문서의 스키마를 따른다.

- Telegram Bot API sendMessage: https://core.telegram.org/bots/api#sendmessage
- Slack Incoming Webhooks: https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/
- Discord Execute Webhook: https://discord.com/developers/docs/resources/webhook#execute-webhook
토큰/웹훅 URL 은 테스트용 더미 값이며, 로그에 절대 나타나지 않아야 한다.
"""

from __future__ import annotations

import json
import logging
import time

import pytest
import responses

from tradingbot.config import Credentials, DiscordConfig, NotifyConfig, SlackConfig, TelegramConfig
from tradingbot.exceptions import BrokerError, ConfigError
from tradingbot.notify import (
    DiscordNotifier,
    MultiNotifier,
    Notifier,
    NullNotifier,
    SlackNotifier,
    TelegramNotifier,
    create_notifier,
)
from tradingbot.notify.base import MASK, mask_secrets, split_text
from tradingbot.notify.discord import DISCORD_MAX_LEN
from tradingbot.notify.telegram import TELEGRAM_MAX_LEN
from tradingbot.utils.http import HttpClient

TOKEN = "7000000001:AAtest-token-SHOULD-NEVER-BE-LOGGED"
CHAT_ID = "-1001234567890"
TG_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
SLACK_URL = "https://hooks.slack.com/services/T00000000/B00000000/XXXXSECRETXXXX"
DISCORD_URL = "https://discord.com/api/webhooks/123456789012345678/DISCORD-SECRET-TOKEN"

SECRETS = (TOKEN, "XXXXSECRETXXXX", "DISCORD-SECRET-TOKEN")


# ----------------------------------------------------------------------------- 공식 응답 스키마
def tg_ok(text: str) -> dict:
    """Telegram sendMessage 성공 응답 (Message 객체)."""
    return {
        "ok": True,
        "result": {
            "message_id": 42,
            "from": {"id": 7000000001, "is_bot": True, "first_name": "bot", "username": "test_bot"},
            "chat": {"id": int(CHAT_ID), "title": "alerts", "type": "supergroup"},
            "date": 1_760_000_000,
            "text": text,
        },
    }


def tg_error(code: int, description: str) -> dict:
    return {"ok": False, "error_code": code, "description": description}


def discord_rate_limited() -> dict:
    return {"message": "You are being rate limited.", "retry_after": 0.01, "global": False}


# ----------------------------------------------------------------------------- 픽스처
@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)


@pytest.fixture
def mocked():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield rsps


@pytest.fixture
def logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.DEBUG)
    return caplog


def assert_no_secret_logged(caplog: pytest.LogCaptureFixture) -> None:
    for rec in caplog.records:
        rendered = rec.getMessage()
        for s in SECRETS:
            assert s not in rendered, f"비밀값이 로그에 노출: {rec.name}: {rendered}"
            assert s not in str(rec.msg)
            assert s not in str(rec.args)


def body(call: responses.Call) -> dict:
    return json.loads(call.request.body)


# ----------------------------------------------------------------------------- split_text
class TestSplitText:
    def test_short_text_single_chunk(self) -> None:
        assert split_text("hello", 10) == ["hello"]
        assert split_text("exactly10!", 10) == ["exactly10!"]

    def test_prefers_newline_boundary(self) -> None:
        text = "line one\nline two\nline three"
        chunks = split_text(text, 18)
        assert chunks == ["line one\nline two", "line three"]
        assert all(len(c) <= 18 for c in chunks)

    def test_hard_cut_without_newline(self) -> None:
        assert split_text("x" * 25, 10) == ["x" * 10, "x" * 10, "x" * 5]

    def test_drops_blank_chunks(self) -> None:
        assert split_text("\n\n\n", 2) == []
        assert split_text("a\n\n\n\n\nb", 2) == ["a", "b"]

    def test_invalid_limit(self) -> None:
        with pytest.raises(ValueError):
            split_text("x", 0)

    def test_reassembles_without_loss_of_content(self) -> None:
        text = "\n".join(f"행 {i} 체결 알림 KRW-BTC" for i in range(200))
        chunks = split_text(text, 100)
        assert all(0 < len(c) <= 100 for c in chunks)
        assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


# ----------------------------------------------------------------------------- Notifier 베이스
class _Boom(Notifier):
    name = "boom"

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls: list[str] = []

    def _send(self, text: str) -> None:
        self.calls.append(text)
        raise self.exc


class _Recorder(Notifier):
    name = "rec"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def _send(self, text: str) -> None:
        self.calls.append(text)


class TestNotifierBase:
    def test_send_swallows_broker_error(self, logs) -> None:
        n = _Boom(BrokerError("전송 실패"))
        n.send("hi")  # 예외 없음
        assert n.calls == ["hi"]
        assert any("알림 전송 실패" in r.getMessage() and r.levelno == logging.WARNING for r in logs.records)

    def test_send_swallows_any_exception(self, logs) -> None:
        n = _Boom(RuntimeError("뜻밖의 오류"))
        n.send("hi")
        assert any("예기치 못한 오류" in r.getMessage() and r.levelno == logging.ERROR for r in logs.records)

    @pytest.mark.parametrize("empty", ["", "   ", "\n\n", None, 123])
    def test_empty_text_not_sent(self, empty) -> None:
        n = _Recorder()
        n.send(empty)  # type: ignore[arg-type]
        assert n.calls == []

    def test_null_notifier(self) -> None:
        NullNotifier().send("아무것도 안 함")

    def test_multi_notifier_calls_all_even_if_one_fails(self, logs) -> None:
        a, b, c = _Recorder(), _Boom(BrokerError("x")), _Recorder()
        m = MultiNotifier([a, b, c])
        assert len(m) == 3
        m.send("메시지")
        assert a.calls == ["메시지"] and b.calls == ["메시지"] and c.calls == ["메시지"]
        assert "rec" in repr(m) and "boom" in repr(m)

    def test_multi_notifier_empty_and_type_check(self) -> None:
        MultiNotifier([]).send("x")
        with pytest.raises(TypeError):
            MultiNotifier([object()])  # type: ignore[list-item]

    def test_mask_secrets_masks_registered(self) -> None:
        TelegramNotifier(TOKEN, CHAT_ID)  # 등록
        assert (
            mask_secrets(f"url=https://api.telegram.org/bot{TOKEN}/x")
            == f"url=https://api.telegram.org/bot{MASK}/x"
        )


# ----------------------------------------------------------------------------- Telegram
class TestTelegram:
    def test_constructor_validation(self) -> None:
        with pytest.raises(ConfigError):
            TelegramNotifier("", CHAT_ID)
        with pytest.raises(ConfigError):
            TelegramNotifier(TOKEN, "")
        with pytest.raises(ConfigError):
            TelegramNotifier(TOKEN, None)  # type: ignore[arg-type]
        n = TelegramNotifier(TOKEN, 12345)
        assert n.chat_id == "12345"
        assert TOKEN not in repr(n)
        assert MASK in repr(n)

    def test_send_payload_and_html_escape(self, mocked, logs) -> None:
        text = "체결 <KRW-BTC> 수량 & 가격 > 0"
        mocked.add(responses.POST, TG_URL, json=tg_ok(text), status=200)
        n = TelegramNotifier(TOKEN, CHAT_ID)
        n.send(text)
        assert len(mocked.calls) == 1
        call = mocked.calls[0]
        assert call.request.url == TG_URL  # URL 에는 토큰이 들어간다
        assert call.request.headers["Content-Type"] == "application/json"
        payload = body(call)
        assert payload == {
            "chat_id": CHAT_ID,
            "text": "체결 &lt;KRW-BTC&gt; 수량 &amp; 가격 &gt; 0",
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "link_preview_options": {"is_disabled": True},
        }
        assert_no_secret_logged(logs)

    def test_long_text_is_chunked(self, mocked) -> None:
        mocked.add(responses.POST, TG_URL, json=tg_ok("..."), status=200)
        n = TelegramNotifier(TOKEN, CHAT_ID)
        text = "\n".join(f"{i:05d} 거래 요약 라인" for i in range(600))  # ≈ 10,000자
        assert len(text) > 2 * TELEGRAM_MAX_LEN
        n.send(text)
        assert len(mocked.calls) == 3
        for call in mocked.calls:
            assert 0 < len(body(call)["text"]) <= TELEGRAM_MAX_LEN
        joined = "".join(body(c)["text"] for c in mocked.calls)
        assert joined.replace("\n", "") == text.replace("\n", "")

    def test_http_error_is_swallowed_and_token_masked(self, mocked, logs) -> None:
        mocked.add(responses.POST, TG_URL, json=tg_error(401, "Unauthorized"), status=401)
        n = TelegramNotifier(TOKEN, CHAT_ID)
        n.send("hello")  # 예외 없음
        warnings = [r for r in logs.records if r.levelno >= logging.WARNING]
        assert warnings, "실패가 로그에 남아야 한다"
        assert any("401" in r.getMessage() for r in warnings)
        assert any(MASK in r.getMessage() for r in warnings)
        assert_no_secret_logged(logs)

    def test_api_level_error_ok_false(self, mocked, logs) -> None:
        mocked.add(responses.POST, TG_URL, json=tg_error(400, "Bad Request: chat not found"), status=200)
        TelegramNotifier(TOKEN, CHAT_ID).send("hello")
        assert any("chat not found" in r.getMessage() for r in logs.records if r.levelno == logging.WARNING)
        assert_no_secret_logged(logs)

    def test_retries_on_5xx_then_succeeds_without_leaking_token(self, mocked, logs) -> None:
        mocked.add(
            responses.POST,
            TG_URL,
            json={"ok": False, "error_code": 502, "description": "Bad Gateway"},
            status=502,
        )
        mocked.add(responses.POST, TG_URL, json=tg_ok("hello"), status=200)
        TelegramNotifier(TOKEN, CHAT_ID).send("hello")
        assert len(mocked.calls) == 2
        retry_logs = [
            r for r in logs.records if r.name == "tradingbot.utils.http" and "재시도" in r.getMessage()
        ]
        assert retry_logs, "HttpClient 의 재시도 경고가 찍혀야 한다"
        assert all(MASK in r.getMessage() for r in retry_logs)
        assert_no_secret_logged(logs)

    def test_retries_on_429_with_retry_after(self, mocked) -> None:
        mocked.add(
            responses.POST,
            TG_URL,
            json={
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests: retry after 1",
                "parameters": {"retry_after": 1},
            },
            status=429,
            headers={"Retry-After": "1"},
        )
        mocked.add(responses.POST, TG_URL, json=tg_ok("x"), status=200)
        TelegramNotifier(TOKEN, CHAT_ID).send("x")
        assert len(mocked.calls) == 2

    def test_network_error_is_swallowed(self, mocked, logs) -> None:
        mocked.add(responses.POST, TG_URL, body=responses.ConnectionError("connection reset"))
        TelegramNotifier(TOKEN, CHAT_ID, max_retries=1).send("hello")
        assert len(mocked.calls) == 2  # 1회 재시도
        assert any("네트워크 오류" in r.getMessage() for r in logs.records if r.levelno == logging.WARNING)
        assert_no_secret_logged(logs)

    def test_exhausted_retries_logged_once_masked(self, mocked, logs) -> None:
        for _ in range(3):
            mocked.add(responses.POST, TG_URL, json=tg_error(503, "Service Unavailable"), status=503)
        TelegramNotifier(TOKEN, CHAT_ID, max_retries=2).send("hello")
        assert len(mocked.calls) == 3
        assert_no_secret_logged(logs)

    def test_custom_client_is_used(self, mocked) -> None:
        mocked.add(responses.POST, TG_URL, json=tg_ok("x"), status=200)
        client = HttpClient(timeout=1.0, max_retries=0)
        n = TelegramNotifier(TOKEN, CHAT_ID, client=client)
        assert n._client is client
        n.send("x")
        assert len(mocked.calls) == 1

    def test_empty_after_escape_still_sent(self, mocked) -> None:
        mocked.add(responses.POST, TG_URL, json=tg_ok("&"), status=200)
        TelegramNotifier(TOKEN, CHAT_ID).send("&")
        assert body(mocked.calls[0])["text"] == "&amp;"


# ----------------------------------------------------------------------------- Slack
class TestSlack:
    def test_constructor_validation(self) -> None:
        with pytest.raises(ConfigError):
            SlackNotifier("")
        with pytest.raises(ConfigError):
            SlackNotifier("http://hooks.slack.com/services/x")  # https 필수
        n = SlackNotifier(SLACK_URL)
        assert "XXXXSECRETXXXX" not in repr(n)

    def test_send_payload(self, mocked, logs) -> None:
        mocked.add(responses.POST, SLACK_URL, body="ok", status=200, content_type="text/plain")
        SlackNotifier(SLACK_URL).send("슬랙 <알림> & 테스트")
        assert len(mocked.calls) == 1
        assert body(mocked.calls[0]) == {"text": "슬랙 <알림> & 테스트"}  # 슬랙은 escape 하지 않는다
        assert_no_secret_logged(logs)

    def test_error_masks_webhook_url(self, mocked, logs) -> None:
        mocked.add(responses.POST, SLACK_URL, body="invalid_payload", status=400, content_type="text/plain")
        SlackNotifier(SLACK_URL).send("x")
        warn = [
            r for r in logs.records if r.levelno == logging.WARNING and "알림 전송 실패" in r.getMessage()
        ]
        assert len(warn) == 1
        assert "invalid_payload" in warn[0].getMessage()
        assert MASK in warn[0].getMessage()
        assert_no_secret_logged(logs)

    def test_retry_on_500(self, mocked, logs) -> None:
        mocked.add(responses.POST, SLACK_URL, body="internal error", status=500)
        mocked.add(responses.POST, SLACK_URL, body="ok", status=200)
        SlackNotifier(SLACK_URL).send("x")
        assert len(mocked.calls) == 2
        assert_no_secret_logged(logs)

    def test_no_chunking_for_slack(self, mocked) -> None:
        mocked.add(responses.POST, SLACK_URL, body="ok", status=200)
        SlackNotifier(SLACK_URL).send("x" * 5000)
        assert len(mocked.calls) == 1


# ----------------------------------------------------------------------------- Discord
class TestDiscord:
    def test_constructor_validation(self) -> None:
        with pytest.raises(ConfigError):
            DiscordNotifier("   ")
        with pytest.raises(ConfigError):
            DiscordNotifier("ftp://discord.com/api/webhooks/1/x")
        assert "DISCORD-SECRET-TOKEN" not in repr(DiscordNotifier(DISCORD_URL))

    def test_send_payload_204(self, mocked, logs) -> None:
        mocked.add(responses.POST, DISCORD_URL, status=204)  # wait 없이 보내면 204 No Content
        DiscordNotifier(DISCORD_URL).send("디스코드 알림")
        assert len(mocked.calls) == 1
        assert body(mocked.calls[0]) == {"content": "디스코드 알림"}
        assert_no_secret_logged(logs)

    def test_chunking_2000(self, mocked) -> None:
        mocked.add(responses.POST, DISCORD_URL, status=204)
        text = "\n".join(f"{i:04d} 체결 KRW-ETH 매도" for i in range(300))  # ≈ 5,400자
        assert len(text) > 2 * DISCORD_MAX_LEN
        DiscordNotifier(DISCORD_URL).send(text)
        assert len(mocked.calls) == 3
        for call in mocked.calls:
            assert 0 < len(body(call)["content"]) <= DISCORD_MAX_LEN
        joined = "".join(body(c)["content"] for c in mocked.calls)
        assert joined.replace("\n", "") == text.replace("\n", "")

    def test_exactly_limit_is_one_message(self, mocked) -> None:
        mocked.add(responses.POST, DISCORD_URL, status=204)
        DiscordNotifier(DISCORD_URL).send("a" * DISCORD_MAX_LEN)
        assert len(mocked.calls) == 1
        mocked.reset()
        mocked.add(responses.POST, DISCORD_URL, status=204)
        DiscordNotifier(DISCORD_URL).send("a" * (DISCORD_MAX_LEN + 1))
        assert len(mocked.calls) == 2

    def test_rate_limit_retry(self, mocked, logs) -> None:
        mocked.add(
            responses.POST, DISCORD_URL, json=discord_rate_limited(), status=429, headers={"Retry-After": "0"}
        )
        mocked.add(responses.POST, DISCORD_URL, status=204)
        DiscordNotifier(DISCORD_URL).send("x")
        assert len(mocked.calls) == 2
        assert_no_secret_logged(logs)

    def test_error_masks_webhook_url(self, mocked, logs) -> None:
        mocked.add(
            responses.POST, DISCORD_URL, json={"message": "Unknown Webhook", "code": 10015}, status=404
        )
        DiscordNotifier(DISCORD_URL).send("x")
        warn = [r for r in logs.records if r.levelno == logging.WARNING]
        assert any("Unknown Webhook" in r.getMessage() and MASK in r.getMessage() for r in warn)
        assert_no_secret_logged(logs)

    def test_failure_in_middle_chunk_stops_and_logs_once(self, mocked, logs) -> None:
        mocked.add(responses.POST, DISCORD_URL, status=204)
        mocked.add(
            responses.POST, DISCORD_URL, json={"message": "Unknown Webhook", "code": 10015}, status=404
        )
        DiscordNotifier(DISCORD_URL).send("b" * (DISCORD_MAX_LEN * 3))
        assert len(mocked.calls) == 2  # 두 번째 조각에서 실패 → 세 번째는 보내지 않음
        assert sum(1 for r in logs.records if "알림 전송 실패" in r.getMessage()) == 1


# ----------------------------------------------------------------------------- create_notifier
def cfg(tg: bool = False, slack: bool = False, discord: bool = False) -> NotifyConfig:
    return NotifyConfig(
        telegram=TelegramConfig(enabled=tg),
        slack=SlackConfig(enabled=slack),
        discord=DiscordConfig(enabled=discord),
    )


class TestCreateNotifier:
    def test_nothing_enabled(self) -> None:
        n = create_notifier(cfg(), Credentials(telegram_bot_token=TOKEN, telegram_chat_id=CHAT_ID))
        assert isinstance(n, NullNotifier)

    def test_enabled_without_credentials_warns_and_skips(self, logs) -> None:
        n = create_notifier(cfg(tg=True, slack=True, discord=True), Credentials())
        assert isinstance(n, NullNotifier)
        msgs = [r.getMessage() for r in logs.records if r.levelno == logging.WARNING]
        assert any("TELEGRAM_BOT_TOKEN" in m for m in msgs)
        assert any("SLACK_WEBHOOK_URL" in m for m in msgs)
        assert any("DISCORD_WEBHOOK_URL" in m for m in msgs)

    def test_telegram_needs_both_token_and_chat_id(self, logs) -> None:
        n = create_notifier(cfg(tg=True), Credentials(telegram_bot_token=TOKEN))
        assert isinstance(n, NullNotifier)
        n = create_notifier(cfg(tg=True), Credentials(telegram_chat_id=CHAT_ID))
        assert isinstance(n, NullNotifier)
        assert_no_secret_logged(logs)

    def test_single_channel_returns_that_notifier(self) -> None:
        n = create_notifier(cfg(tg=True), Credentials(telegram_bot_token=TOKEN, telegram_chat_id=CHAT_ID))
        assert isinstance(n, TelegramNotifier)
        n = create_notifier(cfg(slack=True), Credentials(slack_webhook_url=SLACK_URL))
        assert isinstance(n, SlackNotifier)
        n = create_notifier(cfg(discord=True), Credentials(discord_webhook_url=DISCORD_URL))
        assert isinstance(n, DiscordNotifier)

    def test_multiple_channels(self, logs) -> None:
        creds = Credentials(
            telegram_bot_token=TOKEN,
            telegram_chat_id=CHAT_ID,
            slack_webhook_url=SLACK_URL,
            discord_webhook_url=DISCORD_URL,
        )
        n = create_notifier(cfg(tg=True, slack=True, discord=True), creds)
        assert isinstance(n, MultiNotifier)
        assert [x.name for x in n.notifiers] == ["telegram", "slack", "discord"]
        assert_no_secret_logged(logs)

    def test_disabled_channels_ignored_even_with_credentials(self) -> None:
        creds = Credentials(telegram_bot_token=TOKEN, telegram_chat_id=CHAT_ID, slack_webhook_url=SLACK_URL)
        n = create_notifier(cfg(slack=True), creds)
        assert isinstance(n, SlackNotifier)

    def test_invalid_credential_value_is_skipped(self, logs) -> None:
        n = create_notifier(cfg(slack=True), Credentials(slack_webhook_url="http://insecure.example"))
        assert isinstance(n, NullNotifier)
        assert any("설정 오류" in r.getMessage() for r in logs.records if r.levelno == logging.WARNING)

    def test_end_to_end_multi_send(self, mocked, logs) -> None:
        mocked.add(responses.POST, TG_URL, json=tg_ok("x"), status=200)
        mocked.add(responses.POST, SLACK_URL, body="ok", status=200)
        mocked.add(
            responses.POST, DISCORD_URL, json={"message": "Unknown Webhook", "code": 10015}, status=404
        )
        creds = Credentials(
            telegram_bot_token=TOKEN,
            telegram_chat_id=CHAT_ID,
            slack_webhook_url=SLACK_URL,
            discord_webhook_url=DISCORD_URL,
        )
        n = create_notifier(cfg(tg=True, slack=True, discord=True), creds)
        n.send("체결: KRW-BTC 매수")  # 디스코드 실패해도 예외 없음
        urls = [c.request.url for c in mocked.calls]
        assert urls == [TG_URL, SLACK_URL, DISCORD_URL]
        assert_no_secret_logged(logs)
