"""
Gabagool Position Manager

Manages the full lifecycle of Gabagool positions from entry to profit realization.
Handles position tracking, merge execution, and post-resolution redemption.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict

from .executor import TrackedPosition
from .reconciler import PositionReconciler, PositionStatus, ReconciliationResult
from . import config

logger = logging.getLogger(__name__)


@dataclass
class PositionSummary:
    """Summary of position profit/loss."""
    position_id: str
    market_slug: str
    status: str
    entry_cost: float
    current_value: float
    realized_pnl: float
    unrealized_pnl: float
    is_profitable: bool


class PositionManager:
    """
    Manages Gabagool positions through their lifecycle.

    Lifecycle stages:
    1. ENTRY: Position created after successful execution
    2. MONITORING: Track fills, reconcile imbalances
    3. PROFIT_READY: Position balanced, ready for merge or resolution
    4. REALIZATION: Execute merge or await resolution
    5. CLOSED: Profit/loss realized

    Persistence:
    - Positions are saved to JSON file for recovery across restarts
    - Active positions are checked on startup
    """

    def __init__(
        self,
        client=None,
        reconciler: PositionReconciler = None,
        positions_file: str = None,
    ):
        """
        Initialize the position manager.

        Args:
            client: PolymarketClient for blockchain operations
            reconciler: PositionReconciler for handling imbalances
            positions_file: Path to save/load positions (for persistence)
        """
        self.client = client
        self.reconciler = reconciler or PositionReconciler(client=client)

        # Position storage
        self.positions: Dict[str, TrackedPosition] = {}
        self.closed_positions: List[TrackedPosition] = []

        # Persistence
        self.positions_file = positions_file or "gabagool_positions.json"

        # Statistics
        self.total_entries = 0
        self.total_merges = 0
        self.total_redemptions = 0
        self.total_profit = 0.0

    def add_position(self, position: TrackedPosition) -> None:
        """
        Add a new position to track.

        Args:
            position: The position to add
        """
        self.positions[position.id] = position
        self.total_entries += 1

        logger.info(
            f"Added position {position.id} for {position.market_slug}: "
            f"target={position.target_size}, combined_cost={position.combined_cost:.4f}"
        )

        self._save_positions()

    def get_position(self, position_id: str) -> Optional[TrackedPosition]:
        """Get a position by ID."""
        return self.positions.get(position_id)

    def get_active_positions(self) -> List[TrackedPosition]:
        """Get all active (non-closed) positions."""
        return [p for p in self.positions.values() if not p.is_closed]

    def get_merge_ready_positions(self) -> List[TrackedPosition]:
        """Get positions that are balanced and ready to merge."""
        return [
            p for p in self.positions.values()
            if p.is_balanced and not p.is_closed and p.min_filled > 0
        ]

    async def update_position_fills(
        self,
        position_id: str,
        up_filled: float,
        down_filled: float,
    ) -> None:
        """
        Update fill amounts for a position.

        Args:
            position_id: Position to update
            up_filled: New UP fill amount
            down_filled: New DOWN fill amount
        """
        position = self.positions.get(position_id)
        if not position:
            logger.warning(f"Position {position_id} not found")
            return

        old_up = position.up_filled
        old_down = position.down_filled

        position.up_filled = up_filled
        position.down_filled = down_filled

        # Check if now balanced
        if abs(up_filled - down_filled) < 0.01:
            position.is_balanced = True

        logger.debug(
            f"Updated {position_id}: UP {old_up:.2f}->{up_filled:.2f}, "
            f"DOWN {old_down:.2f}->{down_filled:.2f}, balanced={position.is_balanced}"
        )

        self._save_positions()

    async def reconcile_positions(self) -> List[ReconciliationResult]:
        """
        Reconcile all positions that need attention.

        Returns:
            List of reconciliation results
        """
        positions_needing_reconciliation = [
            p for p in self.positions.values()
            if not p.is_closed and not p.is_balanced
        ]

        if not positions_needing_reconciliation:
            return []

        results = await self.reconciler.reconcile_all(positions_needing_reconciliation)

        self._save_positions()
        return results

    async def process_merges(self) -> int:
        """
        Process all positions ready for merge.

        Returns:
            Number of successful merges
        """
        merge_ready = self.get_merge_ready_positions()
        if not merge_ready:
            return 0

        successful = 0

        for position in merge_ready:
            success = await self._execute_merge(position)
            if success:
                successful += 1

        return successful

    async def _execute_merge(self, position: TrackedPosition) -> bool:
        """
        Execute merge for a balanced position.

        Args:
            position: Position to merge

        Returns:
            True if merge successful
        """
        if not position.is_balanced:
            logger.warning(f"Cannot merge unbalanced position {position.id}")
            return False

        merge_size = int(position.min_filled * 1e6)  # Raw token amount

        logger.info(
            f"Merging position {position.id}: {position.min_filled:.2f} shares "
            f"on {position.market_slug}"
        )

        if config.DRY_RUN:
            logger.info(f"[DRY RUN] Would merge {merge_size} tokens")
            self._close_position(position, "merged_dry_run")
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

            self._close_position(position, "merged")
            self.total_merges += 1

            return True

        except Exception as e:
            logger.error(f"Merge failed for {position.id}: {e}")
            return False

    async def check_resolutions(self) -> int:
        """
        Check for resolved markets and redeem positions.

        Returns:
            Number of successful redemptions
        """
        if self.client is None:
            return 0

        active = self.get_active_positions()
        successful = 0

        for position in active:
            try:
                is_resolved, _ = self.client.is_market_resolved(position.up_token)

                if is_resolved:
                    logger.info(f"Market resolved for {position.id}, attempting redemption")
                    success = await self._execute_redemption(position)
                    if success:
                        successful += 1

            except Exception as e:
                logger.error(f"Error checking resolution for {position.id}: {e}")

        return successful

    async def _execute_redemption(self, position: TrackedPosition) -> bool:
        """
        Redeem positions after market resolution.

        Args:
            position: Position to redeem

        Returns:
            True if redemption successful
        """
        logger.info(f"Redeeming position {position.id} on {position.market_slug}")

        if config.DRY_RUN:
            logger.info(f"[DRY RUN] Would redeem position {position.id}")
            self._close_position(position, "redeemed_dry_run")
            return True

        if self.client is None:
            return False

        try:
            result = self.client.redeem_positions(position.condition_id)
            logger.info(f"Redemption result: {result}")

            self._close_position(position, "redeemed")
            self.total_redemptions += 1

            return True

        except Exception as e:
            logger.error(f"Redemption failed for {position.id}: {e}")
            return False

    def _close_position(self, position: TrackedPosition, reason: str) -> None:
        """
        Close a position and calculate profit.

        Args:
            position: Position to close
            reason: Reason for closing (merged, redeemed, etc.)
        """
        position.is_closed = True
        position.close_time = datetime.now(timezone.utc)

        # Calculate profit
        entry_cost = position.min_filled * position.combined_cost
        exit_value = position.min_filled * 1.0  # $1.00 per merged pair
        position.realized_profit = exit_value - entry_cost

        self.total_profit += position.realized_profit

        logger.info(
            f"Closed position {position.id} ({reason}): "
            f"profit=${position.realized_profit:.2f}"
        )

        # Move to closed list
        if position.id in self.positions:
            del self.positions[position.id]
        self.closed_positions.append(position)

        self._save_positions()

    def get_summary(self) -> Dict:
        """Get manager summary statistics."""
        active = self.get_active_positions()
        merge_ready = self.get_merge_ready_positions()

        return {
            "active_positions": len(active),
            "merge_ready": len(merge_ready),
            "closed_positions": len(self.closed_positions),
            "total_entries": self.total_entries,
            "total_merges": self.total_merges,
            "total_redemptions": self.total_redemptions,
            "total_profit": self.total_profit,
            "reconciler": self.reconciler.get_status(),
        }

    def get_position_summaries(self) -> List[PositionSummary]:
        """Get summaries of all active positions."""
        summaries = []

        for position in self.positions.values():
            entry_cost = position.target_size * position.combined_cost
            current_value = position.min_filled * 1.0  # Value if merged now

            summary = PositionSummary(
                position_id=position.id,
                market_slug=position.market_slug,
                status="balanced" if position.is_balanced else "imbalanced",
                entry_cost=entry_cost,
                current_value=current_value,
                realized_pnl=position.realized_profit,
                unrealized_pnl=current_value - (position.min_filled * position.combined_cost),
                is_profitable=current_value > (position.min_filled * position.combined_cost),
            )
            summaries.append(summary)

        return summaries

    def _save_positions(self) -> None:
        """Save positions to file for persistence."""
        try:
            data = {
                "positions": {
                    pid: {
                        "id": p.id,
                        "market_slug": p.market_slug,
                        "condition_id": p.condition_id,
                        "up_token": p.up_token,
                        "down_token": p.down_token,
                        "neg_risk": p.neg_risk,
                        "entry_time": p.entry_time.isoformat(),
                        "up_entry_price": p.up_entry_price,
                        "down_entry_price": p.down_entry_price,
                        "combined_cost": p.combined_cost,
                        "target_size": p.target_size,
                        "up_filled": p.up_filled,
                        "down_filled": p.down_filled,
                        "is_balanced": p.is_balanced,
                        "is_closed": p.is_closed,
                    }
                    for pid, p in self.positions.items()
                },
                "stats": {
                    "total_entries": self.total_entries,
                    "total_merges": self.total_merges,
                    "total_redemptions": self.total_redemptions,
                    "total_profit": self.total_profit,
                },
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }

            with open(self.positions_file, "w") as f:
                json.dump(data, f, indent=2)

        except Exception as e:
            logger.error(f"Failed to save positions: {e}")

    def load_positions(self) -> int:
        """
        Load positions from file.

        Returns:
            Number of positions loaded
        """
        try:
            path = Path(self.positions_file)
            if not path.exists():
                return 0

            with open(path) as f:
                data = json.load(f)

            for pid, pdata in data.get("positions", {}).items():
                position = TrackedPosition(
                    id=pdata["id"],
                    market_slug=pdata["market_slug"],
                    condition_id=pdata["condition_id"],
                    up_token=pdata["up_token"],
                    down_token=pdata["down_token"],
                    neg_risk=pdata["neg_risk"],
                    entry_time=datetime.fromisoformat(pdata["entry_time"]),
                    up_entry_price=pdata["up_entry_price"],
                    down_entry_price=pdata["down_entry_price"],
                    combined_cost=pdata["combined_cost"],
                    target_size=pdata["target_size"],
                    up_filled=pdata["up_filled"],
                    down_filled=pdata["down_filled"],
                    is_balanced=pdata["is_balanced"],
                    is_closed=pdata.get("is_closed", False),
                )
                self.positions[pid] = position

            # Restore stats
            stats = data.get("stats", {})
            self.total_entries = stats.get("total_entries", len(self.positions))
            self.total_merges = stats.get("total_merges", 0)
            self.total_redemptions = stats.get("total_redemptions", 0)
            self.total_profit = stats.get("total_profit", 0.0)

            logger.info(f"Loaded {len(self.positions)} positions from {self.positions_file}")
            return len(self.positions)

        except Exception as e:
            logger.error(f"Failed to load positions: {e}")
            return 0
