"""
InventoryManager - Position tracking and skewing for active quoting.

Handles:
- Position tracking per market (authoritative from UserChannelManager fills)
- Liability calculation (worst-case loss = shares x entry_price)
- Skew factor calculation based on position
- Hard position limits enforcement (MAX_POSITION_PER_MARKET, MAX_LIABILITY_PER_MARKET_USDC)
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, Optional

from .config import ActiveQuotingConfig
from .models import Fill, Position, OrderSide

logger = logging.getLogger(__name__)


@dataclass
class InventoryLimits:
    """Result of checking position limits."""
    can_buy: bool = True
    can_sell: bool = True
    buy_limit_reason: str = ""
    sell_limit_reason: str = ""


class InventoryManager:
    """
    Manages inventory/position tracking for active quoting.

    This class:
    1. Tracks positions per token from fills (authoritative source)
    2. Calculates worst-case liability (max loss if position goes to 0)
    3. Computes skew factor to encourage position rebalancing
    4. Enforces hard position limits to control risk
    """

    def __init__(self, config: ActiveQuotingConfig):
        """
        Initialize the InventoryManager.

        Args:
            config: Active quoting configuration
        """
        self.config = config
        self._positions: Dict[str, Position] = {}  # token_id -> Position
        self._pending_buys: Dict[str, float] = {}  # token_id -> pending buy size

    @property
    def positions(self) -> Dict[str, Position]:
        """Get all positions."""
        return self._positions

    def get_position(self, token_id: str) -> Position:
        """
        Get position for a token, creating if not exists.

        Args:
            token_id: Token ID

        Returns:
            Position for the token
        """
        if token_id not in self._positions:
            self._positions[token_id] = Position(token_id=token_id)
        return self._positions[token_id]

    def get_inventory(self, token_id: str) -> float:
        """
        Get current inventory (position size) for a token.

        Args:
            token_id: Token ID

        Returns:
            Inventory size (positive = long)
        """
        return self.get_position(token_id).size

    def update_from_fill(self, fill: Fill) -> None:
        """
        Update position based on a fill event.

        This is the authoritative source for position updates.

        Args:
            fill: Fill from UserChannelManager
        """
        position = self.get_position(fill.token_id)
        old_size = position.size

        position.update_from_fill(fill)

        logger.info(
            f"Position updated: {fill.token_id} "
            f"{old_size:.2f} -> {position.size:.2f} "
            f"(fill: {fill.side.value} {fill.size:.2f} @ {fill.price:.4f})"
        )

        # Release pending buy capacity when fill comes in
        if fill.side == OrderSide.BUY:
            self.release_pending_buy(fill.token_id, fill.size)

    def reserve_pending_buy(self, token_id: str, size: float) -> None:
        """
        Reserve capacity for a pending buy order.

        This prevents the race condition where multiple orders are placed
        before fills come in to update the position.

        Args:
            token_id: Token ID
            size: Order size to reserve
        """
        current = self._pending_buys.get(token_id, 0.0)
        self._pending_buys[token_id] = current + size
        logger.debug(f"Reserved {size:.2f} buy capacity for {token_id[:20]}... (total pending: {self._pending_buys[token_id]:.2f})")

    def release_pending_buy(self, token_id: str, size: float) -> None:
        """
        Release reserved capacity (on fill or cancel).

        Args:
            token_id: Token ID
            size: Order size to release
        """
        current = self._pending_buys.get(token_id, 0.0)
        self._pending_buys[token_id] = max(0.0, current - size)
        logger.debug(f"Released {size:.2f} buy capacity for {token_id[:20]}... (remaining pending: {self._pending_buys[token_id]:.2f})")

    def clear_pending_buys(self, token_id: str) -> None:
        """
        Clear all pending buy reservations for a token.

        Called when cancelling all orders for a token.

        Args:
            token_id: Token ID
        """
        if token_id in self._pending_buys:
            released = self._pending_buys[token_id]
            self._pending_buys[token_id] = 0.0
            if released > 0:
                logger.debug(f"Cleared {released:.2f} pending buy capacity for {token_id[:20]}...")

    def clear_all_pending_buys(self) -> None:
        """
        Clear all pending buy reservations for all tokens.

        Called when cancelling all orders (shutdown, kill switch).
        """
        total = sum(self._pending_buys.values())
        self._pending_buys.clear()
        if total > 0:
            logger.info(f"Cleared {total:.2f} total pending buy capacity")

    def get_pending_buy_size(self, token_id: str) -> float:
        """
        Get total pending buy size for a token.

        Args:
            token_id: Token ID

        Returns:
            Total pending buy order size
        """
        return self._pending_buys.get(token_id, 0.0)

    def calculate_liability(self, token_id: str) -> float:
        """
        Calculate worst-case liability for a position.

        For binary options, max loss = shares x entry_price (if outcome goes to 0).

        Args:
            token_id: Token ID

        Returns:
            Maximum liability in USDC
        """
        position = self.get_position(token_id)
        return position.max_liability

    def calculate_total_liability(self) -> float:
        """
        Calculate total liability across all positions.

        Returns:
            Total maximum liability in USDC
        """
        return sum(
            pos.max_liability for pos in self._positions.values()
        )

    def calculate_skew_factor(self, token_id: str) -> float:
        """
        Calculate skew factor for quote adjustment.

        Skew = inventory_skew_coefficient x inventory

        Positive skew (long position) -> lower bids/asks (encourage selling)
        Negative skew (short position) -> higher bids/asks (encourage buying)

        Args:
            token_id: Token ID

        Returns:
            Skew factor (in ticks, based on coefficient)
        """
        inventory = self.get_inventory(token_id)
        return self.config.inventory_skew_coefficient * inventory

    def calculate_skew_ticks(self, token_id: str, tick_size: float) -> int:
        """
        Calculate skew in whole ticks.

        Args:
            token_id: Token ID
            tick_size: Market tick size

        Returns:
            Number of ticks to skew quotes
        """
        skew_factor = self.calculate_skew_factor(token_id)
        return int(round(skew_factor))

    def check_limits(self, token_id: str) -> InventoryLimits:
        """
        Check if position limits allow buying/selling.

        Limits checked:
        - MAX_POSITION_PER_MARKET: Share count limit (including pending orders)
        - MAX_LIABILITY_PER_MARKET_USDC: Worst-case loss limit
        - MAX_TOTAL_LIABILITY_USDC: Total exposure limit

        Args:
            token_id: Token ID

        Returns:
            InventoryLimits indicating what actions are allowed
        """
        limits = InventoryLimits()
        position = self.get_position(token_id)
        pending = self.get_pending_buy_size(token_id)
        effective_position = position.size + pending

        # Check position size limit (including pending orders)
        if effective_position >= self.config.max_position_per_market:
            limits.can_buy = False
            limits.buy_limit_reason = (
                f"Position {position.size:.0f} + pending {pending:.0f} >= max {self.config.max_position_per_market}"
            )

        # Check liability per market
        liability = self.calculate_liability(token_id)
        if liability >= self.config.max_liability_per_market_usdc:
            limits.can_buy = False
            limits.buy_limit_reason = (
                f"Liability ${liability:.2f} >= max ${self.config.max_liability_per_market_usdc:.2f}"
            )

        # Check total liability
        total_liability = self.calculate_total_liability()
        if total_liability >= self.config.max_total_liability_usdc:
            limits.can_buy = False
            limits.buy_limit_reason = (
                f"Total liability ${total_liability:.2f} >= max ${self.config.max_total_liability_usdc:.2f}"
            )

        # Selling is always allowed (reduces position)
        # But if position is 0 or negative, can't sell
        if position.size <= 0:
            limits.can_sell = False
            limits.sell_limit_reason = "No position to sell"

        return limits

    def can_place_order(self, token_id: str, side: OrderSide, size: float) -> tuple[bool, str]:
        """
        Check if an order can be placed given position limits.

        Args:
            token_id: Token ID
            side: Order side
            size: Order size in shares

        Returns:
            Tuple of (allowed, reason)
        """
        limits = self.check_limits(token_id)

        if side == OrderSide.BUY:
            if not limits.can_buy:
                return False, limits.buy_limit_reason

            # Check if this buy would exceed limits (including pending orders)
            position = self.get_position(token_id)
            pending = self.get_pending_buy_size(token_id)
            projected_size = position.size + pending + size

            if projected_size > self.config.max_position_per_market:
                return False, (
                    f"Buy would exceed position limit: "
                    f"{projected_size:.0f} (pos={position.size:.0f} + pending={pending:.0f} + order={size:.0f}) > {self.config.max_position_per_market}"
                )
        else:  # SELL
            if not limits.can_sell:
                return False, limits.sell_limit_reason

        return True, ""

    def get_adjusted_order_size(
        self,
        token_id: str,
        side: OrderSide,
        target_size: float
    ) -> float:
        """
        Get adjusted order size respecting limits.

        Args:
            token_id: Token ID
            side: Order side
            target_size: Desired order size

        Returns:
            Adjusted size (may be 0 if limits prevent ordering)
        """
        if side == OrderSide.SELL:
            # For sells, can only sell what we have
            position = self.get_position(token_id)
            return min(target_size, max(0, position.size))

        # For buys, respect position limits INCLUDING pending orders
        position = self.get_position(token_id)
        pending = self.get_pending_buy_size(token_id)
        effective_position = position.size + pending
        remaining_capacity = self.config.max_position_per_market - effective_position

        if remaining_capacity <= 0:
            if pending > 0:
                logger.debug(
                    f"Buy blocked for {token_id[:20]}...: "
                    f"position={position.size:.0f} + pending={pending:.0f} >= max={self.config.max_position_per_market}"
                )
            return 0

        return min(target_size, remaining_capacity)

    def reset_position(self, token_id: str) -> None:
        """
        Reset position for a token (for testing or reconciliation).

        Args:
            token_id: Token ID
        """
        if token_id in self._positions:
            del self._positions[token_id]

    def reset_all(self) -> None:
        """Reset all positions."""
        self._positions.clear()

    def set_position(
        self,
        token_id: str,
        size: float,
        avg_entry_price: float = 0.5
    ) -> None:
        """
        Set position directly (for reconciliation with external state).

        Args:
            token_id: Token ID
            size: Position size
            avg_entry_price: Average entry price
        """
        position = self.get_position(token_id)
        position.size = size
        position.avg_entry_price = avg_entry_price

    def get_summary(self) -> Dict[str, dict]:
        """
        Get summary of all positions for logging/monitoring.

        Returns:
            Dict mapping token_id to position summary
        """
        return {
            token_id: {
                "size": pos.size,
                "avg_entry": pos.avg_entry_price,
                "liability": pos.max_liability,
                "realized_pnl": pos.realized_pnl,
                "skew_factor": self.calculate_skew_factor(token_id),
            }
            for token_id, pos in self._positions.items()
            if pos.size != 0
        }
