"""
Delta-neutral strategy for 15-minute crypto markets.

"Only 50" Strategy:
- Always place BUY orders at exactly 0.50 on both UP and DOWN sides
- Let orders sit until they fill (no competitive updates, no rescue)
- When price crosses 50, orders fill automatically
- The only failure mode is execution risk (order doesn't fill when price crosses)

This is dramatically simpler than dynamic pricing because:
1. If both sides fill at 50: delta-neutral, earn rebates on both
2. If one side fills: order at 50 on other side will fill when outcome crosses 50
3. No need to chase, update, or rescue - the order at 50 IS the rescue
"""
import json
import time
from typing import Dict, Any, Tuple
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import TRADE_SIZE, FIXED_PRICE, DRY_RUN


@dataclass
class OrderResult:
    """Result of placing mirror orders."""
    success: bool
    message: str
    up_order_id: str = ""
    down_order_id: str = ""
    up_price: float = 0.0
    down_price: float = 0.0


class DeltaNeutralStrategy:
    """
    Delta-neutral market making strategy using "Only 50" approach.

    Places equal-sized orders on both Up and Down outcomes at exactly 0.50
    to maximize maker rebates while maintaining delta-neutral exposure.

    Key insight: In a binary market, if the outcome crosses 50, the price
    must cross 50. So an order sitting at 50 will fill when needed.
    """

    def __init__(self, client, trade_size: float = None):
        """
        Initialize the strategy.

        Args:
            client: PolymarketClient instance
            trade_size: Override trade size per side (default from config)
        """
        self.client = client
        self.trade_size = trade_size if trade_size is not None else TRADE_SIZE

    def get_tokens(self, market: Dict[str, Any]) -> Tuple[str, str]:
        """
        Extract Up and Down token IDs from market data.

        Returns:
            Tuple of (up_token_id, down_token_id)
        """
        clob_tokens = market.get("clobTokenIds", [])

        # API returns clobTokenIds as a JSON string, need to parse it
        if isinstance(clob_tokens, str):
            try:
                clob_tokens = json.loads(clob_tokens)
            except json.JSONDecodeError:
                raise ValueError(f"Failed to parse clobTokenIds: {clob_tokens}")

        if len(clob_tokens) < 2:
            raise ValueError(f"Market missing token IDs: {market.get('slug', 'unknown')}")

        # First token is Up, second is Down
        return clob_tokens[0], clob_tokens[1]

    def is_neg_risk_market(self, market: Dict[str, Any]) -> bool:
        """Check if this is a negative risk market."""
        neg_risk = market.get("negRisk")
        if neg_risk is None:
            neg_risk = market.get("neg_risk")
        return neg_risk == True or neg_risk == "TRUE" or neg_risk == "true"

    def _place_single_order(
        self,
        token_id: str,
        neg_risk: bool,
        side_name: str,
        tick_size: float = 0.01
    ) -> Tuple[bool, str, str]:
        """
        Place a single BUY order at FIXED_PRICE (0.50).

        Includes retry logic for "crosses book" errors - reduces price by 1 tick.

        Args:
            token_id: Token to buy
            neg_risk: Whether market uses negative risk adapter
            side_name: "UP" or "DOWN" for logging
            tick_size: Price increment for retries

        Returns:
            Tuple of (success, order_id, error_message)
        """
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        current_price = FIXED_PRICE
        max_retries = 3  # Allow reducing price up to 3 ticks (0.50 -> 0.47)
        min_price = 0.45  # Don't go below this

        for attempt in range(max_retries):
            print(f"[{timestamp}] Placing {side_name} order: {self.trade_size} @ {current_price} (post-only)")

            try:
                resp = self.client.create_order(
                    marketId=token_id,
                    action="BUY",
                    price=current_price,
                    size=self.trade_size,
                    neg_risk=neg_risk,
                    post_only=True
                )

                if resp and resp.get("success") == True:
                    order_id = resp.get("orderID", "")
                    print(f"[{timestamp}] {side_name} order placed: {order_id[:20]}...")
                    return True, order_id, ""

                # Check for "crosses book" error
                error_msg = str(resp.get("errorMsg", "")) if resp else ""
                if "cross" in error_msg.lower():
                    # Price would cross the book - try lower price
                    current_price = round(current_price - tick_size, 2)
                    if current_price < min_price:
                        return False, "", f"Price too low after retries: {current_price}"
                    print(f"[{timestamp}] {side_name} order crossed book, reducing to {current_price}")
                    time.sleep(0.1)
                    continue
                else:
                    return False, "", error_msg or "Unknown error"

            except Exception as e:
                error_str = str(e).lower()
                if "cross" in error_str:
                    current_price = round(current_price - tick_size, 2)
                    if current_price < min_price:
                        return False, "", f"Price too low after retries: {current_price}"
                    print(f"[{timestamp}] {side_name} order crossed book (exception), reducing to {current_price}")
                    time.sleep(0.1)
                    continue
                else:
                    return False, "", str(e)

        return False, "", "Max retries exceeded"

    def place_mirror_orders(
        self,
        market: Dict[str, Any],
        skip_up: bool = False,
        skip_down: bool = False
    ) -> OrderResult:
        """
        Place mirror Up and Down orders at FIXED_PRICE (0.50).

        This creates a delta-neutral position: regardless of the outcome,
        one side will win and the other will lose, netting to approximately
        zero P&L while earning maker rebates.

        Args:
            market: Market data from the Gamma API
            skip_up: If True, skip placing Up order (use when long Up)
            skip_down: If True, skip placing Down order (use when long Down)

        Returns:
            OrderResult with success status, message, and order details
        """
        slug = market.get("slug", "unknown")
        question = market.get("question", "Unknown market")
        tick_size = float(market.get("orderPriceMinTickSize", 0.01))

        try:
            up_token, down_token = self.get_tokens(market)
            neg_risk = self.is_neg_risk_market(market)

            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

            if DRY_RUN:
                print(f"[{timestamp}] [DRY RUN] Would place POST-ONLY orders:")
                print(f"  Market: {question}")
                print(f"  Up token: {up_token[:20]}...")
                print(f"  Down token: {down_token[:20]}...")
                print(f"  Price: {FIXED_PRICE} (fixed){' [UP SKIP]' if skip_up else ''}{' [DOWN SKIP]' if skip_down else ''}")
                print(f"  Size: ${self.trade_size} per side")
                print(f"  Neg risk: {neg_risk}")
                return OrderResult(
                    success=True,
                    message="Dry run - orders not placed",
                    up_price=FIXED_PRICE,
                    down_price=FIXED_PRICE
                )

            up_order_id = ""
            down_order_id = ""
            up_price = FIXED_PRICE
            down_price = FIXED_PRICE

            # Place Up order
            if skip_up:
                print(f"[{timestamp}] SKIPPING Up order (position imbalance)")
            else:
                success, order_id, error = self._place_single_order(
                    up_token, neg_risk, "UP", tick_size
                )
                if not success:
                    return OrderResult(success=False, message=f"Failed to place Up order: {error}")
                up_order_id = order_id

            # Place Down order
            if skip_down:
                print(f"[{timestamp}] SKIPPING Down order (position imbalance)")
            else:
                success, order_id, error = self._place_single_order(
                    down_token, neg_risk, "DOWN", tick_size
                )
                if not success:
                    # Cancel Up order if Down failed
                    if not skip_up:
                        print(f"[{timestamp}] Down order failed, cancelling Up order")
                        try:
                            self.client.cancel_all_asset(up_token)
                        except Exception:
                            pass
                    return OrderResult(success=False, message=f"Failed to place Down order: {error}")
                down_order_id = order_id

            # Build result message
            if skip_up and skip_down:
                msg = f"Both orders skipped (imbalance) on {slug}"
            elif skip_up:
                msg = f"Placed Down @ {FIXED_PRICE} on {slug} (Up skipped)"
            elif skip_down:
                msg = f"Placed Up @ {FIXED_PRICE} on {slug} (Down skipped)"
            else:
                msg = f"Placed mirror orders @ {FIXED_PRICE} on {slug}"

            return OrderResult(
                success=True,
                message=msg,
                up_order_id=up_order_id,
                down_order_id=down_order_id,
                up_price=up_price,
                down_price=down_price
            )

        except Exception as e:
            return OrderResult(success=False, message=f"Error placing orders: {e}")

    def get_existing_orders(self, market: Dict[str, Any]) -> Dict[str, Any]:
        """
        Get existing orders for this market.

        Returns:
            Dict with 'up' and 'down' order info, or empty if none
        """
        try:
            up_token, down_token = self.get_tokens(market)
            all_orders = self.client.get_all_orders()

            if all_orders.empty:
                return {"up": None, "down": None}

            up_orders = all_orders[all_orders["asset_id"] == up_token]
            down_orders = all_orders[all_orders["asset_id"] == down_token]

            return {
                "up": up_orders.to_dict("records") if not up_orders.empty else None,
                "down": down_orders.to_dict("records") if not down_orders.empty else None,
            }
        except Exception:
            return {"up": None, "down": None}

    def cancel_market_orders(self, market: Dict[str, Any]) -> bool:
        """
        Cancel all orders for this market.

        Returns:
            True if cancelled successfully
        """
        if DRY_RUN:
            print("[DRY RUN] Would cancel orders")
            return True

        try:
            up_token, down_token = self.get_tokens(market)
            self.client.cancel_all_asset(up_token)
            self.client.cancel_all_asset(down_token)
            return True
        except Exception as e:
            print(f"Error cancelling orders: {e}")
            return False
