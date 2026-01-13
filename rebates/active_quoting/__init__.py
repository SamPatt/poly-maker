"""
Active Two-Sided Quoting Strategy for 15-minute crypto markets.

This module implements real-time two-sided market making with:
- Dynamic quote pricing at best bid/ask
- Quote refresh with hysteresis
- Momentum detection and automatic pullback
- Inventory management and skewing
- Risk management with circuit breakers
- WebSocket-based order/fill state tracking
- Fill analytics with markout tracking
- Telegram alerts (Phase 6)
- Database persistence (Phase 6)
"""

from .config import ActiveQuotingConfig
from .models import (
    Quote,
    OrderbookState,
    OrderbookLevel,
    MomentumState,
    Fill,
    OrderState,
    Position,
    MarketState,
    OrderSide,
    OrderStatus,
)
from .orderbook_manager import OrderbookManager
from .user_channel_manager import UserChannelManager
from .quote_engine import QuoteEngine, QuoteAction, QuoteDecision
from .order_manager import OrderManager, OrderResult, BatchOrderResult
from .inventory_manager import InventoryManager, InventoryLimits
from .momentum_detector import MomentumDetector, MomentumEvent
from .risk_manager import RiskManager, CircuitBreakerState, MarketRiskState, GlobalRiskState
from .fill_analytics import FillAnalytics, FillRecord, MarkoutSample, MarketStats, AggregateStats
from .bot import ActiveQuotingBot, run_bot
from .persistence import ActiveQuotingPersistence, PersistenceConfig, create_persistence
from .alerts import (
    TELEGRAM_ENABLED,
    send_active_quoting_startup_alert,
    send_active_quoting_shutdown_alert,
    send_active_quoting_fill_alert,
    send_active_quoting_circuit_breaker_alert,
    send_active_quoting_daily_summary,
    send_active_quoting_error_alert,
    send_active_quoting_market_halt_alert,
    FillAlertThrottler,
)

__all__ = [
    # Config
    "ActiveQuotingConfig",
    # Models
    "Quote",
    "OrderbookState",
    "OrderbookLevel",
    "MomentumState",
    "Fill",
    "OrderState",
    "Position",
    "MarketState",
    "OrderSide",
    "OrderStatus",
    # Phase 1 - WebSocket Managers
    "OrderbookManager",
    "UserChannelManager",
    # Phase 2 - Quote Engine & Order Manager
    "QuoteEngine",
    "QuoteAction",
    "QuoteDecision",
    "OrderManager",
    "OrderResult",
    "BatchOrderResult",
    # Phase 3 - Inventory & Momentum
    "InventoryManager",
    "InventoryLimits",
    "MomentumDetector",
    "MomentumEvent",
    # Phase 4 - Risk Management
    "RiskManager",
    "CircuitBreakerState",
    "MarketRiskState",
    "GlobalRiskState",
    # Phase 5 - Bot & Analytics
    "FillAnalytics",
    "FillRecord",
    "MarkoutSample",
    "MarketStats",
    "AggregateStats",
    "ActiveQuotingBot",
    "run_bot",
    # Phase 6 - Production Hardening
    "ActiveQuotingPersistence",
    "PersistenceConfig",
    "create_persistence",
    "TELEGRAM_ENABLED",
    "send_active_quoting_startup_alert",
    "send_active_quoting_shutdown_alert",
    "send_active_quoting_fill_alert",
    "send_active_quoting_circuit_breaker_alert",
    "send_active_quoting_daily_summary",
    "send_active_quoting_error_alert",
    "send_active_quoting_market_halt_alert",
    "FillAlertThrottler",
]
