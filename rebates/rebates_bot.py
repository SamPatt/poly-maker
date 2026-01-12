"""
15-Minute Crypto Market Maker Rebates Bot

"Only 50" Strategy:
- Place BUY orders at exactly 0.50 on both UP and DOWN sides
- Let orders sit until they fill (no competitive updates, no rescue)
- When price crosses 50, orders fill automatically
- The only failure mode is execution risk (order doesn't fill when price crosses)

This is dramatically simpler than the previous approach because:
1. If both sides fill at 50: delta-neutral, earn rebates on both
2. If one side fills: order at 50 on other side will fill when outcome crosses 50
3. No need to chase, update, or rescue - the order at 50 IS the rescue

Position imbalance is handled by skipping the overweight side on new markets.
"""
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, Tuple
from dataclasses import dataclass

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from poly_data.polymarket_client import PolymarketClient

from .market_finder import CryptoMarketFinder
from .strategy import DeltaNeutralStrategy, OrderResult
from .config import (
    TRADE_SIZE,
    FIXED_PRICE,
    DRY_RUN,
    CHECK_INTERVAL_SECONDS,
    SAFETY_BUFFER_SECONDS,
    ASSETS,
    MAX_POSITION_IMBALANCE,
)
from alerts.telegram import (
    send_rebates_startup_alert,
    send_rebates_order_alert,
    send_rebates_resolution_alert,
    send_rebates_redemption_alert,
    send_rebates_fill_alert,
)
from db.supabase_client import (
    save_rebates_market,
    update_rebates_market_status,
    update_rebates_market_fills,
    get_pending_rebates_markets,
    mark_rebates_market_redeemed,
    cleanup_old_rebates_markets,
)
from redemption import redeem_position


@dataclass
class TrackedMarket:
    """Track a market we've placed orders on."""
    slug: str
    question: str
    event_start: datetime
    up_token: str
    down_token: str
    order_time: datetime
    condition_id: str = ""
    status: str = "UPCOMING"  # UPCOMING -> LIVE -> RESOLVED -> REDEEMED
    up_filled: bool = False
    down_filled: bool = False
    up_order_placed: bool = True  # False if skipped due to imbalance
    down_order_placed: bool = True  # False if skipped due to imbalance
    logged_live: bool = False
    logged_resolved: bool = False
    redeemed: bool = False
    redeem_attempted: bool = False
    neg_risk: bool = False
    tick_size: float = 0.01


@dataclass
class ExecutionStats:
    """Track execution reliability metrics."""
    markets_entered: int = 0
    both_sides_filled: int = 0
    one_side_filled: int = 0
    no_fills: int = 0

    def log_summary(self):
        """Log execution stats summary."""
        if self.markets_entered == 0:
            return
        both_rate = (self.both_sides_filled / self.markets_entered) * 100
        one_rate = (self.one_side_filled / self.markets_entered) * 100
        none_rate = (self.no_fills / self.markets_entered) * 100
        print(f"Execution Stats: {self.markets_entered} markets | "
              f"Both filled: {both_rate:.1f}% | One filled: {one_rate:.1f}% | None: {none_rate:.1f}%")


class RebatesBot:
    """
    15-minute crypto market maker rebates bot using "Only 50" strategy.

    Continuously finds upcoming 15-minute markets and places
    delta-neutral orders at 0.50 to capture maker rebates.
    """

    def __init__(self):
        print("=" * 60)
        print("15-MINUTE CRYPTO REBATES BOT")
        print("Strategy: Only 50")
        print("=" * 60)
        print(f"Dry Run Mode: {DRY_RUN}")
        print(f"Trade Size: ${TRADE_SIZE} per side")
        print(f"Fixed Price: {FIXED_PRICE}")
        print(f"Safety Buffer: {SAFETY_BUFFER_SECONDS}s")
        print(f"Assets: {', '.join(ASSETS).upper()}")
        print("=" * 60)

        if not DRY_RUN:
            print("\n*** LIVE TRADING MODE - REAL ORDERS WILL BE PLACED ***\n")

        # Track markets
        self.tracked_markets: Dict[str, TrackedMarket] = {}

        # Execution reliability stats
        self.stats = ExecutionStats()

        # Initialize components
        print("Initializing Polymarket client...")
        self.client = PolymarketClient()

        print("Initializing market finder...")
        self.finder = CryptoMarketFinder()

        print("Initializing strategy...")
        self.strategy = DeltaNeutralStrategy(self.client, TRADE_SIZE)

        print("Bot initialized successfully.\n")

        # Load pending markets from database
        self._load_pending_markets()

        # Cleanup old markets
        deleted = cleanup_old_rebates_markets(days=7)
        if deleted > 0:
            print(f"Cleaned up {deleted} old redeemed markets from database")

        # Send startup alert
        send_rebates_startup_alert(DRY_RUN, TRADE_SIZE)

    def get_position_imbalance(self) -> Tuple[float, float, float]:
        """
        Calculate total Up and Down positions across all tracked 15-min markets.

        Returns:
            Tuple of (up_total, down_total, imbalance) where:
            - up_total: Total shares of Up positions
            - down_total: Total shares of Down positions
            - imbalance: up_total - down_total (positive = long Up)
        """
        if DRY_RUN:
            return 0.0, 0.0, 0.0

        try:
            positions_df = self.client.get_all_positions()

            if positions_df.empty:
                return 0.0, 0.0, 0.0

            up_tokens = {t.up_token for t in self.tracked_markets.values() if t.up_token}
            down_tokens = {t.down_token for t in self.tracked_markets.values() if t.down_token}

            up_total = 0.0
            down_total = 0.0

            for _, row in positions_df.iterrows():
                asset_id = str(row.get("asset", ""))
                size = float(row.get("size", 0))

                if size <= 0:
                    continue

                if asset_id in up_tokens:
                    up_total += size
                elif asset_id in down_tokens:
                    down_total += size

            return up_total, down_total, up_total - down_total

        except Exception as e:
            self.log(f"Error getting position imbalance: {e}")
            return 0.0, 0.0, 0.0

    def _load_pending_markets(self) -> None:
        """Load markets from database that need continued tracking."""
        try:
            pending_df = get_pending_rebates_markets()
            if pending_df.empty:
                print("No pending markets to resume from database")
                return

            loaded = 0
            for _, row in pending_df.iterrows():
                slug = row.get("slug")
                if not slug or slug in self.tracked_markets:
                    continue

                event_start = row.get("event_start")
                if isinstance(event_start, str):
                    event_start = datetime.fromisoformat(event_start.replace("Z", "+00:00"))
                elif hasattr(event_start, 'tzinfo') and event_start.tzinfo is None:
                    event_start = event_start.replace(tzinfo=timezone.utc)

                tracked = TrackedMarket(
                    slug=slug,
                    question=row.get("question", "Unknown"),
                    event_start=event_start,
                    up_token=row.get("up_token", ""),
                    down_token=row.get("down_token", ""),
                    order_time=row.get("order_time", datetime.now(timezone.utc)),
                    condition_id=row.get("condition_id", ""),
                    status=row.get("status", "UPCOMING"),
                    up_filled=bool(row.get("up_filled", False)),
                    down_filled=bool(row.get("down_filled", False)),
                    neg_risk=bool(row.get("neg_risk", False)),
                    tick_size=float(row.get("tick_size", 0.01)),
                    logged_live=row.get("status") in ("LIVE", "RESOLVED"),
                    logged_resolved=row.get("status") == "RESOLVED",
                    redeemed=bool(row.get("redeemed", False)),
                )

                self.tracked_markets[slug] = tracked
                loaded += 1

            if loaded > 0:
                print(f"Resumed tracking {loaded} markets from database")

        except Exception as e:
            print(f"Error loading pending markets: {e}")
            import traceback
            traceback.print_exc()

    def log(self, message: str):
        """Log with timestamp."""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"[{timestamp}] {message}")

    def check_order_fills(self, tracked: TrackedMarket) -> None:
        """
        Check if orders for a tracked market have been filled.

        Updates tracked.up_filled and tracked.down_filled.
        """
        if DRY_RUN:
            return

        try:
            all_orders = self.client.get_all_orders()

            # Check UP
            if tracked.up_order_placed and not tracked.up_filled:
                if all_orders.empty:
                    up_open = False
                else:
                    up_open = not all_orders[all_orders["asset_id"] == tracked.up_token].empty

                if not up_open:
                    tracked.up_filled = True
                    update_rebates_market_fills(tracked.slug, up_filled=True)
                    self.log(f"  UP filled: {tracked.question[:50]}...")
                    send_rebates_fill_alert(
                        question=tracked.question,
                        side="UP",
                        price=FIXED_PRICE,
                        size=TRADE_SIZE,
                        dry_run=DRY_RUN
                    )

            # Check DOWN
            if tracked.down_order_placed and not tracked.down_filled:
                if all_orders.empty:
                    down_open = False
                else:
                    down_open = not all_orders[all_orders["asset_id"] == tracked.down_token].empty

                if not down_open:
                    tracked.down_filled = True
                    update_rebates_market_fills(tracked.slug, down_filled=True)
                    self.log(f"  DOWN filled: {tracked.question[:50]}...")
                    send_rebates_fill_alert(
                        question=tracked.question,
                        side="DOWN",
                        price=FIXED_PRICE,
                        size=TRADE_SIZE,
                        dry_run=DRY_RUN
                    )

        except Exception:
            pass  # Don't spam logs on API errors

    def check_market_status(self, tracked: TrackedMarket) -> None:
        """Check and update market status (UPCOMING -> LIVE -> RESOLVED)."""
        now = datetime.now(timezone.utc)

        # UPCOMING -> LIVE
        if tracked.status == "UPCOMING" and now >= tracked.event_start:
            tracked.status = "LIVE"
            update_rebates_market_status(tracked.slug, "LIVE")
            if not tracked.logged_live:
                self.log(f"MARKET LIVE: {tracked.question}")
                tracked.logged_live = True

        # LIVE -> RESOLVED (15 min after start)
        resolution_time = tracked.event_start + timedelta(minutes=15)
        if tracked.status == "LIVE" and now >= resolution_time:
            tracked.status = "RESOLVED"
            update_rebates_market_status(tracked.slug, "RESOLVED")
            if not tracked.logged_resolved:
                self.log(f"MARKET RESOLVED: {tracked.question}")

                # Update execution stats
                self.stats.markets_entered += 1
                if tracked.up_filled and tracked.down_filled:
                    self.stats.both_sides_filled += 1
                    self.log(f"  Both sides filled - earned rebates on ${TRADE_SIZE * 2:.2f}")
                elif tracked.up_filled or tracked.down_filled:
                    self.stats.one_side_filled += 1
                    filled_side = "UP" if tracked.up_filled else "DOWN"
                    self.log(f"  One side filled ({filled_side}) - partial rebates")
                else:
                    self.stats.no_fills += 1
                    self.log(f"  No fills - orders expired")

                tracked.logged_resolved = True

                send_rebates_resolution_alert(
                    question=tracked.question,
                    up_filled=tracked.up_filled,
                    down_filled=tracked.down_filled,
                    trade_size=TRADE_SIZE,
                    dry_run=DRY_RUN
                )

    def attempt_redemption(self, tracked: TrackedMarket) -> bool:
        """Attempt to redeem winning positions for a resolved market."""
        if tracked.redeemed or tracked.redeem_attempted:
            return True

        if not tracked.condition_id:
            tracked.redeem_attempted = True
            return False

        if not tracked.up_filled and not tracked.down_filled:
            tracked.redeem_attempted = True
            return True

        if DRY_RUN:
            tracked.redeem_attempted = True
            send_rebates_redemption_alert(
                question=tracked.question,
                condition_id=tracked.condition_id,
                dry_run=True
            )
            return True

        # Wait 60s after resolution for blockchain confirmation
        resolution_time = tracked.event_start + timedelta(minutes=15)
        time_since_resolution = (datetime.now(timezone.utc) - resolution_time).total_seconds()
        if time_since_resolution < 60:
            return False

        self.log(f"  Redeeming: {tracked.slug}")

        def on_success(condition_id: str, tx_hash: str):
            self.log(f"  Redemption successful! TX: {tx_hash[:20] if tx_hash else 'unknown'}...")
            tracked.redeemed = True
            tracked.redeem_attempted = True
            tracked.status = "REDEEMED"
            mark_rebates_market_redeemed(tracked.slug)
            send_rebates_redemption_alert(
                question=tracked.question,
                condition_id=tracked.condition_id,
                dry_run=False
            )

        def on_error(condition_id: str, error_msg: str):
            self.log(f"  Redemption failed: {error_msg[:100]}")
            tracked.redeem_attempted = True

        redeem_position(
            tracked.condition_id,
            on_success=on_success,
            on_error=on_error,
            blocking=False
        )

        tracked.redeem_attempted = True
        return True

    def monitor_tracked_markets(self) -> None:
        """Monitor all tracked markets for status changes and fills."""
        for slug, tracked in list(self.tracked_markets.items()):
            # Check fills
            self.check_order_fills(tracked)

            # Update status
            self.check_market_status(tracked)

            # Attempt redemption for resolved markets
            if tracked.status == "RESOLVED":
                self.attempt_redemption(tracked)

        # Cleanup old redeemed markets
        redeemed = [s for s, t in self.tracked_markets.items() if t.status == "REDEEMED"]
        if len(redeemed) > 50:
            for slug in redeemed[:len(redeemed) - 50]:
                del self.tracked_markets[slug]

    def process_market(self, market: dict) -> bool:
        """
        Process a single market: verify safety and place orders.

        Returns True if orders were placed successfully.
        """
        slug = market.get("slug", "unknown")
        question = market.get("question", "Unknown")

        if slug in self.tracked_markets:
            return False

        if not self.finder.is_safe_to_trade(market):
            return False

        event_start = market.get("_event_start")
        if event_start:
            now = datetime.now(timezone.utc)
            time_until = (event_start - now).total_seconds()
            self.log(f"Processing: {question}")
            self.log(f"  Starts in: {time_until:.0f}s")

        # Check position imbalance
        up_total, down_total, imbalance = self.get_position_imbalance()
        skip_up = False
        skip_down = False

        if abs(imbalance) > MAX_POSITION_IMBALANCE:
            if imbalance > 0:
                skip_up = True
                self.log(f"  IMBALANCE: Up={up_total:.1f} Down={down_total:.1f} - skipping Up")
            else:
                skip_down = True
                self.log(f"  IMBALANCE: Up={up_total:.1f} Down={down_total:.1f} - skipping Down")
        elif abs(imbalance) > 0:
            self.log(f"  Position: Up={up_total:.1f} Down={down_total:.1f} (within threshold)")

        # Place orders at 0.50
        result = self.strategy.place_mirror_orders(market, skip_up=skip_up, skip_down=skip_down)

        if result.success:
            try:
                up_token, down_token = self.strategy.get_tokens(market)
            except Exception:
                up_token, down_token = "", ""

            condition_id = market.get("conditionId", "")
            neg_risk = self.strategy.is_neg_risk_market(market)
            tick_size = float(market.get("orderPriceMinTickSize", 0.01))

            self.tracked_markets[slug] = TrackedMarket(
                slug=slug,
                question=question,
                event_start=event_start or datetime.now(timezone.utc),
                up_token=up_token,
                down_token=down_token,
                order_time=datetime.now(timezone.utc),
                condition_id=condition_id,
                up_order_placed=not skip_up,
                down_order_placed=not skip_down,
                neg_risk=neg_risk,
                tick_size=tick_size,
            )

            self.log(f"SUCCESS: {result.message}")

            # Persist to database
            save_rebates_market(
                slug=slug,
                question=question,
                condition_id=condition_id,
                up_token=up_token,
                down_token=down_token,
                event_start=(event_start or datetime.now(timezone.utc)).isoformat(),
                up_price=FIXED_PRICE,
                down_price=FIXED_PRICE,
                neg_risk=neg_risk,
                tick_size=tick_size
            )

            send_rebates_order_alert(
                question=question,
                trade_size=TRADE_SIZE,
                price=FIXED_PRICE,
                dry_run=DRY_RUN
            )
        else:
            self.log(f"FAILED: {result.message}")

        return result.success

    def run_once(self) -> int:
        """Run one cycle of market discovery and order placement."""
        self.log("Scanning for upcoming markets...")

        # Monitor existing markets
        self.monitor_tracked_markets()

        # Find new markets
        markets = self.finder.get_upcoming_markets()
        self.log(f"Found {len(markets)} upcoming markets")

        if not markets:
            return 0

        traded_count = 0
        for market in markets:
            if not self.finder.is_safe_to_trade(market):
                continue

            if self.process_market(market):
                traded_count += 1

            time.sleep(0.5)

        return traded_count

    def log_status_summary(self) -> None:
        """Log a summary of tracked markets by status."""
        upcoming = sum(1 for t in self.tracked_markets.values() if t.status == "UPCOMING")
        live = sum(1 for t in self.tracked_markets.values() if t.status == "LIVE")
        resolved = sum(1 for t in self.tracked_markets.values() if t.status == "RESOLVED")
        redeemed = sum(1 for t in self.tracked_markets.values() if t.status == "REDEEMED")

        if upcoming or live or resolved:
            self.log(f"Tracking: {upcoming} UPCOMING, {live} LIVE, {resolved} RESOLVED, {redeemed} REDEEMED")

        up_total, down_total, imbalance = self.get_position_imbalance()
        if up_total > 0 or down_total > 0:
            status = "BALANCED" if abs(imbalance) <= MAX_POSITION_IMBALANCE else "IMBALANCED"
            self.log(f"Positions: Up={up_total:.1f} Down={down_total:.1f} Imbalance={imbalance:+.1f} [{status}]")

        # Log execution stats periodically
        self.stats.log_summary()

    def run(self):
        """
        Main loop - continuously find markets and place orders.

        Simplified from original: no competitive updates, no rescue.
        Just monitor fills and redemptions.
        """
        self.log("Starting main loop...")
        self.log(f"Market scan every {CHECK_INTERVAL_SECONDS}s")
        self.log(f"Fill check every 15s")

        FILL_CHECK_INTERVAL = 15
        cycle_count = 0
        last_full_scan = 0

        while True:
            try:
                now = time.time()

                if now - last_full_scan >= CHECK_INTERVAL_SECONDS:
                    traded = self.run_once()
                    if traded > 0:
                        self.log(f"Traded {traded} markets this cycle")

                    cycle_count += 1
                    if cycle_count % 5 == 0:
                        self.log_status_summary()

                    last_full_scan = now
                else:
                    # Quick check - just monitor fills
                    self.monitor_tracked_markets()

                time.sleep(FILL_CHECK_INTERVAL)

            except KeyboardInterrupt:
                self.log("Interrupted by user, shutting down...")
                break
            except Exception as e:
                self.log(f"Error in main loop: {e}")
                import traceback
                traceback.print_exc()
                self.log("Retrying in 30s...")
                time.sleep(30)

        self.log("Bot stopped.")
        self.stats.log_summary()


def main():
    """Entry point for the rebates bot."""
    bot = RebatesBot()
    bot.run()


if __name__ == "__main__":
    main()
