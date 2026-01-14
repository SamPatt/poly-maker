"""
OrderManager - Order placement and management for active quoting.

Implements:
- feeRateBps handling (fetch via GET /fee-rate?token_id=...)
- Post-only flag enforcement
- Batch order placement (up to 15 orders per request)
- Cancel/replace logic with hysteresis
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple, Any

import aiohttp

from .config import ActiveQuotingConfig
from .models import OrderState, OrderStatus, OrderSide, Quote

logger = logging.getLogger(__name__)


@dataclass
class FeeRateCache:
    """Cached fee rate for a token."""
    fee_rate_bps: int
    cached_at: float


@dataclass
class OrderResult:
    """Result of an order operation."""
    success: bool
    order_id: Optional[str] = None
    error_msg: str = ""
    order_state: Optional[OrderState] = None


@dataclass
class BatchOrderResult:
    """Result of a batch order operation."""
    successful_orders: List[OrderResult] = field(default_factory=list)
    failed_orders: List[OrderResult] = field(default_factory=list)

    @property
    def all_succeeded(self) -> bool:
        return len(self.failed_orders) == 0 and len(self.successful_orders) > 0


class OrderManager:
    """
    Manages order placement, cancellation, and fee handling for active quoting.

    This class:
    1. Fetches and caches fee rates for 15-minute markets
    2. Places orders with proper fee handling and post-only flag
    3. Supports batch order placement (up to 15 orders)
    4. Tracks pending and active orders
    5. Provides cancel/replace functionality
    """

    # Polymarket API endpoints
    BASE_URL = "https://clob.polymarket.com"
    FEE_RATE_ENDPOINT = "/fee-rate"
    ORDERS_ENDPOINT = "/orders"
    BATCH_ORDERS_ENDPOINT = "/orders/batch"

    def __init__(
        self,
        config: ActiveQuotingConfig,
        api_key: str,
        api_secret: str,
        api_passphrase: str,
        poly_client: Any = None,  # Optional PolymarketClient for actual order signing
    ):
        """
        Initialize the OrderManager.

        Args:
            config: Active quoting configuration
            api_key: Polymarket API key
            api_secret: Polymarket API secret
            api_passphrase: Polymarket API passphrase
            poly_client: Optional PolymarketClient instance for order signing
        """
        self.config = config
        self._api_key = api_key
        self._api_secret = api_secret
        self._api_passphrase = api_passphrase
        self._poly_client = poly_client

        # Fee rate cache: token_id -> FeeRateCache
        self._fee_cache: Dict[str, FeeRateCache] = {}

        # Pending orders waiting for confirmation
        self._pending_orders: Dict[str, OrderState] = {}

        # HTTP session for async requests
        self._session: Optional[aiohttp.ClientSession] = None

        # Rate limiting
        self._last_order_time: float = 0
        self._orders_in_window: int = 0
        self._rate_limit_window_start: float = 0

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Ensure HTTP session is created."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers=self._get_auth_headers(),
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    def _get_auth_headers(self) -> Dict[str, str]:
        """Get authentication headers for API requests."""
        return {
            "POLY_API_KEY": self._api_key,
            "POLY_API_SECRET": self._api_secret,
            "POLY_PASSPHRASE": self._api_passphrase,
            "Content-Type": "application/json",
        }

    async def close(self) -> None:
        """Close HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # --- Fee Rate Management ---

    async def get_fee_rate(self, token_id: str) -> int:
        """
        Get the fee rate for a token, with caching.

        Args:
            token_id: The token ID to get fee rate for

        Returns:
            Fee rate in basis points (e.g., 100 = 1%)
        """
        now = time.time()

        # Check cache
        if token_id in self._fee_cache:
            cached = self._fee_cache[token_id]
            if now - cached.cached_at < self.config.fee_cache_ttl_seconds:
                return cached.fee_rate_bps

        # Fetch from API
        fee_rate = await self._fetch_fee_rate(token_id)
        self._fee_cache[token_id] = FeeRateCache(
            fee_rate_bps=fee_rate,
            cached_at=now,
        )
        return fee_rate

    async def _fetch_fee_rate(self, token_id: str) -> int:
        """
        Fetch fee rate from API.

        Args:
            token_id: The token ID

        Returns:
            Fee rate in basis points
        """
        session = await self._ensure_session()

        try:
            url = f"{self.BASE_URL}{self.FEE_RATE_ENDPOINT}?token_id={token_id}"
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    # API may return feeRateBps or fee_rate_bps
                    fee_rate = data.get("feeRateBps") or data.get("fee_rate_bps", 0)
                    logger.debug(f"Fee rate for {token_id[:20]}...: {fee_rate} bps")
                    return int(fee_rate)
                else:
                    logger.warning(
                        f"Failed to fetch fee rate for {token_id[:20]}...: "
                        f"status={response.status}"
                    )
                    return 0
        except Exception as e:
            logger.error(f"Error fetching fee rate: {e}")
            return 0

    def clear_fee_cache(self, token_id: Optional[str] = None) -> None:
        """
        Clear fee rate cache.

        Args:
            token_id: Specific token to clear, or None to clear all
        """
        if token_id:
            self._fee_cache.pop(token_id, None)
        else:
            self._fee_cache.clear()

    # --- Order Placement ---

    async def place_order(
        self,
        token_id: str,
        side: OrderSide,
        price: float,
        size: float,
        neg_risk: bool = False,
        post_only: Optional[bool] = None,
    ) -> OrderResult:
        """
        Place a single order with fee handling and post-only flag.

        Args:
            token_id: Token to trade
            side: BUY or SELL
            price: Order price
            size: Order size in USDC
            neg_risk: Whether this is a negative risk market
            post_only: Override post_only setting (defaults to config.post_only)

        Returns:
            OrderResult with success status and order details
        """
        if self.config.dry_run:
            return self._simulate_order(token_id, side, price, size)

        if not self._poly_client:
            return OrderResult(
                success=False,
                error_msg="No PolymarketClient configured for live orders"
            )

        try:
            # Get fee rate
            fee_rate_bps = await self.get_fee_rate(token_id)

            # Determine post_only setting (parameter overrides config)
            use_post_only = post_only if post_only is not None else self.config.post_only

            # Create and place order using PolymarketClient
            # The PolymarketClient.create_order method handles signing
            response = self._poly_client.create_order(
                marketId=token_id,
                action=side.value,
                price=price,
                size=size,
                neg_risk=neg_risk,
                post_only=use_post_only,
            )

            if response.get("success") is False:
                error_msg = response.get("errorMsg", "Unknown error")
                logger.warning(
                    f"Order rejected: {side.value} {size:.2f} @ {price:.4f} "
                    f"for {token_id[:20]}... - {error_msg}"
                )
                return OrderResult(
                    success=False,
                    error_msg=error_msg,
                )

            order_id = response.get("orderID") or response.get("id")

            # Create order state
            order_state = OrderState(
                order_id=order_id or "",
                token_id=token_id,
                side=side,
                price=price,
                original_size=size,
                remaining_size=size,
                status=OrderStatus.PENDING,
                post_only=use_post_only,
                fee_rate_bps=fee_rate_bps,
            )

            if order_id:
                self._pending_orders[order_id] = order_state

            return OrderResult(
                success=True,
                order_id=order_id,
                order_state=order_state,
            )

        except Exception as e:
            logger.error(
                f"Error placing {side.value} order: {e} "
                f"(token={token_id[:20]}... price={price:.4f} size={size:.2f})"
            )
            return OrderResult(
                success=False,
                error_msg=str(e),
            )

    def _simulate_order(
        self,
        token_id: str,
        side: OrderSide,
        price: float,
        size: float,
    ) -> OrderResult:
        """Simulate order placement in dry run mode."""
        import uuid
        order_id = f"sim_{uuid.uuid4().hex[:12]}"

        order_state = OrderState(
            order_id=order_id,
            token_id=token_id,
            side=side,
            price=price,
            original_size=size,
            remaining_size=size,
            status=OrderStatus.OPEN,  # Simulated orders go straight to OPEN
            post_only=True,
            fee_rate_bps=0,
        )

        self._pending_orders[order_id] = order_state

        logger.info(
            f"[DRY RUN] Placed {side.value} order: "
            f"price={price:.4f} size={size:.2f} token={token_id[:20]}..."
        )

        return OrderResult(
            success=True,
            order_id=order_id,
            order_state=order_state,
        )

    async def place_quote(
        self,
        quote: Quote,
        neg_risk: bool = False,
    ) -> Tuple[OrderResult, OrderResult]:
        """
        Place a two-sided quote (bid and ask orders).

        Args:
            quote: Quote containing bid and ask prices/sizes
            neg_risk: Whether this is a negative risk market

        Returns:
            Tuple of (bid_result, ask_result)
        """
        # Place bid and ask in parallel
        bid_task = self.place_order(
            token_id=quote.token_id,
            side=OrderSide.BUY,
            price=quote.bid_price,
            size=quote.bid_size,
            neg_risk=neg_risk,
        )

        ask_task = self.place_order(
            token_id=quote.token_id,
            side=OrderSide.SELL,
            price=quote.ask_price,
            size=quote.ask_size,
            neg_risk=neg_risk,
        )

        bid_result, ask_result = await asyncio.gather(bid_task, ask_task)
        return bid_result, ask_result

    async def place_orders_batch(
        self,
        orders: List[Tuple[str, OrderSide, float, float, bool]],
    ) -> BatchOrderResult:
        """
        Place multiple orders in a batch (up to batch_size per request).

        Args:
            orders: List of (token_id, side, price, size, neg_risk) tuples

        Returns:
            BatchOrderResult with successful and failed orders
        """
        if not orders:
            return BatchOrderResult()

        result = BatchOrderResult()

        # Process in batches of batch_size
        for i in range(0, len(orders), self.config.batch_size):
            batch = orders[i:i + self.config.batch_size]

            # For now, place orders individually in parallel
            # TODO: Use actual batch API when available
            tasks = [
                self.place_order(token_id, side, price, size, neg_risk)
                for token_id, side, price, size, neg_risk in batch
            ]

            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            for order_result in batch_results:
                if isinstance(order_result, Exception):
                    result.failed_orders.append(
                        OrderResult(success=False, error_msg=str(order_result))
                    )
                elif order_result.success:
                    result.successful_orders.append(order_result)
                else:
                    result.failed_orders.append(order_result)

        return result

    # --- Order Cancellation ---

    async def cancel_order(self, order_id: str) -> bool:
        """
        Cancel a single order.

        Args:
            order_id: The order ID to cancel

        Returns:
            True if cancellation succeeded
        """
        if self.config.dry_run:
            return self._simulate_cancel(order_id)

        if not self._poly_client:
            logger.error("No PolymarketClient configured for cancellation")
            return False

        try:
            # Use py-clob-client's cancel method
            self._poly_client.client.cancel(order_id)

            # Update pending orders
            if order_id in self._pending_orders:
                self._pending_orders[order_id].status = OrderStatus.CANCELLED
                self._pending_orders[order_id].updated_at = datetime.utcnow()

            logger.debug(f"Cancelled order: {order_id}")
            return True

        except Exception as e:
            logger.error(f"Error cancelling order {order_id}: {e}")
            return False

    def _simulate_cancel(self, order_id: str) -> bool:
        """Simulate order cancellation in dry run mode."""
        if order_id in self._pending_orders:
            self._pending_orders[order_id].status = OrderStatus.CANCELLED
            self._pending_orders[order_id].updated_at = datetime.utcnow()
            logger.info(f"[DRY RUN] Cancelled order: {order_id}")
            return True
        return False

    async def cancel_all_for_token(self, token_id: str) -> int:
        """
        Cancel all orders for a specific token.

        Args:
            token_id: The token ID

        Returns:
            Number of orders cancelled
        """
        if self.config.dry_run:
            return self._simulate_cancel_all_for_token(token_id)

        if not self._poly_client:
            logger.error("No PolymarketClient configured for cancellation")
            return 0

        try:
            self._poly_client.cancel_all_asset(token_id)

            # Update pending orders
            cancelled = 0
            for order_id, order in list(self._pending_orders.items()):
                if order.token_id == token_id and order.is_open():
                    order.status = OrderStatus.CANCELLED
                    order.updated_at = datetime.utcnow()
                    cancelled += 1

            logger.info(f"Cancelled {cancelled} orders for token {token_id[:20]}...")
            return cancelled

        except Exception as e:
            logger.error(f"Error cancelling orders for {token_id}: {e}")
            return 0

    def _simulate_cancel_all_for_token(self, token_id: str) -> int:
        """Simulate cancellation in dry run mode."""
        cancelled = 0
        for order_id, order in list(self._pending_orders.items()):
            if order.token_id == token_id and order.is_open():
                order.status = OrderStatus.CANCELLED
                order.updated_at = datetime.utcnow()
                cancelled += 1
        logger.info(f"[DRY RUN] Cancelled {cancelled} orders for token {token_id[:20]}...")
        return cancelled

    async def cancel_all(self) -> int:
        """
        Cancel all open orders.

        Returns:
            Number of orders cancelled
        """
        # Get unique tokens
        tokens = set(
            order.token_id
            for order in self._pending_orders.values()
            if order.is_open()
        )

        total_cancelled = 0
        for token_id in tokens:
            cancelled = await self.cancel_all_for_token(token_id)
            total_cancelled += cancelled

        return total_cancelled

    # --- Order State Management ---

    def get_pending_orders(self, token_id: Optional[str] = None) -> List[OrderState]:
        """
        Get pending orders.

        Args:
            token_id: Filter by token ID (optional)

        Returns:
            List of pending order states
        """
        orders = [
            order for order in self._pending_orders.values()
            if order.is_open()
        ]

        if token_id:
            orders = [o for o in orders if o.token_id == token_id]

        return orders

    def get_order(self, order_id: str) -> Optional[OrderState]:
        """Get order state by ID."""
        return self._pending_orders.get(order_id)

    def update_order_state(self, order_id: str, status: OrderStatus) -> None:
        """
        Update order status.

        Called when receiving updates from UserChannelManager.
        """
        if order_id in self._pending_orders:
            self._pending_orders[order_id].status = status
            self._pending_orders[order_id].updated_at = datetime.utcnow()

    def reconcile_with_api_orders(self, api_orders: List[Dict[str, Any]]) -> None:
        """
        Reconcile internal order state with orders fetched from REST API.

        This ensures cancel_all() and open-order tracking reflect reality
        after reconnects or missed WebSocket updates.
        """
        api_order_ids: Set[str] = set()

        for api_order in api_orders or []:
            order_id = api_order.get("id") or api_order.get("order_id")
            if not order_id:
                continue

            api_order_ids.add(order_id)

            status_str = api_order.get("status", "").upper()
            status_map = {
                "PENDING": OrderStatus.PENDING,
                "OPEN": OrderStatus.OPEN,
                "LIVE": OrderStatus.OPEN,
                "MATCHED": OrderStatus.PARTIALLY_FILLED,
                "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
                "FILLED": OrderStatus.FILLED,
                "CANCELLED": OrderStatus.CANCELLED,
                "CANCELED": OrderStatus.CANCELLED,
                "EXPIRED": OrderStatus.EXPIRED,
                "REJECTED": OrderStatus.REJECTED,
            }
            status = status_map.get(status_str, OrderStatus.PENDING)

            side_str = api_order.get("side", "").upper()
            side = OrderSide.BUY if side_str == "BUY" else OrderSide.SELL
            token_id = api_order.get("asset_id", api_order.get("token_id", ""))

            original_size = float(api_order.get("original_size", api_order.get("size", 0)))
            matched = float(api_order.get("size_matched", 0))
            remaining_size = float(api_order.get("remaining_size", original_size - matched))
            price = float(api_order.get("price", 0))

            if order_id in self._pending_orders:
                order = self._pending_orders[order_id]
                order.status = status
                order.remaining_size = remaining_size
                order.updated_at = datetime.utcnow()
            else:
                order = OrderState(
                    order_id=order_id,
                    token_id=token_id,
                    side=side,
                    price=price,
                    original_size=original_size,
                    remaining_size=remaining_size,
                    status=status,
                )
                self._pending_orders[order_id] = order

        for order_id, order in list(self._pending_orders.items()):
            if order.is_open() and order_id not in api_order_ids:
                logger.warning(
                    f"Reconcile: Order {order_id} not in API response, "
                    f"marking as cancelled"
                )
                order.status = OrderStatus.CANCELLED
                order.updated_at = datetime.utcnow()

    def remove_order(self, order_id: str) -> Optional[OrderState]:
        """Remove an order from tracking."""
        return self._pending_orders.pop(order_id, None)

    def clear_terminal_orders(self) -> int:
        """
        Remove orders in terminal states (filled, cancelled, expired, rejected).

        Returns:
            Number of orders removed
        """
        to_remove = [
            order_id
            for order_id, order in self._pending_orders.items()
            if order.is_done()
        ]

        for order_id in to_remove:
            del self._pending_orders[order_id]

        return len(to_remove)

    # --- Cancel/Replace (Hysteresis) ---

    async def replace_quote(
        self,
        old_quote: Quote,
        new_quote: Quote,
        neg_risk: bool = False,
    ) -> Tuple[OrderResult, OrderResult]:
        """
        Replace existing quote with new one.

        Cancels old orders and places new ones.

        Args:
            old_quote: The existing quote to replace
            new_quote: The new quote to place
            neg_risk: Whether this is a negative risk market

        Returns:
            Tuple of (bid_result, ask_result) for new orders
        """
        # Cancel existing orders for this token
        await self.cancel_all_for_token(old_quote.token_id)

        # Small delay to ensure cancellation is processed
        await asyncio.sleep(0.1)

        # Place new quote
        return await self.place_quote(new_quote, neg_risk=neg_risk)

    # --- Utility Methods ---

    def get_open_order_count(self, token_id: Optional[str] = None) -> int:
        """Get count of open orders."""
        return len(self.get_pending_orders(token_id))

    def get_open_order_size(
        self,
        token_id: str,
        side: Optional[OrderSide] = None,
    ) -> float:
        """Get total size of open orders for a token/side."""
        orders = self.get_pending_orders(token_id)

        if side:
            orders = [o for o in orders if o.side == side]

        return sum(o.remaining_size for o in orders)
