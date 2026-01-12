#!/usr/bin/env python3
"""
Quick one-shot scan for Gabagool opportunities.

Usage:
    python -m rebates.gabagool.scan_once
"""

import asyncio
import logging
from datetime import datetime, timezone

from ..market_finder import CryptoMarketFinder
from .scanner import GabagoolScanner
from .monitor import GabagoolMonitor
from . import config

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    print("\n" + "=" * 60)
    print("GABAGOOL SCANNER - One-Shot Scan")
    print("=" * 60)
    print(f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"Profit threshold: {config.PROFIT_THRESHOLD} (combined YES+NO must be below this)")
    print(f"Min liquidity: {config.MIN_LIQUIDITY} shares")
    print(f"Trade size: ${config.TRADE_SIZE}")
    print("=" * 60 + "\n")

    # Find upcoming markets
    print("Finding upcoming 15-minute crypto markets...")
    finder = CryptoMarketFinder()
    markets = finder.get_upcoming_markets()

    if not markets:
        print("No upcoming markets found.")
        return

    print(f"Found {len(markets)} upcoming markets:\n")
    for m in markets:
        info = finder.get_market_info(m)
        print(f"  - {info}")

    print("\n" + "-" * 60)
    print("Scanning orderbooks for arbitrage opportunities...")
    print("-" * 60 + "\n")

    # Create scanner to check spreads
    scanner = GabagoolScanner()

    # Show current spreads for each market
    print("Current market spreads (YES + NO prices):\n")
    for m in markets:
        slug = m.get("slug", "unknown")
        clob_tokens = m.get("clobTokenIds", [])
        if isinstance(clob_tokens, str):
            import json
            clob_tokens = json.loads(clob_tokens)

        if len(clob_tokens) >= 2:
            up_token, down_token = clob_tokens[0], clob_tokens[1]

            # Fetch orderbooks
            up_book = scanner._fetch_orderbook(up_token)
            down_book = scanner._fetch_orderbook(down_token)

            if up_book and down_book:
                up_price, up_size = scanner._get_best_ask(up_book)
                down_price, down_size = scanner._get_best_ask(down_book)

                if up_price and down_price:
                    combined = up_price + down_price
                    spread_from_1 = (1.0 - combined) * 100

                    # Highlight if close to profitable
                    if combined < config.PROFIT_THRESHOLD:
                        marker = "  <-- OPPORTUNITY!"
                    elif combined < 1.0:
                        marker = f"  ({spread_from_1:.2f}% from $1.00)"
                    else:
                        marker = f"  (+{-spread_from_1:.2f}% above $1.00)"

                    asset = m.get("_asset", "???")
                    print(f"  {asset}: UP={up_price:.3f} + DOWN={down_price:.3f} = {combined:.4f}{marker}")
                else:
                    print(f"  {slug}: No asks available")
            else:
                print(f"  {slug}: Could not fetch orderbooks")

    print()

    # Create monitor and do one scan
    monitor = GabagoolMonitor(market_finder=finder)
    opportunities = asyncio.get_event_loop().run_until_complete(monitor.scan_once())

    if not opportunities:
        print("No arbitrage opportunities found at this time.")
        print("\nThis is normal - opportunities are rare and fleeting.")
        print(f"The threshold requires YES + NO < {config.PROFIT_THRESHOLD}")
        print("Most markets have YES + NO ≈ 1.00 (no arbitrage)")
    else:
        print(f"\n{'!'*60}")
        print(f"FOUND {len(opportunities)} OPPORTUNITY(IES)!")
        print(f"{'!'*60}")

        for opp in opportunities:
            monitor._log_opportunity(opp)

    print("\n" + "=" * 60)
    print("Scan complete.")
    print(f"Scans performed: {monitor.scans_performed}")
    print(f"Opportunities detected: {monitor.opportunities_detected}")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
