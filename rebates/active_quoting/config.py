"""
Configuration for Active Two-Sided Quoting Strategy.

All parameters from the research document with environment variable overrides.
"""
import os
from dataclasses import dataclass, field
from typing import List


@dataclass
class ActiveQuotingConfig:
    """Configuration for the active quoting bot."""

    # --- Quote Pricing ---
    quote_offset_ticks: int = 0  # Quote AT best bid/ask, not inside
    improve_when_spread_ticks: int = 4  # Only improve by 1 tick if spread >= 4 ticks
    max_spread_ticks: int = 10  # Widen to this during momentum

    # --- Quote Refresh (Hysteresis) ---
    refresh_threshold_ticks: int = 2  # Only refresh if quote is >= 2 ticks from target
    min_refresh_interval_ms: int = 500  # Per market, prevents churn
    global_refresh_cap_per_sec: int = 10  # Across all markets

    # --- Momentum Detection ---
    momentum_threshold_ticks: int = 3  # Trigger if price moves 3+ ticks
    momentum_window_ms: int = 500  # Within 500ms
    cooldown_seconds: float = 2.0  # Pause after momentum
    sweep_depth_threshold: float = 0.5  # 50% depth removed = sweep

    # --- Inventory Management ---
    max_position_per_market: int = 100  # Shares
    max_liability_per_market_usdc: float = 50.0  # Worst-case loss
    max_total_liability_usdc: float = 500.0  # Across all markets
    inventory_skew_coefficient: float = 0.1  # Linear: skew = coef * inventory

    # --- Risk Management ---
    max_drawdown_per_market_usdc: float = 20.0  # Stop quoting that market
    max_drawdown_global_usdc: float = 100.0  # Kill switch
    max_consecutive_errors: int = 5
    stale_feed_timeout_seconds: float = 30.0  # Max time without WS events
    circuit_breaker_recovery_seconds: float = 60.0  # Recovery period after halt

    # --- End-of-Market Wind-Down ---
    wind_down_start_seconds: float = 300.0  # Start wind-down 5 minutes (300s) before end
    wind_down_taker_threshold_seconds: float = 40.0  # Switch to taker mode at 40 seconds
    wind_down_taker_price_threshold: float = 0.25  # Only taker sell if price < $0.25

    # --- WebSocket Gap Safety (Phase 6) ---
    halt_on_ws_gaps: bool = True  # Halt quoting when WS gaps cannot be reconciled
    ws_gap_reconcile_attempts: int = 3  # Max reconciliation attempts before halting
    ws_gap_recovery_interval_seconds: float = 30.0  # Interval for recovery attempts when halted

    # --- Order Management ---
    order_size_usdc: float = 10.0  # Shares per side (misleading name - actually shares, not USDC). Polymarket minimum is 5 shares.
    batch_size: int = 15  # Max orders per batch request
    cancel_on_momentum: bool = True
    post_only: bool = True  # Always

    # --- Fee Handling ---
    fee_cache_ttl_seconds: int = 300  # Cache fee rates for 5 min

    # --- WebSocket Configuration ---
    market_ws_uri: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    user_ws_uri: str = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
    ws_ping_interval: int = 5
    ws_reconnect_delay_seconds: float = 5.0
    ws_max_reconnect_attempts: int = 10

    # --- General ---
    dry_run: bool = True
    assets: List[str] = field(default_factory=lambda: ["btc", "eth", "sol"])
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "ActiveQuotingConfig":
        """Create config from environment variables."""
        return cls(
            # Quote Pricing
            quote_offset_ticks=int(os.getenv("AQ_QUOTE_OFFSET_TICKS", "0")),
            improve_when_spread_ticks=int(os.getenv("AQ_IMPROVE_WHEN_SPREAD_TICKS", "4")),
            max_spread_ticks=int(os.getenv("AQ_MAX_SPREAD_TICKS", "10")),
            # Quote Refresh
            refresh_threshold_ticks=int(os.getenv("AQ_REFRESH_THRESHOLD_TICKS", "2")),
            min_refresh_interval_ms=int(os.getenv("AQ_MIN_REFRESH_INTERVAL_MS", "500")),
            global_refresh_cap_per_sec=int(os.getenv("AQ_GLOBAL_REFRESH_CAP_PER_SEC", "10")),
            # Momentum Detection
            momentum_threshold_ticks=int(os.getenv("AQ_MOMENTUM_THRESHOLD_TICKS", "3")),
            momentum_window_ms=int(os.getenv("AQ_MOMENTUM_WINDOW_MS", "500")),
            cooldown_seconds=float(os.getenv("AQ_COOLDOWN_SECONDS", "2.0")),
            sweep_depth_threshold=float(os.getenv("AQ_SWEEP_DEPTH_THRESHOLD", "0.5")),
            # Inventory Management
            max_position_per_market=int(os.getenv("AQ_MAX_POSITION_PER_MARKET", "100")),
            max_liability_per_market_usdc=float(os.getenv("AQ_MAX_LIABILITY_PER_MARKET_USDC", "50.0")),
            max_total_liability_usdc=float(os.getenv("AQ_MAX_TOTAL_LIABILITY_USDC", "500.0")),
            inventory_skew_coefficient=float(os.getenv("AQ_INVENTORY_SKEW_COEFFICIENT", "0.1")),
            # Risk Management
            max_drawdown_per_market_usdc=float(os.getenv("AQ_MAX_DRAWDOWN_PER_MARKET_USDC", "20.0")),
            max_drawdown_global_usdc=float(os.getenv("AQ_MAX_DRAWDOWN_GLOBAL_USDC", "100.0")),
            max_consecutive_errors=int(os.getenv("AQ_MAX_CONSECUTIVE_ERRORS", "5")),
            stale_feed_timeout_seconds=float(os.getenv("AQ_STALE_FEED_TIMEOUT_SECONDS", "30.0")),
            circuit_breaker_recovery_seconds=float(os.getenv("AQ_CIRCUIT_BREAKER_RECOVERY_SECONDS", "60.0")),
            # End-of-Market Wind-Down
            wind_down_start_seconds=float(os.getenv("AQ_WIND_DOWN_START_SECONDS", "300.0")),
            wind_down_taker_threshold_seconds=float(os.getenv("AQ_WIND_DOWN_TAKER_THRESHOLD_SECONDS", "40.0")),
            wind_down_taker_price_threshold=float(os.getenv("AQ_WIND_DOWN_TAKER_PRICE_THRESHOLD", "0.25")),
            # WebSocket Gap Safety (Phase 6)
            halt_on_ws_gaps=os.getenv("AQ_HALT_ON_WS_GAPS", "true").lower() == "true",
            ws_gap_reconcile_attempts=int(os.getenv("AQ_WS_GAP_RECONCILE_ATTEMPTS", "3")),
            ws_gap_recovery_interval_seconds=float(os.getenv("AQ_WS_GAP_RECOVERY_INTERVAL_SECONDS", "30.0")),
            # Order Management
            order_size_usdc=float(os.getenv("AQ_ORDER_SIZE_USDC", "10.0")),
            batch_size=int(os.getenv("AQ_BATCH_SIZE", "15")),
            cancel_on_momentum=os.getenv("AQ_CANCEL_ON_MOMENTUM", "true").lower() == "true",
            post_only=os.getenv("AQ_POST_ONLY", "true").lower() == "true",
            # Fee Handling
            fee_cache_ttl_seconds=int(os.getenv("AQ_FEE_CACHE_TTL_SECONDS", "300")),
            # WebSocket Configuration
            market_ws_uri=os.getenv("AQ_MARKET_WS_URI", "wss://ws-subscriptions-clob.polymarket.com/ws/market"),
            user_ws_uri=os.getenv("AQ_USER_WS_URI", "wss://ws-subscriptions-clob.polymarket.com/ws/user"),
            ws_ping_interval=int(os.getenv("AQ_WS_PING_INTERVAL", "5")),
            ws_reconnect_delay_seconds=float(os.getenv("AQ_WS_RECONNECT_DELAY_SECONDS", "5.0")),
            ws_max_reconnect_attempts=int(os.getenv("AQ_WS_MAX_RECONNECT_ATTEMPTS", "10")),
            # General
            dry_run=os.getenv("AQ_DRY_RUN", "true").lower() == "true",
            assets=os.getenv("AQ_ASSETS", "btc,eth,sol").split(","),
            log_level=os.getenv("AQ_LOG_LEVEL", "INFO"),
        )

    def validate(self) -> None:
        """Validate configuration values."""
        errors = []

        # Quote pricing validation
        if self.quote_offset_ticks < 0:
            errors.append("quote_offset_ticks must be >= 0")
        if self.improve_when_spread_ticks < 1:
            errors.append("improve_when_spread_ticks must be >= 1")
        if self.max_spread_ticks < self.improve_when_spread_ticks:
            errors.append("max_spread_ticks must be >= improve_when_spread_ticks")

        # Refresh validation
        if self.refresh_threshold_ticks < 1:
            errors.append("refresh_threshold_ticks must be >= 1")
        if self.min_refresh_interval_ms < 0:
            errors.append("min_refresh_interval_ms must be >= 0")
        if self.global_refresh_cap_per_sec < 1:
            errors.append("global_refresh_cap_per_sec must be >= 1")

        # Momentum validation
        if self.momentum_threshold_ticks < 1:
            errors.append("momentum_threshold_ticks must be >= 1")
        if self.momentum_window_ms < 100:
            errors.append("momentum_window_ms must be >= 100")
        if self.cooldown_seconds < 0:
            errors.append("cooldown_seconds must be >= 0")
        if not 0 < self.sweep_depth_threshold <= 1:
            errors.append("sweep_depth_threshold must be in (0, 1]")

        # Inventory validation
        if self.max_position_per_market < 1:
            errors.append("max_position_per_market must be >= 1")
        if self.max_liability_per_market_usdc <= 0:
            errors.append("max_liability_per_market_usdc must be > 0")
        if self.max_total_liability_usdc < self.max_liability_per_market_usdc:
            errors.append("max_total_liability_usdc must be >= max_liability_per_market_usdc")
        if self.inventory_skew_coefficient < 0:
            errors.append("inventory_skew_coefficient must be >= 0")

        # Risk validation
        if self.max_drawdown_per_market_usdc <= 0:
            errors.append("max_drawdown_per_market_usdc must be > 0")
        if self.max_drawdown_global_usdc < self.max_drawdown_per_market_usdc:
            errors.append("max_drawdown_global_usdc must be >= max_drawdown_per_market_usdc")
        if self.max_consecutive_errors < 1:
            errors.append("max_consecutive_errors must be >= 1")
        if self.stale_feed_timeout_seconds <= 0:
            errors.append("stale_feed_timeout_seconds must be > 0")
        if self.circuit_breaker_recovery_seconds <= 0:
            errors.append("circuit_breaker_recovery_seconds must be > 0")

        # WebSocket Gap Safety validation (Phase 6)
        if self.ws_gap_reconcile_attempts < 1:
            errors.append("ws_gap_reconcile_attempts must be >= 1")
        if self.ws_gap_recovery_interval_seconds <= 0:
            errors.append("ws_gap_recovery_interval_seconds must be > 0")

        # Order validation (only enforce minimum in live mode)
        # Note: order_size_usdc is actually SHARES despite the name. Polymarket minimum is 5 shares.
        if not self.dry_run and self.order_size_usdc < 5:
            errors.append("order_size_usdc must be >= 5 for live trading (Polymarket minimum is 5 shares)")
        if not 1 <= self.batch_size <= 15:
            errors.append("batch_size must be between 1 and 15")

        # Fee validation
        if self.fee_cache_ttl_seconds < 0:
            errors.append("fee_cache_ttl_seconds must be >= 0")

        # WebSocket validation
        if self.ws_ping_interval < 1:
            errors.append("ws_ping_interval must be >= 1")
        if self.ws_reconnect_delay_seconds < 0:
            errors.append("ws_reconnect_delay_seconds must be >= 0")
        if self.ws_max_reconnect_attempts < 1:
            errors.append("ws_max_reconnect_attempts must be >= 1")

        # General validation
        if not self.assets:
            errors.append("assets list must not be empty")

        if errors:
            raise ValueError(f"Configuration validation failed:\n" + "\n".join(f"  - {e}" for e in errors))

    def __post_init__(self):
        """Validate configuration after initialization."""
        self.validate()
