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
from typing import Dict, Set, Optional, Any, Tuple
from dataclasses import dataclass, field

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from poly_data.polymarket_client import PolymarketClient

from .market_finder import CryptoMarketFinder
from .strategy import DeltaNeutralStrategy, OrderResult
from .config import (
    TRADE_SIZE,
    TARGET_PRICE,
    DRY_RUN,
    CHECK_INTERVAL_SECONDS,
    SAFETY_BUFFER_SECONDS,
    ASSETS,
)
from alerts.telegram import (
    send_rebates_startup_alert,
    send_rebates_order_alert,
    send_rebates_resolution_alert,
    send_rebates_redemption_alert,
    send_rebates_rescue_alert,
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
    condition_id: str = ""  # For redemption
    status: str = "UPCOMING"  # UPCOMING -> LIVE -> RESOLVED -> REDEEMED
    up_filled: bool = False
    down_filled: bool = False
    logged_live: bool = False
    logged_resolved: bool = False
    redeemed: bool = False
    redeem_attempted: bool = False
    # Order tracking for dynamic updates
    up_order_id: str = ""
    down_order_id: str = ""
    up_price: float = 0.0
    down_price: float = 0.0
    last_update: Optional[datetime] = None
    neg_risk: bool = False
    tick_size: float = 0.01


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

        # Send startup alert
        send_rebates_startup_alert(DRY_RUN, TRADE_SIZE)

    def log(self, message: str):
        """Log with timestamp."""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"[{timestamp}] {message}")

    def check_order_fills(self, tracked: TrackedMarket) -> Tuple[bool, bool]:
        """
        Check if orders for a tracked market have been filled.

        Returns:
            Tuple of (up_open, down_open) - whether orders are still open
        """
        if DRY_RUN:
            return False, False

        try:
            all_orders = self.client.get_all_orders()
            if all_orders.empty:
                # No open orders - assume filled or cancelled
                if not tracked.up_filled:
                    tracked.up_filled = True
                if not tracked.down_filled:
                    tracked.down_filled = True
                return False, False

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

            return up_open, down_open

        except Exception as e:
            return False, False  # Don't spam logs on API errors

    def rescue_unfilled_orders(self, tracked: TrackedMarket) -> None:
        """
        Attempt to rescue unfilled orders during LIVE phase.

        If one side is filled but the other isn't, we have directional exposure.
        This method tries to fill the missing side by:
        1. Re-placing at a more aggressive price (based on orderbook)
        2. As time runs out, allowing higher price ceilings
        3. Last resort: crossing the spread (taker order)

        Price ceiling escalation based on time remaining:
        - > 10 min: max 0.52
        - 5-10 min: max 0.55
        - 2-5 min: max 0.58
        - < 2 min: max 0.60
        - < 30 sec: taker order (cross spread, cap at 0.65)
        """
        if DRY_RUN:
            return

        # Check current fill status
        up_open, down_open = self.check_order_fills(tracked)

        # If both filled or both open, nothing to rescue
        if tracked.up_filled and tracked.down_filled:
            return  # Both filled, we're good
        if up_open and down_open:
            return  # Both still open, wait for fills

        # Calculate time remaining until resolution
        resolution_time = tracked.event_start + timedelta(minutes=15)
        now = datetime.now(timezone.utc)
        time_remaining = (resolution_time - now).total_seconds()

        if time_remaining <= 0:
            return  # Already resolved, too late

        # Determine which side needs rescue
        need_rescue_up = not tracked.up_filled and not up_open
        need_rescue_down = not tracked.down_filled and not down_open

        # Actually, let's reconsider the logic:
        # - up_filled = True means UP order was filled (we hold UP position)
        # - up_open = True means UP order is still open (waiting for fill)
        # - Need rescue if: one side is filled, other side is still OPEN (not filled)

        # One side filled, other still open = directional risk
        if tracked.up_filled and down_open:
            # UP filled, DOWN still open - need to get DOWN filled
            self._rescue_single_side(tracked, "DOWN", tracked.down_token, time_remaining)
        elif tracked.down_filled and up_open:
            # DOWN filled, UP still open - need to get UP filled
            self._rescue_single_side(tracked, "UP", tracked.up_token, time_remaining)

    def _rescue_single_side(
        self,
        tracked: TrackedMarket,
        side: str,
        token_id: str,
        time_remaining: float
    ) -> None:
        """
        Rescue a single unfilled side by re-placing at better price.

        Args:
            tracked: The tracked market
            side: "UP" or "DOWN"
            token_id: Token ID for the unfilled side
            time_remaining: Seconds until resolution
        """
        # Get current price for alerts
        current_price = tracked.up_price if side == "UP" else tracked.down_price

        # Determine max price based on time remaining
        if time_remaining < 30:
            # Last resort - taker order
            self.log(f"  RESCUE {side}: <30s remaining, attempting taker order")
            taker_price = self.strategy.get_taker_price(token_id)
            success, result = self.strategy.place_taker_order(
                token_id, self.strategy.trade_size, tracked.neg_risk
            )
            if success:
                self.log(f"  RESCUE {side}: Taker order placed")
                if side == "UP":
                    tracked.up_filled = True
                else:
                    tracked.down_filled = True
                # Send rescue alert
                alert_result = send_rebates_rescue_alert(
                    question=tracked.question,
                    side=side,
                    old_price=current_price,
                    new_price=taker_price or 0.50,
                    is_taker=True,
                    dry_run=DRY_RUN
                )
                self.log(f"  RESCUE {side}: Taker alert sent={alert_result}")
            else:
                self.log(f"  RESCUE {side}: Taker order failed: {result}")
            return

        elif time_remaining < 120:  # < 2 min
            max_price = 0.60
        elif time_remaining < 300:  # < 5 min
            max_price = 0.58
        elif time_remaining < 600:  # < 10 min
            max_price = 0.55
        else:
            max_price = 0.52

        # Get competitive price from orderbook (up to our ceiling)
        new_price = self.strategy.get_best_maker_price(
            token_id,
            tracked.tick_size,
            max_price=max_price
        )

        if new_price is None:
            self.log(f"  RESCUE {side}: Could not get orderbook price")
            return

        # Only update if price is better than current
        if new_price <= current_price:
            # Already at or above this price, no change needed
            return

        self.log(f"  RESCUE {side}: Updating {current_price:.2f} -> {new_price:.2f} (max={max_price:.2f}, {time_remaining:.0f}s left)")

        # Cancel and re-place order
        success, new_order_id = self.strategy.update_single_order(
            token_id, new_price, tracked.neg_risk
        )

        if success:
            if side == "UP":
                tracked.up_price = new_price
                if new_order_id and not new_order_id.startswith("Failed"):
                    tracked.up_order_id = new_order_id
            else:
                tracked.down_price = new_price
                if new_order_id and not new_order_id.startswith("Failed"):
                    tracked.down_order_id = new_order_id

            # Send rescue alert
            alert_result = send_rebates_rescue_alert(
                question=tracked.question,
                side=side,
                old_price=current_price,
                new_price=new_price,
                is_taker=False,
                dry_run=DRY_RUN
            )
            self.log(f"  RESCUE {side}: Alert sent={alert_result}")
        else:
            self.log(f"  RESCUE {side}: Update failed: {new_order_id}")

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

                # Send Telegram alert for resolution
                resolution_result = send_rebates_resolution_alert(
                    question=tracked.question,
                    up_filled=tracked.up_filled,
                    down_filled=tracked.down_filled,
                    trade_size=TRADE_SIZE,
                    dry_run=DRY_RUN
                )
                self.log(f"  Resolution alert sent={resolution_result}")

    def attempt_redemption(self, tracked: TrackedMarket) -> bool:
        """
        Attempt to redeem winning positions for a resolved market.

        Returns True if redemption succeeded or was skipped.
        """
        # Skip if already redeemed or attempted
        if tracked.redeemed or tracked.redeem_attempted:
            return True

        # Skip if no condition ID
        if not tracked.condition_id:
            self.log(f"  No condition ID for redemption: {tracked.slug}")
            tracked.redeem_attempted = True
            return False

        # Skip if no fills (nothing to redeem)
        if not tracked.up_filled and not tracked.down_filled:
            self.log(f"  No fills to redeem: {tracked.slug}")
            tracked.redeem_attempted = True
            return True

        # Skip in dry run mode
        if DRY_RUN:
            self.log(f"  [DRY RUN] Would redeem: {tracked.slug}")
            tracked.redeem_attempted = True
            send_rebates_redemption_alert(
                question=tracked.question,
                condition_id=tracked.condition_id,
                dry_run=True
            )
            return True

        # Wait a bit after resolution for blockchain confirmation
        resolution_time = tracked.event_start + timedelta(minutes=15)
        time_since_resolution = (datetime.now(timezone.utc) - resolution_time).total_seconds()

        # Wait at least 60 seconds after resolution before redeeming
        if time_since_resolution < 60:
            return False

        try:
            self.log(f"  Redeeming positions: {tracked.slug}")
            self.log(f"    Condition ID: {tracked.condition_id}")

            result = self.client.redeem_positions(tracked.condition_id)

            self.log(f"  Redemption successful!")
            tracked.redeemed = True
            tracked.redeem_attempted = True
            tracked.status = "REDEEMED"

            # Send Telegram alert
            send_rebates_redemption_alert(
                question=tracked.question,
                condition_id=tracked.condition_id,
                dry_run=False
            )
            return True

        except Exception as e:
            self.log(f"  Redemption failed: {e}")
            tracked.redeem_attempted = True
            return False

    def update_upcoming_orders(self, tracked: TrackedMarket) -> None:
        """
        Check and update orders for an UPCOMING market to stay competitive.

        Only updates if:
        - Market is still UPCOMING
        - At least 10 seconds since last update (avoid hammering API)
        - Our price is no longer competitive
        """
        now = datetime.now(timezone.utc)

        # Don't update too frequently (minimum 10 seconds between updates)
        if tracked.last_update:
            seconds_since_update = (now - tracked.last_update).total_seconds()
            if seconds_since_update < 10:
                return

        # Don't update if market starts very soon (within 5 seconds) - too risky
        time_until_start = (tracked.event_start - now).total_seconds()
        if time_until_start < 5:
            return

        # Check UP order competitiveness
        up_competitive, up_best_price = self.strategy.check_order_competitiveness(
            tracked.up_token, tracked.up_price, tracked.tick_size
        )

        # Check DOWN order competitiveness
        down_competitive, down_best_price = self.strategy.check_order_competitiveness(
            tracked.down_token, tracked.down_price, tracked.tick_size
        )

        # Update UP order if not competitive
        if not up_competitive and up_best_price is not None:
            self.log(f"  Updating UP order: {tracked.up_price} -> {up_best_price} ({tracked.question[:40]}...)")
            success, new_order_id = self.strategy.update_single_order(
                tracked.up_token, up_best_price, tracked.neg_risk
            )
            if success:
                tracked.up_price = up_best_price
                if new_order_id and not new_order_id.startswith("Failed"):
                    tracked.up_order_id = new_order_id

        # Update DOWN order if not competitive
        if not down_competitive and down_best_price is not None:
            self.log(f"  Updating DOWN order: {tracked.down_price} -> {down_best_price} ({tracked.question[:40]}...)")
            success, new_order_id = self.strategy.update_single_order(
                tracked.down_token, down_best_price, tracked.neg_risk
            )
            if success:
                tracked.down_price = down_best_price
                if new_order_id and not new_order_id.startswith("Failed"):
                    tracked.down_order_id = new_order_id

        tracked.last_update = now

    def monitor_tracked_markets(self) -> None:
        """Monitor all tracked markets for status changes, fills, and order updates."""
        for slug, tracked in list(self.tracked_markets.items()):
            # Update status
            self.check_market_status(tracked)

            # Update orders for UPCOMING markets to stay competitive
            if tracked.status == "UPCOMING":
                self.update_upcoming_orders(tracked)

            # For LIVE markets: check fills and rescue unfilled orders
            if tracked.status == "LIVE":
                # Rescue unfilled orders (also checks fills internally)
                self.rescue_unfilled_orders(tracked)

            # Attempt redemption for RESOLVED markets
            if tracked.status == "RESOLVED":
                self.attempt_redemption(tracked)

        # Cleanup old redeemed markets (keep last 50)
        redeemed = [s for s, t in self.tracked_markets.items() if t.status == "REDEEMED"]
        if len(redeemed) > 50:
            for slug in redeemed[:len(redeemed) - 50]:
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
        result = self.strategy.place_mirror_orders(market)

        if result.success:
            # Track this market
            try:
                up_token, down_token = self.strategy.get_tokens(market)
            except Exception:
                up_token, down_token = "", ""

            # Get condition ID for redemption
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
                up_order_id=result.up_order_id,
                down_order_id=result.down_order_id,
                up_price=result.up_price,
                down_price=result.down_price,
                last_update=datetime.now(timezone.utc),
                neg_risk=neg_risk,
                tick_size=tick_size,
            )
            self.log(f"SUCCESS: {result.message}")

            # Send Telegram alert for orders placed
            send_rebates_order_alert(
                question=question,
                trade_size=TRADE_SIZE,
                price=(result.up_price + result.down_price) / 2,  # Average price
                dry_run=DRY_RUN
            )
        else:
            self.log(f"FAILED: {result.message}")

        return result.success

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
        redeemed = sum(1 for t in self.tracked_markets.values() if t.status == "REDEEMED")

        if upcoming or live or resolved:
            self.log(f"Tracking: {upcoming} UPCOMING, {live} LIVE, {resolved} RESOLVED, {redeemed} REDEEMED")

    def run(self):
        """
        Main loop - continuously find markets and place orders.

        Runs indefinitely until interrupted.

        Uses two update frequencies:
        - Full market scan: Every CHECK_INTERVAL_SECONDS (60s)
        - Order updates for UPCOMING markets: Every 15s
        """
        self.log("Starting main loop...")
        self.log(f"Full market scan every {CHECK_INTERVAL_SECONDS}s")
        self.log(f"Order updates every 15s for UPCOMING markets")

        ORDER_UPDATE_INTERVAL = 15  # Seconds between order updates
        cycle_count = 0
        last_full_scan = 0  # Force immediate scan on start

        while True:
            try:
                now = time.time()

                # Full market scan at longer interval
                if now - last_full_scan >= CHECK_INTERVAL_SECONDS:
                    traded = self.run_once()
                    if traded > 0:
                        self.log(f"Traded {traded} markets this cycle")

                    cycle_count += 1
                    if cycle_count % 5 == 0:
                        self.log_status_summary()

                    last_full_scan = now
                else:
                    # Quick update cycle - just monitor existing markets
                    # This updates orders for UPCOMING markets to stay competitive
                    self.monitor_tracked_markets()

                # Sleep for the shorter interval
                time.sleep(ORDER_UPDATE_INTERVAL)

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
