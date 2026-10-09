"""공통 예외."""


class TradingBotError(Exception):
    """모든 봇 예외의 베이스."""


class ConfigError(TradingBotError):
    pass


class BrokerError(TradingBotError):
    """브로커/거래소 API 호출 실패."""

    def __init__(self, message: str, *, status_code: int | None = None, payload: object = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class AuthenticationError(BrokerError):
    pass


class RateLimitError(BrokerError):
    pass


class InsufficientFunds(BrokerError):
    pass


class OrderError(BrokerError):
    pass


class MarketClosed(BrokerError):
    pass


class DataError(TradingBotError):
    pass
