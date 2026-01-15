"""
PnL Tracker - Real-time profit/loss tracking for active quoting.

Provides clear visibility into whether rapid trades are making or losing money by:
1. Tracking "round-trip" trades (buy -> sell cycles)
2. Showing per-trade P&L immediately when a position is closed
3. Maintaining a running session summary with periodic logging
4. Separating gross P&L from fees for clarity
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from collections import deque

from .models import Fill, OrderSide, Position

logger = logging.getLogger(__name__)


@dataclass
class TradeResult:
    """Result of a single sell trade (closing part of a position)."""
    timestamp: datetime
    token_id: str
    market_name: str
    shares_sold: float
    sell_price: float
    avg_buy_price: float
    gross_pnl: float  # Price difference only
    fee: float  # Fee for this trade
    net_pnl: float  # Gross - fee
    position_remaining: float  # Position after this trade

    @property
    def pnl_per_share(self) -> float:
        return self.gross_pnl / self.shares_sold if self.shares_sold > 0 else 0

    @property
    def return_pct(self) -> float:
        """Return as percentage of entry price."""
        if self.avg_buy_price > 0 and self.shares_sold > 0:
            return (self.pnl_per_share / self.avg_buy_price) * 100
        return 0


@dataclass
class SessionStats:
    """Accumulated statistics for the trading session."""
    start_time: datetime = field(default_factory=datetime.utcnow)

    # Trade counts
    total_buys: int = 0
    total_sells: int = 0
    total_trades: int = 0

    # Volume
    buy_volume: float = 0.0  # Shares bought
    sell_volume: float = 0.0  # Shares sold
    buy_notional: float = 0.0  # USDC spent buying
    sell_notional: float = 0.0  # USDC received selling

    # P&L
    gross_pnl: float = 0.0  # From trading (price difference)
    total_fees: float = 0.0  # All fees paid
    net_pnl: float = 0.0  # Gross - fees

    # Win/loss tracking
    winning_trades: int = 0
    losing_trades: int = 0
    breakeven_trades: int = 0
    largest_win: float = 0.0
    largest_loss: float = 0.0

    # Running averages
    avg_win: float = 0.0
    avg_loss: float = 0.0

    @property
    def win_rate(self) -> float:
        """Win rate as percentage."""
        closed = self.winning_trades + self.losing_trades + self.breakeven_trades
        if closed == 0:
            return 0
        return (self.winning_trades / closed) * 100

    @property
    def session_duration(self) -> timedelta:
        return datetime.utcnow() - self.start_time

    @property
    def pnl_per_hour(self) -> float:
        """Net P&L per hour of trading."""
        hours = self.session_duration.total_seconds() / 3600
        if hours < 0.01:  # Less than ~30 seconds
            return 0
        return self.net_pnl / hours


class PnLTracker:
    """
    Tracks realized P&L from trading with clear per-trade visibility.

    Key design principles:
    1. P&L is only realized on SELLS (closing positions)
    2. Every sell shows the profit/loss from that specific trade
    3. Session summary provides running total
    4. Separates gross P&L from fees for clarity
    """

    def __init__(
        self,
        log_interval_seconds: int = 60,
        recent_trades_limit: int = 50,
    ):
        self.log_interval_seconds = log_interval_seconds
        self.recent_trades_limit = recent_trades_limit

        # Session statistics
        self.session = SessionStats()

        # Per-market tracking
        self._market_stats: Dict[str, SessionStats] = {}

        # Recent trades for display (FIFO queue)
        self._recent_trades: deque[TradeResult] = deque(maxlen=recent_trades_limit)

        # Last summary log time
        self._last_summary_time = datetime.utcnow()

        # Market name lookup
        self._market_names: Dict[str, str] = {}

    def set_market_name(self, token_id: str, name: str) -> None:
        """Set human-readable name for a market."""
        self._market_names[token_id] = name

    def _get_market_name(self, token_id: str) -> str:
        """Get market name, falling back to truncated token_id."""
        return self._market_names.get(token_id, token_id[:16] + "...")

    def _get_market_stats(self, token_id: str) -> SessionStats:
        """Get or create stats for a market."""
        if token_id not in self._market_stats:
            self._market_stats[token_id] = SessionStats()
        return self._market_stats[token_id]

    def record_buy(self, fill: Fill) -> None:
        """
        Record a buy trade.

        Buys don't realize P&L - they just add to position.
        We track them for volume/statistics.
        """
        market_name = self._get_market_name(fill.token_id)

        # Update session stats
        self.session.total_buys += 1
        self.session.total_trades += 1
        self.session.buy_volume += fill.size
        self.session.buy_notional += fill.notional
        self.session.total_fees += fill.fee

        # Update market stats
        market_stats = self._get_market_stats(fill.token_id)
        market_stats.total_buys += 1
        market_stats.total_trades += 1
        market_stats.buy_volume += fill.size
        market_stats.buy_notional += fill.notional
        market_stats.total_fees += fill.fee

        logger.info(
            f"BUY  {fill.size:>6.1f} @ {fill.price:.4f} = ${fill.notional:>7.2f} | "
            f"{market_name}"
        )

    def record_sell(
        self,
        fill: Fill,
        avg_entry_price: float,
        position_before: float,
    ) -> Optional[TradeResult]:
        """
        Record a sell trade and calculate realized P&L.

        This is where we realize profit or loss.

        Args:
            fill: The sell Fill
            avg_entry_price: Average price we paid for shares we're selling
            position_before: Position size before this fill

        Returns:
            TradeResult with P&L details
        """
        market_name = self._get_market_name(fill.token_id)

        # Calculate P&L
        shares_sold = min(fill.size, position_before)  # Can't sell more than we have
        if shares_sold <= 0:
            logger.warning(f"Sell with no position? fill.size={fill.size}, position={position_before}")
            return None

        gross_pnl = (fill.price - avg_entry_price) * shares_sold
        net_pnl = gross_pnl - fill.fee
        position_after = position_before - fill.size

        # Create trade result
        result = TradeResult(
            timestamp=fill.timestamp,
            token_id=fill.token_id,
            market_name=market_name,
            shares_sold=shares_sold,
            sell_price=fill.price,
            avg_buy_price=avg_entry_price,
            gross_pnl=gross_pnl,
            fee=fill.fee,
            net_pnl=net_pnl,
            position_remaining=max(0, position_after),
        )

        # Update session stats
        self.session.total_sells += 1
        self.session.total_trades += 1
        self.session.sell_volume += fill.size
        self.session.sell_notional += fill.notional
        self.session.total_fees += fill.fee
        self.session.gross_pnl += gross_pnl
        self.session.net_pnl += net_pnl

        # Track wins/losses
        if net_pnl > 0.001:  # Threshold to avoid float noise
            self.session.winning_trades += 1
            self.session.largest_win = max(self.session.largest_win, net_pnl)
            # Update running average
            n = self.session.winning_trades
            self.session.avg_win = self.session.avg_win * ((n-1)/n) + net_pnl/n
        elif net_pnl < -0.001:
            self.session.losing_trades += 1
            self.session.largest_loss = min(self.session.largest_loss, net_pnl)
            # Update running average
            n = self.session.losing_trades
            self.session.avg_loss = self.session.avg_loss * ((n-1)/n) + net_pnl/n
        else:
            self.session.breakeven_trades += 1

        # Update market stats
        market_stats = self._get_market_stats(fill.token_id)
        market_stats.total_sells += 1
        market_stats.total_trades += 1
        market_stats.sell_volume += fill.size
        market_stats.sell_notional += fill.notional
        market_stats.total_fees += fill.fee
        market_stats.gross_pnl += gross_pnl
        market_stats.net_pnl += net_pnl
        if net_pnl > 0.001:
            market_stats.winning_trades += 1
        elif net_pnl < -0.001:
            market_stats.losing_trades += 1
        else:
            market_stats.breakeven_trades += 1

        # Store in recent trades
        self._recent_trades.append(result)

        # Log the trade result with P&L
        pnl_symbol = "+" if net_pnl >= 0 else ""
        pnl_color = "" if net_pnl >= 0 else ""  # Could add ANSI colors

        logger.info(
            f"SELL {shares_sold:>6.1f} @ {fill.price:.4f} (bought @ {avg_entry_price:.4f}) | "
            f"P&L: {pnl_symbol}${net_pnl:.2f} | "
            f"Session: {pnl_symbol if self.session.net_pnl >= 0 else ''}${self.session.net_pnl:.2f} | "
            f"{market_name}"
        )

        return result

    def record_fill(
        self,
        fill: Fill,
        position: Position,
        position_before_fill: float,
        avg_entry_before: Optional[float] = None,
    ) -> Optional[TradeResult]:
        """
        Record a fill and handle P&L calculation.

        This is the main entry point - call this for every fill.

        Args:
            fill: The Fill object
            position: Position AFTER the fill (with updated avg_entry_price)
            position_before_fill: Position size BEFORE the fill
            avg_entry_before: Average entry price BEFORE the fill (for accurate P&L on sells)

        Returns:
            TradeResult if this was a sell, None for buys
        """
        if fill.side == OrderSide.BUY:
            self.record_buy(fill)
            return None
        else:
            # For sells, use avg_entry_before if provided (more accurate when selling entire position)
            # Falls back to position.avg_entry_price which may be 0 if position is now empty
            entry_price = avg_entry_before if avg_entry_before is not None else position.avg_entry_price
            return self.record_sell(
                fill=fill,
                avg_entry_price=entry_price,
                position_before=position_before_fill,
            )

    def maybe_log_summary(self, force: bool = False) -> bool:
        """
        Log a session summary if enough time has passed.

        Args:
            force: Log even if interval hasn't passed

        Returns:
            True if summary was logged
        """
        now = datetime.utcnow()
        elapsed = (now - self._last_summary_time).total_seconds()

        if not force and elapsed < self.log_interval_seconds:
            return False

        self._last_summary_time = now
        self._log_summary()
        return True

    def _log_summary(self) -> None:
        """Log a comprehensive session summary."""
        s = self.session
        duration = s.session_duration
        hours = duration.total_seconds() / 3600

        # Format duration nicely
        if hours >= 1:
            duration_str = f"{hours:.1f}h"
        else:
            duration_str = f"{duration.total_seconds() / 60:.0f}m"

        # Build summary
        closed_trades = s.winning_trades + s.losing_trades + s.breakeven_trades

        net_symbol = "+" if s.net_pnl >= 0 else ""
        gross_symbol = "+" if s.gross_pnl >= 0 else ""
        hourly_symbol = "+" if s.pnl_per_hour >= 0 else ""

        summary_lines = [
            f"{'='*60}",
            f"SESSION P&L SUMMARY ({duration_str})",
            f"{'='*60}",
            f"Net P&L:     {net_symbol}${s.net_pnl:>8.2f}  ({hourly_symbol}${s.pnl_per_hour:.2f}/hr)",
            f"Gross P&L:   {gross_symbol}${s.gross_pnl:>8.2f}",
            f"Fees Paid:   ${s.total_fees:>8.2f}",
            f"",
            f"Trades:      {s.total_trades:>4} ({s.total_buys} buys, {s.total_sells} sells)",
            f"Win Rate:    {s.win_rate:>5.1f}%  ({s.winning_trades}W / {s.losing_trades}L / {s.breakeven_trades}BE)",
        ]

        if s.winning_trades > 0:
            summary_lines.append(f"Avg Win:     ${s.avg_win:>8.2f}  (best: ${s.largest_win:.2f})")
        if s.losing_trades > 0:
            summary_lines.append(f"Avg Loss:    ${s.avg_loss:>8.2f}  (worst: ${s.largest_loss:.2f})")

        summary_lines.append(f"{'='*60}")

        # Log each line
        for line in summary_lines:
            logger.info(line)

    def get_recent_trades(self, limit: int = 10) -> List[TradeResult]:
        """Get the most recent trades."""
        trades = list(self._recent_trades)
        return trades[-limit:]

    def get_market_summary(self, token_id: str) -> Optional[Dict]:
        """Get summary for a specific market."""
        stats = self._market_stats.get(token_id)
        if not stats:
            return None

        return {
            "market": self._get_market_name(token_id),
            "trades": stats.total_trades,
            "net_pnl": stats.net_pnl,
            "gross_pnl": stats.gross_pnl,
            "fees": stats.total_fees,
            "win_rate": stats.win_rate,
            "volume": stats.buy_volume + stats.sell_volume,
        }

    def get_session_summary(self) -> Dict:
        """Get complete session summary as dict."""
        s = self.session
        return {
            "duration_seconds": s.session_duration.total_seconds(),
            "net_pnl": s.net_pnl,
            "gross_pnl": s.gross_pnl,
            "total_fees": s.total_fees,
            "total_trades": s.total_trades,
            "buys": s.total_buys,
            "sells": s.total_sells,
            "winning_trades": s.winning_trades,
            "losing_trades": s.losing_trades,
            "win_rate": s.win_rate,
            "pnl_per_hour": s.pnl_per_hour,
            "avg_win": s.avg_win,
            "avg_loss": s.avg_loss,
            "largest_win": s.largest_win,
            "largest_loss": s.largest_loss,
            "buy_volume": s.buy_volume,
            "sell_volume": s.sell_volume,
            "buy_notional": s.buy_notional,
            "sell_notional": s.sell_notional,
            "markets": {
                token_id: self.get_market_summary(token_id)
                for token_id in self._market_stats
            },
        }

    def reset(self) -> None:
        """Reset all statistics."""
        self.session = SessionStats()
        self._market_stats.clear()
        self._recent_trades.clear()
        self._last_summary_time = datetime.utcnow()
        logger.info("PnL tracker reset")
