"""
RiskManager - Circuit breaker and risk management for active quoting.

Implements:
- Per-market drawdown tracking (realized + unrealized P&L)
- Global drawdown tracking across all markets
- Stale feed detection (no orderbook update within threshold)
- Circuit breaker states: NORMAL, WARNING, HALTED, RECOVERING
- State transition logic with configurable thresholds
- Recovery logic (gradual re-entry after halt)
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, Optional, Callable, Awaitable, List, Set

from .config import ActiveQuotingConfig
from .models import Position, OrderbookState

logger = logging.getLogger(__name__)


class CircuitBreakerState(Enum):
    """Circuit breaker state machine states."""
    NORMAL = "NORMAL"           # Normal operation
    WARNING = "WARNING"         # Reduced limits (50%)
    HALTED = "HALTED"          # All orders cancelled, no new orders
    RECOVERING = "RECOVERING"  # Gradual re-entry (25% limits)


@dataclass
class MarketRiskState:
    """Risk state for a single market."""
    token_id: str
    realized_pnl: float = 0.0       # Cumulative realized P&L
    unrealized_pnl: float = 0.0     # Current unrealized P&L
    peak_pnl: float = 0.0           # Highest P&L seen (for drawdown calculation)
    current_drawdown: float = 0.0   # Current drawdown from peak
    last_update_time: Optional[datetime] = None
    is_stale: bool = False
    halted: bool = False            # Market-specific halt
    # Market time window for smart stale detection
    market_start_time: Optional[datetime] = None  # When market goes live
    market_end_time: Optional[datetime] = None    # When market resolves

    @property
    def total_pnl(self) -> float:
        """Total P&L (realized + unrealized)."""
        return self.realized_pnl + self.unrealized_pnl

    def should_monitor_staleness(self, now: Optional[datetime] = None) -> bool:
        """
        Check if this market should be monitored for stale feeds.

        Only monitor:
        - Currently live markets (now is between start and end)
        - Next upcoming market (starts within 15 minutes)

        Don't monitor:
        - Resolved markets (end time has passed)
        - Future markets (more than 15 minutes until start)
        """
        if self.market_start_time is None or self.market_end_time is None:
            # No time window set - monitor by default
            return True

        if now is None:
            now = datetime.utcnow()

        # Already resolved - don't monitor
        if now >= self.market_end_time:
            return False

        # Currently live - monitor
        if self.market_start_time <= now < self.market_end_time:
            return True

        # Check if next upcoming (within 15 minutes of start)
        time_until_start = (self.market_start_time - now).total_seconds()
        if 0 < time_until_start <= 900:  # 15 minutes = 900 seconds
            return True

        # Future market (more than 15 min away) - don't monitor
        return False

    def update_pnl(self, realized: float, unrealized: float) -> None:
        """Update P&L and recalculate drawdown."""
        self.realized_pnl = realized
        self.unrealized_pnl = unrealized
        total = self.total_pnl

        # Update peak if we have new high
        if total > self.peak_pnl:
            self.peak_pnl = total

        # Calculate drawdown from peak
        self.current_drawdown = self.peak_pnl - total
        self.last_update_time = datetime.utcnow()


@dataclass
class GlobalRiskState:
    """Global risk state across all markets."""
    total_realized_pnl: float = 0.0
    total_unrealized_pnl: float = 0.0
    peak_total_pnl: float = 0.0
    current_drawdown: float = 0.0
    circuit_breaker_state: CircuitBreakerState = CircuitBreakerState.NORMAL
    halted_at: Optional[datetime] = None
    recovering_since: Optional[datetime] = None
    warning_at: Optional[datetime] = None
    consecutive_errors: int = 0
    last_error_time: Optional[datetime] = None

    @property
    def total_pnl(self) -> float:
        """Total P&L (realized + unrealized)."""
        return self.total_realized_pnl + self.total_unrealized_pnl

    def update_total_pnl(self) -> None:
        """Update peak and drawdown based on current total P&L."""
        total = self.total_pnl

        # Update peak if we have new high
        if total > self.peak_total_pnl:
            self.peak_total_pnl = total

        # Calculate drawdown from peak
        self.current_drawdown = self.peak_total_pnl - total


class RiskManager:
    """
    Manages risk limits and circuit breaker for active quoting.

    This class:
    1. Tracks per-market and global P&L and drawdown
    2. Detects stale market feeds
    3. Implements circuit breaker state machine
    4. Provides position limit multipliers based on state
    5. Triggers callbacks on state changes
    """

    def __init__(
        self,
        config: ActiveQuotingConfig,
        on_state_change: Optional[Callable[[CircuitBreakerState, CircuitBreakerState, str], Awaitable[None]]] = None,
        on_market_halt: Optional[Callable[[str, str], Awaitable[None]]] = None,
        on_kill_switch: Optional[Callable[[], Awaitable[None]]] = None,
    ):
        """
        Initialize the RiskManager.

        Args:
            config: Active quoting configuration
            on_state_change: Callback when circuit breaker state changes (old_state, new_state, reason)
            on_market_halt: Callback when a market is halted (token_id, reason)
            on_kill_switch: Callback when kill switch is triggered (cancel all orders)
        """
        self.config = config
        self.on_state_change = on_state_change
        self.on_market_halt = on_market_halt
        self.on_kill_switch = on_kill_switch

        # State
        self._market_states: Dict[str, MarketRiskState] = {}
        self._global_state = GlobalRiskState()
        self._stale_markets: Set[str] = set()

        # Recovery parameters
        self._recovery_duration_seconds = getattr(
            config, 'circuit_breaker_recovery_seconds', 60.0
        )

    @property
    def state(self) -> CircuitBreakerState:
        """Get current circuit breaker state."""
        return self._global_state.circuit_breaker_state

    @property
    def global_state(self) -> GlobalRiskState:
        """Get global risk state."""
        return self._global_state

    def get_market_state(self, token_id: str) -> MarketRiskState:
        """
        Get risk state for a market, creating if needed.

        Args:
            token_id: Token ID

        Returns:
            MarketRiskState for the token
        """
        if token_id not in self._market_states:
            self._market_states[token_id] = MarketRiskState(token_id=token_id)
        return self._market_states[token_id]

    def set_market_time_window(
        self,
        token_id: str,
        start_time: Optional[datetime],
        end_time: Optional[datetime]
    ) -> None:
        """
        Set the time window for a market (for smart stale detection).

        Args:
            token_id: Token ID
            start_time: When the market goes live
            end_time: When the market resolves
        """
        market_state = self.get_market_state(token_id)
        market_state.market_start_time = start_time
        market_state.market_end_time = end_time

    # --- P&L Tracking ---

    def update_market_pnl(
        self,
        token_id: str,
        realized_pnl: float,
        unrealized_pnl: float
    ) -> None:
        """
        Update P&L for a market.

        Args:
            token_id: Token ID
            realized_pnl: Realized P&L for this market
            unrealized_pnl: Unrealized P&L for this market
        """
        market_state = self.get_market_state(token_id)
        market_state.update_pnl(realized_pnl, unrealized_pnl)

        # Recalculate global state
        self._recalculate_global_pnl()

        # Check for drawdown limits
        self._check_drawdown_limits(token_id)

    def update_from_position(
        self,
        token_id: str,
        position: Position,
        current_price: Optional[float] = None
    ) -> None:
        """
        Update P&L from a position object.

        Args:
            token_id: Token ID
            position: Position object with realized P&L
            current_price: Current market price for unrealized P&L calculation
        """
        realized_pnl = position.realized_pnl

        # Calculate unrealized P&L if we have a current price
        unrealized_pnl = 0.0
        if current_price is not None and position.size > 0:
            unrealized_pnl = (current_price - position.avg_entry_price) * position.size

        self.update_market_pnl(token_id, realized_pnl, unrealized_pnl)

    def _recalculate_global_pnl(self) -> None:
        """Recalculate global P&L from all markets."""
        self._global_state.total_realized_pnl = sum(
            m.realized_pnl for m in self._market_states.values()
        )
        self._global_state.total_unrealized_pnl = sum(
            m.unrealized_pnl for m in self._market_states.values()
        )
        self._global_state.update_total_pnl()

    def _check_drawdown_limits(self, token_id: str) -> None:
        """Check if drawdown limits have been exceeded."""
        market_state = self.get_market_state(token_id)

        # Check per-market drawdown
        if market_state.current_drawdown >= self.config.max_drawdown_per_market_usdc:
            if not market_state.halted:
                market_state.halted = True
                logger.warning(
                    f"Market {token_id} halted: drawdown ${market_state.current_drawdown:.2f} "
                    f">= limit ${self.config.max_drawdown_per_market_usdc:.2f}"
                )
                if self.on_market_halt:
                    asyncio.create_task(
                        self.on_market_halt(
                            token_id,
                            f"Drawdown ${market_state.current_drawdown:.2f} exceeded limit"
                        )
                    )

        # Check global drawdown
        if self._global_state.current_drawdown >= self.config.max_drawdown_global_usdc:
            self._trigger_halt(
                f"Global drawdown ${self._global_state.current_drawdown:.2f} "
                f">= limit ${self.config.max_drawdown_global_usdc:.2f}"
            )

    # --- Stale Feed Detection ---

    def update_feed_timestamp(self, token_id: str) -> None:
        """
        Record that we received a feed update for a market.

        Args:
            token_id: Token ID
        """
        market_state = self.get_market_state(token_id)
        market_state.last_update_time = datetime.utcnow()
        market_state.is_stale = False

        # Remove from stale set if it was there
        self._stale_markets.discard(token_id)

    def check_stale_feeds(self) -> List[str]:
        """
        Check for stale market feeds.

        Only checks markets that are currently live or coming up next.
        Ignores resolved markets and far-future markets.

        Returns:
            List of token IDs with stale feeds
        """
        now = datetime.utcnow()
        threshold_seconds = self.config.stale_feed_timeout_seconds
        stale_tokens = []

        for token_id, market_state in self._market_states.items():
            # Skip markets that shouldn't be monitored (resolved or far future)
            if not market_state.should_monitor_staleness(now):
                # Clear stale flag if market is no longer being monitored
                if market_state.is_stale:
                    market_state.is_stale = False
                    self._stale_markets.discard(token_id)
                continue

            if market_state.last_update_time is None:
                # Never received an update
                continue

            age_seconds = (now - market_state.last_update_time).total_seconds()
            if age_seconds > threshold_seconds:
                if not market_state.is_stale:
                    market_state.is_stale = True
                    self._stale_markets.add(token_id)
                    # Log with market time info for context
                    time_context = ""
                    if market_state.market_start_time and market_state.market_end_time:
                        start_str = market_state.market_start_time.strftime("%H:%M")
                        end_str = market_state.market_end_time.strftime("%H:%M")
                        time_context = f" (market: {start_str}-{end_str} UTC)"
                    logger.warning(
                        f"Market {token_id[:20]}... feed stale: "
                        f"no update for {age_seconds:.1f}s{time_context}"
                    )
                stale_tokens.append(token_id)

        # If any feeds are stale, trigger WARNING state
        if stale_tokens and self.state == CircuitBreakerState.NORMAL:
            self._trigger_warning(f"Stale feeds detected: {len(stale_tokens)} markets")

        return stale_tokens

    def is_feed_stale(self, token_id: str) -> bool:
        """Check if a specific market's feed is stale."""
        return token_id in self._stale_markets

    def get_stale_markets(self) -> Set[str]:
        """Get set of markets with stale feeds."""
        return self._stale_markets.copy()

    # --- Circuit Breaker State Machine ---

    async def _transition_state(
        self,
        new_state: CircuitBreakerState,
        reason: str
    ) -> None:
        """
        Transition to a new circuit breaker state.

        Args:
            new_state: New state to transition to
            reason: Reason for transition
        """
        old_state = self._global_state.circuit_breaker_state
        if old_state == new_state:
            return

        self._global_state.circuit_breaker_state = new_state
        now = datetime.utcnow()

        # Update timestamps
        if new_state == CircuitBreakerState.HALTED:
            self._global_state.halted_at = now
            self._global_state.recovering_since = None
            self._global_state.warning_at = None
        elif new_state == CircuitBreakerState.RECOVERING:
            self._global_state.recovering_since = now
            self._global_state.warning_at = None
        elif new_state == CircuitBreakerState.WARNING:
            self._global_state.warning_at = now
            self._global_state.halted_at = None
            self._global_state.recovering_since = None
        elif new_state == CircuitBreakerState.NORMAL:
            self._global_state.halted_at = None
            self._global_state.recovering_since = None
            self._global_state.warning_at = None

        logger.warning(
            f"Circuit breaker: {old_state.value} -> {new_state.value} "
            f"(reason: {reason})"
        )

        # Trigger callback
        if self.on_state_change:
            await self.on_state_change(old_state, new_state, reason)

        # If transitioning to HALTED, trigger kill switch
        if new_state == CircuitBreakerState.HALTED:
            if self.on_kill_switch:
                await self.on_kill_switch()

    def _trigger_warning(self, reason: str) -> None:
        """Trigger WARNING state."""
        if self.state not in (CircuitBreakerState.HALTED, CircuitBreakerState.RECOVERING):
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(
                    self._transition_state(CircuitBreakerState.WARNING, reason)
                )
            except RuntimeError:
                # No running event loop - set state directly
                self._global_state.circuit_breaker_state = CircuitBreakerState.WARNING
                self._global_state.warning_at = datetime.utcnow()
                logger.warning(f"Circuit breaker: WARNING (reason: {reason})")

    def _trigger_halt(self, reason: str) -> None:
        """Trigger HALTED state."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                self._transition_state(CircuitBreakerState.HALTED, reason)
            )
        except RuntimeError:
            # No running event loop - set state directly
            self._global_state.circuit_breaker_state = CircuitBreakerState.HALTED
            self._global_state.halted_at = datetime.utcnow()
            logger.warning(f"Circuit breaker: HALTED (reason: {reason})")

    async def trigger_halt(self, reason: str) -> None:
        """Public method to trigger HALTED state."""
        await self._transition_state(CircuitBreakerState.HALTED, reason)

    async def trigger_warning(self, reason: str) -> None:
        """Public method to trigger WARNING state."""
        if self.state not in (CircuitBreakerState.HALTED, CircuitBreakerState.RECOVERING):
            await self._transition_state(CircuitBreakerState.WARNING, reason)

    async def start_recovery(self) -> None:
        """
        Start recovery from HALTED state.

        Called after the halt condition has been resolved.
        """
        if self.state != CircuitBreakerState.HALTED:
            logger.warning("Cannot start recovery: not in HALTED state")
            return

        await self._transition_state(
            CircuitBreakerState.RECOVERING,
            "Manual recovery initiated"
        )

    async def check_recovery_complete(self) -> bool:
        """
        Check if recovery period is complete.

        Returns:
            True if recovery is complete and we can return to NORMAL
        """
        if self.state != CircuitBreakerState.RECOVERING:
            return False

        if self._global_state.recovering_since is None:
            return False

        recovery_time = (datetime.utcnow() - self._global_state.recovering_since).total_seconds()
        if recovery_time >= self._recovery_duration_seconds:
            await self._transition_state(
                CircuitBreakerState.NORMAL,
                f"Recovery period complete ({recovery_time:.0f}s)"
            )
            return True

        return False

    async def clear_warning(self) -> None:
        """
        Clear WARNING state and return to NORMAL.

        Called when warning condition has been resolved.
        """
        if self.state == CircuitBreakerState.WARNING:
            await self._transition_state(
                CircuitBreakerState.NORMAL,
                "Warning condition cleared"
            )

    # --- Position Limit Multipliers ---

    def get_position_limit_multiplier(self) -> float:
        """
        Get position limit multiplier based on circuit breaker state.

        Returns:
            Multiplier to apply to position limits:
            - NORMAL: 1.0 (100%)
            - WARNING: 0.5 (50%)
            - RECOVERING: 0.25 (25%)
            - HALTED: 0.0 (no new positions)
        """
        state = self.state
        if state == CircuitBreakerState.NORMAL:
            return 1.0
        elif state == CircuitBreakerState.WARNING:
            return 0.5
        elif state == CircuitBreakerState.RECOVERING:
            return 0.25
        elif state == CircuitBreakerState.HALTED:
            return 0.0
        return 0.0

    def can_place_orders(self) -> bool:
        """Check if orders can be placed in current state."""
        return self.state != CircuitBreakerState.HALTED

    def get_adjusted_position_limit(self, base_limit: int) -> int:
        """
        Get risk-adjusted position limit.

        Args:
            base_limit: The base position limit from config

        Returns:
            Adjusted limit based on circuit breaker state
        """
        multiplier = self.get_position_limit_multiplier()
        return int(base_limit * multiplier)

    def get_adjusted_liability_limit(self, base_limit: float) -> float:
        """
        Get risk-adjusted liability limit.

        Args:
            base_limit: The base liability limit from config

        Returns:
            Adjusted limit based on circuit breaker state
        """
        multiplier = self.get_position_limit_multiplier()
        return base_limit * multiplier

    def get_adjusted_order_size(self, base_size: float) -> float:
        """
        Get risk-adjusted order size.

        Args:
            base_size: The base order size from config

        Returns:
            Adjusted size based on circuit breaker state
        """
        multiplier = self.get_position_limit_multiplier()
        return base_size * multiplier

    def can_place_orders_for_market(self, token_id: str) -> tuple[bool, str]:
        """
        Check if orders can be placed for a specific market.

        Args:
            token_id: Token ID

        Returns:
            Tuple of (allowed, reason)
        """
        # Check global state first
        if self.state == CircuitBreakerState.HALTED:
            return False, "Circuit breaker halted"

        # Check market-specific halt
        market_state = self.get_market_state(token_id)
        if market_state.halted:
            return False, f"Market halted due to drawdown"

        # Check stale feed
        if market_state.is_stale:
            return False, "Market feed is stale"

        # Check if market has ended (can't trade on resolved markets)
        if market_state.market_end_time is not None:
            now = datetime.utcnow()
            if now >= market_state.market_end_time:
                return False, "Market has ended"

        return True, ""

    # --- Error Tracking ---

    def record_error(self) -> None:
        """Record an error occurrence for circuit breaker tracking."""
        now = datetime.utcnow()

        # Reset consecutive errors if last error was more than 60s ago
        if self._global_state.last_error_time:
            time_since_last = (now - self._global_state.last_error_time).total_seconds()
            if time_since_last > 60:
                self._global_state.consecutive_errors = 0

        self._global_state.consecutive_errors += 1
        self._global_state.last_error_time = now

        # Check error threshold
        if self._global_state.consecutive_errors >= self.config.max_consecutive_errors:
            self._trigger_halt(
                f"Max consecutive errors reached: "
                f"{self._global_state.consecutive_errors}"
            )

    def clear_errors(self) -> None:
        """Clear error count (called after successful operation)."""
        self._global_state.consecutive_errors = 0

    # --- Disconnect Handling ---

    async def on_market_disconnect(self) -> None:
        """Handle market WebSocket disconnect."""
        logger.warning("Market WebSocket disconnected - triggering WARNING")
        await self.trigger_warning("Market WebSocket disconnected")

    async def on_user_disconnect(self) -> None:
        """
        Handle user WebSocket disconnect.

        This is critical - without user channel we can't track fills.
        """
        logger.error("User WebSocket disconnected - triggering HALT")
        await self.trigger_halt("User WebSocket disconnected (cannot track fills)")

    # --- State Summary ---

    def get_summary(self) -> dict:
        """Get summary of risk state for logging/monitoring."""
        return {
            "state": self.state.value,
            "global": {
                "total_pnl": self._global_state.total_pnl,
                "realized_pnl": self._global_state.total_realized_pnl,
                "unrealized_pnl": self._global_state.total_unrealized_pnl,
                "current_drawdown": self._global_state.current_drawdown,
                "peak_pnl": self._global_state.peak_total_pnl,
                "consecutive_errors": self._global_state.consecutive_errors,
            },
            "markets": {
                token_id: {
                    "total_pnl": m.total_pnl,
                    "drawdown": m.current_drawdown,
                    "halted": m.halted,
                    "stale": m.is_stale,
                }
                for token_id, m in self._market_states.items()
            },
            "stale_markets": list(self._stale_markets),
            "position_limit_multiplier": self.get_position_limit_multiplier(),
        }

    # --- Reset Methods ---

    def reset_market(self, token_id: str) -> None:
        """Reset risk state for a market."""
        if token_id in self._market_states:
            del self._market_states[token_id]
        self._stale_markets.discard(token_id)

    def reset_all(self) -> None:
        """Reset all risk state."""
        self._market_states.clear()
        self._stale_markets.clear()
        self._global_state = GlobalRiskState()

    async def force_reset_to_normal(self) -> None:
        """Force reset circuit breaker to NORMAL state (for testing/recovery)."""
        await self._transition_state(
            CircuitBreakerState.NORMAL,
            "Forced reset"
        )
        self._global_state.consecutive_errors = 0
