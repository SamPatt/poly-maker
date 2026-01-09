"""
15-Minute Crypto Market Maker Rebates Bot

This bot runs alongside the main trading bot to capture maker rebates
on Polymarket's 15-minute crypto Up/Down markets.

Strategy:
- Find upcoming 15-minute BTC/ETH/SOL markets
- Place delta-neutral orders (both Up and Down at 50%)
- Earn maker rebates when takers fill orders
- Repeat for each 15-minute cycle

CRITICAL: Only trades on UPCOMING markets, never LIVE.
"""
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Set

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

        # Track markets we've already placed orders on
        self.traded_markets: Set[str] = set()

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

    def process_market(self, market: dict) -> bool:
        """
        Process a single market: verify safety and place orders.

        Returns:
            True if orders were placed successfully
        """
        slug = market.get("slug", "unknown")
        question = market.get("question", "Unknown")

        # Skip if we've already traded this market
        if slug in self.traded_markets:
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
            self.traded_markets.add(slug)
            self.log(f"SUCCESS: {message}")
        else:
            self.log(f"FAILED: {message}")

        return success

    def cleanup_old_markets(self):
        """Remove markets from tracking that are now resolved."""
        # Keep last 100 markets to prevent memory growth
        if len(self.traded_markets) > 100:
            # Convert to list, sort, keep last 100
            sorted_markets = sorted(self.traded_markets)
            self.traded_markets = set(sorted_markets[-100:])
            self.log(f"Cleaned up traded markets cache")

    def run_once(self) -> int:
        """
        Run one cycle of market discovery and order placement.

        Returns:
            Number of markets successfully traded
        """
        self.log("Scanning for upcoming markets...")

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

        self.cleanup_old_markets()
        return traded_count

    def run(self):
        """
        Main loop - continuously find markets and place orders.

        Runs indefinitely until interrupted.
        """
        self.log("Starting main loop...")
        self.log(f"Checking for markets every {CHECK_INTERVAL_SECONDS}s")

        while True:
            try:
                traded = self.run_once()

                if traded > 0:
                    self.log(f"Traded {traded} markets this cycle")

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
