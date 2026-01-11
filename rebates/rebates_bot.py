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
    send_rebates_rescue_filled_alert,
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
    # Track if rescue orders were placed (to send alerts when filled)
    up_rescue_attempted: bool = False
    down_rescue_attempted: bool = False


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

        # Load pending markets from database (for restart recovery)
        self._load_pending_markets()

        # Cleanup old redeemed markets
        deleted = cleanup_old_rebates_markets(days=7)
        if deleted > 0:
            print(f"Cleaned up {deleted} old redeemed markets from database")

        # Send startup alert
        send_rebates_startup_alert(DRY_RUN, TRADE_SIZE)

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

                # Parse event_start from database
                event_start = row.get("event_start")
                if isinstance(event_start, str):
                    event_start = datetime.fromisoformat(event_start.replace("Z", "+00:00"))
                elif hasattr(event_start, 'tzinfo') and event_start.tzinfo is None:
                    event_start = event_start.replace(tzinfo=timezone.utc)

                # Create TrackedMarket from database row
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
                    up_price=float(row.get("up_price", 0.50)),
                    down_price=float(row.get("down_price", 0.50)),
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
                # Log status breakdown
                by_status = {}
                for t in self.tracked_markets.values():
                    by_status[t.status] = by_status.get(t.status, 0) + 1
                for status, count in sorted(by_status.items()):
                    print(f"  {status}: {count}")

        except Exception as e:
            print(f"Error loading pending markets: {e}")
            import traceback
            traceback.print_exc()

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
                update_rebates_market_fills(tracked.slug, up_filled=True)
                self.log(f"  UP order filled: {tracked.question}")
                # Send fill alert
                send_rebates_fill_alert(
                    question=tracked.question,
                    side="UP",
                    price=tracked.up_price,
                    size=TRADE_SIZE,
                    dry_run=DRY_RUN
                )
            if not down_open and not tracked.down_filled:
                tracked.down_filled = True
                update_rebates_market_fills(tracked.slug, down_filled=True)
                self.log(f"  DOWN order filled: {tracked.question}")
                # Send fill alert
                send_rebates_fill_alert(
                    question=tracked.question,
                    side="DOWN",
                    price=tracked.down_price,
                    size=TRADE_SIZE,
                    dry_run=DRY_RUN
                )

            return up_open, down_open

        except Exception as e:
            return False, False  # Don't spam logs on API errors

    def rescue_unfilled_orders(self, tracked: TrackedMarket) -> None:
        """
        Attempt to rescue unfilled orders IMMEDIATELY when one side fills.

        If one side is filled but the other isn't, we have directional exposure.

        NEW AGGRESSIVE STRATEGY:
        - When one side fills, IMMEDIATELY place a taker order on the other side
        - Don't wait, don't escalate slowly - just cross the spread and get filled
        - Better to pay 52-55% and guarantee both sides fill than risk one-sided exposure

        This ensures we always earn rebates on both sides.
        """
        if DRY_RUN:
            return

        # Check current fill status
        up_open, down_open = self.check_order_fills(tracked)

        # If both filled, we're done
        if tracked.up_filled and tracked.down_filled:
            return

        # If both still open, wait for fills
        if up_open and down_open:
            return

        # One side filled, other still open = directional risk - RESCUE IMMEDIATELY
        if tracked.up_filled and down_open:
            # UP filled, DOWN still open - need to get DOWN filled NOW
            self._rescue_immediate(tracked, "DOWN", tracked.down_token)
        elif tracked.down_filled and up_open:
            # DOWN filled, UP still open - need to get UP filled NOW
            self._rescue_immediate(tracked, "UP", tracked.up_token)

    def _rescue_immediate(
        self,
        tracked: TrackedMarket,
        side: str,
        token_id: str
    ) -> None:
        """
        Immediately rescue unfilled side by placing aggressive taker order.

        Strategy: Cross the spread immediately to guarantee fill.
        We'd rather pay 52-55% and get filled than sit at 50% and miss.

        Args:
            tracked: The tracked market
            side: "UP" or "DOWN"
            token_id: Token ID for the unfilled side
        """
        # Only attempt rescue once per side
        if side == "UP" and tracked.up_rescue_attempted:
            return
        if side == "DOWN" and tracked.down_rescue_attempted:
            return

        current_price = tracked.up_price if side == "UP" else tracked.down_price
        self.log(f"  RESCUE {side}: One side filled, crossing spread immediately")

        # Cancel existing order and place taker order
        try:
            self.strategy.client.cancel_all_asset(token_id)
        except Exception as e:
            self.log(f"  RESCUE {side}: Cancel failed: {e}")

        # Place taker order (cross the spread)
        success, result = self.strategy.place_taker_order(
            token_id, self.strategy.trade_size, tracked.neg_risk
        )

        if success:
            self.log(f"  RESCUE {side}: Taker order placed successfully")
            if side == "UP":
                tracked.up_filled = True
                tracked.up_rescue_attempted = True
            else:
                tracked.down_filled = True
                tracked.down_rescue_attempted = True

            # Get the taker price for alert
            taker_price = self.strategy.get_taker_price(token_id) or 0.55

            # Send rescue alert
            send_rebates_rescue_alert(
                question=tracked.question,
                side=side,
                old_price=current_price,
                new_price=taker_price,
                is_taker=True,
                dry_run=DRY_RUN
            )
        else:
            self.log(f"  RESCUE {side}: Taker order failed: {result}")
            # Mark as attempted to avoid spam
            if side == "UP":
                tracked.up_rescue_attempted = True
            else:
                tracked.down_rescue_attempted = True

    def _rescue_single_side(
        self,
        tracked: TrackedMarket,
        side: str,
        token_id: str,
        time_remaining: float
    ) -> None:
        """
        Rescue a single unfilled side by re-placing at better price.

        AGGRESSIVE rescue strategy - prioritize getting filled over saving money.
        Better to pay 55-60% than lose 100% at resolution.

        Price ceiling AND aggression escalation:
        | Time Left | Max Price | Aggression | Meaning                    |
        |-----------|-----------|------------|----------------------------|
        | > 12 min  | 0.55      | 20%        | 20% of spread toward ask   |
        | 8-12 min  | 0.58      | 30%        | 30% of spread toward ask   |
        | 5-8 min   | 0.60      | 40%        | 40% of spread toward ask   |
        | 2-5 min   | 0.62      | 60%        | 60% of spread toward ask   |
        | 1-2 min   | 0.65      | 80%        | 80% of spread toward ask   |
        | < 60 sec  | TAKER     | 100%       | Cross spread (cap at 0.70) |

        Example: If bid=0.48, ask=0.52 (spread=0.04):
        - 20% aggression: 0.48 + 0.01 + (0.04 * 0.2) = 0.498 -> 0.50
        - 60% aggression: 0.48 + 0.01 + (0.04 * 0.6) = 0.514 -> 0.51
        - 80% aggression: 0.48 + 0.01 + (0.04 * 0.8) = 0.522 -> 0.52

        Args:
            tracked: The tracked market
            side: "UP" or "DOWN"
            token_id: Token ID for the unfilled side
            time_remaining: Seconds until resolution
        """
        # Get current price for alerts
        current_price = tracked.up_price if side == "UP" else tracked.down_price

        # Determine max price based on time remaining - AGGRESSIVE thresholds
        if time_remaining < 60:
            # Last resort - taker order at 60 seconds (was 30)
            # Only attempt taker once
            if side == "UP" and tracked.up_rescue_attempted:
                return
            if side == "DOWN" and tracked.down_rescue_attempted:
                return

            self.log(f"  RESCUE {side}: <60s remaining, attempting taker order")
            taker_price = self.strategy.get_taker_price(token_id)
            success, result = self.strategy.place_taker_order(
                token_id, self.strategy.trade_size, tracked.neg_risk
            )
            if success:
                self.log(f"  RESCUE {side}: Taker order placed")
                if side == "UP":
                    tracked.up_filled = True
                    tracked.up_rescue_attempted = True
                else:
                    tracked.down_filled = True
                    tracked.down_rescue_attempted = True
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
                # Mark as attempted so we don't spam taker orders
                if side == "UP":
                    tracked.up_rescue_attempted = True
                else:
                    tracked.down_rescue_attempted = True
            return

        elif time_remaining < 120:  # 1-2 min
            max_price = 0.65
            aggression = 0.8  # Very aggressive - 80% toward ask
        elif time_remaining < 300:  # 2-5 min
            max_price = 0.62
            aggression = 0.6  # Aggressive - 60% toward ask
        elif time_remaining < 480:  # 5-8 min
            max_price = 0.60
            aggression = 0.4  # Moderate - 40% toward ask
        elif time_remaining < 720:  # 8-12 min
            max_price = 0.58
            aggression = 0.3  # Slightly aggressive - 30% toward ask
        else:  # > 12 min
            max_price = 0.55
            aggression = 0.2  # Start with some aggression - 20% toward ask

        # Get competitive price from orderbook (up to our ceiling)
        # Aggression determines how close to the ask we place
        new_price = self.strategy.get_best_maker_price(
            token_id,
            tracked.tick_size,
            max_price=max_price,
            aggression=aggression
        )

        if new_price is None:
            self.log(f"  RESCUE {side}: Could not get orderbook price")
            return

        # Only update if price is better than current
        # This allows multiple rescues as price ceiling increases
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
                # Don't set rescue_attempted - allow further rescues at higher prices
                if new_order_id and not new_order_id.startswith("Failed"):
                    tracked.up_order_id = new_order_id
            else:
                tracked.down_price = new_price
                # Don't set rescue_attempted - allow further rescues at higher prices
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
            update_rebates_market_status(tracked.slug, "LIVE")
            if not tracked.logged_live:
                self.log(f"MARKET LIVE: {tracked.question}")
                tracked.logged_live = True

        # Check if market has RESOLVED (15 min after start)
        resolution_time = tracked.event_start + timedelta(minutes=15)
        if tracked.status == "LIVE" and now >= resolution_time:
            tracked.status = "RESOLVED"
            update_rebates_market_status(tracked.slug, "RESOLVED")
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

        self.log(f"  Redeeming positions: {tracked.slug}")
        self.log(f"    Condition ID: {tracked.condition_id}")

        # Use standalone redemption module (non-blocking)
        def on_redeem_success(condition_id: str, tx_hash: str):
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

        def on_redeem_error(condition_id: str, error_msg: str):
            self.log(f"  Redemption failed: {error_msg[:100]}")
            tracked.redeem_attempted = True

        # Run redemption in background thread so we don't block the main loop
        redeem_position(
            tracked.condition_id,
            on_success=on_redeem_success,
            on_error=on_redeem_error,
            blocking=False
        )

        # Mark as attempted immediately (callbacks will update status)
        tracked.redeem_attempted = True
        return True

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
                # Also check fills and rescue during UPCOMING - fills can happen anytime!
                self.rescue_unfilled_orders(tracked)

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

            # Persist to database for restart recovery
            event_start_iso = (event_start or datetime.now(timezone.utc)).isoformat()
            save_rebates_market(
                slug=slug,
                question=question,
                condition_id=condition_id,
                up_token=up_token,
                down_token=down_token,
                event_start=event_start_iso,
                up_price=result.up_price,
                down_price=result.down_price,
                neg_risk=neg_risk,
                tick_size=tick_size
            )

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
