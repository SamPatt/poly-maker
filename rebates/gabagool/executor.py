"""
Gabagool Executor - Order execution for arbitrage opportunities.

Handles the critical task of placing paired YES+NO orders simultaneously
while managing partial fills and position imbalances.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Tuple
from enum import Enum

from .scanner import Opportunity
from .circuit_breaker import CircuitBreaker
from . import config

logger = logging.getLogger(__name__)


class ExecutionStrategy(Enum):
    """Order execution strategy."""
    TAKER = "taker"      # Aggressive - crosses spread, pays fees
    MAKER = "maker"      # Passive - posts limit orders, no fees
    HYBRID = "hybrid"    # Start maker, escalate to taker if needed


@dataclass
class ExecutionResult:
    """Result of an execution attempt."""
    success: bool
    reason: str = ""

    # Fill details
    up_filled: float = 0.0
    down_filled: float = 0.0
    up_price: float = 0.0
    down_price: float = 0.0

    # Cost and profit
    total_cost: float = 0.0
    expected_profit: float = 0.0

    # Order IDs for tracking
    up_order_id: str = ""
    down_order_id: str = ""


@dataclass
class TrackedPosition:
    """
    Track a Gabagool arbitrage position from entry to profit realization.

    A position is "balanced" when we hold equal amounts of YES and NO tokens,
    which guarantees profit at settlement regardless of outcome.
    """
    # Identification
    id: str
    market_slug: str
    condition_id: str

    # Tokens
    up_token: str
    down_token: str
    neg_risk: bool

    # Entry details
    entry_time: datetime
    up_entry_price: float
    down_entry_price: float
    combined_cost: float
    target_size: float

    # Fill tracking
    up_filled: float = 0.0
    down_filled: float = 0.0
    up_order_id: str = ""
    down_order_id: str = ""

    # Status
    is_balanced: bool = False
    is_closed: bool = False
    close_time: Optional[datetime] = None
    realized_profit: float = 0.0

    @property
    def min_filled(self) -> float:
        """Minimum of the two sides - this is the balanced/profitable amount."""
        return min(self.up_filled, self.down_filled)

    @property
    def imbalance(self) -> float:
        """Difference between sides - exposed to directional risk."""
        return abs(self.up_filled - self.down_filled)

    @property
    def imbalance_side(self) -> Optional[str]:
        """Which side has excess (needs to be sold or balanced)."""
        if self.up_filled > self.down_filled:
            return "up"
        elif self.down_filled > self.up_filled:
            return "down"
        return None


class GabagoolExecutor:
    """
    Executes Gabagool arbitrage trades.

    Handles order placement, fill monitoring, and position management.
    The critical challenge is ensuring both sides fill equally to avoid
    directional exposure.

    Supports three execution strategies:
    - TAKER: Fast, guaranteed fills, pays taker fees
    - MAKER: No fees, but may not fill
    - HYBRID: Start maker, escalate to taker if needed (recommended)
    """

    def __init__(
        self,
        client=None,
        circuit_breaker: CircuitBreaker = None,
        strategy: ExecutionStrategy = ExecutionStrategy.HYBRID,
        dry_run: bool = None,
    ):
        """
        Initialize the executor.

        Args:
            client: PolymarketClient instance for order placement
            circuit_breaker: Risk management circuit breaker
            strategy: Default execution strategy
            dry_run: If True, simulate orders without executing
        """
        self.client = client
        self.circuit_breaker = circuit_breaker
        self.strategy = strategy
        self.dry_run = dry_run if dry_run is not None else config.DRY_RUN

        # Position tracking
        self.active_positions: List[TrackedPosition] = []
        self.completed_positions: List[TrackedPosition] = []

        # Statistics
        self.executions_attempted = 0
        self.executions_successful = 0
        self.total_profit = 0.0

    async def execute(
        self,
        opportunity: Opportunity,
        size: float = None,
        strategy: ExecutionStrategy = None,
    ) -> ExecutionResult:
        """
        Execute a Gabagool arbitrage opportunity.

        Args:
            opportunity: Detected opportunity to execute
            size: Position size (defaults to config.TRADE_SIZE)
            strategy: Execution strategy (defaults to self.strategy)

        Returns:
            ExecutionResult with fill details
        """
        if size is None:
            size = config.TRADE_SIZE
        if strategy is None:
            strategy = self.strategy

        self.executions_attempted += 1

        # Check circuit breaker
        if self.circuit_breaker:
            can_trade, reason = await self.circuit_breaker.check_can_trade(
                opportunity.market_slug, size
            )
            if not can_trade:
                logger.warning(f"Circuit breaker blocked execution: {reason}")
                return ExecutionResult(success=False, reason=f"Circuit breaker: {reason}")

        # Log the execution attempt
        logger.info(
            f"Executing Gabagool on {opportunity.market_slug}: "
            f"UP={opportunity.up_price:.3f} DOWN={opportunity.down_price:.3f} "
            f"Combined={opportunity.combined_cost:.4f} Size={size}"
        )

        if self.dry_run:
            return await self._execute_dry_run(opportunity, size)

        # Choose execution strategy
        if strategy == ExecutionStrategy.TAKER:
            result = await self._execute_taker(opportunity, size)
        elif strategy == ExecutionStrategy.MAKER:
            result = await self._execute_maker(opportunity, size)
        else:
            result = await self._execute_hybrid(opportunity, size)

        # Record result with circuit breaker
        if self.circuit_breaker and result.success:
            await self.circuit_breaker.record_trade_result(
                market_id=opportunity.market_slug,
                size=result.up_filled + result.down_filled,
                pnl=result.expected_profit,
                success=True,
            )
            self.executions_successful += 1

        return result

    async def _execute_dry_run(
        self,
        opportunity: Opportunity,
        size: float,
    ) -> ExecutionResult:
        """Simulate execution without placing real orders."""
        logger.info(f"[DRY RUN] Would execute {size} shares on {opportunity.market_slug}")
        logger.info(
            f"[DRY RUN] BUY UP @ {opportunity.up_price:.3f}, "
            f"BUY DOWN @ {opportunity.down_price:.3f}"
        )

        expected_profit = size * (1.0 - opportunity.combined_cost) - config.GAS_COST_USD

        logger.info(
            f"[DRY RUN] Expected profit: ${expected_profit:.2f} "
            f"({opportunity.gross_profit_pct:.2f}%)"
        )

        return ExecutionResult(
            success=True,
            reason="Dry run - no orders placed",
            up_filled=size,
            down_filled=size,
            up_price=opportunity.up_price,
            down_price=opportunity.down_price,
            total_cost=size * opportunity.combined_cost,
            expected_profit=expected_profit,
        )

    async def _execute_taker(
        self,
        opportunity: Opportunity,
        size: float,
    ) -> ExecutionResult:
        """
        Execute using taker orders for guaranteed immediate fill.

        Crosses the spread to immediately fill. Pays taker fees but
        guarantees execution.
        """
        logger.info(f"Executing TAKER strategy on {opportunity.market_slug}")

        # Place both orders simultaneously
        up_task = self._place_order(
            opportunity.up_token,
            "BUY",
            opportunity.up_price,
            size,
            opportunity.neg_risk,
            post_only=False,  # Allow taking
        )
        down_task = self._place_order(
            opportunity.down_token,
            "BUY",
            opportunity.down_price,
            size,
            opportunity.neg_risk,
            post_only=False,
        )

        # Wait for both with timeout
        try:
            up_result, down_result = await asyncio.wait_for(
                asyncio.gather(up_task, down_task, return_exceptions=True),
                timeout=10.0
            )
        except asyncio.TimeoutError:
            logger.error("Taker execution timed out")
            await self._cancel_orders(opportunity.up_token, opportunity.down_token)
            return ExecutionResult(success=False, reason="Execution timeout")

        # Check for exceptions
        if isinstance(up_result, Exception):
            logger.error(f"UP order failed: {up_result}")
            return ExecutionResult(success=False, reason=f"UP order error: {up_result}")
        if isinstance(down_result, Exception):
            logger.error(f"DOWN order failed: {down_result}")
            return ExecutionResult(success=False, reason=f"DOWN order error: {down_result}")

        # Process results
        return self._process_execution_results(
            opportunity, size, up_result, down_result
        )

    async def _execute_maker(
        self,
        opportunity: Opportunity,
        size: float,
        timeout: float = 30.0,
    ) -> ExecutionResult:
        """
        Execute using maker orders for zero taker fee.

        Posts limit orders that don't immediately cross. Earns maker
        rebates but may not fill if market moves.
        """
        logger.info(f"Executing MAKER strategy on {opportunity.market_slug}")

        # Place post-only orders at slightly better prices
        # (1 tick below the ask to be maker, not taker)
        tick_size = 0.01
        up_maker_price = opportunity.up_price - tick_size
        down_maker_price = opportunity.down_price - tick_size

        # Verify combined maker price still profitable
        combined_maker = up_maker_price + down_maker_price
        if combined_maker >= config.PROFIT_THRESHOLD:
            return ExecutionResult(
                success=False,
                reason=f"Maker prices not profitable: {combined_maker:.4f}"
            )

        up_result = await self._place_order(
            opportunity.up_token,
            "BUY",
            up_maker_price,
            size,
            opportunity.neg_risk,
            post_only=True,
        )
        down_result = await self._place_order(
            opportunity.down_token,
            "BUY",
            down_maker_price,
            size,
            opportunity.neg_risk,
            post_only=True,
        )

        # Check if orders were placed
        up_order_id = up_result.get("orderID") if isinstance(up_result, dict) else None
        down_order_id = down_result.get("orderID") if isinstance(down_result, dict) else None

        if not up_order_id or not down_order_id:
            logger.warning("Failed to place maker orders")
            await self._cancel_orders(opportunity.up_token, opportunity.down_token)
            return ExecutionResult(success=False, reason="Failed to place maker orders")

        # Monitor for fills
        return await self._monitor_maker_fills(
            opportunity,
            size,
            up_order_id,
            down_order_id,
            timeout,
        )

    async def _execute_hybrid(
        self,
        opportunity: Opportunity,
        size: float,
    ) -> ExecutionResult:
        """
        Hybrid approach: Start with maker, escalate to taker if needed.

        This maximizes profit (by avoiding taker fees when possible)
        while ensuring fills (by escalating to taker if maker stalls).
        """
        logger.info(f"Executing HYBRID strategy on {opportunity.market_slug}")

        # Phase 1: Try maker first with short timeout
        maker_result = await self._execute_maker(opportunity, size, timeout=15.0)

        if maker_result.success:
            return maker_result

        # Phase 2: Escalate to taker if maker didn't fill
        if maker_result.reason == "Partial fill" or "timeout" in maker_result.reason.lower():
            logger.info("Maker incomplete, escalating to taker")

            # Cancel any remaining orders
            await self._cancel_orders(opportunity.up_token, opportunity.down_token)

            # Calculate remaining size needed
            remaining_up = size - maker_result.up_filled
            remaining_down = size - maker_result.down_filled

            if remaining_up > 0 and remaining_down > 0:
                # Both sides need more - do taker execution
                return await self._execute_taker(opportunity, min(remaining_up, remaining_down))
            elif remaining_up > 0:
                # Just need UP side
                return await self._rescue_single_side(
                    opportunity.up_token,
                    remaining_up,
                    opportunity.up_price,
                    opportunity.neg_risk,
                    maker_result,
                )
            elif remaining_down > 0:
                # Just need DOWN side
                return await self._rescue_single_side(
                    opportunity.down_token,
                    remaining_down,
                    opportunity.down_price,
                    opportunity.neg_risk,
                    maker_result,
                )

        return maker_result

    async def _place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        neg_risk: bool,
        post_only: bool = False,
    ) -> dict:
        """Place a single order via the client."""
        if self.client is None:
            raise ValueError("No client configured for order placement")

        try:
            result = self.client.create_order(
                marketId=token_id,
                action=side,
                price=price,
                size=size,
                neg_risk=neg_risk,
                post_only=post_only,
            )
            logger.debug(f"Order placed: {side} {size} @ {price} = {result}")
            return result
        except Exception as e:
            logger.error(f"Order placement failed: {e}")
            return {"success": False, "errorMsg": str(e)}

    async def _cancel_orders(self, up_token: str, down_token: str):
        """Cancel all orders for both tokens."""
        if self.client is None:
            return

        try:
            self.client.cancel_all_asset(up_token)
            self.client.cancel_all_asset(down_token)
            logger.debug(f"Cancelled orders for {up_token[:20]}... and {down_token[:20]}...")
        except Exception as e:
            logger.error(f"Failed to cancel orders: {e}")

    async def _monitor_maker_fills(
        self,
        opportunity: Opportunity,
        size: float,
        up_order_id: str,
        down_order_id: str,
        timeout: float,
    ) -> ExecutionResult:
        """Monitor maker orders for fills."""
        start_time = time.time()

        while time.time() - start_time < timeout:
            # Check fill status (would need to query order status from client)
            # For now, simplified version
            await asyncio.sleep(1.0)

            # TODO: Implement actual fill checking via client.get_order()
            # This would need the client to support order status queries

        # Timeout reached without full fill
        logger.warning("Maker orders timed out")
        return ExecutionResult(
            success=False,
            reason="Maker timeout - orders may be partially filled",
            up_order_id=up_order_id,
            down_order_id=down_order_id,
        )

    async def _rescue_single_side(
        self,
        token_id: str,
        size: float,
        price: float,
        neg_risk: bool,
        partial_result: ExecutionResult,
    ) -> ExecutionResult:
        """Rescue a partial fill by completing the other side."""
        logger.info(f"Rescuing single side: {size} shares of {token_id[:20]}...")

        result = await self._place_order(
            token_id,
            "BUY",
            price * 1.01,  # Slightly higher to ensure fill
            size,
            neg_risk,
            post_only=False,
        )

        if result.get("success", False) or result.get("orderID"):
            # Update the partial result
            return ExecutionResult(
                success=True,
                reason="Rescued with taker order",
                up_filled=partial_result.up_filled + size if "up" in token_id else partial_result.up_filled,
                down_filled=partial_result.down_filled + size if "down" in token_id else partial_result.down_filled,
                total_cost=partial_result.total_cost + (size * price * 1.01),
            )

        return ExecutionResult(
            success=False,
            reason=f"Rescue failed: {result.get('errorMsg', 'Unknown error')}",
        )

    def _process_execution_results(
        self,
        opportunity: Opportunity,
        size: float,
        up_result: dict,
        down_result: dict,
    ) -> ExecutionResult:
        """Process order placement results into ExecutionResult."""
        up_success = up_result.get("success", True) if isinstance(up_result, dict) else False
        down_success = down_result.get("success", True) if isinstance(down_result, dict) else False

        # Check for "crosses book" or other errors
        up_error = up_result.get("errorMsg", "") if isinstance(up_result, dict) else str(up_result)
        down_error = down_result.get("errorMsg", "") if isinstance(down_result, dict) else str(down_result)

        if up_error or down_error:
            return ExecutionResult(
                success=False,
                reason=f"Order errors - UP: {up_error}, DOWN: {down_error}",
            )

        # Assume full fill for now (would need fill confirmation in production)
        total_cost = size * (opportunity.up_price + opportunity.down_price)
        expected_profit = size * (1.0 - opportunity.combined_cost) - config.GAS_COST_USD

        # Create tracked position
        position = TrackedPosition(
            id=f"{opportunity.market_slug}-{int(time.time())}",
            market_slug=opportunity.market_slug,
            condition_id=opportunity.condition_id,
            up_token=opportunity.up_token,
            down_token=opportunity.down_token,
            neg_risk=opportunity.neg_risk,
            entry_time=datetime.now(timezone.utc),
            up_entry_price=opportunity.up_price,
            down_entry_price=opportunity.down_price,
            combined_cost=opportunity.combined_cost,
            target_size=size,
            up_filled=size,
            down_filled=size,
            up_order_id=up_result.get("orderID", ""),
            down_order_id=down_result.get("orderID", ""),
            is_balanced=True,
        )
        self.active_positions.append(position)

        logger.info(
            f"Execution complete: {opportunity.market_slug} "
            f"Size={size} Cost=${total_cost:.2f} Expected profit=${expected_profit:.2f}"
        )

        return ExecutionResult(
            success=True,
            reason="Orders placed successfully",
            up_filled=size,
            down_filled=size,
            up_price=opportunity.up_price,
            down_price=opportunity.down_price,
            total_cost=total_cost,
            expected_profit=expected_profit,
            up_order_id=up_result.get("orderID", ""),
            down_order_id=down_result.get("orderID", ""),
        )

    async def merge_position(self, position: TrackedPosition) -> bool:
        """
        Merge a balanced position to realize profit.

        When we hold equal amounts of YES and NO, merging converts them
        back to USDC at $1.00 per pair, realizing the arbitrage profit.
        """
        if not position.is_balanced:
            logger.warning(f"Cannot merge unbalanced position: {position.id}")
            return False

        merge_size = int(position.min_filled * 1e6)  # Convert to raw token amount

        logger.info(
            f"Merging position {position.id}: {position.min_filled} shares "
            f"on {position.market_slug}"
        )

        if self.dry_run:
            logger.info(f"[DRY RUN] Would merge {merge_size} tokens")
            position.is_closed = True
            position.close_time = datetime.now(timezone.utc)
            position.realized_profit = position.min_filled * (1.0 - position.combined_cost)
            self.total_profit += position.realized_profit
            return True

        if self.client is None:
            logger.warning("No client for merge operation")
            return False

        try:
            result = self.client.merge_positions(
                amount_to_merge=merge_size,
                condition_id=position.condition_id,
                is_neg_risk_market=position.neg_risk,
            )
            logger.info(f"Merge result: {result}")

            position.is_closed = True
            position.close_time = datetime.now(timezone.utc)
            position.realized_profit = position.min_filled * (1.0 - position.combined_cost)
            self.total_profit += position.realized_profit

            # Move to completed
            self.active_positions.remove(position)
            self.completed_positions.append(position)

            return True

        except Exception as e:
            logger.error(f"Merge failed: {e}")
            return False

    def get_status(self) -> dict:
        """Get executor status."""
        return {
            "dry_run": self.dry_run,
            "strategy": self.strategy.value,
            "executions_attempted": self.executions_attempted,
            "executions_successful": self.executions_successful,
            "active_positions": len(self.active_positions),
            "completed_positions": len(self.completed_positions),
            "total_profit": self.total_profit,
        }
