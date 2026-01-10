"""
15-Minute Crypto Market Maker Rebates Bot

This bot runs alongside the main trading bot to capture maker rebates
on Polymarket's 15-minute crypto Up/Down markets.

Strategy:
- Find upcoming 15-minute BTC/ETH/SOL markets
- Place delta-neutral orders (both Up and Down at 50%)
- Earn maker rebates when takers fill orders
- Repeat for each 15-minute cycle

Economics:
- Buy UP at $0.50 + Buy DOWN at $0.50 = $1 total per share pair
- At resolution: one side worth $1, other worth $0 = get $1 back
- Net P&L on position: $0 (wash)
- Profit: maker rebates (~1.56% at 50% probability)

CRITICAL: Only places orders on UPCOMING markets, never LIVE.
Orders stay open when market goes LIVE - that's when takers fill them.
"""
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Set, Optional, Any
from dataclasses import dataclass, field

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from poly_data.polymarket_client import PolymarketClient

from .market_finder import CryptoMarketFinder
from .strategy import DeltaNeutralStrategy
from .config import (
    TRADE_SIZE,
    TARGET_PRICE,
    DRY_RUN,
    CHECK_INTERVAL_SECONDS,
    SAFETY_BUFFER_SECONDS,
    ASSETS,
)


@dataclass
class TrackedMarket:
    """Track a market we've placed orders on."""
    slug: str
    question: str
    event_start: datetime
    up_token: str
    down_token: str
    order_time: datetime
    status: str = "UPCOMING"  # UPCOMING -> LIVE -> RESOLVED
    up_filled: bool = False
    down_filled: bool = False
    logged_live: bool = False
    logged_resolved: bool = False


class RebatesBot:
    """
    15-minute crypto market maker rebates bot.

    Continuously finds upcoming 15-minute markets and places
    delta-neutral orders to capture maker rebates.
    """

    def __init__(self):
        print("=" * 60)
        print("15-MINUTE CRYPTO REBATES BOT")
        print("=" * 60)
        print(f"Dry Run Mode: {DRY_RUN}")
        print(f"Trade Size: ${TRADE_SIZE} per side")
        print(f"Target Price: {TARGET_PRICE}")
        print(f"Safety Buffer: {SAFETY_BUFFER_SECONDS}s")
        print(f"Assets: {', '.join(ASSETS).upper()}")
        print("=" * 60)

        if not DRY_RUN:
            print("\n*** LIVE TRADING MODE - REAL ORDERS WILL BE PLACED ***\n")

        # Track markets with full details
        self.tracked_markets: Dict[str, TrackedMarket] = {}

        # Initialize components
        print("Initializing Polymarket client...")
        self.client = PolymarketClient()

        print("Initializing market finder...")
        self.finder = CryptoMarketFinder()

        print("Initializing strategy...")
        self.strategy = DeltaNeutralStrategy(self.client, TRADE_SIZE)

        print("Bot initialized successfully.\n")

    def log(self, message: str):
        """Log with timestamp."""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"[{timestamp}] {message}")

    def check_order_fills(self, tracked: TrackedMarket) -> None:
        """Check if orders for a tracked market have been filled."""
        if DRY_RUN:
            return

        try:
            all_orders = self.client.get_all_orders()
            if all_orders.empty:
                # No open orders - assume filled or cancelled
                if not tracked.up_filled:
                    tracked.up_filled = True
                if not tracked.down_filled:
                    tracked.down_filled = True
                return

            # Check for open orders on our tokens
            up_open = not all_orders[all_orders["asset_id"] == tracked.up_token].empty
            down_open = not all_orders[all_orders["asset_id"] == tracked.down_token].empty

            # If order is no longer open, it was filled (or cancelled)
            if not up_open and not tracked.up_filled:
                tracked.up_filled = True
                self.log(f"  UP order filled: {tracked.question}")
            if not down_open and not tracked.down_filled:
                tracked.down_filled = True
                self.log(f"  DOWN order filled: {tracked.question}")

        except Exception as e:
            pass  # Don't spam logs on API errors

    def check_market_status(self, tracked: TrackedMarket) -> None:
        """Check and update market status (UPCOMING -> LIVE -> RESOLVED)."""
        now = datetime.now(timezone.utc)

        # Check if market has gone LIVE
        if tracked.status == "UPCOMING" and now >= tracked.event_start:
            tracked.status = "LIVE"
            if not tracked.logged_live:
                self.log(f"MARKET LIVE: {tracked.question}")
                tracked.logged_live = True

        # Check if market has RESOLVED (15 min after start)
        resolution_time = tracked.event_start + timedelta(minutes=15)
        if tracked.status == "LIVE" and now >= resolution_time:
            tracked.status = "RESOLVED"
            if not tracked.logged_resolved:
                self.log(f"MARKET RESOLVED: {tracked.question}")
                # Log final fill status
                if DRY_RUN:
                    self.log(f"  [DRY RUN] Would have held UP + DOWN until resolution")
                else:
                    up_status = "FILLED" if tracked.up_filled else "UNFILLED"
                    down_status = "FILLED" if tracked.down_filled else "UNFILLED"
                    self.log(f"  UP: {up_status}, DOWN: {down_status}")
                    if tracked.up_filled and tracked.down_filled:
                        self.log(f"  Both sides filled - earned rebates on ${TRADE_SIZE * 2:.2f} volume")
                    elif tracked.up_filled or tracked.down_filled:
                        self.log(f"  Partial fill - one side only")
                    else:
                        self.log(f"  No fills - orders expired")
                tracked.logged_resolved = True

    def monitor_tracked_markets(self) -> None:
        """Monitor all tracked markets for status changes and fills."""
        for slug, tracked in list(self.tracked_markets.items()):
            # Update status
            self.check_market_status(tracked)

            # Check fills for LIVE markets
            if tracked.status == "LIVE":
                self.check_order_fills(tracked)

        # Cleanup old resolved markets (keep last 50)
        resolved = [s for s, t in self.tracked_markets.items() if t.status == "RESOLVED"]
        if len(resolved) > 50:
            for slug in resolved[:len(resolved) - 50]:
                del self.tracked_markets[slug]

    def process_market(self, market: dict) -> bool:
        """
        Process a single market: verify safety and place orders.

        Returns:
            True if orders were placed successfully
        """
        slug = market.get("slug", "unknown")
        question = market.get("question", "Unknown")

        # Skip if we've already traded this market
        if slug in self.tracked_markets:
            self.log(f"Already traded: {slug}")
            return False

        # Double-check safety (market finder already checked, but be safe)
        if not self.finder.is_safe_to_trade(market):
            self.log(f"Market not safe: {slug}")
            return False

        # Log market info
        event_start = market.get("_event_start")
        if event_start:
            now = datetime.now(timezone.utc)
            time_until = (event_start - now).total_seconds()
            self.log(f"Processing: {question}")
            self.log(f"  Starts in: {time_until:.0f}s")
        else:
            self.log(f"Processing: {question}")

        # Place mirror orders
        success, message = self.strategy.place_mirror_orders(market)

        if success:
            # Track this market
            try:
                up_token, down_token = self.strategy.get_tokens(market)
            except Exception:
                up_token, down_token = "", ""

            self.tracked_markets[slug] = TrackedMarket(
                slug=slug,
                question=question,
                event_start=event_start or datetime.now(timezone.utc),
                up_token=up_token,
                down_token=down_token,
                order_time=datetime.now(timezone.utc),
            )
            self.log(f"SUCCESS: {message}")
        else:
            self.log(f"FAILED: {message}")

        return success

    def run_once(self) -> int:
        """
        Run one cycle of market discovery and order placement.

        Returns:
            Number of markets successfully traded
        """
        self.log("Scanning for upcoming markets...")

        # First, monitor existing tracked markets
        self.monitor_tracked_markets()

        # Find new upcoming markets
        markets = self.finder.get_upcoming_markets()
        self.log(f"Found {len(markets)} upcoming markets")

        if not markets:
            return 0

        traded_count = 0
        for market in markets:
            # Check timing before each trade
            if not self.finder.is_safe_to_trade(market):
                continue

            if self.process_market(market):
                traded_count += 1

            # Small delay between orders
            time.sleep(0.5)

        return traded_count

    def log_status_summary(self) -> None:
        """Log a summary of tracked markets by status."""
        upcoming = sum(1 for t in self.tracked_markets.values() if t.status == "UPCOMING")
        live = sum(1 for t in self.tracked_markets.values() if t.status == "LIVE")
        resolved = sum(1 for t in self.tracked_markets.values() if t.status == "RESOLVED")

        if upcoming or live:
            self.log(f"Tracking: {upcoming} UPCOMING, {live} LIVE, {resolved} RESOLVED")

    def run(self):
        """
        Main loop - continuously find markets and place orders.

        Runs indefinitely until interrupted.
        """
        self.log("Starting main loop...")
        self.log(f"Checking for markets every {CHECK_INTERVAL_SECONDS}s")

        cycle_count = 0
        while True:
            try:
                traded = self.run_once()

                if traded > 0:
                    self.log(f"Traded {traded} markets this cycle")

                # Log status summary every 5 cycles
                cycle_count += 1
                if cycle_count % 5 == 0:
                    self.log_status_summary()

                # Wait before next check
                self.log(f"Sleeping for {CHECK_INTERVAL_SECONDS}s...")
                time.sleep(CHECK_INTERVAL_SECONDS)

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


def main():
    """Entry point for the rebates bot."""
    bot = RebatesBot()
    bot.run()


if __name__ == "__main__":
    main()
