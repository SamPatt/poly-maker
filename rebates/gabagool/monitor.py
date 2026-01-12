"""
Gabagool Opportunity Monitor

Main loop that continuously scans for arbitrage opportunities in 15-minute
crypto markets. Integrates with the existing market finder and scanner.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from ..market_finder import CryptoMarketFinder
from .scanner import GabagoolScanner, Opportunity
from .circuit_breaker import CircuitBreaker
from .executor import GabagoolExecutor, ExecutionStrategy
from . import config

logger = logging.getLogger(__name__)


class GabagoolMonitor:
    """
    Monitors and executes Gabagool arbitrage opportunities.

    Integrates with:
    - CryptoMarketFinder: Discovers upcoming 15-minute markets
    - GabagoolScanner: Scans orderbooks for YES+NO < $1 opportunities
    - CircuitBreaker: Enforces risk limits
    - GabagoolExecutor: Places paired orders when opportunities found

    The monitor continuously scans markets and executes when profitable
    spreads are detected. Supports dry-run mode for testing.
    """

    def __init__(
        self,
        market_finder: CryptoMarketFinder = None,
        scanner: GabagoolScanner = None,
        circuit_breaker: CircuitBreaker = None,
        executor: GabagoolExecutor = None,
        client=None,
    ):
        """
        Initialize the monitor.

        Args:
            market_finder: Market discovery (defaults to new CryptoMarketFinder)
            scanner: Opportunity scanner (defaults to new GabagoolScanner)
            circuit_breaker: Risk management (defaults from config)
            executor: Order executor (defaults to new GabagoolExecutor)
            client: PolymarketClient for order placement (optional)
        """
        self.market_finder = market_finder or CryptoMarketFinder()
        self.scanner = scanner or GabagoolScanner()
        self.circuit_breaker = circuit_breaker or CircuitBreaker(
            config.get_circuit_breaker_config()
        )
        self.executor = executor or GabagoolExecutor(
            client=client,
            circuit_breaker=self.circuit_breaker,
        )

        self.running = False
        self.opportunities_detected = 0
        self.scans_performed = 0
        self.last_opportunity: Optional[Opportunity] = None

    def _extract_tokens(self, market: Dict[str, Any]) -> tuple:
        """Extract UP and DOWN token IDs from market data."""
        clob_tokens = market.get("clobTokenIds", [])

        if isinstance(clob_tokens, str):
            try:
                clob_tokens = json.loads(clob_tokens)
            except json.JSONDecodeError:
                logger.error(f"Failed to parse clobTokenIds for {market.get('slug')}")
                return None, None

        if len(clob_tokens) < 2:
            logger.warning(f"Market {market.get('slug')} missing token IDs")
            return None, None

        return clob_tokens[0], clob_tokens[1]

    def _is_neg_risk(self, market: Dict[str, Any]) -> bool:
        """Check if market uses negative risk."""
        neg_risk = market.get("negRisk") or market.get("neg_risk")
        return neg_risk in (True, "TRUE", "true")

    def _prepare_market_for_scan(self, market: Dict[str, Any]) -> Optional[Dict]:
        """
        Transform market data for scanner input.

        Returns None if market cannot be scanned.
        """
        up_token, down_token = self._extract_tokens(market)
        if not up_token or not down_token:
            return None

        return {
            "slug": market.get("slug", "unknown"),
            "conditionId": market.get("conditionId", ""),
            "up_token": up_token,
            "down_token": down_token,
            "neg_risk": self._is_neg_risk(market),
            "start_time": market.get("_event_start"),
        }

    async def scan_once(self) -> List[Opportunity]:
        """
        Perform a single scan of all upcoming markets.

        Returns:
            List of detected opportunities
        """
        self.scans_performed += 1

        # Check circuit breaker
        can_trade, reason = await self.circuit_breaker.check_can_trade("__monitor__", 0)
        if not can_trade:
            logger.warning(f"Circuit breaker blocked scan: {reason}")
            return []

        # Get upcoming markets
        markets = self.market_finder.get_upcoming_markets()
        if not markets:
            logger.debug("No upcoming markets found")
            return []

        logger.debug(f"Scanning {len(markets)} upcoming markets")

        # Prepare markets for scanning
        scan_markets = []
        for market in markets:
            prepared = self._prepare_market_for_scan(market)
            if prepared:
                scan_markets.append(prepared)

        if not scan_markets:
            return []

        # Scan for opportunities (using async parallel fetching)
        opportunities = await self.scanner.scan_markets_async(scan_markets)

        # Filter opportunities that pass should_execute
        executable_opportunities = []
        for opp in opportunities:
            should_exec, reason = self.scanner.should_execute(opp)
            if should_exec:
                executable_opportunities.append(opp)
                self.opportunities_detected += 1
                self.last_opportunity = opp

                # Log opportunity details
                logger.info(
                    f"GABAGOOL OPPORTUNITY DETECTED: "
                    f"{opp.market_slug} - "
                    f"Combined: {opp.combined_cost:.4f} "
                    f"({opp.up_price:.2f} + {opp.down_price:.2f}) "
                    f"Profit: {opp.gross_profit_pct:.2f}% "
                    f"Expected: ${opp.expected_profit_usd:.2f}"
                )
            else:
                logger.debug(f"Opportunity filtered: {opp.market_slug} - {reason}")

        return executable_opportunities

    async def run(self, scan_interval: float = None, execute: bool = True):
        """
        Run the opportunity detection and execution loop.

        Args:
            scan_interval: Seconds between scans (defaults from config)
            execute: If True, execute on opportunities; if False, just detect
        """
        if scan_interval is None:
            scan_interval = config.SCAN_INTERVAL

        self.running = True
        logger.info(f"Gabagool monitor starting (scan interval: {scan_interval}s)")

        if config.DRY_RUN:
            logger.info("DRY RUN MODE - orders will be simulated, not placed")
        elif not execute:
            logger.info("DETECTION ONLY MODE - opportunities will be logged but not executed")

        while self.running:
            try:
                opportunities = await self.scan_once()

                if opportunities:
                    logger.info(f"Found {len(opportunities)} executable opportunities")

                    for opp in opportunities:
                        self._log_opportunity(opp)

                        # Execute on opportunity if enabled
                        if execute:
                            result = await self.executor.execute(opp)
                            if result.success:
                                logger.info(
                                    f"Executed on {opp.market_slug}: "
                                    f"UP={result.up_filled} DOWN={result.down_filled} "
                                    f"Expected profit=${result.expected_profit:.2f}"
                                )
                            else:
                                logger.warning(
                                    f"Execution failed for {opp.market_slug}: {result.reason}"
                                )

                # Wait before next scan
                await asyncio.sleep(scan_interval)

            except Exception as e:
                logger.error(f"Error in scan loop: {e}", exc_info=True)

                # Record error in circuit breaker
                await self.circuit_breaker.record_trade_result(
                    market_id="__monitor__",
                    size=0,
                    pnl=0,
                    success=False,
                    error_msg=str(e),
                )

                # Wait before retrying
                await asyncio.sleep(scan_interval * 2)

    def stop(self):
        """Stop the monitoring loop."""
        self.running = False
        logger.info("Gabagool monitor stopping")

    def _log_opportunity(self, opp: Opportunity):
        """Log detailed opportunity information."""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        print(f"\n{'='*60}")
        print(f"[{timestamp}] GABAGOOL OPPORTUNITY")
        print(f"{'='*60}")
        print(f"Market: {opp.market_slug}")
        print(f"Condition ID: {opp.condition_id[:20]}...")
        print(f"")
        print(f"Pricing:")
        print(f"  UP ask:   {opp.up_price:.4f} (size: {opp.up_size:.0f})")
        print(f"  DOWN ask: {opp.down_price:.4f} (size: {opp.down_size:.0f})")
        print(f"  Combined: {opp.combined_cost:.4f}")
        print(f"")
        print(f"Profitability:")
        print(f"  Gross profit: {opp.gross_profit_pct:.2f}%")
        print(f"  Net profit:   {opp.net_profit_pct:.2f}%")
        print(f"  Expected USD: ${opp.expected_profit_usd:.2f}")
        print(f"")
        print(f"Liquidity: {opp.max_size:.0f} shares available")
        if opp.seconds_to_start is not None:
            print(f"Time to start: {opp.seconds_to_start:.0f}s")
        print(f"{'='*60}\n")

    def get_status(self) -> dict:
        """Get current monitor status."""
        cb_status = self.circuit_breaker.get_status()
        executor_status = self.executor.get_status()

        return {
            "running": self.running,
            "scans_performed": self.scans_performed,
            "opportunities_detected": self.opportunities_detected,
            "dry_run": config.DRY_RUN,
            "circuit_breaker": cb_status,
            "executor": executor_status,
            "last_opportunity": {
                "market": self.last_opportunity.market_slug,
                "profit_pct": self.last_opportunity.gross_profit_pct,
                "detected_at": self.last_opportunity.detected_at.isoformat(),
            }
            if self.last_opportunity
            else None,
        }


async def main():
    """Run the Gabagool monitor as a standalone process."""
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    monitor = GabagoolMonitor()

    try:
        await monitor.run()
    except KeyboardInterrupt:
        monitor.stop()
        print("\nMonitor stopped by user")


if __name__ == "__main__":
    asyncio.run(main())
