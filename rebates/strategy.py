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

    def _calculate_vwap(
        self,
        orders: list,
        min_order_size: float = 5.0,
        depth_limit: int = 5
    ) -> Optional[float]:
        """
        Calculate Volume-Weighted Average Price for orders.

        This protects against spoofing attacks where someone places small
        fake orders to manipulate the bot's pricing.

        Args:
            orders: List of order dicts with 'price' and 'size' keys
            min_order_size: Minimum $ size to consider (filters small spoof orders)
            depth_limit: Max number of price levels to consider

        Returns:
            VWAP or None if no valid orders
        """
        if not orders:
            return None

        # Filter out small orders that might be spoofing
        valid_orders = []
        for order in orders[:depth_limit]:
            price = float(order.get("price", 0))
            size = float(order.get("size", 0))
            dollar_value = price * size
            if dollar_value >= min_order_size:
                valid_orders.append((price, size))

        if not valid_orders:
            # Fall back to best price if all orders are small
            return float(orders[0].get("price", 0)) if orders else None

        # Calculate VWAP
        total_value = sum(price * size for price, size in valid_orders)
        total_size = sum(size for _, size in valid_orders)

        if total_size == 0:
            return None

        return total_value / total_size

    def get_best_maker_price(
        self,
        token_id: str,
        tick_size: float = 0.01,
        max_price: Optional[float] = None,
        min_price: float = 0.40,
        aggression: float = 0.0
    ) -> Optional[float]:
        """
        Get the best price for a maker BUY order using VWAP-based pricing.

        Uses Volume-Weighted Average Price instead of just the best bid to
        protect against spoofing attacks where someone places small fake
        orders to manipulate the bot's pricing.

        Strategy:
        - max_safe_price = best_ask - 1*tick_size (don't cross the book)
        - vwap_bid = volume-weighted average of top bids (ignoring small orders)
        - spread = best_ask - vwap_bid
        - competitive_price = vwap_bid + tick_size + (spread * aggression)
        - our_price = min(competitive_price, max_safe_price, max_price)

        Args:
            token_id: The token to get price for
            tick_size: Minimum price increment
            max_price: Optional ceiling for our price (for rescue scenarios)
            min_price: Floor for our price (default 0.40)
            aggression: How much of the spread to cross (0.0 = at bid, 1.0 = at ask)
                       - 0.0: Conservative, place at top of bid queue
                       - 0.5: Place halfway between bid and ask
                       - 0.8: Aggressive, place very close to ask

        Returns None if order book fetch fails.
        """
        if max_price is None:
            max_price = self.target_price

        try:
            url = f"{CLOB_API_BASE}/book?token_id={token_id}"
            resp = requests.get(url, timeout=5)
            if resp.status_code != 200:
                return None

            data = resp.json()
            asks = data.get("asks", [])
            bids = data.get("bids", [])

            # Calculate maximum safe price (don't cross the book)
            # Only need 1 tick buffer to avoid crossing
            if asks:
                asks_sorted = sorted(asks, key=lambda x: float(x["price"]))
                best_ask = float(asks_sorted[0]["price"])
                max_safe_price = round(best_ask - tick_size, 2)
            else:
                # No asks = no sellers, we can place at any price up to max
                best_ask = None
                max_safe_price = max_price

            # Calculate competitive price using VWAP (protects against spoofing)
            if bids:
                bids_sorted = sorted(bids, key=lambda x: float(x["price"]), reverse=True)
                best_bid = float(bids_sorted[0]["price"])

                # Use VWAP instead of just best bid
                vwap_bid = self._calculate_vwap(bids_sorted, min_order_size=5.0, depth_limit=5)
                if vwap_bid is None:
                    vwap_bid = best_bid

                # Calculate spread and apply aggression
                if best_ask is not None:
                    spread = best_ask - vwap_bid
                    # Base price is 1 tick above VWAP, then add aggression portion of spread
                    competitive_price = round(vwap_bid + tick_size + (spread * aggression), 2)
                else:
                    # No asks, just go 1 tick above bid
                    competitive_price = round(vwap_bid + tick_size, 2)
            else:
                # No bids = no competition, use a reasonable default
                best_bid = None
                vwap_bid = None
                competitive_price = 0.45

            # Use the competitive price, but don't exceed max safe price or our ceiling
            maker_price = min(competitive_price, max_safe_price, max_price)

            # Don't go below minimum
            if maker_price < min_price:
                maker_price = min_price

            # Log pricing details for monitoring
            best_bid_str = f"{best_bid:.2f}" if best_bid else "none"
            vwap_str = f"{vwap_bid:.2f}" if vwap_bid is not None else "none"
            best_ask_str = f"{best_ask:.2f}" if best_ask else "none"
            agg_str = f" agg={aggression:.0%}" if aggression > 0 else ""
            print(f"  Pricing: bid={best_bid_str} vwap={vwap_str} ask={best_ask_str}{agg_str} -> competitive={competitive_price:.2f} safe={max_safe_price:.2f} max={max_price:.2f} -> final={maker_price:.2f}")

            return maker_price

        except Exception as e:
            print(f"Error fetching order book for {token_id[:20]}...: {e}")
            return None

    def get_taker_price(self, token_id: str) -> Optional[float]:
        """
        Get the price to immediately fill (cross the spread).

        This is used as a last resort to ensure delta-neutrality.
        Returns the best ask price (what we'd pay to buy immediately).
        """
        try:
            url = f"{CLOB_API_BASE}/book?token_id={token_id}"
            resp = requests.get(url, timeout=5)
            if resp.status_code != 200:
                return None

            data = resp.json()
            asks = data.get("asks", [])

            if asks:
                asks_sorted = sorted(asks, key=lambda x: float(x["price"]))
                best_ask = float(asks_sorted[0]["price"])
                print(f"  Taker price (best ask): {best_ask:.2f}")
                return best_ask
            else:
                # No asks - can't market buy
                return None

        except Exception as e:
            print(f"Error fetching taker price for {token_id[:20]}...: {e}")
            return None

    def place_taker_order(
        self,
        token_id: str,
        size: float,
        neg_risk: bool
    ) -> Tuple[bool, str]:
        """
        Place a taker order (market buy) to immediately fill.

        Used as last resort rescue when maker orders aren't getting filled.

        Returns:
            Tuple of (success, order_id or error message)
        """
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        if DRY_RUN:
            print(f"[{timestamp}] [DRY RUN] Would place taker (market) order")
            return True, ""

        taker_price = self.get_taker_price(token_id)
        if taker_price is None:
            return False, "Could not get taker price"

        # Cap at reasonable price to avoid disasters
        # Using 0.70 as cap - better to lose 20% than 100% at resolution
        if taker_price > 0.70:
            print(f"[{timestamp}] Taker price too high ({taker_price:.2f}), skipping rescue")
            return False, f"Taker price too high: {taker_price}"

        try:
            print(f"[{timestamp}] Placing TAKER order: {size} @ {taker_price} (crossing spread)")

            # Regular order (not post_only) will cross the spread
            resp = self.client.create_order(
                marketId=token_id,
                action="BUY",
                price=taker_price,
                size=size,
                neg_risk=neg_risk,
                post_only=False  # Allow taker fills
            )

            if resp and resp.get("success") == True:
                order_id = resp.get("orderID", "")
                print(f"[{timestamp}] Taker order placed: {order_id[:20]}...")
                return True, order_id
            else:
                error_msg = resp.get("errorMsg", "") if resp else "Empty response"
                return False, f"Failed: {error_msg}"

        except Exception as e:
            return False, f"Error: {e}"

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
