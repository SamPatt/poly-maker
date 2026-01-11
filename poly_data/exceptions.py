"""
Custom exception hierarchy for Poly-Maker.

Exception categories:
1. Trading exceptions (order failures, balance issues)
2. API exceptions (Polymarket API, WebSocket errors)
3. Data exceptions (state inconsistencies, parsing errors)
4. External exceptions (blockchain, Telegram)

Each exception has:
- error_type: String identifier for logging/alerting
- should_alert: Whether to send Telegram alert for this error
- context: Optional dict with additional error context
"""

from typing import Dict, Any, Optional


class PolyMakerError(Exception):
    """Base exception for all Poly-Maker errors."""

    error_type: str = "unknown"
    should_alert: bool = False

    def __init__(self, message: str, context: Optional[Dict[str, Any]] = None):
        self.message = message
        self.context = context or {}
        super().__init__(message)

    def __str__(self) -> str:
        if self.context:
            ctx_str = ", ".join(f"{k}={v}" for k, v in list(self.context.items())[:3])
            return f"{self.message} ({ctx_str})"
        return self.message


# --- Trading Exceptions ---


class TradingError(PolyMakerError):
    """Base class for trading-related errors."""

    error_type = "trading"


class InsufficientBalanceError(TradingError):
    """Raised when wallet balance is insufficient for an order."""

    error_type = "insufficient_balance"
    should_alert = True

    def __init__(
        self,
        message: str,
        available: float = 0,
        required: float = 0,
        context: Optional[Dict[str, Any]] = None,
    ):
        ctx = context or {}
        ctx.update({"available": available, "required": required})
        super().__init__(message, ctx)


class OrderCreationError(TradingError):
    """Raised when order creation fails on the exchange."""

    error_type = "order_creation"
    should_alert = True


class OrderCancellationError(TradingError):
    """Raised when order cancellation fails."""

    error_type = "order_cancellation"
    should_alert = False  # Often transient


class StopLossTriggeredError(TradingError):
    """Raised when stop-loss is triggered (informational)."""

    error_type = "stop_loss"
    should_alert = True

    def __init__(
        self,
        message: str,
        market: str = "",
        pnl: float = 0,
        context: Optional[Dict[str, Any]] = None,
    ):
        ctx = context or {}
        ctx.update({"market": market, "pnl": pnl})
        super().__init__(message, ctx)


class PositionMergeError(TradingError):
    """Raised when position merging fails."""

    error_type = "position_merge"
    should_alert = True


class PositionRedemptionError(TradingError):
    """Raised when position redemption fails."""

    error_type = "redemption"
    should_alert = True


# --- API Exceptions ---


class APIError(PolyMakerError):
    """Base class for API-related errors."""

    error_type = "api"


class PolymarketAPIError(APIError):
    """Raised for Polymarket CLOB API errors."""

    error_type = "polymarket_api"
    should_alert = True

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        endpoint: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ):
        ctx = context or {}
        if status_code:
            ctx["status_code"] = status_code
        if endpoint:
            ctx["endpoint"] = endpoint
        super().__init__(message, ctx)


class WebSocketError(APIError):
    """Raised for WebSocket connection errors."""

    error_type = "websocket"
    should_alert = True

    def __init__(
        self,
        message: str,
        connection_type: str = "",
        reconnect_attempts: int = 0,
        context: Optional[Dict[str, Any]] = None,
    ):
        ctx = context or {}
        ctx.update(
            {"connection_type": connection_type, "reconnect_attempts": reconnect_attempts}
        )
        super().__init__(message, ctx)


class RateLimitError(APIError):
    """Raised when rate limited by API."""

    error_type = "rate_limit"
    should_alert = False  # Handle with retry

    def __init__(
        self,
        message: str,
        retry_after: Optional[int] = None,
        context: Optional[Dict[str, Any]] = None,
    ):
        ctx = context or {}
        if retry_after:
            ctx["retry_after"] = retry_after
        super().__init__(message, ctx)


# --- Data Exceptions ---


class DataError(PolyMakerError):
    """Base class for data-related errors."""

    error_type = "data"


class StateInconsistencyError(DataError):
    """Raised when global state is inconsistent."""

    error_type = "state_inconsistency"
    should_alert = True


class MarketDataError(DataError):
    """Raised when market data is missing or invalid."""

    error_type = "market_data"
    should_alert = False


class ConfigurationError(DataError):
    """Raised when configuration is invalid or missing."""

    error_type = "configuration"
    should_alert = True


# --- External Exceptions ---


class ExternalError(PolyMakerError):
    """Base class for external service errors."""

    error_type = "external"


class BlockchainError(ExternalError):
    """Raised for blockchain interaction errors."""

    error_type = "blockchain"
    should_alert = True

    def __init__(
        self,
        message: str,
        tx_hash: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ):
        ctx = context or {}
        if tx_hash:
            ctx["tx_hash"] = tx_hash
        super().__init__(message, ctx)


class TelegramError(ExternalError):
    """Raised when Telegram alert fails."""

    error_type = "telegram"
    should_alert = False  # Don't alert about alert failures


class DatabaseError(ExternalError):
    """Raised for database operation errors."""

    error_type = "database"
    should_alert = True
