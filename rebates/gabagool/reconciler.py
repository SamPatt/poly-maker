"""
Gabagool Position Reconciler

Handles partial fills and position imbalances. When one side fills
but the other doesn't, we need to rescue the position to avoid
directional exposure.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, List, Tuple

from .executor import TrackedPosition
from . import config

logger = logging.getLogger(__name__)


class PositionStatus(Enum):
    """Position lifecycle states."""
    PENDING = "pending"              # Orders placed, awaiting fills
    PARTIALLY_FILLED = "partial"     # One side filled, other pending
    REBALANCING = "rebalancing"      # Actively rescuing imbalanced position
    FILLED = "filled"                # Both sides filled
    MERGE_READY = "merge_ready"      # Balanced, ready to merge
    AWAIT_RESOLUTION = "await_res"   # Waiting for market resolution
    MERGED = "merged"                # Merge completed
    RESOLVED = "resolved"            # Market resolved, redeemed
    CLOSED = "closed"                # Position closed (profit realized)
    FAILED = "failed"                # Position failed (loss or error)


@dataclass
class ReconciliationResult:
    """Result of a reconciliation attempt."""
    success: bool
    action_taken: str
    new_status: PositionStatus
    details: str = ""


class PositionReconciler:
    """
    Reconciles partial fills and manages position imbalances.

    When executing Gabagool trades, there's always a risk that one side
    fills but the other doesn't. This creates directional exposure that
    we need to resolve quickly.

    Strategies:
    1. Wait briefly for natural fill
    2. Rescue with taker order on unfilled side
    3. Exit filled side if rescue fails (last resort)
    """

    def __init__(
        self,
        client=None,
        max_imbalance_pct: float = None,
        rescue_timeout: float = None,
    ):
        """
        Initialize the reconciler.

        Args:
            client: PolymarketClient for order operations
            max_imbalance_pct: Maximum allowed imbalance before forced exit
            rescue_timeout: Seconds to wait before rescue attempt
        """
        self.client = client
        self.max_imbalance_pct = max_imbalance_pct or config.MAX_IMBALANCE_PCT
        self.rescue_timeout = rescue_timeout or config.RECONCILIATION_DELAY

        # Statistics
        self.rescues_attempted = 0
        self.rescues_successful = 0
        self.forced_exits = 0

    async def check_and_reconcile(
        self,
        position: TrackedPosition,
    ) -> ReconciliationResult:
        """
        Check position status and reconcile if needed.

        Args:
            position: The position to check

        Returns:
            ReconciliationResult with action taken
        """
        # Already closed - check this first
        if position.is_closed:
            return ReconciliationResult(
                success=True,
                action_taken="none",
                new_status=PositionStatus.CLOSED,
                details="Position already closed",
            )

        # Already balanced
        if position.is_balanced:
            return ReconciliationResult(
                success=True,
                action_taken="none",
                new_status=PositionStatus.MERGE_READY,
                details="Position is balanced",
            )

        # Check imbalance level
        imbalance = position.imbalance
        imbalance_pct = (imbalance / position.target_size * 100) if position.target_size > 0 else 0

        logger.info(
            f"Position {position.id} imbalance: {imbalance:.2f} shares "
            f"({imbalance_pct:.1f}%) - UP={position.up_filled}, DOWN={position.down_filled}"
        )

        # Determine rescue strategy
        if imbalance_pct > self.max_imbalance_pct:
            # Severe imbalance - may need to exit
            return await self._handle_severe_imbalance(position, imbalance_pct)

        # Try to rescue with taker order
        return await self._rescue_with_taker(position)

    async def _rescue_with_taker(
        self,
        position: TrackedPosition,
    ) -> ReconciliationResult:
        """
        Rescue an imbalanced position by buying the unfilled side.

        Args:
            position: The imbalanced position

        Returns:
            ReconciliationResult
        """
        self.rescues_attempted += 1

        # Determine which side needs filling
        if position.up_filled > position.down_filled:
            # Need more DOWN
            unfilled_side = "down"
            token_id = position.down_token
            needed_size = position.up_filled - position.down_filled
        else:
            # Need more UP
            unfilled_side = "up"
            token_id = position.up_token
            needed_size = position.down_filled - position.up_filled

        logger.info(
            f"Rescuing position {position.id}: need {needed_size:.2f} more {unfilled_side.upper()}"
        )

        if self.client is None:
            logger.warning("No client available for rescue operation")
            return ReconciliationResult(
                success=False,
                action_taken="rescue_skipped",
                new_status=PositionStatus.PARTIALLY_FILLED,
                details="No client available",
            )

        try:
            # Place aggressive taker order to fill immediately
            # Use a price slightly above best ask to ensure fill
            rescue_price = 0.55  # Aggressive price to ensure fill

            result = self.client.create_order(
                marketId=token_id,
                action="BUY",
                price=rescue_price,
                size=needed_size,
                neg_risk=position.neg_risk,
                post_only=False,  # Take liquidity
            )

            if result.get("orderID") or result.get("success", True):
                self.rescues_successful += 1

                # Update position fills
                if unfilled_side == "up":
                    position.up_filled += needed_size
                else:
                    position.down_filled += needed_size

                # Check if now balanced
                if abs(position.up_filled - position.down_filled) < 0.01:
                    position.is_balanced = True
                    return ReconciliationResult(
                        success=True,
                        action_taken="rescue_taker",
                        new_status=PositionStatus.MERGE_READY,
                        details=f"Rescued {needed_size:.2f} {unfilled_side.upper()} shares",
                    )
                else:
                    return ReconciliationResult(
                        success=True,
                        action_taken="rescue_partial",
                        new_status=PositionStatus.PARTIALLY_FILLED,
                        details=f"Partial rescue, still imbalanced",
                    )
            else:
                error_msg = result.get("errorMsg", "Unknown error")
                logger.error(f"Rescue order failed: {error_msg}")
                return ReconciliationResult(
                    success=False,
                    action_taken="rescue_failed",
                    new_status=PositionStatus.PARTIALLY_FILLED,
                    details=error_msg,
                )

        except Exception as e:
            logger.error(f"Rescue failed with exception: {e}")
            return ReconciliationResult(
                success=False,
                action_taken="rescue_error",
                new_status=PositionStatus.PARTIALLY_FILLED,
                details=str(e),
            )

    async def _handle_severe_imbalance(
        self,
        position: TrackedPosition,
        imbalance_pct: float,
    ) -> ReconciliationResult:
        """
        Handle severe imbalance that may require emergency exit.

        When imbalance exceeds threshold, we may need to sell the
        excess position to limit losses.

        Args:
            position: The severely imbalanced position
            imbalance_pct: Current imbalance percentage

        Returns:
            ReconciliationResult
        """
        logger.warning(
            f"Severe imbalance on {position.id}: {imbalance_pct:.1f}% "
            f"exceeds threshold {self.max_imbalance_pct}%"
        )

        # First try rescue
        rescue_result = await self._rescue_with_taker(position)
        if rescue_result.success and rescue_result.new_status == PositionStatus.MERGE_READY:
            return rescue_result

        # Rescue failed - consider emergency exit
        logger.warning(f"Rescue failed for {position.id}, considering emergency exit")

        # For now, mark as failed and let manual intervention handle it
        # In production, we might want to sell the excess position
        self.forced_exits += 1

        return ReconciliationResult(
            success=False,
            action_taken="emergency_flagged",
            new_status=PositionStatus.FAILED,
            details=f"Severe imbalance ({imbalance_pct:.1f}%), requires manual review",
        )

    async def reconcile_all(
        self,
        positions: List[TrackedPosition],
    ) -> List[ReconciliationResult]:
        """
        Reconcile all positions that need attention.

        Args:
            positions: List of positions to check

        Returns:
            List of reconciliation results
        """
        results = []

        for position in positions:
            if not position.is_closed and not position.is_balanced:
                result = await self.check_and_reconcile(position)
                results.append(result)

                if result.success:
                    logger.info(
                        f"Reconciled {position.id}: {result.action_taken} -> {result.new_status.value}"
                    )
                else:
                    logger.warning(
                        f"Failed to reconcile {position.id}: {result.details}"
                    )

        return results

    def get_status(self) -> dict:
        """Get reconciler statistics."""
        return {
            "rescues_attempted": self.rescues_attempted,
            "rescues_successful": self.rescues_successful,
            "forced_exits": self.forced_exits,
            "rescue_success_rate": (
                self.rescues_successful / self.rescues_attempted * 100
                if self.rescues_attempted > 0
                else 0
            ),
        }
