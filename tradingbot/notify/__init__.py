"""알림 채널 (텔레그램 / 슬랙 / 디스코드).

사용:
    notifier = create_notifier(config.notify, Credentials.from_env())
    notifier.send("체결: KRW-BTC 매수 ...")   # 절대 예외를 던지지 않는다
"""

from __future__ import annotations

import logging

from tradingbot.config import Credentials, NotifyConfig
from tradingbot.exceptions import ConfigError
from tradingbot.notify.base import MultiNotifier, Notifier, NullNotifier, mask_secrets, split_text
from tradingbot.notify.discord import DiscordNotifier
from tradingbot.notify.slack import SlackNotifier
from tradingbot.notify.telegram import TelegramNotifier

logger = logging.getLogger(__name__)


def create_notifier(config: NotifyConfig, creds: Credentials) -> Notifier:
    """설정에서 enabled 된 채널 중 자격증명이 있는 것만 묶어 돌려준다.

    - 자격증명이 없는 채널은 경고 후 제외
    - 하나도 없으면 NullNotifier, 하나면 그 채널, 둘 이상이면 MultiNotifier
    """
    notifiers: list[Notifier] = []

    if config.telegram.enabled:
        if creds.telegram_bot_token and creds.telegram_chat_id:
            try:
                notifiers.append(TelegramNotifier(creds.telegram_bot_token, creds.telegram_chat_id))
            except ConfigError as e:
                logger.warning("텔레그램 알림 설정 오류로 제외합니다: %s", mask_secrets(str(e)))
        else:
            logger.warning(
                "텔레그램 알림이 켜져 있지만 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 환경변수가 없어 제외합니다"
            )

    if config.slack.enabled:
        if creds.slack_webhook_url:
            try:
                notifiers.append(SlackNotifier(creds.slack_webhook_url))
            except ConfigError as e:
                logger.warning("슬랙 알림 설정 오류로 제외합니다: %s", mask_secrets(str(e)))
        else:
            logger.warning("슬랙 알림이 켜져 있지만 SLACK_WEBHOOK_URL 환경변수가 없어 제외합니다")

    if config.discord.enabled:
        if creds.discord_webhook_url:
            try:
                notifiers.append(DiscordNotifier(creds.discord_webhook_url))
            except ConfigError as e:
                logger.warning("디스코드 알림 설정 오류로 제외합니다: %s", mask_secrets(str(e)))
        else:
            logger.warning("디스코드 알림이 켜져 있지만 DISCORD_WEBHOOK_URL 환경변수가 없어 제외합니다")

    if not notifiers:
        logger.info("활성화된 알림 채널이 없습니다 (알림 비활성)")
        return NullNotifier()
    logger.info("알림 채널: %s", ", ".join(n.name for n in notifiers))
    if len(notifiers) == 1:
        return notifiers[0]
    return MultiNotifier(notifiers)


__all__ = [
    "DiscordNotifier",
    "MultiNotifier",
    "Notifier",
    "NullNotifier",
    "SlackNotifier",
    "TelegramNotifier",
    "create_notifier",
    "mask_secrets",
    "split_text",
]
