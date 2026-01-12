#!/usr/bin/env python3
"""
Gabagool Strategy Runner

Runs the full Gabagool arbitrage system:
- Scans 15-minute crypto markets for YES+NO < $1.00 opportunities
- Executes paired orders when profitable spread detected
- Manages positions through settlement or merge

Usage:
    # Dry run (simulates trades, no real orders)
    python -m rebates.gabagool.run

    # Live trading (requires proper configuration)
    GABAGOOL_DRY_RUN=false python -m rebates.gabagool.run

    # Detection only (no execution)
    python -m rebates.gabagool.run --detect-only

Environment Variables:
    GABAGOOL_DRY_RUN=true          - Simulate trades (default: true)
    GABAGOOL_PROFIT_THRESHOLD=0.99 - Max combined cost to trade
    GABAGOOL_TRADE_SIZE=50         - Position size per opportunity
    GABAGOOL_SCAN_INTERVAL=1.0     - Seconds between scans
"""

import asyncio
import argparse
import logging
import signal
import sys
from datetime import datetime, timezone

from .monitor import GabagoolMonitor
from . import config

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger(__name__)


def print_banner():
    """Print startup banner."""
    print()
    print("=" * 60)
    print("  GABAGOOL ARBITRAGE STRATEGY")
    print("  Polymarket 15-Minute Crypto Markets")
    print("=" * 60)
    print()
    print(f"  Time:             {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"  Mode:             {'DRY RUN' if config.DRY_RUN else 'LIVE TRADING'}")
    print(f"  Profit Threshold: {config.PROFIT_THRESHOLD} (combined YES+NO)")
    print(f"  Trade Size:       ${config.TRADE_SIZE}")
    print(f"  Scan Interval:    {config.SCAN_INTERVAL}s")
    print(f"  Min Liquidity:    {config.MIN_LIQUIDITY} shares")
    print()
    if config.DRY_RUN:
        print("  *** DRY RUN - No real orders will be placed ***")
    else:
        print("  !!! LIVE TRADING - Real orders will be placed !!!")
    print()
    print("=" * 60)
    print()


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Run the Gabagool arbitrage strategy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python -m rebates.gabagool.run                    # Dry run
    python -m rebates.gabagool.run --detect-only      # Detection only
    GABAGOOL_DRY_RUN=false python -m rebates.gabagool.run  # Live trading
        """
    )
    parser.add_argument(
        "--detect-only",
        action="store_true",
        help="Only detect opportunities, don't execute"
    )
    parser.add_argument(
        "--scan-interval",
        type=float,
        default=None,
        help="Override scan interval (seconds)"
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging"
    )
    return parser.parse_args()


async def run(detect_only: bool = False, scan_interval: float = None):
    """Run the Gabagool monitor."""
    # Try to import the Polymarket client if not in dry run
    client = None
    if not config.DRY_RUN and not detect_only:
        try:
            from poly_data.polymarket_client import PolymarketClient
            client = PolymarketClient()
            logger.info("Polymarket client initialized")
        except Exception as e:
            logger.error(f"Failed to initialize Polymarket client: {e}")
            logger.info("Falling back to dry run mode")

    # Create monitor with optional client
    monitor = GabagoolMonitor(client=client)

    # Handle shutdown gracefully
    def signal_handler(sig, frame):
        logger.info("Shutdown signal received")
        monitor.stop()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Run the monitor
    try:
        await monitor.run(
            scan_interval=scan_interval,
            execute=not detect_only,
        )
    except KeyboardInterrupt:
        pass
    finally:
        monitor.stop()

        # Print summary
        print()
        print("=" * 60)
        print("  SESSION SUMMARY")
        print("=" * 60)
        status = monitor.get_status()
        print(f"  Scans performed:       {status['scans_performed']}")
        print(f"  Opportunities found:   {status['opportunities_detected']}")
        exec_status = status.get('executor', {})
        print(f"  Executions attempted:  {exec_status.get('executions_attempted', 0)}")
        print(f"  Executions successful: {exec_status.get('executions_successful', 0)}")
        print(f"  Total profit:          ${exec_status.get('total_profit', 0):.2f}")
        print("=" * 60)
        print()


def main():
    """Entry point."""
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("rebates.gabagool").setLevel(logging.DEBUG)

    print_banner()

    asyncio.run(run(
        detect_only=args.detect_only,
        scan_interval=args.scan_interval,
    ))


if __name__ == "__main__":
    main()
