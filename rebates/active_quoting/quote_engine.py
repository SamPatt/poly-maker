"""
QuoteEngine - Dynamic quote pricing for active two-sided quoting.

Implements:
- Quote at best_bid/best_ask (offset 0 by default)
- Improve by 1 tick only when spread >= IMPROVE_WHEN_SPREAD_TICKS
- Inventory skew based on position (skew = coefficient × inventory)
- Hysteresis: only refresh if quote is >= REFRESH_THRESHOLD_TICKS from target
- Clamping to prevent crossing spread
- Integration with InventoryManager for dynamic inventory tracking
"""
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional, Tuple, TYPE_CHECKING

from .config import ActiveQuotingConfig
from .models import OrderbookState, Quote, OrderSide, MomentumState

if TYPE_CHECKING:
    from .inventory_manager import InventoryManager

logger = logging.getLogger(__name__)


class QuoteAction(Enum):
    """Actions returned by the quote engine."""
    PLACE_QUOTE = "PLACE_QUOTE"  # Place new quotes
    KEEP_CURRENT = "KEEP_CURRENT"  # Keep current quotes (hysteresis)
    CANCEL_ALL = "CANCEL_ALL"  # Cancel all quotes (momentum, invalid book)


@dataclass
class QuoteDecision:
    """Decision from the quote engine."""
    action: QuoteAction
    quote: Optional[Quote] = None
    reason: str = ""


class QuoteEngine:
    """
    Calculates dynamic quotes based on orderbook state and inventory.

    The engine follows these principles:
    1. Quote at best bid/ask by default (offset 0)
    2. Only improve by 1 tick when spread is wide enough
    3. Skew quotes based on inventory to encourage rebalancing
    4. Apply hysteresis to avoid excessive quote refreshes
    5. Always respect price bounds (0 < price < 1)
    """

    def __init__(
        self,
        config: ActiveQuotingConfig,
        inventory_manager: Optional["InventoryManager"] = None,
    ):
        """
        Initialize the QuoteEngine.

        Args:
            config: Active quoting configuration
            inventory_manager: Optional InventoryManager for dynamic inventory tracking
        """
        self.config = config
        self._inventory_manager = inventory_manager

    @property
    def inventory_manager(self) -> Optional["InventoryManager"]:
        """Get the inventory manager if set."""
        return self._inventory_manager

    def set_inventory_manager(self, manager: "InventoryManager") -> None:
        """
        Set the inventory manager.

        Args:
            manager: InventoryManager instance
        """
        self._inventory_manager = manager

    def calculate_quote(
        self,
        orderbook: OrderbookState,
        inventory: float = 0.0,
        momentum_state: Optional[MomentumState] = None,
        current_quote: Optional[Quote] = None,
    ) -> QuoteDecision:
        """
        Calculate the target quote based on current market state.

        Args:
            orderbook: Current orderbook state
            inventory: Current inventory (positive = long, negative = short)
            momentum_state: Current momentum state (for cooldown check)
            current_quote: Currently active quote (for hysteresis check)

        Returns:
            QuoteDecision with action and optional new quote
        """
        # Check if in momentum cooldown
        if momentum_state and momentum_state.in_cooldown():
            return QuoteDecision(
                action=QuoteAction.CANCEL_ALL,
                reason="In momentum cooldown"
            )

        # Check if orderbook is valid
        if not orderbook.is_valid():
            return QuoteDecision(
                action=QuoteAction.CANCEL_ALL,
                reason="Invalid orderbook (empty or crossed)"
            )

        best_bid = orderbook.best_bid
        best_ask = orderbook.best_ask
        tick_size = orderbook.tick_size

        if best_bid is None or best_ask is None:
            return QuoteDecision(
                action=QuoteAction.CANCEL_ALL,
                reason="No best bid/ask available"
            )

        # Calculate target quote prices
        my_bid, my_ask = self._calculate_base_prices(
            best_bid, best_ask, tick_size
        )

        # Apply inventory skew
        my_bid, my_ask = self._apply_inventory_skew(
            my_bid, my_ask, tick_size, inventory
        )

        # Clamp to valid range
        my_bid, my_ask = self._clamp_prices(
            my_bid, my_ask, tick_size, best_bid, best_ask
        )

        # Create the target quote
        target_quote = Quote(
            token_id=orderbook.token_id,
            bid_price=my_bid,
            ask_price=my_ask,
            bid_size=self.config.order_size_usdc,
            ask_size=self.config.order_size_usdc,
        )

        # Check if we need to refresh (hysteresis)
        if current_quote is not None:
            if not self._needs_refresh(current_quote, target_quote, tick_size):
                return QuoteDecision(
                    action=QuoteAction.KEEP_CURRENT,
                    quote=current_quote,
                    reason="Quote within hysteresis threshold"
                )

        return QuoteDecision(
            action=QuoteAction.PLACE_QUOTE,
            quote=target_quote,
            reason="New quote calculated"
        )

    def _calculate_base_prices(
        self,
        best_bid: float,
        best_ask: float,
        tick_size: float,
    ) -> Tuple[float, float]:
        """
        Calculate base quote prices before skew.

        Default: Quote at best bid/ask.
        Improve by 1 tick only when spread is wide enough.

        Args:
            best_bid: Current best bid price
            best_ask: Current best ask price
            tick_size: Minimum price increment

        Returns:
            Tuple of (my_bid, my_ask)
        """
        spread_ticks = int(round((best_ask - best_bid) / tick_size))

        # Default: quote AT best bid/ask
        my_bid = best_bid
        my_ask = best_ask

        # Only improve if spread is wide enough
        if spread_ticks >= self.config.improve_when_spread_ticks:
            my_bid = best_bid + tick_size
            my_ask = best_ask - tick_size

        return my_bid, my_ask

    def _apply_inventory_skew(
        self,
        my_bid: float,
        my_ask: float,
        tick_size: float,
        inventory: float,
    ) -> Tuple[float, float]:
        """
        Apply inventory skew to encourage position rebalancing.

        Positive inventory (long) -> less aggressive buying, more aggressive selling
        Negative inventory (short) -> more aggressive buying, less aggressive selling

        Args:
            my_bid: Current bid price
            my_ask: Current ask price
            tick_size: Minimum price increment
            inventory: Current inventory

        Returns:
            Tuple of (skewed_bid, skewed_ask)
        """
        if self.config.inventory_skew_coefficient == 0 or inventory == 0:
            return my_bid, my_ask

        # Calculate skew in ticks (positive inventory = long, want to sell)
        # skew_ticks positive = lower bid (less aggressive buying)
        #                     = lower ask (more aggressive selling)
        skew_ticks = int(round(self.config.inventory_skew_coefficient * inventory))

        my_bid -= skew_ticks * tick_size
        my_ask -= skew_ticks * tick_size

        return my_bid, my_ask

    def _clamp_prices(
        self,
        my_bid: float,
        my_ask: float,
        tick_size: float,
        best_bid: float,
        best_ask: float,
    ) -> Tuple[float, float]:
        """
        Clamp prices to valid range and prevent crossing spread.

        Args:
            my_bid: Proposed bid price
            my_ask: Proposed ask price
            tick_size: Minimum price increment
            best_bid: Current best bid price
            best_ask: Current best ask price

        Returns:
            Tuple of (clamped_bid, clamped_ask)
        """
        # Ensure bid doesn't cross best ask (post-only will reject anyway, but be explicit)
        if my_bid >= best_ask:
            my_bid = best_ask - tick_size

        # Ensure ask doesn't cross best bid
        if my_ask <= best_bid:
            my_ask = best_bid + tick_size

        # Clamp to price bounds (0, 1) with at least one tick margin
        my_bid = max(my_bid, tick_size)
        my_ask = min(my_ask, 1.0 - tick_size)

        # Final safety: ensure bid < ask
        if my_bid >= my_ask:
            # Default to best bid/ask if our skew caused crossing
            my_bid = best_bid
            my_ask = best_ask

        return my_bid, my_ask

    def _needs_refresh(
        self,
        current_quote: Quote,
        target_quote: Quote,
        tick_size: float,
    ) -> bool:
        """
        Check if quote needs to be refreshed based on hysteresis threshold.

        Only refresh if the quote is >= REFRESH_THRESHOLD_TICKS from target.
        This prevents excessive cancel/replace which loses queue priority.

        Args:
            current_quote: Currently active quote
            target_quote: Newly calculated target quote
            tick_size: Minimum price increment

        Returns:
            True if refresh is needed
        """
        threshold = self.config.refresh_threshold_ticks * tick_size

        bid_diff = abs(current_quote.bid_price - target_quote.bid_price)
        ask_diff = abs(current_quote.ask_price - target_quote.ask_price)

        # Refresh if either side has moved significantly
        return bid_diff >= threshold or ask_diff >= threshold

    def calculate_quote_for_side(
        self,
        orderbook: OrderbookState,
        side: OrderSide,
        inventory: float = 0.0,
        momentum_state: Optional[MomentumState] = None,
    ) -> Tuple[Optional[float], str]:
        """
        Calculate the target price for a single side.

        Useful when only placing orders on one side due to position limits.

        Args:
            orderbook: Current orderbook state
            side: Which side to calculate (BUY or SELL)
            inventory: Current inventory
            momentum_state: Current momentum state

        Returns:
            Tuple of (price, reason) - price is None if shouldn't quote
        """
        # Check momentum
        if momentum_state and momentum_state.in_cooldown():
            return None, "In momentum cooldown"

        # Check orderbook
        if not orderbook.is_valid():
            return None, "Invalid orderbook"

        best_bid = orderbook.best_bid
        best_ask = orderbook.best_ask
        tick_size = orderbook.tick_size

        if best_bid is None or best_ask is None:
            return None, "No best bid/ask"

        # Calculate base price for this side
        my_bid, my_ask = self._calculate_base_prices(best_bid, best_ask, tick_size)

        # Apply inventory skew
        my_bid, my_ask = self._apply_inventory_skew(my_bid, my_ask, tick_size, inventory)

        # Clamp
        my_bid, my_ask = self._clamp_prices(my_bid, my_ask, tick_size, best_bid, best_ask)

        if side == OrderSide.BUY:
            return my_bid, "Buy quote calculated"
        else:
            return my_ask, "Sell quote calculated"

    def get_spread_ticks(self, orderbook: OrderbookState) -> Optional[int]:
        """
        Get the current spread in ticks.

        Args:
            orderbook: Current orderbook state

        Returns:
            Spread in ticks, or None if invalid orderbook
        """
        return orderbook.spread_ticks()

    def is_spread_wide_enough(self, orderbook: OrderbookState) -> bool:
        """
        Check if spread is wide enough to improve quotes.

        Args:
            orderbook: Current orderbook state

        Returns:
            True if spread >= improve_when_spread_ticks
        """
        spread_ticks = self.get_spread_ticks(orderbook)
        if spread_ticks is None:
            return False
        return spread_ticks >= self.config.improve_when_spread_ticks

    def calculate_quote_with_manager(
        self,
        orderbook: OrderbookState,
        momentum_state: Optional[MomentumState] = None,
        current_quote: Optional[Quote] = None,
    ) -> QuoteDecision:
        """
        Calculate quote using the integrated InventoryManager.

        This is a convenience method that automatically looks up inventory
        from the InventoryManager based on the orderbook's token_id.

        Args:
            orderbook: Current orderbook state
            momentum_state: Current momentum state (for cooldown check)
            current_quote: Currently active quote (for hysteresis check)

        Returns:
            QuoteDecision with action and optional new quote

        Raises:
            ValueError: If no InventoryManager is set
        """
        if self._inventory_manager is None:
            raise ValueError("InventoryManager not set. Use set_inventory_manager() first.")

        inventory = self._inventory_manager.get_inventory(orderbook.token_id)
        return self.calculate_quote(
            orderbook=orderbook,
            inventory=inventory,
            momentum_state=momentum_state,
            current_quote=current_quote,
        )

    def calculate_quote_for_side_with_manager(
        self,
        orderbook: OrderbookState,
        side: OrderSide,
        momentum_state: Optional[MomentumState] = None,
    ) -> Tuple[Optional[float], str]:
        """
        Calculate single-side quote using the integrated InventoryManager.

        This is a convenience method that automatically looks up inventory
        and checks position limits.

        Args:
            orderbook: Current orderbook state
            side: Which side to calculate (BUY or SELL)
            momentum_state: Current momentum state

        Returns:
            Tuple of (price, reason) - price is None if shouldn't quote

        Raises:
            ValueError: If no InventoryManager is set
        """
        if self._inventory_manager is None:
            raise ValueError("InventoryManager not set. Use set_inventory_manager() first.")

        inventory = self._inventory_manager.get_inventory(orderbook.token_id)

        # Check if we can place order on this side
        limits = self._inventory_manager.check_limits(orderbook.token_id)
        if side == OrderSide.BUY and not limits.can_buy:
            return None, limits.buy_limit_reason
        if side == OrderSide.SELL and not limits.can_sell:
            return None, limits.sell_limit_reason

        return self.calculate_quote_for_side(
            orderbook=orderbook,
            side=side,
            inventory=inventory,
            momentum_state=momentum_state,
        )

    def get_inventory_adjusted_sizes(
        self,
        token_id: str,
        base_size: float,
    ) -> Tuple[float, float]:
        """
        Get buy and sell sizes adjusted for position limits.

        Uses the InventoryManager to determine how much can be
        bought or sold given current position.

        Args:
            token_id: Token ID
            base_size: Base order size

        Returns:
            Tuple of (buy_size, sell_size)

        Raises:
            ValueError: If no InventoryManager is set
        """
        if self._inventory_manager is None:
            raise ValueError("InventoryManager not set. Use set_inventory_manager() first.")

        buy_size = self._inventory_manager.get_adjusted_order_size(
            token_id, OrderSide.BUY, base_size
        )
        sell_size = self._inventory_manager.get_adjusted_order_size(
            token_id, OrderSide.SELL, base_size
        )

        return buy_size, sell_size
