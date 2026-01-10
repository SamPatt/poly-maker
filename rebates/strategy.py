"""
Delta-neutral strategy for 15-minute crypto markets.

Places mirror YES/NO (Up/Down) orders at the same price to capture
maker rebates without directional exposure.
"""
import json
from typing import Dict, Any, Tuple, Optional
from datetime import datetime, timezone

from .config import TRADE_SIZE, TARGET_PRICE, DRY_RUN


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

    def place_mirror_orders(self, market: Dict[str, Any]) -> Tuple[bool, str]:
        """
        Place mirror Up and Down orders at the same price.

        This creates a delta-neutral position: regardless of the outcome,
        one side will win and the other will lose, netting to approximately
        zero P&L while earning maker rebates.

        Args:
            market: Market data from the Gamma API

        Returns:
            Tuple of (success, message)
        """
        slug = market.get("slug", "unknown")
        question = market.get("question", "Unknown market")

        try:
            up_token, down_token = self.get_tokens(market)
            neg_risk = self.is_neg_risk_market(market)

            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

            if DRY_RUN:
                print(f"[{timestamp}] [DRY RUN] Would place POST-ONLY orders:")
                print(f"  Market: {question}")
                print(f"  Up token: {up_token[:20]}...")
                print(f"  Down token: {down_token[:20]}...")
                print(f"  Price: {self.target_price}")
                print(f"  Size: ${self.trade_size} per side")
                print(f"  Neg risk: {neg_risk}")
                print(f"  Post-only: True (maker-only, rejected if would immediately match)")
                return True, "Dry run - orders not placed"

            # Place Up order (BUY on the Up outcome)
            # post_only=True ensures we're a maker (order rejected if it would immediately match)
            print(f"[{timestamp}] Placing Up order: {self.trade_size} @ {self.target_price} (post-only)")
            up_resp = self.client.create_order(
                marketId=up_token,
                action="BUY",
                price=self.target_price,
                size=self.trade_size,
                neg_risk=neg_risk,
                post_only=True
            )

            if not up_resp or "error" in str(up_resp).lower():
                # Post-only rejection means our order would have been a taker
                if "post" in str(up_resp).lower() or "reject" in str(up_resp).lower():
                    return False, f"Up order rejected (would be taker): {up_resp}"
                return False, f"Failed to place Up order: {up_resp}"

            # Place Down order (BUY on the Down outcome)
            print(f"[{timestamp}] Placing Down order: {self.trade_size} @ {self.target_price} (post-only)")
            down_resp = self.client.create_order(
                marketId=down_token,
                action="BUY",
                price=self.target_price,
                size=self.trade_size,
                neg_risk=neg_risk,
                post_only=True
            )

            if not down_resp or "error" in str(down_resp).lower():
                # Try to cancel the Up order if Down failed
                print(f"[{timestamp}] Down order failed, cancelling Up order")
                try:
                    self.client.cancel_all_asset(up_token)
                except Exception:
                    pass
                if "post" in str(down_resp).lower() or "reject" in str(down_resp).lower():
                    return False, f"Down order rejected (would be taker): {down_resp}"
                return False, f"Failed to place Down order: {down_resp}"

            return True, f"Placed POST-ONLY mirror orders on {slug}"

        except Exception as e:
            return False, f"Error placing orders: {e}"

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
