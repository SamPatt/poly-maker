"""
RedemptionManager - Handles automatic redemption of resolved market positions.

Monitors markets for resolution and triggers redemption of winning positions
after the market has officially resolved on-chain.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, Optional, Callable, Awaitable, Set

logger = logging.getLogger(__name__)


class RedemptionState(Enum):
    """State of a redemption attempt."""
    PENDING = "PENDING"      # Waiting for resolution
    CHECKING = "CHECKING"    # Checking if resolved
    REDEEMING = "REDEEMING"  # Redemption in progress
    COMPLETED = "COMPLETED"  # Successfully redeemed
    FAILED = "FAILED"        # Redemption failed
    SKIPPED = "SKIPPED"      # No position to redeem


@dataclass
class MarketRedemptionState:
    """Tracks redemption state for a single market."""
    token_id: str
    condition_id: str
    market_end_time: datetime
    position_size: float = 0.0
    state: RedemptionState = RedemptionState.PENDING
    first_check_time: Optional[datetime] = None  # When we first checked after end
    last_check_time: Optional[datetime] = None
    check_count: int = 0
    tx_hash: Optional[str] = None
    error_message: Optional[str] = None


class RedemptionManager:
    """
    Manages market resolution detection and position redemption.

    This class:
    1. Tracks markets awaiting resolution
    2. Polls to detect when markets have resolved (with configurable delay)
    3. Triggers redemption via the redemption module
    4. Handles success/failure callbacks

    Note: There's typically a delay between when a market's time window ends
    and when it's officially resolved on-chain. This manager handles that delay
    by polling with backoff until resolution is detected.
    """

    def __init__(
        self,
        on_redemption_complete: Optional[Callable[[str, str, str, float], Awaitable[None]]] = None,
        on_redemption_error: Optional[Callable[[str, str, str], Awaitable[None]]] = None,
        resolution_check_delay_seconds: float = 60.0,  # Wait after market end before first check
        resolution_check_interval_seconds: float = 30.0,  # Interval between resolution checks
        max_resolution_check_attempts: int = 20,  # Max attempts before giving up (~10 min)
    ):
        """
        Initialize the RedemptionManager.

        Args:
            on_redemption_complete: Callback(token_id, condition_id, tx_hash, position_size)
            on_redemption_error: Callback(token_id, condition_id, error_message)
            resolution_check_delay_seconds: How long to wait after market_end_time before first check
            resolution_check_interval_seconds: How long between resolution check attempts
            max_resolution_check_attempts: Max times to check before giving up
        """
        self.on_redemption_complete = on_redemption_complete
        self.on_redemption_error = on_redemption_error
        self.resolution_check_delay_seconds = resolution_check_delay_seconds
        self.resolution_check_interval_seconds = resolution_check_interval_seconds
        self.max_resolution_check_attempts = max_resolution_check_attempts

        # State tracking
        self._markets: Dict[str, MarketRedemptionState] = {}  # token_id -> state
        self._pending_redemptions: Set[str] = set()  # token_ids with redemption in progress
        self._completed_redemptions: Set[str] = set()  # token_ids already redeemed

    def register_market(
        self,
        token_id: str,
        condition_id: str,
        market_end_time: datetime,
        position_size: float = 0.0,
    ) -> None:
        """
        Register a market for redemption tracking.

        Args:
            token_id: Token ID
            condition_id: Market's condition ID for redemption
            market_end_time: When the market's time window ends
            position_size: Current position size (will be updated before redemption)
        """
        if not condition_id:
            logger.debug(f"No condition_id for {token_id[:20]}..., skipping registration")
            return

        if token_id in self._completed_redemptions:
            logger.debug(f"Market {token_id[:20]}... already redeemed, skipping")
            return

        self._markets[token_id] = MarketRedemptionState(
            token_id=token_id,
            condition_id=condition_id,
            market_end_time=market_end_time,
            position_size=position_size,
        )
        logger.debug(
            f"Registered market {token_id[:20]}... for redemption "
            f"(end: {market_end_time.strftime('%H:%M:%S UTC')}, condition: {condition_id[:20]}...)"
        )

    def update_position_size(self, token_id: str, size: float) -> None:
        """Update the position size for a market."""
        if token_id in self._markets:
            self._markets[token_id].position_size = size

    def get_markets_ready_for_check(self) -> list[MarketRedemptionState]:
        """
        Get markets that are ready to check for resolution.

        Returns markets where:
        - Market end time has passed + initial delay
        - Not already being redeemed
        - Not already completed
        - Haven't exceeded max check attempts
        """
        ready = []
        now = datetime.utcnow()

        for token_id, state in self._markets.items():
            # Skip if already in progress or completed
            if token_id in self._pending_redemptions:
                continue
            if token_id in self._completed_redemptions:
                continue
            if state.state in (RedemptionState.COMPLETED, RedemptionState.SKIPPED):
                continue

            # Check if market end time + delay has passed
            check_after = state.market_end_time + timedelta(seconds=self.resolution_check_delay_seconds)
            if now < check_after:
                continue

            # Check if we haven't exceeded max attempts
            if state.check_count >= self.max_resolution_check_attempts:
                if state.state != RedemptionState.FAILED:
                    state.state = RedemptionState.FAILED
                    state.error_message = f"Exceeded max check attempts ({self.max_resolution_check_attempts})"
                    logger.warning(f"Market {token_id[:20]}... exceeded max resolution checks")
                continue

            # Check interval since last check
            if state.last_check_time:
                time_since_check = (now - state.last_check_time).total_seconds()
                if time_since_check < self.resolution_check_interval_seconds:
                    continue

            ready.append(state)

        return ready

    async def attempt_redemption(
        self,
        token_id: str,
        current_position_size: float,
    ) -> bool:
        """
        Attempt to redeem a position for a resolved market.

        Args:
            token_id: Token ID
            current_position_size: Current position size from inventory manager

        Returns:
            True if redemption was initiated, False otherwise
        """
        state = self._markets.get(token_id)
        if not state:
            logger.warning(f"No redemption state for {token_id[:20]}...")
            return False

        # Update position size
        state.position_size = current_position_size

        # Check if we have a position to redeem
        if current_position_size <= 0:
            logger.info(f"No position to redeem for {token_id[:20]}... (size: {current_position_size})")
            state.state = RedemptionState.SKIPPED
            self._completed_redemptions.add(token_id)
            return False

        # Mark as in progress
        state.state = RedemptionState.REDEEMING
        self._pending_redemptions.add(token_id)

        # Record check time
        now = datetime.utcnow()
        if state.first_check_time is None:
            state.first_check_time = now
        state.last_check_time = now
        state.check_count += 1

        logger.info(
            f"Attempting redemption for {token_id[:20]}... "
            f"(condition: {state.condition_id[:20]}..., size: {current_position_size:.2f})"
        )

        # Import redemption module here to avoid circular imports
        from redemption import redeem_position_async

        def on_success(condition_id: str, tx_hash: str) -> None:
            """Handle successful redemption."""
            asyncio.create_task(self._handle_redemption_success(token_id, condition_id, tx_hash))

        def on_error(condition_id: str, error_msg: str) -> None:
            """Handle failed redemption."""
            asyncio.create_task(self._handle_redemption_error(token_id, condition_id, error_msg))

        # Trigger async redemption
        redeem_position_async(state.condition_id, on_success, on_error)
        return True

    async def _handle_redemption_success(
        self,
        token_id: str,
        condition_id: str,
        tx_hash: str,
    ) -> None:
        """Handle successful redemption callback."""
        state = self._markets.get(token_id)
        if state:
            state.state = RedemptionState.COMPLETED
            state.tx_hash = tx_hash
            position_size = state.position_size
        else:
            position_size = 0.0

        self._pending_redemptions.discard(token_id)
        self._completed_redemptions.add(token_id)

        logger.info(
            f"Redemption successful for {token_id[:20]}... "
            f"(tx: {tx_hash[:20] if tx_hash else 'N/A'}...)"
        )

        # Trigger callback
        if self.on_redemption_complete:
            await self.on_redemption_complete(token_id, condition_id, tx_hash, position_size)

    async def _handle_redemption_error(
        self,
        token_id: str,
        condition_id: str,
        error_msg: str,
    ) -> None:
        """Handle failed redemption callback."""
        state = self._markets.get(token_id)

        # Check if this is a "not resolved yet" error - we should retry
        is_not_resolved = any(phrase in error_msg.lower() for phrase in [
            "not resolved",
            "condition not resolved",
            "payout not set",
            "payoutdenominator is 0",
        ])

        if is_not_resolved and state and state.check_count < self.max_resolution_check_attempts:
            # Market not resolved yet - go back to checking state
            logger.info(
                f"Market {token_id[:20]}... not yet resolved, will retry "
                f"(attempt {state.check_count}/{self.max_resolution_check_attempts})"
            )
            state.state = RedemptionState.CHECKING
            self._pending_redemptions.discard(token_id)
            return

        # Actual failure
        if state:
            state.state = RedemptionState.FAILED
            state.error_message = error_msg

        self._pending_redemptions.discard(token_id)

        logger.error(f"Redemption failed for {token_id[:20]}...: {error_msg}")

        # Trigger callback
        if self.on_redemption_error:
            await self.on_redemption_error(token_id, condition_id, error_msg)

    def get_state(self, token_id: str) -> Optional[MarketRedemptionState]:
        """Get redemption state for a market."""
        return self._markets.get(token_id)

    def is_redemption_complete(self, token_id: str) -> bool:
        """Check if redemption is complete for a market."""
        return token_id in self._completed_redemptions

    def is_redemption_pending(self, token_id: str) -> bool:
        """Check if redemption is in progress for a market."""
        return token_id in self._pending_redemptions

    def get_summary(self) -> dict:
        """Get summary of redemption states."""
        states = {}
        for state in RedemptionState:
            states[state.value] = sum(
                1 for m in self._markets.values() if m.state == state
            )

        return {
            "total_markets": len(self._markets),
            "pending_redemptions": len(self._pending_redemptions),
            "completed_redemptions": len(self._completed_redemptions),
            "states": states,
        }

    def clear_market(self, token_id: str) -> None:
        """Remove a market from tracking."""
        self._markets.pop(token_id, None)
        self._pending_redemptions.discard(token_id)
        # Don't remove from completed - we want to remember we redeemed it
