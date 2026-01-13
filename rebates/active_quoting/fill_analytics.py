"""
FillAnalytics - Markout analysis and fill tracking for active quoting.

Implements:
- Markout calculation (P&L after N seconds: mid_price_at_fill vs mid_price_later)
- Per-fill tracking with timestamps
- Aggregate statistics (fills, volume, realized P&L, markout by time horizon)
- Fee tracking (maker fees earned/paid)
- Toxicity scoring (adverse selection measurement)
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from collections import defaultdict

from .models import Fill, OrderSide

logger = logging.getLogger(__name__)


# Standard markout horizons in seconds
MARKOUT_HORIZONS = [1, 5, 15, 30, 60]


@dataclass
class MarkoutSample:
    """A single markout sample for a fill."""
    fill_id: str
    horizon_seconds: int
    mid_at_fill: float
    mid_at_horizon: Optional[float] = None
    markout: Optional[float] = None  # P&L in price terms
    markout_bps: Optional[float] = None  # P&L in basis points
    captured_at: Optional[datetime] = None


@dataclass
class FillRecord:
    """Enhanced fill record with markout tracking."""
    fill: Fill
    mid_price_at_fill: float
    markouts: Dict[int, MarkoutSample] = field(default_factory=dict)
    captured: bool = False  # All markouts captured

    @property
    def fill_id(self) -> str:
        """Unique fill identifier."""
        return self.fill.trade_id or f"{self.fill.order_id}_{self.fill.timestamp.timestamp()}"

    def get_markout(self, horizon_seconds: int) -> Optional[float]:
        """Get markout for a specific horizon."""
        sample = self.markouts.get(horizon_seconds)
        return sample.markout if sample else None


@dataclass
class MarketStats:
    """Statistics for a single market."""
    token_id: str
    fill_count: int = 0
    buy_count: int = 0
    sell_count: int = 0
    total_volume: float = 0.0  # In shares
    total_notional: float = 0.0  # In USDC
    total_fees_paid: float = 0.0
    total_fees_earned: float = 0.0  # For rebates
    realized_pnl: float = 0.0
    # Markout stats by horizon
    markout_sums: Dict[int, float] = field(default_factory=dict)
    markout_counts: Dict[int, int] = field(default_factory=dict)

    def avg_markout(self, horizon: int) -> Optional[float]:
        """Get average markout for a horizon."""
        count = self.markout_counts.get(horizon, 0)
        if count == 0:
            return None
        return self.markout_sums.get(horizon, 0.0) / count

    def avg_markout_bps(self, horizon: int) -> Optional[float]:
        """Get average markout in basis points."""
        avg = self.avg_markout(horizon)
        if avg is None or self.fill_count == 0:
            return None
        avg_fill_price = self.total_notional / self.total_volume if self.total_volume > 0 else 0.5
        if avg_fill_price > 0:
            return (avg / avg_fill_price) * 10000
        return None


@dataclass
class AggregateStats:
    """Aggregate statistics across all markets."""
    total_fills: int = 0
    total_volume: float = 0.0
    total_notional: float = 0.0
    total_fees_paid: float = 0.0
    total_fees_earned: float = 0.0
    net_fees: float = 0.0
    realized_pnl: float = 0.0
    # Markout stats
    markout_sums: Dict[int, float] = field(default_factory=dict)
    markout_counts: Dict[int, int] = field(default_factory=dict)

    def avg_markout(self, horizon: int) -> Optional[float]:
        """Get average markout for a horizon."""
        count = self.markout_counts.get(horizon, 0)
        if count == 0:
            return None
        return self.markout_sums.get(horizon, 0.0) / count


class FillAnalytics:
    """
    Tracks fill quality through markout analysis.

    Markout measures adverse selection by comparing:
    - Mid price at time of fill
    - Mid price N seconds later

    Positive markout = good fill (price moved in our favor)
    Negative markout = bad fill (adverse selection - we got picked off)
    """

    def __init__(
        self,
        horizons: Optional[List[int]] = None,
        on_markout_captured: Optional[callable] = None,
    ):
        """
        Initialize FillAnalytics.

        Args:
            horizons: Markout horizons in seconds (default: [1, 5, 15, 30, 60])
            on_markout_captured: Callback when a markout is captured
        """
        self.horizons = horizons or MARKOUT_HORIZONS.copy()
        self.on_markout_captured = on_markout_captured

        # Fill tracking
        self._fills: Dict[str, FillRecord] = {}  # fill_id -> FillRecord
        self._pending_markouts: Dict[Tuple[str, int], datetime] = {}  # (fill_id, horizon) -> capture_time

        # Per-market stats
        self._market_stats: Dict[str, MarketStats] = {}

        # Aggregate stats
        self._aggregate = AggregateStats()

        # Running tasks for markout capture
        self._markout_tasks: Dict[str, asyncio.Task] = {}

    @property
    def fills(self) -> Dict[str, FillRecord]:
        """Get all fill records."""
        return self._fills

    @property
    def market_stats(self) -> Dict[str, MarketStats]:
        """Get per-market statistics."""
        return self._market_stats

    @property
    def aggregate_stats(self) -> AggregateStats:
        """Get aggregate statistics."""
        return self._aggregate

    def get_market_stats(self, token_id: str) -> MarketStats:
        """Get or create stats for a market."""
        if token_id not in self._market_stats:
            self._market_stats[token_id] = MarketStats(token_id=token_id)
        return self._market_stats[token_id]

    def record_fill(
        self,
        fill: Fill,
        mid_price_at_fill: float,
        schedule_markouts: bool = True,
    ) -> FillRecord:
        """
        Record a new fill for markout tracking.

        Args:
            fill: The Fill object
            mid_price_at_fill: Market mid price at time of fill
            schedule_markouts: Whether to schedule markout capture tasks

        Returns:
            FillRecord for the fill
        """
        record = FillRecord(
            fill=fill,
            mid_price_at_fill=mid_price_at_fill,
        )

        # Initialize markout samples for each horizon
        for horizon in self.horizons:
            record.markouts[horizon] = MarkoutSample(
                fill_id=record.fill_id,
                horizon_seconds=horizon,
                mid_at_fill=mid_price_at_fill,
            )
            # Track pending markout capture time
            capture_time = fill.timestamp + timedelta(seconds=horizon)
            self._pending_markouts[(record.fill_id, horizon)] = capture_time

        self._fills[record.fill_id] = record

        # Update stats
        self._update_stats_on_fill(fill)

        # Schedule markout capture if requested
        if schedule_markouts:
            self._schedule_markout_capture(record)

        logger.info(
            f"Recorded fill: {fill.side.value} {fill.size:.2f} @ {fill.price:.4f} "
            f"(mid: {mid_price_at_fill:.4f})"
        )

        return record

    def _update_stats_on_fill(self, fill: Fill) -> None:
        """Update statistics when a fill is recorded."""
        # Market stats
        stats = self.get_market_stats(fill.token_id)
        stats.fill_count += 1
        stats.total_volume += fill.size
        stats.total_notional += fill.notional

        if fill.side == OrderSide.BUY:
            stats.buy_count += 1
        else:
            stats.sell_count += 1

        # Fee tracking - negative fees are rebates
        if fill.fee > 0:
            stats.total_fees_paid += fill.fee
        else:
            stats.total_fees_earned += abs(fill.fee)

        # Aggregate stats
        self._aggregate.total_fills += 1
        self._aggregate.total_volume += fill.size
        self._aggregate.total_notional += fill.notional

        if fill.fee > 0:
            self._aggregate.total_fees_paid += fill.fee
        else:
            self._aggregate.total_fees_earned += abs(fill.fee)

        self._aggregate.net_fees = (
            self._aggregate.total_fees_earned - self._aggregate.total_fees_paid
        )

    def _schedule_markout_capture(self, record: FillRecord) -> None:
        """Schedule async tasks to capture markouts at each horizon."""
        for horizon in self.horizons:
            delay = horizon
            task = asyncio.create_task(
                self._capture_markout_after_delay(record.fill_id, horizon, delay)
            )
            task_key = f"{record.fill_id}_{horizon}"
            self._markout_tasks[task_key] = task

    async def _capture_markout_after_delay(
        self,
        fill_id: str,
        horizon: int,
        delay: float,
    ) -> None:
        """Wait and then trigger markout capture."""
        try:
            await asyncio.sleep(delay)
            # The actual price lookup happens in capture_markout
            # which should be called with the current mid price
        except asyncio.CancelledError:
            pass

    def capture_markout(
        self,
        fill_id: str,
        horizon: int,
        mid_price_now: float,
    ) -> Optional[MarkoutSample]:
        """
        Capture a markout sample for a fill at a specific horizon.

        Args:
            fill_id: The fill ID
            horizon: The horizon in seconds
            mid_price_now: Current mid price

        Returns:
            MarkoutSample if captured, None if fill not found
        """
        record = self._fills.get(fill_id)
        if not record:
            return None

        sample = record.markouts.get(horizon)
        if not sample or sample.captured_at is not None:
            return sample  # Already captured or not found

        # Calculate markout
        fill = record.fill
        mid_at_fill = sample.mid_at_fill

        # For BUY: profit if price went up
        # For SELL: profit if price went down
        if fill.side == OrderSide.BUY:
            markout = mid_price_now - mid_at_fill
        else:
            markout = mid_at_fill - mid_price_now

        # Convert to basis points
        if mid_at_fill > 0:
            markout_bps = (markout / mid_at_fill) * 10000
        else:
            markout_bps = 0.0

        # Update sample
        sample.mid_at_horizon = mid_price_now
        sample.markout = markout
        sample.markout_bps = markout_bps
        sample.captured_at = datetime.utcnow()

        # Remove from pending
        self._pending_markouts.pop((fill_id, horizon), None)

        # Update stats
        self._update_stats_on_markout(fill.token_id, horizon, markout)

        # Check if all markouts captured
        if all(s.captured_at is not None for s in record.markouts.values()):
            record.captured = True

        logger.debug(
            f"Markout captured: fill={fill_id} horizon={horizon}s "
            f"markout={markout:.6f} ({markout_bps:.2f}bps)"
        )

        if self.on_markout_captured:
            self.on_markout_captured(sample)

        return sample

    def _update_stats_on_markout(
        self,
        token_id: str,
        horizon: int,
        markout: float,
    ) -> None:
        """Update statistics when a markout is captured."""
        # Market stats
        stats = self.get_market_stats(token_id)
        if horizon not in stats.markout_sums:
            stats.markout_sums[horizon] = 0.0
            stats.markout_counts[horizon] = 0
        stats.markout_sums[horizon] += markout
        stats.markout_counts[horizon] += 1

        # Aggregate stats
        if horizon not in self._aggregate.markout_sums:
            self._aggregate.markout_sums[horizon] = 0.0
            self._aggregate.markout_counts[horizon] = 0
        self._aggregate.markout_sums[horizon] += markout
        self._aggregate.markout_counts[horizon] += 1

    def get_pending_markouts(self) -> List[Tuple[str, int, datetime]]:
        """
        Get list of pending markout captures.

        Returns:
            List of (fill_id, horizon, capture_time) tuples
        """
        return [
            (fill_id, horizon, capture_time)
            for (fill_id, horizon), capture_time in self._pending_markouts.items()
        ]

    def get_due_markouts(self) -> List[Tuple[str, int]]:
        """
        Get markouts that are due for capture.

        Returns:
            List of (fill_id, horizon) tuples ready to capture
        """
        now = datetime.utcnow()
        due = []
        for (fill_id, horizon), capture_time in self._pending_markouts.items():
            if capture_time <= now:
                due.append((fill_id, horizon))
        return due

    def process_markout_captures(self, get_mid_price: callable) -> List[MarkoutSample]:
        """
        Process all due markout captures.

        Args:
            get_mid_price: Callable(token_id) -> Optional[float] to get current mid price

        Returns:
            List of captured MarkoutSample objects
        """
        captured = []
        for fill_id, horizon in self.get_due_markouts():
            record = self._fills.get(fill_id)
            if not record:
                self._pending_markouts.pop((fill_id, horizon), None)
                continue

            mid_price = get_mid_price(record.fill.token_id)
            if mid_price is not None:
                sample = self.capture_markout(fill_id, horizon, mid_price)
                if sample:
                    captured.append(sample)

        return captured

    def update_realized_pnl(self, token_id: str, pnl: float) -> None:
        """Update realized P&L for a market."""
        stats = self.get_market_stats(token_id)
        stats.realized_pnl = pnl
        # Recalculate aggregate
        self._aggregate.realized_pnl = sum(
            s.realized_pnl for s in self._market_stats.values()
        )

    def get_fill_record(self, fill_id: str) -> Optional[FillRecord]:
        """Get fill record by ID."""
        return self._fills.get(fill_id)

    def get_recent_fills(
        self,
        token_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[FillRecord]:
        """
        Get recent fill records, optionally filtered by token.

        Args:
            token_id: Optional token ID filter
            limit: Maximum number of records to return

        Returns:
            List of FillRecord objects, most recent first
        """
        records = list(self._fills.values())
        if token_id:
            records = [r for r in records if r.fill.token_id == token_id]
        # Sort by timestamp descending
        records.sort(key=lambda r: r.fill.timestamp, reverse=True)
        return records[:limit]

    def get_toxicity_score(self, token_id: Optional[str] = None) -> Optional[float]:
        """
        Calculate toxicity score (negative markout rate).

        A higher toxicity score means more adverse selection.
        Score = average negative markout at 5s horizon (in bps).

        Args:
            token_id: Optional token ID (None for aggregate)

        Returns:
            Toxicity score in basis points, or None if insufficient data
        """
        # Use 5-second horizon as primary toxicity measure
        horizon = 5
        if horizon not in self.horizons:
            horizon = self.horizons[0] if self.horizons else None
            if horizon is None:
                return None

        if token_id:
            stats = self._market_stats.get(token_id)
            if not stats:
                return None
            avg = stats.avg_markout(horizon)
        else:
            avg = self._aggregate.avg_markout(horizon)

        if avg is None:
            return None

        # Toxicity is negative markout - invert sign
        # High toxicity = high negative markout = being picked off
        return -avg * 10000 if avg < 0 else 0.0

    def get_summary(self) -> dict:
        """Get summary statistics for logging/monitoring."""
        return {
            "total_fills": self._aggregate.total_fills,
            "total_volume": self._aggregate.total_volume,
            "total_notional": self._aggregate.total_notional,
            "net_fees": self._aggregate.net_fees,
            "fees_earned": self._aggregate.total_fees_earned,
            "fees_paid": self._aggregate.total_fees_paid,
            "realized_pnl": self._aggregate.realized_pnl,
            "markouts": {
                f"{h}s": {
                    "avg": self._aggregate.avg_markout(h),
                    "count": self._aggregate.markout_counts.get(h, 0),
                }
                for h in self.horizons
            },
            "pending_markouts": len(self._pending_markouts),
            "toxicity_score": self.get_toxicity_score(),
            "markets": {
                token_id: {
                    "fills": stats.fill_count,
                    "volume": stats.total_volume,
                    "realized_pnl": stats.realized_pnl,
                }
                for token_id, stats in self._market_stats.items()
            },
        }

    def reset(self, token_id: Optional[str] = None) -> None:
        """
        Reset analytics state.

        Args:
            token_id: Optional token ID to reset (None for all)
        """
        if token_id:
            # Reset specific market
            if token_id in self._market_stats:
                del self._market_stats[token_id]
            # Remove fills for this token
            self._fills = {
                fid: r for fid, r in self._fills.items()
                if r.fill.token_id != token_id
            }
            # Remove pending markouts for this token
            self._pending_markouts = {
                k: v for k, v in self._pending_markouts.items()
                if self._fills.get(k[0], None) is None or
                   self._fills[k[0]].fill.token_id != token_id
            }
        else:
            # Reset all
            self._fills.clear()
            self._pending_markouts.clear()
            self._market_stats.clear()
            self._aggregate = AggregateStats()

            # Cancel all markout tasks
            for task in self._markout_tasks.values():
                task.cancel()
            self._markout_tasks.clear()

    async def shutdown(self) -> None:
        """Clean shutdown - cancel pending markout tasks."""
        for task in self._markout_tasks.values():
            task.cancel()
        self._markout_tasks.clear()
        logger.info("FillAnalytics shutdown complete")
