"""
Gabagool Scanner - Orderbook scanning for arbitrage opportunities.

Continuously monitors orderbooks for YES+NO pairs where the combined cost
is below $1.00, indicating a guaranteed profit opportunity.
"""

import logging
import requests
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, List, Tuple

from . import config

logger = logging.getLogger(__name__)

CLOB_API_BASE = "https://clob.polymarket.com"


@dataclass
class Opportunity:
    """Represents a detected arbitrage opportunity."""

    # Market identification
    market_slug: str
    condition_id: str
    up_token: str
    down_token: str
    neg_risk: bool

    # Pricing
    up_price: float  # Best ask price for UP
    down_price: float  # Best ask price for DOWN
    combined_cost: float  # up_price + down_price

    # Liquidity
    up_size: float  # Size available at up_price
    down_size: float  # Size available at down_price
    max_size: float  # min(up_size, down_size)

    # Profitability
    gross_profit_pct: float  # (1.00 - combined_cost) * 100
    net_profit_pct: float  # After gas estimate
    expected_profit_usd: float  # For trade_size

    # Timing
    detected_at: datetime
    market_start_time: Optional[datetime] = None
    seconds_to_start: Optional[float] = None


class GabagoolScanner:
    """
    Scans orderbooks for Gabagool arbitrage opportunities.

    An opportunity exists when:
    - YES ask + NO ask < PROFIT_THRESHOLD (e.g., 0.99 for maker, 0.98 for taker)
    - Sufficient liquidity exists at those prices
    - Market has enough time remaining

    Fee structure (15-minute crypto markets):
    - Maker orders: FREE (no fees, plus earn rebates!)
    - Taker orders: Up to 1.56% at 50% odds
    - No winner fee on profits
    """

    def __init__(
        self,
        profit_threshold: float = None,
        min_liquidity: float = None,
        min_net_profit_pct: float = None,
        trade_size: float = None,
    ):
        """
        Initialize the scanner.

        Args:
            profit_threshold: Max combined cost to consider profitable (default from config)
            min_liquidity: Minimum shares available at opportunity prices
            min_net_profit_pct: Minimum net profit percentage to report
            trade_size: Trade size for profit calculations
        """
        self.profit_threshold = (
            profit_threshold if profit_threshold is not None else config.PROFIT_THRESHOLD
        )
        self.min_liquidity = (
            min_liquidity if min_liquidity is not None else config.MIN_LIQUIDITY
        )
        self.min_net_profit_pct = (
            min_net_profit_pct if min_net_profit_pct is not None else config.MIN_NET_PROFIT_PCT
        )
        self.trade_size = trade_size if trade_size is not None else config.TRADE_SIZE

        # Estimated gas cost for merge transaction
        self.gas_cost_usd = 0.002

    def _fetch_orderbook(self, token_id: str) -> Optional[dict]:
        """
        Fetch orderbook for a token.

        Returns:
            Dict with 'bids' and 'asks' lists, or None on error
        """
        try:
            url = f"{CLOB_API_BASE}/book?token_id={token_id}"
            resp = requests.get(url, timeout=5)
            if resp.status_code != 200:
                logger.warning(f"Orderbook fetch failed for {token_id[:20]}...: {resp.status_code}")
                return None
            return resp.json()
        except Exception as e:
            logger.error(f"Error fetching orderbook for {token_id[:20]}...: {e}")
            return None

    def _calculate_vwap(
        self,
        orders: list,
        target_size: float,
        min_order_size: float = 5.0,
    ) -> Tuple[Optional[float], float]:
        """
        Calculate Volume-Weighted Average Price for filling target_size.

        This protects against spoofing attacks where someone places small
        fake orders to manipulate the bot's pricing.

        Args:
            orders: List of order dicts with 'price' and 'size' keys
            target_size: Size we want to fill
            min_order_size: Minimum $ size to consider (filters small spoof orders)

        Returns:
            Tuple of (vwap_price, fillable_size)
        """
        if not orders:
            return None, 0.0

        total_value = 0.0
        total_size = 0.0
        remaining_size = target_size

        for order in orders:
            price = float(order.get("price", 0))
            size = float(order.get("size", 0))
            dollar_value = price * size

            # Skip small orders that might be spoofing
            if dollar_value < min_order_size:
                continue

            # How much can we take from this level?
            take_size = min(size, remaining_size)
            total_value += price * take_size
            total_size += take_size
            remaining_size -= take_size

            if remaining_size <= 0:
                break

        if total_size == 0:
            # Fall back to best price if all orders are small
            return float(orders[0].get("price", 0)), float(orders[0].get("size", 0))

        return total_value / total_size, total_size

    def _get_best_ask(
        self,
        orderbook: dict,
        target_size: float = None,
    ) -> Tuple[Optional[float], float]:
        """
        Get the best ask price and size for buying.

        Uses VWAP to protect against spoofing.

        Args:
            orderbook: Orderbook dict with 'asks' list
            target_size: Target size for VWAP calculation

        Returns:
            Tuple of (price, size) or (None, 0) if no asks
        """
        asks = orderbook.get("asks", [])
        if not asks:
            return None, 0.0

        # Sort asks by price (lowest first)
        asks_sorted = sorted(asks, key=lambda x: float(x["price"]))

        if target_size is None:
            target_size = self.trade_size

        return self._calculate_vwap(asks_sorted, target_size)

    def scan_market(
        self,
        market_slug: str,
        condition_id: str,
        up_token: str,
        down_token: str,
        neg_risk: bool,
        market_start_time: datetime = None,
    ) -> Optional[Opportunity]:
        """
        Scan a single market for arbitrage opportunity.

        Args:
            market_slug: Market identifier
            condition_id: Polymarket condition ID
            up_token: Token ID for UP/YES outcome
            down_token: Token ID for DOWN/NO outcome
            neg_risk: Whether this is a negative risk market
            market_start_time: When the market starts (for timing)

        Returns:
            Opportunity if found, None otherwise
        """
        # Fetch orderbooks
        up_book = self._fetch_orderbook(up_token)
        down_book = self._fetch_orderbook(down_token)

        if up_book is None or down_book is None:
            return None

        # Get best ask prices (what we'd pay to buy)
        up_price, up_size = self._get_best_ask(up_book, self.trade_size)
        down_price, down_size = self._get_best_ask(down_book, self.trade_size)

        if up_price is None or down_price is None:
            logger.debug(f"{market_slug}: No asks available")
            return None

        # Calculate combined cost
        combined_cost = up_price + down_price

        # Check profitability threshold
        if combined_cost >= self.profit_threshold:
            logger.debug(
                f"{market_slug}: Combined cost {combined_cost:.4f} >= threshold {self.profit_threshold}"
            )
            return None

        # Check liquidity
        max_size = min(up_size, down_size)
        if max_size < self.min_liquidity:
            logger.debug(
                f"{market_slug}: Insufficient liquidity ({max_size:.1f} < {self.min_liquidity})"
            )
            return None

        # Calculate profits
        gross_profit_pct = (1.00 - combined_cost) * 100
        net_profit = 1.00 - combined_cost - (self.gas_cost_usd / self.trade_size)
        net_profit_pct = net_profit * 100

        # Check minimum profit
        if net_profit_pct < self.min_net_profit_pct:
            logger.debug(
                f"{market_slug}: Net profit {net_profit_pct:.2f}% < minimum {self.min_net_profit_pct}%"
            )
            return None

        # Calculate timing
        now = datetime.now(timezone.utc)
        seconds_to_start = None
        if market_start_time:
            seconds_to_start = (market_start_time - now).total_seconds()

        # Calculate expected profit in USD
        expected_profit_usd = self.trade_size * (1.00 - combined_cost) - self.gas_cost_usd

        opportunity = Opportunity(
            market_slug=market_slug,
            condition_id=condition_id,
            up_token=up_token,
            down_token=down_token,
            neg_risk=neg_risk,
            up_price=up_price,
            down_price=down_price,
            combined_cost=combined_cost,
            up_size=up_size,
            down_size=down_size,
            max_size=max_size,
            gross_profit_pct=gross_profit_pct,
            net_profit_pct=net_profit_pct,
            expected_profit_usd=expected_profit_usd,
            detected_at=now,
            market_start_time=market_start_time,
            seconds_to_start=seconds_to_start,
        )

        logger.info(
            f"OPPORTUNITY: {market_slug} - "
            f"Combined: {combined_cost:.4f} "
            f"({up_price:.2f} + {down_price:.2f}) "
            f"Profit: {gross_profit_pct:.2f}% "
            f"Size: {max_size:.0f} "
            f"Expected: ${expected_profit_usd:.2f}"
        )

        return opportunity

    def scan_markets(
        self,
        markets: List[dict],
    ) -> List[Opportunity]:
        """
        Scan multiple markets for opportunities.

        Args:
            markets: List of market dicts with tokens and metadata

        Returns:
            List of detected opportunities, sorted by profit potential
        """
        opportunities = []

        for market in markets:
            try:
                opportunity = self.scan_market(
                    market_slug=market.get("slug", "unknown"),
                    condition_id=market.get("conditionId", ""),
                    up_token=market.get("up_token", ""),
                    down_token=market.get("down_token", ""),
                    neg_risk=market.get("neg_risk", False),
                    market_start_time=market.get("start_time"),
                )
                if opportunity:
                    opportunities.append(opportunity)
            except Exception as e:
                logger.error(f"Error scanning market {market.get('slug', 'unknown')}: {e}")

        # Sort by expected profit (highest first)
        opportunities.sort(key=lambda o: o.expected_profit_usd, reverse=True)

        return opportunities

    def should_execute(
        self,
        opportunity: Opportunity,
    ) -> Tuple[bool, str]:
        """
        Determine if we should execute on an opportunity.

        Args:
            opportunity: The detected opportunity

        Returns:
            Tuple of (should_execute, reason)
        """
        # Check time to market start
        if opportunity.seconds_to_start is not None:
            if opportunity.seconds_to_start < config.MIN_TIME_TO_START:
                return False, f"Too close to start ({opportunity.seconds_to_start:.0f}s)"

        # Check liquidity
        if opportunity.max_size < config.MIN_ORDER_SIZE:
            return False, f"Insufficient liquidity ({opportunity.max_size:.1f} shares)"

        # Check minimum profit
        if opportunity.net_profit_pct < config.MIN_NET_PROFIT_PCT:
            return False, f"Profit too small ({opportunity.net_profit_pct:.2f}%)"

        return True, "OK"
