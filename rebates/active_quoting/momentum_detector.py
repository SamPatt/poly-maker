"""
MomentumDetector - Adverse selection protection for active quoting.

Handles:
- Track last_trade_price movements (via OrderbookManager on_trade callback)
- Detect price moves >= MOMENTUM_THRESHOLD_TICKS within MOMENTUM_WINDOW_MS
- Detect book sweeps (sudden depth removal >= SWEEP_DEPTH_THRESHOLD)
- Trigger cooldown periods (COOLDOWN_SECONDS)
- Cooldown expiry logic
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Optional, Callable, Awaitable

from .config import ActiveQuotingConfig
from .models import MomentumState, OrderbookState

logger = logging.getLogger(__name__)


@dataclass
class MomentumEvent:
    """Event triggered when momentum is detected."""
    token_id: str
    event_type: str  # "price_move" or "book_sweep"
    details: str
    cooldown_until: datetime


class MomentumDetector:
    """
    Detects momentum and adverse selection signals to protect against fills.

    This class monitors:
    1. Price moves: Rapid price changes within a time window
    2. Book sweeps: Sudden depth removal indicating aggressive taking

    When momentum is detected, a cooldown period is triggered during which
    the bot should cancel quotes to avoid adverse selection.
    """

    def __init__(
        self,
        config: ActiveQuotingConfig,
        on_momentum: Optional[Callable[[MomentumEvent], Awaitable[None]]] = None,
    ):
        """
        Initialize the MomentumDetector.

        Args:
            config: Active quoting configuration
            on_momentum: Callback when momentum is detected
        """
        self.config = config
        self.on_momentum = on_momentum
        self._states: Dict[str, MomentumState] = {}  # token_id -> MomentumState

    @property
    def states(self) -> Dict[str, MomentumState]:
        """Get all momentum states."""
        return self._states

    def get_state(self, token_id: str) -> MomentumState:
        """
        Get momentum state for a token, creating if not exists.

        Args:
            token_id: Token ID

        Returns:
            MomentumState for the token
        """
        if token_id not in self._states:
            self._states[token_id] = MomentumState(token_id=token_id)
        return self._states[token_id]

    def in_cooldown(self, token_id: str) -> bool:
        """
        Check if a token is in cooldown.

        Args:
            token_id: Token ID

        Returns:
            True if in cooldown
        """
        state = self.get_state(token_id)
        return state.in_cooldown()

    def cooldown_remaining_seconds(self, token_id: str) -> float:
        """
        Get remaining cooldown time in seconds.

        Args:
            token_id: Token ID

        Returns:
            Seconds remaining, or 0 if not in cooldown
        """
        state = self.get_state(token_id)
        if not state.in_cooldown():
            return 0

        remaining = (state.cooldown_until - datetime.utcnow()).total_seconds()
        return max(0, remaining)

    async def on_trade(
        self,
        token_id: str,
        price: float,
        tick_size: float,
        timestamp: Optional[datetime] = None,
    ) -> Optional[MomentumEvent]:
        """
        Process a trade event and check for momentum.

        Args:
            token_id: Token ID
            price: Trade price
            tick_size: Market tick size
            timestamp: Trade timestamp (uses current time if not provided)

        Returns:
            MomentumEvent if momentum detected, None otherwise
        """
        state = self.get_state(token_id)
        ts = timestamp or datetime.utcnow()

        # Add trade to history
        state.add_trade(price, ts)

        # Check for price move momentum
        price_change_ticks = state.price_change_ticks(
            self.config.momentum_window_ms,
            tick_size
        )

        if price_change_ticks >= self.config.momentum_threshold_ticks:
            event = await self._trigger_cooldown(
                token_id,
                "price_move",
                f"Price moved {price_change_ticks} ticks in {self.config.momentum_window_ms}ms"
            )
            return event

        return None

    async def on_orderbook_update(
        self,
        orderbook: OrderbookState,
    ) -> Optional[MomentumEvent]:
        """
        Process an orderbook update and check for sweeps.

        A sweep is detected when depth drops significantly compared to
        the previous update.

        Args:
            orderbook: Updated orderbook state

        Returns:
            MomentumEvent if sweep detected, None otherwise
        """
        state = self.get_state(orderbook.token_id)
        tick_size = orderbook.tick_size

        # Calculate current depth
        current_bid_depth = orderbook.bid_depth()
        current_ask_depth = orderbook.ask_depth()

        # Check for bid sweep
        if state.last_bid_depth is not None and state.last_bid_depth > 0:
            depth_ratio = current_bid_depth / state.last_bid_depth
            if depth_ratio < (1 - self.config.sweep_depth_threshold):
                event = await self._trigger_cooldown(
                    orderbook.token_id,
                    "book_sweep",
                    f"Bid depth dropped {(1 - depth_ratio) * 100:.1f}% "
                    f"({state.last_bid_depth:.1f} -> {current_bid_depth:.1f})"
                )
                # Update depth tracking after triggering
                state.last_bid_depth = current_bid_depth
                state.last_ask_depth = current_ask_depth
                return event

        # Check for ask sweep
        if state.last_ask_depth is not None and state.last_ask_depth > 0:
            depth_ratio = current_ask_depth / state.last_ask_depth
            if depth_ratio < (1 - self.config.sweep_depth_threshold):
                event = await self._trigger_cooldown(
                    orderbook.token_id,
                    "book_sweep",
                    f"Ask depth dropped {(1 - depth_ratio) * 100:.1f}% "
                    f"({state.last_ask_depth:.1f} -> {current_ask_depth:.1f})"
                )
                # Update depth tracking after triggering
                state.last_bid_depth = current_bid_depth
                state.last_ask_depth = current_ask_depth
                return event

        # Update depth tracking
        state.last_bid_depth = current_bid_depth
        state.last_ask_depth = current_ask_depth

        return None

    async def _trigger_cooldown(
        self,
        token_id: str,
        event_type: str,
        details: str,
    ) -> MomentumEvent:
        """
        Trigger a cooldown period.

        Args:
            token_id: Token ID
            event_type: Type of momentum event
            details: Description of the event

        Returns:
            MomentumEvent describing the trigger
        """
        state = self.get_state(token_id)
        cooldown_until = datetime.utcnow() + timedelta(seconds=self.config.cooldown_seconds)

        state.is_active = True
        state.cooldown_until = cooldown_until

        logger.warning(
            f"Momentum detected for {token_id}: {event_type} - {details}. "
            f"Cooldown until {cooldown_until.isoformat()}"
        )

        event = MomentumEvent(
            token_id=token_id,
            event_type=event_type,
            details=details,
            cooldown_until=cooldown_until,
        )

        if self.on_momentum:
            await self.on_momentum(event)

        return event

    def check_cooldown_expired(self, token_id: str) -> bool:
        """
        Check if cooldown has expired and reset state if so.

        Args:
            token_id: Token ID

        Returns:
            True if cooldown just expired (was active, now expired)
        """
        state = self.get_state(token_id)

        if state.is_active and not state.in_cooldown():
            # Cooldown has expired
            state.is_active = False
            state.cooldown_until = None
            logger.info(f"Cooldown expired for {token_id}")
            return True

        return False

    def force_cooldown(
        self,
        token_id: str,
        seconds: Optional[float] = None,
        reason: str = "manual",
    ) -> MomentumEvent:
        """
        Force a cooldown period (for testing or manual intervention).

        Args:
            token_id: Token ID
            seconds: Cooldown duration (uses config default if not provided)
            reason: Reason for forced cooldown

        Returns:
            MomentumEvent describing the cooldown
        """
        duration = seconds if seconds is not None else self.config.cooldown_seconds
        cooldown_until = datetime.utcnow() + timedelta(seconds=duration)

        state = self.get_state(token_id)
        state.is_active = True
        state.cooldown_until = cooldown_until

        return MomentumEvent(
            token_id=token_id,
            event_type="forced",
            details=reason,
            cooldown_until=cooldown_until,
        )

    def clear_cooldown(self, token_id: str) -> None:
        """
        Clear cooldown for a token (for testing or manual reset).

        Args:
            token_id: Token ID
        """
        state = self.get_state(token_id)
        state.is_active = False
        state.cooldown_until = None

    def reset(self, token_id: str) -> None:
        """
        Reset all momentum state for a token.

        Args:
            token_id: Token ID
        """
        if token_id in self._states:
            del self._states[token_id]

    def reset_all(self) -> None:
        """Reset all momentum states."""
        self._states.clear()

    def get_summary(self) -> Dict[str, dict]:
        """
        Get summary of momentum states for logging/monitoring.

        Returns:
            Dict mapping token_id to state summary
        """
        return {
            token_id: {
                "in_cooldown": state.in_cooldown(),
                "cooldown_remaining": self.cooldown_remaining_seconds(token_id),
                "trade_count": len(state.last_trade_prices),
                "last_trade_price": state.last_trade_prices[-1] if state.last_trade_prices else None,
            }
            for token_id, state in self._states.items()
        }

    def get_active_cooldowns(self) -> Dict[str, float]:
        """
        Get all tokens currently in cooldown.

        Returns:
            Dict mapping token_id to remaining cooldown seconds
        """
        return {
            token_id: self.cooldown_remaining_seconds(token_id)
            for token_id, state in self._states.items()
            if state.in_cooldown()
        }
