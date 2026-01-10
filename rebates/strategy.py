"""
Delta-neutral strategy for 15-minute crypto markets.

Places mirror YES/NO (Up/Down) orders at the same price to capture
maker rebates without directional exposure.
"""
import json
import requests
from typing import Dict, Any, Tuple, Optional
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import TRADE_SIZE, TARGET_PRICE, DRY_RUN


@dataclass
class OrderResult:
    """Result of placing mirror orders."""
    success: bool
    message: str
    up_order_id: str = ""
    down_order_id: str = ""
    up_price: float = 0.0
    down_price: float = 0.0

CLOB_API_BASE = "https://clob.polymarket.com"


class DeltaNeutralStrategy:
    """
    Delta-neutral market making strategy for maker rebates.

    Places equal-sized orders on both Up and Down outcomes at 50% price
    to maximize maker rebates while maintaining delta-neutral exposure.
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
        self.target_price = TARGET_PRICE

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

    def get_best_maker_price(self, token_id: str, tick_size: float = 0.01) -> Optional[float]:
        """
        Get the best price for a maker BUY order by checking the order book.

        Returns best_ask - 2*tick_size to ensure we don't cross the book
        even if the book changes slightly between fetch and order placement.
        Returns None if order book fetch fails.
        """
        try:
            url = f"{CLOB_API_BASE}/book?token_id={token_id}"
            resp = requests.get(url, timeout=5)
            if resp.status_code != 200:
                return None

            data = resp.json()
            asks = data.get("asks", [])

            if not asks:
                # No asks = no sells, we can place at any price
                return self.target_price

            # Find lowest ask (best sell price)
            asks_sorted = sorted(asks, key=lambda x: float(x["price"]))
            best_ask = float(asks_sorted[0]["price"])

            # Place our BUY two ticks below best ask for safety margin
            # This accounts for order book changes between fetch and placement
            maker_price = round(best_ask - (2 * tick_size), 2)

            # Don't go below reasonable bounds (stay above 0.40 for ~50% markets)
            if maker_price < 0.40:
                maker_price = 0.40

            return maker_price

        except Exception as e:
            print(f"Error fetching order book for {token_id[:20]}...: {e}")
            return None

    def place_mirror_orders(self, market: Dict[str, Any]) -> OrderResult:
        """
        Place mirror Up and Down orders at optimal maker prices.

        Checks the order book for each side and places BUY orders just below
        the best ask to ensure maker status.

        This creates a delta-neutral position: regardless of the outcome,
        one side will win and the other will lose, netting to approximately
        zero P&L while earning maker rebates.

        Args:
            market: Market data from the Gamma API

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

            # Get optimal maker prices from order book
            up_price = self.get_best_maker_price(up_token, tick_size)
            down_price = self.get_best_maker_price(down_token, tick_size)

            # Fall back to target price if order book fetch fails
            if up_price is None:
                up_price = self.target_price
                print(f"[{timestamp}] Using fallback price for Up: {up_price}")
            if down_price is None:
                down_price = self.target_price
                print(f"[{timestamp}] Using fallback price for Down: {down_price}")

            if DRY_RUN:
                print(f"[{timestamp}] [DRY RUN] Would place POST-ONLY orders:")
                print(f"  Market: {question}")
                print(f"  Up token: {up_token[:20]}...")
                print(f"  Down token: {down_token[:20]}...")
                print(f"  Up price: {up_price} (from order book)")
                print(f"  Down price: {down_price} (from order book)")
                print(f"  Size: ${self.trade_size} per side")
                print(f"  Neg risk: {neg_risk}")
                print(f"  Post-only: True (maker-only, rejected if would immediately match)")
                return OrderResult(
                    success=True,
                    message="Dry run - orders not placed",
                    up_price=up_price,
                    down_price=down_price
                )

            # Place Up order (BUY on the Up outcome)
            # post_only=True ensures we're a maker (order rejected if it would immediately match)
            print(f"[{timestamp}] Placing Up order: {self.trade_size} @ {up_price} (post-only)")
            up_resp = self.client.create_order(
                marketId=up_token,
                action="BUY",
                price=up_price,
                size=self.trade_size,
                neg_risk=neg_risk,
                post_only=True
            )

            # Check for success - API returns {'success': True, 'status': 'live'} on success
            up_success = up_resp and up_resp.get("success") == True
            if not up_success:
                error_msg = up_resp.get("errorMsg", "") if up_resp else "Empty response"
                return OrderResult(success=False, message=f"Failed to place Up order: {error_msg}")

            up_order_id = up_resp.get("orderID", "")
            print(f"[{timestamp}] Up order placed: {up_order_id[:20]}...")

            # Place Down order (BUY on the Down outcome)
            print(f"[{timestamp}] Placing Down order: {self.trade_size} @ {down_price} (post-only)")
            down_resp = self.client.create_order(
                marketId=down_token,
                action="BUY",
                price=down_price,
                size=self.trade_size,
                neg_risk=neg_risk,
                post_only=True
            )

            # Check for success
            down_success = down_resp and down_resp.get("success") == True
            if not down_success:
                # Try to cancel the Up order if Down failed
                print(f"[{timestamp}] Down order failed, cancelling Up order")
                try:
                    self.client.cancel_all_asset(up_token)
                except Exception:
                    pass
                error_msg = down_resp.get("errorMsg", "") if down_resp else "Empty response"
                return OrderResult(success=False, message=f"Failed to place Down order: {error_msg}")

            down_order_id = down_resp.get("orderID", "")
            print(f"[{timestamp}] Down order placed: {down_order_id[:20]}...")

            return OrderResult(
                success=True,
                message=f"Placed mirror orders @ Up:{up_price}/Down:{down_price} on {slug}",
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

    def check_order_competitiveness(
        self,
        token_id: str,
        current_price: float,
        tick_size: float = 0.01
    ) -> Tuple[bool, Optional[float]]:
        """
        Check if our order is still at a competitive price.

        Returns:
            Tuple of (is_competitive, new_best_price)
            - is_competitive: True if our price is within 1 tick of optimal
            - new_best_price: The current optimal maker price
        """
        best_price = self.get_best_maker_price(token_id, tick_size)
        if best_price is None:
            return True, None  # Can't check, assume OK

        # We're competitive if we're within 1 tick of the best maker price
        price_diff = abs(best_price - current_price)
        is_competitive = price_diff <= tick_size

        return is_competitive, best_price

    def update_single_order(
        self,
        token_id: str,
        new_price: float,
        neg_risk: bool
    ) -> Tuple[bool, str]:
        """
        Cancel existing order and place a new one at the new price.

        Returns:
            Tuple of (success, new_order_id)
        """
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        if DRY_RUN:
            print(f"[{timestamp}] [DRY RUN] Would update order to {new_price}")
            return True, ""

        try:
            # Cancel existing order for this token
            self.client.cancel_all_asset(token_id)

            # Place new order at better price
            resp = self.client.create_order(
                marketId=token_id,
                action="BUY",
                price=new_price,
                size=self.trade_size,
                neg_risk=neg_risk,
                post_only=True
            )

            if resp and resp.get("success") == True:
                order_id = resp.get("orderID", "")
                return True, order_id
            else:
                error_msg = resp.get("errorMsg", "") if resp else "Empty response"
                return False, f"Failed: {error_msg}"

        except Exception as e:
            return False, f"Error: {e}"
