"""
InventoryManager - Position tracking and skewing for active quoting.

Handles:
- Position tracking per market with dual tracking (confirmed + pending fills)
- Liability calculation (worst-case loss = shares x entry_price)
- Skew factor calculation based on position
- Hard position limits enforcement (MAX_POSITION_PER_MARKET, MAX_LIABILITY_PER_MARKET_USDC)

Phase 2: Dual Tracking Architecture
- confirmed_size: Position from last API sync (authoritative)
- pending_fills: WebSocket fills not yet confirmed by API (tracked by trade_id)
- effective_size: confirmed_size + pending_fill_buys - pending_fill_sells

Phase 3: Conservative Buy Limit Checks
- Buy limits use conservative_exposure = confirmed + pending_fill_buys + pending_order_buys
- This prevents exceeding position limits even with rapid WS fills before API sync
- Sell availability still uses effective_size (confirmed + pending_buys - pending_sells)
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Optional

from .config import ActiveQuotingConfig
from .models import Fill, Position, OrderSide

logger = logging.getLogger(__name__)

# DEPRECATED: Fill protection has been removed. API is now trusted as source of truth.
# The old approach blocked API sync for 60s after fills, causing position drift.
# See docs/INVENTORY_TRACKING_PLAN.md for details.
FILL_PROTECTION_SECONDS = 0.0  # Effectively disabled

# Pending fill age-out threshold
PENDING_FILL_AGE_OUT_SECONDS = 30.0

# Hard max age for pending fills - after this, force age-out even BUY fills
# to prevent memory growth and permanent buy blocking if API never confirms
PENDING_FILL_MAX_AGE_SECONDS = 300.0  # 5 minutes


def _sign(x: float) -> int:
    """Return sign of x: 1 for positive, -1 for negative, 0 for zero."""
    if x > 0:
        return 1
    elif x < 0:
        return -1
    return 0


@dataclass
class PendingFill:
    """A fill received via WebSocket that hasn't been confirmed by API yet."""
    trade_id: str  # Primary key for reconciliation
    side: OrderSide
    size: float
    price: float
    timestamp: datetime

    @property
    def delta(self) -> float:
        """Position delta from this fill (positive for BUY, negative for SELL)."""
        return self.size if self.side == OrderSide.BUY else -self.size


@dataclass
class TrackedPosition:
    """
    Position with dual tracking: confirmed (API) and pending (WebSocket) fills.

    The effective_size property combines both to give the best estimate of
    actual position, while confirmed_size is the last known API snapshot.
    """
    token_id: str
    confirmed_size: float = 0.0
    confirmed_avg_price: float = 0.0  # Avg entry price from API
    confirmed_at: Optional[datetime] = None
    pending_fills: Dict[str, PendingFill] = field(default_factory=dict)
    # Legacy Position fields for compatibility
    realized_pnl: float = 0.0
    total_fees_paid: float = 0.0

    @property
    def pending_fill_buys(self) -> float:
        """Sum of pending BUY fill sizes."""
        return sum(
            f.size for f in self.pending_fills.values()
            if f.side == OrderSide.BUY
        )

    @property
    def pending_fill_sells(self) -> float:
        """Sum of pending SELL fill sizes."""
        return sum(
            f.size for f in self.pending_fills.values()
            if f.side == OrderSide.SELL
        )

    @property
    def pending_delta(self) -> float:
        """Net position delta from pending fills."""
        return self.pending_fill_buys - self.pending_fill_sells

    @property
    def effective_size(self) -> float:
        """
        Best estimate of actual position size.

        Combines confirmed API position with pending WebSocket fills.
        """
        return self.confirmed_size + self.pending_fill_buys - self.pending_fill_sells

    @property
    def size(self) -> float:
        """Alias for effective_size for backwards compatibility."""
        return self.effective_size

    @property
    def avg_entry_price(self) -> float:
        """
        Approximate average entry price.

        Uses confirmed avg price as base. A more accurate calculation would
        weight by size, but for risk/skew purposes this is sufficient.
        """
        if self.effective_size <= 0:
            return 0.0
        # Use confirmed price, or calculate weighted avg if we have pending buys
        if not self.pending_fills:
            return self.confirmed_avg_price

        # Weighted average of confirmed + pending buys
        total_cost = self.confirmed_size * self.confirmed_avg_price
        total_size = self.confirmed_size
        for f in self.pending_fills.values():
            if f.side == OrderSide.BUY:
                total_cost += f.size * f.price
                total_size += f.size

        return total_cost / total_size if total_size > 0 else self.confirmed_avg_price

    @property
    def max_liability(self) -> float:
        """Maximum liability using effective size."""
        return abs(self.effective_size) * self.avg_entry_price


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
    1. Tracks positions per token using dual tracking (confirmed + pending)
    2. Calculates worst-case liability (max loss if position goes to 0)
    3. Computes skew factor to encourage position rebalancing
    4. Enforces hard position limits to control risk

    Phase 2: Dual Tracking Architecture
    - confirmed_size: Position from last API sync (authoritative)
    - pending_fills: WebSocket fills not yet confirmed by API
    - effective_size: Combined view for quoting decisions
    """

    def __init__(self, config: ActiveQuotingConfig):
        """
        Initialize the InventoryManager.

        Args:
            config: Active quoting configuration
        """
        self.config = config
        self._positions: Dict[str, TrackedPosition] = {}  # token_id -> TrackedPosition
        self._pending_buys: Dict[str, float] = {}  # token_id -> pending buy ORDER size (open orders)
        self._last_fill_time: Dict[str, datetime] = {}  # token_id -> last fill timestamp

    @property
    def positions(self) -> Dict[str, TrackedPosition]:
        """Get all positions."""
        return self._positions

    def get_position(self, token_id: str) -> TrackedPosition:
        """
        Get position for a token, creating if not exists.

        Args:
            token_id: Token ID

        Returns:
            TrackedPosition for the token (use .effective_size for best estimate)
        """
        if token_id not in self._positions:
            self._positions[token_id] = TrackedPosition(token_id=token_id)
        return self._positions[token_id]

    def get_inventory(self, token_id: str) -> float:
        """
        Get current inventory (effective position size) for a token.

        Args:
            token_id: Token ID

        Returns:
            Inventory size (positive = long) - uses effective_size
        """
        return self.get_position(token_id).effective_size

    def _synthesize_trade_id(self, fill: Fill) -> str:
        """
        Synthesize a fallback trade_id when one is missing.

        Uses order_id + timestamp to create a unique key that won't collide
        with other fills from the same order.

        Args:
            fill: Fill that may be missing trade_id

        Returns:
            Synthesized trade_id string
        """
        if fill.trade_id:
            return fill.trade_id
        # Synthesize: order_id_timestamp_size (size helps differentiate partials)
        ts_ms = int(fill.timestamp.timestamp() * 1000)
        return f"{fill.order_id}_{ts_ms}_{fill.size:.2f}"

    def update_from_fill(self, fill: Fill) -> None:
        """
        Update position based on a fill event from WebSocket.

        Phase 2: Adds fill to pending_fills rather than updating confirmed_size.
        The confirmed_size is only updated via API sync (set_position).

        Args:
            fill: Fill from UserChannelManager
        """
        position = self.get_position(fill.token_id)
        old_effective = position.effective_size

        # Synthesize trade_id if missing
        trade_id = self._synthesize_trade_id(fill)
        if not fill.trade_id:
            logger.debug(
                f"Fill missing trade_id, synthesized: {trade_id} "
                f"(order={fill.order_id[:12]}...)"
            )

        # Add to pending fills (don't update confirmed_size)
        pending_fill = PendingFill(
            trade_id=trade_id,
            side=fill.side,
            size=fill.size,
            price=fill.price,
            timestamp=fill.timestamp,
        )
        position.pending_fills[trade_id] = pending_fill

        # Track fees and realized PnL (these are immediate, not pending)
        position.total_fees_paid += fill.fee

        # For sells, calculate realized PnL against confirmed avg price
        if fill.side == OrderSide.SELL and position.confirmed_size > 0:
            pnl_per_share = fill.price - position.confirmed_avg_price
            shares_sold = min(fill.size, position.confirmed_size)
            position.realized_pnl += pnl_per_share * shares_sold

        # Record fill time
        self._last_fill_time[fill.token_id] = datetime.utcnow()

        logger.info(
            f"Position updated (pending): {fill.token_id[:20]}... "
            f"{old_effective:.2f} -> {position.effective_size:.2f} "
            f"(fill: {fill.side.value} {fill.size:.2f} @ {fill.price:.4f}, "
            f"trade_id={trade_id[:16]}..., pending_count={len(position.pending_fills)})"
        )

        # Release pending buy ORDER capacity when fill comes in
        if fill.side == OrderSide.BUY:
            self.release_pending_buy(fill.token_id, fill.size)

    def has_recent_fill(self, token_id: str, seconds: float = FILL_PROTECTION_SECONDS) -> bool:
        """
        DEPRECATED: Always returns False now.

        Fill protection has been removed because it caused position drift.
        API is now trusted as source of truth, with pending fills tracked separately.
        See docs/INVENTORY_TRACKING_PLAN.md for the new architecture.

        Args:
            token_id: Token ID
            seconds: Time window (ignored)

        Returns:
            Always False (fill protection disabled)
        """
        # DEPRECATED: Fill protection disabled to prevent position drift
        return False

    def get_last_fill_time(self, token_id: str) -> Optional[datetime]:
        """Get the last fill time for a token."""
        return self._last_fill_time.get(token_id)

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
        Uses confirmed_size (not conservative exposure) because liability is about
        worst-case loss on confirmed positions, not pending fills.

        Args:
            token_id: Token ID

        Returns:
            Maximum liability in USDC
        """
        position = self.get_position(token_id)
        return abs(position.confirmed_size) * position.confirmed_avg_price

    def calculate_total_liability(self) -> float:
        """
        Calculate total liability across all positions.

        Uses confirmed_size (not conservative exposure) because liability is about
        worst-case loss on confirmed positions, not pending fills.

        Returns:
            Total maximum liability in USDC
        """
        return sum(
            abs(pos.confirmed_size) * pos.confirmed_avg_price
            for pos in self._positions.values()
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
        - MAX_POSITION_PER_MARKET: Share count limit (using conservative exposure)
        - MAX_LIABILITY_PER_MARKET_USDC: Worst-case loss limit
        - MAX_TOTAL_LIABILITY_USDC: Total exposure limit

        Phase 3: Uses conservative exposure formula for buy limits:
        conservative_exposure = confirmed + pending_fill_buys + pending_order_buys

        Args:
            token_id: Token ID

        Returns:
            InventoryLimits indicating what actions are allowed
        """
        limits = InventoryLimits()
        position = self.get_position(token_id)
        pending_orders = self.get_pending_buy_size(token_id)

        # Phase 3: Conservative exposure for buy limit checks
        # Counts ALL potential buy-side exposure (worst case: all buys settle, no sells settle)
        confirmed = position.confirmed_size
        pending_fill_buys = position.pending_fill_buys
        conservative_exposure = confirmed + pending_fill_buys + pending_orders

        # Always log all three components for debugging
        logger.debug(
            f"Buy limit check: confirmed={confirmed:.0f} + pending_fills={pending_fill_buys:.0f} + "
            f"pending_orders={pending_orders:.0f} = {conservative_exposure:.0f} "
            f"(max={self.config.max_position_per_market})"
        )

        # Check position size limit (using conservative exposure)
        if conservative_exposure >= self.config.max_position_per_market:
            limits.can_buy = False
            limits.buy_limit_reason = (
                f"Position {confirmed:.0f} + pending_fills {pending_fill_buys:.0f} + "
                f"pending_orders {pending_orders:.0f} >= max {self.config.max_position_per_market}"
            )

        # Check liability per market (uses confirmed_size)
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

        # Selling is allowed if we have effective position (confirmed + pending buys)
        # This allows quick exits after WS fills before API sync
        effective = position.effective_size
        if effective <= 0:
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

            # Phase 3: Check if this buy would exceed limits using conservative exposure
            position = self.get_position(token_id)
            pending_orders = self.get_pending_buy_size(token_id)
            confirmed = position.confirmed_size
            pending_fill_buys = position.pending_fill_buys
            projected_size = confirmed + pending_fill_buys + pending_orders + size

            if projected_size > self.config.max_position_per_market:
                return False, (
                    f"Buy would exceed position limit: "
                    f"{projected_size:.0f} (confirmed={confirmed:.0f} + pending_fills={pending_fill_buys:.0f} + "
                    f"pending_orders={pending_orders:.0f} + order={size:.0f}) > {self.config.max_position_per_market}"
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

        Phase 3: Uses conservative exposure formula for buy limits:
        conservative_exposure = confirmed + pending_fill_buys + pending_order_buys

        Args:
            token_id: Token ID
            side: Order side
            target_size: Desired order size

        Returns:
            Adjusted size (may be 0 if limits prevent ordering)
        """
        position = self.get_position(token_id)

        if side == OrderSide.SELL:
            # For sells, can sell up to effective_size (confirmed + pending buys)
            # This allows quick exits after WS fills before API sync
            return min(target_size, max(0, position.effective_size))

        # Phase 3: For buys, respect position limits using conservative exposure
        confirmed = position.confirmed_size
        pending_fill_buys = position.pending_fill_buys
        pending_orders = self.get_pending_buy_size(token_id)
        conservative_exposure = confirmed + pending_fill_buys + pending_orders
        remaining_capacity = self.config.max_position_per_market - conservative_exposure

        if remaining_capacity <= 0:
            if pending_fill_buys > 0 or pending_orders > 0:
                logger.debug(
                    f"Buy blocked for {token_id[:20]}...: "
                    f"confirmed={confirmed:.0f} + pending_fills={pending_fill_buys:.0f} + "
                    f"pending_orders={pending_orders:.0f} >= max={self.config.max_position_per_market}"
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

    def clear_position(self, token_id: str) -> None:
        """
        Clear position size for a token after redemption.

        Unlike reset_position, this preserves realized PnL for tracking.

        Args:
            token_id: Token ID
        """
        if token_id in self._positions:
            position = self._positions[token_id]
            position.confirmed_size = 0.0
            position.confirmed_avg_price = 0.0
            position.pending_fills.clear()
            # Keep realized_pnl and total_fees_paid for session tracking

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
        Set confirmed position from API sync.

        Phase 2: Updates confirmed_size and reconciles pending fills.
        Pending fills that have been absorbed by the API position change
        are removed (oldest first).

        Args:
            token_id: Token ID
            size: Position size from API
            avg_entry_price: Average entry price from API
        """
        position = self.get_position(token_id)
        old_confirmed = position.confirmed_size

        # Calculate how much the API position absorbed
        absorbed = size - old_confirmed

        # Update confirmed position
        position.confirmed_size = size
        position.confirmed_avg_price = avg_entry_price
        position.confirmed_at = datetime.utcnow()

        # Reconcile pending fills against API change
        self._reconcile_pending_fills(position, absorbed)

        # Age out old pending fills
        self._age_out_pending_fills(position)

        logger.debug(
            f"Position confirmed from API: {token_id[:20]}... "
            f"confirmed={size:.2f}, pending_delta={position.pending_delta:+.2f}, "
            f"effective={position.effective_size:.2f}"
        )

    def _reconcile_pending_fills(self, position: TrackedPosition, absorbed: float) -> None:
        """
        Remove or reduce pending fills that have been absorbed by API, oldest first.

        When the API position changes, we assume the oldest pending fills
        are the ones that got absorbed. Fills are fully removed if completely
        absorbed, or reduced in size if partially absorbed.

        Args:
            position: TrackedPosition to reconcile
            absorbed: Change in confirmed_size from API (positive = buys absorbed)
        """
        if abs(absorbed) < 0.01:
            return  # No significant change

        # Sort pending fills by timestamp (oldest first)
        fills_by_age = sorted(
            position.pending_fills.values(),
            key=lambda f: f.timestamp
        )

        remaining_to_absorb = absorbed
        fills_to_remove = []
        fills_to_reduce = []  # (trade_id, new_size)

        for fill in fills_by_age:
            if abs(remaining_to_absorb) < 0.01:
                break  # Nothing left to absorb

            fill_delta = fill.size if fill.side == OrderSide.BUY else -fill.size

            # Only absorb fills that match the direction of API change
            if _sign(fill_delta) == _sign(remaining_to_absorb):
                if abs(fill_delta) <= abs(remaining_to_absorb):
                    # Fully absorbed - mark for removal
                    fills_to_remove.append(fill.trade_id)
                    remaining_to_absorb -= fill_delta
                    logger.debug(
                        f"Pending fill fully absorbed: {fill.trade_id[:16]}... "
                        f"({fill.side.value} {fill.size:.2f})"
                    )
                else:
                    # Partially absorbed - reduce fill size
                    absorbed_size = abs(remaining_to_absorb)
                    new_size = fill.size - absorbed_size
                    fills_to_reduce.append((fill.trade_id, new_size))
                    logger.debug(
                        f"Pending fill partially absorbed: {fill.trade_id[:16]}... "
                        f"({fill.side.value} {fill.size:.2f} -> {new_size:.2f})"
                    )
                    remaining_to_absorb = 0.0

        # Remove fully absorbed fills
        for trade_id in fills_to_remove:
            del position.pending_fills[trade_id]

        # Reduce partially absorbed fills
        for trade_id, new_size in fills_to_reduce:
            position.pending_fills[trade_id].size = new_size

        if fills_to_remove or fills_to_reduce:
            logger.info(
                f"Reconciled pending fills for {position.token_id[:20]}...: "
                f"removed={len(fills_to_remove)}, reduced={len(fills_to_reduce)}, "
                f"absorbed={absorbed:+.2f}, remaining_pending={len(position.pending_fills)}"
            )

    def _age_out_pending_fills(self, position: TrackedPosition) -> None:
        """
        Remove pending fills older than the age-out threshold.

        Fills that weren't absorbed by API after 30s are likely:
        - Duplicate/stale WebSocket messages
        - Fills for a different session
        - WebSocket/API synchronization issues

        IMPORTANT: BUY fills are handled differently to prevent position limit bypass:
        - SELL fills: Aged out after 30s (conservative for limits)
        - BUY fills: Preserved until 5 min hard cap (prevents limit bypass)

        The hard cap prevents memory growth and permanent buy blocking if
        the API never confirms the fills.

        Args:
            position: TrackedPosition to age out
        """
        now = datetime.utcnow()
        normal_cutoff = now - timedelta(seconds=PENDING_FILL_AGE_OUT_SECONDS)
        hard_cutoff = now - timedelta(seconds=PENDING_FILL_MAX_AGE_SECONDS)

        # Separate old fills by type
        old_sells = {
            trade_id: fill
            for trade_id, fill in position.pending_fills.items()
            if fill.timestamp < normal_cutoff and fill.side == OrderSide.SELL
        }

        old_buys_normal = {
            trade_id: fill
            for trade_id, fill in position.pending_fills.items()
            if fill.timestamp < normal_cutoff and fill.side == OrderSide.BUY
        }

        old_buys_hard = {
            trade_id: fill
            for trade_id, fill in position.pending_fills.items()
            if fill.timestamp < hard_cutoff and fill.side == OrderSide.BUY
        }

        fills_to_remove = []

        # Always age out old SELL fills (conservative for limits)
        if old_sells:
            sell_delta = sum(f.size for f in old_sells.values())
            fills_to_remove.extend(old_sells.keys())
            logger.warning(
                f"Aging out SELL fills for {position.token_id[:20]}...: "
                f"count={len(old_sells)}, size={sell_delta:.2f}, "
                f"trade_ids={list(old_sells.keys())}"
            )

        # Preserve BUY fills until hard cap (prevents limit bypass)
        if old_buys_normal and not old_buys_hard:
            buy_delta = sum(f.size for f in old_buys_normal.values())
            logger.info(
                f"Preserving BUY fills for {position.token_id[:20]}...: "
                f"count={len(old_buys_normal)}, size={buy_delta:.2f}, "
                f"waiting for API to confirm (max {PENDING_FILL_MAX_AGE_SECONDS:.0f}s)"
            )

        # Force age out BUY fills that exceed hard cap
        if old_buys_hard:
            buy_delta = sum(f.size for f in old_buys_hard.values())
            fills_to_remove.extend(old_buys_hard.keys())
            logger.warning(
                f"FORCE aging out BUY fills for {position.token_id[:20]}...: "
                f"count={len(old_buys_hard)}, size={buy_delta:.2f}, "
                f"exceeded {PENDING_FILL_MAX_AGE_SECONDS:.0f}s hard cap. "
                f"API may have issues. trade_ids={list(old_buys_hard.keys())}"
            )

        # Remove the fills
        for trade_id in fills_to_remove:
            del position.pending_fills[trade_id]

    def force_reconcile(self, token_id: str) -> None:
        """
        Force reconciliation by trusting API and clearing all pending fills.

        Called on WebSocket reconnect or when gaps are detected.
        Logs any discrepancy before clearing.

        Args:
            token_id: Token ID to force reconcile
        """
        position = self.get_position(token_id)

        if not position.pending_fills:
            return

        # Log the discrepancy
        pending_delta = position.pending_delta
        trade_ids = list(position.pending_fills.keys())

        logger.warning(
            f"Force reconcile for {token_id[:20]}...: "
            f"clearing {len(trade_ids)} pending fills, "
            f"discarding delta={pending_delta:+.2f}, "
            f"trade_ids={trade_ids}"
        )

        # Clear all pending fills - trust API
        position.pending_fills.clear()

    def force_reconcile_all(self) -> None:
        """
        Force reconcile all positions.

        Called on WebSocket disconnect/reconnect or major gaps.
        """
        for token_id in self._positions:
            self.force_reconcile(token_id)

    def get_summary(self) -> Dict[str, dict]:
        """
        Get summary of all positions for logging/monitoring.

        Returns:
            Dict mapping token_id to position summary
        """
        return {
            token_id: {
                "size": pos.effective_size,
                "confirmed_size": pos.confirmed_size,
                "pending_delta": pos.pending_delta,
                "pending_fills_count": len(pos.pending_fills),
                "avg_entry": pos.avg_entry_price,
                "liability": pos.max_liability,
                "realized_pnl": pos.realized_pnl,
                "skew_factor": self.calculate_skew_factor(token_id),
            }
            for token_id, pos in self._positions.items()
            if pos.effective_size != 0 or len(pos.pending_fills) > 0
        }
